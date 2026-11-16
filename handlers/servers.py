"""Обслуживание серверов по SSH: обновление Remnawave (панель/ноды), обновление Linux, перезагрузка, статус.

Список серверов — в servers.json (см. servers.example.json).
"""
import asyncio
import logging
import os
import shlex
import sys

import asyncssh
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton as Btn

from config import ServerCfg, settings
from core import esc
from handlers.group import announce_works, announce_works_done
from keyboards import AdminMenu, SrvCb, back_to_admin, kb

router = Router()
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))
log = logging.getLogger(__name__)

_lock = asyncio.Lock()  # одновременно идёт только одна операция обслуживания

OS_UPGRADE = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update -q && "
    "apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade && "
    # Образы некоторых хостеров помечают сетевые пакеты как «автоматические» — autoremove их сносит,
    # и после перезагрузки сервер остаётся без сети (реальный случай). Перед очисткой закрепляем их.
    # По одному: если пакета нет на сервере, apt-mark со списком не закрепит вообще ничего.
    "for p in iproute2 netplan.io ifupdown isc-dhcp-client openssh-server systemd; do "
    "dpkg -s $p >/dev/null 2>&1 && apt-mark manual $p >/dev/null; done; "
    "apt-get -y -q autoremove; "
    "if [ -f /var/run/reboot-required ]; then echo '>>> REBOOT REQUIRED'; fi"
)
STATUS = (
    "echo \"uptime: $(uptime -p)\"; "
    "free -h | awk '/Mem/ {print \"RAM: \"$3\" / \"$2}'; "
    "df -h / | awk 'NR==2 {print \"Disk: \"$3\" / \"$2\" (\"$5\")\"}'; "
    "if [ -f /var/run/reboot-required ]; then echo '⚠️ нужна перезагрузка'; fi; "
    "docker ps --format '{{.Names}}: {{.Status}}' 2>/dev/null | grep -i remna || true"
)
REBOOT = "nohup sh -c 'sleep 3; reboot' >/dev/null 2>&1 &"

WORKS_TEXT = {
    "remna": "Прямо сейчас обновляем серверы. Возможны кратковременные обрывы соединения "
             "на несколько минут. Сообщим, когда всё закончится.",
    "os": "Обновляем систему на серверах. Возможны кратковременные обрывы соединения.",
    "reboot": "Перезагружаем сервер. сервис будет недоступен 1–3 минуты.",
}

ACTIONS = {
    "status": "📋 Статус",
    "remna": "⬆️ Обновить Remnawave",
    "os": "🐧 Обновить Linux (apt upgrade)",
    "reboot": "🔁 Перезагрузить",
}


def compose_update(dirs: list[str]) -> str:
    """Официальная процедура из docs.rw: pull → down → up -d, затем чистка старых образов."""
    parts = []
    for d in dirs:
        q = shlex.quote(d)
        parts.append(
            f"if [ -f {q}/docker-compose.yml ] || [ -f {q}/compose.yml ]; then "
            f"echo '>>> {d}' && cd {q} && docker compose pull -q && docker compose down && "
            f"docker compose up -d || exit 1; "
            f"else echo '>>> {d}: нет compose-файла, пропуск'; fi"
        )
    return "; ".join(parts) + "; docker image prune -f >/dev/null; echo '>>> OK'"


def command_for(action: str, srv: ServerCfg) -> str:
    cmd = {"status": STATUS, "os": OS_UPGRADE, "reboot": REBOOT,
           "remna": compose_update(srv.compose_dirs)}[action]
    if srv.user != "root" and not srv.local:
        cmd = f"sudo -n bash -c {shlex.quote(cmd)}"
    return cmd


async def run_on(srv: ServerCfg, cmd: str, timeout: int = 1800) -> tuple[bool, str]:
    try:
        if srv.local:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            return proc.returncode == 0, out.decode(errors="replace")
        kw = {"port": srv.port, "username": srv.user}
        if srv.key_path:
            kw["client_keys"] = [srv.key_path]
        if settings.ssh_known_hosts:
            kw["known_hosts"] = settings.ssh_known_hosts
        async with asyncssh.connect(srv.host, **kw) as conn:
            res = await asyncio.wait_for(conn.run(cmd, check=False), timeout)
            return res.exit_status == 0, (res.stdout or "") + (res.stderr or "")
    except asyncio.TimeoutError:
        return False, f"таймаут {timeout} с"
    except (OSError, asyncssh.Error) as e:
        return False, f"{e.__class__.__name__}: {e}"


