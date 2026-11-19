"""Личный кабинет одного канала: настройка, оплата и наблюдаемый результат.

Настройки хранятся в базе; ожидание ввода и шаг запуска изолированы по пользователю.
"""

import datetime
import logging
from typing import Dict, List, Optional, Tuple

import legal
from pyrogram.errors import MessageNotModified

from ai import cut
from config import POSTS_LIMIT, VIEWS_LIMIT, parse_post_limit, parse_views_range
from store import MAX_DONORS, MAX_POSTS_PER_DAY, MIN_TOPUP, PLANS, StoreError, Tenant, parse_ref
from utils import TEXT_LIMIT, call, markup

log = logging.getLogger("cabinet")

# 0 - не ошибка ввода, а осознанный выбор человека: просмотры не накручивать
VIEWS_PRESETS = (0, 500, 1000, 2000, 5000)

# Кнопки лимита постов в сутки; «Не ограничивать» отдельной кнопкой (0).
POSTS_PRESETS = (5, 10, 20, 50)

# Только обязательные настройки; текст, просмотры и реакции доступны отдельно.
STEPS = ("channel", "donors", "plans")
STEP_TITLES = {
    "channel": "Куда публиковать",
    "donors": "Откуда брать посты",
    "plans": "Тариф и оплата",
}
FORMATS = (
    "Поддерживаются текст, фото с подписью, видео с подписью или без неё, "
    "альбомы фото и видео. Фото и фотоальбомы без подписи пропускаются. "
    "У документов и других вложений переносится только текст."
)

# После ввода возвращаем шаг, а не теряем пошаговую навигацию.
SCREENS = {"donor": "donors", "channel": "channel", "views": "views", "posts": "posts", "sum": "plans"}
CANCEL_INPUT = {"donor": "cdonors", "channel": "cchannel", "sum": "cplans"}


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
        return "нет активного тарифа"
    return f"активен ещё {tenant.days_left(now)} дн. (до {date(tenant.plan_until)})"


def channel_line(tenant: Tenant) -> str:
    if not tenant.target:
        return "не указан"
    status = "доступ подтверждён" if tenant.target_ok else "доступ не подтверждён"
    return f"{ref_word(tenant.target)} — {status}"


def views_line(tenant: Tenant) -> str:
    if not tenant.views:
        return "не заказывать (0)"
    if getattr(tenant, "views_max", None):
        return f"заказывать от {tenant.views} до {tenant.views_max} на каждый пост (случайное число)"
    return f"заказывать {tenant.views} на каждый пост"


def posts_line(tenant: Tenant) -> str:
    cap = getattr(tenant, "post_limit", None)
    if cap is None:
        return f"не больше {MAX_POSTS_PER_DAY} (общее правило)"
    if cap == 0:
        return "не ограничивать"
    return f"не больше {cap}"


def posts_plan_line(tenant: Tenant) -> str:
    """Фраза про лимит постов для экрана тарифа."""
    cap = getattr(tenant, "post_limit", None)
    if cap == 0:
        return "посты в сутки не ограничивать"
    if cap is None:
        return f"до {MAX_POSTS_PER_DAY} постов в сутки (можно изменить в «Настройки»)"
    return f"до {cap} постов в сутки (можно изменить в «Настройки»)"


def add_row(buttons, row: List[Tuple[str, str]]):
    """Дописать ряд кнопок к готовому экрану: так пошаговый запуск не дублирует тексты шагов."""
    rows = [[(button.text, button.callback_data) for button in line] for line in (buttons.inline_keyboard or [])]
    rows.append(list(row))
    return markup(*rows)


