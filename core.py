"""Общие объекты (API, БД) и помощники, которые используют все хендлеры."""
import html
import io
import re
import time
from datetime import datetime, timedelta, timezone

import qrcode
from aiogram.types import BufferedInputFile

from config import settings
from db import DB
from remnawave import Remnawave, human_bytes, parse_dt

api = Remnawave(settings.panel_url, settings.api_token, settings.panel_cookie,
                settings.panel_client_cert, settings.panel_client_key)
db = DB(settings.db_path)

STATUS_RU = {
    "ACTIVE": "🟢 активна",
    "DISABLED": "⛔ отключена",
    "LIMITED": "🟠 лимит трафика исчерпан",
    "EXPIRED": "🔴 истекла",
}


def is_admin(tg_id: int) -> bool:
    return tg_id in settings.admin_ids


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def username_for(tg_id: int) -> str:
    return f"{settings.username_prefix}{tg_id}"


async def find_user(tg_id: int) -> dict | None:
    """Ищет пользователя панели по Telegram ID: сперва по сохранённой привязке, затем по username tg_<id>."""
    link = db.get_link(tg_id)
    if link:
        user = await api.get_user(link["panel_id"])
        if user:
            return user
        db.drop_link(tg_id)
    user = await api.get_user_by_username(username_for(tg_id))
    if not user and time.monotonic() - _miss_cache.get(tg_id, -1e9) > 120:
        # Аккаунт мог быть создан вручную с другим username, но с заполненным telegramId
        async for u in api.iter_users():
            if u.get("telegramId") == tg_id:
                user = u
                break
        if not user:
            _miss_cache[tg_id] = time.monotonic()  # не сканируем панель на каждое нажатие
    if user:
        _miss_cache.pop(tg_id, None)
        db.set_link(tg_id, user["id"], user["username"])
        lock_price(tg_id, user)
    return user


_miss_cache: dict[int, float] = {}


# ---------------- цена: индексация с PRICE_NEW_FROM, старая цена закрепляется за нынешними клиентами ----------------
def _msk_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) - timedelta(hours=3)


def price_switched() -> bool:
    return settings.price_raise and datetime.now(timezone.utc) >= _msk_date(settings.price_new_from)


def lock_price(tg_id: int, user: dict) -> None:
    """Нынешний клиент (аккаунт в панели создан до PRICE_LOCK_CUTOFF включительно), привязавший Telegram
    до PRICE_NEW_FROM, навсегда сохраняет старую цену."""
    if datetime.now(timezone.utc) >= _msk_date(settings.price_new_from) or db.kv_get(f"price_lock:{tg_id}"):
        return  # после даты повышения новые закрепления не выдаются
    created = parse_dt(user.get("createdAt"))
    cutoff_end = _msk_date(settings.price_lock_cutoff) + timedelta(days=1)
    if created and created < cutoff_end:
        db.kv_set(f"price_lock:{tg_id}", str(settings.price_old))


def price_for(tg_id: int | None) -> int:
    if not price_switched():
        return settings.price_old
    return int(db.kv_get(f"price_lock:{tg_id}") or settings.price_new) if tg_id else settings.price_new


async def unlinked_users() -> list[dict]:
    """Пользователи панели без Telegram — кандидаты на привязку."""
    linked = {row["panel_id"] for row in db.conn.execute("SELECT panel_id FROM links")}
    return [u async for u in api.iter_users() if not u.get("telegramId") and u["id"] not in linked]


async def link_user(tg_id: int, user: dict) -> dict:
    updated = await api.update_user(user["id"], telegramId=tg_id)
    db.set_link(tg_id, updated["id"], updated["username"])
    _miss_cache.pop(tg_id, None)
    lock_price(tg_id, updated)
    return updated


async def resolve_target(query: str) -> dict | None:
    """Админский поиск: Telegram ID, username или #id панели."""
    query = query.strip().lstrip("@")
    if query.startswith("#") and query[1:].isdigit():
        return await api.get_user(int(query[1:]))
    if query.isdigit():
        user = await find_user(int(query))
        if user:
            return user
    return await api.get_user_by_username(query)


SHORT_UUID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
# Правило Remnawave для username: 3–36 символов, латиница, цифры, _ и -
USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{3,36}")
RESERVED_NAMES = {"admin", "administrator", "root", "support", "bot", "system", "panel", "null"}


MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
             "октября", "ноября", "декабря"]
BDAY_RE = re.compile(r"\s*(\d{1,2})\s*[.\-/ ]\s*(\d{1,2})(?:\s*[.\-/ ]\s*(\d{2}|\d{4}))?\s*")


