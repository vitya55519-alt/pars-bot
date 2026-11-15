import asyncio
import logging
from collections import OrderedDict
from typing import List, Optional

from pyrogram import filters
from pyrogram.errors import ChatForwardsRestricted
from pyrogram.handlers import MessageHandler
from pyrogram.types import InputMediaPhoto, Message

from ai import cut
from promo import Promoter
from utils import call

log = logging.getLogger("pars")

TELEGRAM_ALBUM_LIMIT = 10


def chat_name(chat) -> str:
    if chat is None:
        return "?"
    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"
    return getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(getattr(chat, "id", "?"))


class ParsBot:
    def __init__(self, client, cfg, ai):
        self.client = client
        self.cfg = cfg
        self.ai = ai
        self.promo = Promoter(client, cfg)
        self._seen_groups: "OrderedDict[str, None]" = OrderedDict()
        self.target_id: Optional[int] = None
        self.target_name = str(cfg.target_chat)

    async def setup(self) -> None:
        target = await self.client.get_chat(self.cfg.target_chat)
        self.target_id = target.id
        self.target_name = chat_name(target)
        log.info("Целевой канал доступен: %s", self.target_name)

        for chat_ref in self.cfg.source_chats:
            try:
                donor = await self.client.get_chat(chat_ref)
                log.info("Донор подключён: %s", chat_name(donor))
            except Exception as exc:  # noqa: BLE001 - один недоступный донор не останавливает остальных
                log.warning("Донор %s недоступен (%s: %s) - проверь, что аккаунт подписан на канал.", chat_ref, type(exc).__name__, exc)

        self.client.add_handler(
            MessageHandler(self.on_message, filters.chat(self.cfg.source_chats) & filters.photo & ~filters.outgoing),
        )

    def _remember_group(self, group_id: str) -> None:
        self._seen_groups[group_id] = None
        while len(self._seen_groups) > self.cfg.cache_size:
            self._seen_groups.popitem(last=False)

    async def on_message(self, client, message: Message) -> None:
        if self.target_id is not None and message.chat.id == self.target_id:
            return

        try:
            if message.media_group_id:
                await self._album(message)
            else:
                await self._single(message)
        except ChatForwardsRestricted:
            log.warning("Донор %s запрещает пересылку/копирование - пост пропущен.", chat_name(message.chat))
        except Exception as exc:  # noqa: BLE001 - скрипт не должен падать на одном посте
            log.error("Сбой на посте %s: %s: %s", message.id, type(exc).__name__, exc)

    async def _single(self, message: Message) -> None:
        log.info("Пойман пост из %s (id %s).", chat_name(message.chat), message.id)

        original = (message.caption or "").strip()
        if not original:
            log.info("В посте нет текста - пропуск (нечего рерайтить).")
            return

        text = await self.ai.rewrite(original)
        log.info("Рерайт завершён (%s символов).", len(text))

        posted = await call(
            self.client.send_photo,
            self.cfg.target_chat,
            message.photo.file_id,
            caption=cut(text, self.cfg.caption_limit),
        )
        log.info("Опубликовано в %s (id %s).", self.target_name, posted.id)
        await self.promo.push(posted)

    async def _album(self, message: Message) -> None:
        group_id = message.media_group_id
        if group_id in self._seen_groups:
            log.debug("Альбом %s уже обработан - пропуск карточки.", group_id)
            return
        self._remember_group(group_id)

        log.info("Пойман альбом из %s (id %s, группа %s).", chat_name(message.chat), message.id, group_id)
        await asyncio.sleep(self.cfg.album_wait)

        items = await call(self.client.get_media_group, message.chat.id, message.id)
        photos = [item for item in items if item.photo]
        if not photos:
            log.info("В альбоме %s нет фотографий - пропуск.", group_id)
            return

        original = next((item.caption for item in items if (item.caption or "").strip()), "").strip()
        if not original:
            log.info("В альбоме %s нет текста - пропуск.", group_id)
            return

        text = await self.ai.rewrite(original)
        log.info("Рерайт альбома завершён (%s символов, карточек %s).", len(text), len(photos[:TELEGRAM_ALBUM_LIMIT]))

        caption = cut(text, self.cfg.caption_limit)
        media: List[InputMediaPhoto] = []
        for index, item in enumerate(photos[:TELEGRAM_ALBUM_LIMIT]):
            media.append(
                InputMediaPhoto(
                    media=item.photo.file_id,
                    caption=caption if index == 0 else None,
                )
            )

        posted = await call(self.client.send_media_group, self.cfg.target_chat, media)
        main_message = next((item for item in posted if (item.caption or "").strip()), posted[0])
        log.info("Альбом опубликован в %s (id %s).", self.target_name, main_message.id)
        await self.promo.push(main_message)
