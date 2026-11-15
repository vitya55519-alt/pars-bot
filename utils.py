import asyncio
import logging

from pyrogram.errors import FloodWait

log = logging.getLogger("pars")


async def call(func, *args, **kwargs):
    """Telegram-метод с автоматическим ожиданием FloodWait."""
    while True:
        try:
            return await func(*args, **kwargs)
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0) + 1
            log.warning("Лимит Telegram: пауза %s сек. Продолжим автоматически.", wait)
            await asyncio.sleep(wait)
