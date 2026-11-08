"""
Мониторинг линии Алматы (MES tn-mes2.aspect-systems.ru) + Telegram-бот.

Логика:
  * Раз в CHECK_INTERVAL_SECONDS (10 мин) обновляет экран "ВСЕ ЗАВОДЫ / АЛМАТЫ / ЛИНИИ ЗАВОДА".
  * Если линия СЕЙЧАС стоит в "Аварийном простое" дольше ALERT_THRESHOLD_MINUTES (10 мин)
    -> отправляет в Telegram скриншот экрана. В остальных случаях молчит.
  * Если пользователь пишет боту "статус" (или /status) -> присылает скриншот экрана.

Режимы запуска:
  python monitor.py           - основной цикл (работает RUN_MINUTES минут)
  python monitor.py refresh   - только продлить сессию и сохранить куки в refreshed_auth.json
"""
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

TARGET_URL = os.getenv("TARGET_URL", "https://tn-mes2.aspect-systems.ru/").rstrip("/") + "/"
FACTORY_NAME = os.getenv("LINE_NAME", "Алматы")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
AUTH_STATE_JSON = os.getenv("AUTH_STATE_JSON", "").strip()

STATE_FILE = "auth_state_pw.json"
REFRESHED_FILE = "refreshed_auth.json"

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
ALERT_THRESHOLD_MINUTES = int(os.getenv("ALERT_THRESHOLD_MINUTES", "10"))
REMIND_EVERY_MINUTES = int(os.getenv("REMIND_EVERY_MINUTES", "30"))  # 0 = только одно уведомление на простой
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "345"))
STATUS_MAX_AGE_SECONDS = 20 * 60  # на старте отвечаем на "статус", присланные не раньше 20 мин назад

EMERGENCY_DOWNTIME_TYPE_ID = "fb9cb97d-8082-47a7-a01d-d172401a41a8"
ALMATY_TZ = timezone(timedelta(hours=5))
SCREENSHOT = "almaty.png"


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
    try:
        r = requests.post(url, timeout=timeout, **kwargs)
        data = r.json()
        if not data.get("ok"):
            log(f"[Telegram {method}] ошибка: {data}")
        return data
    except Exception as e:
        log(f"[Telegram {method}] исключение: {e}")
        return None


def send_text(text: str, chat_id: str = None):
    tg("sendMessage", data={"chat_id": chat_id or TG_CHAT_ID, "text": text, "parse_mode": "HTML"})


