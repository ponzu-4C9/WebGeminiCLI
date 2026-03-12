from __future__ import annotations

from pathlib import Path
import traceback

from agent_loop import AgentLoop
from gemini_web import GeminiWebClient
from tool_fs import FileSystemTools
from tool_shell import PowerShellTool


def read_user_request() -> str:
    print("リクエストを入力してください。終了するには、単独の行に EOF と入力してください。")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "EOF":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main() -> None:
    workspace_root = Path(__file__).resolve().parent
    client = GeminiWebClient(timeout_minutes=5)
    fs_tools = FileSystemTools(workspace_root)
    shell_tool = PowerShellTool(workspace_root)
    loop = AgentLoop(client=client, fs_tools=fs_tools, shell_tool=shell_tool)

    try:
        print("[起動] main: client.connect 開始")
        client.connect()
        print("[起動] main: client.connect 完了")
        print("現在のGeminiチャットに古いコンテキストがある場合は、まずブラウザで新しいチャットを開いてください。")
        input("ブラウザの準備ができたら Enter キーを押してください...")
        ready_message = loop.bootstrap()
        print(f"エージェントの起動レスポンス: {ready_message}")

        while True:
            print("\nコマンド: 終了するには /exit と入力してください")
            request = read_user_request()
            if not request:
                continue
            if request.strip().lower() == "/exit":
                break

            final_message = loop.run(request)
            print("\n=== 最終回答 ===")
            print(final_message)
    except KeyboardInterrupt:
        print("\n中断されました。")
        traceback.print_exc()
    except Exception:
        print("\n未処理例外が発生しました。")
        traceback.print_exc()
    finally:
        answer = input("\nブラウザを閉じますか？ [Y/n]: ").strip().lower()
        if answer not in {"n", "no"}:
            client.close()


if __name__ == "__main__":
    main()