"""Акции и реквизиты оплаты.

• «бонус за первую оплату через бота» → +M дней за ПЕРВЫЙ одобренный чек (на месяц и больше) — любому клиенту:
  новому, нынешнему, бывшему. Цель — приучить платить через бота (PAID_PROMO_*; LIMIT 0 — без лимита).
• «первые N, привязавших подписку» (FIRST_PROMO_*) — старая акция, выключается FIRST_PROMO_LIMIT=0.
"""
import asyncio
import json
import logging
from datetime import datetime

from aiogram import Bot

from config import settings
from core import api, db, esc, find_user, price_for, price_switched
from keyboards import user_menu
from remnawave import RemnawaveError

log = logging.getLogger(__name__)


def promo_slots_left() -> int:
    if settings.first_promo_limit <= 0 or settings.first_promo_days <= 0:
        return 0
    return max(0, settings.first_promo_limit - db.first_promo_count())


def paid_slots_left() -> int:
    if settings.paid_promo_days <= 0:
        return 0
    if settings.paid_promo_limit <= 0:  # без лимита
        return 10 ** 9
    return max(0, settings.paid_promo_limit - db.paid_promo_count())


def paid_promo_eligible(tg_id: int | None) -> bool:
    """Ещё не платил через бота и бонус не получал (tg_id=None — общий текст, без проверки человека)."""
    if paid_slots_left() <= 0:
        return False
    if tg_id is None:
        return True
    return db.approved_payments_count(tg_id) == 0 and not db.has_paid_promo(tg_id)


def paid_promo_line(tg_id: int | None = None) -> str:
    if not paid_promo_eligible(tg_id):
        return ""
    line = (f"\n\n🎁 За <b>первую оплату через бота</b> — <b>+{settings.paid_promo_days} дн.</b> в подарок "
            f"(при оплате от месяца).")
    if settings.paid_promo_limit > 0:
        line += f" Осталось мест: <b>{paid_slots_left()}</b>"
    return line


_DIGITS = "ноль один два три четыре пять шесть семь восемь девять".split()


def phone_in_words(phone: str) -> str:
    """+70001234567 → «плюс семь, ноль ноль ноль, один два три, четыре пять, шесть семь»."""
    d = "".join(ch for ch in phone if ch.isdigit())
    groups = [d[:1], d[1:4], d[4:7], d[7:9], d[9:]] if len(d) == 11 else [d]
    words = ", ".join(" ".join(_DIGITS[int(c)] for c in g) for g in groups if g)
    return ("плюс " if phone.strip().startswith("+") else "") + words


def phone_pretty(phone: str) -> str:
    d = "".join(ch for ch in phone if ch.isdigit())
    if len(d) != 11:
        return phone
    return f"+{d[0]} {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}"


def processing_note() -> str:
    """Короткая строка про сроки — для ответа на чек и напоминаний."""
    return (f"Перевод в <b>{esc(settings.pay_bank)}</b> — предпочтительно, проверка до 4 ч; "
            f"в другой банк — до 24 ч.")


def price_line(tg_id: int | None) -> str:
    """Сумма для конкретного клиента + предупреждение об индексации, если она его коснётся."""
    p = price_for(tg_id)
    line = f"Сумма: <b>{p} ₽</b> за 31 день\n"
    locked = bool(tg_id and db.kv_get(f"price_lock:{tg_id}"))
    d = settings.price_new_from
    when = f"{d[8:10]}.{d[5:7]}.{d[:4]}"
    if price_switched():
        if locked:
            line += f"🔒 Для тебя сохранена прежняя цена — {settings.price_old} ₽.\n"
    elif not settings.price_raise:
        pass
    elif locked:
        line += f"🔒 С {when} цена для новых — {settings.price_new} ₽, у тебя остаётся {settings.price_old} ₽.\n"
    else:
        line += f"ℹ️ С {when} — {settings.price_new} ₽ за 31 день.\n"
    return line


def payment_details(tg_id: int | None = None) -> str:
    """Реквизиты для оплаты — строго СБП в один банк. Сумма — своя для каждого клиента."""
    if not settings.pay_phone:
        return esc(settings.support_text)
    price = price_line(tg_id)
    name = f"Получатель: <b>{esc(settings.pay_name)}</b>\n" if settings.pay_name else ""
    note = f"\n🚫 {esc(settings.pay_note)}" if settings.pay_note else ""
    return (f"<b>Оплата только переводом по СБП</b>\n"
            f"Телефон: <code>{esc(phone_pretty(settings.pay_phone))}</code>\n"
            f"Прописью: <i>{esc(phone_in_words(settings.pay_phone))}</i>\n{name}{price}"
            f"🏦 Банк получателя: <b>{esc(settings.pay_bank)}</b> — желательно, обработка до 4 часов.\n"
            f"⏳ Перевод в другой банк тоже примем, но обработка займёт до 24 часов.{note}")


