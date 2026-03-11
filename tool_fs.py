from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any


class FileSystemTools:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def _resolve_path(self, relative_path: str) -> Path:
        candidate = (self.workspace_root / relative_path).resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as error:
            raise ValueError("Path must stay inside the workspace.") from error
        return candidate

    def list_dir(self, relative_path: str = ".") -> dict[str, Any]:
        directory = self._resolve_path(relative_path)
        if not directory.exists():
            raise FileNotFoundError(f"Directory not found: {relative_path}")
        if not directory.is_dir():
            raise NotADirectoryError(f"Not a directory: {relative_path}")

        entries = []
        for entry in sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            entries.append(
                {
                    "name": entry.name,
                    "path": str(entry.relative_to(self.workspace_root)).replace("\\", "/"),
                    "type": "dir" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else None,
                }
            )

        return {"path": relative_path, "entries": entries}

    def read_file(self, relative_path: str) -> dict[str, Any]:
        file_path = self._resolve_path(relative_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {relative_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Not a file: {relative_path}")

        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        numbered_lines = [f"{i+1:4d} | {line}" for i, line in enumerate(lines)]

        return {
            "path": relative_path,
            "line_count": len(lines),
            "content": "\n".join(numbered_lines),
        }

    def preview_patch(self, relative_path: str, start_line: int, end_line: int, new_text: str) -> dict[str, Any]:
        file_path = self._resolve_path(relative_path)
        
        if file_path.exists():
            if not file_path.is_file():
                raise IsADirectoryError(f"Not a file: {relative_path}")
            before_content = file_path.read_text(encoding="utf-8")
        else:
            before_content = ""

        lines = before_content.splitlines()
        
        start_idx = max(0, start_line - 1)
        end_idx = max(0, min(len(lines), end_line))
        
        before_lines = lines[:start_idx]
        after_lines = lines[end_idx:]
        
        new_lines = new_text.splitlines() if new_text else []
        
        final_lines = before_lines + new_lines + after_lines
        after_content = "\n".join(final_lines)
        if final_lines and before_content.endswith("\n"):
            after_content += "\n"
        elif not final_lines and new_text:
            after_content = new_text

        diff_lines = difflib.unified_diff(
            before_content.splitlines(),
            after_content.splitlines(),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            lineterm="",
        )

        return {
            "path": relative_path,
            "diff": "\n".join(diff_lines),
            "new_content": after_content,
            "match_start_line": start_line,
            "match_end_line": end_line,
        }

    def apply_patch(self, relative_path: str, start_line: int, end_line: int, new_text: str) -> dict[str, Any]:
        preview = self.preview_patch(relative_path, start_line, end_line, new_text)
        file_path = self._resolve_path(relative_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(preview["new_content"], encoding="utf-8")
        return {"path": relative_path, "diff": preview["diff"]}