"""Админка: пользователи, дни, чеки, заявки, ноды."""
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import Message

from config import settings
from core import (api, db, esc, lock_price, price_for, find_by_name_or_link, find_user, fmt_birthday, link_user, parse_birthday,
                  profile_text, qr_file, resolve_target,
                  tg_of, unlinked_users, username_for, valid_username)
from keyboards import (AdminMenu, LinkCb, NodeCb, PayCb, ReqCb, SquadCb, UserCb, UserMenu, admin_menu,
                       back_to_admin, cancel_kb, kb, payment_kb, user_card_kb, user_menu)
from remnawave import RemnawaveError, human_bytes
from promo import paid_promo_line, payment_details, reward_first_paid, reward_first_verified
from tracking import clean_chat

router = Router()
log = logging.getLogger(__name__)

# Все хендлеры этого роутера — только для админов
router.message.filter(F.from_user.id.in_(settings.admin_ids), F.chat.type == "private")
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))


class CreateUser(StatesGroup):
    tg_id = State()
    days = State()
    traffic = State()
    devices = State()
    squads = State()


class AddDays(StatesGroup):
    target = State()
    days = State()


class FindUser(StatesGroup):
    query = State()


class LinkSelf(StatesGroup):
    wait = State()


class CustomDays(StatesGroup):
    payment = State()  # своё кол-во дней по чеку
    user = State()     # ± дни из карточки пользователя
    amount = State()   # сумма из чека → дни по тарифу клиента


async def notify(bot: Bot, tg_id: int | None, text: str, **kw) -> None:
    if not tg_id:
        return
    try:
        await bot.send_message(tg_id, text, **kw)
    except Exception as e:
        log.info("Не удалось уведомить %s: %s", tg_id, e)


def parse_int(text: str | None) -> int | None:
    try:
        return int((text or "").strip().replace("+", ""))
    except ValueError:
        return None


async def show_card(target: Message, user: dict) -> None:
    await target.answer(await profile_text(user, for_admin=True),
                        reply_markup=user_card_kb(user["id"], user.get("status", "")))


# ======================= меню =======================
@router.callback_query(AdminMenu.filter(F.action.in_({"menu", "cancel"})))
async def cb_admin_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await cb.message.answer("<b>🛡 Админ-панель</b>", reply_markup=admin_menu())


@router.callback_query(AdminMenu.filter(F.action == "clean"))
async def cb_admin_clean(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await cb.answer("Чистим…")
    n = await clean_chat(bot, cb.from_user.id)
    pending = len(db.pending_payments())
    note = f"\n🧾 Чеков на проверке: {pending} — они доступны в «🧾 Чеки»." if pending else ""
    await bot.send_message(cb.from_user.id, f"🧹 Удалено сообщений: {n}.{note}", reply_markup=admin_menu())


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=admin_menu())


@router.callback_query(AdminMenu.filter(F.action == "me"))
async def cb_me(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    user = await find_user(cb.from_user.id)
    if not user:
        await state.set_state(LinkSelf.wait)
        return await cb.message.answer(
            "🔗 Ваш Telegram пока не привязан к аккаунту в панели.\n\n"
            "Пришлите мне ваш <b>username из панели</b>.\n\n"
            "Не помните username? Просто пришлите мне <b>свою ссылку подписки</b> — "
            "я сам найду аккаунт и привяжу.", reply_markup=cancel_kb())
    await cb.message.answer(await profile_text(user), reply_markup=user_menu())


@router.message(LinkSelf.wait, F.text)
async def link_self(msg: Message, state: FSMContext):
    user = await find_by_name_or_link(msg.text)
    if not user:
        return await msg.answer("Не нашёл такого пользователя в панели. Проверьте username или ссылку:",
                                reply_markup=cancel_kb())
    owner = user.get("telegramId")
    if owner and owner != msg.from_user.id:
        return await msg.answer(f"⚠️ <b>{esc(user['username'])}</b> уже привязан к Telegram <code>{owner}</code>. "
                                "Если это ошибка — перепривяжите: <code>/link ВАШ_ID username</code>.",
                                reply_markup=cancel_kb())
    await state.clear()
    try:
        updated = await link_user(msg.from_user.id, user)
    except RemnawaveError as e:
        return await msg.answer(f"💥 {esc(e)}", reply_markup=back_to_admin())
    await msg.answer(f"✅ Аккаунт <b>{esc(updated['username'])}</b> привязан к вам. Так его видит клиент:")
    await msg.answer(await profile_text(updated), reply_markup=user_menu())


# ======================= создание пользователя =======================
@router.callback_query(AdminMenu.filter(F.action == "create"))
async def create_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(CreateUser.tg_id)
    await cb.message.answer("➕ Введите <b>Telegram ID</b> пользователя (число).\n"
                            "<i>Его можно увидеть в заявке на доступ или через @userinfobot.</i>",
                            reply_markup=cancel_kb())


@router.message(Command("create"))
async def cmd_create(msg: Message, state: FSMContext):
    """Быстрый вариант: /create <tg_id> [дни]"""
    args = (msg.text or "").split()
    if len(args) >= 2 and args[1].isdigit():
        days = parse_int(args[2]) if len(args) > 2 else settings.default_days
        await state.update_data(tg_id=int(args[1]), days=days or settings.default_days,
                                traffic=settings.default_traffic_gb, devices=settings.default_devices)
        return await ask_squads(msg, state)
    await state.set_state(CreateUser.tg_id)
    await msg.answer("➕ Введите Telegram ID пользователя:", reply_markup=cancel_kb())


@router.message(CreateUser.tg_id)
async def create_tg(msg: Message, state: FSMContext):
    tg_id = parse_int(msg.text)
    if not tg_id or tg_id <= 0:
        return await msg.answer("Нужно число — Telegram ID.", reply_markup=cancel_kb())
    if await find_user(tg_id):
        await state.clear()
        return await msg.answer("⚠️ У этого пользователя уже есть профиль.", reply_markup=admin_menu())
    await state.update_data(tg_id=tg_id)
    await state.set_state(CreateUser.days)
    await msg.answer(f"Сколько дней подписки? (по умолчанию {settings.default_days})", reply_markup=kb(
        [Btn(text=f"{d} дн.", callback_data=f"cd:{d}") for d in (3, 7, 31, 93)],
        [Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())],
    ))


