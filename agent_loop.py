from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from gemini_web import GeminiWebClient
from tool_fs import FileSystemTools
from tool_shell import PowerShellTool


SYSTEM_PROMPT = """You are a constrained coding agent operating through a browser chat.
You must output exactly one JSON action per response.

Allowed actions:
1. {"action":"list_dir","path":"relative/path"}
2. {"action":"read_file","path":"relative/path"}
3. {"action":"run_shell","command":"Read-only shell command"}
4. {"action":"final","message":"answer to the user"}

5. edit_file is SPECIAL. To avoid JSON escaping issues with multi-line code, output the JSON with ONLY the path, start_line, and end_line. Then put the replacement code OUTSIDE the JSON in a standard Markdown code block:
{"action":"edit_file","path":"main.py","start_line":10,"end_line":15}
```python
replacement text here
```

Rules:
- Stay inside the current workspace.
- Use relative paths only.
- Prefer one small safe step at a time.
- read_file returns the entire file with line numbers. Use these exact line numbers for edit_file.
- For edit_file, start_line and end_line are inclusive (1-based). To insert without deleting, use start_line = N, end_line = N-1.
- File changes are allowed only through edit_file.
- run_shell is read-only only. Prefer dir, type, rg, git status, git diff, git log, git show, git branch, git rev-parse.
- Never use python, py, powershell, pwsh, cmd, base64, redirection, pipes, or shell chaining.
- Do not ask to use tools you do not have.
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
            "Your last response was INVALID.\n"
            f"Error: {error_message}\n"
            f"Raw response:\n{raw_response}\n\n"
            "*** CRITICAL FIX REQUIRED ***\n"
            "If you are trying to use edit_file, output the JSON with start_line and end_line, then use a Markdown code block outside the JSON. Example:\n"
            "{\"action\":\"edit_file\", \"path\":\"main.py\", \"start_line\": 10, \"end_line\": 12}\n"
            "```python\nprint(\"New\")\n```\n\n"
            "Return exactly one valid action."
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
            self._log(f"dispatch read_file path={action.payload['path']}")
            return self.fs_tools.read_file(str(action.payload["path"]))

        if action.action == "run_shell":
            self._log(f"dispatch run_shell command={action.payload['command']}")
            return self.shell_tool.run(str(action.payload["command"]))

        if action.action == "edit_file":
            path = str(action.payload["path"])
            start_line = int(action.payload.get("start_line", 1))
            end_line = int(action.payload.get("end_line", start_line))
            new_text = str(action.payload.get("new_text", ""))
            
            preview = self.fs_tools.preview_patch(path, start_line, end_line, new_text)
            self._log(
                f"dispatch edit_file path={path} target_lines={start_line}-{end_line} new_len={len(new_text)}"
            )
            self.output_fn("\n--- diff preview ---")
            self.output_fn(preview["diff"] or "(no changes)")
            answer = self.input_fn("Apply this patch? [y/N]: ").strip().lower()
            if answer != "y":
                return {"path": path, "applied": False, "reason": "user_rejected"}
            applied = self.fs_tools.apply_patch(path, start_line, end_line, new_text)
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
            
        if action == "edit_file":
            new_text_match = re.search(r"```(?:python|py|txt|json|html|css|js|ts)?\n?(.*?)\n?```", response_text[len(json_text):], re.DOTALL)
            if new_text_match:
                payload["new_text"] = new_text_match.group(1)
            elif "new_text" not in payload:
                raise ValueError("edit_file requires a Markdown code block outside the JSON.")
            if "start_line" not in payload or "end_line" not in payload:
                raise ValueError("edit_file requires start_line and end_line in JSON.")

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