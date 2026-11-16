import asyncio
import datetime
import logging
from collections import OrderedDict
from typing import List

from pyrogram import filters
from pyrogram.errors import ChatForwardsRestricted
from pyrogram.handlers import MessageHandler
from pyrogram.types import InputMediaPhoto, Message

from ai import cut
from commands import Controls
from promo import Promoter
from reactions import Reactor
from utils import call, chat_name

log = logging.getLogger("pars")

TELEGRAM_ALBUM_LIMIT = 10


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ParsBot:
    def __init__(self, client, cfg, ai, registry, session):
        self.client = client
        self.cfg = cfg
        self.ai = ai
        self.registry = registry
        self.promo = Promoter(client, cfg)
        self.reactions = Reactor(cfg, session)
        self.controls = Controls(client, cfg, registry, self)
        self._seen_groups: "OrderedDict[str, None]" = OrderedDict()
        self.stats = {"caught": 0, "published": 0, "forwarded": 0, "errors": 0}
        self.last_publish = ""

    async def setup(self) -> None:
        await self.registry.rebuild()

        is_donor = filters.create(lambda _, __, message: self.registry.route(message.chat.id) is not None)
        self.client.add_handler(MessageHandler(self.on_message, is_donor & filters.photo & ~filters.outgoing))
        self.controls.setup()

        log.info("Доноров в списке: %s.", self.registry.donor_count)

    def _remember_group(self, group_id: str) -> None:
        self._seen_groups[group_id] = None
        while len(self._seen_groups) > self.cfg.cache_size:
            self._seen_groups.popitem(last=False)

    async def on_message(self, client, message: Message) -> None:
        category = self.registry.route(message.chat.id)
        if category is None:
            return

        self.stats["caught"] += 1
        try:
            if message.media_group_id:
                await self._album(message, category)
            else:
                await self._single(message, category)
        except ChatForwardsRestricted:
            log.warning("Донор %s запрещает пересылку/копирование - пост пропущен.", chat_name(message.chat))
        except Exception as exc:  # noqa: BLE001 - скрипт не должен падать на одном посте
            self.stats["errors"] += 1
            log.error("Сбой на посте %s: %s: %s", message.id, type(exc).__name__, exc)

    async def _single(self, message: Message, category) -> None:
        log.info("Пойман пост из %s (тема «%s», id %s).", chat_name(message.chat), category.name, message.id)

        original = (message.caption or "").strip()
        if not original:
            log.info("В посте нет текста - пропуск (нечего рерайтить).")
            return

        text = await self.ai.rewrite(original)
        log.info("Рерайт завершён (%s символов).", len(text))

        posted = await call(
            self.client.send_photo,
            category.target,
            message.photo.file_id,
            caption=cut(text, self.cfg.caption_limit),
        )
        self._published(category, posted)
        await self._boost(posted)

    async def _album(self, message: Message, category) -> None:
        group_id = message.media_group_id
        if group_id in self._seen_groups:
            log.debug("Альбом %s уже обработан - пропуск карточки.", group_id)
            return
        self._remember_group(group_id)

        log.info("Пойман альбом из %s (тема «%s», id %s).", chat_name(message.chat), category.name, message.id)
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
        media: List[InputMediaPhoto] = [
            InputMediaPhoto(media=item.photo.file_id, caption=caption if index == 0 else "")
            for index, item in enumerate(photos[:TELEGRAM_ALBUM_LIMIT])
        ]

        posted = await call(self.client.send_media_group, category.target, media)
        main_message = next((item for item in posted if (item.caption or "").strip()), posted[0])
        self._published(category, main_message)
        await self._boost(main_message)

    def _published(self, category, posted: Message) -> None:
        self.stats["published"] += 1
        self.last_publish = now()
        log.info("Опубликовано в %s (тема «%s», id %s).", category.target_name, category.name, posted.id)

    async def _boost(self, message: Message) -> None:
        if await self.promo.push(message):
            self.stats["forwarded"] += 1
        self.reactions.schedule(message)