async def _set_days(target: Message, state: FSMContext, days: int):
    await state.update_data(days=days)
    await state.set_state(CreateUser.traffic)
    await target.answer(f"Лимит трафика в ГБ в месяц (0 — безлимит, по умолчанию {settings.default_traffic_gb}):",
                        reply_markup=kb([Btn(text=f"{g or '∞'}", callback_data=f"ct:{g}") for g in (0, 50, 100, 300)],
                                        [Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())]))


@router.callback_query(CreateUser.days, F.data.startswith("cd:"))
async def create_days_btn(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _set_days(cb.message, state, int(cb.data[3:]))


@router.message(CreateUser.days)
async def create_days(msg: Message, state: FSMContext):
    days = parse_int(msg.text)
    if not days or days <= 0:
        return await msg.answer("Нужно положительное число дней.")
    await _set_days(msg, state, days)


async def _set_traffic(target: Message, state: FSMContext, gb: int):
    await state.update_data(traffic=gb)
    await state.set_state(CreateUser.devices)
    await target.answer(f"Лимит устройств (0 — без лимита, по умолчанию {settings.default_devices}):",
                        reply_markup=kb([Btn(text=str(d or '∞'), callback_data=f"cv:{d}") for d in (0, 1, 2, 3, 5)],
                                        [Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())]))


@router.callback_query(CreateUser.traffic, F.data.startswith("ct:"))
async def create_traffic_btn(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _set_traffic(cb.message, state, int(cb.data[3:]))


@router.message(CreateUser.traffic)
async def create_traffic(msg: Message, state: FSMContext):
    gb = parse_int(msg.text)
    if gb is None or gb < 0:
        return await msg.answer("Нужно число ГБ (0 — безлимит).")
    await _set_traffic(msg, state, gb)


@router.callback_query(CreateUser.devices, F.data.startswith("cv:"))
async def create_devices_btn(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(devices=int(cb.data[3:]))
    await ask_squads(cb.message, state)


@router.message(CreateUser.devices)
async def create_devices(msg: Message, state: FSMContext):
    n = parse_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("Нужно число устройств (0 — без лимита).")
    await state.update_data(devices=n)
    await ask_squads(msg, state)


def _squads_kb(squads: list[dict], chosen: list[str]):
    rows = [[Btn(text=("✅ " if s["uuid"] in chosen else "▫️ ") + s["name"],
                 callback_data=SquadCb(idx=i).pack())] for i, s in enumerate(squads)]
    rows.append([Btn(text="➡️ Создать", callback_data=SquadCb(idx=-1).pack())])
    rows.append([Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())])
    return kb(*rows)


async def ask_squads(target: Message, state: FSMContext):
    try:
        squads = [{"uuid": s["uuid"], "name": s["name"]} for s in await api.get_squads()]
    except RemnawaveError as e:
        await state.clear()
        return await target.answer(f"💥 Не удалось получить сквады: {esc(e)}", reply_markup=admin_menu())
    chosen = [u for u in settings.default_squads if any(s["uuid"] == u for s in squads)]
    if not chosen and len(squads) == 1:
        chosen = [squads[0]["uuid"]]
    await state.update_data(squads=squads, chosen=chosen)
    await state.set_state(CreateUser.squads)
    await target.answer("Выберите сквады (группы нод), к которым дать доступ:", reply_markup=_squads_kb(squads, chosen))


@router.callback_query(CreateUser.squads, SquadCb.filter(F.idx >= 0))
async def toggle_squad(cb: CallbackQuery, callback_data: SquadCb, state: FSMContext):
    data = await state.get_data()
    squads, chosen = data["squads"], list(data["chosen"])
    if callback_data.idx >= len(squads):
        return await cb.answer()
    uuid = squads[callback_data.idx]["uuid"]
    chosen.remove(uuid) if uuid in chosen else chosen.append(uuid)
    await state.update_data(chosen=chosen)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=_squads_kb(squads, chosen))


