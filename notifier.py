"""Фоновые задачи: напоминания об окончании подписки и автоочистка переписки."""
import asyncio
import calendar
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from config import settings
from core import api, db, esc, find_user, tg_of
from aiogram.types import InlineKeyboardButton as Btn

from keyboards import UserCb, UserMenu, kb, user_menu
from promo import loyalty_hint, paid_promo_line, processing_note
from remnawave import parse_dt

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
DAY_FROM, DAY_TO = 10, 21  # напоминания клиентам — только днём по МСК, ночью не беспокоим


def now_utc() -> datetime:  # отдельной функцией — чтобы в тестах подменять время
    return datetime.now(timezone.utc)


def _daytime() -> bool:
    return DAY_FROM <= now_utc().astimezone(MSK).hour < DAY_TO


def _stages() -> list[tuple[float, int, str]]:
    """(порог в часах до конца, номер этапа, вид) — от дальнего к ближнему.
    До окончания — по REMIND_HOURS (по умолчанию 72,24,6), затем «закончилась», затем одно догоняющее."""
    out = [(h, i, "before") for i, h in enumerate(settings.remind_hours, 1)]
    out.append((0, len(out) + 1, "expired"))
    if settings.remind_after_hours > 0:
        out.append((-settings.remind_after_hours, len(out) + 1, "followup"))
    return out


def _bday_hint(tg_id: int, exp: datetime) -> str:
    """ДР выпадает ПОСЛЕ окончания подписки (в пределах 2 недель) и подарок положен — намекнуть продлить.
    Правила те же, что у подарка: дата указана ≥30 дней назад или есть исключение, в этом году ещё не дарили."""
    if settings.bday_gift_days <= 0:
        return ""
    row = db.get_birthday(tg_id)
    if not row or not row["day"] or not row["month"]:
        return ""
    end = exp.astimezone(MSK).date()
    for year in (end.year, end.year + 1):
        try:
            bday = datetime(year, row["month"], row["day"]).date()
        except ValueError:  # 29.02 в невисокосный год
            bday = datetime(year, 2, 28).date()
        if bday > end:
            break
    if not end < bday <= end + timedelta(days=14) or (row["last_gift_year"] or 0) >= bday.year:
        return ""
    exception = db.kv_get(f"bday_exception:{tg_id}") == str(bday.year)
    set_ok = datetime.fromtimestamp(row["set_at"], timezone.utc).date() <= bday - timedelta(days=30)
    if not (exception or set_ok):
        return ""
    return (f"\n\n🎂 Кстати, {bday:%d.%m} у тебя день рождения — продли подписку, и в этот день "
            f"я подарю тебе <b>+{settings.bday_gift_days} дн.</b> 🎁")


def _reminder_text(kind: str, exp: datetime, hours: float, tg_id: int, panel_id: int | None = None) -> str:
    local = exp.astimezone(MSK)
    today = now_utc().astimezone(MSK).date()
    pay = "Оплатить и прислать чек можно прямо здесь — кнопка «💳 Оплатить / продлить»." + _bday_hint(tg_id, exp)
    if kind == "before":
        if local.date() == today:
            head = f"⏰ <b>Подписка заканчивается сегодня в {local:%H:%M}</b> (МСК)"
        elif local.date() == today + timedelta(days=1):
            head = f"⏳ <b>Подписка заканчивается завтра в {local:%H:%M}</b> (МСК)"
        else:
            head = f"⏳ <b>Подписка заканчивается через {(local.date() - today).days} дн.</b> — {local:%d.%m}"
        return (f"{head}\n{pay}\n\n💡 {processing_note()}" + paid_promo_line(tg_id)
                + loyalty_hint(panel_id))
    if kind == "expired":
        return (f"🔴 <b>Подписка закончилась</b> ({local:%d.%m %H:%M} МСК)\n"
                f"Оплатите и пришлите чек — после проверки доступ вернётся.\n{pay}\n\n💡 {processing_note()}"
                + paid_promo_line(tg_id) + loyalty_hint(panel_id, expired=True))
    days_ago = max(1, round(-hours / 24))
    return (f"👋 Подписка закончилась {days_ago} дн. назад.\n"
            f"Захотите вернуться — оплата и чек в одну кнопку, доступ включится после проверки."
            + paid_promo_line(tg_id))


