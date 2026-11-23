"""Настройки бота. Всё читается из файла .env (см. .env.example)."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"В .env не задана обязательная переменная {name}")
    return value


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


@dataclass
class ServerCfg:
    """Linux-сервер, которым бот управляет по SSH (или локально)."""
    name: str
    role: str  # "panel", "node" или "bot" (сервер самого бота: только Linux, без Remnawave)
    host: str = ""
    port: int = 22
    user: str = "root"
    key_path: str = ""
    local: bool = False  # True — команды выполняются на этой же машине без SSH
    compose_dirs: list[str] = field(default_factory=list)


@dataclass
class Settings:
    bot_token: str
    admin_ids: list[int]
    panel_url: str
    api_token: str
    panel_cookie: str
    panel_client_cert: str  # клиентский сертификат для входа в панель (mTLS), пусто — не предъявляем
    panel_client_key: str
    db_path: Path
    default_days: int
    ref_trial_days: int
    ref_paid_bonus_days: int   # обоим, когда приглашённый впервые оплатил месяц
    ref_min_paid_days: int     # сколько дней в одобренном чеке считается «оплатой месяца»
    bday_gift_days: int
    first_promo_limit: int  # сколько первых верифицированных получают бонус (0 — акция выключена)
    first_promo_days: int
    paid_promo_limit: int   # бонус за ПЕРВЫЙ одобренный чек через бота: сколько человек максимум (0 — без лимита)
    paid_promo_days: int    # сколько дней дарить за первую оплату через бота (0 — бонус выключен)
    loyal_tiers: tuple      # бонус за оплаты подряд: ((месяцев, дней), ...), напр. 3:7,6:14; пусто — выключено
    loyal_grace_hours: int  # сколько часов после окончания чек ещё считается «вовремя»
    big_promo_limit: int    # «первые N, оплатившие надолго» получают подарок (0 — акция выключена)
    big_promo_min_days: int # от скольких дней в одобренном чеке (124 = от 4 месяцев)
    big_promo_days: int     # сколько дней в подарок
    pay_bank: str           # реквизиты для оплаты (СБП)
    pay_phone: str
    pay_name: str
    pay_price: str
    pay_note: str           # доп. правило оплаты (например «без комментария»)
    price_old: int          # цена за 31 день сейчас (и навсегда — для закреплённых нынешних клиентов)
    price_new: int          # цена после индексации
    price_new_from: str     # дата (МСК) с которой действует новая цена, YYYY-MM-DD
    price_lock_cutoff: str  # «нынешние клиенты» — аккаунт создан не позже этой даты (МСК)
    price_raise: bool       # PRICE_RAISE=0 — повышения не будет (например, если до конца года набрали 30 платных)
    channel_ephemeral_hours: int  # через сколько часов удалять из канала посты о техработах (макс. 46)
    default_traffic_gb: int
    default_devices: int
    default_squads: list[str]
    username_prefix: str
    support_text: str
    servers: list[ServerCfg]
    ssh_known_hosts: str | None
    group_chat_id: int | None  # может быть заполнен автоматически при знакомстве в группе
    bot_name: str
    welcome_text: str
    welcome_delete_after: int  # минуты, 0 — не удалять
    remind_hours: list[int]    # за сколько часов до конца напоминать (днём, 10–21 МСК)
    remind_after_hours: int    # одно догоняющее напоминание через N часов после окончания (0 — не напоминать)
    chat_autoclean_hours: int  # через сколько часов чистить переписку пользователей, 0 — не чистить


def _load_servers() -> list[ServerCfg]:
    path = BASE_DIR / "servers.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = []
    for s in raw:
        role = s.get("role", "node")
        default_dirs = {"panel": ["/opt/remnawave", "/opt/remnawave/subscription"],
                        "node": ["/opt/remnanode"], "bot": []}
        dirs = s.get("compose_dirs", default_dirs.get(role, []))
        servers.append(ServerCfg(
            name=s["name"], role=role, host=s.get("host", ""), port=int(s.get("port", 22)),
            user=s.get("user", "root"), key_path=os.path.expanduser(s.get("key_path", "")),
            local=bool(s.get("local", False)), compose_dirs=dirs,
        ))
    return servers


def load_settings() -> Settings:
    known_hosts = os.getenv("SSH_KNOWN_HOSTS", "").strip()
    return Settings(
        bot_token=_req("BOT_TOKEN"),
        admin_ids=_int_list(_req("ADMIN_IDS")),
        panel_url=_req("PANEL_URL").rstrip("/"),
        api_token=_req("API_TOKEN"),
        panel_cookie=os.getenv("PANEL_COOKIE", "").strip(),
        panel_client_cert=os.getenv("PANEL_CLIENT_CERT", "").strip(),
        panel_client_key=os.getenv("PANEL_CLIENT_KEY", "").strip(),
        db_path=BASE_DIR / os.getenv("DB_PATH", "bot.db"),
        default_days=int(os.getenv("DEFAULT_DAYS", "31")),
        ref_trial_days=int(os.getenv("REF_TRIAL_DAYS", "7")),
        ref_paid_bonus_days=int(os.getenv("REF_PAID_BONUS_DAYS", "14")),
        ref_min_paid_days=int(os.getenv("REF_MIN_PAID_DAYS", "28")),
        bday_gift_days=int(os.getenv("BDAY_GIFT_DAYS", "14")),
        first_promo_limit=int(os.getenv("FIRST_PROMO_LIMIT", "20")),
        first_promo_days=int(os.getenv("FIRST_PROMO_DAYS", "7")),
        paid_promo_limit=int(os.getenv("PAID_PROMO_LIMIT", "0")),
        paid_promo_days=int(os.getenv("PAID_PROMO_DAYS", "7")),
        loyal_tiers=tuple(sorted((int(m), int(d)) for m, d in
                                 (x.split(":") for x in os.getenv("LOYAL_TIERS", "3:7,6:14").replace(" ", "").split(",") if x))),
        loyal_grace_hours=int(os.getenv("LOYAL_GRACE_HOURS", "72")),
        big_promo_limit=int(os.getenv("BIG_PROMO_LIMIT", "0")),
        big_promo_min_days=int(os.getenv("BIG_PROMO_MIN_DAYS", "124")),
        big_promo_days=int(os.getenv("BIG_PROMO_DAYS", "31")),
        pay_bank=os.getenv("PAY_BANK", "").strip(),
        pay_phone=os.getenv("PAY_PHONE", "").strip(),
        pay_name=os.getenv("PAY_NAME", "").strip(),
        pay_price=os.getenv("PAY_PRICE", "").strip(),
        pay_note=os.getenv("PAY_NOTE", "").replace("\\n", "\n").strip(),
        price_old=int(os.getenv("PRICE_OLD", "200")),
        price_new=int(os.getenv("PRICE_NEW", "250")),
        price_new_from=os.getenv("PRICE_NEW_FROM", "2099-01-01"),
        price_lock_cutoff=os.getenv("PRICE_LOCK_CUTOFF", "2098-12-31"),
        price_raise=os.getenv("PRICE_RAISE", "0").strip() not in ("0", "false", "no", ""),
        channel_ephemeral_hours=int(os.getenv("CHANNEL_EPHEMERAL_HOURS", "24")),
        default_traffic_gb=int(os.getenv("DEFAULT_TRAFFIC_GB", "0")),
        default_devices=int(os.getenv("DEFAULT_DEVICES", "3")),
        default_squads=[s for s in os.getenv("DEFAULT_SQUAD_UUIDS", "").replace(" ", "").split(",") if s],
        username_prefix=os.getenv("USERNAME_PREFIX", "tg_"),
        support_text=os.getenv("SUPPORT_TEXT", "Оплатите подписку и отправьте PDF-чек через кнопку ниже."),
        servers=_load_servers(),
        ssh_known_hosts=os.path.expanduser(known_hosts) if known_hosts else None,
        group_chat_id=int(os.getenv("GROUP_CHAT_ID")) if os.getenv("GROUP_CHAT_ID", "").strip() else None,
        bot_name=os.getenv("BOT_NAME", "Бот").strip() or "Бот",
        welcome_text=os.getenv("WELCOME_TEXT", "").replace("\\n", "\n") or (
            "👋 Привет, {name}!\n"
            "Здесь мы публикуем новости и акции и предупреждаем о технических работах.\n"
            "Уже есть подписка — привяжи её. Нужен доступ — жми кнопку ниже."),
        welcome_delete_after=int(os.getenv("WELCOME_DELETE_AFTER", "10")),
        remind_hours=sorted({h for h in _int_list(os.getenv("REMIND_HOURS", "72,24,6")) if h > 0}, reverse=True),
        remind_after_hours=max(0, int(os.getenv("REMIND_AFTER_HOURS", "72"))),
        # Telegram позволяет боту удалять сообщения только моложе 48 часов
        chat_autoclean_hours=min(int(os.getenv("CHAT_AUTOCLEAN_HOURS", "24")), 47),
    )


settings = load_settings()
