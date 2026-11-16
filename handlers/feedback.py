"""Опрос «как тебе бот»: админ рассылает одним нажатием, клиент отвечает одной кнопкой.

Варианты: 👍 помог · 👎 не помог · ❤️ спасибо за бота · ✍️ другое.
• «Другое» — это свой текст: ответ засчитывается, только когда клиент его прислал.
• «Не помог» — бот просит пару слов (можно пропустить).
• Каждый текст приходит админу с кнопками «✅ Решено» и «💬 Ответить и закрыть» —
  клиент получает уведомление, что его вопрос решён (и комментарий админа).
"""
import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as Btn

from config import settings
from core import db, esc, find_user
from keyboards import AdminMenu, back_to_admin, cancel_kb, kb, user_menu

router = Router()
log = logging.getLogger(__name__)
is_admin = F.from_user.id.in_(settings.admin_ids)

ANSWERS = {
    "ok": "👍 Бот помог",
    "bad": "👎 Бот не помог",
    "thx": "❤️ Спасибо за бота",
    "other": "✍️ Другое",
}
ASK_TEXT = {"bad": "Жаль 😔 Напиши в двух словах, что не получилось — исправим.",
            "other": "✍️ Напиши своими словами, что хочешь сказать о боте — замечание, идея, вопрос. "
                     "Я передам напрямую админу."}

