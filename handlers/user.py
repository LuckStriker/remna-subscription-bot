"""Сценарии обычного пользователя: профиль, QR, устройства, отправка чека, заявка на доступ."""
import io
import logging
import re
import time

import receipts

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import settings
from aiogram.types import CopyTextButton
from aiogram.types import InlineKeyboardButton as Btn

from guides import guide

from core import (api, db, esc, find_user, fmt_birthday, is_admin, link_user, parse_birthday, price_for,
                  profile_text, qr_file, valid_username)
from promo import paid_promo_line, payment_details, processing_note, promo_slots_left, reward_first_verified
from tracking import clean_chat
from keyboards import (UserMenu, access_request_kb, admin_menu, back_to_user_menu, kb,
                       payment_kb, user_menu)

router = Router()
# В группе бот не должен реагировать на /start, PDF и прочее — только в личке
router.message.filter(F.chat.type == "private")
log = logging.getLogger(__name__)

MAX_RECEIPT_MB = 10


class PayStates(StatesGroup):
    waiting_receipt = State()


class LinkStates(StatesGroup):
    waiting_sub = State()


LINK_ATTEMPTS = 5          # неудачных попыток привязки…
LINK_WINDOW = 3600         # …в час
_link_fails: dict[int, list[float]] = {}
SHORT_UUID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext, command: CommandObject | None = None):
    await state.clear()
    if is_admin(msg.from_user.id):
        return await msg.answer(f"<b>🤖 {esc(settings.bot_name)} на связи, админ!</b>\nВыберите действие:",
                                reply_markup=admin_menu())
    user = await find_user(msg.from_user.id)
    if user:
        if command and command.args and command.args.startswith("ref_"):
            # Старый клиент пришёл по чужой ссылке — бонусы только для новых
            return await msg.answer(
                "👋 Ты уже наш клиент — реферальная ссылка работает только для новых, так что бонус "
                "тут не прокатит 🙂\n\nЧтобы продлить подписку — оплати и пришли чек:\n\n"
                f"{payment_details(msg.from_user.id)}", reply_markup=user_menu())
        await msg.answer(f"👋 Привет, {esc(msg.from_user.first_name)}! Я {esc(settings.bot_name)}, "
                         f"тут твой личный кабинет.", reply_markup=user_menu())
        return await maybe_ask_birthday(msg, state)
    if command and command.args == "link":
        return await start_link(msg, state)
    if command and command.args and command.args.startswith("ref_"):
        ref = await _accept_referrer(msg.from_user.id, command.args[4:])
        if ref:
            return await msg.answer(
                f"🤝 Привет! Тебя пригласил друг — <b>{esc(ref['username'])}</b>.\n\n"
                f"Я {esc(settings.bot_name)} 🤖. Нажми «🔑 Запросить доступ», придумай себе имя — "
                f"и после одобрения получишь <b>{settings.ref_trial_days} дн.</b> на знакомство."
                + (f"\n\n🎁 А когда оплатишь месяц, тебе и другу начислится по "
                   f"<b>+{settings.ref_paid_bonus_days} дн.</b>" if settings.ref_paid_bonus_days > 0 else "")
                + "\n\n<i>Бонусы — только для новых. Если ты уже был нашим клиентом, аккаунт заведём заново, "
                  "но без бонусных дней.</i>" + paid_promo_line(msg.from_user.id),
                reply_markup=user_menu(registered=False))
    await msg.answer(
        f"🔒 Ваш Telegram пока не привязан к подписке.\n"
        f"Ваш Telegram ID: <code>{msg.from_user.id}</code>\n\n"
        f"• Уже пользуетесь сервисом? Нажмите «🔗 Привязать» и пришлите свою ссылку подписки — "
        f"бот сам найдёт аккаунт и привяжет его.\n"
        f"• Новый пользователь? Нажмите «🔑 Запросить доступ»."
        + (f"\n\n🏆 Первые {settings.first_promo_limit}, кто привяжет подписку, получают "
           f"<b>+{settings.first_promo_days} дн.</b> Осталось мест: <b>{promo_slots_left()}</b>"
           if promo_slots_left() > 0 else "") + paid_promo_line(msg.from_user.id),
        reply_markup=user_menu(registered=False))


