"""Учёт сообщений в личке, чтобы пользователь и админ могли одной кнопкой очистить переписку.

Telegram не даёт боту прочитать историю чата, поэтому бот сам запоминает id всех сообщений:
своих (через middleware запросов) и входящих (через middleware апдейтов).
"""
from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import Message

from core import db
from notifier import delete_many


class OutgoingTracker(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        # id личных чатов всегда положительные, у групп — отрицательные
        if isinstance(result, Message) and result.chat.type == "private" and result.chat.id > 0:
            db.track_msg(result.chat.id, result.message_id)
        return result


class IncomingTracker(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type == "private":
            db.track_msg(event.chat.id, event.message_id)
        return await handler(event, data)


async def clean_chat(bot: Bot, chat_id: int) -> int:
    ids = db.take_msgs(chat_id=chat_id).get(chat_id, [])
    await delete_many(bot, chat_id, ids)
    return len(ids)