async def _retire(bot: Bot, chat_id: int, msg_id: int | None, text: str | None = None) -> None:
    """Старое напоминание: открепить; удалить (Telegram даёт удалять только моложе 48 ч) или переписать."""
    if not msg_id:
        return
    try:
        await bot.unpin_chat_message(chat_id, message_id=msg_id)
    except Exception:
        pass
    if text:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
            return
        except Exception:
            pass
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        try:
            await bot.edit_message_text("⏳ Напоминание обновлено — актуальное закреплено вверху чата.",
                                        chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass


async def _alert_admin_unlinked(bot: Bot, user: dict, hours: float) -> None:
    """Клиент не привязал Telegram — бот не может ему написать, поэтому предупреждаем админа:
    один раз за сутки до конца и один раз после окончания."""
    if hours > 24 or hours < -72:
        return
    stage = 2 if hours <= 0 else 1
    key = -int(user["id"])  # отрицательный ключ — чтобы не пересекаться с Telegram ID в таблице напоминаний
    if stage <= db.reminder_stage(key, user["expireAt"]):
        return
    if stage == 1:
        left = f"{int(hours * 60)} мин" if hours < 1 else f"{int(hours)} ч"
        head = f"⏰ У <b>{esc(user['username'])}</b> подписка кончится через <b>{left}</b>"
    else:
        head = f"🔴 У <b>{esc(user['username'])}</b> подписка <b>закончилась</b>"
    text = (f"{head}.\nTelegram не привязан — сам бот ему написать не может, <b>напомни лично</b>.\n"
            f"<i>Пусть привяжет подписку в боте — тогда напоминания пойдут автоматически.</i>")
    markup = kb([Btn(text="+31 дн.", callback_data=UserCb(action="add", uid=user["id"], days=31).pack()),
                 Btn(text="+7 дн.", callback_data=UserCb(action="add", uid=user["id"], days=7).pack())],
                [Btn(text="👤 Карточка", callback_data=UserCb(action="refresh", uid=user["id"]).pack())])
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as e:
            log.info("Админ %s не уведомлён: %s", admin_id, e)
    db.set_reminder_stage(key, user["expireAt"], stage)


async def check_once(bot: Bot) -> None:
    """Напоминания об окончании: одно актуальное сообщение, закреплено вверху чата, автоочистка его не трогает.
    Новый этап — новое сообщение (с уведомлением), старое убирается. Продлили — сообщение становится «✅ продлено»."""
    now = now_utc()
    stages = _stages()
    top, last_limit = stages[0][0], stages[-1][0]
    daytime = _daytime()
    async for user in api.iter_users():
        tg_id = tg_of(user)
        exp = parse_dt(user.get("expireAt"))
        if not exp or user.get("status") == "DISABLED":
            continue
        hours = (exp - now).total_seconds() / 3600
        if not tg_id:
            await _alert_admin_unlinked(bot, user, hours)
            continue
        row = db.reminder_row(tg_id)
        # Продлили (срок сменился и он в будущем) — висящее напоминание превращаем в «✅ продлено» и открепляем
        if row and row["msg_id"] and row["expire_at"] != user["expireAt"] and hours > 0:
            await _retire(bot, tg_id, row["msg_id"],
                          f"✅ <b>Подписка продлена</b> до {exp.astimezone(MSK):%d.%m.%Y}. Спасибо!")
            db.set_reminder_stage(tg_id, user["expireAt"], 0, None)
            row = None
        if hours > top or hours < last_limit - 72:  # далеко до конца или догоняющее уже позади
            continue
        if not daytime:  # ночью не пишем: этап дождётся 10:00
            continue
        # Наиболее «поздний» пройденный этап (промежуточные пропускаем — без спама)
        stage, kind = next(((st, kd) for limit, st, kd in reversed(stages) if hours <= limit), (0, ""))
        if not stage or stage <= db.reminder_stage(tg_id, user["expireAt"]):
            continue
        try:
            m = await bot.send_message(tg_id, _reminder_text(kind, exp, hours, tg_id, user.get("id")), reply_markup=user_menu())
        except Exception as e:
            log.info("Напоминание %s не доставлено: %s", tg_id, e)
            db.set_reminder_stage(tg_id, user["expireAt"], stage, row["msg_id"] if row else None)
            continue
        db.untrack_msg(tg_id, m.message_id)  # не даём автоочистке удалить напоминание через сутки
        try:
            await bot.pin_chat_message(tg_id, m.message_id, disable_notification=True)
        except Exception:
            pass
        await _retire(bot, tg_id, row["msg_id"] if row else None)
        db.set_reminder_stage(tg_id, user["expireAt"], stage, m.message_id)
        await asyncio.sleep(0.05)  # бережём лимиты Telegram


async def _channel_line(bot: Bot) -> str:
    """«📣 Канал: 57 подписчиков, за сутки +2 / −1 (ушли: …)» — пусто, если за сутки никто не пришёл и не ушёл."""
    events = db.channel_events_since(int(now_utc().timestamp()) - 86400)
    if not events:
        return ""
    joins = [e for e in events if e["kind"] == "join"]
    leaves = [e for e in events if e["kind"] == "leave"]
    total = ""
    chat = settings.group_chat_id or db.kv_get("group_chat_id")
    if chat:
        try:
            total = f"{await bot.get_chat_member_count(int(chat))} подписчиков, "
        except Exception:
            pass
    line = f"📣 Канал: {total}за сутки <b>+{len(joins)}</b> / <b>−{len(leaves)}</b>"
    if leaves:
        line += "\nУшли: " + ", ".join(esc(e["name"] or str(e["tg_id"])) for e in leaves[:10])
    return line


async def admin_digest_once(bot: Bot) -> None:
    """Раз в день утром — админу: кто заканчивается в ближайшие 3 дня и кто закончился за неделю и не продлил."""
    local = now_utc().astimezone(MSK)
    if not 10 <= local.hour < 12 or db.kv_get("admin_digest_day") == local.strftime("%Y-%m-%d"):
        return
    db.kv_set("admin_digest_day", local.strftime("%Y-%m-%d"))
    soon, gone = [], []
    async for user in api.iter_users():
        exp = parse_dt(user.get("expireAt"))
        if not exp or user.get("status") == "DISABLED":
            continue
        hours = (exp - now_utc()).total_seconds() / 3600
        tg = "" if tg_of(user) else " · <i>без Telegram</i>"
        line = f"• <b>{esc(user['username'])}</b> — {exp.astimezone(MSK):%d.%m %H:%M}{tg}"
        if 0 < hours <= 72:
            soon.append((hours, line))
        elif -168 <= hours <= 0:
            gone.append((hours, line))
    chan = await _channel_line(bot)
    if not soon and not gone and not chan:
        return
    text = "☀️ <b>Сводка по подпискам</b>" + (f"\n\n{chan}" if chan else "")
    if soon:
        text += "\n\n⏳ Заканчиваются в ближайшие 3 дня:\n" + "\n".join(l for _, l in sorted(soon))
    if gone:
        text += "\n\n🔴 Закончились за неделю и не продлены:\n" + "\n".join(l for _, l in sorted(gone, reverse=True))
    text += "\n\n<i>Личное «привет, продлеваешь?» работает лучше любого напоминания.</i>"
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def delete_many(bot: Bot, chat_id: int, ids: list[int]) -> None:
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(chat_id, ids[i:i + 100])
        except Exception as e:
            log.debug("Не удалось удалить сообщения в %s: %s", chat_id, e)


async def autoclean_once(bot: Bot) -> None:
    # Переписку админов автоматически не трогаем — там чеки и отчёты
    if settings.chat_autoclean_hours > 0:
        old = db.take_msgs(older_than=settings.chat_autoclean_hours * 3600, exclude_chats=settings.admin_ids)
        for chat_id, ids in old.items():
            await delete_many(bot, chat_id, ids)
    db.take_msgs(older_than=48 * 3600)  # старше 48 ч Telegram удалить уже не даст — просто забываем


BDAY_MIN_AGE = 30 * 86400  # дату нужно указать минимум за 30 дней — иначе можно «назначить» ДР на сегодня


async def birthdays_once(bot: Bot) -> None:
    if settings.bday_gift_days <= 0:
        return
    today = datetime.now(MSK)
    if today.hour < 10:  # поздравляем днём, а не в полночь
        return
    rows = db.birthdays_on(today.day, today.month)
    if today.month == 2 and today.day == 28 and not calendar.isleap(today.year):
        rows += db.birthdays_on(29, 2)  # родившихся 29 февраля поздравляем 28-го
    now_utc = datetime.now(timezone.utc)
    for row in rows:
        tg_id = row["tg_id"]
        # Исключение, выданное админом на этот год (kv bday_exception:<tg> = год): дарим, даже если дату
        # указали меньше чем за 30 дней
        exception = db.kv_get(f"bday_exception:{tg_id}") == str(today.year)
        if (row["last_gift_year"] or 0) >= today.year or (time.time() - row["set_at"] < BDAY_MIN_AGE and not exception):
            continue
        user = await find_user(tg_id)
        if not user:
            continue
        exp = parse_dt(user.get("expireAt"))
        active = user.get("status") == "ACTIVE" and exp and exp > now_utc
        if not db.mark_birthday_gift(tg_id, today.year):  # атомарная отметка — второй раз не подарим
            continue
        if active:  # подарок только при активной подписке — исключение снимает лишь правило 30 дней
            try:
                updated = await api.add_days(user, settings.bday_gift_days)
            except Exception as e:
                log.warning("ДР-подарок %s не начислен: %s", tg_id, e)
                continue
            until = parse_dt(updated.get("expireAt"))
            text = (f"🎂 <b>С днём рождения!</b>\n\n"
                    f"Желаю счастья, здоровья и чтобы всё летало 🤖\n"
                    f"Дарю тебе <b>+{settings.bday_gift_days} дн.</b> к подписке 🎁"
                    + (f" — теперь она действует до <b>{until.astimezone(MSK):%d.%m.%Y}</b>." if until else "."))
            admin_note = f"🎂 Сегодня ДР у <b>{esc(user['username'])}</b> — подарено +{settings.bday_gift_days} дн."
            if exception:
                admin_note += ("\n⚠️ <b>По исключению</b>: дату указали меньше чем за 30 дней до ДР — "
                               "обычно подарок в этом случае не даётся, ты разрешил вручную.")
        else:
            text = "🎂 <b>С днём рождения!</b>\n\nЖелаю счастья, здоровья и всего самого лучшего 🤖"
            admin_note = (f"🎂 Сегодня ДР у <b>{esc(user['username'])}</b> — подписка неактивна, "
                          f"поздравили без подарка.")
        try:
            await bot.send_message(tg_id, text, reply_markup=user_menu())
        except Exception as e:
            log.info("Поздравление %s не доставлено: %s", tg_id, e)
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, admin_note)
            except Exception:
                pass


