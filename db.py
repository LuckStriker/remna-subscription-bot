"""Локальная SQLite-база: привязки Telegram → панель, чеки, состояние напоминаний.

Запросы крошечные, поэтому используется обычный sqlite3 без отдельного async-драйвера.
"""
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    tg_id     INTEGER PRIMARY KEY,
    panel_id  INTEGER NOT NULL,
    username  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL,
    tg_name     TEXT,
    file_id     TEXT NOT NULL,
    file_name   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    days        INTEGER,
    decided_by  INTEGER,
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER
);
CREATE TABLE IF NOT EXISTS reminders (
    tg_id     INTEGER PRIMARY KEY,
    expire_at TEXT NOT NULL,
    stage     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS access_requests (
    tg_id      INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS first_promo (
    tg_id      INTEGER PRIMARY KEY,
    panel_id   INTEGER NOT NULL UNIQUE,   -- один аккаунт панели — один бонус, даже если перепривязали Telegram
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS new_subs (    -- новые подписчики (заведены ботом по заявке), а не старые клиенты
    tg_id      INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paid_promo (  -- «первые N новых, оплативших» — один раз на человека
    tg_id      INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS birthdays (
    tg_id          INTEGER PRIMARY KEY,
    day            INTEGER,            -- NULL = пользователь отказался говорить
    month          INTEGER,
    set_at         INTEGER NOT NULL,
    last_gift_year INTEGER
);
CREATE TABLE IF NOT EXISTS referrals (
    invitee_id  INTEGER PRIMARY KEY,   -- один человек может быть приглашён только один раз
    referrer_id INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS chat_msgs (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(reminders)")}
        if "msg_id" not in cols:  # миграция со старой схемы
            self.conn.execute("ALTER TABLE reminders ADD COLUMN msg_id INTEGER")
        ref_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(referrals)")}
        if "paid_bonus_at" not in ref_cols:
            self.conn.execute("ALTER TABLE referrals ADD COLUMN paid_bonus_at INTEGER")
        req_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(access_requests)")}
        for col, typ in (("tg_name", "TEXT"), ("username", "TEXT"), ("referrer_id", "INTEGER")):
            if col not in req_cols:
                self.conn.execute(f"ALTER TABLE access_requests ADD COLUMN {col} {typ}")  # имена — константы
        pay_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(payments)")}
        if "file_unique" not in pay_cols:  # отпечаток файла чека — ловим повторную отправку того же PDF
            self.conn.execute("ALTER TABLE payments ADD COLUMN file_unique TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS payments_file_unique ON payments(file_unique)")
        for col, typ in (("sbp_id", "TEXT"), ("op_ts", "INTEGER")):  # из автопроверки чека
            if col not in pay_cols:
                self.conn.execute(f"ALTER TABLE payments ADD COLUMN {col} {typ}")  # имена — константы
        self.conn.execute("CREATE INDEX IF NOT EXISTS payments_sbp_id ON payments(sbp_id)")
        self.conn.commit()

    # ---- заявки на доступ с выбранным именем и пригласившим ----
    def save_request(self, tg_id: int, tg_name: str, username: str, referrer_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO access_requests(tg_id, created_at, tg_name, username, referrer_id) VALUES(?,?,?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET created_at=excluded.created_at, tg_name=excluded.tg_name, "
            "username=excluded.username, referrer_id=excluded.referrer_id",
            (tg_id, int(time.time()), tg_name, username, referrer_id),
        )
        self.conn.commit()

    def get_request(self, tg_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM access_requests WHERE tg_id=?", (tg_id,)).fetchone()

    def last_request_at(self, tg_id: int) -> int:
        row = self.get_request(tg_id)
        return row["created_at"] if row else 0

    def requested_username_taken(self, username: str, tg_id: int) -> bool:
        """Имя уже занято чужой ожидающей заявкой (регистр не важен)."""
        return self.conn.execute(
            "SELECT 1 FROM access_requests WHERE lower(username)=lower(?) AND tg_id<>?", (username, tg_id)
        ).fetchone() is not None

    def close_request(self, tg_id: int) -> None:
        # created_at оставляем — он работает как антиспам-таймер повторных заявок
        self.conn.execute("UPDATE access_requests SET username=NULL, referrer_id=NULL WHERE tg_id=?", (tg_id,))
        self.conn.commit()

    # ---- акция «первые N верифицированных» ----
    def claim_first_promo(self, tg_id: int, panel_id: int, limit: int) -> int | None:
        """Атомарно занимает место в акции. Возвращает номер места (1..limit) или None (мест нет / уже получал)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO first_promo(tg_id, panel_id, created_at) "
            "SELECT ?, ?, ? WHERE (SELECT COUNT(*) FROM first_promo) < ?",
            (tg_id, panel_id, int(time.time()), limit))
        self.conn.commit()
        if cur.rowcount != 1:
            return None
        return self.conn.execute("SELECT COUNT(*) FROM first_promo").fetchone()[0]

    def unclaim_first_promo(self, tg_id: int) -> None:
        self.conn.execute("DELETE FROM first_promo WHERE tg_id=?", (tg_id,))
        self.conn.commit()

    def first_promo_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM first_promo").fetchone()[0]

    # ---- новые подписчики и акция «первые N оплативших» ----
    def mark_new_sub(self, tg_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO new_subs(tg_id, created_at) VALUES(?,?)", (tg_id, int(time.time())))
        self.conn.commit()

    def is_new_sub(self, tg_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM new_subs WHERE tg_id=?", (tg_id,)).fetchone() is not None

    def claim_paid_promo(self, tg_id: int, limit: int) -> int | None:
        """Атомарно: номер по счёту или None (уже получал / мест нет). limit <= 0 — без лимита."""
        if limit <= 0:
            cur = self.conn.execute("INSERT OR IGNORE INTO paid_promo(tg_id, created_at) VALUES(?, ?)",
                                    (tg_id, int(time.time())))
        else:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO paid_promo(tg_id, created_at) SELECT ?, ? WHERE (SELECT COUNT(*) FROM paid_promo) < ?",
                (tg_id, int(time.time()), limit))
        self.conn.commit()
        if cur.rowcount != 1:
            return None
        return self.conn.execute("SELECT COUNT(*) FROM paid_promo").fetchone()[0]

    def unclaim_paid_promo(self, tg_id: int) -> None:
        self.conn.execute("DELETE FROM paid_promo WHERE tg_id=?", (tg_id,))
        self.conn.commit()

    def paid_promo_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM paid_promo").fetchone()[0]

    def has_paid_promo(self, tg_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM paid_promo WHERE tg_id=?", (tg_id,)).fetchone() is not None

    def approved_payments_count(self, tg_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM payments WHERE tg_id=? AND status='approved'",
                                 (tg_id,)).fetchone()[0]

    def linked_tg_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT tg_id FROM links")]

    # ---- дни рождения ----
    def get_birthday(self, tg_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM birthdays WHERE tg_id=?", (tg_id,)).fetchone()

    def set_birthday(self, tg_id: int, day: int | None, month: int | None, force: bool = False) -> bool:
        """Пользователь задаёт ДР один раз (иначе можно «переносить» его ради подарка). force — для админа."""
        if force:
            self.conn.execute(
                "INSERT INTO birthdays(tg_id, day, month, set_at) VALUES(?,?,?,?) "
                "ON CONFLICT(tg_id) DO UPDATE SET day=excluded.day, month=excluded.month",
                (tg_id, day, month, int(time.time())))
            self.conn.commit()
            return True
        if day is None:  # «не скажу» — только если ещё ничего не записано
            cur = self.conn.execute("INSERT OR IGNORE INTO birthdays(tg_id, day, month, set_at) VALUES(?,?,?,?)",
                                    (tg_id, None, None, int(time.time())))
        else:  # дату можно указать один раз; кто раньше отказался — может передумать (set_at обновится → 30 дн.)
            cur = self.conn.execute(
                "INSERT INTO birthdays(tg_id, day, month, set_at) VALUES(?,?,?,?) "
                "ON CONFLICT(tg_id) DO UPDATE SET day=excluded.day, month=excluded.month, set_at=excluded.set_at "
                "WHERE birthdays.day IS NULL",
                (tg_id, day, month, int(time.time())))
        self.conn.commit()
        return cur.rowcount == 1

    def birthdays_on(self, day: int, month: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM birthdays WHERE day=? AND month=?", (day, month)).fetchall()

    def mark_birthday_gift(self, tg_id: int, year: int) -> bool:
        """Атомарно: True только при первом вызове за год — подарок не задублируется."""
        cur = self.conn.execute(
            "UPDATE birthdays SET last_gift_year=? WHERE tg_id=? AND (last_gift_year IS NULL OR last_gift_year<?)",
            (year, tg_id, year))
        self.conn.commit()
        return cur.rowcount == 1

    # ---- рефералы ----
    def add_referral(self, invitee_id: int, referrer_id: int) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO referrals(invitee_id, referrer_id, created_at) VALUES(?,?,?)",
                                (invitee_id, referrer_id, int(time.time())))
        self.conn.commit()
        return cur.rowcount == 1

    def get_referrer_of(self, invitee_id: int) -> int | None:
        row = self.conn.execute("SELECT referrer_id FROM referrals WHERE invitee_id=?", (invitee_id,)).fetchone()
        return row["referrer_id"] if row else None

    def claim_paid_bonus(self, invitee_id: int) -> int | None:
        """Атомарно помечает, что за этого друга бонус выдан. Возвращает id пригласившего или None (уже выдан/не реферал)."""
        row = self.conn.execute("SELECT referrer_id FROM referrals WHERE invitee_id=? AND paid_bonus_at IS NULL",
                                (invitee_id,)).fetchone()
        if not row:
            return None
        cur = self.conn.execute("UPDATE referrals SET paid_bonus_at=? WHERE invitee_id=? AND paid_bonus_at IS NULL",
                                (int(time.time()), invitee_id))
        self.conn.commit()
        return row["referrer_id"] if cur.rowcount == 1 else None

    def paid_referral_count(self, referrer_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND paid_bonus_at IS NOT NULL",
                                 (referrer_id,)).fetchone()[0]

    def referral_count(self, referrer_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (referrer_id,)).fetchone()[0]

    # ---- простые настройки, которые бот запоминает сам ----
    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv(key, value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    # ---- история сообщений в личке (для очистки чата) ----
    def track_msg(self, chat_id: int, message_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO chat_msgs(chat_id, message_id, ts) VALUES(?,?,?)",
                          (chat_id, message_id, int(time.time())))
        self.conn.commit()

    def untrack_msg(self, chat_id: int, message_id: int) -> None:
        self.conn.execute("DELETE FROM chat_msgs WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        self.conn.commit()

    def take_msgs(self, chat_id: int | None = None, older_than: int = 0,
                  exclude_chats: list[int] | None = None) -> dict[int, list[int]]:
        """Забирает (и удаляет из учёта) сообщения: {chat_id: [message_id, …]}."""
        q, args = "SELECT chat_id, message_id FROM chat_msgs WHERE ts <= ?", [int(time.time()) - older_than]
        if chat_id is not None:
            q += " AND chat_id=?"
            args.append(chat_id)
        for ex in exclude_chats or []:
            q += " AND chat_id<>?"
            args.append(ex)
        rows = self.conn.execute(q, args).fetchall()
        out: dict[int, list[int]] = {}
        for r in rows:
            out.setdefault(r["chat_id"], []).append(r["message_id"])
        if rows:
            self.conn.executemany("DELETE FROM chat_msgs WHERE chat_id=? AND message_id=?",
                                  [(r["chat_id"], r["message_id"]) for r in rows])
            self.conn.commit()
        return out

    # ---- привязки ----
    def get_link(self, tg_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM links WHERE tg_id=?", (tg_id,)).fetchone()

    def set_link(self, tg_id: int, panel_id: int, username: str) -> None:
        self.conn.execute(
            "INSERT INTO links(tg_id, panel_id, username) VALUES(?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET panel_id=excluded.panel_id, username=excluded.username",
            (tg_id, panel_id, username),
        )
        self.conn.commit()

    def drop_link(self, tg_id: int) -> None:
        self.conn.execute("DELETE FROM links WHERE tg_id=?", (tg_id,))
        self.conn.commit()

    def forget_user(self, tg_id: int | None, panel_id: int) -> None:
        """После удаления клиента из панели: убрать привязку, напоминания, ДР, заявки.
        Записи акции и рефералов НЕ трогаем — иначе можно переподключиться и получить бонусы заново."""
        self.conn.execute("DELETE FROM links WHERE panel_id=?", (panel_id,))
        self.conn.execute("DELETE FROM reminders WHERE tg_id=?", (-int(panel_id),))
        if tg_id:
            for sql in ("DELETE FROM links WHERE tg_id=?", "DELETE FROM reminders WHERE tg_id=?",
                        "DELETE FROM birthdays WHERE tg_id=?", "DELETE FROM access_requests WHERE tg_id=?",
                        "DELETE FROM kv WHERE key=?"):
                self.conn.execute(sql, (f"ref:{tg_id}",) if "kv" in sql else (tg_id,))
        self.conn.commit()

    # ---- чеки ----
    def add_payment(self, tg_id: int, tg_name: str, file_id: str, file_name: str | None,
                    file_unique: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO payments(tg_id, tg_name, file_id, file_name, created_at, file_unique) VALUES(?,?,?,?,?,?)",
            (tg_id, tg_name, file_id, file_name, int(time.time()), file_unique),
        )
        self.conn.commit()
        return cur.lastrowid

    def payment_by_sbp(self, sbp_id: str, exclude_pid: int) -> int | None:
        """Чек с той же операцией СБП (повтор оплаты даже в другом файле), кроме отклонённых."""
        row = self.conn.execute("SELECT id FROM payments WHERE sbp_id=? AND id<>? AND status<>'rejected' ORDER BY id",
                                (sbp_id, exclude_pid)).fetchone()
        return row["id"] if row else None

    def set_payment_check(self, pid: int, sbp_id: str | None, op_ts: float | None) -> None:
        self.conn.execute("UPDATE payments SET sbp_id=?, op_ts=? WHERE id=?",
                          (sbp_id, int(op_ts) if op_ts else None, pid))
        self.conn.commit()

    def same_file_payments(self, file_unique: str | None, exclude_pid: int) -> list[sqlite3.Row]:
        """Чеки с тем же файлом (повторная отправка одного PDF — от того же или другого человека)."""
        if not file_unique:
            return []
        return self.conn.execute(
            "SELECT id, tg_id, status, created_at FROM payments WHERE file_unique=? AND id<>? ORDER BY id",
            (file_unique, exclude_pid)).fetchall()

    def get_payment(self, pid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()

    def pending_payments(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id").fetchall()

    def user_has_pending(self, tg_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM payments WHERE tg_id=? AND status='pending'", (tg_id,)
        ).fetchone() is not None

    def decide_payment(self, pid: int, status: str, admin_id: int, days: int | None = None) -> bool:
        """Атомарно меняет статус; False — если чек уже кто-то обработал."""
        cur = self.conn.execute(
            "UPDATE payments SET status=?, days=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'",
            (status, days, admin_id, int(time.time()), pid),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def reopen_payment(self, pid: int) -> None:
        self.conn.execute("UPDATE payments SET status='pending', days=NULL, decided_by=NULL WHERE id=?", (pid,))
        self.conn.commit()

    # ---- напоминания ----
    def reminder_stage(self, tg_id: int, expire_at: str) -> int:
        row = self.conn.execute("SELECT * FROM reminders WHERE tg_id=?", (tg_id,)).fetchone()
        if not row or row["expire_at"] != expire_at:
            return 0  # срок поменялся (продлили) — начинаем цикл напоминаний заново
        return row["stage"]

    def reminder_row(self, tg_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM reminders WHERE tg_id=?", (tg_id,)).fetchone()

    def reminder_msg(self, tg_id: int) -> int | None:
        row = self.conn.execute("SELECT msg_id FROM reminders WHERE tg_id=?", (tg_id,)).fetchone()
        return row["msg_id"] if row else None

    def set_reminder_stage(self, tg_id: int, expire_at: str, stage: int, msg_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO reminders(tg_id, expire_at, stage, msg_id) VALUES(?,?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET expire_at=excluded.expire_at, stage=excluded.stage, "
            "msg_id=excluded.msg_id",
            (tg_id, expire_at, stage, msg_id),
        )
        self.conn.commit()

    # ---- заявки на доступ (антиспам) ----
    def can_request_access(self, tg_id: int, cooldown: int = 3600) -> bool:
        return time.time() - self.last_request_at(tg_id) >= cooldown
