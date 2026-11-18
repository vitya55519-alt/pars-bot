"""Управление донорами кнопками в твоём боте.

Бот работает в одном процессе с юзерботом, поэтому нажимает действия прямо
через реестр: список по-прежнему живёт в группе с темами, но руками в ней
ничего писать не нужно. Служебных сообщений в чате не появляется.
"""

import logging
from typing import Optional, Tuple

from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import BotCommand, InlineKeyboardMarkup

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
    "Как пользоваться:\n"
    "0) «🏛 Создать группу» - напиши название (например «АвтоПост»). "
    "Бот создаст форум-группу, ты в ней владелец - там живёт весь список.\n"
    "1) «➕ Новый канал» - напиши название своего канала, куда постить. "
    "Тема с таким же названием создаётся сама.\n"
    "2) Открой канал и нажми «➕ Донор» - пришли @юзернейм или ссылку https://t.me/... "
    "Сколько ссылок в одном сообщении, столько доноров и добавится.\n"
    "3) Кнопка 🗑 под донором - убрать его из списка.\n"
    "4) «✏️ Переименовать» - если название темы должно отличаться от названия канала.\n"
    "5) «🗑 Убрать канал целиком» - удалить тему и всех её доноров (спросит подтверждение).\n"
    "6) «✏️ Переименовать склад» - сменить название группы, где живёт список.\n"
    "7) «🔄 Обновить список» - перечитать группу заново.\n"
    "8) «⏸ Пауза» - посты ловим, но не публикуем. «▶ Пуск» - снова работаем.\n"
    "9) «🎭 Реакции» - включаются и выключаются бесплатно накручивать реакции "
    "на свежие посты. Сервис даёт один заказ в час на всех, поэтому очередь держит "
    "по одному заказу на канал и всегда самый свежий его пост - старые посты тем же "
    "заказом не повторяются. Через 20 минут и через два часа после заказа бот сам "
    "пересчитывает реакции под постом - числа видны в «🩺 Проверка» и в «📊 Статус».\n"
    "10) «🎯 Реакция на пост» - заказать реакции вручную: после кнопки пришли ссылку "
    "https://t.me/канал/номер. Сначала бот сам откроет этот пост своим аккаунтом: "
    "не увидит - заказ не потратит. Увидит - поставит в общую очередь, но ручной заказ "
    "идёт раньше свежих постов, и новый пост канала его не вытеснит. Окно сервиса одно "
    "в час, поэтому часть времени уходит на ожидание. О результате бот напишет сюда сам, "
    "реакции пересчитает через 20 минут и через 2 часа.\n"
    "11) «👁 Просмотров» - напиши число от 1 до 10000: столько просмотров бот отправит "
    "промо-боту после каждой новой публикации. Сохраняется в склад, перезапуск не сбрасывает.\n"
    "12) «🩺 Проверка» - бот смотрит последние сообщения у доноров, пускает ли его в канал "
    "и сколько постов не дошло, а заодно считает реакции под свежими постами канала. "
    "«⤵️ Догнать пропущенное» публикует их тем же маршрутом.\n"
    "13) «💰 Заявки на оплату» - очередь клиентов, которые написали сумму, но ещё не "
    "получили деньги на баланс. Проверил поступление - жми «✔ Начислить»: сумма ложится "
    "один раз, повторная кнопка уже ничего не добавит. Не нашёл платёж - «✖ Не нашёл», "
    "тогда у клиента в истории заявка станет отклонённой, а баланс не изменится.\n"
    "14) «👥 Клиенты» - все кабинеты одной таблицей: тариф, баланс и когда у человека "
    "выходил последний пост. Если клиент перевёл мимо заявки, нажми «💵 Начислить» и "
    "напиши сумму - она попадёт ему на баланс, и тариф он купит сам.\n\n"
    "Ловим любой пост донора, в котором есть текст: с картинкой - картинкой, альбом - "
    "пачкой карточек, без картинки - текстом. Картинки идут по своим номерам, ничего "
    "не скачивается. У поста с видео или документом переносим только текст. Пост без "
    "текста не публикуем - перепечатывать нечего. Если донор - группа, а не канал, "
    "берём только посты самого чата: переписку в канал не пишем.\n"
    "Текстовый пост после рерайта в канале не опознать, поэтому «Догнать» берёт из них "
    "только то, что вышло уже после запуска бота; такие посты видны в «Проверке» отдельной "
    "строкой «текстовых до запуска не проверить».\n\n"
    "Список хранится в группе с темами (тема = канал, ссылки в теме = доноры), "
    "поэтому после перезапуска всё восстанавливается само."
)