@router.callback_query(CreateUser.squads, SquadCb.filter(F.idx == -1))
async def create_finish(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("chosen"):
        return await cb.answer("Выберите хотя бы один сквад", show_alert=True)
    await cb.answer()
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    await do_create(cb.message, bot, data["tg_id"], data["days"], data["traffic"], data["devices"], data["chosen"])


async def do_create(target: Message, bot: Bot, tg_id: int, days: int, traffic: int,
                    devices: int, squads: list[str], username: str | None = None,
                    returning: bool = False) -> dict | None:
    """returning=True — бывший клиент: аккаунт с нуля, 0 дней (выключен до оплаты), без бонусов."""
    if username and (not valid_username(username) or await api.get_user_by_username(username)):
        await target.answer(f"⚠️ Имя <code>{esc(username)}</code> уже занято или недопустимо — "
                            f"создаю как <code>{esc(username_for(tg_id))}</code>.")
        username = None
    try:
        user = await api.create_user(username=username or username_for(tg_id), days=days, telegram_id=tg_id,
                                     traffic_gb=traffic, devices=devices or None, squads=squads,
                                     description="создан ботом")
    except RemnawaveError as e:
        await target.answer(f"💥 Ошибка создания: {esc(e)}", reply_markup=admin_menu())
        return None
    db.set_link(tg_id, user["id"], user["username"])
    lock_price(tg_id, user)  # заведён в этом году — тоже сохраняет старую цену
    if returning:
        try:
            user = await api.disable_user(user["id"])
        except RemnawaveError as e:
            await target.answer(f"⚠️ Не удалось выключить до оплаты: {esc(e)}")
    elif tg_id not in settings.admin_ids:
        db.mark_new_sub(tg_id)
    url = user.get("subscriptionUrl", "")
    what = "бывший клиент, выключен до оплаты" if returning else f"на {days} дн."
    await target.answer_photo(qr_file(url), caption=f"✅ <b>Создан {esc(user['username'])}</b> — {what}\n"
                                                    f"<code>{esc(url)}</code>",
                              reply_markup=user_card_kb(user["id"], user.get("status", "")))
    if returning:
        await notify(bot, tg_id,
                     "👋 Ты уже был нашим клиентом — по приглашению так не прокатит 🙂\n"
                     "Аккаунт заведён с нуля, но <b>без бонусных дней</b> на знакомство.\n\n"
                     f"{payment_details(tg_id)}\n\n📎 Оплати и пришли чек <b>PDF-файлом</b> кнопкой "
                     "«💳 Оплатить / продлить» — после проверки доступ включится.", reply_markup=user_menu())
        return user
    await notify(bot, tg_id, f"🎉 <b>Доступ открыт!</b>\nПодписка активна {days} дн.\n"
                             "Нажмите «🖼 QR-код», чтобы подключиться." + paid_promo_line(), reply_markup=user_menu())
    if settings.bday_gift_days > 0 and not (db.get_birthday(tg_id) or {"day": None})["day"]:
        db.kv_set(f"bday_nag:{tg_id}", f"0,{int(time.time())}")
        await notify(bot, tg_id, f"🎂 Кстати, укажи свой день рождения — в этот день подарю "
                                 f"<b>+{settings.bday_gift_days} дн.</b> подписки 🎁",
                     reply_markup=kb([Btn(text="🎂 Указать дату", callback_data=UserMenu(action="bday_ask").pack())]))
    return user


# ======================= заявки на доступ =======================
@router.callback_query(ReqCb.filter(F.action == "link"))
async def cb_request_link(cb: CallbackQuery, callback_data: ReqCb):
    await cb.answer()
    try:
        users = await unlinked_users()
    except RemnawaveError as e:
        return await cb.message.answer(f"💥 {esc(e)}")
    if not users:
        return await cb.message.answer("Все пользователи панели уже привязаны к Telegram.")
    users.sort(key=lambda u: (u.get("username") or "").lower())
    rows = [[Btn(text=f"{u['username']} · до {(u.get('expireAt') or '')[:10]}",
                 callback_data=LinkCb(tg_id=callback_data.tg_id, uid=u["id"]).pack())] for u in users[:60]]
    rows.append([Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())])
    await cb.message.answer(f"Кому из панели принадлежит Telegram <code>{callback_data.tg_id}</code>?",
                            reply_markup=kb(*rows))