@router.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"Ваш Telegram ID: <code>{msg.from_user.id}</code>")


# ---------------- самопривязка по ссылке подписки ----------------
async def start_link(target: Message, state: FSMContext):
    await state.set_state(LinkStates.waiting_sub)
    await target.answer(
        "🔗 <b>Привязка подписки</b>\n\n"
        "Пришлите сюда вашу <b>ссылку подписки</b> — ту, что добавлена в приложение "
        "(вида <code>https://sub.…/AbCd…</code>).\n"
        "Где взять: в приложении (Happ, v2rayTun, Hiddify и т.п.) откройте подписку → «Поделиться/Копировать ссылку».\n\n"
        "<i>Ссылка — это ваш личный ключ, поэтому так бот понимает, что аккаунт ваш.</i>"
        + (f"\n\n🏆 Осталось мест в акции: <b>{promo_slots_left()}</b> — первые {settings.first_promo_limit} "
           f"получают <b>+{settings.first_promo_days} дн.</b>" if promo_slots_left() > 0 else ""),
        reply_markup=back_to_user_menu())


@router.callback_query(UserMenu.filter(F.action == "link"))
async def cb_link(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if await find_user(cb.from_user.id):
        return await cb.message.answer("✅ Ваш аккаунт уже привязан.", reply_markup=user_menu())
    await _tidy(cb)
    await start_link(cb.message, state)


@router.message(LinkStates.waiting_sub, F.text, ~F.text.startswith("/"))
async def on_sub_link(msg: Message, state: FSMContext, bot: Bot):
    tg_id = msg.from_user.id
    now = time.time()
    fails = [t for t in _link_fails.get(tg_id, []) if now - t < LINK_WINDOW]
    _link_fails[tg_id] = fails
    if len(fails) >= LINK_ATTEMPTS:
        return await msg.answer("⛔ Слишком много неудачных попыток. Попробуйте через час или напишите администратору.")

    # Ссылка — секрет, не оставляем её в переписке
    try:
        await msg.delete()
    except Exception:
        pass

    text = msg.text.strip()
    path_parts = [p for p in re.split(r"[/?#\s]+", text) if p]
    candidates = [p for p in reversed(path_parts) if SHORT_UUID_RE.fullmatch(p)][:3]
    user = None
    for cand in candidates:
        user = await api.get_user_by_short_uuid(cand)
        if user:
            break
    if not user:
        fails.append(now)
        left = LINK_ATTEMPTS - len(fails)
        return await msg.answer(f"❌ Не нашёл подписку по этой ссылке. Проверьте, что скопировали её целиком. "
                                f"Осталось попыток: {left}.", reply_markup=back_to_user_menu())

    owner = user.get("telegramId")
    if owner and owner != tg_id:
        await state.clear()
        await msg.answer("⚠️ Эта подписка уже привязана к другому Telegram-аккаунту. "
                         "Если это ошибка — напишите администратору.")
        await _tell_admins(bot, f"⚠️ <b>Попытка привязать чужую подписку</b>\n"
                                f"{esc(msg.from_user.full_name)} (<code>{tg_id}</code>) прислал ссылку аккаунта "
                                f"<b>{esc(user['username'])}</b>, который привязан к <code>{owner}</code>.")
        return

    await state.clear()
    try:
        updated = await link_user(tg_id, user)
    except Exception as e:
        log.warning("Самопривязка %s не удалась: %s", tg_id, e)
        return await msg.answer("💥 Не получилось сохранить привязку, попробуйте позже.")
    _link_fails.pop(tg_id, None)
    await msg.answer(f"✅ Готово! Подписка <b>{esc(updated['username'])}</b> привязана к вашему Telegram.\n"
                     f"Теперь здесь видны срок, трафик и устройства, а перед окончанием подписки придёт напоминание.",
                     reply_markup=user_menu())
    await _tell_admins(bot, f"🔗 {esc(msg.from_user.full_name)} (@{esc(msg.from_user.username or '—')}, "
                            f"<code>{tg_id}</code>) сам привязал аккаунт <b>{esc(updated['username'])}</b>.")
    await reward_first_verified(bot, tg_id, updated)
    await maybe_ask_birthday(msg, state)


# ---------------- день рождения ----------------
class BdayStates(StatesGroup):
    wait = State()


async def maybe_ask_birthday(target: Message, state: FSMContext, force: bool = False) -> None:
    """Спрашиваем у подтверждённых клиентов (аккаунт уже найден). force — по кнопке из напоминания:
    спрашиваем и тех, кто раньше нажал «не скажу»."""
    row = db.get_birthday(target.chat.id)
    if settings.bday_gift_days <= 0 or (row and (row["day"] or not force)):
        return
    await state.set_state(BdayStates.wait)
    await target.answer(
        f"🎂 Кстати, когда у тебя день рождения?\n"
        f"Напиши дату в формате <b>ДД.ММ</b> (например <code>14.03</code>) — в этот день я подарю тебе "
        f"<b>{settings.bday_gift_days} дн.</b> подписки 🎁\n\n<i>Дату можно указать только один раз.</i>",
        reply_markup=kb([Btn(text="🙈 Не скажу", callback_data=UserMenu(action="bday_skip").pack())]))


@router.message(BdayStates.wait, F.text, ~F.text.startswith("/"))
async def on_birthday(msg: Message, state: FSMContext, bot: Bot):
    parsed = parse_birthday(msg.text)
    if not parsed:
        return await msg.answer("Не понял дату 🤔 Напиши так: <code>14.03</code>",
                                reply_markup=kb([Btn(text="🙈 Не скажу",
                                                     callback_data=UserMenu(action="bday_skip").pack())]))
    await state.clear()
    if db.set_birthday(msg.from_user.id, *parsed):
        await msg.answer(f"🎉 Запомнил: <b>{fmt_birthday(*parsed)}</b>. Жди подарок!", reply_markup=user_menu())
        await _verified_note(bot, msg.from_user, f"ДР — <b>{fmt_birthday(*parsed)}</b> 🎂")
    else:
        await msg.answer("Дата уже указана. Если ошибся — напиши администратору.", reply_markup=user_menu())


@router.callback_query(UserMenu.filter(F.action == "bday_skip"))
async def cb_bday_skip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    first_time = db.set_birthday(cb.from_user.id, None, None)
    await cb.answer("Ок, не спрашиваю 🙂")
    await _tidy(cb)
    if first_time:
        await _verified_note(bot, cb.from_user, "ДР указывать не стал 🙈")


@router.callback_query(UserMenu.filter(F.action == "bday_ask"))
async def cb_bday_ask(cb: CallbackQuery, state: FSMContext):
    row = db.get_birthday(cb.from_user.id)
    if row and row["day"]:
        return await cb.answer(f"Уже знаю: {fmt_birthday(row['day'], row['month'])} 🎂", show_alert=True)
    if not await find_user(cb.from_user.id):
        return await cb.answer("Сначала нужна подписка", show_alert=True)
    await cb.answer()
    await maybe_ask_birthday(cb.message, state, force=True)


@router.callback_query(UserMenu.filter(F.action == "bday_never"))
async def cb_bday_never(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    db.set_birthday(cb.from_user.id, None, None)
    db.kv_set(f"bday_nag:{cb.from_user.id}", f"99,{int(time.time())}")
    await cb.answer("Ок, больше не напоминаю 🙂")
    await _tidy(cb)


async def _verified_note(bot: Bot, tg_user, detail: str) -> None:
    """Админу: клиент прошёл верификацию полностью (подписка привязана + ответил про ДР)."""
    user = await find_user(tg_user.id)
    acc = f"<b>{esc(user['username'])}</b>" if user else "—"
    await _tell_admins(bot, f"✅ <b>Полная верификация</b>\n"
                            f"{esc(tg_user.full_name)} (@{esc(tg_user.username or '—')}, <code>{tg_user.id}</code>)\n"
                            f"Аккаунт: {acc}\n{detail}")


async def _tell_admins(bot: Bot, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


@router.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    await cmd_start(msg, state)


async def _tidy(cb: CallbackQuery) -> None:
    """Убирает экран, с которого нажали кнопку: в чате остаётся только актуальный экран."""
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(UserMenu.filter(F.action == "menu"))
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await _tidy(cb)
    registered = is_admin(cb.from_user.id) or bool(await find_user(cb.from_user.id))
    await cb.message.answer("Главное меню:", reply_markup=user_menu(registered))


@router.callback_query(UserMenu.filter(F.action == "clean"))
async def cb_clean(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await cb.answer("Чистим…")
    await clean_chat(bot, cb.from_user.id)
    await _tidy(cb)
    registered = bool(await find_user(cb.from_user.id))
    await bot.send_message(cb.from_user.id, "🧹 Чат очищен.", reply_markup=user_menu(registered))


async def _require_user(cb: CallbackQuery) -> dict | None:
    user = await find_user(cb.from_user.id)
    if not user:
        await cb.answer()
        await cb.message.answer("❌ Подписка не найдена.", reply_markup=user_menu(registered=False))
    return user


@router.callback_query(UserMenu.filter(F.action == "profile"))
async def cb_profile(cb: CallbackQuery, state: FSMContext):
    user = await _require_user(cb)
    if not user:
        return
    await cb.answer()
    await _tidy(cb)
    await cb.message.answer(await profile_text(user), reply_markup=user_menu())
    await maybe_ask_birthday(cb.message, state)


@router.callback_query(UserMenu.filter(F.action == "qr"))
async def cb_qr(cb: CallbackQuery):
    user = await _require_user(cb)
    if not user:
        return
    await cb.answer()
    await _tidy(cb)
    url = user.get("subscriptionUrl", "")
    await cb.message.answer_photo(
        qr_file(url),
        caption=f"🖼 <b>QR-код подписки</b>\nОтсканируйте его в приложении или скопируйте ссылку:\n<code>{esc(url)}</code>",
        reply_markup=back_to_user_menu(),
    )


@router.callback_query(UserMenu.filter(F.action == "guide"))
async def cb_guide(cb: CallbackQuery):
    await cb.answer()
    await _tidy(cb)
    await cb.message.answer("📲 <b>Как подключиться</b>\nВыбери своё устройство:", reply_markup=kb(
        [Btn(text="🍏 iPhone / iPad / Mac", callback_data=UserMenu(action="guide_ios").pack())],
        [Btn(text="🤖 Android", callback_data=UserMenu(action="guide_android").pack())],
        [Btn(text="🌍 Российский App Store — как сменить регион", callback_data=UserMenu(action="guide_appstore").pack())],
        [Btn(text="⬅️ Меню", callback_data=UserMenu(action="menu").pack())],
    ))


@router.callback_query(UserMenu.filter(F.action.in_({"guide_ios", "guide_android", "guide_appstore"})))
async def cb_guide_platform(cb: CallbackQuery, callback_data: UserMenu):
    key = callback_data.action.removeprefix("guide_")
    await cb.answer()
    await _tidy(cb)
    rows = []
    if key in ("ios", "android"):
        user = await find_user(cb.from_user.id)
        url = (user or {}).get("subscriptionUrl")
        if url:  # кнопка сама копирует ссылку в буфер — не нужно выделять текст
            rows.append([Btn(text="📋 Скопировать ссылку подписки", copy_text=CopyTextButton(text=url))])
            rows.append([Btn(text="🖼 Показать QR-код", callback_data=UserMenu(action="qr").pack())])
        if key == "ios":
            rows.append([Btn(text="🌍 Как сменить регион App Store", callback_data=UserMenu(action="guide_appstore").pack())])
    rows.append([Btn(text="⬅️ К выбору устройства", callback_data=UserMenu(action="guide").pack())])
    await cb.message.answer(guide(key), reply_markup=kb(*rows), disable_web_page_preview=True)


@router.callback_query(UserMenu.filter(F.action == "devices"))
async def cb_devices(cb: CallbackQuery):
    user = await _require_user(cb)
    if not user:
        return
    await cb.answer()
    await _tidy(cb)
    data = await api.get_devices(user["id"])
    devices = data.get("devices", [])
    limit = user.get("hwidDeviceLimit")
    head = f"📱 <b>Устройства: {len(devices)}{f' из {limit}' if limit else ''}</b>"
    if not devices:
        return await cb.message.answer(head + "\n\nПока ни одно устройство не подключалось.",
                                       reply_markup=back_to_user_menu())
    rows = []
    for i, d in enumerate(devices, 1):
        name = " ".join(x for x in (d.get("platform"), d.get("osVersion"), d.get("deviceModel")) if x) or "Неизвестно"
        rows.append(f"{i}. {esc(name)} — с {esc((d.get('createdAt') or '')[:10])}")
    await cb.message.answer(head + "\n\n" + "\n".join(rows) +
                            "\n\n<i>Чтобы отвязать устройство, напишите администратору.</i>",
                            reply_markup=back_to_user_menu())


# ---------------- чеки ----------------
@router.callback_query(UserMenu.filter(F.action == "pay"))
async def cb_pay(cb: CallbackQuery, state: FSMContext):
    user = await _require_user(cb)
    if not user:
        return
    await cb.answer()
    if db.user_has_pending(cb.from_user.id):
        return await cb.message.answer("⏳ Ваш предыдущий чек ещё на проверке. Дождитесь решения администратора.")
    await state.set_state(PayStates.waiting_receipt)
    await _tidy(cb)
    rows = []
    if settings.pay_phone:
        rows.append([Btn(text="📋 Скопировать телефон", copy_text=CopyTextButton(text=settings.pay_phone))])
    rows.append([Btn(text="⬅️ Меню", callback_data=UserMenu(action="menu").pack())])
    await cb.message.answer(f"💳 {payment_details(cb.from_user.id)}\n\n"
                            f"🗓 <b>Оплачивайте в день окончания подписки и сразу присылайте чек.</b>\n"
                            f"Платите заранее — добавьте к чеку подпись «наперёд», чтобы админ знал. "
                            f"Дни не сгорают: продление добавится к концу текущего срока.\n\n"
                            f"📎 После оплаты пришлите чек из банка <b>PDF-файлом</b> "
                            f"в этот чат." + paid_promo_line(cb.from_user.id), reply_markup=kb(*rows))


@router.message(F.document, ~F.from_user.id.in_(settings.admin_ids))
async def on_document(msg: Message, bot: Bot, state: FSMContext):
    doc = msg.document
    is_pdf = doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")
    if not is_pdf:
        return await msg.answer("⚠️ Нужен именно PDF-файл с чеком.")
    if doc.file_size and doc.file_size > MAX_RECEIPT_MB * 1024 * 1024:
        return await msg.answer(f"⚠️ Файл больше {MAX_RECEIPT_MB} МБ.")
    user = await find_user(msg.from_user.id)
    if not user:
        return await msg.answer(
            "🔗 Чтобы я понял, чья это оплата, сначала <b>привяжи подписку</b> — это один раз и полминуты:\n"
            "нажми «🔗 У меня уже есть подписка — привязать» и пришли ссылку подписки из приложения "
            "(работает, даже если подписка уже закончилась).\n\nПотом просто пришли этот чек ещё раз.",
            reply_markup=user_menu(registered=False))
    if db.user_has_pending(msg.from_user.id):
        return await msg.answer("⏳ Предыдущий чек ещё на проверке.")

    await state.clear()
    pid = db.add_payment(msg.from_user.id, msg.from_user.full_name, doc.file_id, doc.file_name, doc.file_unique_id)
    dups = db.same_file_payments(doc.file_unique_id, pid)
    status_ru = {"approved": "принят", "rejected": "отклонён", "pending": "на проверке"}
    dup_note = ("\n\n🚨 <b>ЭТОТ ЖЕ ФАЙЛ УЖЕ ПРИСЫЛАЛИ:</b> "
                + "; ".join(f"чек #{d['id']} ({status_ru.get(d['status'], d['status'])}"
                            + ("" if d["tg_id"] == msg.from_user.id else f", другой Telegram <code>{d['tg_id']}</code>")
                            + ")" for d in dups[-3:])
                + "\nПроверь, что это новая оплата.") if dups else ""
    caption = (f"🧾 <b>Чек #{pid}</b>\n"
               f"От: {esc(msg.from_user.full_name)} (@{esc(msg.from_user.username or '—')})\n"
               f"Telegram ID: <code>{msg.from_user.id}</code>\n"
               f"Аккаунт: <code>{esc(user['username'])}</code>, до {esc((user.get('expireAt') or '')[:10])}\n"
               f"Тариф клиента: <b>{price_for(msg.from_user.id)} ₽</b>"
               + (" 🔒 (закреплён)" if db.kv_get(f"price_lock:{msg.from_user.id}") else "")
               + dup_note)
    # подпись клиента («наперёд» и т.п.) — короткой строкой; лимит подписи к файлу в Telegram 1024 символа
    note = " ".join((msg.caption or "").split())[:150]
    if note:
        with_note = caption + f"\n💬 Подпись клиента: <i>{esc(note)}</i>"
        caption = with_note if len(with_note) <= 1000 else caption + "\n💬 Клиент добавил подпись, но она не поместилась — спроси у него"
    delivered = []
    for admin_id in settings.admin_ids:
        try:
            m = await bot.send_document(admin_id, doc.file_id, caption=caption, reply_markup=payment_kb(pid))
            delivered.append(m)
        except Exception as e:
            log.warning("Не удалось отправить чек админу %s: %s", admin_id, e)
    if not delivered:
        db.decide_payment(pid, "rejected", 0)
        return await msg.answer("💥 Не удалось передать чек администратору, попробуйте позже.")
    await msg.answer("✅ Чек получен и отправлен на проверку. Я напишу, как только администратор примет решение.\n\n"
                     f"⏳ {processing_note()}", reply_markup=user_menu())
    await _auto_check(bot, pid, msg.from_user.id, doc, delivered)


async def _auto_check(bot: Bot, pid: int, tg_id: int, doc, admin_msgs: list) -> None:
    """Помощник проверки: читает дату/сумму/получателя из PDF и отвечает админу под чеком. Клиент этого не видит.
    Любая ошибка здесь не мешает чеку — админ просто проверит вручную."""
    report = None
    amount = days = None
    try:
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        pay10 = "".join(ch for ch in settings.pay_phone if ch.isdigit())[-10:]
        parsed = await receipts.check(buf.getvalue(), pay10)
        dup_pid = None
        if parsed and parsed.get("sbp_id"):
            dup_pid = db.payment_by_sbp(parsed["sbp_id"], pid)
            db.set_payment_check(pid, parsed["sbp_id"], parsed.get("op_ts"))
        tariff = price_for(tg_id)
        report = receipts.assess(parsed, tariff, time.time(), dup_pid)
        # Готовый расчёт по сумме из PDF: дни = сумма × 31 ÷ тариф клиента (как кнопка «По сумме из чека»).
        # Недоплату и повторный чек не подсказываем: там решение руками (недоплата — с предупреждением клиенту).
        a = (parsed or {}).get("amount")
        if a and a >= tariff and not dup_pid:
            amount, days = int(a), int(a) * 31 // tariff
    except Exception as e:
        log.warning("Автопроверка чека #%s не удалась: %s", pid, e)
        report = "🤖 <b>Проверка чека:</b> не получилось — проверь вручную."
    for m in admin_msgs:
        try:
            await bot.send_message(m.chat.id, report, reply_to_message_id=m.message_id)
        except Exception as e:
            log.warning("Не удалось отправить отчёт проверки чека #%s: %s", pid, e)
        if amount and days:
            try:
                await m.edit_reply_markup(reply_markup=payment_kb(pid, amount, days))
            except Exception as e:
                log.warning("Не удалось добавить кнопку расчёта к чеку #%s: %s", pid, e)


@router.message(PayStates.waiting_receipt)
async def on_not_pdf(msg: Message):
    await msg.answer("📎 Пришлите чек PDF-файлом (скрепка → Файл).", reply_markup=back_to_user_menu())


# ---------------- рефералка ----------------
async def _accept_referrer(tg_id: int, raw: str) -> dict | None:
    """Запоминает пригласившего, если ссылка честная: свой ID нельзя, пригласивший должен быть клиентом."""
    if not raw.isdigit() or int(raw) == tg_id or db.get_referrer_of(tg_id) is not None:
        return None
    ref_id = int(raw)
    ref_user = await find_user(ref_id)
    if not ref_user:
        return None
    db.kv_set(f"ref:{tg_id}", str(ref_id))
    return ref_user


@router.callback_query(UserMenu.filter(F.action == "invite"))
async def cb_invite(cb: CallbackQuery):
    if not await find_user(cb.from_user.id):
        return await cb.answer("Сначала нужна своя подписка", show_alert=True)
    await cb.answer()
    await _tidy(cb)
    me = await cb.bot.me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    bonus = (f"\n\n🎁 Когда друг оплатит месяц подписки, вам <b>обоим</b> начислится "
             f"<b>+{settings.ref_paid_bonus_days} дн.</b>" if settings.ref_paid_bonus_days > 0 else "")
    paid = db.paid_referral_count(cb.from_user.id)
    await cb.message.answer(
        f"🤝 <b>Пригласи друга</b>\n\n"
        f"Отправь ему эту ссылку. Друг придумает себе имя, а после одобрения админом получит "
        f"<b>{settings.ref_trial_days} дн.</b> на знакомство.{bonus}\n\n"
        f"<code>{link}</code>\n\n"
        f"Приглашено: <b>{db.referral_count(cb.from_user.id)}</b>, из них оплатили: <b>{paid}</b>",
        reply_markup=back_to_user_menu())


# ---------------- заявка на доступ (с выбором имени) ----------------
class AccessStates(StatesGroup):
    name = State()


INVITE_ONLY = ("Привет! Я подключаю только по приглашению, это маленький сервис для своих. "
               "Если среди знакомых есть кто-то, кто уже пользуется, попросите у него ссылку-приглашение из бота. "
               "По ней я вас подключу, и ещё будет бонус обоим 🤝")
_invite_noted: dict[int, float] = {}  # не дёргать админа повторно от одного и того же человека


async def _valid_referrer(tg_id: int) -> dict | None:
    """Пригласивший из реферальной ссылки (kv ref:<tg>), если он действующий клиент."""
    raw = db.kv_get(f"ref:{tg_id}")
    return await find_user(int(raw)) if raw and raw.isdigit() else None


async def _note_invite_only(bot: Bot, u) -> None:
    """Тихая заметка админу: кто постучался без приглашения. Без кнопок и без звука."""
    if time.time() - _invite_noted.get(u.id, 0) < 86400:
        return
    _invite_noted[u.id] = time.time()
    text = (f"🔒 Без приглашения, отказал автоматически: {esc(u.full_name)} "
            f"(@{esc(u.username or 'нет')}, <code>{u.id}</code>)")
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, disable_notification=True)
        except Exception as e:
            log.warning("Не удалось отправить заметку админу %s: %s", admin_id, e)


@router.callback_query(UserMenu.filter(F.action == "request"))
async def cb_request(cb: CallbackQuery, state: FSMContext):
    if await find_user(cb.from_user.id):
        await cb.answer("У вас уже есть подписка")
        return await cb.message.answer("Главное меню:", reply_markup=user_menu())
    if not db.can_request_access(cb.from_user.id):
        return await cb.answer("Заявка уже отправлена, ожидайте ответа", show_alert=True)
    await cb.answer()
    if not await _valid_referrer(cb.from_user.id):  # доступ только по приглашению: чужих бот отсекает сам
        await cb.message.answer(INVITE_ONLY, reply_markup=back_to_user_menu())
        return await _note_invite_only(cb.bot, cb.from_user)
    await state.set_state(AccessStates.name)
    await cb.message.answer(
        "✍️ <b>Придумай себе имя</b> — так тебя будет видно в приложении и в панели.\n\n"
        "Правила: 3–36 символов, только латинские буквы, цифры, <code>_</code> и <code>-</code>.\n"
        "Например: <code>ivan_petrov</code>, <code>Masha-2</code>.",
        reply_markup=back_to_user_menu())


@router.message(AccessStates.name, F.text, ~F.text.startswith("/"))
async def on_name(msg: Message, state: FSMContext, bot: Bot):
    name = msg.text.strip()
    if not valid_username(name):
        return await msg.answer("❌ Так нельзя. Нужно 3–36 символов: латиница, цифры, <code>_</code> или "
                                "<code>-</code>, без пробелов. Попробуй ещё:", reply_markup=back_to_user_menu())
    if await api.get_user_by_username(name) or db.requested_username_taken(name, msg.from_user.id):
        return await msg.answer("😕 Это имя уже занято, придумай другое:", reply_markup=back_to_user_menu())
    if not db.can_request_access(msg.from_user.id):
        await state.clear()
        return await msg.answer("⏳ Заявка уже отправлена, ожидайте ответа.")

    await state.clear()
    ref_raw = db.kv_get(f"ref:{msg.from_user.id}")
    ref_id = int(ref_raw) if ref_raw else None
    ref_user = await find_user(ref_id) if ref_id else None
    if not ref_user:  # мог начать заявку до включения правила или пригласивший пропал из панели
        await msg.answer(INVITE_ONLY, reply_markup=back_to_user_menu())
        return await _note_invite_only(bot, msg.from_user)
    db.save_request(msg.from_user.id, msg.from_user.full_name, name, ref_id)

    text = (f"🔔 <b>Заявка на доступ</b>\n"
            f"Telegram: {esc(msg.from_user.full_name)} (@{esc(msg.from_user.username or '—')})\n"
            f"ID: <code>{msg.from_user.id}</code>\n"
            f"Хочет имя: <code>{esc(name)}</code>")
    if ref_user:
        text += f"\n🤝 Пригласил: <b>{esc(ref_user['username'])}</b> (<code>{ref_id}</code>)"
    kb_ = access_request_kb(msg.from_user.id, settings.ref_trial_days if ref_user else 0)
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb_)
        except Exception as e:
            log.warning("Не удалось отправить заявку админу %s: %s", admin_id, e)
    await msg.answer(f"📨 Готово! Заявка на имя <code>{esc(name)}</code> отправлена администратору. "
                     f"Я напишу, как только будет решение.")


@router.message(AccessStates.name)
async def on_name_not_text(msg: Message):
    await msg.answer("Пришли имя текстом 🙂", reply_markup=back_to_user_menu())