async def reward_first_paid(bot: Bot, target, tg_id: int, paid_days: int) -> None:
    """После одобренного чека: если это ПЕРВЫЙ одобренный чек человека (на месяц и больше) — +M дней.
    Любому клиенту: новому, нынешнему, бывшему. Вызывается после того, как чек уже отмечен одобренным."""
    if (paid_slots_left() <= 0 or paid_days < settings.ref_min_paid_days or tg_id in settings.admin_ids
            or db.approved_payments_count(tg_id) != 1):
        return
    user = await find_user(tg_id)
    if not user:
        await target.answer("⚠️ Бонус за первую оплату через бота не начислен: аккаунт не найден")
        return
    # Защита от повтора: бонус — один раз НА ПОДПИСКУ, а не на Telegram (второй Telegram к той же подписке не поможет)
    panel_key = f"paid_promo_panel:{int(user['id'])}"
    if db.kv_get(panel_key):
        return
    place = db.claim_paid_promo(tg_id, settings.paid_promo_limit)
    if not place:
        return
    db.kv_set(panel_key, str(tg_id))
    try:
        updated = await api.add_days(user, settings.paid_promo_days)
    except RemnawaveError as e:
        db.unclaim_paid_promo(tg_id)
        db.kv_set(panel_key, "")
        await target.answer(f"⚠️ Бонус за первую оплату через бота не начислен: {esc(e)}")
        return
    days, limit = settings.paid_promo_days, settings.paid_promo_limit
    try:
        await bot.send_message(tg_id, f"🎁 <b>Бонус за первую оплату через бота!</b>\n"
                                      f"Спасибо, что оплачиваешь здесь — держи <b>+{days} дн.</b> в подарок.\n"
                                      f"Подписка действует до <b>{esc((updated.get('expireAt') or '')[:10])}</b>.",
                               reply_markup=user_menu())
    except Exception:
        pass
    await target.answer(f"🎁 Бонус за первую оплату через бота: +{days} дн. для <b>{esc(updated['username'])}</b>"
                        + (f" ({place}/{limit})" if limit > 0 else ""))


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def loyalty_step(state: dict, months: int, on_time: bool) -> tuple[dict, int, int]:
    """Чистая логика серии (без побочных эффектов). state = {"m": месяцев подряд, "g": выдано ступеней}.
    Возвращает (новое состояние, бонус в днях, месяцев до следующей ступени; 0 — цикл закрыт)."""
    tiers = settings.loyal_tiers
    m, g = (state.get("m", 0), state.get("g", 0)) if on_time else (0, 0)
    m += months
    bonus = 0
    while g < len(tiers) and m >= tiers[g][0]:
        bonus += tiers[g][1]
        g += 1
    if g >= len(tiers):          # последняя ступень взята — цикл начинается заново
        m, g = 0, 0
        return {"m": m, "g": g}, bonus, 0
    return {"m": m, "g": g}, bonus, tiers[g][0] - m


def loyalty_hint(panel_id: int | None, expired: bool = False) -> str:
    """Строка для напоминания: сколько будет месяцев подряд, если оплатить вовремя (месячный чек)."""
    if not settings.loyal_tiers or panel_id is None:
        return ""
    try:
        state = json.loads(db.kv_get(f"loyal:{int(panel_id)}") or "{}")
    except ValueError:
        state = {}
    new, bonus, left = loyalty_step(state, 1, True)
    n = state.get("m", 0) + 1
    when = (f"в течение {settings.loyal_grace_hours} ч" if expired else "до окончания")
    if bonus:
        return f"\n\n🔥 Оплатишь {when} — это {n}-й месяц подряд и <b>+{bonus} дн.</b> в подарок."
    if n == 1:
        m1, d1 = settings.loyal_tiers[0]
        return f"\n\n🔥 Платишь вовремя {m1} мес. подряд — <b>+{d1} дн.</b> в подарок."
    return f"\n\n🔥 Оплатишь {when} — будет {n}-й месяц подряд, серия сохранится."


_big_lock = asyncio.Lock()


def big_promo_given() -> int:
    return db.conn.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'big_promo:%' AND value<>''").fetchone()[0]


