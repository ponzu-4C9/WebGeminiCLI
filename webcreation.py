import os
import sys
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import undetected_chromedriver as uc


def setup_driver():
    """Setup Chrome driver with user profile to keep login state."""
    options = uc.ChromeOptions()
    profile_dir = os.path.join(os.getcwd(), 'chrome_profile')
    # version_main=145 を明示的に指定して、現在のChromeバージョン(145)と一致させます
    driver = uc.Chrome(options=options, user_data_dir=profile_dir, version_main=145)
    
    driver.implicitly_wait(10)
    return driver


def wait_for_user_login(driver):
    """Wait for the user to be logged in to Gemini."""
    print("ログイン状態を確認中...")
    driver.get("https://gemini.google.com/app")
    time.sleep(5)

    while "accounts.google.com" in driver.current_url or not _find_input_box(driver):
        print("\n" + "=" * 50)
        print("ブラウザでGoogleアカウントにログインしてください。")
        print("ログイン完了後、Geminiのチャット画面が表示されたらEnterを押してください。")
        print("=" * 50 + "\n")
        input("Enter を押してください...")
        driver.get("https://gemini.google.com/app")
        time.sleep(5)

    print("ログイン確認OK!")


def _find_input_box(driver):
    """Try to find the Gemini input box. Returns element or None."""
    selectors = [
        (By.TAG_NAME, "rich-textarea"),
        (By.CSS_SELECTOR, "div[contenteditable='true']"),
        (By.TAG_NAME, "textarea")
    ]
    for by, selector in selectors:
        elems = driver.find_elements(by, selector)
        if elems:
            for elem in elems:
                if elem.is_displayed() and elem.is_enabled():
                    return elem
    return None


def _wait_for_input_box(driver, timeout=30):
    """Wait until input box is available and return it."""
    wait = WebDriverWait(driver, timeout)
    def condition(d):
        return _find_input_box(d)
    return wait.until(condition)


def _count_responses(driver):
    """Count the number of model response elements on the page."""
    return len(driver.find_elements(By.CSS_SELECTOR, "message-content"))