def _targets(target: str) -> list[ServerCfg]:
    if target == "all":
        return settings.servers
    if target in ("panel", "node"):
        return [s for s in settings.servers if s.role == target]
    i = int(target)
    return [settings.servers[i]] if 0 <= i < len(settings.servers) else []


def _target_name(target: str) -> str:
    return {"all": "все серверы", "panel": "панель", "node": "все ноды"}.get(
        target, settings.servers[int(target)].name if target.isdigit() and int(target) < len(settings.servers) else "?")


@router.callback_query(AdminMenu.filter(F.action == "servers"))
async def servers_menu(cb: CallbackQuery):
    await cb.answer()
    if not settings.servers:
        return await cb.message.answer("Серверы не настроены. Создайте <code>servers.json</code> "
                                       "по образцу <code>servers.example.json</code> и перезапустите бота.",
                                       reply_markup=back_to_admin())
    lines = ["<b>🛠 Серверы</b>"]
    for s in settings.servers:
        where = "локально" if s.local else f"{esc(s.user)}@{esc(s.host)}:{s.port}"
        lines.append(f"• <b>{esc(s.name)}</b> [{s.role}] — {where}")
    a = lambda t, act, tg="all": Btn(text=t, callback_data=SrvCb(action=act, target=tg).pack())  # noqa: E731
    await cb.message.answer("\n".join(lines), reply_markup=kb(
        [a("📋 Статус всех", "status")],
        [a("⬆️ Remnawave: панель + все ноды", "remna")],
        [a("⬆️ Только панель", "remna", "panel"), a("⬆️ Только ноды", "remna", "node")],
        [a("🐧 Обновить Linux на всех", "os")],
        [a("🔧 Выбрать сервер…", "pick")],
        [Btn(text="⬅️ Меню", callback_data=AdminMenu(action="menu").pack())],
    ))


@router.callback_query(SrvCb.filter(F.action == "pick"))
async def pick_server(cb: CallbackQuery):
    await cb.answer()
    rows = [[Btn(text=f"{s.name} [{s.role}]", callback_data=SrvCb(action="one", target=str(i)).pack())]
            for i, s in enumerate(settings.servers)]
    rows.append([Btn(text="⬅️ Назад", callback_data=AdminMenu(action="servers").pack())])
    await cb.message.answer("Выберите сервер:", reply_markup=kb(*rows))


@router.callback_query(SrvCb.filter(F.action == "one"))
async def one_server(cb: CallbackQuery, callback_data: SrvCb):
    await cb.answer()
    t = callback_data.target
    rows = [[Btn(text=title, callback_data=SrvCb(action=act, target=t).pack())] for act, title in ACTIONS.items()]
    rows.append([Btn(text="⬅️ Назад", callback_data=SrvCb(action="pick").pack())])
    await cb.message.answer(f"Сервер <b>{esc(_target_name(t))}</b>:", reply_markup=kb(*rows))


