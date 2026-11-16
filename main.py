import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

from config import settings
from core import api, db
from handlers import admin, feedback, gift, group, servers, user
from monitor import monitor_loop
from notifier import background_loop
from tracking import IncomingTracker, OutgoingTracker


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Не засоряем журнал: без строки на каждый HTTP-запрос и каждый апдейт — только предупреждения и ошибки
    for noisy in ("httpx", "aiogram.event", "asyncssh"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    bot.session.middleware(OutgoingTracker())
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(IncomingTracker())    # Порядок важен: админские роутеры (с фильтром по ID) раньше пользовательского
    dp.include_routers(admin.router, gift.router, servers.router, group.router, feedback.router, user.router)

    if not settings.group_chat_id and db.kv_get("group_chat_id"):
        settings.group_chat_id = int(db.kv_get("group_chat_id"))

    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    try:  # то, что пользователь видит в пустом чате с ботом
        await bot.set_my_short_description(f"🤖 {settings.bot_name}: подписка, QR, оплата")
        await bot.set_my_description(
            f"Привет! Я {settings.bot_name} 🤖 — помогу с подпиской.\n\n"
            "📊 Покажу срок подписки, трафик и устройства\n🖼 Выдам QR-код для подключения\n"
            "💳 Приму чек об оплате\n⏰ Напомню, когда подписка подходит к концу\n\nЖми «Старт»!")
    except Exception:
        logging.warning("Не удалось обновить описание бота")
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Админ-меню"),
                BotCommand(command="status", description="Всё ли ок: ноды, аптайм, диски"),
                BotCommand(command="user", description="Карточка: /user ID"),
                BotCommand(command="days", description="Дни: /days ID 31 (или -5)"),
                BotCommand(command="create", description="Создать: /create TG_ID [дни]"),
                BotCommand(command="link", description="Привязать: /link TG_ID username"),
                BotCommand(command="bday", description="ДР клиента: /bday ID 14.03"),
                BotCommand(command="bdays", description="Все дни рождения, ближайшие сверху"),
                BotCommand(command="verified", description="Кто прошёл верификацию"),
                BotCommand(command="feedback", description="Итоги опроса «как вам бот»"),
                BotCommand(command="cancel", description="Отменить ввод"),
            ], scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            logging.warning("Админ %s ещё не писал боту — команды не установлены", admin_id)

    reminders = asyncio.create_task(background_loop(bot))
    watchdog = asyncio.create_task(monitor_loop(bot))
    try:
        # chat_member нужно запрашивать явно — иначе Telegram не присылает входы в группу
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        reminders.cancel()
        watchdog.cancel()
        await api.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
