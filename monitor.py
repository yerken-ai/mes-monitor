"""
Telegram-бот для получения скриншота линии Алматы (MES tn-mes2.aspect-systems.ru).

Логика:
  * Бот слушает сообщения в Telegram (long polling).
  * По команде /status (или нажатию кнопки "📸 Статус линии") делает актуальный скриншот
    экрана линии завода "Алматы" и отправляет его в чат с временной меткой.
  * По команде /start отправляет приветствие с удобной кнопкой "📸 Статус линии".
  * Никаких автоматических оповещений и спама в чат — фото высылается ТОЛЬКО по запросу.

Режимы запуска:
  python monitor.py           - режим бота (слушает команды /status в Telegram)
  python monitor.py status    - разовый запуск: сразу сделать скриншот, отправить в TG и завершить
  python monitor.py refresh   - продлить сессию и сохранить куки
"""
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
import urllib3
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

TARGET_URL = os.getenv("TARGET_URL", "https://tn-mes2.aspect-systems.ru/").rstrip("/") + "/"
FACTORY_NAME = os.getenv("LINE_NAME", "Алматы")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
AUTH_STATE_JSON = os.getenv("AUTH_STATE_JSON", "").strip()

STATE_FILE = "auth_state_pw.json"
REFRESHED_FILE = "refreshed_auth.json"
SCREENSHOT = "almaty.png"

RUN_MINUTES = int(os.getenv("RUN_MINUTES", "330"))  # до 5.5 часов в одном прогоне (для GitHub Actions)
ALMATY_TZ = timezone(timedelta(hours=5))


def log(msg: str):
    print(f"[{datetime.now(ALMATY_TZ):%H:%M:%S}] {msg}", flush=True)


def now_local() -> datetime:
    return datetime.now(ALMATY_TZ)


# ----------------------------------------------------------------- Telegram
def tg(method: str, **kwargs):
    if not TG_BOT_TOKEN:
        log(f"[TG не настроен] {method}")
        return None
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    timeout = kwargs.pop("_timeout", 30)
    kwargs.setdefault("verify", False)
    try:
        r = requests.post(url, timeout=timeout, **kwargs)
        data = r.json()
        if not data.get("ok"):
            log(f"[Telegram {method}] ошибка: {data}")
        return data
    except Exception as e:
        log(f"[Telegram {method}] исключение: {e}")
        return None


