from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as KB


class UserMenu(CallbackData, prefix="u"):
    action: str  # profile / qr / devices / pay / request / menu


class AdminMenu(CallbackData, prefix="a"):
    action: str


class PayCb(CallbackData, prefix="pay"):
    action: str  # ok / custom / no
    pid: int
    days: int = 0


class ReqCb(CallbackData, prefix="req"):
    action: str  # ok / no / link / old
    tg_id: int
    days: int = 0


class GiftCb(CallbackData, prefix="gf"):
    action: str        # days / skip / scope / run
    days: int = 0
    scope: str = ""    # active / all
    notify: int = 0    # 0 — тихо, 1 — в личку, 2 — в личку и пост в группе


class LinkCb(CallbackData, prefix="lk"):
    tg_id: int
    uid: int


class UserCb(CallbackData, prefix="uc"):
    action: str  # add / custom / enable / disable / reset / qr / refresh
    uid: int
    days: int = 0


class SquadCb(CallbackData, prefix="sq"):
    idx: int  # -1 = готово


class NodeCb(CallbackData, prefix="n"):
    action: str  # list / restart / restart_all
    uuid: str = ""


class SrvCb(CallbackData, prefix="s"):
    action: str  # panel / nodes / os / reboot / status
    target: str = "all"  # индекс сервера или all
    confirm: int = 0  # 1 — выполнить, 2 — выполнить и объявить в группе


class AnnCb(CallbackData, prefix="ann"):
    action: str  # kind / publish / cancel
    kind: str = ""  # works / promo / news / plain
    grp: int = 0  # 1 — опубликовать в группе
    pin: int = 0  # 1 — закрепить в группе
    dm: int = 0   # 1 — разослать в личку всем подписчикам


def kb(*rows: list[Btn]) -> KB:
    return KB(inline_keyboard=list(rows))


def user_menu(registered: bool = True) -> KB:
    if not registered:
        return kb(
            [Btn(text="🔗 У меня уже есть подписка — привязать", callback_data=UserMenu(action="link").pack())],
            [Btn(text="🔑 Запросить доступ", callback_data=UserMenu(action="request").pack())],
            [Btn(text="🧹 Очистить чат", callback_data=UserMenu(action="clean").pack())],
        )
    return kb(
        [Btn(text="📊 Моя подписка", callback_data=UserMenu(action="profile").pack()),
         Btn(text="📲 Как подключиться", callback_data=UserMenu(action="guide").pack())],
        [Btn(text="🖼 QR-код", callback_data=UserMenu(action="qr").pack()),
         Btn(text="📱 Устройства", callback_data=UserMenu(action="devices").pack())],
        [Btn(text="💳 Оплатить / продлить", callback_data=UserMenu(action="pay").pack())],
        [Btn(text="🤝 Пригласить друга", callback_data=UserMenu(action="invite").pack()),
         Btn(text="🧹 Очистить чат", callback_data=UserMenu(action="clean").pack())],
    )


def back_to_user_menu() -> KB:
    return kb([Btn(text="⬅️ Меню", callback_data=UserMenu(action="menu").pack())])


def admin_menu() -> KB:
    a = lambda t, act: Btn(text=t, callback_data=AdminMenu(action=act).pack())  # noqa: E731
    return kb(
        [a("➕ Создать пользователя", "create"), a("📅 Добавить дни", "days")],
        [a("🔎 Найти пользователя", "find"), a("🧾 Чеки", "payments")],
        [a("🩺 Всё ли ок? (состояние)", "health")],
        [a("🖥 Ноды", "nodes"), a("🛠 Серверы и обновления", "servers")],
        [a("📢 Объявление / акция", "announce"), a("🎁 Подарить дни всем", "gift")],
        [a("💬 Опрос: как вам бот", "feedback")],
        [a("👤 Мой профиль", "me"), a("🧹 Очистить чат", "clean")],
    )


def back_to_admin() -> KB:
    return kb([Btn(text="⬅️ Админ-меню", callback_data=AdminMenu(action="menu").pack())])


def cancel_kb() -> KB:
    return kb([Btn(text="✖️ Отмена", callback_data=AdminMenu(action="cancel").pack())])


def payment_kb(pid: int) -> KB:
    p = lambda t, act, d=0: Btn(text=t, callback_data=PayCb(action=act, pid=pid, days=d).pack())  # noqa: E731
    return kb(
        [p("✅ +31 дн.", "ok", 31), p("✅ +62 дн.", "ok", 62), p("✅ +93 дн.", "ok", 93)],
        [p("💰 По сумме из чека", "amount"), p("✍️ Своё кол-во дней", "custom")],
        [p("❌ Отклонить", "no")],
    )


def access_request_kb(tg_id: int, referral_days: int = 0) -> KB:
    r = lambda t, act, d=0: Btn(text=t, callback_data=ReqCb(action=act, tg_id=tg_id, days=d).pack())  # noqa: E731
    first = ([r(f"✅ Да — {referral_days} дн. (по приглашению)", "ok", referral_days)] if referral_days
             else [r("✅ 7 дней (тест)", "ok", 7), r("✅ 31 день", "ok", 31)])
    return kb(
        first,
        [r("🔗 Это существующий клиент — привязать", "link")],
        [r("👴 Бывший клиент — с нуля, без бонусов, ждём чек", "old")],
        [r("❌ Нет", "no")],
    )


def user_card_kb(uid: int, status: str) -> KB:
    u = lambda t, act, d=0: Btn(text=t, callback_data=UserCb(action=act, uid=uid, days=d).pack())  # noqa: E731
    toggle = u("⛔ Отключить", "disable") if status != "DISABLED" else u("🟢 Включить", "enable")
    return kb(
        [u("+7 дн.", "add", 7), u("+31 дн.", "add", 31), u("+93 дн.", "add", 93)],
        [u("✍️ ± дни", "custom"), u("🔄 Сбросить трафик", "reset")],
        [toggle, u("🖼 QR", "qr"), u("♻️ Обновить", "refresh")],
        [u("🗑 Удалить", "delete")],
        [Btn(text="⬅️ Админ-меню", callback_data=AdminMenu(action="menu").pack())],
    )
