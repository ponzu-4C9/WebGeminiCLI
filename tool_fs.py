from __future__ import annotations

import difflib
import re
import textwrap
from pathlib import Path
from typing import Any


class FileSystemTools:
    EDIT_BLOCK_PATTERN = re.compile(
        r"<<<<<<< SEARCH\n(?P<search>[\s\S]*?)\n?=======\n(?P<replace>[\s\S]*?)\n?>>>>>>> REPLACE",
        re.MULTILINE,
    )

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

    def preview_edit_instructions(self, relative_path: str, instructions: str) -> dict[str, Any]:
        file_path = self._resolve_path(relative_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {relative_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Not a file: {relative_path}")

        before_content = file_path.read_text(encoding="utf-8")
        after_content = self._apply_edit_instructions_to_content(before_content, instructions)
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
        }

    def apply_edit_instructions(self, relative_path: str, instructions: str) -> dict[str, Any]:
        preview = self.preview_edit_instructions(relative_path, instructions)
        file_path = self._resolve_path(relative_path)
        file_path.write_text(preview["new_content"], encoding="utf-8")
        return {"path": relative_path, "diff": preview["diff"]}

    def _apply_edit_instructions_to_content(self, content: str, instructions: str) -> str:
        normalized_instructions = self._normalize_newlines(instructions).strip()
        matches = list(self.EDIT_BLOCK_PATTERN.finditer(normalized_instructions))
        if not matches:
            raise ValueError("editFile には SEARCH/REPLACE ブロックが必要です。")

        updated_content = self._normalize_newlines(content)
        for match in matches:
            search_text = match.group("search")
            replace_text = match.group("replace")
            updated_content = self._replace_once(updated_content, search_text, replace_text)

        if content.endswith("\r\n"):
            return updated_content.replace("\n", "\r\n")
        return updated_content

    def _replace_once(self, content: str, search_text: str, replace_text: str) -> str:
        normalized_search = self._normalize_newlines(search_text).strip("\n")
        if not normalized_search.strip():
            replacement = self._reindent_replacement(
                replace_text,
                "",
                "",
                False,
            )
            if not replacement:
                return content
            separator = "\n" if content and not replacement.endswith("\n") else ""
            return replacement + separator + content

        pattern = self._build_flexible_pattern(normalized_search)
        regex = re.compile(pattern, re.MULTILINE)
        candidates = list(regex.finditer(content))
        if not candidates:
            raise ValueError("SEARCH ブロックに一致する箇所が見つかりません。")
        if len(candidates) > 1:
            raise ValueError("SEARCH ブロックに一致する箇所が複数あります。より具体的に指定してください。")

        start, end = candidates[0].span()
        matched_text = candidates[0].group(0)
        base_indent, first_line_has_existing_indent = self._detect_match_indent(content, start)
        replacement = self._reindent_replacement(
            replace_text,
            matched_text,
            base_indent,
            first_line_has_existing_indent,
        )
        return content[:start] + replacement + content[end:]

    def _build_flexible_pattern(self, search_text: str) -> str:
        parts = re.split(r"(\s+)", search_text)
        pattern_parts: list[str] = []
        for part in parts:
            if not part:
                continue
            if part.isspace():
                pattern_parts.append(r"\s+")
            else:
                pattern_parts.append(re.escape(part))
        return "".join(pattern_parts)

    def _reindent_replacement(
        self,
        replacement_text: str,
        matched_text: str,
        base_indent: str,
        first_line_has_existing_indent: bool,
    ) -> str:
        normalized_replacement = self._normalize_newlines(replacement_text)
        stripped_replacement = normalized_replacement.strip("\n")
        if not stripped_replacement:
            return ""

        dedented = textwrap.dedent(stripped_replacement)
        adjusted_lines = []
        for index, line in enumerate(dedented.split("\n")):
            if line:
                if index == 0 and first_line_has_existing_indent:
                    adjusted_lines.append(line)
                else:
                    adjusted_lines.append(base_indent + line)
            else:
                adjusted_lines.append("")
        return "\n".join(adjusted_lines)

    def _detect_indent(self, text: str) -> str:
        for line in self._normalize_newlines(text).split("\n"):
            if line.strip():
                return re.match(r"^[ \t]*", line).group(0)
        return ""

    def _detect_match_indent(self, content: str, start_index: int) -> tuple[str, bool]:
        line_start = content.rfind("\n", 0, start_index) + 1
        prefix = content[line_start:start_index]
        if prefix.strip():
            return self._detect_indent(content[start_index:]), False
        return prefix, bool(prefix)

    def _normalize_newlines(self, text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")