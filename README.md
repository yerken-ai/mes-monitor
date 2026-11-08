# Мониторинг линии Алматы (MES) → Telegram (через GitHub Actions)

Проект автоматически открывает `https://tn-mes2.aspect-systems.ru/` с вашей сохраненной сессией (без необходимости каждый раз вводить 2FA), каждые 5 минут обновляет таблицу, находит строку **«Алматы»** и присылает в Telegram **текст статуса + свежий скриншот таблицы**.

---

## Шаг 1. Создайте Telegram-бота (2 минуты)
1. Откройте в Telegram бота **[@BotFather](https://t.me/BotFather)** и отправьте команду `/newbot`.
2. Придумайте имя и username бота — в ответ вы получите **токен** (например, `7123456789:AAH...`).
3. Напишите вашему новому боту любое сообщение (нажмите `Start`), чтобы он имел право вам писать.
4. Узнайте свой **Chat ID**: напишите боту **[@userinfobot](https://t.me/userinfobot)** — он пришлет ваш `Id` (число, например `123456789`).

---

## Шаг 2. Скопируйте вашу активную сессию из браузера (без установки программ на ПК)
Поскольку вы уже вошли на сайт `https://tn-mes2.aspect-systems.ru/` в своем браузере:
1. Установите в браузер (Chrome / Edge / Яндекс) бесплатное расширение **[Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)**.
2. Перейдите на открытую вкладку `https://tn-mes2.aspect-systems.ru/`, где видна таблица.
3. Нажмите на иконку **Cookie-Editor** в правом верхнем углу браузера → нажмите кнопку **Export** (внизу справа) → **Export as JSON**.
4. Ваши Cookies скопированы в буфер обмена!

*(Альтернативно: если на ПК установлен Python, можно выполнить `python save_session.py` — он создаст файл `auth_state.json`).*

---

## Шаг 3. Загрузите проект в GitHub и добавьте секреты
1. Создайте на GitHub новый **Private (приватный)** репозиторий и загрузите туда файлы из этой папки (`monitor.py`, `save_session.py`, `requirements.txt`, `.github/workflows/monitor.yml`).
2. В репозитории на GitHub перейдите в **Settings → Secrets and variables → Actions → New repository secret** и создайте 3 секрета:
   - `TG_BOT_TOKEN` — токен вашего Telegram-бота.
   - `TG_CHAT_ID` — ваш цифровой ID в Telegram.
   - `AUTH_STATE_JSON` — откройте файл `auth_state.json` блокнотом, скопируйте **весь текст целиком** и вставьте сюда.
3. Перейдите во вкладку **Actions** на GitHub, выберите слева **MES Almaty Line Monitor** и нажмите **Run workflow**, чтобы сразу проверить отправку первого сообщения!
