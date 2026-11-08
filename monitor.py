import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = os.getenv("TARGET_URL", "https://tn-mes2.aspect-systems.ru/")
LINE_NAME = os.getenv("LINE_NAME", "Алматы")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
AUTH_STATE_JSON = os.getenv("AUTH_STATE_JSON", "").strip()
STATE_FILE = "auth_state.json"

# Количество проверок за один запуск (в GitHub Actions по умолчанию 11 проверок с паузой 5 минут = ~55 минут беспрерывной работы вкладки)
LOOP_COUNT = int(os.getenv("LOOP_COUNT", "1"))
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "300"))  # 300 сек = 5 минут

# Часовой пояс Алматы (UTC+5)
ALMATY_TZ = timezone(timedelta(hours=5))


def send_telegram_message(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"[TG не настроен] Сообщение:\n{text}")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[Ошибка отправки текста в Telegram]: {e}")


def send_telegram_photo(photo_path: str, caption: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(f"[TG не настроен] Подпись к фото:\n{caption}")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": TG_CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"},
                files={"photo": f},
                timeout=30,
            )
        resp.raise_for_status()
    except Exception as e:
        print(f"[Ошибка отправки фото в Telegram]: {e}")
        send_telegram_message(caption)


def normalize_same_site(val) -> str:
    if not val or not isinstance(val, str):
        return "Lax"
    v = val.lower()
    if v in ("no_restriction", "none"):
        return "None"
    if v == "strict":
        return "Strict"
    return "Lax"


def prepare_state_file() -> str:
    raw_json = AUTH_STATE_JSON
    if not raw_json and os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw_json = f.read().strip()

    if not raw_json:
        raise FileNotFoundError(
            "Не найден файл сессии auth_state.json и не задан секрет AUTH_STATE_JSON."
        )

    data = json.loads(raw_json)

    # Поддержка экспорта из браузерного расширения Cookie-Editor (список cookies [...])
    if isinstance(data, list):
        pw_cookies = []
        for c in data:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", "tn-mes2.aspect-systems.ru"),
                "path": c.get("path", "/"),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": normalize_same_site(c.get("sameSite")),
            }
            if "expirationDate" in c and c["expirationDate"]:
                cookie["expires"] = float(c["expirationDate"])
            pw_cookies.append(cookie)
        data = {"cookies": pw_cookies, "origins": []}

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return STATE_FILE


def extract_almaty_status(page) -> str:
    """Ищет на странице строку таблицы или блок, относящийся к линии Алматы."""
    # Ждем появления текста на странице (до 20 секунд, т.к. таблица может подгружаться через AJAX)
    try:
        page.get_by_text(LINE_NAME, exact=False).first.wait_for(timeout=20000)
    except PlaywrightTimeoutError:
        pass

    # Вариант 1: Поиск строки таблицы <tr> или строки грида (role="row" / .dx-row / .k-master-row / и т.д.)
    selectors = [
        f"tr:has-text('{LINE_NAME}')",
        f"[role='row']:has-text('{LINE_NAME}')",
        f".dx-data-row:has-text('{LINE_NAME}')",
        f".ag-row:has-text('{LINE_NAME}')",
    ]

    extracted_lines = []
    for sel in selectors:
        loc = page.locator(sel)
        count = loc.count()
        if count > 0:
            for i in range(min(count, 5)):
                raw_text = loc.nth(i).inner_text()
                parts = [p.strip() for p in raw_text.splitlines() if p.strip()]
                line_str = " | ".join(parts)
                if line_str and line_str not in extracted_lines:
                    extracted_lines.append(line_str)
            if extracted_lines:
                break

    # Вариант 2: Если таблица сверстана нестандартными div'ами
    if not extracted_lines:
        elements = page.get_by_text(LINE_NAME, exact=False).all()
        for el in elements[:3]:
            try:
                parent_text = el.locator("..").inner_text()
                parts = [p.strip() for p in parent_text.splitlines() if p.strip()]
                line_str = " | ".join(parts)
                if line_str and line_str not in extracted_lines:
                    extracted_lines.append(line_str)
            except Exception:
                continue

    if extracted_lines:
        return "\n".join(extracted_lines)

    return ""


def is_login_page(page) -> bool:
    """Проверяет, не перебросило ли нас на страницу ввода пароля / 2FA."""
    url_lower = page.url.lower()
    if any(k in url_lower for k in ["login", "auth", "signin", "sso", "oauth"]):
        return True
    if page.locator("input[type='password']").count() > 0:
        return True
    return False


def main():
    state_path = prepare_state_file()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=state_path,
            viewport={"width": 1600, "height": 900},
            locale="ru-RU",
        )
        page = context.new_page()

        print(f"Открываем {TARGET_URL} ...")
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

        for iteration in range(1, LOOP_COUNT + 1):
            now_str = datetime.now(ALMATY_TZ).strftime("%d.%m.%Y %H:%M:%S")
            print(f"\n--- Проверка #{iteration}/{LOOP_COUNT} ({now_str}) ---")

            if iteration > 1:
                try:
                    page.reload(wait_until="networkidle", timeout=60000)
                except Exception as e:
                    print(f"Предупреждение при обновлении страницы: {e}")

            # Даем 3 секунды на догрузку динамических данных таблицы (gridView)
            time.sleep(3)

            if is_login_page(page):
                msg = (
                    f"⚠️ <b>Внимание! Сессия на сайте MES истекла ({now_str}).</b>\n"
                    f"Сайт запросил повторную авторизацию (2FA).\n"
                    f"Пожалуйста, запустите <code>save_session.py</code> на ПК и обновите секрет <code>AUTH_STATE_JSON</code>."
                )
                print(msg)
                screenshot_path = "login_expired.png"
                page.screenshot(path=screenshot_path, full_page=True)
                send_telegram_photo(screenshot_path, msg)
                sys.exit(1)

            status_text = extract_almaty_status(page)
            screenshot_path = "status_screenshot.png"
            page.screenshot(path=screenshot_path, full_page=False)

            if status_text:
                caption = (
                    f"📊 <b>Статус линии ({LINE_NAME})</b>\n"
                    f"🕒 Время: <code>{now_str}</code>\n\n"
                    f"<b>Данные строки:</b>\n<code>{status_text}</code>"
                )
            else:
                caption = (
                    f"ℹ️ <b>Мониторинг MES ({LINE_NAME})</b>\n"
                    f"🕒 Время: <code>{now_str}</code>\n"
                    f"Строка «{LINE_NAME}» не распознана текстом — проверьте скриншот таблицы."
                )

            print(caption)
            send_telegram_photo(screenshot_path, caption)

            # Пересохраняем актуальные cookies в рамках сессии
            context.storage_state(path=state_path)

            if iteration < LOOP_COUNT:
                print(f"Ждем {INTERVAL_SECONDS} сек. до следующего обновления...")
                time.sleep(INTERVAL_SECONDS)

        browser.close()


if __name__ == "__main__":
    main()
