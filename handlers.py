"""Поймал пост донора -> переписал текст -> опубликовал в свой канал.

Картинки и видео переносим по file_id (ничего не скачиваем), альбомы - вместе,
сохраняя порядок фотографий и роликов. Видео публикуем и без подписи, не
отправляя пустой текст нейросети. У документов и остальных неподдержанных
носителей переносим только текст, о чём пишем в журнал. Фото без подписи,
как прежде, пропускаем. Если донор - группа, а не канал, ловим только посты
самого чата: реплики подписчиков в канал не идут.
"""

import asyncio
import datetime
import logging
import random
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Union

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChatForwardsRestricted
from pyrogram.handlers import MessageHandler
from pyrogram.types import InputMediaPhoto, InputMediaVideo, Message

from ai import cut
from commands import Controls
from promo import Promoter
from reactions import Reactor, count_reactions, post_link
from smmplus import order as smm_order, roll_reactions
from registry import Category
from store import today
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
OTHER_MEDIA = ("animation", "document", "audio", "voice", "video_note", "sticker")


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
    # верх границы диапазона: ставим на каждый пост случайное число от views до views_max
    views_max: Optional[int] = None
    # моя тема (0 - не моя): лимит постов в сутки считается именно по ней
    topic_id: int = 0
    post_limit: Optional[int] = None
    reactions: bool = True
    rewrite: bool = True
    tenant_id: int = 0


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def post_text(message: Message) -> str:
    """Текст поста: подпись под картинкой или сам текст, если пост без картинки."""
    return (message.caption or message.text or "").strip()


def post_media(message: Message):
    """Переносимый файл поста: фотография или обычный видеоролик."""
    return message.photo or getattr(message, "video", None)


def has_content(message: Message) -> bool:
    """Текст или видео без подписи; прежний пропуск пустых фотографий сохраняем."""
    return bool(post_text(message) or getattr(message, "video", None))