BDAY_NAG_EVERY = 30 * 86400
BDAY_NAG_MAX = 3


async def bday_nag_once(bot: Bot) -> None:
    """Кто не указал ДР (в т.ч. нажал «не скажу») — напоминаем раз в 30 дней, максимум 3 раза, днём."""
    if settings.bday_gift_days <= 0 or not 12 <= datetime.now(MSK).hour < 20:
        return
    now = int(time.time())
    for tg_id in db.linked_tg_ids():
        if tg_id in settings.admin_ids:
            continue
        row = db.get_birthday(tg_id)
        if row and row["day"]:
            continue
        count, last = (int(x) for x in (db.kv_get(f"bday_nag:{tg_id}") or "0,0").split(","))
        if count >= BDAY_NAG_MAX or now - last < BDAY_NAG_EVERY:
            continue
        db.kv_set(f"bday_nag:{tg_id}", f"{count + 1},{now}")
        try:
            await bot.send_message(
                tg_id, f"🎂 Ты ещё не указал день рождения — а в этот день я дарю "
                       f"<b>+{settings.bday_gift_days} дн.</b> подписки 🎁",
                reply_markup=kb([Btn(text="🎂 Указать дату", callback_data=UserMenu(action="bday_ask").pack())],
                                [Btn(text="🙅 Не напоминать", callback_data=UserMenu(action="bday_never").pack())]))
        except Exception as e:
            log.info("Напоминание про ДР %s не доставлено: %s", tg_id, e)
        await asyncio.sleep(0.05)