@router.callback_query(LinkCb.filter())
async def cb_link_pick(cb: CallbackQuery, callback_data: LinkCb, bot: Bot):
    user = await api.get_user(callback_data.uid)
    if not user:
        return await cb.answer("Пользователь не найден", show_alert=True)
    if user.get("telegramId") and user["telegramId"] != callback_data.tg_id:
        return await cb.answer("Этот аккаунт уже привязан к другому Telegram", show_alert=True)
    await cb.answer()
    try:
        updated = await link_user(callback_data.tg_id, user)
    except RemnawaveError as e:
        return await cb.message.answer(f"💥 {esc(e)}")
    db.close_request(callback_data.tg_id)
    await cb.message.edit_text(f"🔗 <b>{esc(updated['username'])}</b> привязан к Telegram "
                               f"<code>{callback_data.tg_id}</code>.")
    await show_card(cb.message, updated)
    await notify(bot, callback_data.tg_id, "✅ Ваш аккаунт найден и привязан! Теперь здесь доступна "
                                           "вся информация о подписке.", reply_markup=user_menu())
    await reward_first_verified(bot, callback_data.tg_id, updated)


@router.callback_query(ReqCb.filter())
async def cb_request(cb: CallbackQuery, callback_data: ReqCb, bot: Bot):
    await cb.answer()
    tg_id = callback_data.tg_id
    await cb.message.edit_reply_markup(reply_markup=None)
    req = db.get_request(tg_id)
    if callback_data.action == "no":
        db.close_request(tg_id)
        await notify(bot, tg_id, "😔 Увы, сейчас не можем выдать доступ. Если есть вопросы — напишите администратору.")
        return await cb.message.edit_text(cb.message.html_text + "\n\n🚫 <b>Отказано</b>")
    if await find_user(tg_id):
        db.close_request(tg_id)
        return await cb.message.answer("⚠️ Профиль уже существует.")
    try:
        squads = settings.default_squads or [s["uuid"] for s in await api.get_squads()][:1]
    except RemnawaveError as e:
        return await cb.message.answer(f"💥 {esc(e)}")
    returning = callback_data.action == "old"
    user = await do_create(cb.message, bot, tg_id, 0 if returning else callback_data.days, settings.default_traffic_gb,
                           settings.default_devices, squads, username=req["username"] if req else None,
                           returning=returning)
    if not user:
        return
    db.close_request(tg_id)
    if returning:  # бывший клиент: ни пробных дней, ни реферала, ни акции
        return await cb.message.edit_text(cb.message.html_text + "\n\n👴 <b>Бывший клиент</b> — заведён с нуля, "
                                                                 "выключен до оплаты, бонусов нет.")
    await cb.message.edit_text(cb.message.html_text + f"\n\n✅ <b>Одобрено на {callback_data.days} дн.</b>")

    ref_id = req["referrer_id"] if req else None
    if ref_id and db.add_referral(tg_id, ref_id):
        text = f"🤝 Твой друг <b>{esc(user['username'])}</b> подключился по твоему приглашению. Спасибо!"
        if settings.ref_paid_bonus_days > 0:
            text += (f"\nКогда он оплатит месяц подписки, вам <b>обоим</b> начислится "
                     f"<b>+{settings.ref_paid_bonus_days} дн.</b> 🎁")
        await notify(bot, ref_id, text, reply_markup=user_menu())


async def reward_referral(target: Message, bot: Bot, invitee_tg: int, paid_days: int) -> None:
    """Приглашённый впервые оплатил месяц → по +N дней ему и пригласившему (один раз за друга)."""
    if settings.ref_paid_bonus_days <= 0 or paid_days < settings.ref_min_paid_days:
        return
    ref_id = db.claim_paid_bonus(invitee_tg)
    if not ref_id:
        return
    bonus = settings.ref_paid_bonus_days
    report = []
    for who, tg in (("приглашённому", invitee_tg), ("пригласившему", ref_id)):
        u = await find_user(tg)
        if not u:
            report.append(f"⚠️ {who}: аккаунт не найден")
            continue
        try:
            upd = await api.add_days(u, bonus)
        except RemnawaveError as e:
            report.append(f"⚠️ {who} ({esc(u['username'])}): {esc(e.message)}")
            continue
        report.append(f"🎁 +{bonus} дн. {who} <b>{esc(upd['username'])}</b>")
        if tg == invitee_tg:
            msg_text = (f"🎁 <b>Бонус за приглашение!</b>\nСпасибо за оплату — тебе и другу, который тебя пригласил, "
                        f"начислено по <b>+{bonus} дн.</b> Подписка действует до "
                        f"<b>{esc((upd.get('expireAt') or '')[:10])}</b>.")
        else:
            msg_text = (f"🎁 <b>Твой друг оплатил подписку!</b>\nЗа приглашение тебе начислено <b>+{bonus} дн.</b> "
                        f"Подписка действует до <b>{esc((upd.get('expireAt') or '')[:10])}</b>. Спасибо! 🤝")
        await notify(bot, tg, msg_text, reply_markup=user_menu())
    await target.answer("🤝 Реферальный бонус:\n" + "\n".join(report))


