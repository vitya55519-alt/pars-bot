"""Управление ботом кнопками в твоём боте: куда постить, откуда читать и что делать с текстом.

Бот работает в одном процессе с юзерботом, поэтому нажимает действия прямо
через реестр: список по-прежнему живёт в группе с темами, но руками в ней
ничего писать не нужно. Настройки канала (переписывать ли текст, просмотры,
реакции) бот записывает служебным сообщением в его тему - после перезапуска
они восстанавливаются сами.
"""

import logging
from typing import Dict, Optional, Tuple

from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, InlineKeyboardMarkup

import legal
from ai import cut
from cabinet import Cabinet
from config import POSTS_LIMIT, VIEWS_LIMIT
from registry import RegistryError
from store import StoreError
from utils import TEXT_LIMIT, call, chunks, markup

log = logging.getLogger("panel")

NAME_LIMIT = 50
DONOR_LIMIT = 1000
LINK_LIMIT = 200
MAX_DONOR_BUTTONS = 30

COMMANDS = [BotCommand("start", "Меню управления"), BotCommand("help", "Как это работает")]

HELP = (
    "Как пользоваться ботом\n\n"
    "1. Добавь тему, например «Спорт». Это название для тебя, не новый канал Telegram.\n"
    "2. В теме нажми «Куда публиковать» и укажи свой канал.\n"
    "3. В «Откуда брать посты» добавь ссылки на каналы-источники.\n\n"
    "Новые посты будут обрабатываться автоматически. Рабочему аккаунту нужен доступ "
    "к источникам и право публиковать в твоём канале.\n\n"
    "«Настройки темы» — текст, просмотры, реакции и название.\n"
    "«Результат» — публикации и проверка доступов.\n"
    "«Управление» — клиенты, заявки и общая пауза твоих тем.\n\n"
    "Переносятся текст, фото с подписью, видео с подписью и без неё, альбомы фото и видео. "
    "У других вложений переносится только текст. Фото и фотоальбомы без подписи пропускаются.\n\n"
    "Просмотры и реакции заказываются отдельно: отправленный заказ ещё не означает, "
    "что сервис его выполнил."
)

NO_GROUP = (
    "Первый запуск\n\n"
    "Один раз создай группу для хранения тем и настроек. Нажми «Создать группу» "
    "и напиши название. Бот создаст её сам, вручную заполнять ничего не нужно.\n\n"
    "Если такая группа уже есть, нажми «Обновить список»."
)