@router.callback_query(SrvCb.filter(F.action.in_(set(ACTIONS))))
async def run_action(cb: CallbackQuery, callback_data: SrvCb):
    action, target = callback_data.action, callback_data.target
    servers = _targets(target)
    if action == "remna":  # на сервере бота Remnawave нет — обновлять там нечего
        servers = [s for s in servers if s.role != "bot" and s.compose_dirs]
    if not servers:
        return await cb.answer("Нет подходящих серверов", show_alert=True)

    # Всё, кроме статуса, требует подтверждения
    if action != "status" and not callback_data.confirm:
        await cb.answer()
        names = ", ".join(esc(s.name) for s in servers)
        warn = ""
        if action == "reboot":
            warn = "\n⚠️ Если бот работает на этом же сервере, он перезапустится вместе с ним."
        if action == "remna":
            warn = "\nПорядок: сначала панель, потом ноды. Во время обновления соединение кратко прервётся."
        rows = [[Btn(text="✅ Выполнить", callback_data=SrvCb(action=action, target=target, confirm=1).pack())]]
        if settings.group_chat_id:
            rows.append([Btn(text="✅ Выполнить и объявить техработы в группе",
                             callback_data=SrvCb(action=action, target=target, confirm=2).pack())])
        rows.append([Btn(text="✖️ Отмена", callback_data=AdminMenu(action="servers").pack())])
        return await cb.message.answer(f"Подтвердите: <b>{ACTIONS[action]}</b>\nСерверы: {names}{warn}",
                                       reply_markup=kb(*rows))

    if _lock.locked():
        return await cb.answer("Уже выполняется другая операция, подождите", show_alert=True)
    await cb.answer()
    if callback_data.confirm:
        await cb.message.edit_reply_markup(reply_markup=None)

    async with _lock:
        progress = await cb.message.answer(f"⏳ {ACTIONS[action]}: {len(servers)} сервер(ов)…")
        works_msg = None
        if callback_data.confirm == 2:
            works_msg = await announce_works(cb.bot, WORKS_TEXT.get(action, WORKS_TEXT["remna"]))
        if action == "remna":
            # Панель обновляем первой (рекомендация Remnawave), ноды — параллельно после неё
            panels = [s for s in servers if s.role == "panel"]
            nodes = [s for s in servers if s.role != "panel"]
            results = [(s, *await run_on(s, command_for(action, s))) for s in panels]
            results += await _parallel(nodes, action)
        else:
            results = await _parallel(servers, action)

        ok = sum(1 for _, good, _ in results if good)
        if works_msg and action != "reboot":  # после reboot сервер ещё перезагружается — «готово» рано
            await announce_works_done(cb.bot, works_msg, ok == len(results))
        await progress.edit_text(f"{'✅' if ok == len(results) else '⚠️'} {ACTIONS[action]}: "
                                 f"успешно {ok} из {len(results)}")
        for srv, good, out in results:
            tail = "\n".join(out.strip().splitlines()[-25:]) or "(нет вывода)"
            await cb.message.answer(f"{'🟢' if good else '🔴'} <b>{esc(srv.name)}</b>\n"
                                    f"<pre>{esc(tail[-3500:])}</pre>")
        if action == "remna":
            await cb.message.answer(await _after_update_check())
        if action in ("remna", "os"):  # после любых обновлений — полная проверка всей цепочки сервиса
            wait = await cb.message.answer("🧪 Запускаю полную проверку сервиса…")
            await wait.edit_text(await _smoke_report())
        await cb.message.answer("Готово.", reply_markup=back_to_admin())


SMOKE = "/opt/infra/scripts/smoke_test.py"


async def _smoke_report(timeout: int = 300) -> str:
    """Полная проверка сервиса (infra/scripts/smoke_test.py): итог + всё, что не ✅."""
    if not os.path.exists(SMOKE):
        return "🧪 Полная проверка недоступна: нет smoke_test.py на сервере бота."
    try:
        p = await asyncio.create_subprocess_exec(sys.executable, SMOKE,  # тот же venv и рабочий каталог, что у бота
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return f"🧪 Полная проверка не уложилась в {timeout // 60} мин — проверь вручную."
    lines = out.decode(errors="replace").strip().splitlines()
    head = lines[0] if lines else "🧪 Проверка: нет вывода"
    problems = [ln for ln in lines[1:] if not ln.startswith("✅")]
    ok_count = sum(1 for ln in lines[1:] if ln.startswith("✅"))
    body = "\n".join(esc(ln) for ln in problems[:25])
    return f"<b>{esc(head)}</b>\n✅ пройдено: {ok_count}" + (f"\n{body}" if body else "")


async def _after_update_check(wait: int = 120) -> str:
    """После обновления Remnawave: дождаться, что панель отвечает и все ноды снова подключены; показать версии.
    «Не встало» видно сразу и конкретно — какая нода и что пишет панель."""
    from core import api  # noqa: PLC0415
    from monitor import panel_version  # noqa: PLC0415
    nodes, err = None, ""
    for _ in range(wait // 10):
        try:
            nodes = await api.get_nodes()
            if all(n.get("isConnected") or n.get("isDisabled") for n in nodes):
                break
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"[:150]
        await asyncio.sleep(10)
    pver = await panel_version()
    lines = ["🔎 <b>Проверка после обновления</b>",
             f"Панель: {'✅' if nodes is not None else '❌ не отвечает ' + esc(err)}"
             + (f" · Remnawave <b>{esc(pver)}</b>" if pver else " · версия: не удалось узнать")]
    for n in nodes or []:
        if n.get("isDisabled"):
            continue
        v = n.get("versions") or {}
        ok = n.get("isConnected")
        lines.append(f"{'✅' if ok else '❌'} {esc(n.get('name'))} · node {esc(v.get('node', '?'))} / "
                     f"xray {esc(v.get('xray', '?'))}"
                     + ("" if ok else f"\n      ⚠️ {esc((n.get('lastStatusMessage') or 'не подключилась за 2 мин')[:150])}"))
    return "\n".join(lines)


async def _parallel(servers: list[ServerCfg], action: str):
    outs = await asyncio.gather(*(run_on(s, command_for(action, s)) for s in servers))
    return [(s, good, out) for s, (good, out) in zip(servers, outs)]