class Cabinet:
    SHOW = {
        "chome": "home",
        "cstart": "wizard",
        "cproject": "project",
        "csettings": "settings",
        "cterms": "agree",
        "cdonors": "donors",
        "cchannel": "channel",
        "ctext": "text",
        "cviews": "views",
        "cposts": "posts",
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
        self._input_version: Dict[int, int] = {}
        self.notice: Dict[int, str] = {}
        self.wizard: Dict[int, int] = {}
        # последнее экранное сообщение в чате: при открытии нового окна прежнее удаляется
        self.screens: Dict[int, int] = {}

    # ==== вход ====

    async def on_message(self, message) -> None:
        user_id = message.chat.id
        text = (message.text or "").strip()
        self._input_version[user_id] = self._input_version.get(user_id, 0) + 1
        kind, _ = self.awaiting.pop(user_id, ("", 0))
        if kind and not text.startswith("/"):
            await self._apply_text(message, kind, text)
            return

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        self.wizard.pop(user_id, None)
        if command == "/help":
            screen = "guide"
        elif command == "/cancel":
            screen = "home"
        else:
            screen = await self._entry(user_id)
        body, buttons = await self._view(user_id, screen)
        sent = await call(message.reply, cut(body, TEXT_LIMIT), reply_markup=buttons)
        if sent is not None:
            await self._forget_screen(user_id, sent.id)

    async def on_button(self, query) -> None:
        try:
            await self._route(query)
        except (StoreError, ValueError) as exc:
            self._note(query.from_user.id, str(exc))
            await self._show(query, self._back_to(query))
        except Exception as exc:  # noqa: BLE001 - кнопка всегда должна чем-то ответить
            log.error("Кабинет: кнопка «%s» не сделалась: %s: %s", query.data, type(exc).__name__, exc)
            await call(query.answer, "Не удалось выполнить действие. Попробуй ещё раз.", show_alert=True)

    async def _entry(self, user_id: int) -> str:
        """Новый вход всегда начинает с первого незаполненного обязательного шага."""
        tenant = await self.store.ensure_tenant(user_id)
        donors = await self.store.list_donors(user_id)
        index = self._next_step(tenant, donors, now_utc())
        if index < len(STEPS):
            self.wizard[user_id] = index
            return "wizard"
        self.wizard.pop(user_id, None)
        return "home"

    # ==== разбор кнопок ====

    async def _route(self, query) -> None:
        user_id = query.from_user.id
        parts = (query.data or "").split("|")
        key = parts[0]
        arg = parts[1] if len(parts) > 1 else ""

        # Любая кнопка завершает старый ввод только этого пользователя.
        # Новый ввод ниже включается явно, готовое число не оставляет ожидания.
        version = self._input_version[user_id] = self._input_version.get(user_id, 0) + 1
        self.awaiting.pop(user_id, None)
        if key == "cviews":
            self.wizard.pop(user_id, None)
            self.awaiting[user_id] = ("views", 0)
            if arg:
                await self._apply(user_id, "views", arg)
                if self._input_version.get(user_id) == version:
                    self.awaiting.pop(user_id, None)
            await self._show(query, "views")
        elif key == "cposts":
            self.wizard.pop(user_id, None)
            self.awaiting[user_id] = ("posts", 0)
            if arg:
                await self._apply(user_id, "posts", arg)
                if self._input_version.get(user_id) == version:
                    self.awaiting.pop(user_id, None)
            await self._show(query, "posts")
        elif key == "cstart":
            await self._entry(user_id)
            await self._show(query, "wizard")
        elif key in self.SHOW:
            screen = self.SHOW[key]
            index = self.wizard.get(user_id, len(STEPS))
            # Возврат из ввода/оплаты сохраняет текущий шаг. Другая навигация
            # выходит из запуска, чтобы старые кнопки не навязывали его позже.
            if index >= len(STEPS) or screen not in (
                STEPS[index], "agree" if STEPS[index] == "plans" else ""
            ):
                self.wizard.pop(user_id, None)
            await self._show(query, screen)
        elif key == "cset_target":
            self.awaiting[user_id] = ("channel", 0)
            await self._show(query, "target")
        elif key == "cadd":
            self.awaiting[user_id] = ("donor", 0)
            await self._show(query, "add")
        elif key == "cnext":
            index = await self._step(user_id)
            tenant = await self.store.ensure_tenant(user_id)
            donors = await self.store.list_donors(user_id)
            missing = self._next_step(tenant, donors, now_utc())
            if index < len(STEPS) and missing == index:
                self._note(user_id, {
                    "channel": "Сначала укажи канал, куда публиковать.",
                    "donors": "Сначала добавь хотя бы один канал-источник.",
                    "plans": "Сначала подключи тариф. Заявка на пополнение ещё не означает оплату тарифа.",
                }[STEPS[index]])
            else:
                index = min(index + 1, len(STEPS))
            self.wizard[user_id] = index
            await self._show(query, "wizard")
        elif key == "cprev":
            self.wizard[user_id] = max(0, await self._step(user_id) - 1)
            await self._show(query, "wizard")
        elif key == "ctopup":
            tenant = await self.store.ensure_tenant(user_id)
            if not tenant.agreed:
                await self._show(query, "agree")
                return
            self.awaiting[user_id] = ("sum", 0)
            await self._show(query, "sum")
        elif key == "cdoc":
            parent = parts[2] if len(parts) > 2 else "guide"
            await self._send_document(query, int(arg or 0), parent)
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
            self._note(user_id, "Условия приняты. Можно перейти к оплате.")
            await self._show(query, "plans")
        else:
            await call(query.answer, "Такой кнопки нет — открой «Личный кабинет» через /start.")

    async def _send_document(self, query, kind: int, parent: str = "guide") -> None:
        """Старые ссылки на документы работают; новые помнят родительский экран."""
        title, pages = legal.document(kind)
        back = {"guide": "cguide", "plans": "cplans", "agree": "cterms"}.get(parent, "cguide")
        await call(query.answer, title)
        for index, page in enumerate(pages):
            text = f"{title}\n\n{page}" if index == 0 else page
            buttons = markup([("Назад", back)]) if index == len(pages) - 1 else None
            await call(self.client.send_message, query.message.chat.id, cut(text, TEXT_LIMIT), reply_markup=buttons)

    async def _remove_donor(self, query, position: int) -> None:
        donors = await self.store.list_donors(query.from_user.id)
        if not 1 <= position <= len(donors):
            self._note(query.from_user.id, "Этот источник уже удалён.")
        else:
            donor = donors[position - 1]
            await self.store.remove_donor(query.from_user.id, donor.ref)
            self._note(query.from_user.id, f"Источник {ref_word(donor.ref)} удалён.")
        await self._show(query, "donors")

    async def _toggle_reactions(self, query) -> None:
        tenant = await self.store.ensure_tenant(query.from_user.id)
        turned_on = not tenant.reactions
        await self.store.set_reactions(tenant.id, turned_on)
        self._note(
            tenant.id,
            ("Заказ реакций включён. Доставка реакций не гарантируется."
             if self.cfg.reactions_enabled else "Выбор сохранён, но сервис реакций сейчас выключен.")
            if turned_on else "Заказ реакций выключен. Настройка публикации не изменилась.",
        )
        await self._show(query, "reactions")

    async def _toggle_rewrite(self, query) -> None:
        tenant = await self.store.ensure_tenant(query.from_user.id)
        turned_on = not tenant.rewrite
        await self.store.set_rewrite(tenant.id, turned_on)
        if not turned_on:
            self._note(tenant.id, "Выбран текст автора без переписывания.")
        elif self._ai_ready:
            self._note(tenant.id, "Выбрано переписывание текста нейросетью.")
        else:
            self._note(tenant.id, "Выбор переписывания сохранён. Нейросеть сейчас недоступна — используется текст автора.")
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
            self._note(tenant.id, "Сначала укажи канал, куда публиковать — первый шаг настройки.")
            if tenant.id in self.wizard:
                self.wizard[tenant.id] = 0
            await self._show(query, "channel")
            return
        await self.store.buy(tenant.id, kind)
        price = PLANS[kind]["price"]
        self._note(tenant.id, f"{PLANS[kind]['title']} подключён за {price} ₽.")
        await self._show(query, "plans", flash=f"-{price} ₽")

    # ==== ввод текстом ====

    async def _apply_text(self, message, kind: str, text: str) -> None:
        user_id = message.chat.id
        screen = SCREENS.get(kind, "home")
        version = self._input_version.get(user_id, 0)
        try:
            await self._apply(user_id, kind, text)
        except (StoreError, ValueError) as exc:
            if self._input_version.get(user_id, 0) == version:
                self.awaiting[user_id] = (kind, 0)
            self._note(user_id, f"{exc}\nПовтори ввод сообщением или нажми «Отмена».")
        except Exception as exc:  # noqa: BLE001 - ввод клиента не роняет бота
            log.error("Кабинет: действие из текста упало: %s: %s", type(exc).__name__, exc)
            self._note(user_id, "Не удалось выполнить действие. Проверь результат перед повторной попыткой.")

        if self._input_version.get(user_id, 0) != version:
            note = self.notice.pop(user_id, "")
            if note:
                await call(message.reply, cut(note, TEXT_LIMIT))
            return
        body, buttons = await self._view(user_id, screen)
        sent = await call(message.reply, cut(body, TEXT_LIMIT), reply_markup=buttons)
        if sent is not None:
            await self._forget_screen(user_id, sent.id)

    async def _apply(self, user_id: int, kind: str, text: str) -> None:
        if kind == "channel":
            ref = self._public_ref(text)
            await self.store.set_target(user_id, str(ref), ref if isinstance(ref, int) else None, "")
            self._note(user_id, f"Канал {ref_word(ref)} сохранён. Доступ ещё не подтверждён; статус — в «Результат».")
        elif kind == "donor":
            donor = await self.store.add_donor(user_id, parse_ref(text))
            self._note(user_id, f"Источник {ref_word(donor.ref)} добавлен. Рабочий аккаунт попробует подписаться на него.")
        elif kind == "views":
            number, top = parse_views_range(text, low=0)
            await self.store.set_views(user_id, number, top)
            if number and top:
                self._note(
                    user_id,
                    f"Сохранено: на каждый пост случайное число от {number} до {top}. Фактическое число не гарантируется.",
                )
            elif number:
                self._note(
                    user_id,
                    f"Сохранено: заказывать {number} просмотров на пост. Фактическое число не гарантируется.",
                )
            else:
                self._note(user_id, "Сохранено: не заказывать просмотры.")
        elif kind == "posts":
            value = parse_post_limit(text)
            await self.store.set_post_limit(user_id, value)
            if value == 0:
                self._note(user_id, "Сохранено: посты в сутки не ограничивать.")
            else:
                self._note(
                    user_id,
                    f"Сохранено: не больше {value} постов в сутки. "
                    "Лишние посты в тот же день пропускаются, на завтра не копятся.",
                )
        elif kind == "sum":
            request_id = await self.store.request_topup(user_id, self._rubles(text))
            self._note(user_id, f"Заявка №{request_id} принята и ждёт проверки платежа. После зачисления можно купить тариф.")

    @staticmethod
    def _public_ref(text: str):
        """Ссылка на канал клиента: закрытый адрес вида t.me/c/... не годится - по нему сервисы пост не найдут."""
        if "t.me/c/" in (text or "").lower():
            raise ValueError(
                "Ссылка t.me/c/... не подходит для этой настройки.\n"
                "Открой канал → Изменить → Публичная ссылка и пришли @name или https://t.me/name."
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
        """Заполнение шагов не равнозначно подтверждённому доступу или публикации."""
        if not tenant.target:
            return 0
        if not donors:
            return 1
        if not tenant.active(now):
            return 2
        return len(STEPS)

    async def _step(self, user_id: int) -> int:
        tenant = await self.store.ensure_tenant(user_id)
        donors = await self.store.list_donors(user_id)
        missing = self._next_step(tenant, donors, now_utc())
        # Можно вернуться назад, но нельзя оказаться за незаполненным шагом.
        return min(max(0, self.wizard.get(user_id, missing)), missing)

    # ==== экраны ====

    async def _view(self, user_id: int, kind: str) -> Tuple[str, object]:
        index = self.wizard.get(user_id, len(STEPS))
        step = STEPS[index] if 0 <= index < len(STEPS) else ""
        children = {"channel": {"target"}, "donors": {"add"}, "plans": {"sum", "agree"}}
        if kind == "wizard" or (step and kind == step):
            tenant = await self.store.ensure_tenant(user_id)
            body, buttons = await self._wizard_view(user_id, tenant, now_utc())
        else:
            if kind not in children.get(step, set()):
                self.wizard.pop(user_id, None)
            body, buttons = await self._screen_view(user_id, kind)

        waiting, _ = self.awaiting.get(user_id, ("", 0))
        if waiting in CANCEL_INPUT and kind == SCREENS[waiting]:
            buttons = add_row(buttons, [("Отмена", CANCEL_INPUT[waiting])])
        return body, buttons

    async def _screen_view(self, user_id: int, kind: str) -> Tuple[str, object]:
        """Содержимое экранов без оболочки пошагового запуска."""
        tenant = await self.store.ensure_tenant(user_id)
        now = now_utc()

        if kind == "project":
            donors = await self.store.list_donors(user_id)
            return self._notice(
                user_id,
                f"Мой канал\n\nКанал: {channel_line(tenant)}\n"
                f"Источников: {len(donors)} из {MAX_DONORS}\n\n"
                "Здесь настраивается один канал публикации.",
            ), markup(
                [("Куда публиковать", "cchannel")],
                [("Откуда брать посты", "cdonors")],
                [("Настройки", "csettings")],
                [("Назад: Личный кабинет", "chome")],
            )

        if kind == "settings":
            return self._notice(
                user_id,
                f"Настройки канала\n\nТекст: {self._text_line(tenant)}\n"
                f"Просмотры: {views_line(tenant)}\n"
                f"Посты в сутки: {posts_line(tenant)}\n"
                f"Заказ реакций: {self._reaction_setting(tenant)}\n\n"
                "Это выбранные настройки, не подтверждение результата.",
            ), markup(
                [("Текст постов", "ctext")],
                [("Просмотры", "cviews")],
                [("Посты в сутки", "cposts")],
                [("Реакции", "cboost")],
                [("Назад: Мой канал", "cproject")],
            )

        if kind == "donors":
            return await self._donors_view(user_id, tenant)

        if kind == "add":
            return self._notice(
                user_id,
                "Добавить источник\n\nПришли @name или https://t.me/name одним сообщением.\n"
                f"Можно добавить до {MAX_DONORS} источников. Рабочий аккаунт сам подписывается на них; "
                "доступ подтверждается отдельно.",
            ), markup([("Отмена", "cdonors")])

        if kind == "channel":
            return self._notice(
                user_id,
                "Куда публиковать\n\n"
                f"Добавь рабочего аккаунта {self.worker} в администраторы своего канала.\n"
                "Нужны права публиковать сообщения и оставлять реакции. Затем укажи ссылку на канал.\n\n"
                f"Сейчас: {channel_line(tenant)}\n"
                "Сохранение ссылки не подтверждает доступ. Его статус — в «Результат».",
            ), markup(
                [("Указать мой канал", "cset_target")],
                [("Назад: Мой канал", "cproject")],
            )

        if kind == "target":
            return self._notice(
                user_id,
                "Пришли ссылку на свой публичный канал: @name или https://t.me/name.\n"
                "Ссылка t.me/c/... не подходит. Публичный адрес задаётся в настройках канала.\n\n"
                f"Сейчас: {channel_line(tenant)}",
            ), markup([("Отмена", "cchannel")])

        if kind == "text":
            return self._notice(
                user_id,
                "Текст постов\n\nМожно оставить слова автора или поручить нейросети переписать текст. "
                "Если нейросеть недоступна, используется текст автора; твой выбор сохраняется.\n\n"
                f"Сейчас: {self._text_line(tenant)}",
            ), markup([self._text_cell(tenant)], [("Назад: Настройки", "csettings")])

        if kind == "views":
            presets = [
                [("Не заказывать (0)" if count == 0 else str(count), f"cviews|{count}") for count in VIEWS_PRESETS[:3]],
                [(str(count), f"cviews|{count}") for count in VIEWS_PRESETS[3:]],
            ]
            waiting = self.awaiting.get(user_id, ("", 0))[0] == "views"
            rows = presets if waiting else presets + [[("Ввести другое число", "cviews")]]
            return self._notice(
                user_id,
                "Просмотры\n\n"
                f"Число для заказа на новый пост: от 0 до {VIEWS_LIMIT}. Ноль — не заказывать.\n"
                "Можно указать диапазон, например 1200-1600: тогда на каждый пост бот будет "
                "заказывать случайное число из этого диапазона.\n"
                + ("Пришли число или диапазон сообщением, либо выбери кнопкой.\n\n" if waiting
                   else "Выбери число кнопкой или нажми «Ввести другое число».\n\n")
                + "Это запрос внешнему сервису, а не гарантия количества просмотров.\n"
                f"Сейчас: {views_line(tenant)}",
            ), markup(*rows, [("Отмена" if waiting else "Назад: Настройки", "csettings")])

        if kind == "posts":
            presets = [
                [(str(count), f"cposts|{count}") for count in POSTS_PRESETS[:2]],
                [(str(count), f"cposts|{count}") for count in POSTS_PRESETS[2:]],
                [("Не ограничивать (0)", "cposts|0")],
            ]
            waiting = self.awaiting.get(user_id, ("", 0))[0] == "posts"
            rows = presets if waiting else presets + [[("Ввести своё число", "cposts")]]
            return self._notice(
                user_id,
                "Посты в сутки\n\n"
                f"Сколько постов в сутки брать из источников и публиковать в канал: от 0 до {POSTS_LIMIT}. "
                "Ноль — не ограничивать.\n"
                "Счёт обнуляется в полночь по Москве. Лишние посты в тот же день пропускаются, на завтра не копятся.\n"
                + ("Пришли число сообщением, либо выбери кнопкой.\n\n" if waiting
                   else "Выбери число кнопкой или нажми «Ввести своё число».\n\n")
                + f"Сейчас: {posts_line(tenant)}",
            ), markup(*rows, [("Отмена" if waiting else "Назад: Настройки", "csettings")])

        if kind == "reactions":
            return self._notice(
                user_id,
                "Реакции\n\nЗаказы для новых постов обрабатываются очередью. Доставка и сроки не гарантируются.\n"
                "Для заказа разреши все реакции: канал → Изменить → Реакции.\n\n"
                f"Заказ реакций: {self._reaction_setting(tenant)}.\n"
                f"{self._reactions_line(tenant)}",
            ), markup([self._reactions_cell(tenant)], [("Назад: Настройки", "csettings")])

        if kind == "result":
            return await self._result_view(user_id, tenant, now)

        if kind == "guide":
            return self._notice(
                user_id,
                "Помощь\n\nНастройка: куда публиковать → откуда брать посты → тариф.\n"
                "В «Мой канал» можно менять канал и источники; в «Настройки» — текст, просмотры, лимит постов и реакции.\n"
                "«Результат» показывает известные статусы и последний пост, не обещание новых публикаций.\n\n"
                f"{FORMATS}\n\n"
                f"Рабочий аккаунт: {self.worker}. Он подписывается на источники; "
                "в твоём канале ему нужны права администратора: публиковать сообщения и оставлять реакции.",
            ), markup(
                [("Настроить канал", "cstart")],
                [("Соглашение", "cdoc|1|guide"), ("Политика", "cdoc|2|guide")],
                [("Назад: Личный кабинет", "chome")],
            )

        if kind == "plans":
            payments = await self.store.list_payments(user_id, 3)
            history = "\n\nПоследние оплаты:\n" + "\n".join(self._payment_line(row) for row in payments) if payments else ""
            return self._notice(
                user_id,
                "Тариф и оплата\n\n"
                f"Неделя — {PLANS['week']['price']} ₽ ({PLANS['week']['days']} дней)\n"
                f"Месяц — {PLANS['month']['price']} ₽ ({PLANS['month']['days']} дней)\n"
                f"Один канал, до {MAX_DONORS} источников, {posts_plan_line(tenant)}\n"
                "Настройки просмотров и реакций не гарантируют их получение.\n\n"
                f"Баланс: {tenant.balance} ₽\nТариф: {plan_line(tenant, now)}\n\n"
                "Пополнение проверяется вручную. После зачисления выбери тариф — стоимость спишется с баланса. "
                "Покупка добавляет дни к оставшемуся сроку."
                + history,
            ), markup(
                [(f"Купить неделю {PLANS['week']['price']} ₽", "cbuy|week")],
                [(f"Купить месяц {PLANS['month']['price']} ₽", "cbuy|month")],
                [("Пополнить баланс", "ctopup")],
                [("Соглашение", "cdoc|1|plans"), ("Политика", "cdoc|2|plans")],
                [("Назад: Личный кабинет", "chome")],
            )

        if kind == "sum":
            return self._notice(
                user_id,
                "Пополнение баланса\n\n"
                f"{self.cfg.payment_details or 'Реквизиты пока не указаны. Запроси их у владельца сервиса.'}\n\n"
                f"После перевода пришли сумму одним числом, от {MIN_TOPUP} ₽. "
                "Заявка поступит на проверку, зачисление не мгновенное.",
            ), markup([("Отмена", "cplans")])

        if kind == "agree":
            return self._notice(
                user_id,
                "Перед оплатой прочитай соглашение и политику конфиденциальности.",
            ), markup(
                [("Соглашение", "cdoc|1|agree"), ("Политика", "cdoc|2|agree")],
                [("Принимаю условия", "cagree")],
                [("Отмена", "cplans")],
            )

        return await self._home_view(user_id, tenant, now)

    # ==== отдельные экраны ====

    async def _home_view(self, user_id: int, tenant: Tenant, now: datetime.datetime) -> Tuple[str, object]:
        donors = await self.store.list_donors(user_id)
        bound = sum(1 for donor in donors if donor.chat_id)
        posts = await self.store.posts_today(user_id)
        blocker = self._blocker(tenant, donors, bound, posts)
        lines = [
            "Личный кабинет",
            "",
            f"Канал: {channel_line(tenant)}",
            f"Состояние: {blocker[0]}" if blocker else "Состояние: канал подключён, автопубликация настроена.",
            f"Дальше: {blocker[1]}" if blocker else "Дальше: проверь свежие публикации в «Результат».",
            f"Последний пост: {tenant.last_post_phrase(now)}",
        ]
        rows = [[("Мой канал", "cproject")], [("Результат", "cresult")],
                [("Тариф и оплата", "cplans")], [("Помощь", "cguide")]]
        if self._next_step(tenant, donors, now) < len(STEPS):
            rows.insert(0, [("Настроить канал", "cstart")])
        return self._notice(user_id, "\n".join(lines)), markup(*rows)

    def _payment_line(self, row: dict) -> str:
        title = PLANS.get(row.get("kind"), {}).get("title", "Пополнение")
        status = {"done": "", "pending": " (ждёт проверки)", "rejected": " (отклонено)"}.get(
            row.get("status"), " (статус не указан)"
        )
        return f"{title} {row.get('amount_rub')} ₽ {date(row.get('created_at'))}{status}".strip()

    async def _donors_view(self, user_id: int, tenant: Tenant) -> Tuple[str, object]:
        donors = await self.store.list_donors(user_id)
        lines = [
            "Откуда брать посты\n\n"
            f"Добавь до {MAX_DONORS} каналов-источников. Рабочий аккаунт сам подписывается на них.\n"
            f"{FORMATS}\n\n"
            f"Источников: {len(donors)} из {MAX_DONORS}.",
        ]
        for donor in donors:
            status = "подключён" if donor.chat_id else "доступ не подтверждён"
            lines.append(f"{ref_word(donor.ref)} — {status}")
        rows = [[(f"Удалить {ref_word(donor.ref)[:22]}", f"cdel|{index}")] for index, donor in enumerate(donors, 1)]
        return self._notice(user_id, "\n".join(lines)), markup(
            [("Добавить источник", "cadd")], *rows, [("Назад: Мой канал", "cproject")],
        )

    async def _result_view(self, user_id: int, tenant: Tenant, now: datetime.datetime) -> Tuple[str, object]:
        """Наблюдаемые данные отдельно от выбранных настроек."""
        donors = await self.store.list_donors(user_id)
        bound = sum(1 for donor in donors if donor.chat_id)
        posts = await self.store.posts_today(user_id)
        cap = tenant.cap_today()
        blocker = self._blocker(tenant, donors, bound, posts)
        lines = ["Результат", ""]
        if blocker:
            lines += [f"Состояние: {blocker[0]}", f"Дальше: {blocker[1]}", ""]
        else:
            lines += ["Канал подключён, автопубликация настроена. Последний опубликованный пост указан ниже.", ""]
        lines += [
            f"Канал: {channel_line(tenant)}",
            f"Источников: {len(donors)} (подключено {bound})",
            f"Постов сегодня: {posts} из {cap}" if cap else f"Постов сегодня: {posts} (без лимита)",
            f"Последний пост: {tenant.last_post_phrase(now)}",
            f"Реакции: {self._reactions_line(tenant)}",
            "",
            "Настройки:",
            f"Текст: {self._text_line(tenant)}",
            f"Просмотры: {views_line(tenant)}",
            f"Посты в сутки: {posts_line(tenant)}",
            f"Заказ реакций: {self._reaction_setting(tenant)}",
            f"Тариф: {plan_line(tenant, now)}",
        ]
        if donor_problems := [ref_word(donor.ref) for donor in donors if not donor.chat_id]:
            lines += ["", "Доступ к источникам не подтверждён: " + ", ".join(donor_problems[:5])]
        return self._notice(user_id, "\n".join(lines)), markup(
            [("Обновить", "cresult")],
            [("Мой канал", "cproject"), ("Тариф и оплата", "cplans")],
            [("Назад: Личный кабинет", "chome")],
        )

    def _blocker(self, tenant: Tenant, donors: List, bound: int, posts: int) -> Optional[Tuple[str, str]]:
        """Только известные условия: отсутствие подтверждения не объясняет его причину."""
        if not tenant.target:
            return ("не указан канал публикации", "открой «Мой канал» → «Куда публиковать»")
        if not donors:
            return ("не указан ни один источник", "открой «Мой канал» → «Откуда брать посты»")
        if not tenant.active(now_utc()):
            return ("нет активного тарифа", "открой «Тариф и оплата» и подключи тариф")
        if not tenant.target_ok:
            return (
                "доступ к каналу публикации не подтверждён",
                "проверь ссылку и права рабочего аккаунта в «Мой канал» → «Куда публиковать», затем обнови «Результат»",
            )
        if not bound:
            return (
                "доступ к источникам не подтверждён",
                "проверь ссылки в «Мой канал» → «Откуда брать посты», затем обнови «Результат»",
            )
        cap = tenant.cap_today()
        if cap and posts >= cap:
            return (
                f"на сегодня использован лимит {cap} постов",
                "проверь «Результат» в следующие сутки или измени лимит в «Настройки» → «Посты в сутки»",
            )
        return None

    async def _wizard_view(self, user_id: int, tenant: Tenant, now: datetime.datetime) -> Tuple[str, object]:
        index = await self._step(user_id)
        self.wizard[user_id] = index
        if index >= len(STEPS):
            self.wizard.pop(user_id, None)
            donors = await self.store.list_donors(user_id)
            posts = await self.store.posts_today(user_id)
            blocker = self._blocker(tenant, donors, sum(1 for donor in donors if donor.chat_id), posts)
            state = (f"Состояние: {blocker[0]}\nДальше: {blocker[1]}" if blocker
                     else "Канал подключён. Последние публикации можно посмотреть в «Результат».")
            return self._notice(
                user_id,
                "Обязательные настройки сохранены\n\n"
                f"Канал: {channel_line(tenant)}\nИсточников: {len(donors)}\n"
                f"Тариф: {plan_line(tenant, now)}\n\n{state}\n\n"
                "Завершение настройки не подтверждает выход постов. "
                "Текст, просмотры, лимит постов и реакции можно менять в «Мой канал» → «Настройки».",
            ), markup([("Результат", "cresult")], [("Личный кабинет", "chome")])

        body, buttons = await self._screen_view(user_id, STEPS[index])
        # Вместо обычного возврата к разделу — предыдущий шаг и явный выход.
        rows = [
            [(button.text, button.callback_data) for button in row]
            for row in buttons.inline_keyboard
            if not any(button.callback_data in {"cproject", "chome"} for button in row)
        ]
        rows.append([("Назад", "cprev" if index else "cproject"), ("Дальше", "cnext")])
        rows.append([("В личный кабинет", "chome")])
        head = f"Настроить канал\nШаг {index + 1} из {len(STEPS)} — {STEP_TITLES[STEPS[index]]}\n\n"
        return head + body, markup(*rows)

    def _reaction_setting(self, tenant: Tenant) -> str:
        if not tenant.reactions:
            return "выключен"
        if not self.cfg.reactions_enabled:
            return "выбран, но сервис сейчас выключен"
        return "включён"

    @staticmethod
    def _reactions_line(tenant: Tenant) -> str:
        added = tenant.reactions_added
        if added is None:
            return "результат ещё не измерен"
        if added == 0:
            return "по последнему замеру число не изменилось"
        if added > 0:
            return f"по последнему замеру +{added}; это изменение, не подтверждение доставки сервисом"
        return f"по последнему замеру снижение на {abs(added)}; причина изменения не установлена"

    def _reactions_cell(self, tenant: Tenant) -> Tuple[str, str]:
        return ("Заказ реакций: выключить" if tenant.reactions else "Заказ реакций: включить", "creactions")

    @property
    def _ai_ready(self) -> bool:
        return bool(self.cfg.ai_enabled and self.cfg.minimax_api_key)

    def _text_line(self, tenant: Tenant) -> str:
        if not tenant.rewrite:
            return "текст автора, без переписывания"
        if not self._ai_ready:
            return "текст автора; нейросеть недоступна, выбор переписывания сохранён"
        return "переписывание нейросетью"

    def _text_cell(self, tenant: Tenant) -> Tuple[str, str]:
        if not tenant.rewrite:
            label = "Выбрать переписывание"
        else:
            label = "Оставить текст автора"
        return label, "cretext"

    @property
    def worker(self) -> str:
        """Контакт рабочего аккаунта, без выдуманного адреса или обещаний."""
        return self.cfg.worker_contact or "(контакт пока не указан — запроси у владельца сервиса)"

    # ==== вывод ====

    async def _forget_screen(self, chat_id: int, keep: int) -> None:
        """В чате живёт одно окно: прежнее экранное сообщение удаляем, чтобы старые кнопки не плодились."""
        previous, self.screens[chat_id] = self.screens.get(chat_id), keep
        if previous and previous != keep:
            try:
                await call(self.client.delete_messages, chat_id, previous)
            except Exception as exc:  # noqa: BLE001 - окно могли удалить и без нас, это не повод портить экран
                log.debug("Прежний экран %s удалить не удалось: %s", previous, type(exc).__name__)

    async def _show(self, query, kind: str, flash: str = "") -> None:
        text, buttons = await self._view(query.from_user.id, kind)
        try:
            await call(
                self.client.edit_message_text,
                query.message.chat.id,
                query.message.id,
                cut(text, TEXT_LIMIT),
                reply_markup=buttons,
            )
        except MessageNotModified:
            pass
        await self._forget_screen(query.message.chat.id, query.message.id)
        await call(query.answer, flash[:180])

    def _back_to(self, query) -> str:
        key = (query.data or "").split("|")[0]
        return {
            "cdel": "donors",
            "cadd": "donors",
            "cviews": "views",
            "cposts": "posts",
            "cbuy": "plans",
            "ctopup": "plans",
            "cchannel": "channel",
            "cset_target": "channel",
            "creactions": "reactions",
            "cretext": "text",
            "cnext": "wizard",
            "cprev": "wizard",
        }.get(key, "home")

    def _note(self, user_id: int, text: str) -> None:
        self.notice[user_id] = text

    def _notice(self, user_id: int, text: str) -> str:
        notice = self.notice.pop(user_id, "")
        return f"{notice}\n\n{text}" if notice else text
