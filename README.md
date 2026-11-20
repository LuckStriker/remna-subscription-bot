# Remnawave Subscription Bot

**[English](#english) · [Русский](#русский)**

---

## English

A Telegram bot that runs a small subscription service on top of the
[Remnawave](https://github.com/remnawave) panel: users request access, pay and send receipts,
get reminders — the admin approves everything in a couple of taps and keeps the servers healthy
from the same chat.

### Features

**For users**
- Request access with a chosen username, or link an existing subscription to Telegram
- Subscription card: expiry date, traffic, connected devices (HWID), QR code, setup guides
- Payment flow: payment details → upload a receipt → admin approval → days added automatically
- Expiry reminders pinned at the top of the chat (configurable schedule, daytime only); after renewal the reminder turns into "✅ renewed"
- Referral program: trial days for the invitee, bonus days for both after the first payment
- Loyalty bonus for paying on time several months in a row (e.g. 3 months → +7 days, 6 → +14), streak shown in the profile
- Bonus for the first payment made through the bot
- Birthday gift days, promo campaigns ("first N users"), price lock for early customers
- Automatic chat cleanup

**For the admin**
- Create users, add/remove days, find users, enable/disable, reset traffic
- Receipt queue: approve by preset periods, by custom days or by the paid amount
- Receipt auto-check (PDF): payment date and time, amount vs tariff, recipient, status, SBP operation ID —
  flags old receipts, wrong recipient, underpayment, PDF-editor traces and the same payment sent twice
  (even as a different file). The admin still makes the final decision
- Rejecting a receipt asks for a reason; the user gets a clear explanation and what to do next
- Announcements and gifts to all users (DM and/or group), polls with feedback threads
- Server maintenance over SSH: Remnawave update (panel first, then nodes), OS update, reboot, status
- After every update — automatic health check of the whole service
- Monitoring: panel and nodes watchdog with confirmed alerts, disk and certificate checks,
  missed night-job alerts

**For the group / channel**
- Welcome message for newcomers (auto-deleted), quiet pinning, auto-deletion of service posts

### Stack
Python 3.12 · aiogram 3 · httpx · asyncssh · pypdf · SQLite · Remnawave API 3.x
(supports a panel behind a secret cookie and/or client certificates — mTLS)

### Quick start
```bash
git clone <this repo> /opt/remnabot && cd /opt/remnabot
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env                 # fill in BOT_TOKEN, ADMIN_IDS, PANEL_URL, API_TOKEN
cp servers.example.json servers.json # optional: servers for SSH maintenance buttons
venv/bin/python main.py              # or install remnabot.service as a systemd unit
```
All settings are documented in `.env.example`. Secrets, the database and `servers.json`
are excluded from git by `.gitignore`.

### Notes
- The bot needs a Remnawave API token with access to users and nodes.
- For SSH maintenance the bot uses a key-based login to each server (`key_path` in `servers.json`).
- User-facing texts are in Russian.

### License
[MIT](LICENSE)

---

## Русский

Telegram-бот для небольшого сервиса подписок на базе панели
[Remnawave](https://github.com/remnawave): пользователи запрашивают доступ, оплачивают и присылают
чеки, получают напоминания, а админ всё подтверждает в пару нажатий и следит за серверами из того же чата.

### Возможности

**Для пользователя**
- Запрос доступа со своим именем или привязка уже существующей подписки к Telegram
- Карточка подписки: срок, трафик, подключённые устройства (HWID), QR-код, инструкции по подключению
- Оплата: реквизиты → отправка чека → проверка админом → дни начисляются автоматически
- Напоминания об окончании подписки закрепляются вверху чата (по расписанию, только днём); после продления напоминание само становится «✅ продлено»
- Реферальная программа: пробные дни приглашённому, бонус обоим после первой оплаты
- Бонус за оплаты без перерыва (например, 3 месяца подряд → +7 дней, 6 → +14), серия видна в профиле
- Бонус за первую оплату через бота
- Подарок на день рождения, акции «первые N», фиксация цены для ранних клиентов
- Автоочистка переписки

**Для админа**
- Создание пользователей, начисление и списание дней, поиск, включение и отключение, сброс трафика
- Очередь чеков: одобрение на готовый срок, на своё количество дней или по сумме из чека
- Автопроверка чека (PDF): дата и время платежа, сумма против тарифа, получатель, статус, номер операции СБП —
  помечает старые чеки, чужого получателя, недоплату, следы PDF-редактора и повтор той же оплаты
  (даже другим файлом). Окончательное решение — за админом
- Отказ по чеку — с выбором причины: пользователь получает понятное объяснение и что сделать дальше
- Объявления и подарки всем (в личку и/или в группу), опросы с обратной связью
- Обслуживание серверов по SSH: обновление Remnawave (сначала панель, потом ноды), обновление ОС,
  перезагрузка, статус
- После каждого обновления — автоматическая полная проверка сервиса
- Мониторинг: сторож панели и нод с подтверждёнными тревогами, проверка дисков и сертификатов,
  тревога о пропущенных ночных заданиях

**Для группы / канала**
- Приветствие новичков (удаляется само), тихое закрепление, автоудаление служебных постов

### Технологии
Python 3.12 · aiogram 3 · httpx · asyncssh · pypdf · SQLite · Remnawave API 3.x
(поддерживает панель за секретной cookie и/или по клиентским сертификатам — mTLS)

### Быстрый старт
```bash
git clone <этот репозиторий> /opt/remnabot && cd /opt/remnabot
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env                 # заполнить BOT_TOKEN, ADMIN_IDS, PANEL_URL, API_TOKEN
cp servers.example.json servers.json # необязательно: серверы для кнопок обслуживания
venv/bin/python main.py              # или установить remnabot.service как службу systemd
```
Все настройки описаны в `.env.example`. Секреты, база и `servers.json` исключены из git через `.gitignore`.

### Примечания
- Боту нужен API-токен Remnawave с доступом к пользователям и нодам.
- Для обслуживания по SSH бот входит на серверы по ключу (`key_path` в `servers.json`).
- Тексты для пользователей — на русском.

### Лицензия
[MIT](LICENSE)
