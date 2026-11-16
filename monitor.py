"""Сторож: пишет админу, только если проблема подтвердилась несколькими проверками подряд.

• Нода не на связи / панель не отвечает — проверка раз в 5 мин, тревога после 3 провалов подряд (~15 мин).
• Сертификат < 10 дней / диск > 90% — проверка по SSH раз в час, тревога после 2 провалов подряд.
• Важен факт: одно сообщение «упало», одно «восстановилось». Без повторов и без подсчёта длительности.
• Ночью (00–09 МСК) будят только нода и панель; сертификат и диск ждут утра.
Состояние хранится в базе бота (kv) — перезапуск бота не вызывает повторных тревог.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton as Btn

from config import settings
from core import api, db, esc
from handlers.servers import run_on
from keyboards import NodeCb, kb

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
FAST_INTERVAL = 300        # ноды и панель
SLOW_INTERVAL = 3600       # диск и сертификаты по SSH
FAST_CONFIRM = 3           # провалов подряд до тревоги
SLOW_CONFIRM = 2
DISK_LIMIT = 90            # %
CERT_DAYS = 10             # cron продлевает за 30 дней — меньше 10 значит, что продление сломалось
QUIET = range(0, 9)        # часы МСК, когда не будим из-за несрочного

SSH_CHECK = (
    "df -P / | awk 'NR==2 {gsub(\"%\",\"\",$5); print \"disk \" $5}'; "
    "for f in /etc/letsencrypt/live/*/fullchain.pem; do [ -f \"$f\" ] || continue; "
    "e=$(openssl x509 -enddate -noout -in \"$f\" | cut -d= -f2); "
    "echo \"cert $(basename \"$(dirname \"$f\")\") $(( ($(date -d \"$e\" +%s) - $(date +%s)) / 86400 ))\"; done"
)

_KV = "monitor_state"


def _load() -> dict:
    try:
        return json.loads(db.kv_get(_KV) or "{}")
    except ValueError:
        return {}


def _save(state: dict) -> None:
    db.kv_set(_KV, json.dumps(state, ensure_ascii=False))


async def _send(bot: Bot, text: str, markup=None) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as e:
            log.info("Админ %s не получил тревогу: %s", admin_id, e)


class Watch:
    """Один наблюдаемый признак (нода, панель, диск сервера, сертификат)."""

    def __init__(self, bot: Bot, state: dict):
        self.bot, self.state, self.seen = bot, state, set()

    async def observe(self, key: str, bad: bool, *, title: str, detail: str = "", need: int,
                      urgent: bool, ok: str = "", markup=None) -> None:
        self.seen.add(key)
        now = time.time()
        s = self.state.get(key)
        if not bad:
            if s and s.get("alerted"):
                await _send(self.bot, f"✅ {ok or title} — снова в норме.")
            self.state.pop(key, None)
            return
        s = s or {"fails": 0, "since": now, "alerted": False, "last": 0}
        s["fails"] += 1
        self.state[key] = s
        quiet = datetime.now(MSK).hour in QUIET and not urgent
        if s["fails"] < need or quiet or s["alerted"]:  # важен факт: одна тревога, без повторов
            return
        await _send(self.bot, f"🔴 {title}" + (f"\n{detail}" if detail else ""), markup)
        s["alerted"], s["last"] = True, now

    async def forget_unseen(self, prefix: str) -> None:
        """Признак исчез (ноду удалили/выключили, сертификат убрали) — молча забываем."""
        for key in [k for k in self.state if k.startswith(prefix) and k not in self.seen]:
            self.state.pop(key)


async def _tcp_ok(host: str, port: int = 443, timeout: float = 5) -> bool:
    """Прямая проверка ноды, когда панель недоступна: открывается ли порт 443."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _plural_nodes(n: int) -> str:
    return "нода" if n % 10 == 1 and n % 100 != 11 else (
        "ноды" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "нод")


