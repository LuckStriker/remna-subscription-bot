"""Акции и реквизиты оплаты.

• «первые N новых подписчиков, чей чек одобрен» → +M дней (PAID_PROMO_*). Старые клиенты не участвуют.
• «первые N, привязавших подписку» (FIRST_PROMO_*) — старая акция, выключается FIRST_PROMO_LIMIT=0.
"""
import logging

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
    if settings.paid_promo_limit <= 0 or settings.paid_promo_days <= 0:
        return 0
    return max(0, settings.paid_promo_limit - db.paid_promo_count())


def paid_promo_line() -> str:
    left = paid_slots_left()
    if not left:
        return ""
    return (f"\n\n🏆 Первые <b>{settings.paid_promo_limit}</b> новых подписчиков, оплативших подписку, "
            f"получают <b>+{settings.paid_promo_days} дн.</b> в подарок. Осталось мест: <b>{left}</b>")


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
    """После одобренного чека: новый подписчик (заведён ботом по заявке) из первых N получает +M дней."""
    if (paid_slots_left() <= 0 or paid_days < settings.ref_min_paid_days
            or tg_id in settings.admin_ids or not db.is_new_sub(tg_id)):
        return
    place = db.claim_paid_promo(tg_id, settings.paid_promo_limit)
    if not place:
        return
    user = await find_user(tg_id)
    try:
        if not user:
            raise RemnawaveError(404, "аккаунт не найден")
        updated = await api.add_days(user, settings.paid_promo_days)
    except RemnawaveError as e:
        db.unclaim_paid_promo(tg_id)
        await target.answer(f"⚠️ Бонус «первые {settings.paid_promo_limit}» не начислен: {esc(e)}")
        return
    n, limit, days = place, settings.paid_promo_limit, settings.paid_promo_days
    try:
        await bot.send_message(tg_id, f"🏆 <b>Ты в числе первых {limit}!</b>\n"
                                      f"Спасибо за оплату — держи <b>+{days} дн.</b> в подарок 🎁\n"
                                      f"Подписка действует до <b>{esc((updated.get('expireAt') or '')[:10])}</b>.",
                               reply_markup=user_menu())
    except Exception:
        pass
    await target.answer(f"🏆 Акция «первые {limit} оплативших»: <b>{n}/{limit}</b> — +{days} дн. для "
                        f"<b>{esc(updated['username'])}</b>"
                        + ("\n🏁 Все места разобраны, акция завершена." if n >= limit else ""))


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