def parse_birthday(text: str) -> tuple[int, int] | None:
    """«14.03», «14-3», «14.03.1995» → (14, 3). Год не храним — он не нужен."""
    m = BDAY_RE.fullmatch(text or "")
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    try:
        datetime(2000, month, day)  # 2000 — високосный, 29.02 допустимо
    except ValueError:
        return None
    return day, month


def fmt_birthday(day: int, month: int) -> str:
    return f"{day} {MONTHS_RU[month - 1]}"


def valid_username(name: str) -> bool:
    return bool(USERNAME_RE.fullmatch(name)) and name.lower() not in RESERVED_NAMES


async def find_by_sub_link(text: str) -> dict | None:
    """Находит пользователя по ссылке подписки (или голому shortUuid) из произвольного текста."""
    parts = [p for p in re.split(r"[/?#\s]+", text.strip()) if p]
    for cand in [p for p in reversed(parts) if SHORT_UUID_RE.fullmatch(p)][:3]:
        user = await api.get_user_by_short_uuid(cand)
        if user:
            return user
    return None


async def find_by_name_or_link(text: str) -> dict | None:
    """Админский ввод: username в панели или ссылка подписки."""
    text = text.strip()
    if "/" in text:
        return await find_by_sub_link(text)
    return await api.get_user_by_username(text.lstrip("@")) or await find_by_sub_link(text)


def tg_of(user: dict) -> int | None:
    """Telegram ID владельца аккаунта панели."""
    if user.get("telegramId"):
        return int(user["telegramId"])
    name = user.get("username") or ""
    if name.startswith(settings.username_prefix) and name[len(settings.username_prefix):].isdigit():
        return int(name[len(settings.username_prefix):])
    return None


def days_left(user: dict) -> float:
    exp = parse_dt(user.get("expireAt"))
    if not exp:
        return 0
    return (exp - datetime.now(timezone.utc)).total_seconds() / 86400


async def profile_text(user: dict, for_admin: bool = False) -> str:
    exp = parse_dt(user.get("expireAt"))
    left = days_left(user)
    if left > 1:
        left_txt = f"{int(left)} дн."
    elif left > 0:
        left_txt = f"{int(left * 24)} ч."
    else:
        left_txt = "0"

    traffic = user.get("userTraffic") or {}
    used = traffic.get("usedTrafficBytes", 0)
    limit = user.get("trafficLimitBytes") or 0
    traffic_txt = f"{human_bytes(used)} из {human_bytes(limit)}" if limit else f"{human_bytes(used)} из ∞"

    dev_limit = user.get("hwidDeviceLimit")
    try:
        devices = await api.get_devices(user["id"])
        dev_count = devices.get("total", len(devices.get("devices", [])))
    except Exception:
        dev_count = "?"
    dev_txt = f"{dev_count} из {dev_limit}" if dev_limit else f"{dev_count} (без лимита)"

    lines = [
        f"<b>👤 {esc(user.get('username'))}</b>",
        f"Статус: {STATUS_RU.get(user.get('status'), esc(user.get('status')))}",
        f"📅 Действует до: <b>{exp.strftime('%d.%m.%Y %H:%M') if exp else '—'}</b> UTC",
        f"⏳ Осталось: <b>{left_txt}</b>",
        f"📊 Трафик: <b>{traffic_txt}</b>",
        f"📱 Устройства: <b>{dev_txt}</b>",
    ]
    if for_admin:
        lines.append(f"🆔 id панели: <code>{user.get('id')}</code>, Telegram: <code>{tg_of(user) or '—'}</code>")
        squads = ", ".join(esc(s.get("name")) for s in user.get("activeInternalSquads") or []) or "—"
        lines.append(f"👥 Сквады: {squads}")
        tg = tg_of(user)
        bd = db.get_birthday(tg) if tg else None
        if bd and bd["day"]:
            lines.append(f"🎂 ДР: {fmt_birthday(bd['day'], bd['month'])}")
        total = traffic.get("lifetimeUsedTrafficBytes")
        if total is not None:
            lines.append(f"Σ За всё время: {human_bytes(total)}")
    lines.append(f"\n🔗 Ссылка подписки:\n<code>{esc(user.get('subscriptionUrl'))}</code>")
    return "\n".join(lines)


def qr_file(data: str) -> BufferedInputFile:
    """QR генерируется локально — ссылка подписки не уходит сторонним сервисам."""
    img = qrcode.make(data, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="subscription.png")
