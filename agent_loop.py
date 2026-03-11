from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from gemini_web import GeminiWebClient
from tool_fs import FileSystemTools
from tool_shell import PowerShellTool


SYSTEM_PROMPT = """You are a constrained coding agent operating through a browser chat.
You must respond with raw JSON only. Do not use Markdown. Do not wrap JSON in code fences.
You have exactly one action per response.

Allowed actions:
1. {"action":"list_dir","path":"relative/path"}
2. {"action":"read_file","path":"relative/path","start_line":1,"end_line":200}
3. {"action":"edit_file","path":"relative/path","old_text":"exact old text","new_text":"replacement text"}
4. {"action":"run_shell","command":"Read-only shell command"}
5. {"action":"final","message":"answer to the user"}

Rules:
- Stay inside the current workspace.
- Use relative paths only.
- Prefer one small safe step at a time.
- For edit_file, old_text must match exactly once in the file.
- For edit_file, old_text and new_text must be valid JSON strings.
- Escape all inner double quotes inside old_text and new_text as \".
- Example valid edit_file JSON: {"action":"edit_file","path":"main.py","old_text":"print(\"hello\")","new_text":"print(\"hello world\")"}
- File changes are allowed only through edit_file.
- run_shell is read-only only. Prefer dir, type, rg, git status, git diff, git log, git show, git branch, git rev-parse.
- Never use python, py, powershell, pwsh, cmd, base64, redirection, pipes, or shell chaining.
- Do not ask to use tools you do not have.
- Do not output explanations outside the JSON object.
- When enough information is gathered, return final.
"""


@dataclass
class AgentAction:
    action: str
    payload: dict[str, Any]


