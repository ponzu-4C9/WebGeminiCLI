import time
from dataclasses import dataclass
from typing import Any, Optional

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from webcreation import ensure_pro_mode, setup_driver, wait_for_user_login


@dataclass
class GeminiWebClient:
    timeout_minutes: int = 5
    completion_grace_seconds: int = 3
    driver: Optional[Any] = None

    def connect(self) -> None:
        print("[startup] connect: begin")
        if self.driver is None:
            print("[startup] connect: setup_driver start")
            self.driver = setup_driver()
            print("[startup] connect: setup_driver done")
        print("[startup] connect: wait_for_user_login start")
        wait_for_user_login(self.driver)
        print("[startup] connect: wait_for_user_login done")
        print("[startup] connect: ensure_pro_mode start")
        ensure_pro_mode(self.driver)
        print("[startup] connect: ensure_pro_mode done")

    def send(self, prompt: str, prompt_label: str = "agent") -> str:
        if self.driver is None:
            raise RuntimeError("Driver is not connected.")
        return self._ask_normal_chat(
            prompt,
            prompt_label=prompt_label,
            timeout_minutes=self.timeout_minutes,
        )

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def _ask_normal_chat(self, prompt: str, prompt_label: str, timeout_minutes: int) -> str:
        label = f"[{prompt_label}] " if prompt_label else ""
        print(f"\n{label}プロンプトを送信準備中...")
        print(f"{label}[debug] prompt_length={len(prompt)}")
        print(f"{label}[debug] prompt_full_start")
        print(prompt)
        print(f"{label}[debug] prompt_full_end")

        print(f"{label}[debug] wait_for_input_box start")
        input_box = self._wait_for_input_box(timeout=30)
        print(f"{label}[debug] wait_for_input_box done")
        initial_count = self._count_responses()
        print(f"{label}[debug] initial_response_count={initial_count}")

        for attempt in range(2):
            try:
                ActionChains(self.driver).move_to_element(input_box).click().perform()
                print(f"{label}[debug] input_box click succeeded on attempt={attempt + 1}")
                break
            except Exception:
                if attempt == 0:
                    print(f"{label}[debug] input_box click failed on attempt=1, retrying")
                    time.sleep(1)
                    input_box = self._wait_for_input_box(timeout=5)
                else:
                    print(f"{label}[debug] input_box click fallback to execute_script")
                    self.driver.execute_script("arguments[0].click();", input_box)
        time.sleep(0.5)

        try:
            ActionChains(self.driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
        except Exception:
            pass
        time.sleep(0.5)

        self.driver.execute_script(
            """
            let el = arguments[0];
            let target = el.querySelector('.ql-editor') || el.querySelector('p') || el;
            if (!target) target = el;
            target.focus();
            document.execCommand('insertText', false, arguments[1]);
            """,
            input_box,
            prompt,
        )
        time.sleep(1)
        print(f"{label}[debug] prompt inserted via execCommand")

        current_val = self.driver.execute_script("return arguments[0].textContent;", input_box)
        if not current_val or not current_val.strip():
            print(f"  {label}JS入力に失敗したため、キー入力に切り替えます...")
            lines = prompt.split("\n")
            for index, line in enumerate(lines):
                if line:
                    try:
                        ActionChains(self.driver).send_keys(line).perform()
                    except Exception:
                        pass
                if index < len(lines) - 1:
                    try:
                        ActionChains(self.driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()
                    except Exception:
                        pass
            time.sleep(1)
            print(f"{label}[debug] fallback keyboard input complete")
        else:
            print(f"{label}[debug] input_box_text_length={len(current_val)}")

        sent = False
        try:
            send_btn = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button[aria-label*='Send'], button[aria-label*='送信'], button.send-button")
                )
            )
            send_btn.click()
            sent = True
            print(f"{label}[debug] send_button click succeeded")
        except Exception:
            pass

        if not sent:
            try:
                ActionChains(self.driver).key_down(Keys.CONTROL).send_keys(Keys.ENTER).key_up(Keys.CONTROL).perform()
                time.sleep(0.5)
                if self._count_responses() == initial_count:
                    input_box.send_keys(Keys.ENTER)
                sent = True
                print(f"{label}[debug] send fallback keyboard shortcut used")
            except Exception:
                pass

        if not sent:
            raise RuntimeError(f"{label}送信に失敗しました。")

        print(f"{label}プロンプト送信完了。応答を待機中...")

        for waited in range(120):
            current_count = self._count_responses()
            if current_count > initial_count:
                print(f"{label}[debug] response_count changed {initial_count} -> {current_count} after {waited} sec")
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"{label}応答が 120 秒以内に開始されませんでした。")

        timeout_sec = timeout_minutes * 60
        idle_count = 0
        saw_generating_indicator = False
        last_text = ""
        last_indicator_state: Optional[bool] = None

        for elapsed in range(timeout_sec):
            status = self._get_status()
            current_text = status["text"]
            is_generating = status["is_generating"]

            if last_indicator_state is None or last_indicator_state != is_generating:
                state_name = "on" if is_generating else "off"
                print(f"{label}[debug] generating_indicator={state_name} at {elapsed} sec")
                last_indicator_state = is_generating

            if current_text:
                last_text = current_text

            if is_generating:
                saw_generating_indicator = True
                idle_count = 0
            else:
                idle_count += 1
                if last_text and (saw_generating_indicator or idle_count >= self.completion_grace_seconds):
                    if idle_count >= self.completion_grace_seconds:
                        break

            if elapsed > 0 and elapsed % 5 == 0:
                if is_generating:
                    state = "(生成中インジケータあり)"
                else:
                    state = f"(生成停止確認中: {idle_count}/{self.completion_grace_seconds})"
                print(f"  ... {elapsed}秒経過 {state} 現在の文字数: {len(last_text)}")
                print(f"{label}[debug] latest_text_full_start")
                print(last_text)
                print(f"{label}[debug] latest_text_full_end")

            time.sleep(1)
        else:
            print(f"{label}WARNING: {timeout_minutes}分経過しても応答完了を確認できませんでした。現在のテキストを返します。")

        result = self._get_status()["text"] or last_text
        print(f"{label}応答取得完了 ({len(result)} 文字)")
        print(f"{label}[debug] raw_response_full_start")
        print(result)
        print(f"{label}[debug] raw_response_full_end")
        return result

    def _find_input_box(self) -> Optional[Any]:
        selectors = [
            (By.TAG_NAME, "rich-textarea"),
            (By.CSS_SELECTOR, "div[contenteditable='true']"),
            (By.TAG_NAME, "textarea"),
        ]
        for by, selector in selectors:
            elements = self.driver.find_elements(by, selector)
            for element in elements:
                if element.is_displayed() and element.is_enabled():
                    return element
        return None

    def _wait_for_input_box(self, timeout: int) -> Any:
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(lambda _: self._find_input_box())

    def _count_responses(self) -> int:
        return len(self.driver.find_elements(By.CSS_SELECTOR, "message-content"))

    def _get_status(self) -> dict[str, Any]:
        status = self.driver.execute_script(
            """
            function getText(node) {
                if (node.nodeType === Node.TEXT_NODE) {
                    return node.textContent || "";
                }
                if (node.nodeType === Node.ELEMENT_NODE && node.nodeName === 'BR') {
                    return "\\n";
                }

                let text = "";
                if (node.shadowRoot) {
                    text += getText(node.shadowRoot);
                }
                if (node.childNodes) {
                    for (let i = 0; i < node.childNodes.length; i++) {
                        let child = node.childNodes[i];
                        if (child.nodeName !== 'SCRIPT' && child.nodeName !== 'STYLE') {
                            text += getText(child);
                        }
                    }
                }

                if (node.nodeType === Node.ELEMENT_NODE) {
                    let blockTags = new Set(['P', 'DIV', 'PRE', 'CODE', 'LI', 'UL', 'OL']);
                    if (blockTags.has(node.nodeName)) {
                        text += "\\n";
                    }
                }
                return text;
            }

            let messages = document.querySelectorAll('message-content');
            let latestText = '';
            if (messages.length > 0) {
                latestText = getText(messages[messages.length - 1]).replace(/\\n{3,}/g, "\\n\\n").trim();
            }

            let isGenerating = false;
            let stopBtn = document.querySelector('button[aria-label*="Stop"], button[aria-label*="停止"], button mat-icon[data-mat-icon-name="stop_circle"], button mat-icon[data-mat-icon-name="stop"]');
            if (stopBtn && stopBtn.offsetParent !== null) {
                isGenerating = true;
            }
            let spinner = document.querySelector('mat-progress-spinner, .generating-indicator, [data-test-id="generate-stop-button"]');
            if (spinner && spinner.offsetParent !== null) {
                isGenerating = true;
            }

            return { text: latestText, is_generating: isGenerating };
            """
        )
        return {
            "text": str(status.get("text", "")),
            "is_generating": bool(status.get("is_generating", False)),
        }

    def _preview_text(self, text: str, limit: int = 300) -> str:
        return text