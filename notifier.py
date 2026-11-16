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
from promo import processing_note
from remnawave import parse_dt

log = logging.getLogger(__name__)


def _stages() -> list[tuple[float, int, str]]:
    """(порог в часах, номер этапа, текст) — от дальнего к ближнему; последний этап — «истекла»."""
    out = []
    for i, h in enumerate(settings.remind_hours, 1):
        when = f"{h // 24} дн." if h % 24 == 0 and h >= 48 else ("сутки" if h == 24 else f"{h} ч.")
        out.append((h, i, f"⏳ Подписка заканчивается через {when}\n"
                          f"Чтобы доступ не отключился, оплатите и отправьте чек кнопкой «💳 Оплатить / продлить» "
                          f"(там же реквизиты).\n\n💡 {processing_note()}"))
    out.append((0, len(out) + 1, "🔴 Подписка закончилась. Отправьте чек об оплате — после проверки доступ вернётся.\n\n"
                                 f"💡 {processing_note()}"))
    return out


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
    now = datetime.now(timezone.utc)
    stages = _stages()
    top = stages[0][0]
    async for user in api.iter_users():
        tg_id = tg_of(user)
        exp = parse_dt(user.get("expireAt"))
        if not exp or user.get("status") == "DISABLED":
            continue
        hours = (exp - now).total_seconds() / 3600
        if not tg_id:
            await _alert_admin_unlinked(bot, user, hours)
            continue
        if hours > top or hours < -72:  # далеко до конца или давно истекла
            continue
        # Наиболее «поздний» этап, порог которого уже пройден (промежуточные пропускаем — без спама)
        stage, text = next(((st, tx) for limit, st, tx in reversed(stages) if hours <= limit), (0, ""))
        if not stage or stage <= db.reminder_stage(tg_id, user["expireAt"]):
            continue
        old = db.reminder_msg(tg_id)
        if old:  # предыдущее напоминание убираем — в чате всегда только одно
            try:
                await bot.delete_message(tg_id, old)
            except Exception:
                pass
        msg_id = None
        try:
            m = await bot.send_message(tg_id, text, reply_markup=user_menu())
            msg_id = m.message_id
        except Exception as e:
            log.info("Напоминание %s не доставлено: %s", tg_id, e)
        db.set_reminder_stage(tg_id, user["expireAt"], stage, msg_id)
        await asyncio.sleep(0.05)  # бережём лимиты Telegram


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


MSK = timezone(timedelta(hours=3))
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
            text = (f"🎂 <b>С днём рождения!</b>\n\n"
                    f"От всей нашей команды — счастья, здоровья и всего самого лучшего 🎉\n"
                    f"В подарок к подписке добавлено <b>+{settings.bday_gift_days} дн.</b> 🎁\n"
                    f"Теперь она действует до <b>{(updated.get('expireAt') or '')[:10]}</b>.")
            admin_note = f"🎂 Сегодня ДР у <b>{esc(user['username'])}</b> — подарено +{settings.bday_gift_days} дн."
            if exception:
                admin_note += ("\n⚠️ <b>По исключению</b>: дату указали меньше чем за 30 дней до ДР — "
                               "обычно подарок в этом случае не даётся, ты разрешил вручную.")
        else:
            text = "🎂 <b>С днём рождения!</b>\n\nОт всей нашей команды — счастья, здоровья и всего самого лучшего 🎉"
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
        for job in (check_once, autoclean_once, birthdays_once, bday_nag_once, channel_autodelete_once,
                    jobs_watch_once):
            try:
                await job(bot)
            except Exception:
                log.exception("Ошибка фоновой задачи %s", job.__name__)
        await asyncio.sleep(interval)