async def check_fast(w: Watch) -> None:
    """Панель и ноды — одним сводным сообщением: что случилось, сколько нод в строю, у кого пропал доступ."""
    st = w.state.setdefault("fast", {})       # key → {"fails", "down"(подтверждено), "reason"}
    meta = w.state.setdefault("nodes_meta", {})  # uuid → {name, address, online} — последнее, что знали
    w.seen.update({"fast", "nodes_meta"})
    for old in [k for k in w.state if k == "panel" or k.startswith("node:")]:  # ключи старого формата
        w.state.pop(old)

    try:
        nodes = await api.get_nodes()
        panel_err = ""
    except Exception as e:
        nodes, panel_err = None, f"{e.__class__.__name__}: {e}"[:200]

    status: dict[str, tuple[bool, str]] = {"panel": (nodes is not None, panel_err)}
    direct = nodes is None
    if nodes is not None:
        live = {}
        for n in nodes:
            if n.get("isDisabled"):
                continue
            up = bool(n.get("isConnected"))
            prev_online = meta.get(n["uuid"], {}).get("online", 0)
            live[n["uuid"]] = {"name": n.get("name") or "?", "address": n.get("address") or "",
                               # пока нода жива — помним, сколько на ней сидело; упала — оставляем прежнее
                               "online": int(n.get("usersOnline") or 0) if up else prev_online}
            status[f"node:{n['uuid']}"] = (up, n.get("lastStatusMessage") or "")
        meta.clear()
        meta.update(live)
    else:  # без панели проверяем ноды напрямую по 443
        res = await asyncio.gather(*(_tcp_ok(m["address"]) for m in meta.values()))
        for uuid, up in zip(meta, res):
            status[f"node:{uuid}"] = (up, "" if up else "порт 443 не отвечает")

    went_down, came_up = [], []
    for key, (up, reason) in status.items():
        s = st.setdefault(key, {"fails": 0, "down": False, "reason": ""})
        if up:
            if s["down"]:
                came_up.append(key)
            s.update(fails=0, down=False, reason="")
        else:
            s["fails"] += 1
            s["reason"] = reason or s["reason"]
            if s["fails"] >= FAST_CONFIRM and not s["down"]:
                s["down"] = True
                went_down.append(key)
    for key in [k for k in st if k not in status]:  # ноду удалили/выключили — забываем молча
        st.pop(key)
    if went_down or came_up:
        await _send(w.bot, _fast_report(st, meta, went_down, came_up, direct), _restart_kb(st, meta, direct))


def _fast_report(st: dict, meta: dict, went_down: list, came_up: list, direct: bool) -> str:
    def name(key):
        return "панель" if key == "panel" else meta.get(key[5:], {}).get("name", "?")

    node_keys = [f"node:{u}" for u in meta]
    down_nodes = [k for k in node_keys if st.get(k, {}).get("down")]
    panel_down = st.get("panel", {}).get("down", False)
    total = len(node_keys)

    lines = []
    if went_down:
        if panel_down and total and len(down_nodes) == total:
            head = "Упало всё: панель и все ноды"
        elif set(went_down) == {"panel"}:
            head = "Упал мейн — панель не отвечает"
        else:
            nd = [name(k) for k in went_down if k != "panel"]
            head = (f"Упала нода {nd[0]}" if len(nd) == 1 else f"Упали {len(nd)} {_plural_nodes(len(nd))}: "
                    + ", ".join(nd))
            if "panel" in went_down:
                head = "Упала панель; " + head[0].lower() + head[1:]
        lines.append(f"🔴 <b>{esc(head)}</b>")
    if came_up:
        lines.append("✅ <b>Восстановилось: " + esc(", ".join(name(k) for k in came_up)) + "</b>")
    if not panel_down and not down_nodes:
        lines.append(f"\n🟢 Всё в норме: панель работает, ноды {total} из {total}.")
        return "\n".join(lines)

    p = st.get("panel", {})
    lines.append("")
    lines.append("Панель: " + (f"❌ не отвечает — <code>{esc(p.get('reason', ''))}</code>" if panel_down
                                else "✅ работает"))
    lines.append(f"Ноды: в строю <b>{total - len(down_nodes)} из {total}</b>"
                 + (" <i>(проверено напрямую, панель недоступна)</i>" if direct else ""))
    lost = 0
    for k in node_keys:
        m = meta[k[5:]]
        if st.get(k, {}).get("down"):
            lost += m.get("online", 0)
            reason = st[k].get("reason")
            lines.append(f"❌ <b>{esc(m['name'])}</b> · {esc(m['address'])}"
                         + (f" · было онлайн {m['online']}" if m.get("online") else "")
                         + (f"\n      ⚠️ {esc(reason[:150])}" if reason else ""))
        else:
            lines.append(f"✅ {esc(m['name'])}" + (f" · онлайн {m['online']}" if m.get("online") and not direct else ""))
    lines.append("")
    if down_nodes:
        lines.append(f"👥 Без доступа сейчас: ~<b>{lost}</b> чел. (кто был на упавших нодах)"
                     + (" — приложения обычно сами переключаются на живую ноду." if len(down_nodes) < total else "."))
    if panel_down:
        lines.append("🧾 Пока панель лежит: не работают продления, чеки и новые подписки. "
                     "Уже подключённые клиенты доступ не теряют, если их нода жива.")
    return "\n".join(lines)


