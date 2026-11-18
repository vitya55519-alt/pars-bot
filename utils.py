import asyncio
import datetime
import logging
import re
from typing import Iterator, List, Optional, Tuple, Union

from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("pars")

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002190-\U00002BFF\uFE0F\u200D\u20E3]")

TEXT_LIMIT = 3900
CALLBACK_LIMIT = 64

Cell = Tuple[str, str]


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def as_utc(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Telegram присылает дату без пояса времени - это UTC.

    Сравнивать такую дату можно только с такой же: Python кидает TypeError
    на смеси наивной и осознанной даты, и процесс на Railway падает на старте.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def markup(*rows: Union[Cell, List[Cell]]) -> InlineKeyboardMarkup:
    built = []
    for row in rows:
        cells = [row] if isinstance(row, tuple) else list(row)
        built.append(
            [
                InlineKeyboardButton(text=text[:TEXT_LIMIT], callback_data=data[:CALLBACK_LIMIT])
                for text, data in cells
            ]
        )
    return InlineKeyboardMarkup(built)


def chunks(items: List, size: int = 2) -> Iterator[List]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


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


def strip_source(text: str, chat) -> str:
    """Убирает подпись донора из текста: его @юзернейм и ссылки на его канал, только его."""
    username = (getattr(chat, "username", "") or "").strip()
    if not username or not text:
        return text

    name = re.escape(username)
    mention = re.compile(rf"(?<![\w.])@{name}\b(/[0-9]+)?", re.IGNORECASE)
    link = re.compile(rf"(?:https?://)?(?:t|telegram)\.me/(?:s/)?{name}\b(/[0-9]+)?", re.IGNORECASE)
    cleaned = mention.sub(" ", link.sub(" ", text))
    kept: List[str] = []
    for line in (re.sub(r"\s+", " ", item).strip() for item in cleaned.splitlines()):
        if not line and (not kept or not kept[-1]):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


async def call(func, *args, **kwargs):
    """Telegram-метод с автоматическим ожиданием FloodWait."""
    while True:
        try:
            return await func(*args, **kwargs)
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0) + 1
            log.warning("Лимит Telegram: пауза %s сек. Продолжим автоматически.", wait)
            await asyncio.sleep(wait)
