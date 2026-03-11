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

    def read_file(self, relative_path: str, start_line: int = 1, end_line: int = 200) -> dict[str, Any]:
        if start_line < 1 or end_line < start_line:
            raise ValueError("Invalid line range.")

        file_path = self._resolve_path(relative_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {relative_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Not a file: {relative_path}")

        lines = file_path.read_text(encoding="utf-8").splitlines()
        selected = lines[start_line - 1:end_line]
        return {
            "path": relative_path,
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "line_count": len(lines),
            "content": "\n".join(selected),
        }

    def preview_patch(self, relative_path: str, old_text: str, new_text: str) -> dict[str, Any]:
        file_path = self._resolve_path(relative_path)
        match_start_line = None
        match_end_line = None

        if file_path.exists():
            if not file_path.is_file():
                raise IsADirectoryError(f"Not a file: {relative_path}")
            before_content = file_path.read_text(encoding="utf-8")
            if old_text not in before_content:
                raise ValueError("old_text was not found in the target file.")
            occurrences = before_content.count(old_text)
            if occurrences != 1:
                raise ValueError(f"old_text must match exactly once. Found {occurrences} matches.")
            match_index = before_content.index(old_text)
            match_start_line = before_content[:match_index].count("\n") + 1
            match_end_line = match_start_line + old_text.count("\n")
            after_content = before_content.replace(old_text, new_text, 1)
        else:
            if old_text:
                raise FileNotFoundError("Target file does not exist and old_text is not empty.")
            before_content = ""
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
            "match_start_line": match_start_line,
            "match_end_line": match_end_line,
        }

    def apply_patch(self, relative_path: str, old_text: str, new_text: str) -> dict[str, Any]:
        preview = self.preview_patch(relative_path, old_text, new_text)
        file_path = self._resolve_path(relative_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(preview["new_content"], encoding="utf-8")
        return {"path": relative_path, "diff": preview["diff"]}