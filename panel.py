"""Управление ботом кнопками в твоём боте: куда постить, откуда читать и что делать с текстом.

Бот работает в одном процессе с юзерботом, поэтому нажимает действия прямо
через реестр: список по-прежнему живёт в группе с темами, но руками в ней
ничего писать не нужно. Настройки канала (переписывать ли текст, просмотры,
реакции) бот записывает служебным сообщением в его тему - после перезапуска
они восстанавливаются сами.
"""

import logging
from typing import Optional, Tuple

from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, InlineKeyboardMarkup

import legal
from ai import cut
from cabinet import Cabinet
from config import VIEWS_LIMIT
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
    "Как пользоваться ботом\n"
    "\n"
    "Бот делает три вещи: читает посты в каналах, которые ты укажешь, выкладывает их в твои "
    "каналы той же картинкой и теми же словами, что в источнике, и добавляет просмотры и "
    "реакции - у каждого канала своё.\n"
    "Текст можно поручить нейросети MiniMax: она перепишет пост своими словами, картинка "
    "останется та же. Это выбор человека - кнопка «✏️ Текст» у каждого канала, а общий "
    "выключатель - переменная REWRITE (REWRITE=0 - нейросеть не работает ни у одного канала).\n"
    "\n"
    "Настройка идёт по порядку: сначала канал, куда постить, потом каналы, откуда брать посты.\n"
    "\n"
    "1) «🏛 Создать группу» - один раз. Бот создаст группу, в которой хранится список каналов. "
    "Руками в ней писать ничего не нужно.\n"
    "2) «➕ Добавить канал» - напиши название, например «спортивные новости». Бот создаст с ним "
    "тему в своей группе; это только название для тебя, новый канал в Telegram он не создаёт.\n"
    "2а) В этом же экране - «📤 Куда постить»: пришли @юзернейм своего канала, который уже есть. "
    "Если канал с таким же названием, как тема, уже открыт этому аккаунту, бот подставит его сам.\n"
    "3) Открой канал в «🗂 Мои каналы» и нажми «➕ Читать ещё канал» - пришли ссылку "
    "(@юзернейм или https://t.me/...), откуда брать посты. Сколько ссылок в сообщении, "
    "столько каналов и добавится.\n"
    "4) Кнопка «✖ @имя» под каналом - перестать его читать.\n"
    "5) В том же экране канала - три переключателя, каждый касается только этого канала:\n"
    "   «✏️ Текст: переписывает ИИ» - выключи, и посты будут выходить словами автора.\n"
    "      Пока нейросеть выключена общей переменной REWRITE=0, эта кнопка сохраняет твой "
    "выбор, но пост всё равно выходит словами автора.\n"
    f"   «👁 Просмотры на пост» - число от 0 до {VIEWS_LIMIT} на каждый пост, 0 - не накручивать.\n"
    "   «🎭 Реакции: ставим / не ставим» - ставить ли под посты бесплатные реакции.\n"
    "   «✏️ Название темы» - переименовать тему как тебе удобно; канал, куда постить, "
    "от этого не меняется.\n"
    "\n"
    "Просмотры, реакции и переписывание бот сохраняет в свою группу, поэтому после "
    "перезапуска ничего не сбивается.\n"
    "\n"
    "6) «🩺 Проверка: почему не идёт» - отвечает на «почему не публикует»: открывается ли "
    "канал, откуда берём посты, когда там был последний пост, пускает ли Telegram в твой "
    "канал публиковать и сколько реакций стоит под свежими постами. "
    "«⤵️ Догнать пропущенные посты» выкладывает то, что не дошло само.\n"
    "7) «⏸ Пауза - не публиковать» - посты ловим, но не публикуем. "
    "«▶ Пуск - возобновить публикацию» - снова работаем.\n"
    "8) «🎯 Реакция на конкретный пост» - заказать реакции для одного поста: после кнопки "
    "пришли ссылку https://t.me/канал/номер. Бот сначала сам откроет этот пост: не увидит - заказ "
    "не потратит. Результат напишет здесь же, через несколько минут.\n"
    "9) «💰 Заявки на оплату» и «👥 Клиенты» - кабинеты клиентов: заявки, балансы и "
    "начисления руками, если человек перевёл мимо заявки.\n"
    "10) «📄 Соглашение» и «🔒 Политика» - документы сервиса, читаются прямо здесь.\n"
    "\n"
    "Про реакции честно: сервис smmnakrutka.ru принимает заказ раз в 5 минут, и каждый новый "
    "пост ждёт своей очереди отдельно - один пост не вытесняет другой. Если сервис отвечает "
    "словом «лимит», бот не отступает на час, а пробует тот же пост ещё раз в следующем окне. "
    "Через 20 минут и через 2 часа бот пересчитывает реакции под постом - если сервис ничего "
    "не добавил, так и будет написано. Гарантий бесплатный сервис не даёт ни нам, ни себе.\n"
    "\n"
    "Что берется из поста источника: любой пост с текстом. С картинкой - той же картинкой "
    "(ничего не скачивается), альбом - пачкой карточек, без картинки - просто текстом. "
    "У поста с видео или документом переносим только текст, о чём пишет в журнал. Пост без "
    "текста не публикуем - перепечатывать нечего. Если источник - группа, а не канал, берём "
    "посты самого канала: переписку подписчиков не пишем."
)

