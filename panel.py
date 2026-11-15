"""Управление донорами кнопками в твоём боте.

Бот работает в одном процессе с юзерботом, поэтому нажимает действия прямо
через реестр: список по-прежнему живёт в группе с темами, но руками в ней
ничего писать не нужно. Служебных сообщений в чате не появляется.
"""

import logging
from typing import Iterator, List, Optional, Tuple, Union

from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from ai import cut
from registry import Donor, RegistryError
from utils import call

log = logging.getLogger("panel")

TEXT_LIMIT = 3900
CALLBACK_LIMIT = 64
NAME_LIMIT = 50
DONOR_LIMIT = 1000
MAX_DONOR_BUTTONS = 30

COMMANDS = [BotCommand("start", "Меню управления"), BotCommand("help", "Как это работает")]

HELP = (
    "Как пользоваться:\n"
    "1) «➕ Новый канал» - напиши название своего канала, куда постить. Тема в группе создаётся сама.\n"
    "2) Открой канал и нажми «➕ Донор» - пришли @юзернейм или ссылку https://t.me/... "
    "Сколько ссылок в одном сообщении, столько доноров и добавится.\n"
    "3) Кнопка 🗑 под донором - убрать его из списка.\n"
    "4) «✏️ Переименовать» - если название темы должно отличаться от названия канала.\n"
    "5) «🔄 Обновить список» - перечитать группу заново.\n"
    "6) «⏸ Пауза» - посты ловим, но не публикуем. «▶ Пуск» - снова работаем.\n\n"
    "Список хранится в группе с темами (тема = канал, ссылки в теме = доноры), "
    "поэтому после перезапуска всё восстанавливается само."
)

NO_GROUP = (
    "Группы с темами пока нет - именно в ней хранится список доноров.\n\n"
    "1) Создай группу (например «АвтоПост»).\n"
    "2) В настройках группы включи «Темы».\n"
    "3) Добавь в группу аккаунт {account} и дай ему права администратора: "
    "управлять темами и удалять сообщения.\n"
    "4) Нажми «🔄 Обновить список»."
)

Cell = Tuple[str, str]


def markup(*rows: Union[Cell, List[Cell]]) -> InlineKeyboardMarkup:
    built = []
    for row in rows:
        cells = [row] if isinstance(row, tuple) else list(row)
        built.append(
            [
                InlineKeyboardButton(text=text[:TEXT_LIMIT], callback_data=data[:CALLBACK_LIMIT])
                for text, data in cells
            ]
        )
    return InlineKeyboardMarkup(built)