def ask_gemini(driver, prompt, prompt_label="", timeout_minutes=5):
    """
    Send a prompt to Gemini and wait for the full response.
    Returns the response text as a string.
    """
    label = f"[{prompt_label}] " if prompt_label else ""
    print(f"\n{label}プロンプトを送信準備中...")

    # ── 1. 入力ボックスを取得 ──
    input_box = _wait_for_input_box(driver, timeout=30)

    # ── 2. 送信前のレスポンス数を記録 ──
    initial_count = _count_responses(driver)

    # ── 3. テキストを入力 ──
    for attempt in range(2):
        try:
            ActionChains(driver).move_to_element(input_box).click().perform()
            break
        except Exception as e:
            if attempt == 0:
                 print(f"  {label}入力ボックスのクリックがブロックされました。障害物を排除して再試行します...")
                 clear_obstacles(driver)
                 # 再度要素を取得し直す
                 input_box = _wait_for_input_box(driver, timeout=5)
            else:
                 try:
                     driver.execute_script("arguments[0].click();", input_box)
                 except Exception:
                     pass
    time.sleep(0.5)

    # 既存のテキストがあればクリアする (Ctrl+A -> Backspace)
    try:
        ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
    except Exception:
        pass
    time.sleep(0.5)

    # React等の内部状態を壊さないように、標準のペーストコマンドをエミュレートする
    driver.execute_script("""
        let el = arguments[0];
        let target = el.querySelector('.ql-editor') || el.querySelector('p') || el;
        if (!target) target = el;
        target.focus();
        document.execCommand('insertText', false, arguments[1]);
    """, input_box, prompt)
    time.sleep(1)

    # JSでの入力が失敗した場合のフォールバック (確実にテキストを入力させる)
    current_val = driver.execute_script("return arguments[0].textContent;", input_box)
    if not current_val or current_val.strip() == "":
        print(f"  {label}JSでの高速入力に失敗しました。標準のキー入力を使用します（少し時間がかかります）...")
        # 改行で分割し、Shift+Enterで改行を入力することで途中の誤送信を防ぐ
        lines = prompt.split('\n')
        for i, line in enumerate(lines):
            if line:
                try:
                    ActionChains(driver).send_keys(line).perform()
                except Exception:
                    pass
            if i < len(lines) - 1:
                try:
                    ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()
                except Exception:
                    pass
        time.sleep(1)

    # ── 4. 送信ボタンをクリック ──
    sent = False
    try:
        wait = WebDriverWait(driver, 5)
        send_btn = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[aria-label*='Send'], button[aria-label*='送信'], button.send-button")
        ))
        send_btn.click()
        sent = True
    except Exception:
        pass

    if not sent:
        # Fallback: Ctrl + Enter または Enter で送信
        try:
            ActionChains(driver).key_down(Keys.CONTROL).send_keys(Keys.ENTER).key_up(Keys.CONTROL).perform()
            time.sleep(0.5)
            # それでも送信されていなければただのEnterを試す
            if _count_responses(driver) == initial_count:
                 input_box.send_keys(Keys.ENTER)
            sent = True
        except Exception:
            pass

    if not sent:
        raise RuntimeError(f"{label}送信に失敗しました。")

    print(f"{label}プロンプト送信完了。応答を待機中...")

    # ── 5. 新しいレスポンスが出現するまで待つ ──
    max_wait_appearance = 120  # seconds
    for i in range(max_wait_appearance):
        if _count_responses(driver) > initial_count:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"{label}応答が {max_wait_appearance} 秒以内に開始されませんでした。")

    # ── 6. レスポンスが安定する (生成完了) まで待つ ──
    timeout_sec = timeout_minutes * 60
    last_text = ""
    last_len = 0
    stable_count = 0
    # Canvasの場合、生成が一時停止したように見えてもバックグラウンドで動いていることがあるため
    # より長い安定時間(15秒)を要求する
    required_stable = 15  

    # Canvasを含めた全体のテキストと、生成中を示すインジケーターの状態を取得するJS関数
    js_get_status = """
        let result = "";
        let isGenerating = false;
        
        // 1. 生成中インジケーター（スピナーや「停止」ボタン等）の確認
        let stopBtn = document.querySelector('button[aria-label*="Stop"], button[aria-label*="停止"], button mat-icon[data-mat-icon-name="stop_circle"], button mat-icon[data-mat-icon-name="stop"]');
        if (stopBtn && stopBtn.closest('button') && stopBtn.closest('button').offsetParent !== null) {
            isGenerating = true;
        }
        let spark = document.querySelector('mat-progress-spinner, .generating-indicator, [data-test-id="generate-stop-button"]');
        if (spark && spark.offsetParent !== null) {
            isGenerating = true;
        }
        
        // 2. Shadow DOMを貫通してすべてのテキストを取得する関数 (Canvasの中身を確実に検知するため)
        function getShadowText(node) {
            let text = "";
            if (node.nodeType === Node.TEXT_NODE) {
                return node.textContent + " ";
            }
            if (node.shadowRoot) {
                text += getShadowText(node.shadowRoot);
            }
            if (node.childNodes) {
                for (let i = 0; i < node.childNodes.length; i++) {
                    let child = node.childNodes[i];
                    if (child.nodeName !== 'SCRIPT' && child.nodeName !== 'STYLE') {
                        text += getShadowText(child);
                    }
                }
            }
            return text;
        }
        
        // 3. テキストの抽出
        let messages = document.querySelectorAll("message-content");
        if (messages.length > 0) {
            result += "=== Chat Message ===\\n";
            result += getShadowText(messages[messages.length - 1]) + "\\n\\n";
        }
        
        let canvasText = "";
        let canvasElements = document.querySelectorAll("mat-sidenav, spark-project-workspace, [class*='workspace'], [class*='canvas'], .canvas-container");
        let usedElements = new Set();
        
        for (let el of canvasElements) {
             if (el.offsetParent !== null && !el.closest('message-content')) {
                 let parentAlreadyUsed = false;
                 for (let used of usedElements) {
                     if (used.contains(el)) {
                         parentAlreadyUsed = true;
                         break;
                     }
                 }
                 if (!parentAlreadyUsed) {
                     usedElements.add(el);
                     canvasText += getShadowText(el) + "\\n\\n";
                 }
             }
        }
        
        // iframeの中身を考慮(Canvasがiframeの場合)
        let iframes = document.querySelectorAll("iframe");
        for (let iframe of iframes) {
             try {
                 if (iframe.contentWindow && iframe.contentWindow.document && iframe.contentWindow.document.body) {
                     canvasText += getShadowText(iframe.contentWindow.document.body) + "\\n\\n";
                 }
             } catch(e) { 
                 // Cross-origin iframe 
             }
        }
        
        if (canvasText.trim().length > 0) {
            result += "=== Canvas/Editor Content ===\\n" + canvasText.trim();
        }
        
        // ページ全体のテキスト量（Shadow DOM含む）を計算して、変更検知の精度を上げる
        let bodyTextLen = getShadowText(document.body).length;
        
        return { text: result.trim(), bodyLength: bodyTextLen, isGenerating: isGenerating };
    """

    for i in range(timeout_sec):
        try:
            status = driver.execute_script(js_get_status)
            current_text = status.get('text', '')
            current_len = status.get('bodyLength', 0)
            is_generating = status.get('isGenerating', False)
            
            if not current_text:
                time.sleep(1)
                continue
        except Exception:
            # stale element – ページが更新された可能性
            time.sleep(1)
            continue

        # 生成中インジケーターが出ている間、またはページ全体のテキスト量が増え続けている間は完了とみなさない
        if is_generating:
            stable_count = 0
            last_text = current_text
            last_len = current_len
        elif current_text == last_text and current_len == last_len and current_text.strip() != "":
            stable_count += 1
            if stable_count >= required_stable:
                break
        else:
            stable_count = 0
            last_text = current_text
            last_len = current_len

        # 進捗表示 (10秒ごと)
        if i > 0 and i % 10 == 0:
            elapsed = i
            if is_generating:
                gen_str = "(生成中インジケータあり)"
            else:
                gen_str = f"(テキスト安定待機中: {stable_count}/{required_stable})"
            print(f"  ... {elapsed}秒経過 {gen_str} 現在の文字数: {len(current_text)} (全体: {current_len})")

        time.sleep(1)
    else:
        print(f"{label}WARNING: {timeout_minutes}分経過しても応答が安定しませんでした。現在のテキストを返します。")

    # 最終取得
    try:
        status = driver.execute_script(js_get_status)
        result = status.get('text', last_text)
    except Exception:
        result = last_text

    print(f"{label}応答取得完了 ({len(result)} 文字)")
    return result