def other_media(message: Message) -> str:
    """Неподдержанный носитель: документ, анимация, голос. Переносим только его текст."""
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
        # счётчик постов в сутки для моих тем без базы: {(topic_id, день): число}
        self._topic_usage: Dict[tuple, int] = {}
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
        has_post = (filters.photo | filters.video | filters.text | filters.caption) & ~filters.service
        self.client.add_handler(MessageHandler(self.on_message, is_donor & has_post & ~filters.outgoing))
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
        posts = [message for message in history if has_content(message)]
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
                        views_max=self.registry.views_max_for(category),
                        topic_id=category.topic_id,
                        post_limit=self.registry.posts_for(category),
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
            views_max=getattr(tenant, "views_max", None),
            post_limit=getattr(tenant, "post_limit", None),
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
        if not has_content(message):
            log.info("В посте нет текста или видео - пропуск.")
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
        if not author:
            return ""
        if not self.cfg.ai_enabled:
            # REWRITE=0 - общий выключатель: ни одного обращения к MiniMax, даже если
            # у канала переписывание осталось включённым с прежних времён.
            log.info("Нейросеть выключена переменной REWRITE - публикуем текст автора без запросов к ИИ.")
            return ""
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
        """Суточный лимит: у клиента - свой или общий, у моей темы - выбранный в настройках."""
        if dest.tenant_id and self.store is not None:
            try:
                used, cap = await self.store.quota_state(dest.tenant_id)
            except Exception as exc:  # noqa: BLE001 - без цифр базы пост лучше не удваивать на следующий день
                log.error("Клиент %s: лимит дня не прочитан (%s) - пост не публикую.", dest.tenant_id, type(exc).__name__)
                return False
            if cap and used >= cap:
                log.warning(
                    "Клиент %s: дневной лимит %s постов исчерпан (вышло %s) - пост пропущен,"
                    " после полуночи по Москве снова пойдут.",
                    dest.tenant_id,
                    cap,
                    used,
                )
                return False
        if dest.topic_id and dest.post_limit:
            used = await self.topic_posts_used(dest.topic_id)
            if used is None:
                return False
            if used >= dest.post_limit:
                log.warning(
                    "%s: выбрано не больше %s постов в сутки - сегодня уже вышло %s, этот пост пропускаю."
                    " После полуночи по Москве счётчик обнулится.",
                    dest.label,
                    dest.post_limit,
                    used,
                )
                return False
        return True

    async def topic_posts_used(self, topic_id: int) -> Optional[int]:
        """Сколько постов тема выложила сегодня. None - счётчик не прочитан."""
        if self.store is not None:
            try:
                return await self.store.topic_posts_today(topic_id)
            except Exception as exc:  # noqa: BLE001 - без счётчика лучше пропустить пост, чем выложить двойню
                self.stats["errors"] += 1
                log.error("Тема %s: счётчик дня не прочитан (%s) - пост не публикую.", topic_id, type(exc).__name__)
                return None
        return self._topic_usage.get((topic_id, today()), 0)

    async def _bump_topic(self, topic_id: int) -> None:
        """Учёт обнуляется по московской полуночи и живёт в базе: перезапуск день не сбрасывает."""
        if self.store is not None:
            try:
                await self.store.count_topic_post(topic_id)
                return
            except Exception as exc:  # noqa: BLE001 - пост вышел, счётчик догоним памятью
                log.error("Тема %s: пост в счётчик дня не записан (%s: %s).", topic_id, type(exc).__name__, exc)
        self._topic_usage[(topic_id, today())] = self._topic_usage.get((topic_id, today()), 0) + 1
        if len(self._topic_usage) > 400:
            fresh = {key: value for key, value in self._topic_usage.items() if key[1] == today()}
            self._topic_usage.clear()
            self._topic_usage.update(fresh)

    async def _publish(self, dest: Destination, message: Message, text: str) -> Message:
        """Фото и видео несём по file_id вместе с подписью, текстовый пост - без разметки."""
        video = getattr(message, "video", None)
        if video:
            return await call(
                self.client.send_video,
                dest.target,
                video.file_id,
                caption=cut(text, self.cfg.caption_limit),
                parse_mode=ParseMode.DISABLED,
                has_spoiler=getattr(message, "has_media_spoiler", None),
            )
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
        media_items = [item for item in items if post_media(item)][:TELEGRAM_ALBUM_LIMIT]
        original = next((item.caption for item in items if (item.caption or "").strip()), "").strip()
        if not original and not any(getattr(item, "video", None) for item in media_items):
            log.info("В альбоме %s нет текста или видео - пропуск.", group_id)
            return

        author = self._drop_source(original, message.chat)
        rewritten = await self._rewritten(message, dests, author)

        if len(media_items) < 2:
            # Telegram не принимает альбом из одного файла: отправляем его обычным постом.
            source = media_items[0] if media_items else message
            for dest in dests:
                if not await self._allowed(dest):
                    continue
                posted = await self._publish(dest, source, self._text_for(dest, author, rewritten))
                await self._published(dest, posted, message)
                await self._boost(dest, posted)
            return

        log.info(
            "Альбом: файлов %s (видео %s), текстов %s символов.",
            len(media_items),
            sum(bool(getattr(item, "video", None)) for item in media_items),
            len(rewritten or author),
        )

        for dest in dests:
            if not await self._allowed(dest):
                continue
            caption = cut(self._text_for(dest, author, rewritten), self.cfg.caption_limit)
            media: List[Union[InputMediaPhoto, InputMediaVideo]] = []
            for index, item in enumerate(media_items):
                media_type = InputMediaVideo if getattr(item, "video", None) else InputMediaPhoto
                media.append(media_type(
                    media=post_media(item).file_id,
                    caption=caption if index == 0 else "",
                    parse_mode=ParseMode.DISABLED,
                    has_spoiler=getattr(item, "has_media_spoiler", None),
                ))
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
        if dest.topic_id:
            await self._bump_topic(dest.topic_id)
        if dest.tenant_id and self.store is not None:
            try:
                await self.store.count_post(dest.tenant_id)
                await self.store.note_publish(dest.tenant_id, posted.id)
            except Exception as exc:  # noqa: BLE001 - пост вышел, учёт допишем следующим
                log.error("Клиент %s: пост в учёт не записан (%s: %s).", dest.tenant_id, type(exc).__name__, exc)

    def _what_posted(self, message: Message) -> str:
        """По журналу видно, что ушло в канал: видео, картинка или только текст."""
        if getattr(message, "video", None):
            return "видео с текстом" if (message.caption or "").strip() else "видео без подписи"
        if message.photo:
            return "картинка с текстом"
        if (message.text or "").strip():
            return "текст без картинки"
        return "без текста и картинки"

    def _roll_views(self, dest: Destination) -> Optional[int]:
        """Диапазон просмотров даёт на каждый пост своё случайное число от нижней границы к верхней."""
        if dest.views and dest.views_max and dest.views_max > dest.views:
            value = random.randint(dest.views, dest.views_max)
            log.info("%s: просмотры на этот пост - %s (диапазон %s-%s).", dest.label, value, dest.views, dest.views_max)
            return value
        return dest.views

    async def _boost(self, dest: Destination, message: Message) -> None:
        """Продвижение и реакции по настройкам адресата: у клиента своё число просмотров и свой флаг реакций."""
        if await self.promo.push(message, self._roll_views(dest)):
            self.stats["forwarded"] += 1
        if not dest.reactions:
            log.info("Реакции: у %s они выключены - пост в порядок накрутки не ставлю.", dest.label)
            return
        if self.cfg.smmplus_key:
            link = post_link(message)
            if not link:
                log.info("Реакции: у канала нет публичной ссылки - сервис не увидит пост, пропускаю.")
                return
            qty = roll_reactions(self.cfg)
            if await smm_order(self.reactions.session, self.cfg, link, qty):
                self.stats["reactions"] = self.stats.get("reactions", 0) + 1
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
        """Фото и видео, которые уже лежат в нашем канале: по ним проверяем доставку."""
        return {
            post_media(message).file_unique_id
            for message in await self._history(category.target, limit)
            if post_media(message)
        }

    def _unverifiable(self, message: Message) -> bool:
        """Текстовый пост, вышедший до старта этого процесса.

        После рерайта текст в канале не опознать, а в памяти отправленного нет:
        считаем такой пост доставленным - дубль в канале хуже пропущенного поста.
        """
        return (
            not post_media(message)
            and post_key(message) not in self._sent_posts
            and not (message.date and as_utc(message.date) > as_utc(self.started))
        )

    def _carried(self, message: Message, published: Set[str]) -> bool:
        """Фото и видео опознаём по file_unique_id, текст - по отправленному в этом запуске."""
        if post_key(message) in self._sent_posts:
            return True
        media = post_media(message)
        if media:
            return media.file_unique_id in published
        return self._unverifiable(message)

    def _not_carried(self, history: List[Message], published: Set[str]) -> List[Message]:
        return [
            message
            for message in history
            if has_content(message) and not self._carried(message, published)
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
        posts = [message for message in reversed(history) if has_content(message)][:show]
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
        posts = [message for message in history if has_content(message)]
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
                views_max=self.registry.views_max_for(category),
                topic_id=category.topic_id,
                post_limit=self.registry.posts_for(category),
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
