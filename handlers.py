"""Поймал пост донора -> переписал текст -> опубликовал в свой канал.

Берём любой пост донора, в котором есть текст: с картинкой - картинкой по
file_id (ничего не скачиваем), альбом - пачкой карточек, без картинки - просто
текстом. У поста с видео или документом переносим только текст: такие носители
мы по file_id не отдаём, о чём пишет в журнал. Пост без текста не публикуем -
перепечатывать нечего. Если донор - группа, а не канал, ловим только посты
самого чата: реплики подписчиков в канал не идут.
"""

import asyncio
import datetime
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple, Union

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChatForwardsRestricted
from pyrogram.handlers import MessageHandler
from pyrogram.types import InputMediaPhoto, Message

from ai import cut
from commands import Controls
from promo import Promoter
from reactions import Reactor, count_reactions
from registry import Category
from store import MAX_POSTS_PER_DAY
from utils import TEXT_LIMIT, as_utc, call, chat_name, strip_source, utc_now

log = logging.getLogger("pars")

TELEGRAM_ALBUM_LIMIT = 10
LOOKBACK = 10
REACTIONS_LOOKBACK = 10
CATCH_UP_LIMIT = 10
# как часто говорим в журнал, доходят ли до нас сообщения: молчание неотличимо от «нечего ловить»
HEARTBEAT_SECONDS = 900
WRITING_STATUSES = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
# носители, которые мы по file_id не переносим: у такого поста в канал пойдёт только текст
OTHER_MEDIA = ("video", "animation", "document", "audio", "voice", "video_note", "sticker")


@dataclass
class Destination:
    """Куда положить пост и с какими настройками: у клиента они свои.

    Один пост донора умеет разойтись по нескольким каналам - своему складу и
    кабинетам клиентов, которые подписаны на того же донора. Рерайт тогда
    считается один раз, а публикаций столько, сколько адресатов.
    """

    target: Union[int, str]
    name: str
    label: str
    views: Optional[int] = None
    reactions: bool = True
    rewrite: bool = True
    tenant_id: int = 0


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def post_text(message: Message) -> str:
    """Текст поста: подпись под картинкой или сам текст, если пост без картинки."""
    return (message.caption or message.text or "").strip()


def other_media(message: Message) -> str:
    """Чем занят пост, кроме картинки: видеоролик, документ, голос. Пусто - картинка или текст."""
    return next((kind for kind in OTHER_MEDIA if getattr(message, kind, None) is not None), "")


def post_key(message: Message) -> str:
    """Чем пост донора опознаётся: чат и номер. Память о них живёт до перезапуска."""
    return f"{message.chat.id}:{message.id}"


def is_broadcast(message: Message) -> bool:
    """Пост канала, а не строчка из переписки: донором бывает и группа, там ловим только посты самого чата."""
    if getattr(message.chat, "type", None) == ChatType.CHANNEL:
        return True
    return getattr(message, "sender_chat", None) is not None