# ────────────────────────────────────────────
#  プロンプト定義
# ────────────────────────────────────────────

def build_prompt_1(j_content):
    return f"""{j_content}
こちらのデザイン指示書で1枚のランディングページを作成してください。テキストはダミーでそれっぽいものを入れてください。 ヘッダーフッターは禁止。 #技術要件 必ず以下の技術要件で生成すること ・Next.js (App Router), React, Tailwind CSS ・セクション単位でコンポーネントを作成せよ。 ・CSSの@importルール（Google Fontsなど）は、必ずCSSファイルまたは<style>タグの最上部に配置すること。他のCSSルール（:root、セレクタ、プロパティなど）よりも前に記述する必要がある。これを守らないとビルドエラーが発生する。"""


def build_prompt_2(k_content):
    return f"""デザインはそのままで、以下のサイト構成を100%反映した1ページのランディングページのデザインを作成してください。 先ほどのデザイン指示書を100%反映した状態で、サイト構成を反映させてください。 ヘッダーフッターは禁止。 よくある質問はアコーディオンで表示必須。 図解や表を表示する際は画像ではなく、コーディングで生成してください。 アイコンは基本的にLucide Reactから使用すること。（SNSアイコンはSimple Iconsを使用すること) アイコンをプレースホルダ―画像にするのは禁止。 画像はUnplashから使用禁止。実際の画像は設置しないこと。 どのような画像が表示されるべきか視覚的に理解できるよう「画像プレースホルダー機能」を使用して枠を配置し、画像を説明してください。 画像は内容に合うよう、できるだけ詳しくわかりやすく説明を入れてください。 画像の上にあるべきテキストはそのままデザインで表示すること。 所在地を表示する場合はGooglemapを埋め込むこと。 そして、Googlemapにはかならず住所の赤ピンを立たせること。所在地ではなく、「対応エリア」の画像やイラストは生成禁止・配置禁止。 【技術要件】 必ず以下の技術要件で生成すること - Next.js (App Router), React, Tailwind CSS - セクション単位でコンポーネントを作成せよ。 - CSSの@importルール（Google Fontsなど）は、必ずCSSファイルまたは<style>タグの最上部に配置すること。他のCSSルール（:root、セレクタ、プロパティなど）よりも前に記述する必要がある。これを守らないとビルドエラーが発生する。 【スタイリング要件】 - Next.js + Tailwind CSSへのリファクタリングを前提とした実装 - CSS変数を定義する場合、各要素に明示的にクラスやスタイルで色を指定 - body要素への色の継承に依存せず、テキスト要素ごとに色を指定 - ボタンの状態（active/inactive/hover）ごとに、明示的に色を指定 CTAは遷移できるか確認してください。 現在のデザインは、ダミーテキストなので絶対に以下のサイト構成を100%反映させてください。 サイト構成
{k_content}"""


