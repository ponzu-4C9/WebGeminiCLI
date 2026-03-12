from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from gemini_web import GeminiWebClient
from tool_fs import FileSystemTools
from tool_shell import PowerShellTool


SYSTEM_PROMPT = """あなたは、ブラウザ上の Gemini を通じて動作する制約付きコーディングエージェントです。
返答は必ず日本語で行い、毎回ちょうど1つの行動だけを返してください。
返答は必ずプレーンテキストのみで行ってください。
Markdown記号（#や*）を使わず、プレーンテキストのみで回答してください。
Markdownの見出し、箇条書き、コードフェンス、装飾記法は禁止です。

許可される行動は次の3種類だけです。

1. 読み取り専用コマンドを1行で返す。
許可されるコマンドは以下のみです。
- dir
- tree
- Get-Content
- type
- Select-String
- git status
- git diff
- git log

使用例:
- dir .
- dir .\subdir
- tree .
- Get-Content -Encoding UTF8 -Path .\main.py
- Get-Content -Encoding UTF8 -Path .\main.py, .\tool_fs.py
- type .\main.py
- Select-String -Path .\*.py -Pattern "SYSTEM_PROMPT"
- git status
- git diff
- git log --oneline -5

重要ルール:
- Get-Content を使うときは、必ず -Encoding UTF8 を付けること。
- 複数ファイルを読む場合は、Get-Content -Encoding UTF8 -Path .\main.py, .\tool_fs.py のように指定してよい。
- 上記以外のコマンド、PowerShell 構文、リダイレクト、パイプ、連結はすべて禁止です。
- ワークスペース外へ出る行動は禁止です。

2. ファイル編集を行う。
編集は editFile だけを使います。行番号指定は禁止です。
形式は以下に厳密に従ってください。

editFile path=.\main.py
<<<<<<< SEARCH
元のテキスト
=======
新しいテキスト
>>>>>>> REPLACE

editFile のルール:
- path は相対パスのみ。
- editFile の次の行から、すぐに SEARCH/REPLACE ブロックを書いてください。
- コードフェンスは不要です。使わないでください。
- editFile の直後に書いてよいのは SEARCH/REPLACE ブロックだけです。Plaintext などの見出しや説明文を挟んではいけません。
- 1回の editFile 応答の中に SEARCH/REPLACE を複数書いてよい。
- SEARCH は既存ファイル内の断片を示す。
- SEARCH が空の場合は、REPLACE をファイル先頭へ挿入する命令として扱う。
- 置換処理はインデントや改行の多少の揺れを許容する。
- 編集前には、必要なファイルを読み取りコマンドで確認すること。

3. 最終回答を返す。
形式:

final
ユーザーへの最終回答本文

追加ルール:
- 毎回、上記3種類のどれか1つだけを返すこと。
- 説明だけの途中応答や余計な前置きは禁止です。
- 許可されていない行動は絶対にしてはいけません。
- まずは安全に情報を読み、十分な情報が集まったら final を返してください。
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
            + "\n準備ができたら、以下の形式で返答してください。\nfinal\nREADY"
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
            "ユーザー依頼:\n"
            f"{user_request}\n\n"
            "重要: Markdown記号（#や*）を使わず、プレーンテキストのみで回答してください。\n"
            "次に実行する1つの行動だけを、許可された形式で返してください。"
        )

    def _build_repair_prompt(self, error_message: str, raw_response: str) -> str:
        return (
            "前回の返答は無効でした。\n"
            f"エラー: {error_message}\n"
            f"前回の返答:\n{raw_response}\n\n"
            "重要: Markdown記号（#や*）を使わず、プレーンテキストのみで回答してください。\n"
            "次は必ず許可された形式の1つだけを返してください。\n"
            "例1:\nGet-Content -Encoding UTF8 -Path .\\main.py, .\\tool_fs.py\n\n"
            "例2:\neditFile path=.\\main.py\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n\n"
            "例2の注意:\n- editFile の次の行は必ず <<<<<<< SEARCH にすること\n- Plaintext などの説明文を入れないこと\n- 空の SEARCH は先頭挿入を意味する\n\n"
            "例3:\nfinal\n完了しました"
        )

    def _build_tool_result_prompt(self, action: AgentAction, result: dict[str, Any]) -> str:
        result_json = json.dumps(result, ensure_ascii=False)
        return (
            "ツール実行結果:\n"
            f"action={action.action}\n"
            f"request={json.dumps(action.payload, ensure_ascii=False)}\n"
            f"result={result_json}\n\n"
            "重要: Markdown記号（#や*）を使わず、プレーンテキストのみで回答してください。\n"
            "次に実行する1つの行動だけを、許可された形式で返してください。"
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
            instructions = str(action.payload.get("instructions", ""))
            preview = self.fs_tools.preview_edit_instructions(path, instructions)
            self._log(f"dispatch edit_file path={path} instruction_len={len(instructions)}")
            self.output_fn("\n--- diff preview ---")
            self.output_fn(preview["diff"] or "(no changes)")
            answer = self.input_fn("Apply this patch? [y/N]: ").strip().lower()
            if answer != "y":
                return {"path": path, "applied": False, "reason": "user_rejected"}
            applied = self.fs_tools.apply_edit_instructions(path, instructions)
            return {"path": path, "applied": True, "diff": applied["diff"]}

        raise ValueError(f"Unsupported action: {action.action}")

    def _parse_response(self, response_text: str) -> AgentAction:
        text = response_text.strip()
        if not text:
            raise ValueError("空の返答は無効です。")

        final_match = re.match(r"^final\s*(?:\r?\n(?P<message>[\s\S]*))?$", text)
        if final_match:
            return AgentAction(
                action="final",
                payload={"message": (final_match.group("message") or "").strip()},
            )

        edit_match = re.match(r"^editFile\s+path=(?P<path>\S+)", text)
        if edit_match:
            block_match = re.search(r"```(?:text|python|py|txt)?\r?\n([\s\S]*?)\r?\n```", text)
            instructions = block_match.group(1) if block_match else ""
            if not instructions:
                fallback_match = re.search(
                    r"(?:(?:Plaintext|text|python|py|txt)\s+)?(<<<<<<< SEARCH[\s\S]*?>>>>>>> REPLACE)",
                    text,
                    re.IGNORECASE,
                )
                if fallback_match:
                    instructions = fallback_match.group(1)
            if not instructions:
                raise ValueError("editFile には SEARCH/REPLACE ブロックが必要です。")
            return AgentAction(
                action="edit_file",
                payload={
                    "path": edit_match.group("path").strip(),
                    "instructions": instructions,
                },
            )

        first_line = text.splitlines()[0].strip()
        if len(text.splitlines()) > 1:
            raise ValueError("コマンド応答は1行のみ、editFile または final は専用形式で返してください。")
        return AgentAction(action="run_shell", payload={"command": first_line})

    def _log(self, message: str) -> None:
        self.output_fn(f"[agent-debug] {message}")

    def _preview_text(self, text: str, limit: int = 300) -> str:
        return text