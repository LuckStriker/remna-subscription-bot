# Changelog

## v1.3.0 — 2026-11-28

### English
- **One-tap approval by receipt amount.** After the PDF auto-check the bot adds a button on top of the receipt card,
  e.g. "✅ 350 ₽ → +36 days": days = amount × 31 ÷ the client's price. Not shown for underpayment or a duplicate
  receipt — those stay manual (underpayment comes with a warning to the client).
- **Access by invitation only.** A request without a valid referral link is declined by the bot itself with a polite
  message ("ask a friend for an invite link"); the admin gets a silent note, no buttons. Requests via a referral link
  work as before.
- **Device limit from the panel.** `DEFAULT_DEVICES=0` no longer sets a per-user limit, so the panel's own rule
  applies; the admin prompt says "0 — panel rule".

### Русский
- **Подтверждение по сумме чека в одно нажатие.** После автопроверки PDF бот добавляет сверху карточки чека кнопку,
  например «✅ 350 ₽ → +36 дн.»: дни = сумма × 31 ÷ тариф клиента. При недоплате и повторном чеке кнопки нет,
  решение руками (при недоплате клиенту уходит предупреждение).
- **Доступ только по приглашению.** Заявку без действующей реферальной ссылки бот отклоняет сам вежливым
  сообщением («попросите у друга ссылку-приглашение»), админу тихая заметка без кнопок. Заявки по ссылке как раньше.
- **Лимит устройств из панели.** `DEFAULT_DEVICES=0` больше не задаёт лимит аккаунту, действует правило панели;
  в подсказке админу «0 — по правилу панели».

## v1.2.0 — 2026-11-23

### English
- **Channel subscriber stats.** Joins and leaves of the channel are recorded (kept 180 days). The admin gets a message
  on every leave — whether the person is a client (account and expiry) or not. The morning digest shows the subscriber
  count and "+N / −M" for the last day. Telegram does not give bots the subscriber list, so tracking starts from the update.
- **"First N who pay for a long period" promo** (optional, BIG_PROMO_*, off by default): a receipt for
  BIG_PROMO_MIN_DAYS+ days gives BIG_PROMO_DAYS on top of all other bonuses, once per subscription, race-safe.
- Payment screen: pay on the expiry day; for advance payments add the caption "in advance" — shown to the admin.

### Русский
- **Статистика подписчиков канала.** Входы и выходы записываются (хранятся 180 дней). Админу приходит сообщение
  о каждой отписке: клиент это (аккаунт и срок) или нет. В утренней сводке — число подписчиков и «+N / −M» за сутки.
  Список подписчиков Telegram ботам не отдаёт, поэтому учёт идёт с момента обновления.
- **Акция «первые N, оплатившие надолго»** (по желанию, BIG_PROMO_*, по умолчанию выключена): чек на
  BIG_PROMO_MIN_DAYS+ дней даёт BIG_PROMO_DAYS сверху всех бонусов, один раз на подписку, без двойных начислений.
- Экран оплаты: оплачивать в день окончания; при оплате заранее — подпись «наперёд», её видит админ.
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
