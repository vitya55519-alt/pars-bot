import asyncio
import logging
import re

from pyrogram.errors import FloodWait

log = logging.getLogger("pars")

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002190-\U00002BFF\uFE0F\u200D\u20E3]")


def chat_name(chat) -> str:
    if chat is None:
        return "?"
    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"
    return getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(getattr(chat, "id", "?"))


def norm_title(text: str) -> str:
    """Название канала и название темы сверяются без учёта регистра, эмодзи и лишних пробелов."""
    return " ".join(EMOJI_RE.sub(" ", text or "").split()).casefold()


async def call(func, *args, **kwargs):
    """Telegram-метод с автоматическим ожиданием FloodWait."""
    while True:
        try:
            return await func(*args, **kwargs)
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0) + 1
            log.warning("Лимит Telegram: пауза %s сек. Продолжим автоматически.", wait)
            await asyncio.sleep(wait)
