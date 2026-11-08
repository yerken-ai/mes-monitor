import json
import os
import re
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

LOOP_COUNT = int(os.getenv("LOOP_COUNT", "1"))
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "300"))

ALMATY_TZ = timezone(timedelta(hours=5))


def log(msg: str):
    print(msg, flush=True)


def send_telegram_message(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log(f"[TG не настроен] Сообщение:\n{text}")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        if not resp.ok:
            log(f"[Ошибка Telegram API sendMessage]: {resp.status_code} {resp.text}")
        else:
            log("[OK] Текстовое сообщение отправлено в Telegram.")
    except Exception as e:
        log(f"[Ошибка отправки текста в Telegram]: {e}")


def send_telegram_photo(photo_path: str, caption: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log(f"[TG не настроен] Подпись к фото:\n{caption}")
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
        if not resp.ok:
            log(f"[Ошибка Telegram API sendPhoto]: {resp.status_code} {resp.text}")
            send_telegram_message(caption)
        else:
            log("[OK] Скриншот и статус успешно отправлены в Telegram!")
    except Exception as e:
        log(f"[Ошибка отправки фото в Telegram]: {e}")
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
    raw_cookies = data if isinstance(data, list) else data.get("cookies", [])

    # 1. Продлеваем сессию через официальный endpoint /gologin/refresh (выдает новый 30-дневный JWT-TOKEN и настройки WORKSPACE / DM_BASE_URL)
    req_session = requests.Session()
    user_login = "Zhilkibagarov%40tn-ca.kz"
    for c in raw_cookies:
        req_session.cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain", "tn-mes2.aspect-systems.ru"),
            path=c.get("path", "/"),
        )
        if c["name"] == "STUDIO-auth":
            try:
                import urllib.parse
                parsed_auth = json.loads(urllib.parse.unquote(c["value"]))
                if parsed_auth.get("user"):
                    user_login = urllib.parse.quote(parsed_auth["user"])
            except Exception:
                pass

    dm_base_url = "http://dm-api/rest/"
    workspace = "aspect"
    refreshed_jwt = None
    refreshed_xsrf = None
    refreshed_auth = None

    try:
        log("Обновляем токен сессии через /gologin/refresh ...")
        r = req_session.post(
            f"{TARGET_URL.rstrip('/')}/gologin/refresh",
            headers={"Content-Type": "application/json", "name": user_login},
            timeout=20,
        )
        if r.ok:
            resp_json = r.json()
            if isinstance(resp_json, dict) and "cookie" in resp_json:
                for c_str in resp_json.get("cookie", []):
                    if c_str.startswith("JWT-TOKEN="):
                        refreshed_jwt = c_str.split(";", 1)[0].split("=", 1)[1]
                    elif c_str.startswith("XSRF-TOKEN="):
                        refreshed_xsrf = c_str.split(";", 1)[0].split("=", 1)[1]
                    elif c_str.startswith("auth="):
                        refreshed_auth = c_str.split(";", 1)[0].split("=", 1)[1]
                env_data = resp_json.get("env", {})
                dm_base_url = env_data.get("DM_BASE_URL", dm_base_url)
                workspace = env_data.get("WORKSPACE", workspace)
                log(f"[OK] Токен сессии успешно обновлен (WORKSPACE={workspace}).")
    except Exception as e:
        log(f"[Предупреждение /gologin/refresh]: {e}")

    pw_cookies = []
    far_future = float(int(time.time()) + 30 * 86400)

    for c in raw_cookies:
        name = c["name"]
        val = c["value"]
        if name == "STUDIO-JWT-TOKEN" and refreshed_jwt:
            val = refreshed_jwt
        elif name == "STUDIO-XSRF-TOKEN" and refreshed_xsrf:
            val = refreshed_xsrf
        elif name == "STUDIO-auth" and refreshed_auth:
            val = refreshed_auth

        same_site = normalize_same_site(c.get("sameSite"))
        secure = bool(c.get("secure", True)) if same_site == "None" else bool(c.get("secure", False))

        cookie = {
            "name": name,
            "value": val,
            "domain": c.get("domain", "tn-mes2.aspect-systems.ru"),
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": secure,
            "sameSite": same_site,
        }
        if "expirationDate" in c and c["expirationDate"]:
            cookie["expires"] = max(float(c["expirationDate"]), far_future)
        pw_cookies.append(cookie)

    # Добавляем обязательные системные куки Aip Studio (JWT-TOKEN, XSRF-TOKEN, auth, pathName)
    studio_jwt = refreshed_jwt or next((c["value"] for c in pw_cookies if c["name"] == "STUDIO-JWT-TOKEN"), "")
    studio_xsrf = refreshed_xsrf or next((c["value"] for c in pw_cookies if c["name"] == "STUDIO-XSRF-TOKEN"), "")
    studio_auth = refreshed_auth or next((c["value"] for c in pw_cookies if c["name"] == "STUDIO-auth"), "")

    extra_cookies = [
        {"name": "JWT-TOKEN", "value": studio_jwt, "httpOnly": True, "secure": True},
        {"name": "XSRF-TOKEN", "value": studio_xsrf, "httpOnly": False, "secure": True},
        {"name": "auth", "value": studio_auth, "httpOnly": False, "secure": False},
        {"name": "pathName", "value": "/", "httpOnly": False, "secure": False},
    ]
    existing_names = {c["name"] for c in pw_cookies if c["domain"] == "tn-mes2.aspect-systems.ru"}
    for ec in extra_cookies:
        if ec["name"] not in existing_names and ec["value"]:
            pw_cookies.append({
                "name": ec["name"],
                "value": ec["value"],
                "domain": "tn-mes2.aspect-systems.ru",
                "path": "/",
                "httpOnly": ec["httpOnly"],
                "secure": ec["secure"],
                "sameSite": "Lax",
                "expires": far_future,
            })

    # Обязательные переменные localStorage для Aip Studio (без них сайт показывает окно логина!)
    origins = [
        {
            "origin": "https://tn-mes2.aspect-systems.ru",
            "localStorage": [
                {"name": "DM_BASE_URL", "value": dm_base_url},
                {"name": "WORKSPACE", "value": workspace},
                {"name": "loggedIn", "value": "true"},
                {"name": "loginBySAML", "value": "true"},
            ],
        }
    ]

    state_obj = {"cookies": pw_cookies, "origins": origins}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state_obj, f, ensure_ascii=False, indent=2)
    return STATE_FILE


