from __future__ import annotations

import re
import shlex
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
        r"dir(?:\s+.+)?",
        r"tree(?:\s+.+)?",
        r"type\s+.+",
        r"Get-Content\s+.+",
        r"Select-String\s+.+",
        r"git\s+status(?:\s+.*)?",
        r"git\s+diff(?:\s+.*)?",
        r"git\s+log(?:\s+.*)?",
    ]

    def __init__(self, workspace_root: Path, default_timeout: int = 30) -> None:
        self.workspace_root = workspace_root.resolve()
        self.default_timeout = default_timeout

    def _resolve_workspace_path(self, raw_path: str) -> Path:
        cleaned = raw_path.strip().strip('"').strip("'")
        if not cleaned:
            cleaned = "."
        candidate = (self.workspace_root / cleaned).resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as error:
            raise PermissionError("Path must stay inside the workspace.") from error
        return candidate

    def _build_result(self, command: str, stdout: str, stderr: str = "", exit_code: int = 0, timed_out: bool = False) -> dict[str, Any]:
        return {
            "command": command,
            "exit_code": exit_code,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "timed_out": timed_out,
        }

    def _run_dir(self, command: str) -> dict[str, Any]:
        path_text = command[3:].strip() or "."
        directory = self._resolve_workspace_path(path_text)
        if not directory.exists():
            raise FileNotFoundError(f"Path not found: {path_text}")
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {path_text}")

        lines = [f"Directory: {directory}", ""]
        for entry in sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            entry_type = "d" if entry.is_dir() else "-"
            size_text = "" if entry.is_dir() else str(entry.stat().st_size)
            lines.append(f"{entry_type} {entry.name} {size_text}".rstrip())
        return self._build_result(command, "\n".join(lines) + "\n")

    def _render_tree(self, directory: Path, prefix: str = "") -> list[str]:
        entries = sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        lines: list[str] = []
        for index, entry in enumerate(entries):
            connector = "└── " if index == len(entries) - 1 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}")
            if entry.is_dir():
                child_prefix = f"{prefix}{'    ' if index == len(entries) - 1 else '│   '}"
                lines.extend(self._render_tree(entry, child_prefix))
        return lines

    def _run_tree(self, command: str) -> dict[str, Any]:
        path_text = command[4:].strip() or "."
        directory = self._resolve_workspace_path(path_text)
        if not directory.exists():
            raise FileNotFoundError(f"Path not found: {path_text}")
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {path_text}")

        lines = [str(directory)]
        lines.extend(self._render_tree(directory))
        return self._build_result(command, "\n".join(lines) + "\n")

    def _parse_get_content_paths(self, command: str) -> list[Path]:
        path_match = re.search(r"-Path\s+(?P<paths>.+)$", command, flags=re.IGNORECASE)
        if not path_match:
            raise ValueError("Get-Content must include -Path.")
        raw_paths = [part.strip() for part in path_match.group("paths").split(",")]
        return [self._resolve_workspace_path(path_text) for path_text in raw_paths if path_text]

    def _run_get_content(self, command: str) -> dict[str, Any]:
        paths = self._parse_get_content_paths(command)
        outputs = []
        for path in paths:
            if not path.exists():
                raise FileNotFoundError(f"File not found: {path}")
            if not path.is_file():
                raise IsADirectoryError(f"Not a file: {path}")
            outputs.append(path.read_text(encoding="utf-8"))
        return self._build_result(command, "\n".join(outputs))

    def _run_type(self, command: str) -> dict[str, Any]:
        path_text = command[4:].strip()
        if not path_text:
            raise ValueError("type must include a path.")
        path = self._resolve_workspace_path(path_text)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path_text}")
        if not path.is_file():
            raise IsADirectoryError(f"Not a file: {path_text}")
        return self._build_result(command, path.read_text(encoding="utf-8"))

    def _run_select_string(self, command: str) -> dict[str, Any]:
        path_match = re.search(r"-Path\s+(?P<paths>.+?)\s+-Pattern\s+", command, flags=re.IGNORECASE)
        pattern_match = re.search(r"-Pattern\s+(?P<pattern>.+)$", command, flags=re.IGNORECASE)
        if not path_match or not pattern_match:
            raise ValueError("Select-String must include -Path and -Pattern.")

        raw_pattern = pattern_match.group("pattern").strip().strip('"').strip("'")
        regex = re.compile(raw_pattern)
        raw_paths = [part.strip().strip('"').strip("'") for part in path_match.group("paths").split(",")]

        matches: list[str] = []
        for raw_path in raw_paths:
            normalized = raw_path.replace(".\\", "").replace("\\", "/") or "."
            for file_path in sorted(self.workspace_root.glob(normalized)):
                resolved = file_path.resolve()
                try:
                    resolved.relative_to(self.workspace_root)
                except ValueError as error:
                    raise PermissionError("Path must stay inside the workspace.") from error
                if not resolved.is_file():
                    continue
                for line_number, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), 1):
                    if regex.search(line):
                        relative = resolved.relative_to(self.workspace_root).as_posix()
                        matches.append(f"{relative}:{line_number}: {line}")
        return self._build_result(command, "\n".join(matches) + ("\n" if matches else ""))

    def _run_git(self, command: str, timeout: int) -> dict[str, Any]:
        args = shlex.split(command, posix=False)
        git_args = args[1:]
        if not git_args:
            raise ValueError("git command is empty.")

        extra_config = ["-c", "core.quotepath=false"]
        if git_args[0].lower() == "log":
            extra_config.extend(["-c", "i18n.logOutputEncoding=utf-8"])

        try:
            completed = subprocess.run(
                ["git", *extra_config, *git_args],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return self._build_result(command, error.stdout or "", error.stderr or "", exit_code=-1, timed_out=True)

        return self._build_result(command, completed.stdout or "", completed.stderr or "", exit_code=completed.returncode)

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
                "Only these read-only commands are allowed: dir, tree, Get-Content, type, Select-String, git status, git diff, git log."
            )

        if re.fullmatch(r"Get-Content\s+.+", normalized, flags=re.IGNORECASE):
            if "-encoding utf8" not in lowered:
                raise PermissionError("Get-Content must include -Encoding UTF8.")

    def run(self, command: str, timeout: int | None = None) -> dict[str, Any]:
        self._validate_command(command)
        effective_timeout = timeout or self.default_timeout



        lowered = command.strip().lower()

        if lowered.startswith("dir"):
            return self._run_dir(command)
        if lowered.startswith("tree"):
            return self._run_tree(command)
        if lowered.startswith("get-content"):
            return self._run_get_content(command)
        if lowered.startswith("type"):
            return self._run_type(command)
        if lowered.startswith("select-string"):
            return self._run_select_string(command)
        if lowered.startswith("git "):
            return self._run_git(command, effective_timeout)

        raise PermissionError("Unsupported command.")