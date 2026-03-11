from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


class PowerShellTool:
    BLOCKED_PATTERNS = [
        " python",
        " py ",
        " powershell",
        " pwsh",
        " cmd ",
        " base64",
        " b64decode",
        " frombase64string",
        " certutil",
        " invoke-expression",
        " iex ",
        " exec(",
        " os.system",
        " subprocess",
        " start-process",
        " remove-item",
        " del ",
        " erase ",
        " rd ",
        " rmdir",
        " move-item",
        " rename-item",
        " copy-item",
        " new-item",
        " set-content",
        " add-content",
        " out-file",
        " sc ",
        " git reset",
        " git checkout",
        " git clean",
        " git clone",
        " curl ",
        " invoke-webrequest",
        " invoke-restmethod",
        " wget ",
        " irm ",
        " iwr ",
        " pip ",
        " pip3 ",
        " npm ",
        " pnpm ",
        " yarn ",
    ]
    BLOCKED_CHARS = [">", "<", "|", "&", ";"]
    ALLOWED_COMMAND_PATTERNS = [
        r"dir(?:\s+.*)?",
        r"type\s+.+",
        r"rg(?:\.exe)?(?:\s+.*)?",
        r"git\s+status(?:\s+.*)?",
        r"git\s+diff(?:\s+.*)?",
        r"git\s+log(?:\s+.*)?",
        r"git\s+show(?:\s+.*)?",
        r"git\s+branch(?:\s+.*)?",
        r"git\s+rev-parse(?:\s+.*)?",
        r"get-childitem(?:\s+.*)?",
        r"get-content(?:\s+.*)?",
    ]

    def __init__(self, workspace_root: Path, default_timeout: int = 30) -> None:
        self.workspace_root = workspace_root.resolve()
        self.default_timeout = default_timeout

    def _validate_command(self, command: str) -> None:
        normalized = command.strip()
        if not normalized:
            raise PermissionError("Empty command is not allowed.")

        lowered = f" {normalized.lower()} "

        for blocked_char in self.BLOCKED_CHARS:
            if blocked_char in normalized:
                raise PermissionError(f"Blocked shell operator: {blocked_char}")

        for pattern in self.BLOCKED_PATTERNS:
            if pattern in lowered:
                raise PermissionError(f"Blocked command pattern: {pattern.strip()}")

        if not any(re.fullmatch(pattern, normalized, flags=re.IGNORECASE) for pattern in self.ALLOWED_COMMAND_PATTERNS):
            raise PermissionError(
                "Only read-only commands are allowed: dir, type, rg, git status/diff/log/show/branch/rev-parse, Get-ChildItem, Get-Content."
            )

    def run(self, command: str, timeout: int | None = None) -> dict[str, Any]:
        self._validate_command(command)
        effective_timeout = timeout or self.default_timeout

        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or ""
            stderr = error.stderr or ""
            return {
                "command": command,
                "exit_code": None,
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "timed_out": True,
            }

        return {
            "command": command,
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
            "timed_out": False,
        }