# ======================= добавить дни =======================
@router.callback_query(AdminMenu.filter(F.action == "days"))
async def days_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(AddDays.target)
    await cb.message.answer("📅 Кому? Введите Telegram ID, username в панели или #id панели:", reply_markup=cancel_kb())


@router.message(Command("days"))
async def cmd_days(msg: Message, state: FSMContext, bot: Bot):
    """/days <tg_id|username> <±дни>"""
    args = (msg.text or "").split()
    if len(args) == 3 and parse_int(args[2]):
        user = await resolve_target(args[1])
        if not user:
            return await msg.answer("Пользователь не найден.")
        return await apply_days(msg, bot, user, parse_int(args[2]))
    await state.set_state(AddDays.target)
    await msg.answer("Формат: <code>/days ID 31</code> (или -5 чтобы убавить). Либо введите ID:",
                     reply_markup=cancel_kb())


@router.message(AddDays.target)
async def days_target(msg: Message, state: FSMContext):
    user = await resolve_target(msg.text or "")
    if not user:
        return await msg.answer("Не найден. Попробуйте ещё раз:", reply_markup=cancel_kb())
    await state.update_data(uid=user["id"])
    await state.set_state(AddDays.days)
    await msg.answer(await profile_text(user, for_admin=True) +
                     "\n\nСколько дней добавить? (отрицательное число — убавить)", reply_markup=cancel_kb())


@router.message(AddDays.days)
async def days_value(msg: Message, state: FSMContext, bot: Bot):
    days = parse_int(msg.text)
    if not days:
        return await msg.answer("Нужно целое число, не 0.")
    uid = (await state.get_data())["uid"]
    await state.clear()
    user = await api.get_user(uid)
    if user:
        await apply_days(msg, bot, user, days)


async def apply_days(target: Message, bot: Bot, user: dict, days: int) -> dict | None:
    try:
        updated = await api.add_days(user, days)
    except RemnawaveError as e:
        await target.answer(f"💥 {esc(e.message)}", reply_markup=back_to_admin())
        return None
    sign = "+" if days > 0 else ""
    await target.answer(f"✅ {sign}{days} дн. для <b>{esc(updated['username'])}</b>")
    await show_card(target, updated)
    if days > 0:
        await notify(bot, tg_of(updated), f"🎁 Подписка продлена на <b>{days} дн.</b>\n"
                                          f"Действует до {esc((updated.get('expireAt') or '')[:10])}.",
                     reply_markup=user_menu())
    return updated


# ======================= поиск и карточка =======================
@router.callback_query(AdminMenu.filter(F.action == "find"))
async def find_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(FindUser.query)
    await cb.message.answer("🔎 Telegram ID, username в панели или #id панели:", reply_markup=cancel_kb())


@router.message(FindUser.query)
async def find_query(msg: Message, state: FSMContext):
    user = await resolve_target(msg.text or "")
    if not user:
        return await msg.answer("Не найден. Ещё раз:", reply_markup=cancel_kb())
    await state.clear()
    await show_card(msg, user)


@router.message(Command("user"))
async def cmd_user(msg: Message):
    args = (msg.text or "").split(maxsplit=1)
    user = await resolve_target(args[1]) if len(args) > 1 else None
    if not user:
        return await msg.answer("Формат: <code>/user ID|username</code>")
    await show_card(msg, user)


@router.message(Command("bday"))
async def cmd_bday(msg: Message):
    """/bday <ID|username> <ДД.ММ | clear> — поправить дату рождения клиента."""
    args = (msg.text or "").split()
    if len(args) != 3:
        return await msg.answer("Формат: <code>/bday ID 14.03</code> или <code>/bday ID clear</code>")
    user = await resolve_target(args[1])
    tg = tg_of(user) if user else None
    if not tg:
        return await msg.answer("Клиент не найден или не привязал Telegram.")
    if args[2].lower() == "clear":
        db.set_birthday(tg, None, None, force=True)
        return await msg.answer(f"🎂 Дата рождения {esc(user['username'])} очищена.")
    parsed = parse_birthday(args[2])
    if not parsed:
        return await msg.answer("Не понял дату. Пример: <code>14.03</code>")
    db.set_birthday(tg, *parsed, force=True)
    await msg.answer(f"🎂 {esc(user['username'])}: ДР {fmt_birthday(*parsed)}.")


