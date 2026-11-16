import asyncio
import datetime
import logging
from collections import OrderedDict
from typing import List, Set, Tuple

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import ChatForwardsRestricted
from pyrogram.handlers import MessageHandler
from pyrogram.types import InputMediaPhoto, Message

from ai import cut
from commands import Controls
from promo import Promoter
from reactions import Reactor
from registry import Category
from utils import call, chat_name, strip_source

log = logging.getLogger("pars")

TELEGRAM_ALBUM_LIMIT = 10
LOOKBACK = 10
CATCH_UP_LIMIT = 10
WRITING_STATUSES = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


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
        if not category.target:
            log.warning(
                "Тема «%s» без целевого канала - пост %s пропущен: переименуй тему в канал, куда постить.",
                category.name,
                message.id,
            )
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
            log.error("Сбой на посте %s: %s: %s", message.id, type(exc).__name__, exc, exc_info=True)

    def _drop_source(self, text: str, chat) -> str:
        """Подпись донора - его @юзернейм и ссылки на его канал - в наш канал попадать не должна."""
        cleaned = strip_source(text, chat)
        if cleaned != text:
            log.info("Убрал подпись донора %s из текста поста.", chat_name(chat))
        return cleaned

    async def _single(self, message: Message, category) -> None:
        log.info("Пойман пост из %s (тема «%s», id %s).", chat_name(message.chat), category.name, message.id)

        original = (message.caption or "").strip()
        if not original:
            log.info("В посте нет текста - пропуск (нечего рерайтить).")
            return

        text = self._drop_source(await self.ai.rewrite(original), message.chat)
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

        text = self._drop_source(await self.ai.rewrite(original), message.chat)
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

    # ==== диагностика ====

    async def _history(self, chat, limit: int = LOOKBACK) -> List[Message]:
        """Последние сообщения чата, от старых к новым."""
        items: List[Message] = []
        async for message in self.client.get_chat_history(chat, limit=limit):
            items.append(message)
        items.reverse()
        return items

    async def _published_ids(self, category: Category, limit: int) -> Set[str]:
        """Картинки, которые уже лежат в нашем канале: по ним и проверяем, что дошло."""
        return {
            message.photo.file_unique_id
            for message in await self._history(category.target, limit)
            if message.photo
        }

    async def _missed(self, category: Category, limit: int = LOOKBACK) -> List[Message]:
        """Посты доноров, которых нет в канале: их съел сбой, пауза или перезапуск."""
        published = await self._published_ids(category, limit)
        found: List[Message] = []
        for donor in category.donors.values():
            for message in await self._history(donor.chat_id, limit):
                if not message.photo or not (message.caption or "").strip():
                    continue
                if message.photo.file_unique_id in published:
                    continue
                found.append(message)
        return sorted(found, key=lambda message: message.id)

    async def diagnose(self, limit: int = LOOKBACK) -> str:
        """Честный ответ «почему не публикует»: посты доноров, права в канале и пропущенное."""
        me = await call(self.client.get_me)
        lines: List[str] = []
        for category in self.registry.categories.values():
            lines.append(f"📂 «{category.name}» -> {category.target_name or 'нет канала'}")
            if not category.target:
                lines.append(f"    ⚠ {category.problem or 'канал не определён'} - ловить нечего")
                continue
            published = await self._published_ids(category, limit)
            lines.append(f"    канал: {await self._gate(category.target, me.id)}")
            for donor in category.donors.values():
                lines.append(f"    {donor.name}: {await self._donor_line(donor.chat_id, published, limit)}")
        stats = self.stats
        lines.append(
            f"Итого: поймано {stats['caught']} · опубликовано {stats['published']} · сбоев {stats['errors']}"
            f" · последняя публикация {self.last_publish or 'ещё не было'}"
        )
        return "\n".join(lines)

    async def _gate(self, chat, user_id: int) -> str:
        try:
            member = await call(self.client.get_chat_member, chat, user_id)
        except Exception as exc:  # noqa: BLE001 - канал мог закрыться для этого аккаунта
            return f"не открылся ({type(exc).__name__}) - постить не будем"
        status = getattr(member, "status", None)
        if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            return "аккаунт не подписан - Telegram не даст публиковать"
        if status == ChatMemberStatus.RESTRICTED:
            return "аккаунт ограничен - писать в канал нельзя"
        if status in WRITING_STATUSES:
            if getattr(member, "can_send_messages", True) is False:
                return "админ, но писать запрещено - снимешь запрет, начнём"
            return "пишем свободно"
        return f"не админ ({getattr(status, 'name', status)}) - канал не примет пост"

    async def _donor_line(self, chat_id: int, published: Set[str], limit: int) -> str:
        try:
            history = await self._history(chat_id, limit)
        except Exception as exc:  # noqa: BLE001 - доступ к донору мог пропасть
            return f"не открылся ({type(exc).__name__})"
        if not history:
            return "сообщений нет"
        posts = [message for message in history if message.photo and (message.caption or "").strip()]
        missed = [message for message in posts if message.photo.file_unique_id not in published]
        stamp = self._ago(posts[-1] if posts else history[-1])
        label = "последний пост" if posts else "последнее сообщение"
        return f"{label} {stamp}, постов с фото и текстом {len(posts)}, не дошло {len(missed)}"

    def _ago(self, message: Message) -> str:
        date = getattr(message, "date", None)
        if not date:
            return "без даты"
        minutes = int((datetime.datetime.now(datetime.timezone.utc) - date).total_seconds() // 60)
        if minutes < 1:
            return "только что"
        if minutes < 60:
            return f"{minutes} мин назад"
        return f"{minutes // 60} ч назад"

    async def catch_up(self, limit: int = CATCH_UP_LIMIT) -> str:
        """Догоняет пропущенное тем же маршрутом, что и живой пост: рерайт, публикация, промо, реакции."""
        todo: List[Tuple[Category, Message]] = []
        for category in self.registry.categories.values():
            if category.target:
                for message in await self._missed(category):
                    todo.append((category, message))
        if not todo:
            return "Пропущенного нет: всё свежее у доноров уже лежит в каналах."
        done = 0
        for category, message in todo[:limit]:
            try:
                await self._single(message, category)
                done += 1
            except Exception as exc:  # noqa: BLE001 - один пост не должен рвать догон
                self.stats["errors"] += 1
                log.error("Догнать пост %s не вышло: %s: %s", message.id, type(exc).__name__, exc, exc_info=True)
        rest = len(todo) - done
        tail = f" Остальных {rest} не тронул - нажми ещё раз." if rest > 0 else ""
        return f"Догнал {done} пост(ов).{tail}"
