"""База: клиенты и каналы владельца - кто платит, кого читаем и куда постим.

Склад-группа в Telegram остаётся только для личного режима и как зеркало списка.
Когда клиентов несколько, источник истины - Postgres: там баланс, срок тарифа,
свой канал и свой список доноров на каждого. Каналы самого владельца живут здесь
же (owner_channels, owner_donors): группа с темами больше не список, она показывает
то, что уже лежит в базе.
"""

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Union

import asyncpg

log = logging.getLogger("store")

ChatRef = Union[int, str]

WEEK_RUB = 600
MONTH_RUB = 2000
PLANS = {
    "week": {"title": "Неделя", "days": 7, "price": WEEK_RUB},
    "month": {"title": "Месяц", "days": 30, "price": MONTH_RUB},
}

MAX_DONORS = 10
MAX_POSTS_PER_DAY = 50
MIN_TOPUP = 100
MAX_TOPUP = 300000

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})", re.I)
MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")
NUMERIC_ID_RE = re.compile(r"-100\d{8,16}")


def today(now: Optional[datetime.datetime] = None) -> datetime.date:
    return (now or datetime.datetime.now(datetime.timezone.utc)).date()


def ref_key(ref: ChatRef) -> str:
    """Одинаковый ключ для «@bankrollo», «bankrollo» и числа: база не хранит одно и то же дважды."""
    if isinstance(ref, int):
        return str(ref)
    return str(ref or "").strip().lstrip("@").lower()


def parse_ref(text: str) -> ChatRef:
    """Ссылка на канал из сообщения клиента: @юзернейм, t.me/... или числовой -100..."""
    raw = " ".join((text or "").split())
    for pattern in (LINK_RE, MENTION_RE):
        match = pattern.search(raw)
        if match:
            return match.group(1).lower()
    numeric = NUMERIC_ID_RE.search(raw)
    if numeric:
        return int(numeric.group(0))
    if USERNAME_RE.match(raw):
        return raw.lower()
    raise ValueError(f"Не понимаю ссылку «{raw}». Нужно @юзернейм канала или ссылка вида https://t.me/channel.")