def verified_report() -> str:
    """Все, кто привязал Telegram к подписке: ДР (или отказ / ещё не ответил) и место в акции."""
    from datetime import datetime
    rows = db.conn.execute(
        "SELECT l.tg_id, l.username, b.day, b.month, b.set_at, p.created_at AS promo_at, "
        "(SELECT COUNT(*) FROM first_promo p2 WHERE p2.created_at <= p.created_at) AS place "
        "FROM links l LEFT JOIN birthdays b ON b.tg_id = l.tg_id LEFT JOIN first_promo p ON p.tg_id = l.tg_id "
        "ORDER BY COALESCE(b.set_at, 9e18), l.username").fetchall()
    rows = [r for r in rows if r["tg_id"] not in settings.admin_ids]
    full = sum(1 for r in rows if r["set_at"])
    lines = [f"✅ <b>Верификация</b>: привязали {len(rows)}, полностью (ответили про ДР) — {full}\n"]
    for r in rows:
        if r["set_at"] is None:
            bd = "⏳ про ДР ещё не ответил"
        elif r["day"] is None:
            bd = "🙈 ДР не указал"
        else:
            bd = f"🎂 {fmt_birthday(r['day'], r['month'])}"
        when = datetime.fromtimestamp(r["set_at"]).strftime("%d.%m %H:%M") if r["set_at"] else ""
        promo = f" · 🏆 место {r['place']}" if r["promo_at"] else ""
        lines.append(f"• <b>{esc(r['username'])}</b> (<code>{r['tg_id']}</code>) — {bd}"
                     + (f" · {when}" if when else "") + promo)
    return "\n".join(lines[:80])


@router.message(Command("verified"))
async def cmd_verified(msg: Message):
    await msg.answer(verified_report())


@router.message(Command("bdays"))
async def cmd_bdays(msg: Message):
    """Все дни рождения клиентов: ближайшие сверху."""
    from datetime import date, datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=3))).date()
    rows = db.conn.execute(
        "SELECT b.tg_id, b.day, b.month, l.username FROM birthdays b LEFT JOIN links l ON l.tg_id = b.tg_id "
        "WHERE b.day IS NOT NULL").fetchall()
    skipped = db.conn.execute("SELECT COUNT(*) FROM birthdays WHERE day IS NULL").fetchone()[0]

    def days_until(d: int, m: int) -> int:
        for year in (today.year, today.year + 1, today.year + 2):
            try:
                nxt = date(year, m, d)
            except ValueError:  # 29.02 в невисокосный год — празднуем 28.02
                nxt = date(year, 2, 28)
            if nxt >= today:
                return (nxt - today).days
        return 999

    items = sorted(((days_until(r["day"], r["month"]), r) for r in rows), key=lambda x: x[0])
    if not items:
        return await msg.answer(f"🎂 Пока никто не указал день рождения. Отказались: {skipped}.")
    lines = [f"🎂 <b>Дни рождения</b> (указали {len(items)}, отказались {skipped})\n"]
    for left, r in items[:50]:
        when = "🎉 <b>сегодня</b>" if left == 0 else ("завтра" if left == 1 else f"через {left} дн.")
        lines.append(f"{fmt_birthday(r['day'], r['month'])} — <b>{esc(r['username'] or r['tg_id'])}</b> · {when}")
    await msg.answer("\n".join(lines))


@router.message(Command("link"))
async def cmd_link(msg: Message, bot: Bot):
    """/link <tg_id> <username или ссылка подписки> — привязать существующий аккаунт панели к Telegram."""
    args = (msg.text or "").split()
    if len(args) != 3 or not args[1].isdigit():
        return await msg.answer("Формат: <code>/link TG_ID username</code> или <code>/link TG_ID ссылка_подписки</code>")
    user = await find_by_name_or_link(args[2])
    if not user:
        return await msg.answer("В панели нет такого пользователя.")
    tg_id = int(args[1])
    updated = await link_user(tg_id, user)
    await msg.answer(f"🔗 {esc(user['username'])} привязан к Telegram {tg_id}.")
    await reward_first_verified(bot, tg_id, updated)


@router.callback_query(UserCb.filter())
async def cb_user_card(cb: CallbackQuery, callback_data: UserCb, state: FSMContext, bot: Bot):
    user = await api.get_user(callback_data.uid)
    if not user:
        return await cb.answer("Пользователь не найден", show_alert=True)
    act = callback_data.action
    await cb.answer()
    try:
        if act == "add":
            await apply_days(cb.message, bot, user, callback_data.days)
        elif act == "custom":
            await state.set_state(CustomDays.user)
            await state.update_data(uid=user["id"])
            await cb.message.answer("Сколько дней? (например 45 или -10)", reply_markup=cancel_kb())
        elif act in ("enable", "disable"):
            updated = await (api.enable_user if act == "enable" else api.disable_user)(user["id"])
            await show_card(cb.message, updated)
        elif act == "reset":
            await show_card(cb.message, await api.reset_traffic(user["id"]))
        elif act == "qr":
            url = user.get("subscriptionUrl", "")
            await cb.message.answer_photo(qr_file(url), caption=f"<code>{esc(url)}</code>")
        elif act == "refresh":
            await show_card(cb.message, user)
        elif act == "delete":
            await cb.message.answer(
                f"🗑 Удалить <b>{esc(user['username'])}</b> навсегда?\n"
                f"Подписка до {esc((user.get('expireAt') or '')[:10])}. Вернуть будет нельзя.",
                reply_markup=kb([Btn(text="✅ Да", callback_data=UserCb(action="delete_yes", uid=user["id"]).pack()),
                                 Btn(text="✖️ Нет", callback_data=UserCb(action="delete_no", uid=user["id"]).pack())]))
        elif act == "delete_no":
            await cb.message.edit_text(f"Отменено — <b>{esc(user['username'])}</b> не удалён.")
        elif act == "delete_yes":
            tg = tg_of(user)
            await api.delete_user(user["id"])
            db.forget_user(tg, user["id"])
            await cb.message.edit_text(f"🗑 <b>{esc(user['username'])}</b> удалён.")
    except RemnawaveError as e:
        await cb.message.answer(f"💥 {esc(e)}")