db.conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
    tg_id   INTEGER NOT NULL,
    poll    INTEGER NOT NULL,
    answer  TEXT NOT NULL,
    comment TEXT,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (tg_id, poll)
)""")
_cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(feedback)")}
for _col in ("status", "reply"):  # status: NULL — без текста, open — ждёт решения, done — решено
    if _col not in _cols:
        db.conn.execute(f"ALTER TABLE feedback ADD COLUMN {_col} TEXT")
db.conn.commit()


class FbCb(CallbackData, prefix="fb"):
    action: str        # ans / send / cancel / done / reply
    poll: int = 0
    ans: str = ""
    tg: int = 0


class FbStates(StatesGroup):
    comment = State()


class FbAdmin(StatesGroup):
    reply = State()


def _current_poll() -> int:
    return int(db.kv_get("fb_poll") or 0)


def poll_kb(poll: int):
    b = lambda key: Btn(text=ANSWERS[key], callback_data=FbCb(action="ans", poll=poll, ans=key).pack())  # noqa: E731
    return kb([b("ok"), b("bad")], [b("thx"), b("other")])


POLL_TEXT = ("💬 <b>Как тебе бот?</b>\n"
             "Ответь одной кнопкой — это займёт секунду и поможет сделать его удобнее 🙏")


def _stats(poll: int) -> str:
    rows = db.conn.execute("SELECT answer, COUNT(*) FROM feedback WHERE poll=? GROUP BY answer", (poll,)).fetchall()
    counts = {a: n for a, n in rows}
    total = sum(counts.values())
    sent = int(db.kv_get(f"fb_sent:{poll}") or 0)
    lines = [f"📊 <b>Опрос #{poll}</b> — ответили {total} из {sent}"]
    lines += [f"{ANSWERS[k]}: <b>{counts.get(k, 0)}</b>" for k in ANSWERS]
    comments = db.conn.execute(
        "SELECT f.comment, f.status, l.username FROM feedback f LEFT JOIN links l ON l.tg_id=f.tg_id "
        "WHERE f.poll=? AND f.comment IS NOT NULL ORDER BY f.status='done', f.ts DESC LIMIT 20", (poll,)).fetchall()
    if comments:
        opened = sum(1 for _, s, _ in comments if s != "done")
        lines.append(f"\n<b>Тексты</b> (не решено: {opened}):")
        lines += [f"{'✅' if s == 'done' else '🟡'} <b>{esc(u or '—')}</b>: {esc(c[:300])}" for c, s, u in comments]
    return "\n".join(lines)


def _admin_kb(poll: int, tg: int):
    return kb([Btn(text="✅ Решено", callback_data=FbCb(action="done", poll=poll, tg=tg).pack()),
               Btn(text="💬 Ответить и закрыть", callback_data=FbCb(action="reply", poll=poll, tg=tg).pack())])


# ---------------- админ ----------------
@router.callback_query(AdminMenu.filter(F.action == "feedback"), is_admin)
async def fb_menu(cb: CallbackQuery):
    await cb.answer()
    linked = [t for t in db.linked_tg_ids() if t not in settings.admin_ids]
    poll = _current_poll()
    rows = [[Btn(text=f"📨 Разослать опрос ({len(linked)} чел.)", callback_data=FbCb(action="send").pack())]]
    head = _stats(poll) + "\n\n" if poll else ""
    rows.append([Btn(text="⬅️ Админ-меню", callback_data=AdminMenu(action="menu").pack())])
    await cb.message.answer(f"{head}Вот так увидят клиенты:\n\n{POLL_TEXT}\n\n"
                            f"<i>Получат все, кто привязал Telegram ({len(linked)} чел.). Отправить?</i>",
                            reply_markup=kb(*rows))


@router.message(Command("feedback"), is_admin, F.chat.type == "private")
async def fb_stats_cmd(msg: Message):
    poll = _current_poll()
    await msg.answer(_stats(poll) if poll else "Опросов ещё не было — запусти из админ-меню «💬 Опрос».")


@router.callback_query(FbCb.filter(F.action == "send"), is_admin)
async def fb_send(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    poll = _current_poll() + 1
    db.kv_set("fb_poll", str(poll))
    targets = [t for t in db.linked_tg_ids() if t not in settings.admin_ids]
    ok = 0
    for tg in targets:
        try:
            await bot.send_message(tg, POLL_TEXT, reply_markup=poll_kb(poll))
            ok += 1
        except Exception as e:
            log.info("Опрос %s не доставлен: %s", tg, e)
        await asyncio.sleep(0.05)
    db.kv_set(f"fb_sent:{poll}", str(ok))
    await cb.message.answer(f"📨 Опрос #{poll} отправлен: {ok} из {len(targets)}.\n"
                            f"Ответы буду присылать сюда, сводка — /feedback", reply_markup=back_to_admin())


def _row(tg: int, poll: int):
    return db.conn.execute("SELECT * FROM feedback WHERE tg_id=? AND poll=?", (tg, poll)).fetchone()


@router.callback_query(FbCb.filter(F.action == "done"), is_admin)
async def fb_done(cb: CallbackQuery, callback_data: FbCb, bot: Bot):
    row = _row(callback_data.tg, callback_data.poll)
    if not row or row["status"] == "done":
        return await cb.answer("Уже закрыто", show_alert=True)
    await _close(bot, callback_data.tg, callback_data.poll, None)
    await cb.answer("Закрыто ✅")
    await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Решено</b>")


@router.callback_query(FbCb.filter(F.action == "reply"), is_admin)
async def fb_reply_start(cb: CallbackQuery, callback_data: FbCb, state: FSMContext):
    row = _row(callback_data.tg, callback_data.poll)
    if not row or row["status"] == "done":
        return await cb.answer("Уже закрыто", show_alert=True)
    await cb.answer()
    await state.set_state(FbAdmin.reply)
    await state.update_data(tg=callback_data.tg, poll=callback_data.poll, src=cb.message.message_id,
                            src_text=cb.message.html_text)
    await cb.message.answer("Напиши ответ клиенту — он получит его вместе с отметкой «решено»:",
                            reply_markup=cancel_kb())


@router.message(FbAdmin.reply, is_admin, F.text, ~F.text.startswith("/"))
async def fb_reply_send(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    text = msg.text.strip()[:2000]
    await _close(bot, data["tg"], data["poll"], text)
    try:
        await bot.edit_message_text(data["src_text"] + f"\n\n✅ <b>Решено</b>, ответ: {esc(text)}",
                                    chat_id=msg.chat.id, message_id=data["src"])
    except Exception:
        pass
    await msg.answer("✅ Ответ отправлен, отзыв закрыт.")


async def _close(bot: Bot, tg: int, poll: int, reply: str | None) -> None:
    db.conn.execute("UPDATE feedback SET status='done', reply=? WHERE tg_id=? AND poll=?", (reply, tg, poll))
    db.conn.commit()
    text = "✅ <b>По твоему отзыву — решено.</b> Спасибо, что написал!"
    if reply:
        text += f"\n\n💬 {esc(reply)}"
    try:
        await bot.send_message(tg, text, reply_markup=user_menu())
    except Exception as e:
        log.info("Ответ на отзыв %s не доставлен: %s", tg, e)


# ---------------- клиент ----------------
async def _tell_admins(bot: Bot, text: str, markup=None) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception:
            pass


async def _who(tg_id: int, tg_user) -> str:
    user = await find_user(tg_id)
    acc = f"<b>{esc(user['username'])}</b>" if user else "—"
    return f"{acc} ({esc(tg_user.full_name)}, @{esc(tg_user.username or '—')})"


def _save(tg: int, poll: int, key: str, comment: str | None = None) -> None:
    db.conn.execute(
        "INSERT INTO feedback(tg_id, poll, answer, comment, status, ts) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(tg_id, poll) DO UPDATE SET answer=excluded.answer, comment=excluded.comment, "
        "status=excluded.status, reply=NULL, ts=excluded.ts",
        (tg, poll, key, comment, "open" if comment else None, int(time.time())))
    db.conn.commit()


@router.callback_query(FbCb.filter(F.action == "ans"))
async def fb_answer(cb: CallbackQuery, callback_data: FbCb, state: FSMContext, bot: Bot):
    key = callback_data.ans
    if key not in ANSWERS:
        return await cb.answer()
    if key == "other":  # засчитаем, когда придёт текст
        await cb.answer()
        await state.set_state(FbStates.comment)
        await state.update_data(poll=callback_data.poll, ans=key)
        return await cb.message.answer(ASK_TEXT[key], reply_markup=kb(
            [Btn(text="✖️ Передумал", callback_data=FbCb(action="cancel").pack())]))
    _save(cb.from_user.id, callback_data.poll, key)
    await cb.answer("Спасибо! 🙏")
    try:
        await cb.message.edit_text(f"{POLL_TEXT}\n\nТвой ответ: <b>{ANSWERS[key]}</b>")
    except Exception:
        pass
    if key == "bad":
        await state.set_state(FbStates.comment)
        await state.update_data(poll=callback_data.poll, ans=key)
        await cb.message.answer(ASK_TEXT[key], reply_markup=kb(
            [Btn(text="Пропустить", callback_data=FbCb(action="cancel").pack())]))
    else:
        await _tell_admins(bot, f"💬 Отзыв: <b>{ANSWERS[key]}</b> — {await _who(cb.from_user.id, cb.from_user)}")
        if key == "thx":
            await cb.message.answer("❤️ Тебе спасибо, что с нами! 🤝", reply_markup=user_menu())


@router.callback_query(FbCb.filter(F.action == "cancel"))
async def fb_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await cb.answer("Ок 🙂")
    if data.get("ans") == "bad":  # «не помог» без пояснения — всё равно сообщаем админу
        await _tell_admins(bot, f"💬 Отзыв: <b>{ANSWERS['bad']}</b> (без пояснения) — "
                                f"{await _who(cb.from_user.id, cb.from_user)}")
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.message(FbStates.comment, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def fb_comment(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    poll, key = data.get("poll", _current_poll()), data.get("ans", "other")
    text = msg.text.strip()[:1000]
    _save(msg.from_user.id, poll, key, text)
    await msg.answer("🙏 Спасибо, передал админу! Как решим — напишу.", reply_markup=user_menu())
    await _tell_admins(bot, f"{ANSWERS[key]} — {await _who(msg.from_user.id, msg.from_user)}:\n«{esc(text)}»",
                       _admin_kb(poll, msg.from_user.id))


@router.message(FbStates.comment, F.chat.type == "private")
async def fb_comment_not_text(msg: Message):
    await msg.answer("Напиши текстом 🙂", reply_markup=kb([Btn(text="✖️ Отмена",
                                                               callback_data=FbCb(action="cancel").pack())]))
