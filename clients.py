"""Обслуживание клиентов: подписаться на их источники и проверить их каналы.

Человек присылает две ссылки и ждёт, а рабочий аккаунт делает всё сам: подписывается
на канал-источник, запоминает его числовой id и проверяет, можно ли публиковать в
канал клиента. Отметку «доступ подтверждён» ставит только эта проверка - кабинету
врать незачем, человек должен видеть, что есть на самом деле.

Здесь же живой список каналов, которые ловим по-клиентски: фильтр сообщений не может
каждый раз ходить в базу, поэтому набор держим в памяти и обновляем после синхронизации.
"""

import asyncio
import datetime
import logging
from typing import List, Set

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait

from registry import DONOR_TYPES
from utils import call, chat_name

log = logging.getLogger("clients")

WRITING_STATUSES = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
SYNC_SECONDS = 300
JOIN_LIMIT = 5  # подписок за один проход: их Telegram считает за активные действия


def display_name(chat, fallback: str = "") -> str:
    """Название для человека: сначала название канала, потом @ссылка, потом то, что прислали.

    chat_name() в проекте отвечает за служебные подписи в панели, где @юзернейм точнее.
    В кабинете эти строки читает клиент, и «Хоккей» выглядит лучше, чем «@hockey_ch».
    """
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
    if title:
        return str(title)
    if getattr(chat, "username", None):
        return chat_name(chat)
    return fallback or chat_name(chat)


class Clients:
    def __init__(self, client, store):
        self.client = client
        self.store = store
        self.chats: Set[int] = set()
        self.problems: List[str] = []
        self.last_sync: str = ""
        self.tenants_total = 0
        self.tenants_active = 0

    async def sync(self) -> str:
        """Один проход по всей клиентской части: подписки, проверки каналов, список ловли."""
        joined = await self.subscribe()
        checked = await self.check_targets()
        self.chats = set(await self.store.active_donor_chats())
        tenants = await self.store.list_tenants()
        self.tenants_total = len(tenants)
        self.tenants_active = sum(1 for tenant in tenants if tenant.active())
        self.last_sync = datetime.datetime.now().strftime("%H:%M:%S")
        report = f"подписок: {joined} · каналов подтверждено: {checked} · ловим каналов: {len(self.chats)}"
        if self.problems:
            report += f" · проблем: {len(self.problems)}"
        log.info("Клиенты: %s.", report)
        return report

    async def subscribe(self) -> int:
        """Источники, которых аккаунт ещё не видит: подписываемся и запоминаем числовой id."""
        bound = 0
        refs = await self.store.unbound_refs()
        for ref in refs[:JOIN_LIMIT]:
            chat = await self._open(ref)
            if chat is None:
                continue
            if getattr(chat, "type", None) not in DONOR_TYPES:
                self._fail(ref, f"это {getattr(chat.type, 'name', chat.type)}, а не канал")
                continue
            await self.store.bind_donor(ref, chat.id, display_name(chat, ref))
            log.info("Клиент: подписались на %s (id %s).", display_name(chat, ref), chat.id)
            bound += 1
        return bound

    async def _open(self, ref: str):
        """Открыть канал: сначала как есть, потом через подписку - не состоя в нём, поста не увидишь."""
        try:
            return await call(self.client.get_chat, ref)
        except Exception as exc:  # noqa: BLE001 - причина известна: аккаунт в канал не входит
            log.info("Клиент: канал %s не открылся (%s) - пробую подписаться.", ref, type(exc).__name__)

        for attempt in (1, 2):
            try:
                # join_chat зовём напрямую: обёртка call при лимите уснула бы на его минуты,
                # а подписки - как раз то, что Telegram ограничивает. Нам проще отложить канал.
                result = await self.client.join_chat(ref)
            except FloodWait as exc:
                wait = int(getattr(exc, "value", 0) or 0)
                self._fail(ref, f"Telegram просит паузу {wait} сек на подписку")
                log.warning("Клиент: подписку на %s сдвигаем - Telegram просит %s сек.", ref, wait)
                return None
            except Exception as exc:  # noqa: BLE001 - канал может быть закрыт или ссылка битая
                if attempt == 2:
                    self._fail(ref, f"подписаться не вышло ({type(exc).__name__})")
                    return None
                await asyncio.sleep(2)
                continue
            chat = getattr(result, "chat", None)
            if chat is not None:
                return chat
            try:
                return await call(self.client.get_chat, ref)
            except Exception as exc:  # noqa: BLE001 - подписались, но канал не отдаётся
                self._fail(ref, f"канал не открылся после подписки ({type(exc).__name__})")
                return None
        return None

    async def check_targets(self) -> int:
        """Каналы клиентов без подтверждения: смотрим права и ставим честную отметку."""
        waiting = await self.store.targets_to_check()
        if not waiting:
            return 0
        # лишний запрос к Telegram на пустом месте не нужен: он и выдавал отзыв сессии
        me = await call(self.client.get_me)
        confirmed = 0
        for tenant in waiting:
            ref = tenant.target_id or tenant.target
            try:
                member = await call(self.client.get_chat_member, ref, me.id)
            except Exception as exc:  # noqa: BLE001 - клиент ещё не добавил аккаунта
                log.info("Клиент %s: канал %s не открылся (%s) - жду прав администратора.", tenant.id, ref, type(exc).__name__)
                continue
            status = getattr(member, "status", None)
            allowed = status in WRITING_STATUSES and getattr(member, "can_send_messages", True) is not False
            peer = getattr(member, "chat", None)
            title = display_name(peer, tenant.target) if peer is not None else ""
            await self.store.set_target_ok(tenant.id, allowed, title)
            if allowed:
                confirmed += 1
                self.forget(tenant.target)  # права появились - прежняя жалоба больше не честная
                log.info("Клиент %s: доступ к каналу %s подтверждён - посты пойдут.", tenant.id, ref)
            else:
                self._fail(tenant.target, f"аккаунт не админ канала ({getattr(status, 'name', status)})")
                log.warning("Клиент %s: в канале %s аккаунт не админ (%s) - публикаций не будет.", tenant.id, ref, getattr(status, "name", status))
        return confirmed

    def _fail(self, ref: str, reason: str) -> None:
        """Одна строчка проблем для панели: человек видит, что именно мешает, а не «что-то не так»."""
        line = f"{ref}: {reason}"
        if line not in self.problems:
            self.problems.append(line)
        while len(self.problems) > 20:
            self.problems.pop(0)

    def report(self) -> str:
        """Короткий ответ для панели: сколько кабинетов, когда проверяли и что не открывается."""
        head = f"Кабинеты: всего {self.tenants_total}, с действующим тарифом {self.tenants_active}"
        if not self.last_sync:
            return f"{head}: проверку ещё не выполняли"
        lines = [f"{head}: последняя проверка {self.last_sync}, ловим {len(self.chats)} канал(ов)"]
        lines += [f"    ⚠ {problem}" for problem in self.problems[:5]]
        return "\n".join(lines)

    def forget(self, ref: str) -> None:
        """Проблема решена (канал открылся, права появились) - убираем её из списка."""
        key = str(ref or "").lower()
        self.problems = [line for line in self.problems if not line.lower().startswith(key)]