@dataclass
class Tenant:
    id: int
    target: str = ""
    target_id: Optional[int] = None
    target_title: str = ""
    target_ok: bool = False
    views: int = 0
    views_max: Optional[int] = None
    reactions: bool = True
    rewrite: bool = True
    balance: int = 0
    plan_until: Optional[datetime.datetime] = None
    agreed: bool = False
    last_post_id: int = 0
    last_post_at: Optional[datetime.datetime] = None
    reactions_added: Optional[int] = None

    def active(self, now: Optional[datetime.datetime] = None) -> bool:
        if self.plan_until is None:
            return False
        return self.plan_until > (now or datetime.datetime.now(datetime.timezone.utc))

    def days_left(self, now: Optional[datetime.datetime] = None) -> int:
        if self.plan_until is None:
            return 0
        delta = self.plan_until - (now or datetime.datetime.now(datetime.timezone.utc))
        return max(0, delta.days)

    def price(self, kind: str) -> int:
        return PLANS[kind]["price"]

    def reactions_phrase(self) -> str:
        """Ответ про реакции по последнему заказу этого клиента - без догадок, по замеру."""
        if self.reactions_added is None:
            return "ждём первый замер"
        if self.reactions_added > 0:
            return f"по последнему заказу сервис добавил {self.reactions_added}"
        return "по последнему заказу сервис не добавил ничего"

    def last_post_phrase(self, now: Optional[datetime.datetime] = None) -> str:
        """Как давно у клиента вышел последний пост - главный признак «идёт или не идёт»."""
        if self.last_post_at is None:
            return "постов ещё не было"
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        minutes = int((moment - self.last_post_at).total_seconds() // 60)
        if minutes < 1:
            return "только что"
        if minutes < 60:
            return f"{minutes} мин назад"
        if minutes < 60 * 24:
            return f"{minutes // 60} ч назад"
        return f"{minutes // (60 * 24)} дн назад"

    def renew(self, kind: str, now: Optional[datetime.datetime] = None) -> int:
        """Сколько стоит продление и на какую дату отодвигается тариф.

        Если срок ещё не вышел, дни добавляются к нему, а не к сегодняшнему дню:
        человек, купивший месяц за две недели до конца, не теряет эти две недели.
        """
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        start = self.plan_until if (self.plan_until and self.plan_until > moment) else moment
        self.plan_until = start + datetime.timedelta(days=PLANS[kind]["days"])
        return PLANS[kind]["price"]


@dataclass
class Donor:
    ref: str
    chat_id: Optional[int] = None
    title: str = ""


@dataclass
class OwnerDonor:
    """Канал, откуда берём посты, - один ряд в базе для одного канала владельца.

    message_id - id сообщения-зеркала в теме группы: по нему источник убирается
    командой /убери, когда человек ответил на это сообщение.
    """

    channel_id: int
    chat_id: int
    id: int = 0
    ref: str = ""
    name: str = ""
    message_id: int = 0


@dataclass
class OwnerChannel:
    """Канал владельца: куда постим, какие настройки, откуда берём посты.

    id - число, которым канал знает панель (кнопки cat|<id>); при переносе из
    группы им остаётся id темы, поэтому старые кнопки не ломаются.
    topic_id - id темы-зеркала в группе, 0 - темы ещё нет.
    target_ref - канал, куда постить, вписанный человеком (@юзернейм или id). name при
    этом только название темы: переименование не должно менять адресата.
    """

    id: int
    name: str
    topic_id: int = 0
    target: Optional[int] = None
    target_name: str = ""
    target_ref: str = ""
    views: Optional[int] = None
    reactions: Optional[bool] = None
    rewrite: Optional[bool] = None
    donors: List[OwnerDonor] = field(default_factory=list)


class StoreError(Exception):
    pass


def check_topup(amount: int) -> int:
    """Рамку суммы держим одну и ту же и в кабинете, и в панели: иначе клиенту запрещают то, что можно себе."""
    if not MIN_TOPUP <= amount <= MAX_TOPUP:
        raise StoreError(f"Сумму нужно написать от {MIN_TOPUP} до {MAX_TOPUP} ₽.")
    return amount


class Store:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Store":
        if not dsn:
            raise StoreError("DATABASE_URL пуст - в Railway нужно подключить PostgreSQL.")
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
        store = cls(pool)
        await store._migrate()
        log.info("База клиентов подключена.")
        return store

    async def close(self) -> None:
        await self.pool.close()

    async def _migrate(self) -> None:
        async with self.pool.acquire() as con:
            await con.execute(SCHEMA)
            for statement in MIGRATIONS:
                await con.execute(statement)

    # ==== клиенты ====

    async def get_tenant(self, tenant_id: int) -> Optional[Tenant]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
        return _tenant(row) if row else None

    async def ensure_tenant(self, tenant_id: int) -> Tenant:
        async with self.pool.acquire() as con:
            await con.execute(
                "INSERT INTO tenants (id) VALUES ($1) ON CONFLICT (id) DO NOTHING",
                tenant_id,
            )
            row = await con.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
        return _tenant(row)

    async def list_tenants(self) -> List[Tenant]:
        async with self.pool.acquire() as con:
            rows = await con.fetch("SELECT * FROM tenants ORDER BY id")
        return [_tenant(row) for row in rows]

    async def set_target(self, tenant_id: int, target: str, target_id: Optional[int], title: str) -> None:
        """Канал меняется - отметку проверки сбрасываем: рабочий аккаунт должен убедиться заново."""
        async with self.pool.acquire() as con:
            await con.execute(
                """
                UPDATE tenants SET target = $2, target_id = $3, target_title = $4, target_ok = FALSE, updated_at = now()
                WHERE id = $1
                """,
                tenant_id,
                target,
                target_id,
                title,
            )

    async def set_agreed(self, tenant_id: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET agreed_at = now(), updated_at = now() WHERE id = $1 AND agreed_at IS NULL",
                tenant_id,
            )

    async def set_views(self, tenant_id: int, views: int, views_max: Optional[int] = None) -> None:
        """Просмотры клиента: одно число или диапазон; верхняя граница None - числа одно."""
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET views = $2, views_max = $3, updated_at = now() WHERE id = $1",
                tenant_id,
                views,
                views_max,
            )

    async def set_reactions(self, tenant_id: int, enabled: bool) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET reactions = $2, updated_at = now() WHERE id = $1",
                tenant_id,
                enabled,
            )

    async def set_rewrite(self, tenant_id: int, enabled: bool) -> None:
        """Переписывать ли посты клиента нейросетью: выключено - публикуем текст источника."""
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET rewrite = $2, updated_at = now() WHERE id = $1",
                tenant_id,
                enabled,
            )

    async def set_target_ok(self, tenant_id: int, ok: bool, title: str = "") -> None:
        """Отметка «доступ подтверждён»: её снимает проверка, а не человек, - иначе враньё в кабинете."""
        async with self.pool.acquire() as con:
            if title:
                await con.execute(
                    "UPDATE tenants SET target_ok = $2, target_title = $3, updated_at = now() WHERE id = $1",
                    tenant_id,
                    ok,
                    title,
                )
            else:
                await con.execute(
                    "UPDATE tenants SET target_ok = $2, updated_at = now() WHERE id = $1", tenant_id, ok
                )

    async def note_publish(self, tenant_id: int, message_id: int) -> None:
        """Пост вышел: помним номер и время - по «сколько назад» человек видит, что автопилот жив."""
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET last_post_id = $2, last_post_at = now(), updated_at = now() WHERE id = $1",
                tenant_id,
                message_id,
            )

    async def note_reactions(self, tenant_id: int, added: int) -> None:
        """Итог заказа реакций для этого клиента: сколько сервис добавил сверх прежнего числа."""
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET reactions_added = $2, updated_at = now() WHERE id = $1", tenant_id, added
            )

    # ==== деньги и тариф ====

    async def add_balance(self, tenant_id: int, amount: int, note: str = "") -> Tenant:
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET balance_rub = balance_rub + $2, updated_at = now() WHERE id = $1",
                tenant_id,
                amount,
            )
            await con.execute(
                "INSERT INTO payments (tenant_id, kind, amount_rub, days, note) VALUES ($1, 'topup', $2, 0, $3)",
                tenant_id,
                amount,
                note,
            )
            row = await con.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
        return _tenant(row)

    async def buy(self, tenant_id: int, kind: str) -> Tenant:
        """Списывает стоимость тарифа с баланса и продлевает срок. Баланса не хватило - StoreError."""
        if kind not in PLANS:
            raise StoreError(f"Нет такого тарифа: {kind}")
        price = PLANS[kind]["price"]
        tenant = await self.ensure_tenant(tenant_id)
        if tenant.balance < price:
            raise StoreError(f"На балансе {tenant.balance} ₽, а {PLANS[kind]['title']} стоит {price} ₽.")
        tenant.renew(kind)
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE tenants SET balance_rub = balance_rub - $2, plan_until = $3, updated_at = now() WHERE id = $1",
                tenant_id,
                price,
                tenant.plan_until,
            )
            await con.execute(
                "INSERT INTO payments (tenant_id, kind, amount_rub, days, note) VALUES ($1, $2, $3, $4, '')",
                tenant_id,
                kind,
                price,
                PLANS[kind]["days"],
            )
        log.info("Клиент %s купил «%s»: %s ₽, до %s.", tenant_id, PLANS[kind]["title"], price, tenant.plan_until)
        return tenant

    async def request_topup(self, tenant_id: int, amount: int) -> int:
        """Заявка на оплату: денег ещё не видели, но очередь на подтверждение уже стоит."""
        check_topup(amount)
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "INSERT INTO payments (tenant_id, kind, amount_rub, status, note) "
                "VALUES ($1, 'topup', $2, 'pending', '') RETURNING id",
                tenant_id,
                amount,
            )
        log.info("Клиент %s заявил оплату %s ₽ (заявка %s).", tenant_id, amount, row["id"])
        return int(row["id"])

    async def credit(self, tenant_id: int, amount: int, note: str = "начислено из панели") -> Tenant:
        """Начислить вручную: клиент перевёл без заявки, а сумму ту же надо проверить."""
        check_topup(amount)
        tenant = await self.add_balance(tenant_id, amount, note)
        log.info("Клиенту %s начислено %s ₽ вручную.", tenant_id, amount)
        return tenant

    async def list_payments(self, tenant_id: int, limit: int = 10) -> List[dict]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT kind, amount_rub, days, status, created_at FROM payments WHERE tenant_id = $1 "
                "ORDER BY id DESC LIMIT $2",
                tenant_id,
                limit,
            )
        return [dict(row) for row in rows]

    async def pending_topups(self, limit: int = 20) -> List[dict]:
        """Очередь на твоё подтверждение: человек заявил сумму, деньги ещё не видел никто."""
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT p.id, p.tenant_id, p.amount_rub, p.created_at, t.target, t.balance_rub "
                "FROM payments p JOIN tenants t ON t.id = p.tenant_id "
                "WHERE p.status = 'pending' ORDER BY p.id LIMIT $1",
                limit,
            )
        return [dict(row) for row in rows]

    async def confirm_topup(self, payment_id: int) -> dict:
        """Начислить: заявка закрывается одной строкой, сумма ложится на баланс ровно один раз."""
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "UPDATE payments SET status = 'done', note = 'начислено из панели' "
                "WHERE id = $1 AND status = 'pending' RETURNING tenant_id, amount_rub",
                payment_id,
            )
            if row is None:
                raise StoreError("Эта заявка уже разобрана - обнови экран.")
            await con.execute(
                "UPDATE tenants SET balance_rub = balance_rub + $2, updated_at = now() WHERE id = $1",
                row["tenant_id"],
                row["amount_rub"],
            )
        log.info("Клиенту %s начислено %s ₽ по заявке %s.", row["tenant_id"], row["amount_rub"], payment_id)
        return dict(row)

    async def reject_topup(self, payment_id: int) -> int:
        """Платежа не нашли: заявку гасим, денег не начисляем - клиент увидит статус в истории."""
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "UPDATE payments SET status = 'rejected', note = 'платёж не найден' "
                "WHERE id = $1 AND status = 'pending' RETURNING tenant_id",
                payment_id,
            )
        if row is None:
            raise StoreError("Этой заявки уже нет в очереди.")
        log.info("Заявка %s отклонена (клиент %s).", payment_id, row["tenant_id"])
        return int(row["tenant_id"])

    # ==== доноры ====
    async def list_donors(self, tenant_id: int) -> List[Donor]:
        async with self.pool.acquire() as con:
            rows = await con.fetch("SELECT ref, chat_id, title FROM donors WHERE tenant_id = $1 ORDER BY ref", tenant_id)
        return [Donor(row["ref"], row["chat_id"], row["title"] or "") for row in rows]

    async def add_donor(self, tenant_id: int, ref: ChatRef) -> Donor:
        key = ref_key(ref)
        if not key:
            raise StoreError("Пустая ссылка.")
        async with self.pool.acquire() as con:
            count = await con.fetchval("SELECT count(*) FROM donors WHERE tenant_id = $1", tenant_id)
            if count >= MAX_DONORS:
                raise StoreError(f"Больше {MAX_DONORS} каналов-источников нельзя - убери лишний.")
            await con.execute(
                "INSERT INTO donors (tenant_id, ref) VALUES ($1, $2) ON CONFLICT (tenant_id, ref) DO NOTHING",
                tenant_id,
                key,
            )
            row = await con.fetchrow("SELECT ref, chat_id, title FROM donors WHERE tenant_id = $1 AND ref = $2", tenant_id, key)
        return Donor(row["ref"], row["chat_id"], row["title"] or "")

    async def remove_donor(self, tenant_id: int, ref: ChatRef) -> None:
        async with self.pool.acquire() as con:
            await con.execute("DELETE FROM donors WHERE tenant_id = $1 AND ref = $2", tenant_id, ref_key(ref))

    async def bind_donor(self, ref: ChatRef, chat_id: int, title: str) -> int:
        """Рабочий аккаунт открыл канал по ссылке: запоминаем числовой id для всех, кто на него подписан."""
        async with self.pool.acquire() as con:
            result = await con.execute("UPDATE donors SET chat_id = $2, title = $3 WHERE ref = $1", ref_key(ref), chat_id, title)
        return int(result.split()[-1])

    async def donors_by_chat(self, chat_id: int) -> List[Tenant]:
        """Маршрут горячего пути: в каком канале мы донор и чьи настройки применять."""
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT t.* FROM tenants t JOIN donors d ON d.tenant_id = t.id
                WHERE d.chat_id = $1 AND t.target_ok AND t.target <> ''
                  AND t.plan_until IS NOT NULL AND t.plan_until > now()
                """,
                chat_id,
            )
        return [_tenant(row) for row in rows]

    async def active_donor_chats(self) -> List[int]:
        """Какие каналы ловить по-клиентски: те же условия, что у маршрута выше."""
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT DISTINCT d.chat_id FROM donors d JOIN tenants t ON t.id = d.tenant_id
                WHERE d.chat_id IS NOT NULL AND t.target_ok AND t.target <> ''
                  AND t.plan_until IS NOT NULL AND t.plan_until > now()
                """
            )
        return [int(row["chat_id"]) for row in rows]

    async def unbound_refs(self) -> List[str]:
        """Ссылки источников, которых ещё нет в Telegram: по ним аккаунт подписывается сам."""
        async with self.pool.acquire() as con:
            rows = await con.fetch("SELECT DISTINCT ref FROM donors WHERE chat_id IS NULL ORDER BY ref")
        return [row["ref"] for row in rows]

    async def targets_to_check(self) -> List[Tenant]:
        """Каналы клиентов, где доступ ещё не подтверждён: проверка ставит отметку, а не человек."""
        async with self.pool.acquire() as con:
            rows = await con.fetch("SELECT * FROM tenants WHERE target <> '' AND target_ok = FALSE ORDER BY id")
        return [_tenant(row) for row in rows]

    # ==== каналы владельца ====

    # Список каналов раньше жил только в темах группы. Теперь он здесь, а группа -
    # зеркало для глаз: её перечитывать не нужно, и правки в ней не властвуют.

    async def list_owner_channels(self) -> List[OwnerChannel]:
        """Все каналы владельца вместе с их источниками - одним проходом за рядами."""
        async with self.pool.acquire() as con:
            rows = await con.fetch("SELECT * FROM owner_channels ORDER BY id")
            donor_rows = await con.fetch("SELECT * FROM owner_donors ORDER BY id")
        channels = {int(row["id"]): _owner_channel(row) for row in rows}
        for row in donor_rows:
            channel = channels.get(int(row["channel_id"]))
            if channel is not None:
                channel.donors.append(_owner_donor(row))
        return list(channels.values())

    async def count_owner_channels(self) -> int:
        async with self.pool.acquire() as con:
            return int(await con.fetchval("SELECT count(*) FROM owner_channels") or 0)

    async def add_owner_channel(self, channel: OwnerChannel) -> OwnerChannel:
        """Запоминаем канал и возвращаем номер, под которым его знает база.

        При переносе из группы id задаём явно - это id темы, поэтому кнопки панели,
        которые человек нажимал раньше, продолжают вести к тому же каналу.
        """
        columns = (channel.name, channel.topic_id, channel.target, channel.target_name,
                   channel.target_ref, channel.views, channel.reactions, channel.rewrite)
        if channel.id:
            async with self.pool.acquire() as con:
                await con.execute(
                    "INSERT INTO owner_channels (id, name, topic_id, target, target_name, target_ref, "
                    "views, reactions, rewrite) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (id) DO NOTHING",
                    channel.id,
                    *columns,
                )
                # явный номер сбивает очередь собственной нумерации - следующий канал не должен получить тот же id
                await con.execute(
                    "SELECT setval(pg_get_serial_sequence('owner_channels', 'id'), "
                    "(SELECT MAX(id) FROM owner_channels))"
                )
            return channel

        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "INSERT INTO owner_channels (name, topic_id, target, target_name, target_ref, "
                "views, reactions, rewrite) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
                *columns,
            )
        channel.id = int(row["id"])
        return channel

    async def save_owner_channel(self, channel: OwnerChannel) -> None:
        """Название, канал-адресат, тема-зеркало и настройки - одним обновлением ряда."""
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE owner_channels SET name = $2, topic_id = $3, target = $4, target_name = $5, "
                "target_ref = $6, views = $7, reactions = $8, rewrite = $9, updated_at = now() WHERE id = $1",
                channel.id,
                channel.name,
                channel.topic_id,
                channel.target,
                channel.target_name,
                channel.target_ref,
                channel.views,
                channel.reactions,
                channel.rewrite,
            )

    async def remove_owner_channel(self, channel_id: int) -> None:
        """Вместе с каналом убираются и его источники - ряды связаны, мусор не остаётся."""
        async with self.pool.acquire() as con:
            await con.execute("DELETE FROM owner_channels WHERE id = $1", channel_id)

    async def add_owner_donor(self, donor: OwnerDonor) -> OwnerDonor:
        """Источник канала: повторная та же ссылка не плодит ряд, а обновляет название."""
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "INSERT INTO owner_donors (channel_id, chat_id, ref, name, message_id) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (channel_id, chat_id) DO UPDATE SET ref = EXCLUDED.ref, name = EXCLUDED.name, "
                "message_id = CASE WHEN EXCLUDED.message_id > 0 THEN EXCLUDED.message_id "
                "ELSE owner_donors.message_id END "
                "RETURNING id, message_id",
                donor.channel_id,
                donor.chat_id,
                donor.ref,
                donor.name,
                donor.message_id,
            )
        donor.id = int(row["id"])
        donor.message_id = int(row["message_id"] or 0)
        return donor

    async def save_owner_donor(self, donor: OwnerDonor) -> None:
        """Нужно, когда зеркальное сообщение источника появилось уже после записи ряда."""
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE owner_donors SET ref = $2, name = $3, chat_id = $4, message_id = $5 WHERE id = $1",
                donor.id,
                donor.ref,
                donor.name,
                donor.chat_id,
                donor.message_id,
            )

    async def remove_owner_donor(self, donor_id: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute("DELETE FROM owner_donors WHERE id = $1", donor_id)

    async def owner_state(self, key: str, default: str = "") -> str:
        """Одиночные настройки сервиса: список перенесён в базу, просмотры по умолчанию."""
        async with self.pool.acquire() as con:
            row = await con.fetchrow("SELECT value FROM owner_state WHERE key = $1", key)
        return row["value"] if row else default

    async def set_owner_state(self, key: str, value: str) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "INSERT INTO owner_state (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                key,
                value,
            )

    # ==== лимит дня ====

    async def posts_today(self, tenant_id: int) -> int:
        async with self.pool.acquire() as con:
            value = await con.fetchval(
                "SELECT posts FROM day_usage WHERE tenant_id = $1 AND day = $2",
                tenant_id,
                today(),
            )
        return int(value or 0)

    async def count_post(self, tenant_id: int) -> int:
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO day_usage (tenant_id, day, posts) VALUES ($1, $2, 1)
                ON CONFLICT (tenant_id, day) DO UPDATE SET posts = day_usage.posts + 1
                RETURNING posts
                """,
                tenant_id,
                today(),
            )
        return int(row["posts"])

    async def quota_left(self, tenant_id: int) -> bool:
        return await self.posts_today(tenant_id) < MAX_POSTS_PER_DAY


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id BIGINT PRIMARY KEY,
    target TEXT NOT NULL DEFAULT '',
    target_id BIGINT,
    target_title TEXT NOT NULL DEFAULT '',
    target_ok BOOLEAN NOT NULL DEFAULT FALSE,
    views INT NOT NULL DEFAULT 0,
    views_max INT,
    reactions BOOLEAN NOT NULL DEFAULT TRUE,
    rewrite BOOLEAN NOT NULL DEFAULT TRUE,
    balance_rub INT NOT NULL DEFAULT 0,
    plan_until TIMESTAMPTZ,
    agreed_at TIMESTAMPTZ,
    last_post_id BIGINT,
    last_post_at TIMESTAMPTZ,
    reactions_added INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS donors (
    tenant_id BIGINT NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    ref TEXT NOT NULL,
    chat_id BIGINT,
    title TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, ref)
);
CREATE INDEX IF NOT EXISTS donors_chat_id_idx ON donors (chat_id);

CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    amount_rub INT NOT NULL,
    days INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'done',
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS day_usage (
    tenant_id BIGINT NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    day DATE NOT NULL,
    posts INT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

CREATE TABLE IF NOT EXISTS owner_channels (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    topic_id BIGINT NOT NULL DEFAULT 0,
    target BIGINT,
    target_name TEXT NOT NULL DEFAULT '',
    target_ref TEXT NOT NULL DEFAULT '',
    views INT,
    reactions BOOLEAN,
    rewrite BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS owner_donors (
    id BIGSERIAL PRIMARY KEY,
    channel_id BIGINT NOT NULL REFERENCES owner_channels (id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    message_id BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_id, chat_id)
);
CREATE INDEX IF NOT EXISTS owner_donors_chat_id_idx ON owner_donors (chat_id);

CREATE TABLE IF NOT EXISTS owner_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


# Колонки, которых не было в первой версии схемы. IF NOT EXISTS переживает любой
# возраст базы: старая база доживёт до них сама, при следующем запуске.
MIGRATIONS = (
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS last_post_id BIGINT",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS last_post_at TIMESTAMPTZ",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS reactions_added INT",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS rewrite BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS views_max INT",
    "ALTER TABLE owner_channels ADD COLUMN IF NOT EXISTS target_ref TEXT NOT NULL DEFAULT ''",
)


def _owner_channel(row) -> OwnerChannel:
    return OwnerChannel(
        id=int(row["id"]),
        name=row["name"] or "",
        topic_id=int(row["topic_id"] or 0),
        target=row["target"],
        target_name=row["target_name"] or "",
        target_ref=row["target_ref"] or "",
        views=row["views"],
        reactions=row["reactions"],
        rewrite=row["rewrite"],
    )


def _owner_donor(row) -> OwnerDonor:
    return OwnerDonor(
        channel_id=int(row["channel_id"]),
        chat_id=int(row["chat_id"]),
        id=int(row["id"]),
        ref=row["ref"] or "",
        name=row["name"] or "",
        message_id=int(row["message_id"] or 0),
    )


def _tenant(row) -> Tenant:
    return Tenant(
        id=int(row["id"]),
        target=row["target"] or "",
        target_id=row["target_id"],
        target_title=row["target_title"] or "",
        target_ok=bool(row["target_ok"]),
        views=int(row["views"] or 0),
        views_max=int(row["views_max"]) if row["views_max"] is not None else None,
        reactions=bool(row["reactions"]),
        rewrite=bool(row["rewrite"]),
        balance=int(row["balance_rub"] or 0),
        plan_until=row["plan_until"],
        agreed=row["agreed_at"] is not None,
        last_post_id=int(row["last_post_id"] or 0),
        last_post_at=row["last_post_at"],
        reactions_added=row["reactions_added"],
    )
