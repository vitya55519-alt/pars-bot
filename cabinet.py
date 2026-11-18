"""Личный кабинет клиента в том же боте.

Владелец аккаунта видит своё меню, все остальные - кабинет: откуда брать
посты, куда их выкладывать, переписывать ли текст, сколько просмотров, идут
ли реакции. Всё живёт в базе, поэтому между перезапусками бот ничего не
должен помнить сам.

Тексты читаются как инструкция: нажал кнопку - получил, что сделать по шагам и
что будет дальше. Служебных слов («склад», «тема», «донор») человек не видит:
для него канал-источник и канал, куда посты ложатся.
"""

import datetime
import logging
from typing import Dict, List, Optional, Tuple

import legal
from ai import cut
from config import VIEWS_LIMIT, parse_views
from store import MAX_DONORS, MAX_POSTS_PER_DAY, MIN_TOPUP, PLANS, StoreError, Tenant, parse_ref
from utils import TEXT_LIMIT, call, markup

log = logging.getLogger("cabinet")

# 0 - не ошибка ввода, а осознанный выбор человека: просмотры не накручивать
VIEWS_PRESETS = (0, 500, 1000, 2000, 5000)

# порядок пошагового запуска: один экран - один шаг, «Дальше» ведёт по ним
STEPS = ("donors", "channel", "text", "views", "plans")
STEP_TITLES = {
    "donors": "Откуда брать посты",
    "channel": "Куда публиковать",
    "text": "Текст поста: переписывать или нет",
    "views": "Просмотры на пост",
    "plans": "Тариф и оплата",
}

# куда возвращаем человека, если он ошибся вводом
SCREENS = {"donor": "donors", "channel": "channel", "views": "views", "sum": "plans"}


def date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "?"


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def ref_word(ref) -> str:
    """Как показать ссылку человеку: юзернейм - с собачкой, числовой id - как есть."""
    value = str(ref or "")
    if not value:
        return "не указан"
    return value if value.startswith("-") else f"@{value}"


def plan_line(tenant: Tenant, now: datetime.datetime) -> str:
    if not tenant.active(now):
        return "⛔ не подключён - посты не публикуются"
    return f"✅ активен ещё {tenant.days_left(now)} дн. (до {date(tenant.plan_until)})"


def channel_line(tenant: Tenant) -> str:
    if not tenant.target:
        return "⛔ не указан"
    return f"{ref_word(tenant.target)} - {'✅ доступ подтверждён' if tenant.target_ok else '⏳ проверяю доступ'}"


def views_line(tenant: Tenant) -> str:
    return f"{tenant.views} на каждый пост" if tenant.views else "не накручиваем (0)"


def add_row(buttons, row: List[Tuple[str, str]]):
    """Дописать ряд кнопок к готовому экрану: так пошаговый запуск не дублирует тексты шагов."""
    rows = [[(button.text, button.callback_data) for button in line] for line in (buttons.inline_keyboard or [])]
    rows.append(list(row))
    return markup(*rows)


