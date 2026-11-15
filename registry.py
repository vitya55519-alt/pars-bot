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
        elif self._forums:
            if len(self._forums) > 1:
                names = ", ".join(chat_name(chat) for chat in self._forums)
                raise RegistryError(f"Несколько групп с темами: {names}. Укажи MANAGEMENT_CHAT.")
            chat = self._forums[0]
        else:
            raise RegistryError(
                "Среди чатов нет группы с темами. Создай группу, включи в ней «Темы» и добавь туда этот аккаунт."
            )

        if not getattr(chat, "is_forum", False):
            raise RegistryError(f"В группе «{chat_name(chat)}» не включены темы: Изменить группу -> Темы.")

        self.group = chat.id
        self.group_id = chat.id
        self.group_name = chat_name(chat)

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
        removed: List[Donor] = []
        async with self.lock:
            for category in self.categories.values():
                for chat_id, donor in list(category.donors.items()):
                    if donor.message_id != message.id:
                        continue
                    del category.donors[chat_id]
                    self.routes.pop(chat_id, None)
                    removed.append(donor)
        return removed

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
