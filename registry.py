"""Реестр доноров живёт в форум-группе: тема = категория, название темы = целевой канал.

Своих файлов и базы нет - источник истины только Telegram, поэтому после
перезапуска Railway список восстанавливается одним rebuild().
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Union

from pyrogram.enums import ChatMemberStatus, ChatType

from utils import call, chat_name, norm_title

log = logging.getLogger("registry")

ChatRef = Union[int, str]

GENERAL_TOPIC_ID = 1
TOPIC_NAME_LIMIT = 50
MANAGER_STATUSES = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
PUBLIC_LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})", re.I)
PRIVATE_LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/c/(\d{4,20})", re.I)
INVITE_LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)\S+", re.I)
MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")
NUMERIC_ID_RE = re.compile(r"(?<!\d)(-100\d{8,16})(?!\d)")
BARE_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
DONOR_TYPES = (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP)


class RegistryError(Exception):
    pass


def extract_refs(text: str) -> List[ChatRef]:
    """Ссылки на чаты из текста: @юзернейм, t.me/..., t.me/c/..., числовой -100... ID."""
    cleaned = INVITE_LINK_RE.sub(" ", text or "")
    refs: List[ChatRef] = []
    for pattern, cast in (
        (PRIVATE_LINK_RE, lambda raw: int(f"-100{raw}")),
        (PUBLIC_LINK_RE, lambda raw: raw.lower()),
        (MENTION_RE, lambda raw: raw.lower()),
        (NUMERIC_ID_RE, int),
    ):
        for match in pattern.finditer(cleaned):
            ref = cast(match.group(1))
            if ref not in refs:
                refs.append(ref)
    return refs


@dataclass
class Donor:
    chat_id: int
    name: str
    link: str
    message_id: int


@dataclass
class Category:
    topic_id: int
    name: str
    donors: Dict[int, Donor] = field(default_factory=dict)
    target: Optional[ChatRef] = None
    target_name: str = ""
    problem: str = ""

    @property
    def donor_names(self) -> List[str]:
        return [donor.name for donor in self.donors.values()]

    def describe(self) -> str:
        where = self.target_name or self.problem or "канал не определён"
        donors = ", ".join(self.donor_names) or "нет доноров"
        return f"Тема «{self.name}» -> {where}\n    доноры: {donors}"


class Registry:
    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.categories: Dict[int, Category] = {}
        self.routes: Dict[int, int] = {}
        self.target_ids: Set[int] = set()
        self.group: Optional[ChatRef] = cfg.management_chat or None
        self.group_id: Optional[int] = None
        self.group_name = ""
        self.paused = False
        self.ready = False
        self.last_error = ""
        self.unreadable: List[str] = []
        self.suppressed: Set[int] = set()
        self._titles: Dict[str, ChatRef] = {}
        self._forums: List = []
        self._general_hint_shown = False

    # ==== сборка списка из группы ====

    async def rebuild(self) -> str:
        async with self.lock:
            await self._scan_dialogs()
            await self._pick_group()
            self.target_ids.clear()
            self.unreadable.clear()
            self.suppressed.clear()

            categories: Dict[int, Category] = {}
            async for topic in self.client.get_forum_topics(self.group_id):
                topic_id = int(getattr(topic, "id", 0) or 0)
                if topic_id and topic_id != GENERAL_TOPIC_ID:
                    categories[topic_id] = Category(topic_id, topic.name or f"Тема {topic_id}")

            for category in categories.values():
                await self._read_topic(category)
            for category in categories.values():
                await self._resolve_target(category)

            self.categories = categories
            self._reindex()
            self.ready = True
            self.last_error = ""
            log.info(
                "Группа управления: %s (id %s), тем: %s.\n%s",
                self.group_name,
                self.group_id,
                len(categories),
                self.summary(),
            )
            return self.summary()

    async def _scan_dialogs(self) -> None:
        titles: Dict[str, ChatRef] = {}
        forums = []
        async for dialog in self.client.get_dialogs():
            chat = dialog.chat
            if getattr(chat, "is_forum", False):
                forums.append(chat)
            if getattr(chat, "type", None) in DONOR_TYPES:
                key = norm_title(getattr(chat, "title", "") or "")
                if key:
                    titles.setdefault(key, chat.id)
        self._titles = titles
        self._forums = forums

    async def _pick_group(self) -> None:
        if self.group:
            try:
                chat = await call(self.client.get_chat, self.group)
            except Exception as exc:  # noqa: BLE001 - понятная причина в тексте ошибки
                raise RegistryError(
                    f"Группа {self.group} не открылась ({type(exc).__name__}). "
                    "Проверь, что этот аккаунт состоит в группе."
                ) from exc
        elif not self._forums:
            raise RegistryError(
                "Среди чатов нет группы с темами. Создай группу, включи в ней «Темы» и добавь туда этот аккаунт."
            )
        elif len(self._forums) == 1:
            chat = self._forums[0]
        else:
            # аккаунт может состоять в чужих группах с темами - своя группа та, где он владелец
            owned = [forum for forum in self._forums if await self._owns(forum.id)]
            if len(owned) == 1:
                chat = owned[0]
            else:
                found = ", ".join(f"«{chat_name(forum)}» (id {forum.id})" for forum in self._forums)
                raise RegistryError(
                    f"Несколько групп с темами: {found}. Своя группа - та, где ты владелец; "
                    "или впиши её id в MANAGEMENT_CHAT."
                )

        if not getattr(chat, "is_forum", False):
            raise RegistryError(f"В группе «{chat_name(chat)}» не включены темы: Изменить группу -> Темы.")

        self.group = chat.id
        self.group_id = chat.id
        self.group_name = chat_name(chat)

    async def _owns(self, chat_id: int) -> bool:
        try:
            member = await call(self.client.get_chat_member, chat_id, (await self.client.get_me()).id)
        except Exception:  # noqa: BLE001 - не смогли проверить права = не считаем своей
            return False
        return member.status == ChatMemberStatus.OWNER

    async def _read_topic(self, category: Category) -> None:
        # id темы в форуме совпадает с id её верхнего сообщения, поэтому история читаем через GetReplies
        async for message in self.client.get_discussion_replies(
            self.group_id, category.topic_id, limit=self.cfg.history_limit
        ):
            if getattr(message, "service", False):
                continue
            text = message.text or message.caption or ""
            if text:
                await self._absorb(text, message.id, category)

    async def _absorb(self, text: str, message_id: int, category: Category) -> List[Donor]:
        added: List[Donor] = []
        for ref in extract_refs(text):
            donor = await self._open(ref, message_id)
            if donor is None or donor.chat_id in category.donors:
                continue
            category.donors[donor.chat_id] = donor
            added.append(donor)
        return added

    async def _open(self, ref: ChatRef, message_id: int) -> Optional[Donor]:
        try:
            chat = await call(self.client.get_chat, ref)
        except Exception as exc:  # noqa: BLE001 - одна битая ссылка не ломает весь список
            self.unreadable.append(str(ref))
            log.warning("Ссылка %s не открылась (%s) - подпишись на канал или используй @юзернейм.", ref, type(exc).__name__)
            return None

        chat_type = getattr(chat, "type", None)
        if chat_type not in DONOR_TYPES:
            log.warning("%s - это %s, донором быть не может.", ref, getattr(chat_type, "name", chat_type))
            return None
        return Donor(chat.id, chat_name(chat), str(ref), message_id)

    async def _resolve_target(self, category: Category) -> None:
        ref = self._target_ref(category.name)
        if ref is None:
            category.target = None
            category.problem = f"нет канала с названием «{category.name}»"
            return
        try:
            chat = await call(self.client.get_chat, ref)
        except Exception as exc:  # noqa: BLE001
            category.problem = f"канал {ref} не открылся ({type(exc).__name__})"
            return
        category.target = chat.id
        category.target_name = chat_name(chat)
        self.target_ids.add(chat.id)

    def _target_ref(self, name: str) -> Optional[ChatRef]:
        refs = extract_refs(name)
        if refs:
            return refs[0]
        match_title = self._titles.get(norm_title(name))
        if match_title is not None:
            return match_title
        if BARE_USERNAME_RE.match(name.strip()):
            return name.strip().lower()
        return None

    def _reindex(self) -> None:
        routes: Dict[int, int] = {}
        for category in self.categories.values():
            for chat_id in category.donors:
                if chat_id == self.group_id or chat_id in self.target_ids:
                    continue
                routes[chat_id] = category.topic_id
        self.routes = routes

    # ==== живые изменения ====

    async def note(self, message) -> List[Donor]:
        """Новое сообщение в группе: ссылки в теме сразу становятся донорами."""
        text = message.text or ""
        if not text or not self.ready or not extract_refs(text):
            return []

        topic_id = int(getattr(message, "message_thread_id", 0) or 0)
        category = self.categories.get(topic_id)
        if category is None:
            if topic_id == GENERAL_TOPIC_ID and not self._general_hint_shown:
                self._general_hint_shown = True
                log.info("Ссылки в теме «General» не разбираются - заводи тему с названием канала.")
            return []

        async with self.lock:
            added = await self._absorb(text, message.id, category)
            for donor in added:
                self.routes[donor.chat_id] = category.topic_id
        return added

    async def forget(self, message) -> List[Donor]:
        """Команда /убери ответом на сообщение со ссылкой."""
        return await self.forget_ids({message.id})

    async def forget_ids(self, message_ids: Set[int]) -> List[Donor]:
        removed: List[Donor] = []
        async with self.lock:
            for category in self.categories.values():
                for chat_id, donor in list(category.donors.items()):
                    if donor.message_id not in message_ids:
                        continue
                    del category.donors[chat_id]
                    self.routes.pop(chat_id, None)
                    removed.append(donor)
        return removed

    # ==== действия по кнопкам из бота управления ====

    async def add_category(self, name: str) -> Category:
        """Новая категория = новая тема с названием целевого канала."""
        clean = " ".join((name or "").split())[:TOPIC_NAME_LIMIT]
        if not clean:
            raise RegistryError("Нужно название канала, например «Спорт» или Спорт @sport_ch.")
        self._need_group()
        if self.categories:
            for category in self.categories.values():
                if norm_title(category.name) == norm_title(clean):
                    raise RegistryError(f"Канал «{category.name}» уже есть в списке.")

        async with self.lock:
            topic = await call(self.client.create_forum_topic, self.group_id, clean)
            topic_id = int(getattr(topic, "id", 0) or getattr(topic, "message_thread_id", 0) or 0)
            if not topic_id:
                raise RegistryError("Telegram не прислал id новой темы - повтори позже.")
            category = Category(topic_id, clean)
            self.categories[topic_id] = category
            await self._resolve_target(category)
            self._reindex()
        return category

    async def rename_category(self, topic_id: int, name: str) -> Category:
        clean = " ".join((name or "").split())[:TOPIC_NAME_LIMIT]
        category = self._category(topic_id)
        if not clean:
            raise RegistryError("Новое название пустое.")

        async with self.lock:
            await call(self.client.edit_forum_topic, self.group_id, topic_id, clean)
            old = category.target
            category.name = clean
            category.problem = ""
            await self._resolve_target(category)
            if old and old not in (c.target for c in self.categories.values()):
                self.target_ids.discard(old)
            self._reindex()
        return category

    async def add_donor(self, topic_id: int, text: str) -> List[Donor]:
        """Пишем ссылку в тему - и она сразу становится донором."""
        category = self._category(topic_id)
        refs = extract_refs(text or "")
        if not refs:
            raise RegistryError("Это не ссылка на канал. Нужен @юзернейм или https://t.me/...")

        posted = await call(self.client.send_message, self.group_id, text, message_thread_id=topic_id)
        async with self.lock:
            added = await self._absorb(text, posted.id, category)
            for donor in added:
                self.routes[donor.chat_id] = topic_id
        if not added:
            await call(self.client.delete_messages, self.group_id, [posted.id])
            raise RegistryError(
                f"Канал {', '.join(str(ref) for ref in refs)} не открылся: "
                "подпишись на него этим аккаунтом или возьми точный @юзернейм."
            )
        return added

    async def remove_donor(self, message_id: int) -> List[Donor]:
        removed = await self.forget_ids({message_id})
        if not removed:
            raise RegistryError("Этого донора в списке уже нет - нажми «Обновить».")
        self.suppressed.add(message_id)
        try:
            await call(self.client.delete_messages, self.group_id, [message_id])
        except Exception as exc:  # noqa: BLE001 - вычеркнули, а удалить не дали права
            self.suppressed.discard(message_id)
            log.warning("Сообщение %s в группе удалить не удалось (%s).", message_id, type(exc).__name__)
        return removed

    def _need_group(self) -> None:
        if not self.group_id:
            raise RegistryError(
                "Группы с темами нет. Создай группу, включи в ней «Темы» и добавь туда этот аккаунт."
            )

    def _category(self, topic_id: int) -> Category:
        self._need_group()
        category = self.categories.get(topic_id)
        if category is None:
            raise RegistryError("Такой темы уже нет - нажми «Обновить».")
        return category

    # ==== чтение ====

    def route(self, chat_id: int) -> Optional[Category]:
        if self.paused:
            return None
        topic_id = self.routes.get(chat_id)
        return self.categories.get(topic_id) if topic_id else None

    async def may_manage(self, message) -> bool:
        if not self.group_id or message.chat.id != self.group_id or not message.from_user:
            return False
        try:
            member = await self.client.get_chat_member(self.group_id, message.from_user.id)
        except Exception:  # noqa: BLE001 - не смогли проверить права = не управляем
            return False
        return member.status in MANAGER_STATUSES

    @property
    def donor_count(self) -> int:
        return len(self.routes)

    def summary(self) -> str:
        if not self.categories:
            return "В группе пока нет тем с донорами."
        return "\n".join(f"  {category.describe()}" for category in self.categories.values())

    def problems(self) -> List[str]:
        found = [category.problem for category in self.categories.values() if category.problem]
        found += [f"тема «{c.name}» без доноров" for c in self.categories.values() if not c.donors]
        if self.unreadable:
            found.append("не открылись ссылки: " + ", ".join(sorted(set(self.unreadable))))
        return found

    def took_out(self, message_id: int) -> bool:
        """Удаление по кнопке не должно запускать перечитывание всей группы."""
        if message_id not in self.suppressed:
            return False
        self.suppressed.discard(message_id)
        return True
