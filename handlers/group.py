"""Группа: приветствие новых участников, объявления/акции, объявления о техработах."""
import asyncio
import logging
import random
import time

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import Message

from config import settings
from core import api, db, esc, tg_of
from keyboards import AdminMenu, AnnCb, back_to_admin, cancel_kb, kb
from promo import promo_slots_left

router = Router()
log = logging.getLogger(__name__)


CHATS = {"group", "supergroup", "channel"}

HEADERS = {
    "works": "🛠 <b>Технические работы</b>",
    "promo": "🎁 <b>Акция!</b>",
    "news": "📢 <b>Объявление</b>",
    "plain": "",
}
KIND_TITLES = {"works": "🛠 Техработы", "promo": "🎁 Акция", "news": "📢 Новость", "plain": "✏️ Без заголовка"}


class AnnStates(StatesGroup):
    content = State()


async def bot_link_kb(bot: Bot, text: str = "🔑 Открыть бота", payload: str = "group"):
    me = await bot.me()
    return kb([Btn(text=text, url=f"https://t.me/{me.username}?start={payload}")])


async def _delete_later(bot: Bot, chat_id: int, message_id: int, minutes: int) -> None:
    await asyncio.sleep(minutes * 60)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ======================= приветствие =======================
@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_join(event: ChatMemberUpdated, bot: Bot):
    if event.chat.type == "channel":
        return  # в канале лично не поприветствовать — был бы пост для всех на каждого подписчика
    if settings.group_chat_id and event.chat.id != settings.group_chat_id:
        return
    member = event.new_chat_member.user
    if member.is_bot:
        return
    mention = f'<a href="tg://user?id={member.id}">{esc(member.first_name)}</a>'
    text = settings.welcome_text.replace("{name}", mention)
    me = await bot.me()
    markup = kb([Btn(text="🔗 У меня есть подписка — привязать", url=f"https://t.me/{me.username}?start=link")],
                [Btn(text="🔑 Получить доступ", url=f"https://t.me/{me.username}?start=group")])
    try:
        m = await bot.send_message(event.chat.id, text, reply_markup=markup)
    except Exception as e:
        return log.warning("Не удалось поприветствовать %s: %s", member.id, e)
    if settings.welcome_delete_after > 0:
        asyncio.create_task(_delete_later(bot, event.chat.id, m.message_id, settings.welcome_delete_after))


@router.message(Command("chatid"), F.chat.type.in_({"group", "supergroup"}),
                F.from_user.id.in_(settings.admin_ids))
async def cmd_chatid(msg: Message):
    """Помогает узнать ID группы для GROUP_CHAT_ID."""
    await msg.reply(f"ID этого чата: <code>{msg.chat.id}</code>\nЧтобы подключить группу, напишите /connect")


# ======================= подключение группы =======================
def set_home_group(chat_id: int) -> None:
    settings.group_chat_id = chat_id
    db.kv_set("group_chat_id", str(chat_id))


async def _check_rights(bot: Bot, chat_id: int) -> str:
    try:
        me = await bot.get_chat_member(chat_id, (await bot.me()).id)
    except Exception:
        return ""
    if me.status != "administrator":
        return "\n⚠️ Я не админ — сделайте меня админом, иначе не смогу публиковать."
    chat = await bot.get_chat(chat_id)
    if chat.type == "channel":
        if not getattr(me, "can_post_messages", False):
            return "\n⚠️ Дайте мне право «Публиковать сообщения» в канале."
        if not getattr(me, "can_edit_messages", False):
            return "\n⚠️ Дайте мне право «Редактировать сообщения» — без него в канале нельзя закреплять."
        return ""
    if not getattr(me, "can_pin_messages", False):
        return "\n⚠️ Дайте мне право «Закреплять сообщения», иначе объявления не закрепятся."
    return ""