async def jobs_watch_once(bot: Bot) -> None:
    """Ночные задания: если к сроку + 1 ч задание не отчиталось (kv job_done:<имя>) — одна тревога админу.
    Срок ставит тот, кто планирует задание: kv job_due:<имя> = unix-время."""
    now = time.time()
    for key, due in db.conn.execute("SELECT key, value FROM kv WHERE key LIKE 'job_due:%'").fetchall():
        name, due = key.split(":", 1)[1], int(due)
        done = int(db.kv_get(f"job_done:{name}") or 0)
        if now < due + 3600 or done >= due or db.kv_get(f"job_alerted:{name}") == str(due):
            continue
        db.kv_set(f"job_alerted:{name}", str(due))
        when = datetime.fromtimestamp(due, MSK).strftime("%d.%m %H:%M МСК")
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, f"⚠️ <b>Ночное задание «{esc(name)}» не выполнилось</b>\n"
                                                 f"Должно было запуститься {when}, но так и не отчиталось. "
                                                 f"Проверь сервер бота: <code>systemctl status {esc(name)}.timer</code>")
            except Exception:
                pass


async def channel_autodelete_once(bot: Bot) -> None:
    from handlers.group import autodelete_once  # handlers.group импортирует много — подгружаем при вызове
    await autodelete_once(bot)


async def background_loop(bot: Bot, interval: int = 600) -> None:
    while True:
        for job in (check_once, admin_digest_once, autoclean_once, birthdays_once, bday_nag_once,
                    channel_autodelete_once, jobs_watch_once):
            try:
                await job(bot)
            except Exception:
                log.exception("Ошибка фоновой задачи %s", job.__name__)
        await asyncio.sleep(interval)