def extract_almaty_status(page) -> str:
    """Ждет загрузки таблицы мониторинга и извлекает данные по линии Алматы + заголовки колонок."""
    log(f"Ожидаем загрузки таблицы и строки '{LINE_NAME}'...")
    try:
        page.get_by_text(LINE_NAME, exact=False).first.wait_for(timeout=45000)
        # Даем еще 3 секунды, чтобы все ячейки таблицы дорисовали значения
        time.sleep(3)
    except PlaywrightTimeoutError:
        log(f"Таймаут ожидания текста '{LINE_NAME}' (45 сек).")

    # Пробуем прочитать заголовки таблицы (если это DevExtreme DataGrid или стандартная таблица)
    headers = []
    try:
        header_cells = page.locator(".dx-datagrid-headers td, thead th, [role='columnheader']").all()
        for hc in header_cells:
            ht = hc.inner_text().strip().replace("\n", " ")
            if ht:
                headers.append(ht)
    except Exception:
        pass

    selectors = [
        f".dx-data-row:has-text('{LINE_NAME}')",
        f"tr:has-text('{LINE_NAME}')",
        f"[role='row']:has-text('{LINE_NAME}')",
    ]

    extracted_blocks = []
    for sel in selectors:
        loc = page.locator(sel)
        count = loc.count()
        if count > 0:
            for i in range(min(count, 5)):
                row = loc.nth(i)
                cells = row.locator("td, [role='gridcell']").all()
                if cells and len(cells) > 1:
                    cell_values = [c.inner_text().strip().replace("\n", " ") for c in cells]
                    if headers and len(headers) == len(cell_values):
                        pairs = [
                            f"• <b>{h}:</b> {v}"
                            for h, v in zip(headers, cell_values)
                            if h and v
                        ]
                        block_str = "\n".join(pairs)
                    else:
                        block_str = " | ".join([v for v in cell_values if v])
                else:
                    raw_text = row.inner_text()
                    parts = [p.strip() for p in raw_text.splitlines() if p.strip()]
                    block_str = " | ".join(parts)

                if block_str and block_str not in extracted_blocks:
                    extracted_blocks.append(block_str)
            if extracted_blocks:
                break

    if not extracted_blocks:
        elements = page.get_by_text(LINE_NAME, exact=False).all()
        for el in elements[:3]:
            try:
                parent_text = el.locator("..").locator("..").inner_text()
                parts = [p.strip() for p in parent_text.splitlines() if p.strip()]
                line_str = " | ".join(parts)
                if line_str and line_str not in extracted_blocks:
                    extracted_blocks.append(line_str[:500])
            except Exception:
                continue

    return "\n\n".join(extracted_blocks)


def is_login_page(page) -> bool:
    url_lower = page.url.lower()
    if any(k in url_lower for k in ["login", "auth", "signin", "sso", "oauth", "adfs"]):
        return True
    if page.locator(".loginFormContainer, input[type='password']").count() > 0:
        return True
    return False


def main():
    state_path = prepare_state_file()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=state_path,
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
        )
        page = context.new_page()

        log(f"Открываем {TARGET_URL} ...")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)

        for iteration in range(1, LOOP_COUNT + 1):
            now_str = datetime.now(ALMATY_TZ).strftime("%d.%m.%Y %H:%M:%S")
            log(f"\n--- Проверка #{iteration}/{LOOP_COUNT} ({now_str}) ---")

            if iteration > 1:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    log(f"Предупреждение при обновлении страницы: {e}")

            # Ждем загрузки интерфейса Aip Studio
            time.sleep(5)

            if is_login_page(page):
                # Пробуем нажать кнопку входа через ADFS/SAML, если мы на странице логина
                log("Обнаружена форма входа, проверяем возможность авто-входа через SAML...")
                msg = (
                    f"⚠️ <b>Внимание! Сессия на сайте MES истекла ({now_str}).</b>\n"
                    f"Пожалуйста, обновите куки в секрете <code>AUTH_STATE_JSON</code>."
                )
                log(msg)
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
                    f"🕒 <code>{now_str}</code>\n\n"
                    f"{status_text}"
                )
            else:
                caption = (
                    f"ℹ️ <b>Мониторинг MES ({LINE_NAME})</b>\n"
                    f"🕒 <code>{now_str}</code>\n"
                    f"Таблица загружена (см. скриншот)."
                )

            log(f"Результат парсинга:\n{caption}")
            send_telegram_photo(screenshot_path, caption)

            if iteration < LOOP_COUNT:
                log(f"Ждем {INTERVAL_SECONDS} сек. до следующего обновления...")
                time.sleep(INTERVAL_SECONDS)

        browser.close()


if __name__ == "__main__":
    main()