class ParsBot:
    def __init__(self, client, cfg, ai, registry, session):
        self.client = client
        self.cfg = cfg
        self.ai = ai
        self.registry = registry
        self.promo = Promoter(client, cfg)
        self.reactions = Reactor(cfg, session, client)
        self.controls = Controls(client, cfg, registry, self)
        self._seen_groups: "OrderedDict[str, None]" = OrderedDict()
        self._sent_posts: "OrderedDict[str, None]" = OrderedDict()
        self.started = utc_now()
        self.stats = {"caught": 0, "published": 0, "forwarded": 0, "errors": 0, "seen": 0}
        self.last_publish = ""
        self.store = None
        self.clients = None

    def attach(self, store, clients) -> None:
        """База клиентов подключается после старта процесса: без неё остаётся личный режим."""
        self.store = store
        self.clients = clients

    async def setup(self) -> None:
        await self.registry.rebuild()

        def interested(_, __, message):
            """Наш пост - тот, чей канал ловим для склада или для кого-то из клиентов."""
            if self.registry.route(message.chat.id) is not None:
                return True
            return bool(self.clients) and message.chat.id in self.clients.chats

        is_donor = filters.create(interested)
        has_text = (filters.photo | filters.text | filters.caption) & ~filters.service
        self.client.add_handler(MessageHandler(self.on_message, is_donor & has_text & ~filters.outgoing))
        self.controls.setup()

        log.info("Каналов, откуда берём посты: %s.", self.registry.donor_count)
        await self._report_sources()

    async def _report_sources(self) -> None:
        """Пишем в журнал, что аккаунт реально видит в каждом канале-источнике.

        «Ничего не ловит» почти всегда значит одно: канал не открылся этому
        аккаунту или там просто не было новых постов. По этим строкам видно какое.
        """
        for category in self.registry.categories.values():
            where = category.target_name or category.problem or "канал не определён"
            for donor in category.donors.values():
                log.info(
                    "Источник %s -> постим в %s: %s",
                    donor.name,
                    where,
                    await self._source_state(donor.chat_id),
                )

    async def _source_state(self, chat_id: int) -> str:
        try:
            history = await self._history(chat_id, 3)
        except Exception as exc:  # noqa: BLE001 - доступ к каналу мог пропасть
            return f"не открылся ({type(exc).__name__}) - этот аккаунт должен быть подписан"
        if not history:
            return "сообщений нет"
        posts = [message for message in history if post_text(message)]
        if not posts:
            return f"постов с текстом не нашли (последнее сообщение {self._ago(history[-1])})"
        return f"последний пост {self._ago(posts[-1])}"

    async def heartbeat(self, every: int = HEARTBEAT_SECONDS) -> None:
        """Раз в 15 минут пишем в журнал, доходят ли до нас сообщения от источников.

        Пустой журнал невозможно отличить от «источники не публикуют» - именно так и
        выглядел отзыв сессии Telegram. Поэтому считаем сами, а если не пришло ничего,
        проверяем одним запросом, отвечает ли аккаунт.
        """
        minutes = max(1, every // 60)
        before = {"seen": 0, "published": 0}
        while True:
            await asyncio.sleep(every)
            seen = self.stats["seen"] - before["seen"]
            published = self.stats["published"] - before["published"]
            before = {"seen": self.stats["seen"], "published": self.stats["published"]}
            if seen or published:
                log.info("За %s мин: сообщений от источников %s, опубликовано постов %s.", minutes, seen, published)
                continue
            log.info("За %s мин от каналов-источников не пришло ни одного сообщения - проверяю аккаунт.", minutes)
            try:
                await self.client.get_me()  # без обёртки call: ждать лимит здесь некому
            except Exception as exc:  # noqa: BLE001 - смысл ровно в том, какая это ошибка
                log.error(
                    "Аккаунт не отвечает Telegram (%s): ловли постов не будет, пока не обновишь вход - "
                    "python make_session.py --window, затем новый SESSION_STRING в Railway.",
                    type(exc).__name__,
                )
            else:
                log.warning(
                    "Аккаунт отвечает, но сообщений не было: либо источники молчали, либо Telegram не присылает нам "
                    "обновления - увидим на следующем посте."
                )

    def _remember(self, store: "OrderedDict[str, None]", key: str) -> None:
        """Память на этот запуск: пачки карточек и уже отправленные посты, не больше cache_size."""
        store[key] = None
        while len(store) > self.cfg.cache_size:
            store.popitem(last=False)

    async def on_message(self, client, message: Message) -> None:
        self.stats["seen"] += 1
        dests = await self._dests(message)
        if not dests:
            return

        if not is_broadcast(message):
            log.info(
                "Донор %s: сообщение %s - реплика в переписке, не пост канала - не публикуем.",
                chat_name(message.chat),
                message.id,
            )
            return

        self.stats["caught"] += 1
        try:
            if message.media_group_id:
                await self._album(message, dests)
            else:
                await self._single(message, dests)
        except ChatForwardsRestricted:
            log.warning("Донор %s запрещает пересылку/копирование - пост пропущен.", chat_name(message.chat))
        except Exception as exc:  # noqa: BLE001 - скрипт не должен падать на одном посте
            self.stats["errors"] += 1
            log.error("Сбой на посте %s: %s: %s", message.id, type(exc).__name__, exc, exc_info=True)

    async def _dests(self, message: Message) -> List[Destination]:
        """Кому нужен этот пост: склад владельца и каждый клиент, у которого донор в списке."""
        dests: List[Destination] = []
        category = self.registry.route(message.chat.id)
        if category is not None:
            if not category.target:
                log.warning(
                    "Канал «%s» без адреса, куда публиковать - пост %s пропущен: "
                    "в названии темы укажи @юзернейм канала или назови тему так же, как канал.",
                    category.name,
                    message.id,
                )
            else:
                dests.append(
                    Destination(
                        target=category.target,
                        name=category.target_name,
                        label=f"мой канал {category.target_name}",
                        views=self.registry.views_for(category),
                        reactions=self.registry.reactions_for(category),
                        rewrite=self.registry.rewrite_for(category),
                    )
                )
        for tenant in await self._tenants(message.chat.id):
            dests.append(self._tenant_dest(tenant))
        return dests

    async def _tenants(self, chat_id: int) -> List:
        """Клиенты, для которых этот канал - источник: только активные, база сама отсекает срок."""
        if self.store is None or not self.clients or chat_id not in self.clients.chats:
            return []
        try:
            return await self.store.donors_by_chat(chat_id)
        except Exception as exc:  # noqa: BLE001 - без ответа базы пост уходит складу, а не в никуда
            self.stats["errors"] += 1
            log.error("Клиенты канала %s не прочитаны (%s: %s).", chat_id, type(exc).__name__, exc)
            return []

    @staticmethod
    def _tenant_dest(tenant) -> Destination:
        return Destination(
            target=tenant.target_id or tenant.target,
            name=tenant.target_title or str(tenant.target),
            label=f"клиент {tenant.id}",
            views=tenant.views,
            reactions=tenant.reactions,
            rewrite=tenant.rewrite,
            tenant_id=tenant.id,
        )

    def _drop_source(self, text: str, chat) -> str:
        """Подпись донора - его @юзернейм и ссылки на его канал - в наш канал попадать не должна."""
        cleaned = strip_source(text, chat)
        if cleaned != text:
            log.info("Убрал подпись канала-источника %s из текста поста.", chat_name(chat))
        return cleaned

    async def _single(self, message: Message, dests: List[Destination]) -> None:
        log.info(
            "Пойман пост из %s (id %s) - адресатов %s: %s.",
            chat_name(message.chat),
            message.id,
            len(dests),
            ", ".join(dest.label for dest in dests),
        )

        original = post_text(message)
        if not original:
            log.info("В посте нет текста - пропуск (перепечатывать нечего).")
            return

        author = self._drop_source(original, message.chat)
        rewritten = await self._rewritten(message, dests, author)

        for dest in dests:
            if not await self._allowed(dest):
                continue
            posted = await self._publish(dest, message, self._text_for(dest, author, rewritten))
            await self._published(dest, posted, message)
            await self._boost(dest, posted)

    async def _rewritten(self, message: Message, dests: List[Destination], author: str) -> str:
        """Переписанный текст считаем только если он кому-то нужен: лишний запрос к ИИ - лишние деньги."""
        if not any(dest.rewrite for dest in dests):
            log.info("Переписывание выключено у всех адресатов - публикуем текст автора.")
            return ""
        text = strip_source(await self.ai.rewrite(author), message.chat)
        log.info("Рерайт завершён (%s символов).", len(text))
        return text

    @staticmethod
    def _text_for(dest: Destination, author: str, rewritten: str) -> str:
        """Что уходит в этот канал: переписанный текст или текст автора - выбор человека, а не наша догадка."""
        return (rewritten or author) if dest.rewrite else author

    async def _allowed(self, dest: Destination) -> bool:
        """Суточный лимит клиента: складу владельца он не нужен, у него один хозяин."""
        if not dest.tenant_id or self.store is None:
            return True
        try:
            left = await self.store.quota_left(dest.tenant_id)
        except Exception as exc:  # noqa: BLE001 - без цифр базы пост лучше не удваивать на следующий день
            log.error("Клиент %s: лимит дня не прочитан (%s) - пост не публикую.", dest.tenant_id, type(exc).__name__)
            return False
        if not left:
            log.warning(
                "Клиент %s: дневной лимит %s постов исчерпан - пост пропущен, после полуночи снова пойдут.",
                dest.tenant_id,
                MAX_POSTS_PER_DAY,
            )
        return left

    async def _publish(self, dest: Destination, message: Message, text: str) -> Message:
        """Картинку несём по file_id, пост без картинки уходит в канал текстом - как есть, без разметки."""
        if message.photo:
            return await call(
                self.client.send_photo,
                dest.target,
                message.photo.file_id,
                caption=cut(text, self.cfg.caption_limit),
            )
        kind = other_media(message)
        if kind:
            log.info("Пост %s: %s по file_id не переносим - в канал пойдёт только текст.", message.id, kind)
        return await call(self.client.send_message, dest.target, cut(text, TEXT_LIMIT), parse_mode=ParseMode.DISABLED)

    async def _album(self, message: Message, dests: List[Destination]) -> None:
        group_id = message.media_group_id
        if group_id in self._seen_groups:
            log.debug("Альбом %s уже обработан - пропуск карточки.", group_id)
            return
        self._remember(self._seen_groups, group_id)

        log.info(
            "Пойман альбом из %s (id %s) - адресатов %s: %s.",
            chat_name(message.chat),
            message.id,
            len(dests),
            ", ".join(dest.label for dest in dests),
        )
        await asyncio.sleep(self.cfg.album_wait)

        items = await call(self.client.get_media_group, message.chat.id, message.id)
        photos = [item for item in items if item.photo]
        original = next((item.caption for item in items if (item.caption or "").strip()), "").strip()
        if not original:
            log.info("В альбоме %s нет текста - пропуск.", group_id)
            return

        author = self._drop_source(original, message.chat)
        rewritten = await self._rewritten(message, dests, author)

        if not photos:
            log.info("В альбоме %s нет фото - публикую его текст обычным постом.", group_id)
            for dest in dests:
                if not await self._allowed(dest):
                    continue
                posted = await self._publish(dest, message, self._text_for(dest, author, rewritten))
                await self._published(dest, posted, message)
                await self._boost(dest, posted)
            return

        log.info("Альбом: карточек %s, текстов %s символов.", len(photos[:TELEGRAM_ALBUM_LIMIT]), len(rewritten or author))

        for dest in dests:
            if not await self._allowed(dest):
                continue
            caption = cut(self._text_for(dest, author, rewritten), self.cfg.caption_limit)
            media: List[InputMediaPhoto] = [
                InputMediaPhoto(media=item.photo.file_id, caption=caption if index == 0 else "")
                for index, item in enumerate(photos[:TELEGRAM_ALBUM_LIMIT])
            ]
            posted = await call(self.client.send_media_group, dest.target, media)
            main_message = next((item for item in posted if (item.caption or "").strip()), posted[0])
            await self._published(dest, main_message, message)
            await self._boost(dest, main_message)

    async def _published(self, dest: Destination, posted: Message, source: Message) -> None:
        self.stats["published"] += 1
        self.last_publish = now()
        self._remember(self._sent_posts, post_key(source))
        log.info(
            "Опубликовано в %s (%s, id %s): %s.",
            dest.name,
            dest.label,
            posted.id,
            self._what_posted(posted),
        )
        if dest.tenant_id and self.store is not None:
            try:
                await self.store.count_post(dest.tenant_id)
                await self.store.note_publish(dest.tenant_id, posted.id)
            except Exception as exc:  # noqa: BLE001 - пост вышел, учёт допишем следующим
                log.error("Клиент %s: пост в учёт не записан (%s: %s).", dest.tenant_id, type(exc).__name__, exc)

    def _what_posted(self, message: Message) -> str:
        """Чем заполнен пост у нас в канале - по журналу видно, ушла картинка или только текст."""
        if message.photo:
            return "картинка с текстом"
        if (message.text or "").strip():
            return "текст без картинки"
        return "без текста и картинки"

    async def _boost(self, dest: Destination, message: Message) -> None:
        """Продвижение и реакции по настройкам адресата: у клиента своё число просмотров и свой флаг реакций."""
        if await self.promo.push(message, dest.views):
            self.stats["forwarded"] += 1
        if not dest.reactions:
            log.info("Реакции: у %s они выключены - пост в порядок накрутки не ставлю.", dest.label)
            return
        self.reactions.schedule(message, on_added=self._reactions_note(dest.tenant_id))

    def _reactions_note(self, tenant_id: int):
        """Замер сервиса по посту клиента идёт в базу: кабинет показывает число, а не «может быть»."""
        if not tenant_id or self.store is None:
            return None

        async def note(added: int) -> None:
            await self.store.note_reactions(tenant_id, added)

        return note

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

    def _unverifiable(self, message: Message) -> bool:
        """Текстовый пост, вышедший до старта этого процесса.

        После рерайта текст в канале не опознать, а в памяти отправленного нет:
        считаем такой пост доставленным - дубль в канале хуже пропущенного поста.
        """
        return (
            not message.photo
            and post_key(message) not in self._sent_posts
            and not (message.date and as_utc(message.date) > as_utc(self.started))
        )

    def _carried(self, message: Message, published: Set[str]) -> bool:
        """Дошёл ли пост до канала: картинку видно по file_unique_id, текст - по отправленному в этом запуске."""
        if post_key(message) in self._sent_posts:
            return True
        if message.photo:
            return message.photo.file_unique_id in published
        return self._unverifiable(message)

    def _not_carried(self, history: List[Message], published: Set[str]) -> List[Message]:
        return [
            message
            for message in history
            if post_text(message) and not self._carried(message, published)
        ]

    async def _missed(self, category: Category, limit: int = LOOKBACK) -> List[Message]:
        """Посты доноров, которых нет в канале: их съел сбой, пауза или перезапуск."""
        published = await self._published_ids(category, limit)
        found: List[Message] = []
        for donor in category.donors.values():
            try:
                history = await self._history(donor.chat_id, limit)
            except Exception as exc:  # noqa: BLE001 - доступ к донору мог пропасть
                log.warning("История %s не прочитана (%s) - догон её не берёт.", donor.name, type(exc).__name__)
                continue
            found += self._not_carried(history, published)
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
            lines.append(f"    настройки канала: {await self._reaction_settings(category.target)}")
            lines.append(f"    реакции в канале: {await self._channel_reactions(category)}")
            for donor in category.donors.values():
                lines.append(f"    {donor.name}: {await self._donor_line(donor.chat_id, published, limit)}")
        stats = self.stats
        lines.append(
            f"Итого: поймано {stats['caught']} · опубликовано {stats['published']} · сбоев {stats['errors']}"
            f" · последняя публикация {self.last_publish or 'ещё не было'}"
        )
        lines.append(self.reactions.report())
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

    async def _reaction_settings(self, chat) -> str:
        """Сервис ставит условие: в канале должны быть разрешены ВСЕ реакции - проверяем это."""
        try:
            info = await call(self.client.get_chat, chat)
        except Exception as exc:  # noqa: BLE001 - канал мог закрыться
            return f"настройки не открыть ({type(exc).__name__})"
        allowed = getattr(info, "available_reactions", None)
        if allowed is None:
            return "ограничений по реакциям Telegram не показывает"
        if getattr(allowed, "all_are_enabled", False):
            return "разрешены все реакции"
        emoji = [(getattr(item, "emoji", None) or "?") for item in (allowed.reactions or [])]
        if not emoji:
            return "в канале не разрешена ни одна реакция - накрутке не встать"
        shown = " ".join(emoji[:10])
        extra = f"… и ещё {len(emoji) - 10}" if len(emoji) > 10 else ""
        return f"разрешено только {len(emoji)} эмодзи ({shown}{extra}) - по условию сервиса нужно все"

    async def _channel_reactions(self, category: Category, show: int = 3) -> str:
        """Сколько реакций под свежими постами самого канала - ответ «где реакции» по кнопке."""
        try:
            history = await self._history(category.target, REACTIONS_LOOKBACK)
        except Exception as exc:  # noqa: BLE001 - канал мог закрыться для этого аккаунта
            return f"канал не открылся ({type(exc).__name__})"
        posts = [message for message in reversed(history) if post_text(message)][:show]
        if not posts:
            return "постов с текстом в канале нет"
        return " · ".join(f"пост {message.id}: {count_reactions(message)}" for message in posts)

    async def _donor_line(self, chat_id: int, published: Set[str], limit: int) -> str:
        try:
            history = await self._history(chat_id, limit)
        except Exception as exc:  # noqa: BLE001 - доступ к донору мог пропасть
            return f"не открылся ({type(exc).__name__})"
        if not history:
            return "сообщений нет"
        posts = [message for message in history if post_text(message)]
        missed = self._not_carried(history, published)
        blind = [message for message in posts if self._unverifiable(message)]
        stamp = self._ago(posts[-1] if posts else history[-1])
        label = "последний пост" if posts else "последнее сообщение"
        note = f" · текстовых до запуска не проверить: {len(blind)}" if blind else ""
        return f"{label} {stamp}, постов с текстом {len(posts)}, не дошло {len(missed)}{note}"

    def _ago(self, message: Message) -> str:
        date = getattr(message, "date", None)
        if not date:
            return "без даты"
        minutes = int((utc_now() - as_utc(date)).total_seconds() // 60)
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
            return "Пропущенного нет: всё свежее уже лежит в каналах."
        done = 0
        for category, message in todo[:limit]:
            dest = Destination(
                target=category.target,
                name=category.target_name,
                label=f"мой канал {category.target_name} (догон)",
                views=self.registry.views_for(category),
                reactions=self.registry.reactions_for(category),
                rewrite=self.registry.rewrite_for(category),
            )
            try:
                await self._single(message, [dest])
                done += 1
            except Exception as exc:  # noqa: BLE001 - один пост не должен рвать догон
                self.stats["errors"] += 1
                log.error("Догнать пост %s не вышло: %s: %s", message.id, type(exc).__name__, exc, exc_info=True)
        rest = len(todo) - done
        tail = f" Остальных {rest} не тронул - нажми ещё раз." if rest > 0 else ""
        return f"Догнал {done} пост(ов).{tail}"
