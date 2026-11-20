"""Помощник проверки чеков (PDF): дата и время операции, сумма, получатель, статус, номер операции СБП.

Разбор PDF идёт в ОТДЕЛЬНОМ процессе с лимитами памяти и времени (кривой или «бомбовый» PDF не уронит бота):
    python -I receipts.py <10 цифр нашего телефона>   ← PDF в stdin, JSON в stdout
Решение всё равно за админом: PDF можно отредактировать. Бот ловит бытовое — старый чек, повтор,
не тот номер, не та сумма, следы PDF-редактора.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
MONTHS = {m: i for i, m in enumerate(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(), 1)}
OK_WORDS = ("успешно", "выполнено", "операция выполнена", "исполнен", "зачислен")
BAD_WORDS = ("отклон", "отмен", "в обработке", "ошибка", "не выполн")
EDITORS = ("acrobat", "ilovepdf", "smallpdf", "pdfescape", "sejda", "foxit", "pdf-xchange", "pdfelement",
           "microsoft", "word", "libreoffice", "openoffice", "canva", "photoshop", "pdf24", "nitro", "ilove")
BANKS = (("яндекс банк", "Яндекс Банк"),
         ("tbank", "Т-Банк"), ("т-банк", "Т-Банк"), ("тинькофф", "Т-Банк"), ("втб", "ВТБ"),
         ("сбер", "Сбер"), ("альфа", "Альфа-Банк"), ("газпромбанк", "Газпромбанк"), ("райффайзен", "Райффайзен"))
DASHES = "‐‑‒–—−"


def _norm(text: str) -> str:
    for d in DASHES:
        text = text.replace(d, "-")
    return text.replace(" ", " ").replace(" ", " ")


def _op_time(text: str) -> float | None:
    """Дата и время операции (МСК) → unix-время. Берём дату со временем; приоритет — строка про операцию."""
    found = []
    for line in text.splitlines():
        for m in re.finditer(r"(\d{2})\.(\d{2})\.(\d{4})(?:,|\s+в)?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", line):
            d, mo, y, h, mi, s = m.groups()
            found.append(("операц" in line.lower(), int(y), int(mo), int(d), int(h), int(mi), int(s or 0)))
        for m in re.finditer(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})(?:\s*г\.?)?,?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
                             line.lower()):
            d, mon, y, h, mi, s = m.groups()
            if mon in MONTHS:
                found.append(("операц" in line.lower(), int(y), MONTHS[mon], int(d), int(h), int(mi), int(s or 0)))
    if not found:
        return None
    found.sort(key=lambda f: not f[0])  # сначала «...операции...»
    _, y, mo, d, h, mi, s = found[0]
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=MSK).timestamp()
    except ValueError:
        return None


def _amount(text: str) -> float | None:
    """Сумма перевода: наибольшая ненулевая сумма в ₽, не из строки про комиссию."""
    best = None
    for line in text.splitlines():
        if "комисс" in line.lower():
            continue
        for m in re.finditer(r"(?<![\d.,])(\d{1,3}(?:[ ]\d{3})*(?:[.,]\d{1,2})?)\s*(?:₽|руб)", line):
            v = float(m.group(1).replace(" ", "").replace(",", "."))
            if v > 0 and (best is None or v > best):
                best = v
    return best


def _sbp_id(text: str) -> str | None:
    """Номер операции СБП (32 символа, начинается с A или B); бывает разорван переносом строки."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in re.finditer(r"\b([AB][0-9A-Z]{15,32})\b", line):
            sid = m.group(1)
            if len(sid) < 32 and i + 1 < len(lines):
                tail = re.match(r"\s*([0-9A-Z]+)", lines[i + 1])
                if tail and len(sid) + len(tail.group(1)) == 32:
                    sid += tail.group(1)
            if len(sid) == 32 and re.search(r"\d{8}", sid) and re.search(r"[A-Z]", sid[1:]):
                return sid
    return None


def parse(data: bytes, pay10: str) -> dict:
    from io import BytesIO

    from pypdf import PdfReader
    r = PdfReader(BytesIO(data))
    text = _norm("\n".join((p.extract_text() or "") for p in r.pages[:3]))
    low = text.lower()
    meta = r.metadata or {}
    producer = " ".join(str(meta.get(k) or "") for k in ("/Producer", "/Creator")).strip()
    created, modified = str(meta.get("/CreationDate") or ""), str(meta.get("/ModDate") or "")
    digits = re.sub(r"\D", "", text)
    return {**_fields(text, low, digits, pay10), "producer": producer[:80],
            "editor": any(e in producer.lower() for e in EDITORS),
            "modified": bool(modified and created and modified[:16] != created[:16])}