NO_GROUP = (
    "Группы-хранилища пока нет - в ней живёт список доноров.\n\n"
    "Нажми «🏛 Создать группу» и напиши её название (например «АвтоПост»). "
    "Бот создаст форум-группу сам, ты в ней владелец - руками ничего настраивать не нужно.\n\n"
    "Если группа уже есть и ты в ней владелец - нажми «🔄 Обновить список»."
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
        elif key in ("add", "channel", "rename", "group", "group_rename"):
            self.awaiting = {
                "add": ("donor", arg),
                "channel": ("channel", 0),
                "rename": ("rename", arg),
                "group": ("group", 0),
                "group_rename": ("group_rename", 0),
            }[key]
            await self._show(query, key, arg)
        elif key == "views":
            if arg:
                await self._apply_views(query, str(arg))
            else:
                self.awaiting = ("views", 0)
                await self._show(query, "views")
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
        elif key == "reactions":
            reactor = self.bot.reactions
            reactor.enabled = not reactor.enabled
            self.notice = (
                "Реакции включил: каждый новый пост ставлю в очередь на бесплатный заказ."
                if reactor.enabled
                else "Реакции выключил - заказы не отправляю, посты публикуются как раньше."
            )
            await self._show(query, "main", flash=self.notice[:TEXT_LIMIT])
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
        self.notice = f"Убрал донора: {names}."
        await self._show(query, "cat", topic_id, flash=self.notice)

    async def _remove_category(self, query, topic_id: int) -> None:
        category = await self.registry.remove_category(topic_id)
        self.notice = f"✅ Убрал канал «{category.name}» и его тему - его доноры больше не ловятся."
        await self._show(query, "topics", flash=self.notice)

    async def _apply_views(self, query, raw: str) -> None:
        try:
            number = await self.registry.save_views(raw)
        except ValueError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "views")
            return
        except RegistryError as exc:
            self.notice = f"⚠ {exc}"
            await self._show(query, "main")
            return
        self.notice = f"✅ Просмотры: {number}. Такое число бот отправит промо-боту после каждой публикации."
        await self._show(query, "main", flash=str(number)[:TEXT_LIMIT])

    async def _rebuild(self, query) -> None:
        await call(query.answer, "Перечитываю группу...")
        await self._edit(query, "⏳ Перечитываю темы группы и ссылки в них...")
        await self.registry.rebuild()
        problems = self.registry.problems()
        self.notice = "Список обновлён." + ("\n⚠ " + "; ".join(problems[:5]) if problems else "")
        await self._show(query, "main", flash="Готово")

    async def _diagnose(self, query) -> None:
        """Отвечает на «почему не публикует» цифрами: что у доноров и пускает ли канал."""
        await call(query.answer, "Проверяю доноров и каналы...")
        await self._edit(query, "⏳ Смотрю последние сообщения доноров и права в каналах...")
        report = await self.bot.diagnose()
        await self._edit(query, cut(f"🩺 Проверка\n{report}", TEXT_LIMIT), self._check_buttons())

    def _check_buttons(self) -> InlineKeyboardMarkup:
        return markup(
            [("⤵️ Догнать пропущенное", "catchup"), ("🩺 Ещё раз", "check")],
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
                self.notice = "✅ Добавил доноров: " + ", ".join(donor.name for donor in added)
                screen = ("cat", topic_id)
            elif kind == "react_link":
                self.notice = await self.bot.reactions.request(cut(text, LINK_LIMIT), self._notifier(message.chat.id))
                screen = ("react", 0)
            elif kind == "channel":
                category = await registry.add_category(text[:NAME_LIMIT])
                self.notice = self._category_notice(category, f"Канал «{category.name}» добавлен")
                screen = ("cat", category.topic_id)
            elif kind == "group":
                name = await registry.create_storage_group(text[:NAME_LIMIT])
                self.notice = f"✅ Создал группу-хранилище «{name}». Её владелец - {self.account}. Жми «➕ Новый канал»."
                screen = ("main", 0)
            elif kind == "group_rename":
                name = await registry.rename_storage_group(text)
                self.notice = f"✅ Склад теперь называется «{name}». Доноры и темы не изменились."
                screen = ("main", 0)
            elif kind == "views":
                number = await registry.save_views(text)
                self.notice = f"✅ Просмотры: {number}. Такое число бот отправит промо-боту после каждой публикации."
                screen = ("main", 0)
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
                self.notice = self._category_notice(category, f"Переименовал в «{category.name}»")
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
        if kind == "views":
            return ("views", 0)
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
        if kind == "drop":
            return self._drop_view(topic_id)
        if kind == "group_rename":
            return self._rename_group_view()
        if kind == "views":
            return self._views_view()
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
            f"Группа: «{registry.group_name}»",
            f"Каналов: {len(registry.categories)} · доноров: {registry.donor_count}",
            f"Состояние: {'⏸ пауза - не публикую' if registry.paused else '▶ работаю'}",
            self.bot.reactions.describe(),
            (
                f"Поймано {stats['caught']} · опубликовано {stats['published']} · "
                f"в промо {stats['forwarded']} · сбоев {stats['errors']}"
            ),
        ]
        problems = registry.problems()
        if problems:
            lines.append("Замечания:\n- " + "\n- ".join(problems[:5]))

        toggle = ("▶ Пуск", "start") if registry.paused else ("⏸ Пауза", "pause")
        clients = getattr(self.bot, "clients", None)
        if clients:
            lines.append(f"Кабинеты: {clients.tenants_total}, с действующим тарифом {clients.tenants_active}")
        reactions_toggle = (
            ("🎭 Реакции: выключить", "reactions")
            if self.bot.reactions.enabled
            else ("🎭 Реакции: включить", "reactions")
        )
        views = f"{self.cfg.promo_views}" if self.cfg.promo_views else "не задано"
        money = [[("💰 Заявки на оплату", "money"), ("👥 Клиенты", "clients")]] if self.store else []
        return (
            cut(self._with_notice("🤖 АвтоПост\n" + "\n".join(lines)), TEXT_LIMIT),
            markup(
                [("📂 Каналы и доноры", "topics"), ("➕ Новый канал", "channel")],
                [("🔄 Обновить список", "update"), ("ℹ️ Статус", "status")],
                [toggle, ("❓ Как это работает", "help")],
                [reactions_toggle, ("🎯 Реакция на пост", "react")],
                [(f"👁 Просмотров: {views}", "views"), ("🩺 Проверка", "check")],
                *money,
                [("✏️ Переименовать склад", "group_rename")],
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
                [("🗑 Убрать канал целиком", f"drop|{topic_id}")],
                [("📂 Все каналы", "topics"), ("⬅️ Меню", "main")],
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
        text = (
            self._with_notice(
                f"{self.cfg.describe()}\n"
                f"Группа: {self.registry.group_name or 'не найдена'}\n"
                f"Просмотры в складе: {stored if stored else 'не записаны - беру из переменной PROMO_VIEWS'}\n"
                f"Последняя публикация: {self.bot.last_publish or 'ещё не было'}\n"
                f"{self.bot.reactions.describe()}\n"
                f"Последний заказ реакций: {self.bot.reactions.last_link or 'ещё не уходил'}"
                f"{' - сервис ответил ' + self.bot.reactions.last_reply if self.bot.reactions.last_reply else ''}\n"
                f"{self.bot.reactions.report()}\n"
                f"Поймано: {stats['caught']} · опубликовано: {stats['published']} · "
                f"в промо: {stats['forwarded']} · сбоев: {stats['errors']}{self._clients_note()}"
            )
        )
        return (
            cut(text, TEXT_LIMIT),
            markup(
                [("👁 Просмотров", "views"), ("🎯 Реакция на пост", "react")],
                [("🩺 Проверка", "check"), ("🔄 Обновить список", "update")],
                [("⬅️ Меню", "main")],
            ),
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

    def _new_group_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        return (
            cut(
                "Напиши название новой группы-хранилища (например «АвтоПост»).\n\n"
                "Бот создаст форум-группу с этим названием, ты станешь её владельцем, "
                "а список доноров будет жить внутри неё. Больше ничего настраивать не нужно.",
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

    def _drop_view(self, topic_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        category = self.registry.categories.get(topic_id)
        donors = len(category.donors) if category else 0
        return (
            cut(
                f"Канал: «{self._name(topic_id)}»\n\nУбрать его целиком? Тема и {donors} донор(ов) "
                "исчезнут из списка, посты оттуда ловиться не будут. Отменить нельзя.\n\n"
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
                f"Сейчас склад называется «{self.registry.group_name}». Внутри него живут темы с донорами - "
                "это не канал, поэтому название лучше дать служебное, например «АвтоПост».\n\n"
                "Напиши новое название склада.",
                TEXT_LIMIT,
            ),
            markup([("⬅️ Отмена", "main")]),
        )

    def _views_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        current = self.cfg.promo_views
        lines = [
            f"Сейчас: {current if current else 'не задано'} просмотров на каждый пост.",
            "",
            f"Напиши одно число от 1 до {VIEWS_LIMIT} - столько просмотров бот будет отправлять промо-боту "
            "сразу после публикации поста. Или выбери готовое кнопкой.",
            "",
            "Число сохраняется в склад, поэтому остаётся таким же после перезапуска.",
        ]
        presets = [(f"{count}", f"views|{count}") for count in (500, 1000, 2000, 5000)]
        return (
            cut(self._with_notice("\n".join(lines)), TEXT_LIMIT),
            markup(presets[:2], presets[2:], [("⬅️ Отмена", "main")]),
        )

    def _react_view(self) -> Tuple[str, InlineKeyboardMarkup]:
        """Экран ручного заказа: как дать ссылку и что уже стоит в очереди."""
        reactor = self.bot.reactions
        lines = [
            "Ручной заказ реакций на конкретный пост."
            if reactor.enabled
            else "Реакции сейчас выключены - включи их, иначе заказ не уйдёт.",
            "",
            "Пришли ссылку на пост: https://t.me/канал/номер (или просто t.me/канал/номер).",
            "Сначала бот откроет этот пост своим аккаунтом: не увидит его - заказ не потратит.",
            "Ручной заказ идёт раньше очереди свежих постов и новым постом не вытесняется.",
            "Окно сервиса одно в час, поэтому ждать можно дольше самого заказа.",
            "",
            reactor.describe(),
            "Ждут заказа:",
            *reactor.queue_lines(),
        ]
        rows = [] if reactor.enabled else [("🎭 Реакции: включить", "reactions")]
        return (
            cut(self._with_notice("\n".join(lines)), TEXT_LIMIT),
            markup(*rows, [("⬅️ Отмена", "main")]),
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