# ---- канал: админ пишет от имени канала, поэтому команды приходят как channel_post ----
@router.channel_post(Command("connect"))
async def channel_connect(post: Message, bot: Bot):
    """Публиковать в канал могут только его админы — значит, команде можно доверять."""
    set_home_group(post.chat.id)
    try:
        await post.delete()
    except Exception:
        pass
    warn = await _check_rights(bot, post.chat.id)
    for admin_id in settings.admin_ids:
        await _tell_admin_once(bot, admin_id, f"✅ Канал «{esc(post.chat.title)}» подключён "
                                              f"(ID <code>{post.chat.id}</code>). Сюда буду публиковать "
                                              f"объявления и техработы.{warn}")


def from_group_admin(msg: Message) -> bool:
    """Наш админ — или анонимный админ группы (пишет «от имени группы», Telegram скрывает его ID)."""
    if msg.from_user and msg.from_user.id in settings.admin_ids:
        return True
    return bool(msg.sender_chat and msg.sender_chat.id == msg.chat.id)


@router.message(Command("connect"), F.chat.type.in_({"group", "supergroup"}), from_group_admin)
async def cmd_connect(msg: Message, bot: Bot):
    set_home_group(msg.chat.id)
    warn = await _check_rights(bot, msg.chat.id)
    m = await msg.reply(f"✅ Группа подключена — сюда буду публиковать объявления и техработы.{warn}")
    try:
        await msg.delete()  # команду из чата убираем
    except Exception:
        pass
    asyncio.create_task(_delete_later(bot, msg.chat.id, m.message_id, 1))
    for admin_id in settings.admin_ids:
        await _tell_admin_once(bot, admin_id, f"✅ Группа «{esc(msg.chat.title)}» подключена "
                                              f"(ID <code>{msg.chat.id}</code>).{warn}")