PROMPT_3 = """セクション構成を100%反映したかったのですが、どこか反映されていないところはありませんか。 すべて完璧に見直し、デザインに反映させてください。 また、アニメーションは禁止です。アニメーションがついている場合はすべて削除してください。 ・テキストはすべて反映されていますか ・図解や表は完全に再現されていますか(図解と表はプレースホルダ―禁止) ・画像はプレースホルダーで設置、説明されていますか ・タイトル以外のフォントサイズは例外なく、PCで16px以上、SPで12px以上になっていますか ・絵文字は禁止 ・SPビューで画像が大きすぎませんか(メインビジュアル以外のすべての画像プレースホルダ―は縦の長さ300pxより小さくすること必須) ・アニメーションは禁止"""


def clear_obstacles(driver):
    """画面を覆うポップアップやダイアログを様々な方法で閉じる・回避する(高速版)"""
    print("  画面の障害物（ポップアップ等）の確認と排除を行います...")
    # 暗黙の待機を無効化し、要素がなければ即座にスルーするように設定
    original_wait = 10
    driver.implicitly_wait(0)
    
    try:
        # アプローチ1: 汎用的な「閉じる」「後で」ボタンを探してクリック
        close_xpaths = [
            "//button[contains(., '後で') or contains(., 'スキップ') or contains(., '閉じる') or contains(., 'Dismiss') or contains(., 'Not now') or contains(., 'No thanks')]",
            "//button[@aria-label='閉じる' or @aria-label='Close' or @aria-label='Dismiss']",
            "//mat-icon[text()='close']/ancestor::button"
        ]
        for xpath in close_xpaths:
            try:
                btns = driver.find_elements(By.XPATH, xpath)
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled():
                        driver.execute_script("arguments[0].click();", btn)
                        print("    -> 障害物を閉じるボタンをクリックしました。")
            except Exception:
                pass

        # アプローチ2: ESCキーの送信 (開いているメニューやダイアログを強制キャンセル)
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
            
        # アプローチ3: 背景(安全な左上端)をクリックしてフォーカスを外す・モーダルを消す
        try:
            action = ActionChains(driver)
            action.move_to_element_with_offset(driver.find_element(By.TAG_NAME, "body"), 10, 10)
            action.click().perform()
        except Exception:
            pass
            
        # UIが落ち着くまで少しだけ待つ
        time.sleep(0.5)

    except Exception as e:
        print(f"  障害物の排除処理中にエラーが発生しましたが、続行します: {e}")
    finally:
        # 暗黙の待機時間を元に戻す
        driver.implicitly_wait(original_wait)


