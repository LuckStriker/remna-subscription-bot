# Changelog

## v1.1.0 — 2026-11-20

### English
**Payments**
- **Receipt auto-check (PDF).** The bot reads the payment date and time, amount, recipient, status and
  SBP operation ID from the receipt and posts a short report under the receipt card for the admin:
  old receipt, wrong recipient, underpayment, PDF-editor traces, the same payment sent twice
  (even as a re-exported file). Parsing runs in a separate process with memory and CPU limits.
  A PDF can be edited, so the admin still makes the final decision.
- **Rejection with a reason.** "Reject" now asks for a reason (6 presets + custom text);
  the user gets a clear explanation and what to do next.
- **Duplicate file detection** — the same PDF file sent again is flagged.
- The user's caption on the receipt (e.g. "paying in advance") is shown to the admin.
- Payment screen explains: pay on the expiry day and send the receipt right away.

**Bonuses**
- **Loyalty streak:** paying on time N months in a row gives bonus days (`LOYAL_TIERS`, default 3→+7, 6→+14),
  the cycle then starts over. Multi-month payments count. Streak is shown in the profile,
  in reminders and in the payment confirmation. Concurrent approvals can't double the bonus.
- **First payment through the bot** bonus for any client (`PAID_PROMO_*`).

**Reminders**
- Reminders are pinned at the top of the chat, sent only in the daytime, and turn into "✅ renewed" after payment.
  Defaults: 3 days, 1 day and 6 hours before expiry + one follow-up after expiry (`REMIND_AFTER_HOURS`).

**Requirements:** new dependency `pypdf`; the database is migrated automatically on start.

### Русский
**Оплата**
- **Автопроверка чека (PDF).** Бот читает из чека дату и время, сумму, получателя, статус и номер операции СБП
  и пишет админу короткий отчёт под карточкой чека: старый чек, чужой получатель, недоплата, следы PDF-редактора,
  повтор той же оплаты (даже если чек выгрузили заново). Разбор идёт в отдельном процессе с лимитами памяти и времени.
  PDF можно отредактировать, поэтому окончательное решение — за админом.
- **Отказ с причиной.** «Отклонить» теперь просит выбрать причину (6 готовых + своя);
  пользователь получает понятное объяснение и что сделать дальше.
- **Повторный файл** — тот же PDF, присланный ещё раз, помечается.
- Подпись пользователя к чеку (например, «наперёд») видна админу.
- На экране оплаты: оплачивайте в день окончания и сразу присылайте чек.

**Бонусы**
- **Серия оплат без перерыва:** N месяцев подряд вовремя — бонусные дни (`LOYAL_TIERS`, по умолчанию 3→+7, 6→+14),
  дальше цикл заново. Оплата сразу за несколько месяцев засчитывается. Серия видна в профиле, в напоминаниях
  и в подтверждении оплаты. Одновременные одобрения не дают бонус дважды.
- **Бонус за первую оплату через бота** — любому клиенту (`PAID_PROMO_*`).

**Напоминания**
- Закрепляются вверху чата, приходят только днём, после оплаты превращаются в «✅ продлено».
  По умолчанию: за 3 дня, за сутки и за 6 часов + одно догоняющее после окончания (`REMIND_AFTER_HOURS`).

**Требования:** новая зависимость `pypdf`; база обновляется автоматически при запуске.
