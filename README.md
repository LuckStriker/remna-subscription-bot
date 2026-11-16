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
- Expiry reminders (configurable schedule) with one reminder kept in the chat at a time
- Referral program: trial days for the invitee, bonus days for both after the first payment
- Birthday gift days, promo campaigns ("first N users"), price lock for early customers
- Automatic chat cleanup

**For the admin**
- Create users, add/remove days, find users, enable/disable, reset traffic
- Receipt queue: approve by preset periods, by custom days or by the paid amount
- Announcements and gifts to all users (DM and/or group), polls with feedback threads
- Server maintenance over SSH: Remnawave update (panel first, then nodes), OS update, reboot, status
- After every update — automatic health check of the whole service
- Monitoring: panel and nodes watchdog with confirmed alerts, disk and certificate checks,
  missed night-job alerts

**For the group / channel**
- Welcome message for newcomers (auto-deleted), quiet pinning, auto-deletion of service posts

### Stack
Python 3.12 · aiogram 3 · httpx · asyncssh · SQLite · Remnawave API 3.x
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
- Напоминания об окончании подписки (по расписанию), в чате всегда одно актуальное
- Реферальная программа: пробные дни приглашённому, бонус обоим после первой оплаты
- Подарок на день рождения, акции «первые N», фиксация цены для ранних клиентов
- Автоочистка переписки

**Для админа**
- Создание пользователей, начисление и списание дней, поиск, включение и отключение, сброс трафика
- Очередь чеков: одобрение на готовый срок, на своё количество дней или по сумме из чека
- Объявления и подарки всем (в личку и/или в группу), опросы с обратной связью
- Обслуживание серверов по SSH: обновление Remnawave (сначала панель, потом ноды), обновление ОС,
  перезагрузка, статус
- После каждого обновления — автоматическая полная проверка сервиса
- Мониторинг: сторож панели и нод с подтверждёнными тревогами, проверка дисков и сертификатов,
  тревога о пропущенных ночных заданиях

**Для группы / канала**
- Приветствие новичков (удаляется само), тихое закрепление, автоудаление служебных постов

### Технологии
Python 3.12 · aiogram 3 · httpx · asyncssh · SQLite · Remnawave API 3.x
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