def chunks(items: List[Donor], size: int = 2) -> Iterator[List[Donor]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Panel:
    def __init__(self, client, cfg, registry, bot):
        self.client = client
        self.cfg = cfg
        self.registry = registry
        self.bot = bot
        self.owner = 0
        self.account = "аккаунт"
        self.awaiting: Tuple[str, int] = ("", 0)
        self.notice = ""

    def setup(self) -> None:
        self.client.add_handler(MessageHandler(self.on_message, filters.private))
        self.client.add_handler(CallbackQueryHandler(self.on_button))

    async def start(self, owner_id: int, account_name: str = "") -> None:
        self.owner = owner_id
        self.account = account_name or "аккаунт"
        try:
            await call(self.client.set_bot_commands, COMMANDS)
        except Exception as exc:  # noqa: BLE001 - меню без подсказок команд тоже работает
            log.warning("Список команд бота не задал (%s).", type(exc).__name__)

    @property
    def admins(self) -> set:
        return {user_id for user_id in [self.owner, *self.cfg.panel_admins] if user_id}

    # ==== вход ====

    async def on_message(self, client, message) -> None:
        if message.chat.id not in self.admins:
            log.warning("Панель: сообщение не от владельца (chat %s) - игнорирую.", message.chat.id)
            await call(message.reply, "Этот бот управляющий: он слушает только владельца аккаунта.")
            return

        text = (message.text or "").strip()
        kind, topic_id = self.awaiting
        if kind and text and not text.startswith("/"):
            self.awaiting = ("", 0)
            await self._apply_text(message, kind, topic_id, text)
            return

        self.awaiting = ("", 0)
        body, buttons = self._view("main")
        await call(message.reply, body, reply_markup=buttons)

    async def on_button(self, client, query) -> None:
        if query.from_user.id not in self.admins:
            await call(query.answer, "Доступ только у владельца аккаунта.", show_alert=True)
            return

        try:
            await self._route(query)
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "main", flash=str(exc))
        except Exception as exc:  # noqa: BLE001 - кнопка всегда должна чем-то ответить
            log.error("Кнопка «%s» не сделалась: %s: %s", query.data, type(exc).__name__, exc)
            await call(query.answer, f"Ошибка: {type(exc).__name__}", show_alert=True)

    # ==== разбор кнопок ====

    async def _route(self, query) -> None:
        parts = (query.data or "").split("|")
        key = parts[0]
        arg = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        if key in ("main", "topics", "status", "help"):
            await self._show(query, key)
        elif key == "cat":
            await self._show(query, "cat", arg)
        elif key in ("add", "channel", "rename"):
            self.awaiting = {"add": ("donor", arg), "channel": ("channel", 0), "rename": ("rename", arg)}[key]
            await self._show(query, key, arg)
        elif key == "del":
            message_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            await self._remove(query, arg, message_id)
        elif key in ("pause", "start"):
            self.registry.paused = key == "pause"
            self.notice = "Пауза: посты ловлю, но не публикую." if self.registry.paused else "Работаю дальше."
            await self._show(query, "main", flash=self.notice)
        elif key == "update":
            await self._rebuild(query)
        else:
            await call(query.answer, "Такой кнопки нет - нажми «Меню».")

    async def _remove(self, query, topic_id: int, message_id: int) -> None:
        removed = await self.registry.remove_donor(message_id)
        names = ", ".join(donor.name for donor in removed)
        self.notice = f"Убрал донора: {names}."
        await self._show(query, "cat", topic_id, flash=self.notice)

    async def _rebuild(self, query) -> None:
        await call(query.answer, "Перечитываю группу...")
        await self._edit(query, "⏳ Перечитываю темы группы и ссылки в них...")
        await self.registry.rebuild()
        problems = self.registry.problems()
        self.notice = "Список обновлён." + ("\n⚠ " + "; ".join(problems[:5]) if problems else "")
        await self._show(query, "main", flash="Готово")

    async def _apply_text(self, message, kind: str, topic_id: int, text: str) -> None:
        registry = self.registry
        try:
            if kind == "donor":
                added = await registry.add_donor(topic_id, cut(text, DONOR_LIMIT))
                self.notice = "✅ Добавил доноров: " + ", ".join(donor.name for donor in added)
                screen = ("cat", topic_id)
            elif kind == "channel":
                category = await registry.add_category(text[:NAME_LIMIT])
                self.notice = self._category_notice(category, f"Канал «{category.name}» добавлен")
                screen = ("cat", category.topic_id)
            else:
                category = await registry.rename_category(topic_id, text[:NAME_LIMIT])
                self.notice = self._category_notice(category, f"Переименовал в «{category.name}»")
                screen = ("cat", topic_id)
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            screen = ("main", 0) if kind == "channel" else ("cat", topic_id)
        except Exception as exc:  # noqa: BLE001 - действие всегда должно заканчиваться ответом
            log.error("Действие из панели упало: %s: %s", type(exc).__name__, exc)
            self.notice = f"⚠ Ошибка: {type(exc).__name__}"
            screen = ("main", 0)

        body, buttons = self._view(*screen)
        await call(message.reply, body, reply_markup=buttons)

    @staticmethod
    def _category_notice(category, head: str) -> str:
        return f"✅ {head}." + (f"\n⚠ {category.problem}" if category.problem else "")

    # ==== экраны ====

    def _view(self, kind: str, topic_id: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        if kind == "topics":
            return self._topics_view()
        if kind == "cat":
            return self._category_view(topic_id)
        if kind == "status":
            return self._status_view()
        if kind == "help":
            return cut(HELP, TEXT_LIMIT), markup([("🔄 Обновить список", "update")], [("⬅️ Меню", "main")])
        if kind == "add":
            return self._add_view(topic_id)
        if kind == "channel":
            return self._new_channel_view()
        if kind == "rename":
            return self._rename_view(topic_id)
        return self._main_view()

    def _main_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        registry = self.registry
        if not registry.ready or not registry.group_id:
            return cut(NO_GROUP.format(account=self.account), TEXT_LIMIT), markup(
                [("🔄 Обновить список", "update"), ("❓ Как это работает", "help")]
            )

        stats = self.bot.stats
        lines = [
            f"Группа: «{registry.group_name}»",
            f"Каналов: {len(registry.categories)} · доноров: {registry.donor_count}",
            f"Состояние: {'⏸ пауза - не публикую' if registry.paused else '▶ работаю'}",
            (
                f"Поймано {stats['caught']} · опубликовано {stats['published']} · "
                f"в промо {stats['forwarded']} · сбоев {stats['errors']}"
            ),
        ]
        problems = registry.problems()
        if problems:
            lines.append("Замечания:\n- " + "\n- ".join(problems[:5]))

        toggle = ("▶ Пуск", "start") if registry.paused else ("⏸ Пауза", "pause")
        return (
            cut(self._with_notice("🤖 АвтоПост\n" + "\n".join(lines)), TEXT_LIMIT),
            markup(
                [("📂 Каналы и доноры", "topics"), ("➕ Новый канал", "channel")],
                [("🔄 Обновить список", "update"), ("ℹ️ Статус", "status")],
                [toggle, ("❓ Как это работает", "help")],
            ),
        )

    def _topics_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        categories = list(self.registry.categories.values())
        if not categories:
            text = "Каналов пока нет. Нажми «➕ Новый канал» и напиши название своего канала."
        else:
            lines = [f"Каналы ({len(categories)}):"]
            for index, category in enumerate(categories, start=1):
                where = category.target_name or category.problem or "канал не определён"
                lines.append(f"{index}. «{category.name}» - {len(category.donors)} дон. -> {where}")
            text = "\n".join(lines)

        rows = [[(f"{category.name} · {len(category.donors)}", f"cat|{category.topic_id}")] for category in categories[:20]]
        return (
            cut(self._with_notice(text), TEXT_LIMIT),
            markup(*rows, [("➕ Новый канал", "channel")], [("⬅️ Меню", "main")]),
        )

    def _category_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        if category is None:
            return (
                self._with_notice("Этого канала уже нет в списке."),
                markup([("📂 Каналы и доноры", "topics")], [("⬅️ Меню", "main")]),
            )

        donors = list(category.donors.values())
        where = category.target_name or category.problem or "канал не определён"
        lines = [f"Канал: «{category.name}»", f"Публикуем в: {where}", ""]
        if donors:
            lines.append(f"Доноры ({len(donors)}) - нажми 🗑, чтобы убрать:")
            lines += [f"• {donor.name} ({donor.link})" for donor in donors[:10]]
            if len(donors) > 10:
                lines.append(f"...и ещё {len(donors) - 10}")
        else:
            lines.append("Доноров пока нет - нажми «➕ Донор» и пришли ссылку на канал.")

        rows = [
            [(f"🗑 {donor.name[:28]}", f"del|{topic_id}|{donor.message_id}") for donor in group]
            for group in chunks(donors[:MAX_DONOR_BUTTONS], 2)
        ]
        return (
            cut(self._with_notice("\n".join(lines)), TEXT_LIMIT),
            markup(
                *rows,
                [("➕ Донор", f"add|{topic_id}"), ("✏️ Переименовать", f"rename|{topic_id}")],
                [("📂 Все каналы", "topics"), ("⬅️ Меню", "main")],
            ),
        )

    def _status_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        stats = self.bot.stats
        text = (
            self._with_notice(
                f"{self.cfg.describe()}\n"
                f"Группа: {self.registry.group_name or 'не найдена'}\n"
                f"Последняя публикация: {self.bot.last_publish or 'ещё не было'}\n"
                f"Поймано: {stats['caught']} · опубликовано: {stats['published']} · "
                f"в промо: {stats['forwarded']} · сбоев: {stats['errors']}"
            )
        )
        return (
            cut(text, TEXT_LIMIT),
            markup([("🔄 Обновить список", "update")], [("⬅️ Меню", "main")]),
        )

    def _add_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        name = category.name if category else "категория"
        return (
            cut(
                f"Канал: «{name}»\n\nПришли ссылку на донора: @юзернейм или https://t.me/канал.\n"
                "Можно несколько ссылок в одном сообщении - добавятся все.",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", f"cat|{topic_id}")]),
        )

    def _new_channel_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        return (
            cut(
                "Напиши название канала, куда постить (например «Спорт»). "
                "Тема с таким названием создастся в группе сама.\n\n"
                "Если название темы должно отличаться от названия канала, напиши так: Спорт @sport_ch",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", "main")]),
        )

    def _rename_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        return (
            cut(
                f"Канал: «{self._name(topic_id)}»\n\nНапиши новое название. Оно же - название темы в группе.\n"
                "Можно указать канал отдельно: Название @username",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", f"cat|{topic_id}")]),
        )

    def _name(self, topic_id: int) -> str:
        category = self.registry.categories.get(topic_id)
        return category.name if category else "?"

    def _with_notice(self, text: str) -> str:
        notice, self.notice = self.notice, ""
        return f"{notice}\n\n{text}" if notice else text

    # ==== вывод ====

    async def _show(self, query, kind: str, topic_id: int = 0, flash: str = "") -> None:
        text, buttons = self._view(kind, topic_id)
        await self._edit(query, text, buttons)
        await call(query.answer, flash[:TEXT_LIMIT])

    async def _edit(self, query, text: str, buttons: Optional[InlineKeyboardMarkup] = None) -> None:
        message = query.message
        try:
            await call(
                self.client.edit_message_text, message.chat.id, message.id, cut(text, TEXT_LIMIT), reply_markup=buttons
            )
        except MessageNotModified:
            pass