class AgentLoop:
    def __init__(
        self,
        client: GeminiWebClient,
        fs_tools: FileSystemTools,
        shell_tool: PowerShellTool,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        max_steps: int = 12,
    ) -> None:
        self.client = client
        self.fs_tools = fs_tools
        self.shell_tool = shell_tool
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.max_steps = max_steps
        self._bootstrapped = False

    def bootstrap(self) -> str:
        prompt = (
            SYSTEM_PROMPT
            + "\nReply now with {\"action\":\"final\",\"message\":\"READY\"}."
        )
        self._log("bootstrap prompt prepared")
        response = self.client.send(prompt, prompt_label="system")
        self._log(f"bootstrap raw response: {self._preview_text(response, 800)}")
        parsed = self._parse_response(response)
        if parsed.action != "final":
            raise RuntimeError("Bootstrap did not return final action.")
        self._bootstrapped = True
        return str(parsed.payload.get("message", ""))

    def run(self, user_request: str) -> str:
        if not self._bootstrapped:
            raise RuntimeError("Agent session is not bootstrapped.")

        next_prompt = self._build_user_prompt(user_request)

        for step in range(1, self.max_steps + 1):
            self._log(f"step={step} request prompt: {self._preview_text(next_prompt, 1000)}")
            response = self.client.send(next_prompt, prompt_label=f"step-{step}")
            self._log(f"step={step} raw response: {self._preview_text(response, 1200)}")
            try:
                parsed = self._parse_response(response)
            except Exception as error:
                self._log(f"step={step} parse error: {error}")
                next_prompt = self._build_repair_prompt(str(error), response)
                self._log(f"step={step} repair prompt: {self._preview_text(next_prompt, 1200)}")
                continue

            self._log(f"step={step} parsed action={parsed.action} payload={json.dumps(parsed.payload, ensure_ascii=False)}")
            if parsed.action == "final":
                return str(parsed.payload.get("message", ""))

            try:
                tool_result = self._dispatch(parsed)
            except Exception as error:
                self._log(f"step={step} tool error type={type(error).__name__} message={error}")
                tool_result = {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "action": parsed.action,
                }
            self._log(f"step={step} tool result={self._preview_text(json.dumps(tool_result, ensure_ascii=False), 1200)}")
            next_prompt = self._build_tool_result_prompt(parsed, tool_result)

        raise RuntimeError("Agent exceeded the maximum number of steps.")

    def _build_user_prompt(self, user_request: str) -> str:
        return (
            "User request:\n"
            f"{user_request}\n\n"
            "Choose the next single JSON action."
        )

    def _build_repair_prompt(self, error_message: str, raw_response: str) -> str:
        return (
            "Your last response was invalid.\n"
            f"Error: {error_message}\n"
            f"Raw response:\n{raw_response}\n\n"
            "Reminder: return strict JSON only.\n"
            "If old_text or new_text contains code like print(\"...\"), escape inner double quotes as \\\" inside the JSON string.\n"
            "Do not use unescaped double quotes inside JSON string values.\n\n"
            "Return exactly one valid JSON action and nothing else."
        )

    def _build_tool_result_prompt(self, action: AgentAction, result: dict[str, Any]) -> str:
        result_json = json.dumps(result, ensure_ascii=False)
        return (
            "Tool result:\n"
            f"action={action.action}\n"
            f"request={json.dumps(action.payload, ensure_ascii=False)}\n"
            f"result={result_json}\n\n"
            "Choose the next single JSON action."
        )

    def _dispatch(self, action: AgentAction) -> dict[str, Any]:
        if action.action == "list_dir":
            self._log(f"dispatch list_dir path={action.payload.get('path', '.')}")
            return self.fs_tools.list_dir(str(action.payload.get("path", ".")))

        if action.action == "read_file":
            self._log(
                f"dispatch read_file path={action.payload['path']} start={int(action.payload.get('start_line', 1))} end={int(action.payload.get('end_line', 200))}"
            )
            return self.fs_tools.read_file(
                relative_path=str(action.payload["path"]),
                start_line=int(action.payload.get("start_line", 1)),
                end_line=int(action.payload.get("end_line", 200)),
            )

        if action.action == "run_shell":
            self._log(f"dispatch run_shell command={action.payload['command']}")
            return self.shell_tool.run(str(action.payload["command"]))

        if action.action == "edit_file":
            path = str(action.payload["path"])
            old_text = str(action.payload.get("old_text", ""))
            new_text = str(action.payload.get("new_text", ""))
            preview = self.fs_tools.preview_patch(path, old_text, new_text)
            if preview["match_start_line"] is not None:
                self._log(
                    f"dispatch edit_file path={path} target_lines={preview['match_start_line']}-{preview['match_end_line']} old_len={len(old_text)} new_len={len(new_text)}"
                )
            else:
                self._log(f"dispatch edit_file path={path} new_file_create old_len={len(old_text)} new_len={len(new_text)}")
            self.output_fn("\n--- diff preview ---")
            self.output_fn(preview["diff"] or "(no changes)")
            answer = self.input_fn("Apply this patch? [y/N]: ").strip().lower()
            if answer != "y":
                return {"path": path, "applied": False, "reason": "user_rejected"}
            applied = self.fs_tools.apply_patch(path, old_text, new_text)
            return {"path": path, "applied": True, "diff": applied["diff"]}

        raise ValueError(f"Unsupported action: {action.action}")

    def _parse_response(self, response_text: str) -> AgentAction:
        json_text = self._extract_json_object(response_text)
        self._log(f"json candidate: {self._preview_text(json_text, 1200)}")
        payload = json.loads(json_text)

        if not isinstance(payload, dict):
            raise ValueError("Response JSON must be an object.")
        action = payload.get("action")
        if action not in {"list_dir", "read_file", "edit_file", "run_shell", "final"}:
            raise ValueError("Response contains an unknown action.")
        return AgentAction(action=action, payload=payload)

    def _extract_json_object(self, text: str) -> str:
        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced_match:
            return fenced_match.group(1)

        start = text.find("{")
        if start == -1:
            raise ValueError("No JSON object found in response.")

        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]

        raise ValueError("Incomplete JSON object in response.")

    def _log(self, message: str) -> None:
        self.output_fn(f"[agent-debug] {message}")

    def _preview_text(self, text: str, limit: int = 300) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit] + "..."