def _restart_kb(st: dict, meta: dict, direct: bool):
    if direct:  # панель лежит — перезапуск через неё не сработает
        return None
    rows = [[Btn(text=f"🔄 Перезапустить {meta[k[5:]]['name']}",
                 callback_data=NodeCb(action="restart", uuid=k[5:]).pack())]
            for k in st if k.startswith("node:") and st[k].get("down") and k[5:] in meta]
    return kb(*rows) if rows else None


async def check_slow(w: Watch) -> None:
    servers = settings.servers
    outs = await asyncio.gather(*(run_on(s, SSH_CHECK, timeout=60) for s in servers))
    for srv, (ok, out) in zip(servers, outs):
        if not ok:  # SSH не дошёл — недоступность ловят проверки нод/панели, здесь не шумим
            w.seen.update(k for k in w.state if k.startswith(f"disk:{srv.name}") or k.startswith(f"cert:{srv.name}:"))
            continue
        for line in out.splitlines():
            p = line.split()
            if len(p) == 2 and p[0] == "disk" and p[1].isdigit():
                used = int(p[1])
                await w.observe(f"disk:{srv.name}", used > DISK_LIMIT,
                                title=f"Диск на <b>{esc(srv.name)}</b> заполнен на {used}%",
                                detail="Почисти: <code>docker system prune -af</code>, логи в /var/log.",
                                ok=f"Диск на <b>{esc(srv.name)}</b> ({used}%)", need=SLOW_CONFIRM, urgent=False)
            elif len(p) == 3 and p[0] == "cert" and p[2].lstrip("-").isdigit():
                days = int(p[2])
                await w.observe(f"cert:{srv.name}:{p[1]}", days < CERT_DAYS,
                                title=f"Сертификат <b>{esc(p[1])}</b> на {esc(srv.name)}: "
                                      + (f"осталось {days} дн." if days >= 0 else "<b>истёк</b>"),
                                detail="Автопродление не сработало — проверь cron/certbot на этом сервере.",
                                ok=f"Сертификат <b>{esc(p[1])}</b> ({days} дн.)", need=SLOW_CONFIRM, urgent=days < 2)
    await w.forget_unseen("disk:")
    await w.forget_unseen("cert:")


STATUS_SSH = "echo \"up $(cat /proc/uptime | cut -d' ' -f1)\"; " + SSH_CHECK
# версия панели — из package.json внутри контейнера backend (есть только на сервере панели)
PANEL_VERSION_CMD = ("docker exec remnawave sh -c 'grep -m1 \"\\\"version\\\"\" package.json' 2>/dev/null "
                     "| sed -E 's/.*\"([0-9][^\"]*)\".*/\\1/'")