@router.message(CustomDays.user)
async def custom_user_days(msg: Message, state: FSMContext, bot: Bot):
    days = parse_int(msg.text)
    if not days:
        return await msg.answer("Нужно целое число, не 0.")
    uid = (await state.get_data())["uid"]
    await state.clear()
    user = await api.get_user(uid)
    if user:
        await apply_days(msg, bot, user, days)


# ======================= чеки =======================
@router.callback_query(AdminMenu.filter(F.action == "payments"))
async def payments_list(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    pending = db.pending_payments()
    if not pending:
        return await cb.message.answer("🧾 Нет чеков на проверке.", reply_markup=back_to_admin())
    for p in pending[:20]:
        await bot.send_document(cb.from_user.id, p["file_id"],
                                caption=f"🧾 <b>Чек #{p['id']}</b> от {esc(p['tg_name'])} "
                                        f"(<code>{p['tg_id']}</code>)",
                                reply_markup=payment_kb(p["id"]))


async def approve_payment(target: Message, bot: Bot, pid: int, days: int, admin_id: int, warn: str = "") -> None:
    """warn — предупреждение клиенту (например, о неполной оплате), добавляется к сообщению о продлении."""
    p = db.get_payment(pid)
    if not p or not db.decide_payment(pid, "approved", admin_id, days):
        await target.answer("⚠️ Этот чек уже обработан.")
        return
    user = await find_user(p["tg_id"])
    if not user:
        db.reopen_payment(pid)
        await target.answer("💥 Профиль пользователя не найден в панели — чек возвращён в очередь.")
        return
    try:
        updated = await api.add_days(user, days)
    except RemnawaveError as e:
        db.reopen_payment(pid)
        await target.answer(f"💥 Панель вернула ошибку, чек возвращён в очередь: {esc(e)}")
        return
    await target.answer(f"✅ Чек #{pid} принят: +{days} дн. для {esc(updated['username'])}, "
                        f"до {esc((updated.get('expireAt') or '')[:10])}.")
    await notify(bot, p["tg_id"], f"✅ <b>Оплата подтверждена!</b>\nПодписка продлена на {days} дн., "
                                  f"действует до {esc((updated.get('expireAt') or '')[:10])}."
                                  + (f"\n\n{warn}" if warn else ""),
                 reply_markup=user_menu())
    await reward_referral(target, bot, p["tg_id"], days)
    await reward_first_paid(bot, target, p["tg_id"], days)


@router.callback_query(PayCb.filter())
async def cb_payment(cb: CallbackQuery, callback_data: PayCb, state: FSMContext, bot: Bot):
    pid = callback_data.pid
    p = db.get_payment(pid)
    if not p or p["status"] != "pending":
        await cb.answer("Чек уже обработан", show_alert=True)
        return await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    if callback_data.action == "ok":
        await cb.message.edit_reply_markup(reply_markup=None)
        await approve_payment(cb.message, bot, pid, callback_data.days, cb.from_user.id)
    elif callback_data.action == "custom":
        await state.set_state(CustomDays.payment)
        await state.update_data(pid=pid)
        await cb.message.answer(f"На сколько дней продлить по чеку #{pid}?", reply_markup=cancel_kb())
    elif callback_data.action == "amount":
        tariff = price_for(p["tg_id"])
        await state.set_state(CustomDays.amount)
        await state.update_data(pid=pid)
        await cb.message.answer(f"Сколько ₽ пришло по чеку #{pid}? Тариф клиента — <b>{tariff} ₽</b> за 31 день "
                                f"(≈ {tariff / 31:.2f} ₽/день). Дни посчитаю сам.", reply_markup=cancel_kb())
    elif callback_data.action == "no":
        if db.decide_payment(pid, "rejected", cb.from_user.id):
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.answer(f"❌ Чек #{pid} отклонён.")
            await notify(bot, p["tg_id"], "❌ Чек не принят. Если это ошибка — свяжитесь с администратором "
                                          "или отправьте корректный чек ещё раз.", reply_markup=user_menu())


@router.message(CustomDays.payment)
async def custom_payment_days(msg: Message, state: FSMContext, bot: Bot):
    days = parse_int(msg.text)
    if not days or days <= 0:
        return await msg.answer("Нужно положительное число дней.")
    pid = (await state.get_data())["pid"]
    await state.clear()
    await approve_payment(msg, bot, pid, days, msg.from_user.id)


def days_for_amount(amount: int, tariff: int) -> int:
    """Дни пропорционально сумме: тариф за 31 день, округление вниз."""
    return amount * 31 // tariff


@router.message(CustomDays.amount)
async def amount_payment_days(msg: Message, state: FSMContext, bot: Bot):
    amount = parse_int(msg.text)
    if not amount or amount <= 0:
        return await msg.answer("Нужна сумма в рублях, целым числом (например 150).")
    pid = (await state.get_data())["pid"]
    p = db.get_payment(pid)
    if not p:
        await state.clear()
        return await msg.answer("Чек не найден.")
    tariff = price_for(p["tg_id"])
    days = days_for_amount(amount, tariff)
    if days < 1:
        return await msg.answer(f"На {amount} ₽ не выходит даже одного дня (тариф {tariff} ₽ / 31 дн.). "
                                f"Введи другую сумму или отклони чек.")
    await state.clear()
    warn = ""
    if amount < tariff:
        warn = (f"⚠️ <b>Сумма перевода ({amount} ₽) меньше твоего тарифа ({tariff} ₽ за 31 день).</b>\n"
                f"Начислено {days} дн. — пропорционально оплате. Пожалуйста, в следующий раз переводи полную сумму.")
        await msg.answer(f"⚠️ Недоплата: {amount} из {tariff} ₽ → {days} дн., клиенту отправлено предупреждение.")
    await approve_payment(msg, bot, pid, days, msg.from_user.id, warn)


# ======================= состояние «всё ли ок» =======================
async def _health(target: Message) -> None:
    from monitor import status_report  # monitor импортирует handlers.servers — подгружаем при вызове
    wait = await target.answer("🩺 Проверяю панель, ноды и серверы…")
    text = await status_report()
    await wait.edit_text(text, reply_markup=kb(
        [Btn(text="♻️ Проверить ещё раз", callback_data=AdminMenu(action="health").pack())],
        [Btn(text="⬅️ Админ-меню", callback_data=AdminMenu(action="menu").pack())]))


@router.callback_query(AdminMenu.filter(F.action == "health"))
async def cb_health(cb: CallbackQuery):
    await cb.answer()
    await _health(cb.message)


@router.message(Command("status"))
async def cmd_status(msg: Message):
    await _health(msg)


# ======================= ноды =======================
def _nodes_text(nodes: list[dict]) -> str:
    if not nodes:
        return "Нод нет."
    lines = ["<b>🖥 Ноды</b>\n"]
    for n in nodes:
        if n.get("isDisabled"):
            icon = "⚫"
        elif n.get("isConnected"):
            icon = "🟢"
        else:
            icon = "🔴"
        v = n.get("versions") or {}
        lines.append(
            f"{icon} <b>{esc(n.get('name'))}</b> {esc(n.get('countryCode', ''))} — {esc(n.get('address'))}\n"
            f"   онлайн: {n.get('usersOnline', 0)}, трафик: {human_bytes(n.get('trafficUsedBytes'))}, "
            f"node {esc(v.get('node', '?'))} / xray {esc(v.get('xray', '?'))}"
        )
        if not n.get("isConnected") and n.get("lastStatusMessage"):
            lines.append(f"   ⚠️ {esc(n['lastStatusMessage'])[:200]}")
    return "\n".join(lines)


def _nodes_kb(nodes: list[dict]):
    rows = [[Btn(text=f"🔄 {n.get('name')}", callback_data=NodeCb(action="restart", uuid=n["uuid"]).pack())]
            for n in nodes]
    rows.append([Btn(text="🔄 Перезапустить все ноды", callback_data=NodeCb(action="restart_all").pack())])
    rows.append([Btn(text="♻️ Обновить список", callback_data=NodeCb(action="list").pack()),
                 Btn(text="⬅️ Меню", callback_data=AdminMenu(action="menu").pack())])
    return kb(*rows)


@router.callback_query(AdminMenu.filter(F.action == "nodes"))
@router.callback_query(NodeCb.filter(F.action == "list"))
async def nodes_list(cb: CallbackQuery):
    await cb.answer()
    try:
        nodes = await api.get_nodes()
    except RemnawaveError as e:
        return await cb.message.answer(f"💥 {esc(e)}", reply_markup=back_to_admin())
    await cb.message.answer(_nodes_text(nodes), reply_markup=_nodes_kb(nodes))


@router.callback_query(NodeCb.filter(F.action.in_({"restart", "restart_all"})))
async def nodes_restart(cb: CallbackQuery, callback_data: NodeCb):
    try:
        if callback_data.action == "restart_all":
            await api.restart_all_nodes()
        else:
            await api.restart_node(callback_data.uuid)
    except RemnawaveError as e:
        return await cb.answer(f"Ошибка: {e.message}"[:190], show_alert=True)
    await cb.answer("Команда на перезапуск отправлена", show_alert=True)