def send_photo(path: str, caption: str, chat_id: str = None):
    try:
        with open(path, "rb") as f:
            res = tg(
                "sendPhoto",
                data={"chat_id": chat_id or TG_CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"},
                files={"photo": f},
                _timeout=60,
            )
        if not res or not res.get("ok"):
            send_text(caption, chat_id)
    except Exception as e:
        log(f"Ошибка отправки фото: {e}")
        send_text(caption, chat_id)


# ----------------------------------------------------------------- Сессия
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
        raise FileNotFoundError("Нет AUTH_STATE_JSON / auth_state.json")
    data = json.loads(raw)
    return data if isinstance(data, list) else data.get("cookies", [])


def refresh_session(raw_cookies):
    """POST /gologin/refresh -> новый JWT/XSRF/auth + env (DM_BASE_URL, WORKSPACE)."""
    s = requests.Session()
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
            log(f"Сессия продлена (jwt={'да' if result['jwt'] else 'нет'}).")
        else:
            log(f"/gologin/refresh вернул {r.status_code}")
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


# ----------------------------------------------------------------- Разбор простоя
def parse_dt(value):
    """Пытается распознать дату/время начала простоя. Возвращает aware datetime (Алматы) или None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, ALMATY_TZ)
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(ALMATY_TZ) if dt.tzinfo else dt.replace(tzinfo=ALMATY_TZ)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=ALMATY_TZ)
        except ValueError:
            continue
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if m:
        n = now_local()
        dt = n.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if dt > n + timedelta(minutes=1):
            dt -= timedelta(days=1)
        return dt
    return None


def parse_duration_minutes(text: str):
    h = re.search(r"(\d+)\s*ч", text)
    m = re.search(r"(\d+)\s*мин", text)
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def find_downtime_records(obj, found):
    """Рекурсивно ищет в JSON объекты, у которых есть поле downtimeType/downtimeStartedAt."""
    if isinstance(obj, dict):
        if "downtimeType" in obj or "downtimeStartedAt" in obj or "downtimeTypeId" in obj:
            found.append(obj)
        for v in obj.values():
            find_downtime_records(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_downtime_records(v, found)


class Monitor:
    def __init__(self, page):
        self.page = page
        self.api_records = []
        page.on("response", self._on_response)

    def _on_response(self, response):
        try:
            if "getIBOViewData" not in response.url:
                return
            data = response.json()
            found = []
            find_downtime_records(data, found)
            if found:
                self.api_records = found
        except Exception:
            pass

    # ----------------------------------------------------------- навигация
    def is_login_page(self) -> bool:
        try:
            return self.page.locator(".loginFormContainer, input[type='password']").count() > 0
        except Exception:
            return False

    def events_header(self):
        return self.page.get_by_text(re.compile(r"события\s+за\s+смену", re.I)).first

    def open_almaty(self):
        """Открывает детальный экран мониторинга линии Алматы: ВСЕ ЗАВОДЫ / АЛМАТЫ / ЛИНИИ ЗАВОДА."""
        self.api_records = []
        for attempt in range(1, 4):
            try:
                log(f"Загружаем {TARGET_URL} (попытка {attempt}/3)...")
                self.page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
                break
            except Exception as e:
                log(f"Предупреждение при загрузке страницы: {e}")
                if attempt == 3:
                    raise
                time.sleep(3)

        self.page.wait_for_timeout(4000)
        if self.is_login_page():
            raise PermissionError("login")

        # ШАГ 1: Нажимаем кнопку 'АЛМАТЫ' в верхнем тулбаре
        log("Шаг 1: Ищем и нажимаем на 'АЛМАТЫ' в верхней панели заводов...")
        almaty_clicked = False
        try:
            coords = self.page.evaluate("""() => {
                const all = Array.from(document.querySelectorAll('*'));
                const btn = all.find(e => {
                    const t = (e.innerText || '').trim();
                    const r = e.getBoundingClientRect();
                    return t === 'АЛМАТЫ' && r.top >= 0 && r.top < 80 && r.width > 20 && r.height > 10;
                });
                if (btn) {
                    btn.click();
                    const r = btn.getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                }
                return null;
            }""")
            if coords:
                self.page.mouse.click(coords["x"], coords["y"])
                almaty_clicked = True
                log(f"Кликнули на 'АЛМАТЫ' в тулбаре по координатам ({coords['x']}, {coords['y']})")
        except Exception as e:
            log(f"Ошибка клика по 'АЛМАТЫ' в тулбаре: {e}")

        # Если не нашли в тулбаре через координаты, пробуем studio.gotoIbo на страницу завода Алматы
        if not almaty_clicked:
            log("Пробуем переход на завод Алматы через studio.gotoIbo...")
            try:
                self.page.evaluate("""() => {
                    if (window.studio && window.studio.gotoIbo) {
                        window.studio.gotoIbo({
                            iboId: '52273723-fd3f-4c30-83da-a0f2763522bc',
                            mode: 'gridView',
                            viewId: 'a26792f5-d534-4c6c-a647-f75d66fc0049',
                            params: { workcenterId: '1261cb69-ea71-402d-b8ca-817a9d0dcd77' }
                        });
                    }
                }""")
            except Exception as e:
                log(f"gotoIbo завода Алматы: {e}")

        # Ждем отображения экрана завода Алматы
        log("Ожидаем загрузки экрана 'ВСЕ ЗАВОДЫ / АЛМАТЫ'...")
        self.page.wait_for_timeout(4000)

        # ШАГ 2: Нажимаем на зеленый блок '1 линия' слева
        log("Шаг 2: Ищем и нажимаем на зеленый блок '1 линия' слева...")
        line_clicked = False
        try:
            line_coords = self.page.evaluate("""() => {
                const all = Array.from(document.querySelectorAll('*'));
                const matches = all.filter(e => {
                    const t = (e.innerText || '').trim();
                    const r = e.getBoundingClientRect();
                    const hasText = (t === '1\\nлиния' || t === '1 линия' || t === 'линия' || (t.includes('1') && t.includes('линия') && t.length < 20));
                    const onLeft = r.left >= 0 && r.left < 160;
                    const inMiddleY = r.top > 60 && r.top < 650;
                    const hasSize = r.width > 20 && r.height > 20;
                    return hasText && onLeft && inMiddleY && hasSize;
                });
                if (matches.length > 0) {
                    const target = matches[0];
                    target.click();
                    target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    const r = target.getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                }
                return null;
            }""")
            if line_coords:
                self.page.mouse.click(line_coords["x"], line_coords["y"])
                line_clicked = True
                log(f"Кликнули на зеленый блок '1 линия' по координатам ({line_coords['x']}, {line_coords['y']})")
        except Exception as e:
            log(f"Ошибка клика по '1 линия': {e}")

        # Дополнительно: вызываем прямой gotoIbo детального экрана линии Алматы
        log("Вызываем gotoIbo для детального экрана линии Алматы...")
        try:
            self.page.evaluate("""() => {
                if (window.studio && window.studio.gotoIbo) {
                    window.studio.gotoIbo({
                        iboId: 'eba1ffb4-f2b1-451a-835e-446af19f93f8',
                        viewId: '97c00ac7-f407-4150-8b3b-991bda19c595',
                        mode: 'gridView',
                        params: {
                            enterprise: '1261cb69-ea71-402d-b8ca-817a9d0dcd77',
                            workcenterId: 'c7d931c6-e645-46b0-941b-7fda5976b782'
                        }
                    }, 'self');
                }
            }""")
        except Exception as e:
            log(f"gotoIbo детального экрана: {e}")

        # ШАГ 3: Ждем загрузки детального экрана мониторинга линии (СОБЫТИЯ ЗА СМЕНУ)
        log("Шаг 3: Ожидаем загрузки детального экрана 'ЛИНИИ ЗАВОДА' / 'СОБЫТИЯ ЗА СМЕНУ'...")
        try:
            self.page.locator("text=СОБЫТИЯ ЗА СМЕНУ, text=ЛИНИИ ЗАВОДА").first.wait_for(timeout=25000)
            log("Экран 'ЛИНИИ ЗАВОДА' успешно загружен!")
        except Exception:
            log("Предупреждение: таймер ожидания экрана истек, фиксируем текущий экран.")

        # Даем 6 секунд дорисоваться всем таблицам, графикам и статусам
        self.page.wait_for_timeout(6000)

    def take_screenshot(self) -> str:
        # Прокручиваем внутренние контейнеры вниз, чтобы были видны и параметры линии, и "События за смену"
        try:
            self.page.evaluate(
                """() => {
                    for (const el of document.querySelectorAll('*')) {
                        const st = getComputedStyle(el);
                        if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
                            el.scrollTop = el.scrollHeight;
                        }
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
            self.page.wait_for_timeout(1000)
        except Exception:
            pass
        self.page.screenshot(path=SCREENSHOT, full_page=False)
        return SCREENSHOT

    # ----------------------------------------------------------- статус
    def detect_status(self) -> dict:
        """Возвращает {'active', 'emergency', 'type', 'start', 'minutes', 'source'}."""
        st = {"active": False, "emergency": False, "type": None, "start": None, "minutes": None, "source": None}

        # 1) Данные, которые загрузил сам сайт (карточка линии)
        for rec in self.api_records:
            dtype = rec.get("downtimeType")
            if isinstance(dtype, dict):
                dtype = dtype.get("name") or dtype.get("value")
            dtype_id = rec.get("downtimeTypeId")
            if isinstance(dtype_id, dict):
                dtype_id = dtype_id.get("id")
            if dtype or dtype_id:
                start = parse_dt(rec.get("downtimeStartedAt"))
                st.update(
                    active=True,
                    type=str(dtype) if dtype else None,
                    emergency=(dtype_id == EMERGENCY_DOWNTIME_TYPE_ID) or ("авар" in str(dtype or "").lower()),
                    start=start,
                    minutes=int((now_local() - start).total_seconds() // 60) if start else None,
                    source="api",
                )
                return st
        if self.api_records:
            st["source"] = "api"

        # 2) Надписи "Начало простоя / Тип простоя" на экране
        try:
            body = self.page.inner_text("body")
        except Exception:
            body = ""
        m_type = re.search(r"Тип простоя:\s*\n?\s*([^\n]+)", body)
        if m_type:
            m_start = re.search(r"Начало простоя:\s*\n?\s*([^\n]+)", body)
            start = parse_dt(m_start.group(1)) if m_start else None
            dtype = m_type.group(1).strip()
            st.update(
                active=True, type=dtype, emergency="авар" in dtype.lower(), start=start,
                minutes=int((now_local() - start).total_seconds() // 60) if start else None, source="dom",
            )
            return st

        # 3) Таблица "События за смену": аварийный простой без времени окончания
        try:
            rows = self.page.locator("tr:has-text('Аварийный простой'), [role='row']:has-text('Аварийный простой')").all()
            for row in rows:
                text = " ".join(row.inner_text().split())
                m = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})?", text)
                if m and not m.group(2):
                    start = parse_dt(m.group(1))
                    minutes = parse_duration_minutes(text)
                    if minutes is None and start:
                        minutes = int((now_local() - start).total_seconds() // 60)
                    st.update(active=True, emergency=True, type="Аварийный простой", start=start, minutes=minutes, source="table")
                    return st
        except Exception:
            pass

        if st["source"] is None:
            st["source"] = "dom"
        return st


def status_caption(st: dict, title: str) -> str:
    lines = [title, f"🕒 <code>{now_local():%d.%m.%Y %H:%M}</code>"]
    if st["active"]:
        lines.append(f"Состояние: <b>{st['type'] or 'простой'}</b>")
        if st["start"]:
            lines.append(f"Начало: {st['start']:%H:%M}")
        if st["minutes"] is not None:
            lines.append(f"Длительность: <b>{st['minutes']} мин.</b>")
    else:
        lines.append("Состояние: ✅ линия работает (активного простоя нет)")
    return "\n".join(lines)


# ----------------------------------------------------------------- Основной цикл
def get_initial_offset():
    """Пропускаем старые сообщения, но возвращаем свежие 'статус' (до 20 мин), чтобы ответить на них."""
    res = tg("getUpdates", data={"timeout": 0, "allowed_updates": json.dumps(["message"])})
    pending, offset = [], None
    if res and res.get("ok"):
        for upd in res["result"]:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if time.time() - msg.get("date", 0) <= STATUS_MAX_AGE_SECONDS:
                pending.append(upd)
    return offset, pending


def is_status_command(upd) -> bool:
    msg = upd.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != TG_CHAT_ID:
        return False
    text = (msg.get("text") or "").strip().lower()
    return text in ("статус", "/status", "/статус", "status", "/start") or text.startswith("статус")


def main():
    state_path = prepare_state_file()
    deadline = time.time() + RUN_MINUTES * 60
    last_alert_key, last_alert_time = None, 0.0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=state_path, viewport={"width": 1920, "height": 1150}, locale="ru-RU")
        page = context.new_page()
        mon = Monitor(page)

        def refresh_and_detect():
            mon.open_almaty()
            st = mon.detect_status()
            log(f"Статус: {st}")
            return st

        def handle_status_request(chat_id):
            try:
                tg("sendChatAction", data={"chat_id": chat_id, "action": "upload_photo"})
                st = refresh_and_detect()
                send_photo(mon.take_screenshot(), f"📊 <b>Линия {FACTORY_NAME}</b> ({now_local():%d.%m.%Y %H:%M})", chat_id)
            except PermissionError:
                send_text("⚠️ Сессия MES истекла — нужно обновить куки (секрет AUTH_STATE_JSON).", chat_id)
                raise
            except Exception as e:
                log(f"Ошибка обработки 'статус': {e}")
                try:
                    page.screenshot(path=SCREENSHOT)
                    send_photo(SCREENSHOT, f"⚠️ Не удалось полностью загрузить экран: {e}", chat_id)
                except Exception:
                    send_text(f"⚠️ Ошибка: {e}", chat_id)

        offset, pending = get_initial_offset()
        for upd in pending:
            if is_status_command(upd):
                handle_status_request(TG_CHAT_ID)
                break

        next_check = time.time()
        fails = 0
        try:
            while time.time() < deadline:
                # ---- плановая проверка раз в 10 минут
                if time.time() >= next_check:
                    next_check = time.time() + CHECK_INTERVAL_SECONDS
                    try:
                        st = refresh_and_detect()
                        fails = 0
                        if st["active"] and st["emergency"] and (st["minutes"] or 0) > ALERT_THRESHOLD_MINUTES:
                            key = st["start"].isoformat() if st["start"] else "unknown"
                            remind_due = REMIND_EVERY_MINUTES > 0 and time.time() - last_alert_time >= REMIND_EVERY_MINUTES * 60
                            if key != last_alert_key or remind_due:
                                send_photo(mon.take_screenshot(), status_caption(st, f"🚨 <b>АВАРИЙНЫЙ ПРОСТОЙ — линия {FACTORY_NAME}</b>"))
                                last_alert_key, last_alert_time = key, time.time()
                        else:
                            last_alert_key = None if not st["active"] else last_alert_key
                    except PermissionError:
                        send_text("⚠️ <b>Сессия MES истекла.</b> Нужно заново выгрузить куки и обновить секрет AUTH_STATE_JSON.")
                        sys.exit(1)
                    except Exception as e:
                        fails += 1
                        log(f"Ошибка проверки #{fails}: {e}")

                # ---- ждём сообщения боту (long polling) до следующей проверки
                wait = max(1, min(25, int(next_check - time.time()), int(deadline - time.time())))
                data = {"timeout": wait, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    data["offset"] = offset
                res = tg("getUpdates", data=data, _timeout=wait + 15)
                if res and res.get("ok"):
                    for upd in res["result"]:
                        offset = upd["update_id"] + 1
                        if is_status_command(upd):
                            log("Получена команда 'статус'")
                            handle_status_request(TG_CHAT_ID)
                else:
                    time.sleep(3)
        except PermissionError:
            sys.exit(1)
        finally:
            # подтверждаем обработанные сообщения, чтобы следующий запуск их не повторил
            if offset is not None:
                tg("getUpdates", data={"offset": offset, "timeout": 0})
            browser.close()


def run_once_status():
    log("Запуск однократной проверки статуса...")
    state_path = prepare_state_file()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=state_path, viewport={"width": 1920, "height": 1150}, locale="ru-RU")
        page = context.new_page()
        mon = Monitor(page)
        try:
            mon.open_almaty()
            st = mon.detect_status()
            log(f"Статус определен: {st}")
            photo = mon.take_screenshot()
            caption = f"📊 <b>Линия {FACTORY_NAME}</b> ({now_local():%d.%m.%Y %H:%M})"
            send_photo(photo, caption)
            log("Скриншот успешно отправлен в Telegram.")
        except PermissionError:
            send_text("⚠️ <b>Сессия MES истекла.</b> Пожалуйста, обновите секрет AUTH_STATE_JSON.")
            sys.exit(1)
        except Exception as e:
            log(f"Ошибка при получении статуса: {e}")
            try:
                page.screenshot(path=SCREENSHOT)
                send_photo(SCREENSHOT, f"⚠️ Ошибка при открытии страницы: {e}")
            except Exception:
                send_text(f"⚠️ Ошибка: {e}")
            sys.exit(1)
        finally:
            browser.close()


def refresh_only():
    raw = load_raw_cookies()
    ref = refresh_session(raw)
    if not ref["jwt"]:
        log("Не удалось продлить сессию — секрет не обновляем.")
        return
    with open(REFRESHED_FILE, "w", encoding="utf-8") as f:
        json.dump(updated_raw_cookies(raw, ref), f, ensure_ascii=False)
    log(f"Сохранено {REFRESHED_FILE}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    if mode == "refresh":
        refresh_only()
    elif mode in ("status", "once"):
        run_once_status()
    else:
        main()