class Panel:
    def __init__(self, client, cfg, registry, bot, store=None):
        self.client = client
        self.cfg = cfg
        self.registry = registry
        self.bot = bot
        self.cabinet = Cabinet(client, cfg, store) if store else None
        self.store = store
        self.owner = 0
        self.account = "аккаунт"
        self.awaiting: Dict[int, Tuple[str, int]] = {}
        self._input_version: Dict[int, int] = {}
        self.notice = ""
        # сколько постов тема выложила сегодня: читается перед экраном лимита, None - ещё не читали
        self.posts_today: Optional[int] = None
        # последнее экранное сообщение в чате: при открытии нового окна прежнее удаляется
        self.screens: Dict[int, int] = {}

    def setup(self) -> None:
        self.client.add_handler(MessageHandler(self.on_message, filters.private))
        self.client.add_handler(CallbackQueryHandler(self.on_button))

    def claim_owner(self, owner_id: int, account_name: str = "") -> None:
        """Владелец известен сразу после входа аккаунта: кнопки его, всё остальное - кабинеты.

        Пока бот панели ждёт свою паузу, владелец уже должен узнаваться,
        иначе его сообщение в боте уедет в чужой кабинет.
        """
        self.owner = owner_id
        self.account = account_name or "аккаунт"

    async def start(self, owner_id: int, account_name: str = "") -> None:
        self.claim_owner(owner_id, account_name)
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
            if self.cabinet:
                await self.cabinet.on_message(message)
                return
            log.warning("Панель: сообщение не от владельца (chat %s) - игнорирую.", message.chat.id)
            await call(message.reply, "Этот бот управляющий: он слушает только владельца аккаунта.")
            return

        text = (message.text or "").strip()
        self._input_version[message.chat.id] = self._input_version.get(message.chat.id, 0) + 1
        kind, topic_id = self.awaiting.pop(message.chat.id, ("", 0))
        if kind and text and not text.startswith("/"):
            await self._apply_text(message, kind, topic_id, text)
            return

        command = text.split()[0].split("@")[0].lower() if text else ""
        body, buttons = self._view("help" if command == "/help" else "main")
        sent = await call(message.reply, body, reply_markup=buttons)
        if sent is not None:
            await self._forget_screen(message.chat.id, sent.id)

    async def on_button(self, client, query) -> None:
        if query.from_user.id not in self.admins:
            if self.cabinet:
                await self.cabinet.on_button(query)
                return
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

        self._input_version[query.from_user.id] = self._input_version.get(query.from_user.id, 0) + 1
        self.awaiting.pop(query.from_user.id, None)
        if key in ("main", "topics", "status", "help", "admin", "rstate"):
            page = int(parts[1]) if key == "topics" and len(parts) > 1 and parts[1].isdigit() else 0
            await self._show(query, key, page)
        elif key in ("cat", "sources", "settings"):
            page = int(parts[2]) if key == "sources" and len(parts) > 2 and parts[2].isdigit() else 0
            await self._show(query, key, arg, page=page)
        elif key == "drop":
            await self._show(query, "drop", arg)
        elif key == "drop_yes":
            await self._remove_category(query, arg)
        elif key in ("add", "channel", "rename", "target", "group", "group_rename"):
            self.awaiting[query.from_user.id] = {
                "add": ("donor", arg),
                "channel": ("channel", 0),
                "rename": ("rename", arg),
                "target": ("target", arg),
                "group": ("group", 0),
                "group_rename": ("group_rename", 0),
            }[key]
            await self._show(query, key, arg)
        elif key == "cv":
            if len(parts) > 2 and parts[2].isdigit():
                await self._apply_category_views(query, arg, parts[2])
            else:
                self.awaiting[query.from_user.id] = ("cviews", arg)
                await self._show(query, "cviews", arg)
        elif key == "cq":
            if len(parts) > 2 and parts[2].replace("-", "").isdigit():
                await self._apply_category_react(query, arg, parts[2])
            else:
                self.awaiting[query.from_user.id] = ("creact", arg)
                await self._show(query, "creact", arg)
        elif key == "pl":
            self.posts_today = await self.bot.topic_posts_used(arg)
            if len(parts) > 2 and parts[2].isdigit():
                await self._apply_category_posts(query, arg, parts[2])
            else:
                self.awaiting[query.from_user.id] = ("cpl", arg)
                await self._show(query, "cpl", arg)
        elif key == "cr":
            await self._toggle_category_reactions(query, arg)
        elif key == "cw":
            await self._toggle_category_rewrite(query, arg)
        elif key == "doc":
            await self._send_document(query, arg)
        elif key == "react":
            self.awaiting[query.from_user.id] = ("react_link", 0)
            await self._show(query, "react")
        elif key == "del":
            message_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            await self._remove(query, arg, message_id)
        elif key in ("pause", "start"):
            self.registry.paused = key == "pause"
            self.notice = (
                "Включена пауза для всех твоих тем. Кабинеты клиентов не затронуты."
                if self.registry.paused else "Автопубликация во всех твоих темах возобновлена."
            )
            await self._show(query, "admin", flash=self.notice)
        elif key == "update":
            await self._rebuild(query)
        elif key == "check":
            await self._diagnose(query)
        elif key == "catchup":
            await self._catch_up(query)
        elif key in ("money", "mpay", "mno", "clients", "credit"):
            if self.store is None:
                await call(query.answer, "Кабинеты клиентов пока не подключены — заявок на оплату нет.", show_alert=True)
                return
            if key == "money":
                await self._money_view(query)
            elif key in ("mpay", "mno"):
                await self._decide_topup(query, arg, accept=key == "mpay")
            elif key == "clients":
                await self._clients_view(query)
            else:
                self.awaiting[query.from_user.id] = ("credit", arg)
                await self._show(query, "credit", arg, flash="Жду сумму")
        else:
            await call(query.answer, "Такой кнопки нет - нажми «Меню».")

    async def _remove(self, query, topic_id: int, message_id: int) -> None:
        removed = await self.registry.remove_donor(message_id)
        names = ", ".join(donor.name for donor in removed)
        self.notice = f"✖ Перестал читать: {names}."
        await self._show(query, "sources", topic_id, flash=self.notice)

    async def _remove_category(self, query, topic_id: int) -> None:
        category = await self.registry.remove_category(topic_id)
        self.notice = (
            f"Тема «{category.name}» удалена. Её настройки и список источников убраны. "
            "Сам канал Telegram и уже опубликованные посты остались."
        )
        await self._show(query, "topics", flash=self.notice)

    async def _apply_category_views(self, query, topic_id: int, raw: str) -> None:
        version = self._input_version.get(query.from_user.id, 0)
        try:
            number, top = await self.registry.save_category_views(topic_id, raw)
        except ValueError as exc:
            self.notice = str(exc)
            if self._input_version.get(query.from_user.id, 0) == version:
                self.awaiting[query.from_user.id] = ("cviews", topic_id)
            await self._show(query, "cviews", topic_id)
            return
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "cat", topic_id)
            return
        shown = f"{number}-{top}" if top else str(number)
        self.notice = (
            f"Просмотры темы «{self._name(topic_id)}»: случайное число от {number} до {top} на каждый пост."
            if top else
            (f"Просмотры темы «{self._name(topic_id)}»: заказывать {number} на каждый пост."
             if number else f"Для темы «{self._name(topic_id)}» просмотры не заказываются.")
        )
        await self._show(query, "settings", topic_id, flash=shown)

    async def _apply_category_react(self, query, topic_id: int, raw: str) -> None:
        version = self._input_version.get(query.from_user.id, 0)
        try:
            number, top = await self.registry.save_category_react_count(topic_id, raw)
        except ValueError as exc:
            self.notice = str(exc)
            if self._input_version.get(query.from_user.id, 0) == version:
                self.awaiting[query.from_user.id] = ("creact", topic_id)
            await self._show(query, "creact", topic_id)
            return
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "cat", topic_id)
            return
        self.notice = self._react_notice(topic_id, number, top)
        await self._show(query, "settings", topic_id)

    def _react_notice(self, topic_id: int, number: int, top) -> str:
        if top:
            return f"Реакции темы «{self._name(topic_id)}»: случайное число от {number} до {top} на каждый пост."
        return f"Реакции темы «{self._name(topic_id)}»: {number} на каждый пост."

    def _posts_notice(self, topic_id: int, number: int) -> str:
        if number:
            return (
                f"Тема «{self._name(topic_id)}»: не больше {number} постов в сутки."
                " Лишнее в тот же день пропускаем, на завтра не копим."
            )
        return f"Тема «{self._name(topic_id)}»: постов в сутки не ограничиваем."

    async def _apply_category_posts(self, query, topic_id: int, raw: str) -> None:
        version = self._input_version.get(query.from_user.id, 0)
        try:
            number = await self.registry.save_category_posts(topic_id, raw)
        except ValueError as exc:
            self.notice = str(exc)
            if self._input_version.get(query.from_user.id, 0) == version:
                self.awaiting[query.from_user.id] = ("cpl", topic_id)
            await self._show(query, "cpl", topic_id)
            return
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "cat", topic_id)
            return
        self.notice = self._posts_notice(topic_id, number)
        await self._show(query, "settings", topic_id, flash=str(number) if number else "нет")

    async def _toggle_category_reactions(self, query, topic_id: int) -> None:
        enabled = await self.registry.toggle_category_reactions(topic_id)
        if enabled and not self.bot.reactions.enabled:
            self.notice = "Выбор сохранён, но сервис реакций сейчас выключен. Заказы не отправляются."
        elif enabled:
            self.notice = "Для новых постов этой темы будут заказываться реакции. Выполнение зависит от сервиса."
        else:
            self.notice = "Новые заказы реакций для этой темы выключены."
        await self._show(query, "settings", topic_id, flash="Сохранено")

    async def _toggle_category_rewrite(self, query, topic_id: int) -> None:
        enabled = await self.registry.toggle_category_rewrite(topic_id)
        if enabled and (not self.cfg.ai_enabled or not self.cfg.minimax_api_key):
            self.notice = f"Выбор сохранён; {self.ai_line()}."
        elif enabled:
            self.notice = "Для новых постов этой темы выбрано переписывание текста нейросетью."
        else:
            self.notice = "Публикуем текст автора как есть."
        await self._show(query, "settings", topic_id, flash="Сохранено")

    async def _send_document(self, query, kind: int) -> None:
        """Документ приходит отдельными сообщениями: длинный текст не влезает в одно, а терять куски нельзя."""
        title, pages = legal.document(kind)
        await call(query.answer, title)
        for index, page in enumerate(pages):
            text = f"{title}\n\n{page}" if index == 0 else page
            buttons = markup([("К помощи", "help"), ("В кабинет", "main")]) if index == len(pages) - 1 else None
            await call(self.client.send_message, query.message.chat.id, cut(text, TEXT_LIMIT), reply_markup=buttons)

    async def _rebuild(self, query) -> None:
        await call(query.answer, "Перечитываю группу...")
        await self._edit(query, "⏳ Перечитываю темы группы и ссылки в них...")
        await self.registry.rebuild()
        problems = self.registry.problems()
        self.notice = "Список обновлён." + ("\n⚠ " + "; ".join(problems[:5]) if problems else "")
        await self._show(query, "main", flash="Готово")

    async def _diagnose(self, query) -> None:
        """Отвечает на «почему не публикует» цифрами: открывается ли канал и пускает ли наш."""
        await call(query.answer, "Проверяю каналы...")
        await self._edit(query, "⏳ Открываю последние посты в каналах, откуда берём тексты, и смотрю права в наших каналах...")
        report = await self.bot.diagnose()
        await self._edit(query, cut(f"Проверка всех твоих тем\n{self._measured_text(report)}", TEXT_LIMIT), self._check_buttons())

    def _check_buttons(self) -> InlineKeyboardMarkup:
        return markup(
            [("Опубликовать пропущенные посты", "catchup")],
            [("Проверить ещё раз", "check"), ("Обновить список", "update")],
            [("К результатам", "status"), ("В кабинет", "main")],
        )

    async def _catch_up(self, query) -> None:
        await call(query.answer, "Догоняю пропущенные посты...")
        await self._edit(query, "⏳ Публикую то, что не дошло само...")
        result = await self.bot.catch_up()
        self.notice = result
        await self._show(query, "main", flash="Готово")

    async def _money_view(self, query, flash: str = "") -> None:
        """Очередь заявок: платёж проверяешь ты, начисление - одна кнопка, повторить её уже нельзя."""
        if flash:
            self.notice = flash  # подсказка Telegram исчезает через секунду, а результат должен остаться на экране
        rows = await self.store.pending_topups()
        lines = ["💰 Заявки на пополнение", ""]
        if rows:
            lines.append("Проверь поступление и нажми «✔ Начислить» - сумма ляжет клиенту на баланс один раз.")
            lines.append("Время заявок — всемирное; для Москвы прибавь 3 часа.")
            lines.append("")
            for row in rows[:10]:
                lines.append(
                    f"№{row['id']} · клиент {row['tenant_id']} ({row.get('target') or 'канал не указан'}) · "
                    f"{row['amount_rub']} ₽ · заявил {self._moment(row.get('created_at'))}"
                )
            if len(rows) > 10:
                lines.append(f"...и ещё {len(rows) - 10}")
        else:
            lines.append("Очередь пуста: никто из клиентов денег ещё не заявлял.")
        buttons = [
            [(f"✔ Начислить {row['amount_rub']} ₽ (№{row['id']})", f"mpay|{row['id']}"), ("✖ Не нашёл", f"mno|{row['id']}")]
            for row in rows[:10]
        ]
        buttons.append([("Клиенты", "clients"), ("К управлению", "admin")])
        await self._edit(query, cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(*buttons))
        await call(query.answer, flash[:180])

    async def _decide_topup(self, query, payment_id: int, accept: bool) -> None:
        try:
            if accept:
                row = await self.store.confirm_topup(payment_id)
                flash = (
                    f"✅ Начислил {row['amount_rub']} ₽ клиенту {row['tenant_id']} - "
                    "теперь он купит тариф у себя в кабинете."
                )
            else:
                tenant_id = await self.store.reject_topup(payment_id)
                flash = f"✅ Заявку №{payment_id} убрал, денег не начислял (клиент {tenant_id})."
        except StoreError as exc:
            flash = f"⚠ {exc}"
        await self._money_view(query, flash=flash)

    async def _clients_view(self, query, flash: str = "") -> None:
        """Клиенты одним экраном: тариф, баланс и последний пост - по нему сразу видно, идёт ли у человека."""
        tenants = await self.store.list_tenants()
        lines = [f"👥 Клиенты: {len(tenants)}", ""]
        if tenants:
            lines.append("«💵 Начислить» - если человек перевёл без заявки.")
            lines.append("")
            for tenant in tenants[:15]:
                plan = f"тариф ещё {tenant.days_left()} дн." if tenant.active() else "без тарифа"
                lines.append(
                    f"{tenant.id} · {tenant.target_title or tenant.target or 'канал не указан'} · "
                    f"{plan} · {tenant.balance} ₽ · {tenant.last_post_phrase()}"
                    f"{'' if tenant.target_ok else ' · ⚠ нет доступа к каналу'}"
                )
            if len(tenants) > 15:
                lines.append(f"...и ещё {len(tenants) - 15}")
        else:
            lines.append("Кабинетов пока никто не открывал.")
        buttons = [[(f"💵 Начислить {tenant.id}", f"credit|{tenant.id}")] for tenant in tenants[:15]]
        buttons.append([("Заявки", "money"), ("К управлению", "admin")])
        await self._edit(query, cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(*buttons))
        await call(query.answer, flash[:180])

    @staticmethod
    def _moment(value) -> str:
        """Время заявки в человеческом виде: база отдаёт datetime, а в тестах бывает строка."""
        fmt = getattr(value, "strftime", None)
        return fmt("%d.%m %H:%M") if callable(fmt) else str(value or "?")

    async def _apply_text(self, message, kind: str, topic_id: int, text: str) -> None:
        registry = self.registry
        version = self._input_version.get(message.chat.id, 0)
        try:
            if kind == "donor":
                added = await registry.add_donor(topic_id, cut(text, DONOR_LIMIT))
                self.notice = "✅ Добавил каналы, откуда берём посты: " + ", ".join(donor.name for donor in added)
                screen = ("sources", topic_id)
            elif kind == "react_link":
                self.notice = await self.bot.reactions.request(cut(text, LINK_LIMIT), self._notifier(message.chat.id))
                screen = ("rstate", 0)
            elif kind == "channel":
                category = await registry.add_category(text[:NAME_LIMIT])
                self.notice = self._category_notice(
                    category,
                    f"Тема «{category.name}» создана. Дальше: «Куда публиковать», затем «Откуда брать посты»",
                )
                screen = ("cat", category.topic_id)
            elif kind == "target":
                category = await registry.set_target(topic_id, text[:NAME_LIMIT])
                self.notice = self._category_notice(
                    category, f"Постим в {category.target_name} - название темы осталось «{category.name}»"
                )
                screen = ("cat", topic_id)
            elif kind == "group":
                name = await registry.create_storage_group(text[:NAME_LIMIT])
                self.notice = (
                    f"✅ Создал группу «{name}». Её владелец - {self.account}. "
                    "Это служебная группа: в ней бот хранит настройки. Нажми «Добавить тему»."
                )
                screen = ("main", 0)
            elif kind == "group_rename":
                name = await registry.rename_storage_group(text)
                self.notice = f"Группа теперь называется «{name}». Каналы и темы внутри не изменились."
                screen = ("admin", 0)
            elif kind == "cviews":
                number, top = await registry.save_category_views(topic_id, text)
                self.notice = (
                    f"Просмотры темы «{self._name(topic_id)}»: случайное число от {number} до {top} на каждый пост."
                    if top else
                    (f"Просмотры темы «{self._name(topic_id)}»: заказывать {number} на каждый пост."
                     if number else f"Для темы «{self._name(topic_id)}» просмотры не заказываются.")
                )
                screen = ("settings", topic_id)
            elif kind == "creact":
                number, top = await registry.save_category_react_count(topic_id, text)
                self.notice = self._react_notice(topic_id, number, top)
                screen = ("settings", topic_id)
            elif kind == "cpl":
                number = await registry.save_category_posts(topic_id, text)
                self.posts_today = await self.bot.topic_posts_used(topic_id)
                self.notice = self._posts_notice(topic_id, number)
                screen = ("settings", topic_id)
            elif kind == "credit":
                digits = "".join(char for char in text if char.isdigit())
                if not digits:
                    raise ValueError("Нужна сумма цифрами, например 600.")
                tenant = await self.store.credit(topic_id, int(digits))
                self.notice = (
                    f"✅ Клиенту {topic_id} начислено {digits} ₽, на балансе {tenant.balance} ₽ -"
                    "тариф он купит сам в кабинете."
                )
                screen = ("admin", 0)
            else:
                category = await registry.rename_category(topic_id, text[:NAME_LIMIT])
                self.notice = self._category_notice(
                    category,
                    f"Тема переименована в «{category.name}», канал для постов не трогал - "
                    f"постим в {category.target_name or 'не задано'}",
                )
                screen = ("cat", topic_id)
        except (ValueError, StoreError, RegistryError) as exc:
            self.notice = str(exc)
            if self._input_version.get(message.chat.id, 0) == version:
                self.awaiting[message.chat.id] = (kind, topic_id)
            screen = self._fail_screen(kind, topic_id)
        except Exception as exc:  # noqa: BLE001 - действие всегда должно заканчиваться ответом
            log.error("Действие из панели упало: %s: %s", type(exc).__name__, exc)
            self.notice = f"⚠ Ошибка: {type(exc).__name__}"
            screen = ("main", 0)

        if self._input_version.get(message.chat.id, 0) != version:
            note, self.notice = self.notice, ""
            if note:
                await call(message.reply, cut(note, TEXT_LIMIT))
            return
        body, buttons = self._view(*screen)
        sent = await call(message.reply, body, reply_markup=buttons)
        if sent is not None:
            await self._forget_screen(message.chat.id, sent.id)

    def _fail_screen(self, kind: str, topic_id: int) -> Tuple[str, int]:
        """После отказа возвращаемся к тому экрану, где просили ввод, - чтобы можно было написать снова."""
        return ({"donor": "add", "react_link": "react"}.get(kind, kind), topic_id)

    def _notifier(self, chat_id: int):
        """Ручной заказ длится несколько минут: по окончанию Reactor пишет результат в этот чат."""

        async def notify(text: str) -> None:
            await call(self.client.send_message, chat_id, cut(text, TEXT_LIMIT))

        return notify

    @staticmethod
    def _category_notice(category, head: str) -> str:
        return f"✅ {head}." + (f"\n⚠ {category.problem}" if category.problem else "")

    # ==== экраны ====

    def _view(self, kind: str, topic_id: int = 0, page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        if kind == "topics":
            return self._topics_view(topic_id)
        if kind == "cat":
            return self._category_view(topic_id)
        if kind == "sources":
            return self._sources_view(topic_id, page)
        if kind == "settings":
            return self._settings_view(topic_id)
        if kind == "admin":
            return self._admin_view()
        if kind == "rstate":
            return self._reactions_view()
        if kind == "status":
            return self._status_view()
        if kind == "help":
            return cut(self._with_notice(HELP), TEXT_LIMIT), markup(
                [("Мои темы", "topics")], [("Соглашение", "doc|1"), ("Политика", "doc|2")],
                [("В кабинет", "main")],
            )
        if kind == "add":
            return self._add_view(topic_id)
        if kind == "channel":
            return self._new_channel_view()
        if kind == "group":
            return self._new_group_view()
        if kind == "rename":
            return self._rename_view(topic_id)
        if kind == "target":
            return self._target_view(topic_id)
        if kind == "drop":
            return self._drop_view(topic_id)
        if kind == "group_rename":
            return self._rename_group_view()
        if kind == "cviews":
            return self._category_views_view(topic_id)
        if kind == "cpl":
            return self._category_posts_view(topic_id)
        if kind == "creact":
            return self._category_react_view(topic_id)
        if kind == "react":
            return self._react_view()
        if kind == "credit":
            return self._credit_view(topic_id)
        return self._main_view()

    def _credit_view(self, tenant_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        return (
            cut(
                self._with_notice(
                    f"Клиент {tenant_id}: напиши сумму цифрами, например 600.\n\n"
                    "Она попадёт ему на баланс, и тариф он купит сам в своём кабинете - "
                    "так начисление видно и у тебя в журнале, и у него в истории платежей."
                ),
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", "clients")]),
        )

    def _main_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        registry = self.registry
        if not registry.ready or not registry.group_id:
            return cut(self._with_notice(NO_GROUP), TEXT_LIMIT), markup(
                [("Создать группу", "group"), ("Обновить список", "update")],
                [("Помощь", "help"), ("Управление", "admin")],
            )

        if registry.paused:
            state = "Публикации твоих тем на паузе. Возобновить — в «Управлении»."
        elif not registry.categories:
            state = "Начни с кнопки «Добавить тему»."
        elif registry.problems():
            state = "Есть незавершённые настройки. Открой «Мои темы»."
        else:
            state = "Автопубликация включена. Результаты — отдельной кнопкой ниже."
        text = f"АвтоПост · твой кабинет\n\n{state}\nТем: {len(registry.categories)}"
        return cut(self._with_notice(text), TEXT_LIMIT), markup(
            [("Мои темы", "topics"), ("Добавить тему", "channel")],
            [("Результат", "status")],
            [("Помощь", "help"), ("Управление", "admin")],
        )

    def ai_line(self) -> str:
        if not self.cfg.ai_enabled:
            return "нейросеть выключена — выходит текст автора"
        if not self.cfg.minimax_api_key:
            return "нейросеть недоступна — выходит текст автора"
        return "нейросеть доступна; используется только там, где выбрано переписывание"

    def _topics_view(self, page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        categories = list(self.registry.categories.values())
        page = min(max(0, page), max(0, (len(categories) - 1) // 20))
        shown = categories[page * 20:(page + 1) * 20]
        lines = ["Мои темы", "", "Открой тему, чтобы настроить публикации."]
        if not categories:
            lines = ["Мои темы", "", "Тем пока нет. Добавь первую, например «Спорт»."]
        for category in shown:
            where = category.target_name or "канал не указан"
            lines.append(f"\n{category.name} → {where}\nИсточников: {len(category.donors)}")
        rows = [[(category.name[:50], f"cat|{category.topic_id}")] for category in shown]
        pages = []
        if page:
            pages.append(("Предыдущие темы", f"topics|{page - 1}"))
        if (page + 1) * 20 < len(categories):
            pages.append(("Следующие темы", f"topics|{page + 1}"))
        if pages:
            rows.append(pages)
        return cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(
            *rows, [("Добавить тему", "channel")], [("В кабинет", "main")],
        )

    def _category_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        if category is None:
            return self._with_notice("Этой темы уже нет в списке."), markup([("Мои темы", "topics")])
        if self.registry.paused:
            state = "Общая пауза твоих тем. Возобновить можно в «Управлении»."
        elif not category.target:
            state = "Сначала укажи канал для публикаций."
        elif not category.donors:
            state = "Теперь добавь каналы, откуда брать посты."
        elif category.problem:
            state = "Нужно проверить настройки и доступ к каналам."
        else:
            state = "Канал и источники заданы. Новые посты обрабатываются автоматически."
        lines = [
            f"Тема «{category.name}»", "",
            f"Куда публиковать: {category.target_name or 'не указано'}",
            f"Откуда брать посты: источников — {len(category.donors)}", "", state,
        ]
        if category.problem:
            lines += ["", category.problem]
        return cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(
            [("Куда публиковать", f"target|{topic_id}")],
            [("Откуда брать посты", f"sources|{topic_id}")],
            [("Настройки темы", f"settings|{topic_id}")],
            [("Мои темы", "topics"), ("В кабинет", "main")],
        )

    def _sources_view(self, topic_id: int, page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        if category is None:
            return self._category_view(topic_id)
        donors = list(category.donors.values())
        page = min(max(0, page), max(0, (len(donors) - 1) // MAX_DONOR_BUTTONS))
        shown = donors[page * MAX_DONOR_BUTTONS:(page + 1) * MAX_DONOR_BUTTONS]
        lines = [f"Тема «{category.name}» · откуда брать посты", ""]
        lines += [donor.name[:100] for donor in shown] or ["Источников пока нет. Нажми «Добавить источник»."]
        if donors:
            lines += ["", "Удаление источника прекращает чтение его новых постов."]
        rows = [[(f"Убрать {donor.name[:28]}", f"del|{topic_id}|{donor.message_id}") for donor in group]
                for group in chunks(shown, 2)]
        pages = []
        if page:
            pages.append(("Предыдущие", f"sources|{topic_id}|{page - 1}"))
        if (page + 1) * MAX_DONOR_BUTTONS < len(donors):
            pages.append(("Следующие", f"sources|{topic_id}|{page + 1}"))
        if pages:
            rows.append(pages)
        return cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(
            [("Добавить источник", f"add|{topic_id}")], *rows, [("К теме", f"cat|{topic_id}")],
        )

    def _settings_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        if category is None:
            return self._category_view(topic_id)
        rewrite = self.registry.rewrite_for(category)
        views = self.registry.views_for(category)
        views_top = self.registry.views_max_for(category)
        reactions = self.registry.reactions_for(category)
        if not rewrite:
            text_state = "словами автора"
        elif not self.cfg.ai_enabled or not self.cfg.minimax_api_key:
            text_state = f"{self.ai_line()}. Выбор переписывания сохранён"
        else:
            text_state = "переписываем с помощью нейросети"
        if not views:
            views_state = "не заказывать"
        elif views_top:
            views_state = f"заказывать случайное число от {views} до {views_top} на каждый пост"
        else:
            views_state = f"заказывать {views} на каждый пост"
        reaction_state = "заказывать" if reactions else "не заказывать"
        if reactions and not self.bot.reactions.enabled:
            reaction_state = "выбраны, но сервис реакций сейчас выключен"
        posts_cap = self.registry.posts_for(category)
        lines = [
            f"Настройки темы «{category.name}»", "",
            f"Текст: {text_state}.",
            f"Просмотры: {views_state}.",
            f"Посты в сутки: {f'не больше {posts_cap}' if posts_cap else 'не ограничивать'}.",
            f"Реакции: {reaction_state}.",
            f"Реакций на пост: {self.registry.react_text(category)}.", "",
            "Это настройки новых постов, не отчёт о выполненных заказах.",
        ]
        return cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(
            [("Оставить текст автора" if rewrite else "Выбрать переписывание", f"cw|{topic_id}")],
            [("Просмотры на пост", f"cv|{topic_id}")],
            [("Посты в сутки", f"pl|{topic_id}")],
            [("Не заказывать реакции" if reactions else "Заказывать реакции", f"cr|{topic_id}")],
            [("Реакций на пост", f"cq|{topic_id}")],
            [("Изменить название", f"rename|{topic_id}")],
            [("Удалить тему", f"drop|{topic_id}")],
            [("К теме", f"cat|{topic_id}")],
        )

    def _admin_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        paused = self.registry.paused
        rows = [[("Клиенты", "clients"), ("Заявки на оплату", "money")]] if self.store else []
        rows += [
            [("Возобновить мои публикации", "start") if paused else ("Пауза моих публикаций", "pause")],
            [("Проверить мои каналы", "check"), ("Обновить список", "update")],
            [("Заказы реакций", "rstate")],
            [("Переименовать служебную группу", "group_rename")],
            [("В кабинет", "main")],
        ]
        text = (
            "Управление\n\n"
            f"Мои публикации: {'общая пауза' if paused else 'включены'}.\n"
            "Пауза относится ко всем твоим темам, но не к кабинетам клиентов. "
            "Уже начатые публикации и заказы просмотров и реакций она не отменяет.\n\n"
            "После перезапуска бота публикации возобновляются."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup(*rows)

    def _clients_note(self) -> str:
        """Клиентская часть в статусе: когда проверяли кабинеты и что не открывается.

        Без базы строки нет - владельцу не показываются несуществующие клиенты.
        """
        clients = getattr(self.bot, "clients", None)
        return f"\n{clients.report()}" if clients else ""

    def _status_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        stats = self.bot.stats
        lines = [
            "Результат", "",
            "За время текущего запуска бота, включая кабинеты клиентов:",
            f"Опубликовано постов: {stats['published']}",
            f"Последняя публикация: {self.bot.last_publish or 'после запуска ещё не зафиксирована'}",
            f"Ошибок обработки: {stats['errors']}", "",
            "Эти числа не являются историей за всё время. После перезапуска отсчёт начинается заново.",
        ]
        if self.registry.paused:
            lines += ["", "Твои темы сейчас на общей паузе."]
        problems = self.registry.problems()
        if problems:
            lines += ["", "Нужно проверить:", *problems[:5]]
        lines.append(self._clients_note())
        return cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(
            [("Проверить мои каналы", "check")],
            [("Заказы реакций", "rstate")],
            [("Обновить результат", "status")],
            [("Мои темы", "topics"), ("В кабинет", "main")],
        )

    @staticmethod
    def _measured_text(text: str) -> str:
        """Изменение счётчика не доказывает, кто именно поставил реакции."""
        return text.replace("сервис не добавил ничего", "изменение не зафиксировано").replace(
            "сервис добавил", "число реакций выросло на"
        )

    def _reactions_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        reactor = self.bot.reactions
        text = (
            "Заказы реакций\n\n"
            f"Автоматические заказы: {'включены' if reactor.enabled else 'выключены'}.\n"
            f"Последняя отправленная ссылка: {reactor.last_link or 'после запуска ещё не было'}.\n"
            f"Ответ на последнюю отправку: {reactor.last_reply or 'пока нет ответа'}.\n\n"
            "Отправка заказа не подтверждает доставку реакций. Замеры показывают изменение "
            "числа, но не позволяют установить, добавил ли реакции именно сервис.\n\n"
            f"{self._measured_text(reactor.report())}"
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup(
            [("Заказать для одного поста", "react")], [("Обновить", "rstate")],
            [("К результатам", "status"), ("Управление", "admin")],
        )

    def _add_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            f"Тема «{self._name(topic_id)}» · добавить источник\n\n"
            "Пришли ссылку: @name или https://t.me/name. Можно несколько ссылок одним сообщением.\n\n"
            "Рабочий аккаунт должен иметь доступ к источнику и быть на него подписан."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup([("Отмена", f"sources|{topic_id}")])

    def _new_channel_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            "Добавить тему\n\nНапиши название, например «Спорт».\n\n"
            "Это название раздела с настройками, не новый канал Telegram. "
            "Затем укажешь, куда публиковать и откуда брать посты. "
            "Если доступный канал уже называется так же, бот подставит его — проверь в карточке темы."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup([("Отмена", "topics")])

    def _new_group_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            "Напиши название группы, например «АвтоПост».\n\n"
            "Бот создаст группу с темами для хранения настроек. Ты станешь её владельцем. "
            "Посты туда не публикуются, вручную заполнять ничего не нужно."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup([("Отмена", "main")])

    def _rename_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            f"Тема «{self._name(topic_id)}»\n\nНапиши новое название.\n"
            "Канал для публикаций не изменится."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup([("Отмена", f"settings|{topic_id}")])

    def _target_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        current = (category.target_name or 'не указано') if category else 'не указано'
        text = (
            f"Тема «{self._name(topic_id)}» · куда публиковать\nСейчас: {current}\n\n"
            "Пришли адрес своего канала, например @sport_ch или https://t.me/sport_ch.\n\n"
            "Рабочему аккаунту нужно право публиковать в этом канале. "
            "Название темы от смены канала не изменится."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup([("Отмена", f"cat|{topic_id}")])

    def _drop_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            f"Удалить тему «{self._name(topic_id)}»?\n\n"
            "Её настройки и список источников будут удалены. Новые посты по этой теме "
            "больше не будут публиковаться. Отменить удаление нельзя.\n\n"
            "Канал Telegram и уже опубликованные посты останутся."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup(
            [("Да, удалить тему", f"drop_yes|{topic_id}")], [("Отмена", f"settings|{topic_id}")],
        )

    def _rename_group_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            f"Группа настроек: «{self.registry.group_name}».\n\n"
            "Напиши новое название группы. Темы, каналы и источники не изменятся."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup([("Отмена", "admin")])

    def _category_views_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        current = self.registry.views_for(category) if category else self.cfg.promo_views
        top = self.registry.views_max_for(category) if category else self.cfg.promo_views_max
        if not current:
            shown = "не заказывать"
        elif top:
            shown = f"случайное число от {current} до {top} на каждый пост"
        else:
            shown = f"{current} на каждый пост"
        text = (
            f"Тема «{self._name(topic_id)}» · просмотры\nСейчас: {shown}.\n\n"
            f"Напиши число от 0 до {VIEWS_LIMIT} или диапазон, например 1200-1600, либо выбери кнопкой. "
            "Ноль — не заказывать просмотры. Диапазон — случайное число из него на каждый новый пост.\n\n"
            "Это размер заказа для новых постов, а не гарантия выполненных просмотров."
        )
        presets = [(str(count), f"cv|{topic_id}|{count}") for count in (500, 1000, 2000, 5000)]
        return cut(self._with_notice(text), TEXT_LIMIT), markup(
            presets[:2], presets[2:], [("Не заказывать", f"cv|{topic_id}|0")],
            [("Отмена", f"settings|{topic_id}")],
        )

    def _category_react_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        shown = self.registry.react_text(category) if category else "по умолчанию"
        low = self.cfg.smmplus_min
        text = (
            f"Тема «{self._name(topic_id)}» · реакций на пост\nСейчас: {shown}.\n\n"
            f"Напиши число от {low} до 50 или диапазон, например 10-30, либо выбери кнопкой. "
            "Диапазон — случайное число из него на каждый новый пост. "
            f"Меньше {low} сервис не выполняет.\n\n"
            "Это размер заказа, а не гарантия выполненных реакций. Работает при заданном SMMPLUS_API_KEY."
        )
        presets = [(str(n), f"cq|{topic_id}|{n}") for n in (10, 20, 30, 50) if n >= low]
        return cut(self._with_notice(text), TEXT_LIMIT), markup(
            presets[:2], presets[2:],
            [(f"Случайно {low}-50", f"cq|{topic_id}|{low}-50")],
            [("Отмена", f"settings|{topic_id}")],
        )

    def _category_posts_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        cap = self.registry.posts_for(category) if category else None
        today_line = f"Сегодня в канал вышло: {self.posts_today}.\n" if self.posts_today is not None else ""
        text = (
            f"Тема «{self._name(topic_id)}» · посты в сутки\n"
            f"Сейчас: {f'не больше {cap} в сутки' if cap else 'не ограничивать'}.\n{today_line}\n"
            f"Напиши число от 0 до {POSTS_LIMIT} или выбери кнопкой. Ноль — не ограничивать. "
            "Что не влезло в лимит за день, не публикуется и на завтра не копится. "
            "Сутки считаются по Москве: после полуночи счётчик обнуляется, счётчик живёт в базе и переживает перезапуск.\n\n"
            "Лимит считается на тему целиком: не важно, из какого источника пришёл пост."
        )
        presets = [(str(count), f"pl|{topic_id}|{count}") for count in (5, 10, 20, 50)]
        return cut(self._with_notice(text), TEXT_LIMIT), markup(
            presets[:2], presets[2:], [("Не ограничивать (0)", f"pl|{topic_id}|0")],
            [("Отмена", f"settings|{topic_id}")],
        )

    def _react_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        reactor = self.bot.reactions
        text = (
            "Заказать реакции для одного поста\n\n"
            + ("Пришли ссылку: https://t.me/канал/номер.\n"
               if reactor.enabled else "Сервис реакций сейчас выключен, заказ не отправится.\n")
            + "Бот проверит, открывается ли пост. Ручной заказ получит приоритет в очереди. "
            "Результат отправки придёт сюда; доставка реакций зависит от сервиса."
        )
        return cut(self._with_notice(text), TEXT_LIMIT), markup([("Отмена", "rstate")])

    def _name(self, topic_id: int) -> str:
        category = self.registry.categories.get(topic_id)
        if not category:
            return "?"
        return category.name

    def _with_notice(self, text: str) -> str:
        notice, self.notice = self.notice, ""
        return f"{notice}\n\n{text}" if notice else text

    # ==== вывод ====

    async def _forget_screen(self, chat_id: int, keep: int) -> None:
        """В чате живёт одно окно: прежнее экранное сообщение удаляем, чтобы старые кнопки не плодились."""
        previous, self.screens[chat_id] = self.screens.get(chat_id), keep
        if previous and previous != keep:
            try:
                await call(self.client.delete_messages, chat_id, previous)
            except Exception as exc:  # noqa: BLE001 - окно могли удалить и без нас, это не повод портить экран
                log.debug("Прежний экран %s удалить не удалось: %s", previous, type(exc).__name__)

    async def _show(self, query, kind: str, topic_id: int = 0, flash: str = "", page: int = 0) -> None:
        text, buttons = self._view(kind, topic_id, page)
        await self._edit(query, text, buttons)
        await call(query.answer, flash[:180])

    async def _edit(self, query, text: str, buttons: Optional[InlineKeyboardMarkup] = None) -> None:
        message = query.message
        try:
            await call(
                self.client.edit_message_text, message.chat.id, message.id, cut(text, TEXT_LIMIT), reply_markup=buttons
            )
        except MessageNotModified:
            pass
        await self._forget_screen(message.chat.id, message.id)