@router.message(Command("connect"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_connect_denied(msg: Message):
    log.info("/connect от не-админа в %s (from=%s)", msg.chat.id, msg.from_user and msg.from_user.id)


@router.my_chat_member(F.chat.type.in_(CHATS), F.from_user.id.in_(settings.admin_ids))
async def on_bot_added(event: ChatMemberUpdated, bot: Bot):
    """Админ добавил бота в группу или сделал админом — запоминаем группу сами, без /connect."""
    if event.new_chat_member.status not in ("member", "administrator"):
        return
    first = not settings.group_chat_id
    if first or event.chat.id == settings.group_chat_id:
        set_home_group(event.chat.id)
        warn = await _check_rights(bot, event.chat.id)
        await _tell_admin_once(bot, event.from_user.id,
                               f"✅ Я в группе «{esc(event.chat.title)}» и запомнил её "
                               f"(ID <code>{event.chat.id}</code>).{warn}\n"
                               "Чтобы я представился участникам — тегните меня там: «прошу любить и жаловать @…».")
    else:
        await _tell_admin_once(bot, event.from_user.id,
                               f"Меня добавили в «{esc(event.chat.title)}», но основная группа уже другая. "
                               "Чтобы переключиться — напишите в новой группе /connect.")


# ======================= знакомство: админ тегает бота в группе =======================
async def bot_mentioned(msg: Message, bot: Bot) -> bool:
    """Фильтр: в сообщении упомянут бот (@username / text_mention) или это ответ на сообщение бота."""
    me = await bot.me()
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == me.id:
        return True
    text = msg.text or msg.caption or ""
    for ent in msg.entities or msg.caption_entities or []:
        if ent.type == "mention" and ent.extract_from(text).lower() == f"@{me.username}".lower():
            return True
        if ent.type == "text_mention" and ent.user and ent.user.id == me.id:
            return True
    return False


def intro_text(admin_name: str, username: str = "") -> str:
    who = f'<a href="https://t.me/{username}">@{username}</a>' if username else f"<b>{esc(settings.bot_name)}</b>"
    return (
        f"👋 Всем привет! Я — <b>{esc(settings.bot_name)}</b> ({who}), маленький помощник {esc(admin_name)}.\n\n"
        "Вот что я умею:\n"
        "📊 покажу, сколько осталось подписки, трафика и какие устройства подключены;\n"
        "🖼 выдам QR-код, чтобы подключиться за секунду;\n"
        "💳 приму чек об оплате и передам админу на проверку;\n"
        "⏰ напомню за сутки и за 12 часов, что подписка заканчивается;\n"
        "📢 буду предупреждать о техработах и рассказывать об акциях.\n\n"
        "Уже пользуетесь сервисом? Нажмите «🔗 Привязать подписку» и пришлите мне ссылку из приложения — "
        "это полминуты. Новенькие — жмите «🔑 Получить доступ».\n\n"
        + (f"🏆 <b>Первые {settings.first_promo_limit}</b>, кто привяжет свою подписку, получат "
           f"<b>+{settings.first_promo_days} дн.</b> в подарок!\n\n" if promo_slots_left() > 0 else "")
        + f"👉 Пишите мне: {who}\n"
        "Прошу любить и жаловать 😏"
    )


async def intro_kb(bot: Bot):
    me = await bot.me()
    return kb([Btn(text=f"💬 Написать мне — @{me.username}", url=f"https://t.me/{me.username}")],
              [Btn(text="🔗 Привязать подписку", url=f"https://t.me/{me.username}?start=link")],
              [Btn(text="🔑 Получить доступ", url=f"https://t.me/{me.username}?start=group")])


async def post_intro(bot: Bot, chat_id: int, boss: str, reply_to: int | None = None) -> Message:
    """Представление бота: его аватарка + текст с кликабельным @username + кнопки. Старое представление убирается.

    В канале посты всегда идут от имени канала, поэтому «лицо» бота показываем фотографией в самом посте.
    """
    me = await bot.me()
    old = db.kv_get("intro_msg")
    if old and old.startswith(f"{chat_id}:"):
        old_id = int(old.split(":", 1)[1])
        for call in (bot.unpin_chat_message(chat_id, message_id=old_id), bot.delete_message(chat_id, old_id)):
            try:
                await call
            except Exception:
                pass
    text, markup = intro_text(boss, me.username), await intro_kb(bot)
    kw = {"reply_markup": markup}
    if reply_to:
        kw["reply_to_message_id"] = reply_to
    photo = None
    try:
        photos = await bot.get_user_profile_photos(me.id, limit=1)
        if photos.total_count:
            photo = photos.photos[0][-1].file_id
    except Exception:
        pass
    m = (await bot.send_photo(chat_id, photo, caption=text, **kw) if photo
         else await bot.send_message(chat_id, text, **kw))
    db.kv_set("intro_msg", f"{chat_id}:{m.message_id}")
    db.kv_set("introduced", str(chat_id))
    try:
        await bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
    except Exception as e:
        log.info("Не закрепил представление: %s", e)
        for admin_id in settings.admin_ids:
            await _tell_admin_once(bot, admin_id, "⚠️ Не смог закрепить представление — дайте мне право закреплять "
                                                  "(в канале это «Редактирование сообщений»).")
    return m


ADMIN_PINGS = ["На связи, админ! 🤖", "Тут я, тут 😏", "Слушаю и повинуюсь 🫡", "Бот на связи 👀"]


@router.message(F.chat.type.in_({"group", "supergroup"}), from_group_admin, bot_mentioned)
async def on_admin_mention(msg: Message, bot: Bot):
    if not settings.group_chat_id:
        # Первая группа, где админ представил бота, становится «домашней» — без правки .env
        settings.group_chat_id = msg.chat.id
        db.kv_set("group_chat_id", str(msg.chat.id))
    if msg.chat.id != settings.group_chat_id:
        return await msg.reply("Я уже работаю в другой группе 🙃 Поменять её можно в .env (GROUP_CHAT_ID).")

    if db.kv_get("introduced") != str(msg.chat.id):
        boss = (msg.from_user.first_name if msg.from_user and msg.from_user.id in settings.admin_ids else "админа этой группы")
        await post_intro(bot, msg.chat.id, boss, reply_to=msg.message_id)
        await _tell_admin_once(bot, settings.admin_ids[0],
                               f"✅ Я представился в «{esc(msg.chat.title)}» и запомнил эту группу "
                               f"(ID <code>{msg.chat.id}</code>). Сюда буду публиковать объявления и техработы.")
        return
    await msg.reply(random.choice(ADMIN_PINGS))


@router.channel_post(bot_mentioned)
async def on_channel_mention(post: Message, bot: Bot):
    """Знакомство в канале: админ публикует пост с @ботом — бот отвечает представлением и закрепляет его."""
    if not settings.group_chat_id:
        set_home_group(post.chat.id)
    if post.chat.id != settings.group_chat_id:
        return
    if db.kv_get("introduced") == str(post.chat.id):
        return  # в канале на повторные упоминания не отвечаем — не мусорим ленту
    await post_intro(bot, post.chat.id, post.author_signature or "нашего канала", reply_to=post.message_id)
    for admin_id in settings.admin_ids:
        await _tell_admin_once(bot, admin_id, f"✅ Я представился в канале «{esc(post.chat.title)}» и запомнил его.")


@router.channel_post(Command("intro"))
@router.message(Command("intro"), F.chat.type.in_({"group", "supergroup"}), from_group_admin)
async def cmd_intro(msg: Message, bot: Bot):
    """Переопубликовать представление (старое бот сам открепит и удалит)."""
    if settings.group_chat_id and msg.chat.id != settings.group_chat_id:
        return
    if not settings.group_chat_id:
        set_home_group(msg.chat.id)
    try:
        await msg.delete()
    except Exception:
        pass
    boss = (msg.author_signature or "нашего канала") if msg.chat.type == "channel" else (
        msg.from_user.first_name if msg.from_user and msg.from_user.id in settings.admin_ids else "админа этой группы")
    await post_intro(bot, msg.chat.id, boss)


@router.message(F.chat.type.in_({"group", "supergroup"}), bot_mentioned)
async def on_user_mention(msg: Message, bot: Bot):
    if settings.group_chat_id and msg.chat.id != settings.group_chat_id:
        return
    m = await msg.reply(f"🤖 Я {esc(settings.bot_name)}. Всё про вашу подписку — у меня в личке:",
                        reply_markup=await intro_kb(bot))
    asyncio.create_task(_delete_later(bot, msg.chat.id, m.message_id, 10))


async def _tell_admin_once(bot: Bot, admin_id: int, text: str) -> None:
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        pass  # админ ещё не открывал бота в личке


# ======================= публикация =======================
async def send_post(bot: Bot, chat_id: int, post: dict, reply_markup=None, disable_notification=False) -> Message:
    header = HEADERS.get(post["kind"], "")
    body = post.get("html") or ""
    text = f"{header}\n\n{body}".strip() if header else body
    kw = {"reply_markup": reply_markup, "disable_notification": disable_notification}
    media = post.get("media")
    if media == "photo":
        return await bot.send_photo(chat_id, post["file_id"], caption=text or None, **kw)
    if media == "video":
        return await bot.send_video(chat_id, post["file_id"], caption=text or None, **kw)
    if media == "animation":
        return await bot.send_animation(chat_id, post["file_id"], caption=text or None, **kw)
    return await bot.send_message(chat_id, text, **kw)


async def _post_kb(bot: Bot, post: dict, public: bool = False):
    """public=True — пост в канал/группу: там он идёт от имени канала, поэтому даём кнопку «кому писать»."""
    me = await bot.me()
    rows = []
    if post["kind"] == "promo":
        rows.append([Btn(text="💳 Воспользоваться акцией", url=f"https://t.me/{me.username}?start=promo")])
    if public:
        rows.append([Btn(text=f"🤖 Вопросы и подписка — @{me.username}", url=f"https://t.me/{me.username}")])
    return kb(*rows) if rows else None


async def broadcast(bot: Bot, post: dict) -> tuple[int, int]:
    """Рассылка в личку всем пользователям панели, у которых есть Telegram."""
    ok = fail = 0
    seen = set()
    markup = await _post_kb(bot, post)
    async for user in api.iter_users():
        tg = tg_of(user)
        if not tg or tg in seen:
            continue
        seen.add(tg)
        try:
            await send_post(bot, tg, post, reply_markup=markup)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/с — в пределах лимитов Telegram
    return ok, fail


def schedule_delete(chat_id: int, message_id: int, hours: float | None = None) -> None:
    """Сиюминутный пост (техработы и т.п.) — удалить через N часов. Telegram даёт боту удалять
    только посты моложе 48 ч, поэтому срок не больше 46 ч."""
    h = min(hours if hours is not None else settings.channel_ephemeral_hours, 46)
    db.kv_set(f"autodel:{chat_id}:{message_id}", str(int(time.time() + h * 3600)))


async def pin_quietly(bot: Bot, chat_id: int, message_id: int) -> None:
    """Закрепить без уведомления и сразу убрать служебное «… закрепил(а) сообщение»."""
    await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
    try:
        await bot.delete_message(chat_id, message_id + 1)  # служебное сообщение о закрепе идёт следующим
    except Exception:
        pass


async def autodelete_once(bot: Bot) -> None:
    now = time.time()
    for key, due in db.conn.execute("SELECT key, value FROM kv WHERE key LIKE 'autodel:%'").fetchall():
        if int(due) > now:
            continue
        _, chat, mid = key.split(":")
        try:
            await bot.delete_message(int(chat), int(mid))
        except Exception as e:
            log.info("Автоудаление %s не удалось: %s", key, e)
        db.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        db.conn.commit()


async def announce_works(bot: Bot, text: str) -> int | None:
    """Публикует и закрепляет объявление о техработах; возвращает message_id. Пост удалится сам."""
    if not settings.group_chat_id:
        return None
    try:
        post = {"kind": "works", "html": text}
        m = await send_post(bot, settings.group_chat_id, post, reply_markup=await _post_kb(bot, post, public=True))
        try:
            await pin_quietly(bot, settings.group_chat_id, m.message_id)
        except Exception as e:
            log.info("Не удалось закрепить: %s", e)
        schedule_delete(settings.group_chat_id, m.message_id)
        return m.message_id
    except Exception as e:
        log.warning("Не удалось объявить техработы: %s", e)
        return None


async def announce_works_done(bot: Bot, start_message_id: int | None, ok: bool) -> None:
    if not settings.group_chat_id or not start_message_id:
        return
    text = ("✅ <b>Технические работы завершены.</b> Всё работает в штатном режиме, спасибо за терпение!"
            if ok else "⚠️ <b>Работы завершены частично.</b> Если не подключается — напишите администратору.")
    try:
        done = await bot.send_message(settings.group_chat_id, text, reply_to_message_id=start_message_id)
        schedule_delete(settings.group_chat_id, done.message_id)
        await bot.unpin_chat_message(settings.group_chat_id, message_id=start_message_id)
    except Exception as e:
        log.info("Итог техработ не опубликован: %s", e)


# ======================= конструктор объявлений (админ, личка) =======================
admin_only = F.from_user.id.in_(settings.admin_ids)


@router.callback_query(AdminMenu.filter(F.action == "announce"), admin_only)
async def ann_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    group = (f"Канал/группа: <code>{settings.group_chat_id}</code>" if settings.group_chat_id else
             "⚠️ Группа не задана (GROUP_CHAT_ID) — доступна только рассылка в личку.\n"
             "Напишите /chatid в группе, чтобы узнать её ID.")
    await cb.message.answer(f"📢 <b>Новое объявление</b>\n{group}\n\nВыберите тип:", reply_markup=kb(
        [Btn(text=t, callback_data=AnnCb(action="kind", kind=k).pack()) for k, t in list(KIND_TITLES.items())[:2]],
        [Btn(text=t, callback_data=AnnCb(action="kind", kind=k).pack()) for k, t in list(KIND_TITLES.items())[2:]],
        [Btn(text="🔗 Пост «Привяжите подписку к Telegram»", callback_data=AnnCb(action="linkpost").pack())],
        [Btn(text="⬅️ Меню", callback_data=AdminMenu(action="menu").pack())],
    ))


LINK_POST = (
    "🔗 <b>Привяжите подписку к Telegram</b>\n\n"
    "Чтобы видеть в боте срок подписки, трафик и устройства, получать QR-код, напоминания об оплате "
    "и отправлять чеки — привяжите свой аккаунт:\n\n"
    "1. Нажмите кнопку ниже — откроется бот.\n"
    "2. Скопируйте ссылку подписки из приложения и отправьте её боту.\n"
    "3. Готово — бот сам найдёт ваш аккаунт.\n\n"
    "<i>Ссылку видит только бот, в чат она не попадает.</i>"
)


@router.callback_query(AnnCb.filter(F.action == "linkpost"), admin_only)
async def ann_linkpost(cb: CallbackQuery, bot: Bot):
    if not settings.group_chat_id:
        return await cb.answer("Сначала задайте GROUP_CHAT_ID в .env", show_alert=True)
    await cb.answer()
    try:
        m = await bot.send_message(settings.group_chat_id, LINK_POST,
                                   reply_markup=await bot_link_kb(bot, "🔗 Привязать подписку", "link"))
        try:
            await bot.pin_chat_message(settings.group_chat_id, m.message_id)
            note = "📌 Опубликовано и закреплено в группе."
        except Exception as e:
            note = f"✅ Опубликовано, но не закрепилось: {esc(e)}"
    except Exception as e:
        note = f"💥 Не удалось опубликовать: {esc(e)}"
    await cb.message.answer(note, reply_markup=back_to_admin())


@router.callback_query(AnnCb.filter(F.action == "kind"), admin_only)
async def ann_kind(cb: CallbackQuery, callback_data: AnnCb, state: FSMContext):
    await cb.answer()
    await state.set_state(AnnStates.content)
    await state.update_data(kind=callback_data.kind)
    hint = {
        "works": "Например: «Сегодня с 03:00 до 03:30 МСК обновляем серверы, возможны кратковременные обрывы».",
        "promo": "Например: «До конца недели 90 дней по цене 60!». Под постом будет кнопка перехода в бота.",
    }.get(callback_data.kind, "")
    await cb.message.answer(f"{KIND_TITLES[callback_data.kind]}\nПришлите текст объявления (можно с фото, "
                            f"видео или GIF; форматирование Telegram сохранится)."
                            + (f"\n<i>{hint}</i>" if hint else ""),
                            reply_markup=cancel_kb())


@router.message(AnnStates.content, admin_only, F.chat.type == "private")
async def ann_content(msg: Message, state: FSMContext, bot: Bot):
    post = {"kind": (await state.get_data())["kind"], "html": msg.html_text if (msg.text or msg.caption) else ""}
    if msg.photo:
        post.update(media="photo", file_id=msg.photo[-1].file_id)
    elif msg.video:
        post.update(media="video", file_id=msg.video.file_id)
    elif msg.animation:
        post.update(media="animation", file_id=msg.animation.file_id)
    elif not msg.text:
        return await msg.answer("Поддерживаются текст, фото, видео и GIF.", reply_markup=cancel_kb())
    if len(post["html"]) > (1000 if post.get("media") else 4000):
        return await msg.answer("Слишком длинно для Telegram — сократите текст.", reply_markup=cancel_kb())
    await state.update_data(post=post)

    await msg.answer("👀 Предпросмотр:")
    await send_post(bot, msg.chat.id, post, reply_markup=await _post_kb(bot, post, public=True))
    text, markup = await _destinations(bot)
    await msg.answer(text, reply_markup=markup)


async def _destinations(bot: Bot) -> tuple[str, object]:
    """Текст и кнопки «Куда публикуем?». Без подключённой группы — подсказка, как её подключить."""
    def p(t, grp, pin, dm):
        return Btn(text=t, callback_data=AnnCb(action="publish", grp=grp, pin=pin, dm=dm).pack())
    rows = []
    text = "Куда публикуем?"
    if settings.group_chat_id:
        rows += [[p("📢 Опубликовать", 1, 0, 0), p("📌 Опубликовать + закрепить", 1, 1, 0)],
                 [p("📌 Опубликовать + закрепить + в личку всем", 1, 1, 1)]]
    else:
        me = await bot.me()
        text = ("Куда публикуем?\n\n⚠️ <b>Группа ещё не подключена.</b> Нажмите «➕ Подключить группу» и выберите "
                "свою группу — или, если я уже там, напишите в группе <code>/connect</code>. "
                "Потом нажмите «🔄 Обновить» — черновик сохранится.")
        rows.append([Btn(text="➕ Подключить группу",
                         url=f"https://t.me/{me.username}?startgroup=connect&admin=pin_messages+delete_messages")])
        rows.append([Btn(text="🔄 Обновить", callback_data=AnnCb(action="dests").pack())])
    rows.append([p("📨 Только в личку всем подписчикам", 0, 0, 1)])
    rows.append([Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())])
    return text, kb(*rows)


@router.callback_query(AnnCb.filter(F.action == "dests"), admin_only)
async def ann_refresh_dests(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not (await state.get_data()).get("post"):
        return await cb.answer("Черновик устарел, начните заново", show_alert=True)
    if not settings.group_chat_id:
        return await cb.answer("Группа всё ещё не подключена: добавьте меня в группу или напишите там /connect",
                               show_alert=True)
    await cb.answer("Группа подключена ✅")
    text, markup = await _destinations(bot)
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(AnnCb.filter(F.action == "publish"), admin_only)
async def ann_publish(cb: CallbackQuery, callback_data: AnnCb, state: FSMContext, bot: Bot):
    post = (await state.get_data()).get("post")
    if not post:
        return await cb.answer("Черновик устарел, начните заново", show_alert=True)
    await state.clear()
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    report = []
    if callback_data.grp and settings.group_chat_id:
        try:
            m = await send_post(bot, settings.group_chat_id, post, reply_markup=await _post_kb(bot, post, public=True))
            report.append("✅ Опубликовано")
            if not callback_data.pin:  # правило канала: всё, что не закреплено, — сиюминутное и удаляется само
                schedule_delete(settings.group_chat_id, m.message_id)
                report.append(f"🧹 Удалится само через {min(settings.channel_ephemeral_hours, 46)} ч "
                              f"(закреплённые посты остаются навсегда)")
            if callback_data.pin:
                try:
                    await pin_quietly(bot, settings.group_chat_id, m.message_id)
                    report.append("📌 Закреплено")
                except Exception as e:
                    report.append(f"⚠️ Не закрепилось: {esc(e)}")
        except Exception as e:
            report.append(f"💥 Не опубликовано: {esc(e)}")
    if callback_data.dm:
        progress = await cb.message.answer("📨 Рассылаю в личку…")
        try:
            ok, fail = await broadcast(bot, post)
            report.append(f"📨 В личку: доставлено {ok}, не доставлено {fail} (заблокировали бота / не писали ему)")
        except Exception as e:
            report.append(f"💥 Рассылка прервана: {esc(e)}")
        await progress.delete()
    await cb.message.answer("\n".join(report) or "Ничего не отправлено.", reply_markup=back_to_admin())