def ensure_pro_mode(driver):
    """Ensure Gemini is set to Pro mode instead of Fast mode."""
    print("\n現在のモデル（モード）を確認中...")
    try:
        # ユーザーから提供された data-test-id に基づいてボタンを取得
        mode_btn = driver.find_element(By.CSS_SELECTOR, "button[data-test-id='bard-mode-menu-button']")
        btn_text = mode_btn.text
        
        if "高速" in btn_text or "Fast" in btn_text:
            print(f"  現在のモードは「{btn_text.strip()}」です。プロモードへ切り替えます...")
            ActionChains(driver).move_to_element(mode_btn).click().perform()
            time.sleep(1.5)  # メニューが開くのを待つ
            
            # メニューからProモードを探してクリック
            menu_items_xpaths = [
                # 提供されたHTMLに基づく確実なセレクタ
                "//button[@data-test-id='bard-mode-option-pro']",
                # フォールバック用のXPath
                "//button[@role='menuitem' and (contains(., 'プロ') or contains(., 'Pro'))]",
                "//div[@role='menuitem' and (contains(., 'プロ') or contains(., 'Pro'))]"
            ]
            
            clicked = False
            for xpath in menu_items_xpaths:
                try:
                    items = driver.find_elements(By.XPATH, xpath)
                    for item in items:
                        if item.is_displayed():
                            ActionChains(driver).move_to_element(item).click().perform()
                            print("  ✅ プロモードを選択しました。")
                            clicked = True
                            time.sleep(2)
                            break
                except Exception:
                    pass
                if clicked:
                    break
                    
            if not clicked:
                print("  ❌ メニューの中に「プロ」の項目が見つかりませんでした。手動で切り替えてください。")
        else:
            print(f"  ✅ 現在のモードは「{btn_text.strip()}」です。高速モードではないため、このまま続行します。")
            
    except Exception as e:
        print(f"  ⚠️ モード選択ボタンの取得をスキップしました（ボタンが見つからないかUIが変更されています）。現在のモードのまま続行します。")


def enable_canvas_mode(driver):
    """入力欄付近のツールボタンからCanvasモードを有効化する。失敗した場合はスクリプトを終了する。"""
    print("\nCanvasモードの有効化を試みます...")
    
    for attempt in range(2):
        try:
            # 1. ツールメニューを開くためのボタンを探す
            xpath_tools = "//button[contains(@class, 'toolbox-drawer-button') and .//mat-icon[@data-mat-icon-name='page_info']]"
            tools_btns = driver.find_elements(By.XPATH, xpath_tools)
            clicked = False
            
            for btn in tools_btns:
                if btn.is_displayed() and btn.is_enabled():
                    try:
                        ActionChains(driver).move_to_element(btn).click().perform()
                        clicked = True
                        print("  「ツール」ボタンをクリックしました。メニュー展開を待機します...")
                        time.sleep(1.5)  # メニューが開くのを待機
                        break
                    except Exception as click_err:
                        # クリック時にElementClickInterceptedExceptionなどが出た場合
                        raise click_err
            
            if not clicked:
                if attempt == 0:
                    raise Exception("「ツール」ボタンが見つからないかクリックできませんでした。")
                else:
                    print("  「ツール」ボタンが見つからないかクリックできませんでした。")
                    print("  【致命的エラー】Canvasモードを有効化できないため、処理を中断します。")
                    sys.exit(1)

            # 2. メニューの中から「Canvas」のボタンを正確に探してクリックする
            xpath_canvas = "//button[@role='menuitemcheckbox' and .//div[contains(@class, 'label') and contains(text(), 'Canvas')]]"
            
            canvas_opts = driver.find_elements(By.XPATH, xpath_canvas)
            canvas_clicked = False
            for opt in canvas_opts:
                if opt.is_displayed():
                    is_checked = opt.get_attribute("aria-checked")
                    if is_checked == "true":
                        print("  Canvasモードはすでにオンになっています。")
                        canvas_clicked = True
                        break
                    
                    try:
                        ActionChains(driver).move_to_element(opt).click().perform()
                        canvas_clicked = True
                        print("  Canvasモードをオンにしました。")
                        time.sleep(2) # 切り替わりを待つ
                        break
                    except Exception as click_err:
                         raise click_err
            
            if not canvas_clicked:
                if attempt == 0:
                    raise Exception("メニュー内に「Canvas」が見つからないかクリックできませんでした。")
                else:
                    print("  メニュー内に「Canvas」が見つかりませんでした。")
                    print("  【致命的エラー】Canvasモードを有効化できないため、処理を中断します。")
                    try:
                        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                    except Exception:
                        pass
                    sys.exit(1)
            
            # 成功したらループを抜ける
            break
                
        except Exception as e:
            if attempt == 0:
                print(f"  操作がブロックされました（詳細: {e}）。障害物を排除して再試行します...")
                clear_obstacles(driver)
            else:
                print(f"  【致命的エラー】Canvasモード切り替え処理中に予期せぬエラーが発生したため、処理を中断します: {e}")
                sys.exit(1)


