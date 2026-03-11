from __future__ import annotations

from pathlib import Path

from agent_loop import AgentLoop
from gemini_web import GeminiWebClient
from tool_fs import FileSystemTools
from tool_shell import PowerShellTool


def read_user_request() -> str:
    print("Enter your request. Finish with EOF on its own line.")
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
        print("[startup] main: client.connect start")
        client.connect()
        print("[startup] main: client.connect done")
        print("If the current Gemini chat has old context, open a fresh chat in the browser first.")
        input("Press Enter when the browser is ready...")
        ready_message = loop.bootstrap()
        print(f"Agent bootstrap response: {ready_message}")

        while True:
            print("\nCommands: /exit to quit")
            request = read_user_request()
            if not request:
                continue
            if request.strip().lower() == "/exit":
                break

            final_message = loop.run(request)
            print("\n=== Final Answer ===")
            print(final_message)
    finally:
        answer = input("\nClose the browser? [Y/n]: ").strip().lower()
        if answer not in {"n", "no"}:
            client.close()


if __name__ == "__main__":
    main()