def _sender_bank(text: str) -> str | None:
    """Банк отправителя: ищем по всему чеку, кроме строк про получателя (и строки сразу после «…получателя»)."""
    skip_next = False
    for line in text.splitlines():
        low = line.lower()
        if skip_next:
            skip_next = False
            continue
        if "получател" in low:
            skip_next = low.rstrip().endswith("получателя")
            continue
        for key, name in BANKS:
            if key in low:
                return name
    return None


def _fields(text: str, low: str, digits: str, pay10: str) -> dict:
    bank = _sender_bank(text)
    return {
        "chars": len(text.strip()),
        "op_ts": _op_time(text),
        "amount": _amount(text),
        "phone_ok": bool(pay10) and pay10 in digits,
        "status_ok": any(w in low for w in OK_WORDS),
        "status_bad": any(w in low for w in BAD_WORDS),
        "sbp_id": _sbp_id(text),
        "bank": bank,
    }


def esc_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ago(sec: float) -> str:
    if sec < 3600:
        return f"{max(1, int(sec // 60))} мин."
    if sec < 2 * 86400:
        return f"{int(sec // 3600)} ч."
    return f"{int(sec // 86400)} дн."


def assess(p: dict | None, tariff: int, submitted_at: float, dup_pid: int | None = None) -> str:
    """Короткий отчёт для админа. 🚨 — серьёзно, ⚠️ — проверь, ✅ — совпало."""
    if not p:
        return "🤖 <b>Проверка чека:</b> не смог прочитать файл — проверь вручную."
    if p.get("chars", 0) < 20:
        return "🤖 <b>Проверка чека:</b> в PDF нет текста (похоже на картинку) — проверь вручную."
    out = []
    if dup_pid:
        out.append(f"🚨 Эта же операция СБП уже была в чеке #{dup_pid}")
    ts = p.get("op_ts")
    if ts is None:
        out.append("⚠️ Дату операции не нашёл")
    else:
        when = datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y %H:%M")
        lag = submitted_at - ts
        if lag < -600:
            out.append(f"🚨 Дата операции в будущем: {when} МСК")
        elif lag > 2 * 86400:
            out.append(f"⚠️ Оплата {when} МСК — <b>{_ago(lag)} назад</b>, чек прислан только сейчас")
        else:
            out.append(f"✅ Оплата {when} МСК ({_ago(max(lag, 0))} назад)")
    a = p.get("amount")
    if a is None:
        out.append("⚠️ Сумму не нашёл")
    else:
        s = f"{a:g} ₽"
        if a < tariff:
            out.append(f"⚠️ Сумма {s} — меньше тарифа {tariff} ₽")
        elif abs(a / tariff - round(a / tariff)) > 0.01:
            out.append(f"⚠️ Сумма {s} — не кратна тарифу {tariff} ₽ (≈ {a / tariff:.1f} мес.)")
        else:
            n = round(a / tariff)
            out.append(f"✅ Сумма {s}" + (f" — {n} мес." if n > 1 else ""))
    out.append("✅ Получатель — наш номер" if p.get("phone_ok") else "🚨 Нашего номера в чеке нет")
    if p.get("status_bad"):
        out.append("🚨 В чеке статус «отклонено / в обработке»")
    if p.get("editor"):
        out.append(f"🚨 Файл сделан в PDF-редакторе: {esc_html(p.get('producer') or '')}")
    elif p.get("modified"):
        out.append("⚠️ Файл изменялся после создания")
    if not p.get("sbp_id"):
        out.append("⚠️ Номер операции СБП не нашёл (у переводов внутри одного банка его может не быть)")
    bank = p.get("bank")
    head = "🤖 <b>Проверка чека</b>" + (f" ({bank})" if bank else "") + ":"
    return head + "\n" + "\n".join(out)


async def check(data: bytes, pay10: str, timeout: float = 20) -> dict | None:
    """Разбор в отдельном процессе с лимитами. None — не получилось (не валим бота)."""
    import asyncio
    import resource

    def limits():
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", __file__, pay10, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, preexec_fn=limits)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(data), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        return json.loads(out) if proc.returncode == 0 and out else None
    except Exception:
        return None


if __name__ == "__main__":
    try:
        print(json.dumps(parse(sys.stdin.buffer.read(), sys.argv[1] if len(sys.argv) > 1 else "")))
    except Exception:
        sys.exit(2)
