"""Тонкий асинхронный клиент Remnawave API (бэкенд 3.x).

Все ответы панели обёрнуты в {"response": ...}; клиент возвращает уже содержимое response.
Пользователь в 3.x идентифицируется числовым `id` (в путях /api/users/{id}).
"""
import ssl
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx


class RemnawaveError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class Remnawave:
    def __init__(self, base_url: str, token: str, cookie: str = "", client_cert: str = "", client_key: str = ""):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if base_url.startswith("http://"):
            # Прямое обращение к бэкенду (127.0.0.1:3000) без reverse proxy:
            # Remnawave требует эти заголовки, иначе отклоняет запросы
            headers["X-Forwarded-For"] = "127.0.0.1"
            headers["X-Forwarded-Proto"] = "https"
        cookies = {}
        if cookie and "=" in cookie:
            # Секретная cookie, если панель закрыта reverse proxy (вход по ?ключ=значение)
            name, value = cookie.split("=", 1)
            cookies[name.strip()] = value.strip()
        verify: ssl.SSLContext | bool = True
        if client_cert and client_key:
            # Панель пускает только по клиентскому сертификату (mTLS) — предъявляем свой
            verify = ssl.create_default_context()
            verify.load_cert_chain(client_cert, client_key)
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, cookies=cookies, timeout=httpx.Timeout(20.0), verify=verify,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs) -> Any:
        try:
            res = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise RemnawaveError(0, f"нет связи с панелью ({e.__class__.__name__})") from e
        if res.status_code >= 400:
            try:
                msg = res.json().get("message") or res.text
            except ValueError:
                msg = res.text
            if res.status_code == 403:
                msg = f"у API-токена нет прав на {method} {path} — выдайте их в настройках токена в панели"
            raise RemnawaveError(res.status_code, str(msg)[:300])
        if not res.content:
            return None
        data = res.json()
        return data.get("response", data) if isinstance(data, dict) else data

    # ---------- пользователи ----------
    async def get_user_by_username(self, username: str) -> dict | None:
        try:
            return await self._call("GET", f"/api/users/by-username/{seg(username)}")
        except RemnawaveError as e:
            if e.status in (400, 404):
                return None
            raise

    async def get_user_by_short_uuid(self, short_uuid: str) -> dict | None:
        try:
            return await self._call("GET", f"/api/users/by-short-uuid/{seg(short_uuid)}")
        except RemnawaveError as e:
            if e.status in (400, 404):
                return None
            raise

    async def get_user(self, user_id: int) -> dict | None:
        try:
            return await self._call("GET", f"/api/users/{int(user_id)}")
        except RemnawaveError as e:
            if e.status in (400, 404):
                return None
            raise

    async def iter_users(self, page_size: int = 500):
        start = 0
        while True:
            data = await self._call("GET", "/api/users", params={"start": start, "size": page_size})
            users = data.get("users", [])
            for u in users:
                yield u
            start += len(users)
            if not users or start >= data.get("total", 0):
                break

    async def create_user(self, *, username: str, days: int, telegram_id: int | None,
                          traffic_gb: int, devices: int | None, squads: list[str],
                          description: str | None = None) -> dict:
        body: dict[str, Any] = {
            "username": username,
            # 0 дней — аккаунт «до оплаты»: панель не принимает дату в прошлом, ставим +10 минут
            "expireAt": iso(datetime.now(timezone.utc) + (timedelta(days=days) if days > 0 else timedelta(minutes=10))),
            "trafficLimitBytes": gb_to_bytes(traffic_gb),
            "trafficLimitStrategy": "MONTH" if traffic_gb else "NO_RESET",
            "telegramId": telegram_id,
            "activeInternalSquads": squads,
        }
        if devices is not None:
            body["hwidDeviceLimit"] = devices
        if description:
            body["description"] = description
        return await self._call("POST", "/api/users", json=body)

    async def update_user(self, user_id: int, **fields) -> dict:
        return await self._call("PATCH", "/api/users", json={"id": user_id, **fields})

    async def add_days(self, user: dict, days: int) -> dict:
        """Сдвигает срок на `days` (может быть отрицательным).

        Если подписка уже истекла и дни добавляются — отсчёт идёт от текущего момента.
        """
        now = datetime.now(timezone.utc)
        current = parse_dt(user.get("expireAt")) or now
        base = max(current, now) if days > 0 else current
        new_exp = base + timedelta(days=days)
        if new_exp <= now:
            raise RemnawaveError(400, "после вычитания дата окончания оказывается в прошлом — "
                                      "используйте отключение пользователя")
        fields: dict[str, Any] = {"expireAt": iso(new_exp)}
        if user.get("status") in ("EXPIRED", "DISABLED", "LIMITED") and days > 0:
            fields["status"] = "ACTIVE"
        return await self.update_user(user["id"], **fields)

    async def delete_user(self, user_id: int) -> None:
        await self._call("DELETE", f"/api/users/{int(user_id)}")

    async def enable_user(self, user_id: int) -> dict:
        return await self._call("POST", f"/api/users/{int(user_id)}/actions/enable")

    async def disable_user(self, user_id: int) -> dict:
        return await self._call("POST", f"/api/users/{int(user_id)}/actions/disable")

    async def reset_traffic(self, user_id: int) -> dict:
        return await self._call("POST", f"/api/users/{int(user_id)}/actions/reset-traffic")

    async def get_devices(self, user_id: int) -> dict:
        return await self._call("GET", f"/api/hwid/devices/{int(user_id)}")

    # ---------- сквады ----------
    async def get_squads(self) -> list[dict]:
        data = await self._call("GET", "/api/internal-squads")
        return data.get("internalSquads", [])

    # ---------- ноды ----------
    async def get_nodes(self) -> list[dict]:
        data = await self._call("GET", "/api/nodes")
        return data if isinstance(data, list) else data.get("nodes", [])

    async def restart_node(self, uuid: str) -> None:
        await self._call("POST", f"/api/nodes/{seg(uuid)}/actions/restart")

    async def restart_all_nodes(self, force: bool = False) -> None:
        await self._call("POST", "/api/nodes/actions/restart-all", json={"forceRestart": force})

    async def get_system_stats(self) -> dict:
        return await self._call("GET", "/api/system/stats")


# ---------- утилиты ----------
def seg(value: str) -> str:
    """Безопасный сегмент пути: «../», «/», «?» и прочее экранируются — запрос не уйдёт на другой адрес API."""
    # точки тоже кодируем: сегмент «..» иначе нормализуется и поднимет запрос на уровень выше
    return quote(str(value), safe="").replace(".", "%2E")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gb_to_bytes(gb: float) -> int:
    return int(gb * 1024 ** 3)


def human_bytes(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} ТБ"