async def panel_version() -> str:
    srv = next((s for s in settings.servers if s.role == "panel"), None)
    if not srv:
        return ""
    ok, out = await run_on(srv, PANEL_VERSION_CMD, timeout=20)
    v = out.strip().splitlines()[-1].strip() if ok and out.strip() else ""
    return v if v[:1].isdigit() else ""


def _ago(sec: float) -> str:
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d} дн {h} ч" if d else (f"{h} ч {m} мин" if h else f"{m} мин")


async def status_report() -> str:
    """Состояние «прямо сейчас» — по кнопке админа: панель, ноды, аптайм, диски, сертификаты."""
    try:
        nodes, panel_err = await api.get_nodes(), ""
    except Exception as e:
        nodes, panel_err = None, f"{e.__class__.__name__}: {e}"[:200]
    servers = settings.servers
    outs, pver = await asyncio.gather(asyncio.gather(*(run_on(s, STATUS_SSH, timeout=25) for s in servers)),
                                      panel_version())

    lines = [f"🩺 <b>Состояние на {datetime.now(MSK):%d.%m %H:%M} МСК</b>\n"]
    lines.append("Панель: " + ("✅ работает" if nodes is not None else f"❌ не отвечает — <code>{esc(panel_err)}</code>")
                 + (f" · Remnawave <b>{esc(pver)}</b>" if pver else " · версия: не удалось узнать"))
    if nodes is not None:
        act = [n for n in nodes if not n.get("isDisabled")]
        up = [n for n in act if n.get("isConnected")]
        online = sum(int(n.get("usersOnline") or 0) for n in act)
        lines.append(f"Ноды: в строю <b>{len(up)} из {len(act)}</b> · онлайн всего <b>{online}</b>")
        for n in act:
            xu = n.get("xrayUptime")
            xu = f" · xray {_ago(float(xu))}" if xu not in (None, "", 0, "0") else ""
            ver = n.get("versions") or {}
            vv = f" · node {esc(ver.get('node', '?'))} / xray {esc(ver.get('xray', '?'))}" if ver else ""
            lines.append(f"{'✅' if n.get('isConnected') else '❌'} {esc(n.get('name'))} · онлайн "
                         f"{n.get('usersOnline', 0)}{xu}{vv}"
                         + ("" if n.get("isConnected") else f"\n      ⚠️ {esc((n.get('lastStatusMessage') or '')[:150])}"))
    lines.append("\n<b>Серверы</b> (аптайм · диск · сертификат):")
    for srv, (ok, out) in zip(servers, outs):
        if not ok:
            lines.append(f"❓ {esc(srv.name)} — SSH не ответил")
            continue
        up_s, disk, certs = None, None, []
        for line in out.splitlines():
            p = line.split()
            if len(p) == 2 and p[0] == "up":
                up_s = float(p[1])
            elif len(p) == 2 and p[0] == "disk" and p[1].isdigit():
                disk = int(p[1])
            elif len(p) == 3 and p[0] == "cert" and p[2].lstrip("-").isdigit():
                certs.append(int(p[2]))
        bad = (disk or 0) > DISK_LIMIT or (certs and min(certs) < CERT_DAYS)
        parts = [f"up {_ago(up_s)}" if up_s else "up ?", f"диск {disk}%" if disk is not None else "диск ?"]
        if certs:
            parts.append(f"серт. {min(certs)} дн.")
        lines.append(f"{'⚠️' if bad else '✅'} {esc(srv.name)} · " + " · ".join(parts))
    return "\n".join(lines)


async def monitor_loop(bot: Bot) -> None:
    await asyncio.sleep(60)  # даём боту спокойно стартовать
    last_slow = 0.0
    while True:
        state = _load()
        w = Watch(bot, state)
        jobs = [check_fast]
        if time.time() - last_slow >= SLOW_INTERVAL:
            jobs.append(check_slow)
        for job in jobs:
            try:
                await job(w)
                if job is check_slow:
                    last_slow = time.time()
            except Exception:
                log.exception("Ошибка сторожа %s", job.__name__)
                # признаки, которые не проверялись, не забываем
        _save(state)
        await asyncio.sleep(FAST_INTERVAL)