def get_multiline_input(prompt_title):
    """複数行の入力を受け付ける関数"""
    print("=" * 50)
    print(prompt_title)
    print("※複数行の入力が可能です。ペースト後、改行して「EOF」と入力しEnterを押してください。")
    print("=" * 50)
    
    lines = []
    while True:
        try:
            line = input()
            if line.strip().upper() == "EOF":
                break
            lines.append(line)
        except EOFError:
            break
            
    content = "\n".join(lines).strip()
    return content


# ────────────────────────────────────────────
#  メイン処理
# ────────────────────────────────────────────

def main():
    # ── ユーザー入力 ──
    j_content = get_multiline_input("【1】J列の内容をペーストしてください")
    if not j_content:
        print("Error: J列の内容が入力されていません。")
        sys.exit(1)

    print()
    k_content = get_multiline_input("【2】K列の内容をペーストしてください")
    if not k_content:
        print("Error: K列の内容が入力されていません。")
        sys.exit(1)

    # ── プロンプト組み立て ──
    prompts = [
        ("1/3", build_prompt_1(j_content)),
        ("2/3", build_prompt_2(k_content)),
        ("3/3", PROMPT_3),
    ]

    # ── ブラウザ起動 & ログイン ──
    driver = None
    try:
        driver = setup_driver()
        wait_for_user_login(driver)
        
        # モデルが高速モードならプロモードに切り替える
        ensure_pro_mode(driver)

        # 最初のプロンプトを投げる前にCanvasモードをオンにする
        enable_canvas_mode(driver)

        all_responses = []

        for label, prompt_text in prompts:
            print(f"\n{'─' * 50}")
            print(f"  プロンプト {label} を送信します")
            print(f"{'─' * 50}")

            response = ask_gemini(
                driver,
                prompt_text,
                prompt_label=label,
                timeout_minutes=5,
            )
            all_responses.append(response)

            # 次のプロンプト送信前に少し待つ（Gemini側の状態安定のため）
            if label != "3/3":
                print(f"  次のプロンプトまで 3 秒待機...")
                time.sleep(3)

        # ── 結果出力 ──
        print("\n\n" + "=" * 60)
        print("  すべてのプロンプト送信が完了しました！")
        print("=" * 60)

        for i, resp in enumerate(all_responses, 1):
            print(f"\n{'─' * 40}")
            print(f"  レスポンス {i}/3  ({len(resp)} 文字)")
            print(f"{'─' * 40}")
            print(resp[:500] + ("..." if len(resp) > 500 else ""))

        print(f"\n全レスポンスの合計文字数: {sum(len(r) for r in all_responses)}")
        print("ブラウザ上で全体の結果を確認してください。")

    except KeyboardInterrupt:
        print("\n中断されました。")
    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            input("\nEnter を押すとブラウザを閉じます...")
            driver.quit()


if __name__ == "__main__":
    main()
