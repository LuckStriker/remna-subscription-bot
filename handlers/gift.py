"""Подарок всем: одной кнопкой начислить дни всем клиентам (акции, праздники) и поздравить их."""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import Message

from config import settings
from core import api, esc, tg_of
from keyboards import AdminMenu, GiftCb, back_to_admin, kb, user_menu
from remnawave import RemnawaveError

router = Router()
router.message.filter(F.from_user.id.in_(settings.admin_ids), F.chat.type == "private")
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))
log = logging.getLogger(__name__)

_lock = asyncio.Lock()
CANCEL = Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())


class GiftStates(StatesGroup):
    days = State()
    reason = State()
    confirm = State()


@router.callback_query(AdminMenu.filter(F.action == "gift"))
async def gift_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(GiftStates.days)
    await cb.message.answer(
        "🎁 <b>Подарок всем</b>\nСколько дней подарить? Выберите или напишите число:",
        reply_markup=kb([Btn(text=f"+{d}", callback_data=GiftCb(action="days", days=d).pack()) for d in (3, 7, 14, 31)],
                        [CANCEL]))


async def _ask_reason(target: Message, state: FSMContext, days: int):
    await state.update_data(days=days)
    await state.set_state(GiftStates.reason)
    await target.answer(f"+{days} дн. Какой повод? Напишите фразу для поздравления — например "
                        "<i>«С Новым годом! 🎄»</i>.",
                        reply_markup=kb([Btn(text="Без повода", callback_data=GiftCb(action="skip").pack())], [CANCEL]))


@router.callback_query(GiftStates.days, GiftCb.filter(F.action == "days"))
async def gift_days_btn(cb: CallbackQuery, callback_data: GiftCb, state: FSMContext):
    await cb.answer()
    await _ask_reason(cb.message, state, callback_data.days)


@router.message(GiftStates.days)
async def gift_days_text(msg: Message, state: FSMContext):
    try:
        days = int((msg.text or "").strip().lstrip("+"))
    except ValueError:
        days = 0
    if not 1 <= days <= 365:
        return await msg.answer("Нужно число от 1 до 365.", reply_markup=kb([CANCEL]))
    await _ask_reason(msg, state, days)


async def _count() -> tuple[int, int, int]:
    active = expired = linked = 0
    async for u in api.iter_users():
        st = u.get("status")
        if st == "DISABLED":
            continue
        if st == "EXPIRED":
            expired += 1
        else:
            active += 1
        if tg_of(u):
            linked += 1
    return active, expired, linked


async def _ask_scope(target: Message, state: FSMContext, reason: str):
    await state.update_data(reason=reason)
    await state.set_state(GiftStates.confirm)
    days = (await state.get_data())["days"]
    try:
        active, expired, linked = await _count()
    except RemnawaveError as e:
        await state.clear()
        return await target.answer(f"💥 {esc(e)}", reply_markup=back_to_admin())

    def run(text, scope, notify):
        return Btn(text=text, callback_data=GiftCb(action="run", days=days, scope=scope, notify=notify).pack())

    rows = []
    for scope, title in (("active", f"активным ({active})"), ("all", f"всем, вкл. истёкших ({active + expired})")):
        if scope == "all" and not expired:
            continue
        if settings.group_chat_id:
            rows.append([run(f"🎁 {title} + личка + пост в группе", scope, 2)])
        rows.append([run(f"🎁 {title} + поздравить в личке", scope, 1)])
        rows.append([run(f"🤫 {title} — тихо", scope, 0)])
    rows.append([CANCEL])
    await target.answer(
        f"🎁 <b>+{days} дн.</b>{(' — ' + esc(reason)) if reason else ''}\n\n"
        f"Активных подписок: <b>{active}</b>\nИстёкших: <b>{expired}</b> (им дни пойдут от сегодня)\n"
        f"Отключённые вручную не трогаем.\nПоздравление в личке получат привязавшие Telegram: <b>{linked}</b>.\n\n"
        "Кому начисляем?", reply_markup=kb(*rows))


@router.callback_query(GiftStates.reason, GiftCb.filter(F.action == "skip"))
async def gift_no_reason(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_scope(cb.message, state, "")


@router.message(GiftStates.reason, F.text)
async def gift_reason(msg: Message, state: FSMContext):
    await _ask_scope(msg, state, msg.html_text[:300])


def gift_text(days: int, reason: str, expire: str) -> str:
    head = f"🎁 <b>{reason}</b>\n\n" if reason else "🎁 <b>Подарок!</b>\n\n"
    return (head + f"Мы добавили к вашей подписке <b>+{days} дн.</b> — просто так, от души 🎁\n"
            f"Теперь она действует до <b>{expire}</b>.\n\nСпасибо, что вы с нами!")


@router.callback_query(GiftStates.confirm, GiftCb.filter(F.action == "run"))
async def gift_run(cb: CallbackQuery, callback_data: GiftCb, state: FSMContext, bot: Bot):
    if _lock.locked():
        return await cb.answer("Начисление уже идёт", show_alert=True)
    reason = (await state.get_data()).get("reason", "")
    await state.clear()
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    days, notify = callback_data.days, callback_data.notify
    progress = await cb.message.answer(f"⏳ Начисляю +{days} дн…")

    ok = fail = sent = 0
    async with _lock:
        users = [u async for u in api.iter_users()]
        for u in users:
            st = u.get("status")
            if st == "DISABLED" or (callback_data.scope == "active" and st == "EXPIRED"):
                continue
            try:
                updated = await api.add_days(u, days)
                ok += 1
            except RemnawaveError as e:
                fail += 1
                log.warning("Подарок %s не начислен: %s", u.get("username"), e)
                continue
            tg = tg_of(updated)
            if notify and tg:
                try:
                    await bot.send_message(tg, gift_text(days, reason, (updated.get("expireAt") or "")[:10]),
                                           reply_markup=user_menu())
                    sent += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)

    report = [f"✅ Начислено +{days} дн.: <b>{ok}</b>"]
    if fail:
        report.append(f"⚠️ Ошибок: {fail} (подробности в журнале)")
    if notify:
        report.append(f"📨 Поздравлений в личку: {sent}")
    if notify == 2 and settings.group_chat_id:
        post = (f"🎁 <b>{reason or 'Подарок для всех!'}</b>\n\n"
                f"Всем подписчикам начислено <b>+{days} дн.</b> к подписке — уже сейчас, ничего делать не нужно 🎁\n"
                "Проверить новый срок можно в боте.")
        me = await bot.me()
        try:
            await bot.send_message(settings.group_chat_id, post, reply_markup=kb(
                [Btn(text="📊 Проверить подписку", url=f"https://t.me/{me.username}?start=gift")]))
            report.append("📢 Пост в группе опубликован")
        except Exception as e:
            report.append(f"💥 Пост в группу не отправлен: {esc(e)}")
    await progress.edit_text("\n".join(report))
    await cb.message.answer("Готово 🎉", reply_markup=back_to_admin())