NO_GROUP = (
    "Группы, где бот хранит список каналов, пока нет.\n\n"
    "Нажми «🏛 Создать группу» и напиши её название, например «АвтоПост». "
    "Группу создаст сам бот, ты в ней владелец - руками в ней ничего делать не нужно.\n\n"
    "Если такая группа уже есть и ты в ней владелец - нажми «🔄 Обновить список»."
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
        self.awaiting: Tuple[str, int] = ("", 0)
        self.notice = ""

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

        if key in ("main", "topics", "status", "help"):
            await self._show(query, key)
        elif key == "cat":
            await self._show(query, "cat", arg)
        elif key == "drop":
            await self._show(query, "drop", arg)
        elif key == "drop_yes":
            await self._remove_category(query, arg)
        elif key in ("add", "channel", "rename", "target", "group", "group_rename"):
            self.awaiting = {
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
                self.awaiting = ("cviews", arg)
                await self._show(query, "cviews", arg)
        elif key == "cr":
            await self._toggle_category_reactions(query, arg)
        elif key == "cw":
            await self._toggle_category_rewrite(query, arg)
        elif key == "doc":
            await self._send_document(query, arg)
        elif key == "react":
            self.awaiting = ("react_link", 0)
            await self._show(query, "react")
        elif key == "del":
            message_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            await self._remove(query, arg, message_id)
        elif key in ("pause", "start"):
            self.registry.paused = key == "pause"
            self.notice = "Пауза: посты ловлю, но не публикую." if self.registry.paused else "Работаю дальше."
            await self._show(query, "main", flash=self.notice)
        elif key == "update":
            await self._rebuild(query)
        elif key == "check":
            await self._diagnose(query)
        elif key == "catchup":
            await self._catch_up(query)
        elif key in ("money", "mpay", "mno", "clients", "credit"):
            if self.store is None:
                await call(query.answer, "Базы клиентов нет (DATABASE_URL не задан) - заявок на оплату тоже нет.", show_alert=True)
                return
            if key == "money":
                await self._money_view(query)
            elif key in ("mpay", "mno"):
                await self._decide_topup(query, arg, accept=key == "mpay")
            elif key == "clients":
                await self._clients_view(query)
            else:
                self.awaiting = ("credit", arg)
                await self._show(query, "credit", arg, flash="Жду сумму")
        else:
            await call(query.answer, "Такой кнопки нет - нажми «Меню».")

    async def _remove(self, query, topic_id: int, message_id: int) -> None:
        removed = await self.registry.remove_donor(message_id)
        names = ", ".join(donor.name for donor in removed)
        self.notice = f"✖ Перестал читать: {names}."
        await self._show(query, "cat", topic_id, flash=self.notice)

    async def _remove_category(self, query, topic_id: int) -> None:
        category = await self.registry.remove_category(topic_id)
        self.notice = (
            f"✅ Убрал канал «{category.target_name or category.name}» и его тему в группе - "
            "посты в него больше не публикуются."
        )
        await self._show(query, "topics", flash=self.notice)

    async def _apply_category_views(self, query, topic_id: int, raw: str) -> None:
        try:
            number = await self.registry.save_category_views(topic_id, raw)
        except ValueError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "cviews", topic_id)
            return
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "cat", topic_id)
            return
        self.notice = f"✅ Просмотры канала «{self._name(topic_id)}»: {number} на каждый пост."
        await self._show(query, "cat", topic_id, flash=str(number))

    async def _toggle_category_reactions(self, query, topic_id: int) -> None:
        try:
            enabled = await self.registry.toggle_category_reactions(topic_id)
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "cat", topic_id)
            return
        name = self._name(topic_id)
        self.notice = (
            f"🎭 Реакции канала «{name}» включены - каждый новый пост встанет в очередь."
            if enabled
            else f"🎭 Реакции канала «{name}» выключены - посты выходят без накрутки реакций."
        )
        if enabled and not self.bot.reactions.enabled:
            self.notice += "\n⚠ Реакции выключены полностью (переменная REACTIONS) - заказы не пойдут, пока не включишь её."
        await self._show(query, "cat", topic_id, flash=self.notice[:TEXT_LIMIT])

    async def _toggle_category_rewrite(self, query, topic_id: int) -> None:
        try:
            enabled = await self.registry.toggle_category_rewrite(topic_id)
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "cat", topic_id)
            return
        name = self._name(topic_id)
        self.notice = (
            f"✏️ Канал «{name}»: текст переписывает нейросеть - пост выходит своими словами, картинка та же."
            if enabled
            else f"✏️ Канал «{name}»: публикуем текст автора как есть, нейросеть не трогаем."
        )
        if enabled and not self.cfg.ai_enabled:
            self.notice += (
                "\n⚠ Сейчас нейросеть выключена для всех (REWRITE=0) - выбор сохранён, "
                "пост всё равно выходит словами автора. Включишь REWRITE=1 - заработает."
            )
        elif enabled and not self.cfg.minimax_api_key:
            self.notice += "\n⚠ Ключа MINIMAX_API_KEY нет - всё равно выходит текст автора."
        await self._show(query, "cat", topic_id, flash=self.notice[:TEXT_LIMIT])

    async def _send_document(self, query, kind: int) -> None:
        """Документ приходит отдельными сообщениями: длинный текст не влезает в одно, а терять куски нельзя."""
        title, pages = legal.document(kind)
        await call(query.answer, title)
        for index, page in enumerate(pages):
            text = f"{title}\n\n{page}" if index == 0 else page
            buttons = markup([("⬅️ Меню", "main")]) if index == len(pages) - 1 else None
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
        await self._edit(query, cut(f"🩺 Проверка\n{report}", TEXT_LIMIT), self._check_buttons())

    def _check_buttons(self) -> InlineKeyboardMarkup:
        return markup(
            [("⤵️ Догнать пропущенные посты", "catchup"), ("🩺 Проверить ещё раз", "check")],
            [("🔄 Обновить список", "update"), ("⬅️ Меню", "main")],
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
            lines.append("Время заявок - в UTC, как в журнале Railway.")
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
        buttons.append([("👥 Клиенты", "clients"), ("⬅️ Меню", "main")])
        await self._edit(query, cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(*buttons))
        await call(query.answer, flash[:TEXT_LIMIT])

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
        buttons.append([("💰 Заявки", "money"), ("⬅️ Меню", "main")])
        await self._edit(query, cut(self._with_notice("\n".join(lines)), TEXT_LIMIT), markup(*buttons))
        await call(query.answer, flash[:TEXT_LIMIT])

    @staticmethod
    def _moment(value) -> str:
        """Время заявки в человеческом виде: база отдаёт datetime, а в тестах бывает строка."""
        fmt = getattr(value, "strftime", None)
        return fmt("%d.%m %H:%M") if callable(fmt) else str(value or "?")

    async def _apply_text(self, message, kind: str, topic_id: int, text: str) -> None:
        registry = self.registry
        try:
            if kind == "donor":
                added = await registry.add_donor(topic_id, cut(text, DONOR_LIMIT))
                self.notice = "✅ Добавил каналы, откуда берём посты: " + ", ".join(donor.name for donor in added)
                screen = ("cat", topic_id)
            elif kind == "react_link":
                self.notice = await self.bot.reactions.request(cut(text, LINK_LIMIT), self._notifier(message.chat.id))
                screen = ("react", 0)
            elif kind == "channel":
                category = await registry.add_category(text[:NAME_LIMIT])
                self.notice = self._category_notice(
                    category,
                    f"Тема «{category.name}» создана. Дальше два шага: «📤 Куда постить» и «➕ Читать ещё канал»",
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
                    "Это служебная группа: в ней бот хранит список каналов. Жми «➕ Добавить канал»."
                )
                screen = ("main", 0)
            elif kind == "group_rename":
                name = await registry.rename_storage_group(text)
                self.notice = f"✅ Группа теперь называется «{name}». Каналы и темы внутри не изменились."
                screen = ("main", 0)
            elif kind == "cviews":
                number = await registry.save_category_views(topic_id, text)
                self.notice = f"✅ Просмотры канала «{self._name(topic_id)}»: {number} на каждый пост."
                screen = ("cat", topic_id)
            elif kind == "credit":
                digits = "".join(char for char in text if char.isdigit())
                if not digits:
                    raise ValueError("Нужна сумма цифрами, например 600.")
                tenant = await self.store.credit(topic_id, int(digits))
                self.notice = (
                    f"✅ Клиенту {topic_id} начислено {digits} ₽, на балансе {tenant.balance} ₽ -"
                    "тариф он купит сам в кабинете."
                )
                screen = ("main", 0)
            else:
                category = await registry.rename_category(topic_id, text[:NAME_LIMIT])
                self.notice = self._category_notice(
                    category,
                    f"Тема переименована в «{category.name}», канал для постов не трогал - "
                    f"постим в {category.target_name or 'не задано'}",
                )
                screen = ("cat", topic_id)
        except (ValueError, StoreError) as exc:
            self.notice = f"⚠ {exc}"
            screen = self._fail_screen(kind, topic_id)
            if kind == "credit":
                self.awaiting = ("credit", topic_id)  # сумму можно написать сразу, без нового нажатия
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            screen = self._fail_screen(kind, topic_id)
        except Exception as exc:  # noqa: BLE001 - действие всегда должно заканчиваться ответом
            log.error("Действие из панели упало: %s: %s", type(exc).__name__, exc)
            self.notice = f"⚠ Ошибка: {type(exc).__name__}"
            screen = ("main", 0)

        body, buttons = self._view(*screen)
        await call(message.reply, body, reply_markup=buttons)

    def _fail_screen(self, kind: str, topic_id: int) -> Tuple[str, int]:
        """После отказа возвращаемся к тому экрану, где просили ввод, - чтобы можно было написать снова."""
        if kind == "cviews":
            return ("cviews", topic_id)
        if kind == "target":
            return ("target", topic_id)
        if kind == "react_link":
            return ("react", 0)
        if kind in ("channel", "group", "group_rename"):
            return ("main", 0)
        if kind == "credit":
            return ("credit", topic_id)
        return ("cat", topic_id)

    def _notifier(self, chat_id: int):
        """Ручной заказ длится несколько минут: по окончанию Reactor пишет результат в этот чат."""

        async def notify(text: str) -> None:
            await call(self.client.send_message, chat_id, cut(text, TEXT_LIMIT))

        return notify

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
            return cut(NO_GROUP, TEXT_LIMIT), markup(
                [("🏛 Создать группу", "group"), ("🔄 Обновить список", "update")],
                [("❓ Как это работает", "help")],
            )

        stats = self.bot.stats
        lines = [
            "🤖 АвтоПост",
            "",
            "Бот читает посты в каналах, которые ты укажешь, и выкладывает их в твои",
            "каналы той же картинкой. Текст - словами автора или через нейросеть, как",
            "стоит в настройке канала. Потом добавляет просмотры и реакции.",
            "",
            f"Список каналов лежит в группе «{registry.group_name}»",
            f"Твоих каналов: {len(registry.categories)} · читаем чужих каналов: {registry.donor_count}",
            f"Состояние: {'⏸ пауза - посты не публикуются' if registry.paused else '▶ работаю, посты публикуются'}",
            f"Нейросеть: {self.ai_line()}",
            self.bot.reactions.describe(),
            (
                f"Поймано {stats['caught']} · опубликовано {stats['published']} · "
                f"в промо {stats['forwarded']} · сбоев {stats['errors']}"
            ),
        ]
        clients = getattr(self.bot, "clients", None)
        if clients:
            lines.append(f"Кабинеты клиентов: {clients.tenants_total}, с действующим тарифом {clients.tenants_active}")
        problems = registry.problems()
        if problems:
            lines.append("")
            lines.append("На что обратить внимание:")
            lines += ["- " + problem for problem in problems[:5]]

        toggle = ("▶ Пуск - возобновить публикацию", "start") if registry.paused else ("⏸ Пауза - не публиковать", "pause")
        money = [[("💰 Заявки на оплату", "money"), ("👥 Клиенты", "clients")]] if self.store else []
        return (
            cut(self._with_notice("\n".join(lines)), TEXT_LIMIT),
            markup(
                [("🗂 Мои каналы", "topics"), ("➕ Добавить канал", "channel")],
                [("🩺 Проверка: почему не идёт", "check"), ("ℹ️ Статус", "status")],
                [toggle],
                [("🔄 Обновить список", "update"), ("❓ Как это работает", "help")],
                [("🎯 Реакция на конкретный пост", "react")],
                *money,
                [("✏️ Переименовать группу", "group_rename")],
                [("📄 Соглашение", "doc|1"), ("🔒 Политика", "doc|2")],
            ),
        )

    def ai_line(self) -> str:
        """Одна честная строка про нейросеть: общая настройка, потом ключ, потом модель."""
        if not self.cfg.ai_enabled:
            return "выключена общей настройкой REWRITE=0 - посты выходят словами автора"
        if not self.cfg.minimax_api_key:
            return "ключа нет - посты выходят словами автора"
        mode = "размышления вслух выключены" if self.cfg.ai_thinking == "disabled" else "размышления вслух включены"
        return f"{self.cfg.ai_model}, {mode}"

    def _topics_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        categories = list(self.registry.categories.values())
        if not categories:
            text = (
                "Каналов, куда постить, пока нет.\n\n"
                "Нажми «➕ Добавить канал» и напиши название или ссылку своего канала, "
                "например «Спорт» или @sport_channel."
            )
        else:
            lines = [
                "🗂 Твои каналы",
                "",
                "Открой канал - в нём видно, откуда берём посты, и его настройки:",
                "переписывать ли текст, сколько просмотров, нужны ли реакции.",
                "",
            ]
            for index, category in enumerate(categories, start=1):
                name = category.target_name or category.name
                lines.append(f"{index}. {name} - читаем {len(category.donors)} канал(ов)")
            text = "\n".join(lines)

        rows = [
            [(f"{category.target_name or category.name} · {len(category.donors)} канал(ов)", f"cat|{category.topic_id}")]
            for category in categories[:20]
        ]
        return (
            cut(self._with_notice(text), TEXT_LIMIT),
            markup(*rows, [("➕ Добавить канал", "channel")], [("⬅️ Меню", "main")]),
        )

    def _category_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        if category is None:
            return (
                self._with_notice("Этого канала уже нет в списке."),
                markup([("🗂 Мои каналы", "topics")], [("⬅️ Меню", "main")]),
            )

        donors = list(category.donors.values())
        where = category.target_name or "не задан - нажми «📤 Куда постить»"
        views = self.registry.views_for(category)
        reactions = self.registry.reactions_for(category)
        rewrite = self.registry.rewrite_for(category)
        lines = [
            f"📂 Тема в группе: «{category.name}» - это только название для тебя",
            f"📤 Куда постим: {where}",
            "",
            "1. Откуда берём посты:",
        ]
        if donors:
            lines += [f"   • {donor.name}" for donor in donors[:10]]
            if len(donors) > 10:
                lines.append(f"   ...и ещё {len(donors) - 10}")
        else:
            lines.append("   пока ни одного - нажми «➕ Читать ещё канал» и пришли ссылку")
        if not rewrite:
            text_state = "публикуем словами автора, не переписывая"
        elif not self.cfg.ai_enabled:
            text_state = (
                "переписывание включено, но нейросеть выключена для всех (REWRITE=0 в Railway) - "
                "выходит текст автора"
            )
        else:
            text_state = "переписывает нейросеть (текст становится уникальным)"
        lines += [
            "",
            "2. Что делаем с текстом:",
            f"   {text_state}",
            "",
            "3. Просмотры:",
            f"   {f'{views} на каждый пост' if views else 'не накручиваем'}",
            "",
            "4. Реакции:",
            f"   {'ставим бесплатные' if reactions else 'не ставим'}",
        ]
        if category.problem:
            lines += ["", f"⚠ {category.problem}"]

        rows = [
            [(f"✖ {donor.name[:28]}", f"del|{topic_id}|{donor.message_id}") for donor in group]
            for group in chunks(donors[:MAX_DONOR_BUTTONS], 2)
        ]
        return (
            cut(self._with_notice("\n".join(lines)), TEXT_LIMIT),
            markup(
                [("➕ Читать ещё канал", f"add|{topic_id}")],
                *rows,
                [("📤 Куда постить", f"target|{topic_id}")],
                [
                    (
                        ("✏️ Текст: переписывает ИИ" if rewrite else "✏️ Текст: словами автора"),
                        f"cw|{topic_id}",
                    ),
                    ("👁 Просмотры на пост", f"cv|{topic_id}"),
                ],
                [
                    (
                        ("🎭 Реакции: ставим" if reactions else "🎭 Реакции: не ставим"),
                        f"cr|{topic_id}",
                    ),
                    ("✏️ Название темы", f"rename|{topic_id}"),
                ],
                [("🩺 Проверка: почему не идёт", "check"), ("🗂 Все каналы", "topics")],
                [("🗑 Убрать этот канал", f"drop|{topic_id}"), ("⬅️ Меню", "main")],
            ),
        )

    def _clients_note(self) -> str:
        """Клиентская часть в статусе: когда проверяли кабинеты и что не открывается.

        Без базы строки нет - владельцу не показываются несуществующие клиенты.
        """
        clients = getattr(self.bot, "clients", None)
        return f"\n{clients.report()}" if clients else ""

    def _status_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        stats = self.bot.stats
        stored = self.registry.views
        default_line = (
            f"Просмотры для новых каналов: {stored} на пост (у каждого канала своё - в «🗂 Мои каналы»)"
            if stored
            else "Просмотры для новых каналов: не заданы (переменная PROMO_VIEWS пустая)"
        )
        rewrite_line = (
            f"Текст для новых каналов: переписывает нейросеть ({self.ai_line()})"
            if self.cfg.ai_enabled
            else "Текст для новых каналов: словами автора - нейросеть выключена переменной REWRITE"
        )
        text = (
            self._with_notice(
                "ℹ️ Что сейчас настроено\n\n"
                f"{self.cfg.describe()}\n"
                f"Группа, где лежит список: {self.registry.group_name or 'не найдена'}\n"
                f"{default_line}\n"
                f"{rewrite_line}\n"
                f"Последняя публикация: {self.bot.last_publish or 'ещё не было'}\n"
                f"{self.bot.reactions.describe()}\n"
                f"Последний заказ реакций: {self.bot.reactions.last_link or 'ещё не уходил'}"
                f"{' - сервис ответил ' + self.bot.reactions.last_reply if self.bot.reactions.last_reply else ''}\n"
                f"{self.bot.reactions.report()}\n"
                f"Поймано постов: {stats['caught']} · опубликовано: {stats['published']} · "
                f"переслано в промо: {stats['forwarded']} · сбоев: {stats['errors']}{self._clients_note()}"
            )
        )
        return (
            cut(text, TEXT_LIMIT),
            markup(
                [("🩺 Проверка: почему не идёт", "check"), ("🎯 Реакция на конкретный пост", "react")],
                [("🔄 Обновить список", "update"), ("⬅️ Меню", "main")],
            ),
        )

    def _add_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        name = self._name(topic_id) if category else "?"
        return (
            cut(
                f"Канал «{name}»: добавляем канал, откуда берём посты.\n\n"
                "Пришли ссылку: @юзернейм или https://t.me/канал.\n"
                "Можно несколько ссылок в одном сообщении - добавятся все.\n\n"
                "Важно: аккаунт бота должен быть подписан на этот канал, иначе посты не дойдут. "
                "Проверить это можно кнопкой «🩺 Проверка».",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", f"cat|{topic_id}")]),
        )

    def _new_channel_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        return (
            cut(
                "Напиши название - бот создаст с ним тему в группе.\n\n"
                "Например: спортивные новости\n\n"
                "Новый канал в Telegram бот не создаёт: название - это только твоя вывеска. "
                "Канал, куда постить, укажешь кнопкой «📤 Куда постить» - впишешь @юзернейм "
                "канала, который уже есть. Потом добавишь каналы, откуда берём посты.\n\n"
                "Если канал с таким же названием уже открыт этому аккаунту, бот подставит его сам.",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", "main")]),
        )

    def _new_group_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        return (
            cut(
                "Напиши название новой группы (например «АвтоПост»).\n\n"
                "Бот создаст форум-группу с этим названием, ты станешь её владельцем. "
                "Внутри группы бот хранит список каналов и их настройки - это служебная группа, "
                "посты туда не публикуются. Больше ничего настраивать не нужно.",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", "main")]),
        )

    def _rename_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        label = category.name if category else self._name(topic_id)
        return (
            cut(
                f"Тема в группе: «{label}»\n\n"
                "Напиши новое название - оно станет только названием темы в группе.\n"
                "Канал, куда бот постит, от этого не изменится: для него есть кнопка «📤 Куда постить».\n\n"
                "Например: спортивные новости",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", f"cat|{topic_id}")]),
        )

    def _target_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        current = (category.target_name or "не задано") if category else "не задано"
        return (
            cut(
                self._with_notice(
                    f"Тема в группе: «{self._name(topic_id)}»\n"
                    f"Сейчас постим в: {current}\n\n"
                    "Напиши @юзернейм канала, куда бот должен постить, например @sport_ch.\n"
                    "Можно ссылкой https://t.me/sport_ch или id вида -1002088...\n\n"
                    "Название темы останется как есть - оно только для тебя. Бот новые каналы не создаёт: "
                    "выбери канал, который уже есть и открыт этому аккаунту."
                ),
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", f"cat|{topic_id}")]),
        )

    def _drop_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        donors = len(category.donors) if category else 0
        return (
            cut(
                f"Канал: «{self._name(topic_id)}»\n\nУбрать его целиком? Тема и {donors} канал(ов), "
                "откуда берём посты, исчезнут из списка, публикация в этот канал остановится. Отменить нельзя.\n\n"
                "Посты, уже опубликованные в канале, остаются.",
                TEXT_LIMIT,
            ),
            markup(
                [("✅ Да, убрать", f"drop_yes|{topic_id}")],
                [("⬅️ Отмена", f"cat|{topic_id}")],
            ),
        )

    def _rename_group_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        return (
            cut(
                f"Сейчас группа называется «{self.registry.group_name}». Это служебная группа: "
                "в ней бот хранит список каналов и их настройки, посты туда не публикуются.\n\n"
                "Напиши новое название группы.",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", "main")]),
        )

    def _category_views_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        current = self.registry.views_for(category) if category else self.cfg.promo_views
        shown = f"{current} на каждый пост" if current else "не накручиваем (0)"
        lines = [
            f"Канал «{self._name(topic_id)}»: просмотры на каждый пост.",
            f"Сейчас: {shown}.",
            "",
            f"Напиши одно число от 0 до {VIEWS_LIMIT} - столько просмотров бот закажет "
            "для каждого нового поста этого канала. Ноль - не накручивать. Или выбери кнопкой.",
            "",
            "Число сохраняется в тему канала и остаётся таким же после перезапуска.",
        ]
        presets = [(f"{count}", f"cv|{topic_id}|{count}") for count in (500, 1000, 2000, 5000)]
        return (
            cut(self._with_notice("\n".join(lines)), TEXT_LIMIT),
            markup(
                presets[:2],
                presets[2:],
                [("0 - не накручивать", f"cv|{topic_id}|0")],
                [("⬅️ Отмена", f"cat|{topic_id}")],
            ),
        )

    def _react_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        """Экран ручного заказа: как дать ссылку и что уже стоит в очереди."""
        reactor = self.bot.reactions
        lines = [
            "Поставить реакции на конкретный пост."
            if reactor.enabled
            else "Реакции выключены полностью (переменная REACTIONS в Railway) - заказ не уйдёт.",
            "",
            "Пришли ссылку на пост: https://t.me/канал/номер (или просто t.me/канал/номер).",
            "Сначала бот откроет этот пост своим аккаунтом: не увидит его - заказ не потратит.",
            "Ручной заказ идёт раньше очереди свежих постов и новым постом не вытесняется.",
            "Заказы идут раз в 5 минут; каждый пост ждёт отдельно, при отказе про лимит "
            "тот же пост попробуем ещё раз в следующем окне.",
            "",
            reactor.describe(),
            "Ждут заказа:",
            *reactor.queue_lines(),
        ]
        return (
            cut(self._with_notice("\n".join(lines)), TEXT_LIMIT),
            markup([("⬅️ Отмена", "main")]),
        )

    def _name(self, topic_id: int) -> str:
        category = self.registry.categories.get(topic_id)
        if not category:
            return "?"
        return category.target_name or category.name

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