class Cabinet:
    SHOW = {
        "chome": "home",
        "cstart": "wizard",
        "cdonors": "donors",
        "cchannel": "channel",
        "ctext": "text",
        "cviews": "views",
        "cplans": "plans",
        "cresult": "result",
        "cguide": "guide",
        "cboost": "reactions",
    }

    def __init__(self, client, cfg, store):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.awaiting: Dict[int, Tuple[str, int]] = {}
        self.notice: Dict[int, str] = {}
        self.wizard: Dict[int, int] = {}

    # ==== вход ====

    async def on_message(self, message) -> None:
        user_id = message.chat.id
        text = (message.text or "").strip()
        kind, _ = self.awaiting.pop(user_id, ("", 0))
        if kind and text and not text.startswith("/"):
            await self._apply_text(message, kind, text)
            return

        body, buttons = await self._view(user_id, await self._entry(user_id))
        await call(message.reply, cut(body, TEXT_LIMIT), reply_markup=buttons)

    async def on_button(self, query) -> None:
        try:
            await self._route(query)
        except (StoreError, ValueError) as exc:
            self._note(query.from_user.id, f"⚠ {exc}")
            await self._show(query, self._back_to(query))
        except Exception as exc:  # noqa: BLE001 - кнопка всегда должна чем-то ответить
            log.error("Кабинет: кнопка «%s» не сделалась: %s: %s", query.data, type(exc).__name__, exc)
            await call(query.answer, f"Ошибка: {type(exc).__name__}", show_alert=True)

    async def _entry(self, user_id: int) -> str:
        """Новичок попадает сразу в пошаговый запуск, у настроенного - обычный кабинет."""
        tenant = await self.store.ensure_tenant(user_id)
        donors = await self.store.list_donors(user_id)
        return "wizard" if self._next_step(tenant, donors, now_utc()) == 0 else "home"

    # ==== разбор кнопок ====

    async def _route(self, query) -> None:
        user_id = query.from_user.id
        parts = (query.data or "").split("|")
        key = parts[0]
        arg = parts[1] if len(parts) > 1 else ""

        if key == "cviews":
            # раньше SHOW: у готовых кнопок числа есть arg, и он обязан примениться, а не открыть экран
            if arg:
                await self._apply(user_id, "views", arg)
            else:
                self.awaiting[user_id] = ("views", 0)
            await self._show(query, "views")
        elif key in self.SHOW:
            await self._show(query, self.SHOW[key])
        elif key == "cset_target":
            self.awaiting[user_id] = ("channel", 0)
            await self._show(query, "target")
        elif key == "cadd":
            self.awaiting[user_id] = ("donor", 0)
            await self._show(query, "add")
        elif key == "cnext":
            self.wizard[user_id] = await self._step(user_id) + 1
            await self._show(query, "wizard")
        elif key == "ctopup":
            tenant = await self.store.ensure_tenant(user_id)
            if not tenant.agreed:
                await self._show(query, "agree")
                return
            self.awaiting[user_id] = ("sum", 0)
            await self._show(query, "sum")
        elif key == "cdoc":
            await self._send_document(query, int(arg or 0))
        elif key == "cdel":
            await self._remove_donor(query, int(arg or 0))
        elif key == "creactions":
            await self._toggle_reactions(query)
        elif key == "cretext":
            await self._toggle_rewrite(query)
        elif key == "cbuy":
            await self._buy(query, arg)
        elif key == "cagree":
            await self.store.set_agreed(user_id)
            self._note(user_id, "✅ Спасибо, условия приняты.")
            await self._show(query, "plans")
        else:
            await call(query.answer, "Такой кнопки нет - нажми «⬅️ Кабинет».")

    async def _send_document(self, query, kind: int) -> None:
        """Документ приходит отдельными сообщениями: длинный текст не влезает в одно, а терять куски нельзя."""
        title, pages = legal.document(kind)
        await call(query.answer, title)
        for index, page in enumerate(pages):
            text = f"{title}\n\n{page}" if index == 0 else page
            buttons = markup([("⬅️ Кабинет", "chome")]) if index == len(pages) - 1 else None
            await call(self.client.send_message, query.message.chat.id, cut(text, TEXT_LIMIT), reply_markup=buttons)

    async def _remove_donor(self, query, position: int) -> None:
        donors = await self.store.list_donors(query.from_user.id)
        if not 1 <= position <= len(donors):
            self._note(query.from_user.id, "⚠ Этот канал уже убран.")
        else:
            donor = donors[position - 1]
            await self.store.remove_donor(query.from_user.id, donor.ref)
            self._note(query.from_user.id, f"✅ Убрал {ref_word(donor.ref)}.")
        await self._show(query, "donors")

    async def _toggle_reactions(self, query) -> None:
        tenant = await self.store.ensure_tenant(query.from_user.id)
        await self.store.set_reactions(tenant.id, not tenant.reactions)
        self._note(
            tenant.id,
            "✨ Реакции включены: каждый новый пост канала встаёт в очередь на бесплатный заказ."
            if not tenant.reactions
            else "✨ Реакции выключены - заказы не отправляю, посты публикуются как раньше.",
        )
        await self._show(query, "reactions")

    async def _toggle_rewrite(self, query) -> None:
        tenant = await self.store.ensure_tenant(query.from_user.id)
        turned_on = not tenant.rewrite
        await self.store.set_rewrite(tenant.id, turned_on)
        if not turned_on:
            self._note(tenant.id, "✏️ Выключил: пост выходит теми же словами, что написал автор канала-источника.")
        elif self.cfg.ai_enabled:
            self._note(tenant.id, "✏️ Включил: каждый пост переписывается своими словами - он не совпадает с оригиналом.")
        else:
            self._note(
                tenant.id,
                "✏️ Переписывание включено и сохранено. Сейчас нейросеть выключена общей "
                "настройкой на стороне сервиса, поэтому пост выходит словами автора.",
            )
        await self._show(query, "text")

    async def _buy(self, query, kind: str) -> None:
        if kind not in PLANS:
            await call(query.answer, "Такого тарифа нет.", show_alert=True)
            return
        tenant = await self.store.ensure_tenant(query.from_user.id)
        if not tenant.agreed:
            await self._show(query, "agree")
            return
        if not tenant.target:
            self._note(tenant.id, "⚠ Сначала укажи канал, куда постить - шаг 2.")
            await self._show(query, "channel")
            return
        await self.store.buy(tenant.id, kind)
        price = PLANS[kind]["price"]
        self._note(tenant.id, f"✅ {PLANS[kind]['title']} подключён за {price} ₽.")
        await self._show(query, "home", flash=f"-{price} ₽")

    # ==== ввод текстом ====

    async def _apply_text(self, message, kind: str, text: str) -> None:
        user_id = message.chat.id
        try:
            await self._apply(user_id, kind, text)
            screen = SCREENS.get(kind, "home")
        except (StoreError, ValueError) as exc:
            self._note(user_id, f"⚠ {exc}")
            screen = SCREENS.get(kind, "home")
        except Exception as exc:  # noqa: BLE001 - ввод клиента не роняет бота
            log.error("Кабинет: действие из текста упало: %s: %s", type(exc).__name__, exc)
            self._note(user_id, f"⚠ Ошибка: {type(exc).__name__}")
            screen = "home"

        body, buttons = await self._view(user_id, screen)
        await call(message.reply, cut(body, TEXT_LIMIT), reply_markup=buttons)

    async def _apply(self, user_id: int, kind: str, text: str) -> None:
        if kind == "channel":
            ref = self._public_ref(text)
            await self.store.set_target(user_id, str(ref), ref if isinstance(ref, int) else None, "")
            self._note(
                user_id,
                f"✅ Канал {ref_word(ref)} записал. Проверю, пускает ли рабочего аккаунта,"
                "\nи поставлю отметку в «📊 Результат» - обычно это пара минут.",
            )
        elif kind == "donor":
            donor = await self.store.add_donor(user_id, parse_ref(text))
            self._note(
                user_id,
                f"✅ Добавил {ref_word(donor.ref)}: буду читать этот канал.\n"
                "Бот подпишется сам и начнёт ловить каждый новый пост этого канала.",
            )
        elif kind == "views":
            number = parse_views(text, low=0)
            await self.store.set_views(user_id, number)
            self._note(
                user_id,
                f"✅ Просмотры: {number} на каждый пост."
                if number
                else "✅ Просмотры не накручиваем - посты будут выходить без них.",
            )
        elif kind == "sum":
            request_id = await self.store.request_topup(user_id, self._rubles(text))
            self._note(
                user_id,
                f"✅ Заявка №{request_id} принята. Посмотрю платёж и начислю сумму на баланс -"
                "\nтогда и купишь тариф.",
            )

    @staticmethod
    def _public_ref(text: str):
        """Ссылка на канал клиента: закрытый адрес вида t.me/c/... не годится - по нему сервисы пост не найдут."""
        if "t.me/c/" in (text or "").lower():
            raise ValueError(
                "Это закрытый канал - по такой ссылке ни промо-бот, ни реакции пост не увидят.\n"
                "Открой свой канал → Изменить → Публичная ссылка и пришли адрес вида @name или https://t.me/name."
            )
        return parse_ref(text)

    @staticmethod
    def _rubles(text: str) -> int:
        digits = "".join(char for char in text if char.isdigit())
        if not digits:
            raise ValueError(f"Напиши сумму цифрами, например 600 - от {MIN_TOPUP} ₽.")
        return int(digits)

    # ==== пошаговый запуск ====

    @staticmethod
    def _next_step(tenant: Tenant, donors: List, now: datetime.datetime) -> int:
        """С какого шага начинать: просмотры и переписывание - не преграда, там всё уже выбрано по умолчанию."""
        if not donors:
            return 0
        if not tenant.target:
            return 1
        if not tenant.active(now):
            return STEPS.index("plans")
        return len(STEPS)

    async def _step(self, user_id: int) -> int:
        tenant = await self.store.ensure_tenant(user_id)
        donors = await self.store.list_donors(user_id)
        return self.wizard.get(user_id, self._next_step(tenant, donors, now_utc()))

    # ==== экраны ====

    async def _view(self, user_id: int, kind: str) -> Tuple[str, object]:
        tenant = await self.store.ensure_tenant(user_id)
        now = now_utc()

        if kind == "wizard":
            return await self._wizard_view(user_id, tenant, now)

        if kind == "donors":
            return await self._donors_view(user_id, tenant)

        if kind == "add":
            return self._notice(
                user_id,
                "Пришли ссылку на канал-источник.\n\n"
                "Где её взять: открой нужный канал, нажми на его название - в адресе будет\n"
                "@name или https://t.me/name. Скопируй и пришли сюда одним сообщением.\n\n"
                f"Сколько можно: до {MAX_DONORS} каналов. Бот подпишется на них сам, и через\n"
                "пару минут начнёт ловить каждый новый пост этого канала.",
            ), markup([("⬅️ К списку каналов", "cdonors")])

        if kind == "channel":
            return self._notice(
                user_id,
                "Что сделать:\n"
                "1. Добавь рабочего аккаунта в администраторы своего канала:\n"
                f"   {self.worker}\n"
                "   Нужны два права: публиковать сообщения и оставлять реакции.\n"
                "2. Нажми «📤 Указать мой канал» и пришли ссылку на него:\n"
                "   @name или https://t.me/name.\n\n"
                "Как поймёшь, что всё готово:\n"
                "Появится «✅ доступ подтверждён» - с этого момента каждый новый пост из\n"
                "каналов-источников выходит в твоём канале. Проверка занимает пару минут.\n\n"
                f"Сейчас: {channel_line(tenant)}",
            ), markup(
                [("📤 Указать мой канал", "cset_target")],
                [("📊 Результат", "cresult")],
                [("⬅️ Кабинет", "chome")],
            )

        if kind == "target":
            return self._notice(
                user_id,
                "Пришли ссылку на свой канал: @name или https://t.me/name.\n\n"
                "Канал должен быть публичным - с именем. Закрытая ссылка вида t.me/c/... не\n"
                "подойдёт: по ней ни промо-бот, ни сервис реакций пост не найдут.\n"
                "Публичную ссылку задают в настройках канала: Изменить → Публичная ссылка.\n\n"
                f"Сейчас: {channel_line(tenant)}",
            ), markup([("⬅️ К шагу 2", "cchannel")])

        if kind == "text":
            return self._notice(
                user_id,
                "Что сделать:\n"
                "Выбери одним переключателем ниже, что делать с текстом чужого поста.\n\n"
                "Как это работает:\n"
                "• Переписывать - бот просит нейросеть написать тот же смысл другими\n"
                "   словами. Пост перестаёт совпадать с оригиналом, картинка остаётся та же.\n"
                "• Словами автора - бот публикует текст точно как в канале-источнике.\n\n"
                "Что выбрать:\n"
                "Если не хочешь, чтобы в твоём канале лежали чужие тексты слово в слово,\n"
                "оставь переписывание. Если важен оригинальный текст - выключи.\n\n"
                f"Сейчас: {self._text_line(tenant)}",
            ), markup(
                [self._text_cell(tenant)],
                [("📊 Результат", "cresult")],
                [("⬅️ Кабинет", "chome")],
            )

        if kind == "views":
            presets = [
                [("не накручивать (0)" if count == 0 else str(count), f"cviews|{count}") for count in VIEWS_PRESETS[:3]],
                [(str(count), f"cviews|{count}") for count in VIEWS_PRESETS[3:]],
            ]
            return self._notice(
                user_id,
                "Сколько хочешь, столько и ставь.\n\n"
                "Что сделать:\n"
                f"1. Напиши одним числом, сколько просмотров добавлять к каждому посту - от 0\n"
                f"   до {VIEWS_LIMIT}. Ноль значит «просмотры не накручивать».\n"
                "   Или выбери готовое кнопкой.\n\n"
                "Что будет дальше:\n"
                "Сразу после публикации поста бот отправляет это число боту продвижения.\n"
                "Сколько назвал - столько и будет на каждом новом посте. Менять можно в\n"
                "любой момент, число остаётся в кабинете.\n\n"
                f"Сейчас: {views_line(tenant)}",
            ), markup(*presets, [("📊 Результат", "cresult")], [("⬅️ Кабинет", "chome")])

        if kind == "reactions":
            return self._notice(
                user_id,
                "Что делает: бесплатно ставит реакции под каждый новый пост твоего канала.\n\n"
                "Обязательное условие:\n"
                "В настройках канала должны быть разрешены ВСЕ реакции. Если часть эмодзи\n"
                "запрещена, сервис не поставит ни одной - это настройка канала, изменить её\n"
                "бот не может: канал → Изменить → Реакции.\n\n"
                "Как устроено:\n"
                "Заказы идут раз в 5 минут; если сервис отказывает словами про лимит\n"
                "или час, бот сам делает паузу до часа. В очереди держится один самый\n"
                "свежий пост канала. Сам заказ идёт около 6 минут: столько сервис требует,\n"
                "чтобы страница была открыта.\n\n"
                "Как проверить, что работает:\n"
                "Бот считает реакции до заказа и после, поэтому в «📊 Результат» видно\n"
                "конкретное число - добавил сервис что-то или нет.\n\n"
                f"Сейчас: {'реакции включены' if tenant.reactions else 'реакции выключены'}."
                f"\n{tenant.reactions_phrase()}",
            ), markup(
                [self._reactions_cell(tenant)],
                [("📊 Результат", "cresult")],
                [("⬅️ Кабинет", "chome")],
            )

        if kind == "result":
            return await self._result_view(user_id, tenant, now)

        if kind == "guide":
            return self._notice(
                user_id,
                "Что делает каждая кнопка\n\n"
                "🚀 Пошаговый запуск - пять шагов по порядку, от ссылок до тарифа.\n"
                f"1️⃣ Откуда брать посты - каналы, которые читаем. До {MAX_DONORS} штук.\n"
                "2️⃣ Куда публиковать - твой канал и как добавить рабочего аккаунта.\n"
                "3️⃣ Текст поста - переписывать чужой текст своими словами или публиковать как есть.\n"
                f"4️⃣ Просмотры - число от 0 до {VIEWS_LIMIT} на каждый пост. 0 - не накручивать.\n"
                "5️⃣ Тариф - цена, баланс, оплата и срок подписки.\n"
                "✨ Реакции - бесплатная накрутка реакций, включается и выключается.\n"
                "📊 Результат - видно, работает ли всё сейчас и что нажать, если нет.\n"
                "📄 Соглашение и 🔒 Политика - документы сервиса, читаются прямо в боте.\n\n"
                f"Рабочий аккаунт, которого нужно добавить в администраторы: {self.worker}\n\n"
                "Дальше ничего нажимать не нужно: посты выходят сами, как только у источника\n"
                "появляется новый текст. Проверить - кнопка «📊 Результат».",
            ), markup(
                [("🚀 Пошаговый запуск", "cstart")],
                [("📊 Результат", "cresult")],
                [("⬅️ Кабинет", "chome")],
            )

        if kind == "plans":
            return self._notice(
                user_id,
                "Сколько стоит\n\n"
                f"• Неделя - {PLANS['week']['price']} ₽ ({PLANS['week']['days']} дней)\n"
                f"• Месяц - {PLANS['month']['price']} ₽ ({PLANS['month']['days']} дней) - выходит дешевле\n\n"
                "Что входит в один кабинет:\n"
                f"свой канал, до {MAX_DONORS} каналов-источников, до {MAX_POSTS_PER_DAY} постов в сутки,\n"
                f"просмотры до {VIEWS_LIMIT} на пост и бесплатные реакции.\n\n"
                f"На балансе: {tenant.balance} ₽\n"
                f"Тариф: {plan_line(tenant, now)}\n\n"
                "Как платить:\n"
                "1. «Пополнить баланс» - напиши сумму цифрами и переведи по реквизитам.\n"
                "2. Я проверю платёж и положу деньги на баланс.\n"
                "3. «Купить неделю» или «Купить месяц» - сумма спишется с баланса.\n"
                "Дни текущей подписки не сгорают: покупка добавляется к остатку.",
            ), markup(
                [(f"Купить неделю {PLANS['week']['price']} ₽", "cbuy|week")],
                [(f"Купить месяц {PLANS['month']['price']} ₽", "cbuy|month")],
                [("💳 Пополнить баланс", "ctopup")],
                [("📄 Соглашение", "cdoc|1"), ("🔒 Политика", "cdoc|2")],
                [("📊 Результат", "cresult")],
                [("⬅️ Кабинет", "chome")],
            )

        if kind == "sum":
            return self._notice(
                user_id,
                "Напиши, сколько перевёл - одним числом, например 600.\n\n"
                f"{self.cfg.payment_details or 'Реквизиты пришлю здесь же - нажми «🆘 Помощь».'}\n\n"
                f"Меньше {MIN_TOPUP} ₽ заявку не делаю. Начислю на баланс, как только увижу платёж,\n"
                "и напишу тебе в этот чат.",
            ), markup([("🆘 Помощь", "cguide")], [("⬅️ Кабинет", "chome")])

        if kind == "agree":
            return (
                "Перед оплатой - документы сервиса.\n"
                "Прочитай их кнопками ниже: соглашение и политика конфиденциальности.",
                markup(
                    [("📄 Соглашение", "cdoc|1"), ("🔒 Политика", "cdoc|2")],
                    [("✅ Принимаю условия", "cagree")],
                    [("⬅️ Кабинет", "chome")],
                ),
            )

        return await self._home_view(user_id, tenant, now)

    # ==== отдельные экраны ====

    async def _home_view(self, user_id: int, tenant: Tenant, now: datetime.datetime) -> Tuple[str, object]:
        donors = await self.store.list_donors(user_id)
        bound = sum(1 for donor in donors if donor.chat_id)
        ready = bool(donors) and bool(tenant.target) and tenant.active(now)
        lines = [
            "🚀 АвтоПост - твой канал на автопилоте",
            "",
            "Бот читает каналы, которые ты укажешь, и выкладывает их посты в твой канал.",
            f"Текст поста: {self._text_line(tenant)}. Следом добавляет просмотры и реакции.",
            "Переписывание, просмотры и реакции включаются и выключаются по отдельности.",
            "",
            f"1️⃣ Откуда брать посты: {len(donors)} из {MAX_DONORS} (читаю {bound})",
            f"2️⃣ Куда публиковать: {channel_line(tenant)}",
            f"3️⃣ Текст поста: {self._text_line(tenant)}",
            f"4️⃣ Просмотры: {views_line(tenant)}",
            f"5️⃣ Тариф: {plan_line(tenant, now)}",
        ]
        payments = await self.store.list_payments(user_id, 3)
        if payments:
            lines.append("")
            lines.append("Последнее по оплате:")
            lines += [f"• {self._payment_line(row)}" for row in payments]
        lines += [
            "",
            "Нажми «🚀 Пошаговый запуск» - проведу по шагам по очереди."
            if not ready
            else "Всё настроено - посты идут сами. «📊 Результат» покажет, что происходит.",
        ]
        rows = (
            [[("🚀 Пошаговый запуск", "cstart")]]
            if not ready
            else [[("📊 Результат", "cresult"), self._reactions_cell(tenant)]]
        )
        return self._notice(user_id, "\n".join(lines)), markup(
            *rows,
            [("1️⃣ Откуда брать посты", "cdonors"), ("2️⃣ Куда публиковать", "cchannel")],
            [("3️⃣ Текст поста", "ctext"), ("4️⃣ Просмотры", "cviews")],
            [("5️⃣ Тариф", "cplans")],
            [("🆘 Помощь", "cguide")],
            [("📄 Соглашение", "cdoc|1"), ("🔒 Политика", "cdoc|2")],
        )

    def _payment_line(self, row: dict) -> str:
        title = PLANS.get(row.get("kind"), {}).get("title", "Пополнение")
        status = "" if row.get("status") == "done" else " (ждёт проверки)"
        return f"{title} {row.get('amount_rub')} ₽ {date(row.get('created_at'))}{status}".strip()

    async def _donors_view(self, user_id: int, tenant: Tenant) -> Tuple[str, object]:
        donors = await self.store.list_donors(user_id)
        lines = [
            "Что сделать:\n"
            "1. Открой канал, чьи посты тебе нравятся.\n"
            "2. Нажми «➕ Добавить канал» и пришли на него ссылку:\n"
            "   @name или https://t.me/name.\n"
            f"3. Повтори для каждого интересного канала - вместе до {MAX_DONORS} штук.\n\n"
            "Что будет дальше:\n"
            "Бот сам подпишется на эти каналы и поймает каждый новый пост, в котором\n"
            "есть текст. Пост с картинкой выйдет с картинкой, пост без картинки -\n"
            "текстом. Ничего скачивать и загружать тебе не нужно.\n\n"
            f"Сейчас в списке: {len(donors)} из {MAX_DONORS}.",
        ]
        for donor in donors:
            lines.append(f"• {ref_word(donor.ref)} - " + ("✅ читаем" if donor.chat_id else "⏳ подписываюсь"))
        rows = [[(f"🗑 {ref_word(donor.ref)[:22]}", f"cdel|{index}")] for index, donor in enumerate(donors, 1)]
        return self._notice(user_id, "\n".join(lines)), markup(
            [("➕ Добавить канал", "cadd")],
            *rows,
            [("📊 Результат", "cresult")],
            [("⬅️ Кабинет", "chome")],
        )

    async def _result_view(self, user_id: int, tenant: Tenant, now: datetime.datetime) -> Tuple[str, object]:
        """Экран «идёт / не идёт»: одна причина, что делать, и все числа по порядку."""
        donors = await self.store.list_donors(user_id)
        bound = sum(1 for donor in donors if donor.chat_id)
        posts = await self.store.posts_today(user_id)
        blocker = self._blocker(tenant, donors, bound, posts)
        lines = ["📊 Результат", ""]
        if blocker:
            lines += [f"⛔ Сейчас не идёт: {blocker[0]}", f"   Что сделать: {blocker[1]}", ""]
        else:
            lines += ["✅ Всё идёт - посты выходят сами.", ""]
        lines += [
            f"Канал: {channel_line(tenant)}",
            f"Каналы, откуда берём посты: {len(donors)} (читаю {bound})",
            f"Постов сегодня: {posts} из {MAX_POSTS_PER_DAY}",
            f"Последний пост: {tenant.last_post_phrase(now)}",
            f"Текст поста: {self._text_line(tenant)}",
            f"Просмотры: {views_line(tenant)}",
            f"Реакции: {'включены' if tenant.reactions else 'выключены'} - {tenant.reactions_phrase()}",
            f"Тариф: {plan_line(tenant, now)}",
        ]
        if donor_problems := [ref_word(donor.ref) for donor in donors if not donor.chat_id]:
            lines += ["", "Не открылись: " + ", ".join(donor_problems[:5]), "   Проверь ссылку или убери канал кнопкой 🗑."]
        return self._notice(user_id, "\n".join(lines)), markup(
            [("1️⃣ Откуда брать посты", "cdonors"), ("2️⃣ Куда публиковать", "cchannel")],
            [("3️⃣ Текст поста", "ctext"), ("4️⃣ Просмотры", "cviews")],
            [("5️⃣ Тариф", "cplans"), ("✨ Реакции", "cboost")],
            [("⬅️ Кабинет", "chome")],
        )

    def _blocker(self, tenant: Tenant, donors: List, bound: int, posts: int) -> Optional[Tuple[str, str]]:
        """Первая настоящая причина, по которой пост сейчас не выйдет, и что нажать."""
        if not tenant.active(now_utc()):
            return ("нужен оплаченный тариф", "нажми «5️⃣ Тариф» - неделя или месяц, оплата с баланса")
        if not donors:
            return ("не указан ни один канал-источник", "нажми «1️⃣ Откуда брать посты» и пришли ссылку на канал")
        if not bound:
            return (
                "ни один канал-источник не открылся",
                "проверь ссылки: канал может быть без публичного адреса - тогда поправь ссылку",
            )
        if not tenant.target:
            return ("не указан канал, куда публиковать", "нажми «2️⃣ Куда публиковать»")
        if not tenant.target_ok:
            return (
                "рабочий аккаунт не админ в твоём канале",
                f"добавь {self.worker} в администраторы с правом публиковать сообщения",
            )
        if posts >= MAX_POSTS_PER_DAY:
            return (
                f"на сегодня использован лимит {MAX_POSTS_PER_DAY} постов",
                "ничего делать не нужно - после полуночи лимит обнулится сам",
            )
        return None

    async def _wizard_view(self, user_id: int, tenant: Tenant, now: datetime.datetime) -> Tuple[str, object]:
        index = await self._step(user_id)
        if index >= len(STEPS):
            donors = await self.store.list_donors(user_id)
            return self._notice(
                user_id,
                "Готово - все шаги пройдены.\n\n"
                f"• Каналы, откуда берём посты: {len(donors)}\n"
                f"• Публикуем в: {channel_line(tenant)}\n"
                f"• Текст поста: {self._text_line(tenant)}\n"
                f"• Просмотры: {views_line(tenant)}\n"
                f"• Тариф: {plan_line(tenant, now)}\n\n"
                "Дальше бот работает сам: ловит новый пост источника и выкладывает его\n"
                f"в твой канал ({self._text_line(tenant)}), затем добавляет просмотры и реакции.\n"
                "Заглядывать стоит только в «📊 Результат» - там видно, идёт ли всё сейчас.",
            ), markup(
                [("📊 Результат", "cresult")],
                [("🚀 В кабинет", "chome")],
            )

        step = STEPS[index]
        body, buttons = await self._view(user_id, step)
        head = f"🚀 Пошаговый запуск\nШаг {index + 1} из {len(STEPS)} - {STEP_TITLES[step]}\n\n"
        return head + body, add_row(buttons, [("✅ Дальше", "cnext")])

    def _reactions_cell(self, tenant: Tenant) -> Tuple[str, str]:
        return ("✨ Реакции: выключить" if tenant.reactions else "✨ Реакции: включить", "creactions")

    def _text_line(self, tenant: Tenant) -> str:
        """Честная строка про текст: без нейросети - общей настройкой или без ключа - выбор не работает."""
        if not tenant.rewrite:
            return "публикуем словами автора"
        if not self.cfg.ai_enabled:
            return "переписывание включено, но нейросеть выключена для всех - выходит словами автора"
        if not self.cfg.minimax_api_key:
            return "переписывание включено, но ключа нейросети нет - выходит словами автора"
        return f"переписывает нейросеть ({self.cfg.ai_model})"

    def _text_cell(self, tenant: Tenant) -> Tuple[str, str]:
        """Переключатель: надпись показывает, как сейчас, - нажал и стало наоборот."""
        return ("✏️ Текст: переписывает ИИ" if tenant.rewrite else "✏️ Текст: словами автора", "cretext")

    @property
    def worker(self) -> str:
        """Как клиенту найти рабочего аккаунта: адрес даёт владелец, без него просим через помощь."""
        return self.cfg.worker_contact or "пришлю по запросу - нажми «🆘 Помощь»"

    # ==== вывод ====

    async def _show(self, query, kind: str, flash: str = "") -> None:
        text, buttons = await self._view(query.from_user.id, kind)
        await call(
            self.client.edit_message_text,
            query.message.chat.id,
            query.message.id,
            cut(text, TEXT_LIMIT),
            reply_markup=buttons,
        )
        await call(query.answer, flash[:TEXT_LIMIT])

    def _back_to(self, query) -> str:
        key = (query.data or "").split("|")[0]
        return {
            "cdel": "donors",
            "cadd": "donors",
            "cviews": "views",
            "cbuy": "plans",
            "ctopup": "plans",
            "cchannel": "channel",
            "cset_target": "channel",
            "creactions": "reactions",
            "cretext": "text",
            "cnext": "wizard",
        }.get(key, "home")

    def _note(self, user_id: int, text: str) -> None:
        self.notice[user_id] = text

    def _notice(self, user_id: int, text: str) -> str:
        notice = self.notice.pop(user_id, "")
        return f"{notice}\n\n{text}" if notice else text