def send_text(text: str, chat_id: str = None, reply_markup: dict = None):
    data = {
        "chat_id": chat_id or TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return tg("sendMessage", data=data)


def send_photo(path: str, caption: str, chat_id: str = None, reply_markup: dict = None):
    data = {
        "chat_id": chat_id or TG_CHAT_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)

    try:
        with open(path, "rb") as f:
            res = tg("sendPhoto", data=data, files={"photo": f}, _timeout=60)
        if not res or not res.get("ok"):
            send_text(caption, chat_id, reply_markup)
    except Exception as e:
        log(f"Ошибка отправки фото: {e}")
        send_text(caption, chat_id, reply_markup)


def send_chat_action(action: str = "upload_photo", chat_id: str = None):
    return tg("sendChatAction", data={"chat_id": chat_id or TG_CHAT_ID, "action": action})


def get_status_keyboard():
    return {
        "keyboard": [
            [{"text": "📸 Статус линии"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


# ----------------------------------------------------------------- Сессия и куки
def normalize_same_site(val) -> str:
    v = (val or "").lower() if isinstance(val, str) else ""
    if v in ("no_restriction", "none"):
        return "None"
    if v == "strict":
        return "Strict"
    return "Lax"


def load_raw_cookies():
    raw = AUTH_STATE_JSON
    if not raw and os.path.exists("auth_state.json"):
        with open("auth_state.json", "r", encoding="utf-8") as f:
            raw = f.read().strip()
    if not raw:
        raise FileNotFoundError("Нет AUTH_STATE_JSON или файла auth_state.json")
    data = json.loads(raw)
    return data if isinstance(data, list) else data.get("cookies", [])


def refresh_session(raw_cookies):
    """POST /gologin/refresh -> обновляет токен сессии (JWT, XSRF)."""
    s = requests.Session()
    s.verify = False
    user_login = "Zhilkibagarov%40tn-ca.kz"
    for c in raw_cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", "tn-mes2.aspect-systems.ru"), path=c.get("path", "/"))
        if c["name"] == "STUDIO-auth":
            try:
                parsed = json.loads(urllib.parse.unquote(c["value"]))
                if parsed.get("user"):
                    user_login = urllib.parse.quote(parsed["user"])
            except Exception:
                pass

    result = {"jwt": None, "xsrf": None, "auth": None, "DM_BASE_URL": "http://dm-api/rest/", "WORKSPACE": "aspect"}
    try:
        r = s.post(f"{TARGET_URL}gologin/refresh", headers={"Content-Type": "application/json", "name": user_login}, timeout=30)
        if r.ok:
            js = r.json()
            for c_str in js.get("cookie", []) or []:
                kv = c_str.split(";", 1)[0]
                k, _, v = kv.partition("=")
                if k == "JWT-TOKEN":
                    result["jwt"] = v
                elif k == "XSRF-TOKEN":
                    result["xsrf"] = v
                elif k == "auth":
                    result["auth"] = v
            env = js.get("env", {}) or {}
            result["DM_BASE_URL"] = env.get("DM_BASE_URL", result["DM_BASE_URL"])
            result["WORKSPACE"] = env.get("WORKSPACE", result["WORKSPACE"])
            log(f"Сессия MES успешно продлена (JWT={bool(result['jwt'])}).")
        else:
            log(f"/gologin/refresh вернул код {r.status_code}")
    except Exception as e:
        log(f"/gologin/refresh ошибка: {e}")
    return result


def updated_raw_cookies(raw_cookies, ref):
    out = []
    for c in raw_cookies:
        c = dict(c)
        if c["name"] == "STUDIO-JWT-TOKEN" and ref["jwt"]:
            c["value"] = ref["jwt"]
        elif c["name"] == "STUDIO-XSRF-TOKEN" and ref["xsrf"]:
            c["value"] = ref["xsrf"]
        elif c["name"] == "STUDIO-auth" and ref["auth"]:
            c["value"] = ref["auth"]
        out.append(c)
    return out


def prepare_state_file() -> str:
    raw_cookies = load_raw_cookies()
    ref = refresh_session(raw_cookies)
    cookies_src = updated_raw_cookies(raw_cookies, ref)
    far_future = float(int(time.time()) + 30 * 86400)

    pw_cookies = []
    for c in cookies_src:
        same_site = normalize_same_site(c.get("sameSite"))
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", "tn-mes2.aspect-systems.ru"),
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": True if same_site == "None" else bool(c.get("secure", False)),
            "sameSite": same_site,
        }
        if c.get("expirationDate"):
            cookie["expires"] = max(float(c["expirationDate"]), far_future)
        pw_cookies.append(cookie)

    def val(name):
        return next((c["value"] for c in pw_cookies if c["name"] == name), "")

    extra = [
        ("JWT-TOKEN", ref["jwt"] or val("STUDIO-JWT-TOKEN"), True, True),
        ("XSRF-TOKEN", ref["xsrf"] or val("STUDIO-XSRF-TOKEN"), False, True),
        ("auth", ref["auth"] or val("STUDIO-auth"), False, False),
        ("pathName", "/", False, False),
    ]
    existing = {c["name"] for c in pw_cookies if c["domain"] == "tn-mes2.aspect-systems.ru"}
    for name, value, http_only, secure in extra:
        if name not in existing and value:
            pw_cookies.append({
                "name": name, "value": value, "domain": "tn-mes2.aspect-systems.ru", "path": "/",
                "httpOnly": http_only, "secure": secure, "sameSite": "Lax", "expires": far_future,
            })

    state = {
        "cookies": pw_cookies,
        "origins": [{
            "origin": TARGET_URL.rstrip("/"),
            "localStorage": [
                {"name": "DM_BASE_URL", "value": ref["DM_BASE_URL"]},
                {"name": "WORKSPACE", "value": ref["WORKSPACE"]},
                {"name": "loggedIn", "value": "true"},
                {"name": "loginBySAML", "value": "true"},
            ],
        }],
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    return STATE_FILE


# ----------------------------------------------------------------- Playwright-навигация
class MesNavigator:
    def __init__(self, page):
        self.page = page

    def is_login_page(self) -> bool:
        try:
            return self.page.locator(".loginFormContainer, input[type='password']").count() > 0
        except Exception:
            return False

    def open_almaty(self):
        """Открывает экран мониторинга линии завода Алматы."""
        for attempt in range(1, 4):
            try:
                log(f"Загрузка {TARGET_URL} (попытка {attempt}/3)...")
                self.page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
                break
            except Exception as e:
                log(f"Ошибка загрузки страницы: {e}")
                if attempt == 3:
                    raise
                time.sleep(3)

        self.page.wait_for_timeout(4000)
        if self.is_login_page():
            raise PermissionError("Сессия MES истекла, требуется повторный вход.")

        # Шаг 1: Нажимаем на карточку Алматы на главной странице
        log("Шаг 1: Нажимаем на карточку Алматы...")
        almaty_target = self.page.locator("text=ТЕХНОВЕНТ СТАНДАРТ").first
        if almaty_target.count() == 0:
            almaty_target = self.page.get_by_text("ТЕХНОВЕНТ").first
        if almaty_target.count() > 0:
            almaty_target.click()
        else:
            self.page.locator("text=АЛМАТЫ").first.click()

        # Ждем загрузки экрана ВСЕ ЗАВОДЫ / АЛМАТЫ
        self.page.wait_for_timeout(4000)

        # Шаг 2: Нажимаем на зеленый блок '1 линия' слева через нативный локатор
        log("Шаг 2: Нажимаем на зеленый блок '1 линия' слева...")
        line_selectors = [
            '[data-id="col-9363a9d851"]',
            '[card-id="c7d931c6-e645-46b0-941b-7fda5976b782"] .ibo-card-label',
            '[card-id="c7d931c6-e645-46b0-941b-7fda5976b782"]',
            'div[style*="rgb(118, 190, 169)"]',
        ]
        clicked = False
        for sel in line_selectors:
            loc = self.page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                clicked = True
                log(f"Успешный клик по селектору линии: {sel}")
                break

        if not clicked:
            self.page.mouse.click(43, 256)

        # Ждем перехода на детальный экран ЛИНИИ ЗАВОДА
        log("Ожидаем отображения экрана 'ЛИНИИ ЗАВОДА'...")
        try:
            self.page.locator("text=ЛИНИИ ЗАВОДА").first.wait_for(timeout=15000)
            log("Экран 'ЛИНИИ ЗАВОДА' найден и активен!")
        except Exception:
            pass

        # Даем 5 секунд дорисоваться всем таблицам и графикам
        self.page.wait_for_timeout(5000)

    def take_screenshot(self) -> str:
        # Устанавливаем оптимальный зум 85%, чтобы в кадр целиком вошли
        # и верхние технологические блоки, и нижняя таблица 'СОБЫТИЯ ЗА СМЕНУ'
        try:
            self.page.evaluate("document.body.style.zoom = '0.85'")
            self.page.wait_for_timeout(1000)
        except Exception:
            pass

        self.page.screenshot(path=SCREENSHOT, full_page=False)
        return SCREENSHOT


# ----------------------------------------------------------------- Обработка запроса фото
def capture_and_send_status(page: MesNavigator, chat_id: str):
    log(f"Формирование фото статуса для chat_id={chat_id}...")
    send_chat_action("upload_photo", chat_id)
    send_text("⏳ Получаю свежий скриншот экрана линии, подождите немного...", chat_id)

    try:
        page.open_almaty()
        photo_path = page.take_screenshot()
        caption = f"📊 <b>Линия {FACTORY_NAME}</b>\n🕒 <i>{now_local():%d.%m.%Y %H:%M:%S}</i>"
        send_photo(photo_path, caption, chat_id=chat_id, reply_markup=get_status_keyboard())
        log("Скриншот успешно отправлен!")
    except PermissionError:
        send_text("⚠️ <b>Сессия MES истекла.</b> Необходимо обновить токен (AUTH_STATE_JSON).", chat_id)
        raise
    except Exception as e:
        log(f"Ошибка получения скриншота: {e}")
        try:
            page.page.screenshot(path=SCREENSHOT)
            send_photo(SCREENSHOT, f"⚠️ Не удалось полностью открыть экран линии:\n<code>{e}</code>", chat_id=chat_id)
        except Exception:
            send_text(f"⚠️ Ошибка: {e}", chat_id)


# ----------------------------------------------------------------- Основной цикл бота
def is_status_command(text: str) -> bool:
    t = text.strip().lower()
    return t in (
        "/status", "status", "статус", "/статус",
        "📸 статус линии", "статус линии", "фото", "/photo"
    ) or t.startswith("/status") or t.startswith("статус")


def is_start_command(text: str) -> bool:
    t = text.strip().lower()
    return t in ("/start", "start", "/help", "help", "старт", "помощь")


def get_browser(p):
    """Запускает Chromium или системный Edge/Chrome."""
    for ch in (None, "msedge", "chrome"):
        try:
            kwargs = {"headless": True}
            if ch:
                kwargs["channel"] = ch
            return p.chromium.launch(**kwargs)
        except Exception:
            continue
    return p.chromium.launch(headless=True)


def run_bot():
    """Слушает сообщения в Telegram и отвечает фото на команду /status."""
    log("=" * 60)
    log("Запуск бота мониторинга линии (ответ ТОЛЬКО по команде /status)...")
    log("=" * 60)

    state_path = prepare_state_file()
    deadline = time.time() + RUN_MINUTES * 60
    offset = None
    pending_recent = []

    init_res = tg("getUpdates", data={"timeout": 0, "allowed_updates": json.dumps(["message"])})
    if init_res and init_res.get("ok") and init_res.get("result"):
        for upd in init_res["result"]:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            # Если команда пришла недавно (за последние 20 минут) — отвечаем на нее
            if time.time() - msg.get("date", 0) <= 1200:
                pending_recent.append(upd)
        log(f"Найдено {len(pending_recent)} недавних сообщений для обработки.")

    with sync_playwright() as p:
        browser = get_browser(p)
        context = browser.new_context(
            storage_state=state_path,
            viewport={"width": 1920, "height": 1150},
            locale="ru-RU",
            ignore_https_errors=True
        )
        page = context.new_page()
        navigator = MesNavigator(page)

        # Обрабатываем недавние команды, присланные до старта
        for update in pending_recent:
            msg = update.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if not TG_CHAT_ID or chat_id == TG_CHAT_ID:
                if is_status_command(text):
                    log(f"Обработка недавней команды /status от {chat_id}")
                    capture_and_send_status(navigator, chat_id)
                elif is_start_command(text):
                    send_text(
                        f"👋 Здравствуйте!\n\nЯ бот мониторинга завода <b>{FACTORY_NAME}</b>.\n"
                        f"Нажмите кнопку ниже или отправьте команду <b>/status</b>, чтобы получить скриншот.",
                        chat_id=chat_id,
                        reply_markup=get_status_keyboard()
                    )

        last_refresh_time = time.time()

        try:
            while time.time() < deadline:
                # Каждые 2 часа фоново продлеваем токен сессии
                if time.time() - last_refresh_time > 7200:
                    try:
                        raw = load_raw_cookies()
                        ref = refresh_session(raw)
                        last_refresh_time = time.time()
                    except Exception as e:
                        log(f"Фоновое продление токена: {e}")

                # Long polling Telegram
                remaining = max(1, min(25, int(deadline - time.time())))
                payload = {"timeout": remaining, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    payload["offset"] = offset

                res = tg("getUpdates", data=payload, _timeout=remaining + 15)
                if not res or not res.get("ok"):
                    time.sleep(2)
                    continue

                for update in res.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    text = msg.get("text") or ""

                    # Проверка авторизованного пользователя (если задан TG_CHAT_ID)
                    if TG_CHAT_ID and chat_id != TG_CHAT_ID:
                        log(f"Игнорируем сообщение от неизвестного чата {chat_id}")
                        continue

                    if is_start_command(text):
                        log(f"Получена команда /start от {chat_id}")
                        welcome = (
                            f"👋 Здравствуйте!\n\n"
                            f"Я бот мониторинга завода <b>{FACTORY_NAME}</b>.\n"
                            f"Нажмите кнопку ниже или отправьте команду <b>/status</b>, "
                            f"чтобы получить свежий скриншот экрана линии."
                        )
                        send_text(welcome, chat_id=chat_id, reply_markup=get_status_keyboard())

                    elif is_status_command(text):
                        log(f"Получена команда /status от {chat_id}")
                        capture_and_send_status(navigator, chat_id)

        except PermissionError:
            log("Критическая ошибка: сессия истекла.")
            sys.exit(1)
        finally:
            if offset is not None:
                tg("getUpdates", data={"offset": offset, "timeout": 0})
            browser.close()


def run_once():
    """Разовый запуск: сделать скриншот, отправить в Telegram и выйти."""
    log("Разовый запуск: отправка фото статуса...")
    state_path = prepare_state_file()
    with sync_playwright() as p:
        browser = get_browser(p)
        context = browser.new_context(
            storage_state=state_path,
            viewport={"width": 1920, "height": 1150},
            locale="ru-RU",
            ignore_https_errors=True
        )
        page = context.new_page()
        navigator = MesNavigator(page)
        try:
            capture_and_send_status(navigator, TG_CHAT_ID)
        finally:
            browser.close()


def refresh_only():
    raw = load_raw_cookies()
    ref = refresh_session(raw)
    if not ref["jwt"]:
        log("Не удалось продлить сессию.")
        return
    with open(REFRESHED_FILE, "w", encoding="utf-8") as f:
        json.dump(updated_raw_cookies(raw, ref), f, ensure_ascii=False)
    log(f"Новые куки сохранены в {REFRESHED_FILE}")


if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "bot"
    if mode in ("status", "once", "now"):
        run_once()
    elif mode == "refresh":
        refresh_only()
    else:
        run_bot()