async def reward_big_payment(updated: dict, tg_id: int, paid_days: int) -> tuple[dict, str, str]:
    """Акция «первые N, оплатившие надолго»: чек от BIG_PROMO_MIN_DAYS дней → +BIG_PROMO_DAYS сверху.
    Один раз на подписку (id в панели), админы не участвуют. Возвращает (подписка, строка клиенту, строка админу)."""
    limit = settings.big_promo_limit
    if limit <= 0 or settings.big_promo_days <= 0 or paid_days < settings.big_promo_min_days \
            or tg_id in settings.admin_ids:
        return updated, "", ""
    async with _big_lock:
        key = f"big_promo:{int(updated['id'])}"
        if db.kv_get(key):
            return updated, "", ""
        place = big_promo_given() + 1
        if place > limit:
            return updated, "", ""
        db.kv_set(key, str(tg_id))
        try:
            updated = await api.add_days(updated, settings.big_promo_days)
        except RemnawaveError as e:
            db.kv_set(key, "")
            return updated, "", f"⚠️ Подарок «первые {limit}» (+{settings.big_promo_days} дн.) не начислен: {esc(e)}"
    until = esc((updated.get("expireAt") or "")[:10])
    admin = (f"🏆 Акция «первые {limit}, оплатившие надолго»: <b>{place}/{limit}</b> — "
             f"+{settings.big_promo_days} дн. для <b>{esc(updated['username'])}</b>"
             + ("\n🏁 Все места разобраны, акция завершена." if place >= limit else ""))
    return (updated,
            f"🏆 <b>Ты в числе первых {limit}, кто оплатил надолго!</b> Держи <b>+{settings.big_promo_days} дн.</b> "
            f"в подарок — подписка действует до <b>{until}</b>.",
            admin)


_loyal_lock = asyncio.Lock()  # два чека одной подписки, одобренные одновременно, не дадут бонус дважды


async def reward_loyalty(user_before: dict, updated: dict, tg_id: int, paid_days: int,
                         submitted_at: int) -> tuple[dict, str, str]:
    async with _loyal_lock:
        return await _reward_loyalty(user_before, updated, tg_id, paid_days, submitted_at)


async def _reward_loyalty(user_before: dict, updated: dict, tg_id: int, paid_days: int,
                          submitted_at: int) -> tuple[dict, str, str]:
    """Бонус за оплаты подряд. Вызывается сразу после продления по чеку.
    «Вовремя» — чек отправлен, пока подписка ещё действовала (+ LOYAL_GRACE_HOURS).
    Серия хранится НА ПОДПИСКУ (id в панели), не на Telegram.
    Возвращает (подписка после бонуса, строка клиенту, строка админу)."""
    months = paid_days // max(1, settings.ref_min_paid_days)
    if not settings.loyal_tiers or months <= 0 or tg_id in settings.admin_ids:
        return updated, "", ""
    on_time = submitted_at <= _ts(user_before.get("expireAt")) + settings.loyal_grace_hours * 3600
    key = f"loyal:{int(updated['id'])}"
    try:
        old = json.loads(db.kv_get(key) or "{}")
    except ValueError:
        old = {}
    new, bonus, left = loyalty_step(old, months, on_time)
    db.kv_set(key, json.dumps(new))
    if bonus:
        try:
            updated = await api.add_days(updated, bonus)
        except RemnawaveError as e:
            db.kv_set(key, json.dumps(old))
            return updated, "", f"⚠️ Бонус за оплаты подряд (+{bonus} дн.) не начислен: {esc(e)}"
        until = esc((updated.get("expireAt") or "")[:10])
        return (updated,
                f"🔥 <b>Спасибо за верность!</b> Оплаты подряд — держи <b>+{bonus} дн.</b> в подарок. "
                f"Подписка действует до <b>{until}</b>.",
                f"🔥 Бонус за оплаты подряд: +{bonus} дн. для <b>{esc(updated['username'])}</b>")
    nxt = next(d for mm, d in settings.loyal_tiers if mm > new["m"])
    word = "месяц" if left == 1 else ("месяца" if left < 5 else "месяцев")
    return (updated,
            f"🔥 Оплат подряд: <b>{new['m']} мес.</b> Ещё {left} {word} без перерыва — и <b>+{nxt} дн.</b> в подарок.",
            "")


async def reward_first_verified(bot: Bot, tg_id: int, user: dict) -> bool:
    """Вызывается после успешной верификации существующего клиента. True — бонус начислен."""
    if promo_slots_left() <= 0 or tg_id in settings.admin_ids or user.get("status") == "DISABLED":
        return False
    place = db.claim_first_promo(tg_id, user["id"], settings.first_promo_limit)
    if not place:
        return False
    try:
        updated = await api.add_days(user, settings.first_promo_days)
    except RemnawaveError as e:
        db.unclaim_first_promo(tg_id)  # место возвращаем — бонус не дошёл
        log.warning("Бонус первых %s для %s не начислен: %s", settings.first_promo_limit, tg_id, e)
        return False
    n, limit, days = place, settings.first_promo_limit, settings.first_promo_days
    try:
        await bot.send_message(tg_id, f"🏆 <b>Ты в числе первых {limit}!</b>\n"
                                      f"Спасибо, что привязал подписку — держи <b>+{days} дн.</b> в подарок 🎁\n"
                                      f"Подписка действует до <b>{esc((updated.get('expireAt') or '')[:10])}</b>.",
                               reply_markup=user_menu())
    except Exception:
        pass
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, f"🏆 Акция «первые {limit}»: <b>{n}/{limit}</b> — "
                                             f"+{days} дн. для <b>{esc(updated['username'])}</b>"
                                             + ("\n🏁 Все места разобраны, акция завершена." if n >= limit else ""))
        except Exception:
            pass
    return True
