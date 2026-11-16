"""Личный кабинет клиента в том же боте.

Владелец аккаунта видит панель склада, все остальные - кабинет: свой канал,
свои доноры, просмотры, баланс. Всё живёт в базе, поэтому между перезапусками
бот ничего не должен помнить сам.
"""

import datetime
import logging
from typing import Dict, Tuple

from ai import cut
from config import VIEWS_LIMIT, parse_views
from store import MAX_DONORS, MAX_POSTS_PER_DAY, MIN_TOPUP, PLANS, StoreError, Tenant, parse_ref
from utils import TEXT_LIMIT, call, markup

log = logging.getLogger("cabinet")

AGREEMENT_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-08-01-39"
PRIVACY_URL = "https://telegra.ph/Politika-konfidencialnosti-08-01-83"

VIEWS_PRESETS = (500, 1000, 2000, 5000)

# куда возвращаем человека, если он ошибся вводом
SCREENS = {"channel": "channel", "donor": "donors", "views": "views", "sum": "plans"}


def date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "?"


def plan_line(tenant: Tenant, now: datetime.datetime) -> str:
    if not tenant.active(now):
        return "⛔ не подключён - посты не публикуются"
    return f"⏳ активен ещё {tenant.days_left(now)} дн. (до {date(tenant.plan_until)})"


def channel_line(tenant: Tenant) -> str:
    if not tenant.target:
        return "➕ не указан"
    return f"{tenant.target_title or tenant.target} - {'✅ доступ подтверждён' if tenant.target_ok else '⏳ ждёт проверки'}"


class Cabinet:
    SHOW = {"chome": "home", "cdocs": "docs", "cplans": "plans", "cdonors": "donors"}

    def __init__(self, client, cfg, store):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.awaiting: Dict[int, Tuple[str, int]] = {}
        self.notice: Dict[int, str] = {}

    # ==== вход ====

    async def on_message(self, message) -> None:
        user_id = message.chat.id
        text = (message.text or "").strip()
        kind, _ = self.awaiting.pop(user_id, ("", 0))
        if kind and text and not text.startswith("/"):
            await self._apply_text(message, kind, text)
            return

        body, buttons = await self._view(user_id, "home")
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

    # ==== разбор кнопок ====

    async def _route(self, query) -> None:
        user_id = query.from_user.id
        parts = (query.data or "").split("|")
        key = parts[0]
        arg = parts[1] if len(parts) > 1 else ""

        if key in self.SHOW:
            await self._show(query, self.SHOW[key])
        elif key == "cchannel":
            self.awaiting[user_id] = ("channel", 0)
            await self._show(query, "channel")
        elif key == "cadd":
            self.awaiting[user_id] = ("donor", 0)
            await self._show(query, "add")
        elif key == "cviews":
            if arg:
                await self._apply(user_id, "views", arg)
            else:
                self.awaiting[user_id] = ("views", 0)
            await self._show(query, "views")
        elif key == "ctopup":
            tenant = await self.store.ensure_tenant(user_id)
            if not tenant.agreed:
                await self._show(query, "agree")
                return
            self.awaiting[user_id] = ("sum", 0)
            await self._show(query, "sum")
        elif key == "cdel":
            await self._remove_donor(query, int(arg or 0))
        elif key == "creactions":
            await self._toggle_reactions(query)
        elif key == "cbuy":
            await self._buy(query, arg)
        elif key == "cagree":
            await self.store.set_agreed(user_id)
            self._note(user_id, "✅ Спасибо, условия приняты.")
            await self._show(query, "plans")
        else:
            await call(query.answer, "Такой кнопки нет - нажми «🏠 Кабинет».")

    async def _remove_donor(self, query, position: int) -> None:
        donors = await self.store.list_donors(query.from_user.id)
        if not 1 <= position <= len(donors):
            self._note(query.from_user.id, "⚠ Этот донор уже убран.")
        else:
            await self.store.remove_donor(query.from_user.id, donors[position - 1].ref)
            self._note(query.from_user.id, f"✅ Убрал {donors[position - 1].ref}.")
        await self._show(query, "donors")

    async def _toggle_reactions(self, query) -> None:
        tenant = await self.store.ensure_tenant(query.from_user.id)
        await self.store.set_reactions(tenant.id, not tenant.reactions)
        self._note(tenant.id, "🎭 Реакции включены." if not tenant.reactions else "🎭 Реакции выключены.")
        await self._show(query, "home")

    async def _buy(self, query, kind: str) -> None:
        if kind not in PLANS:
            await call(query.answer, "Такого тарифа нет.", show_alert=True)
            return
        tenant = await self.store.ensure_tenant(query.from_user.id)
        if not tenant.agreed:
            await self._show(query, "agree")
            return
        if not tenant.target:
            self._note(tenant.id, "⚠ Сначала укажи канал, куда постить.")
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
            ref = parse_ref(text)
            await self.store.set_target(user_id, str(ref), ref if isinstance(ref, int) else None, "")
            self._note(user_id, f"✅ Записал канал {ref}. Появится галочка, когда рабочий аккаунт получит доступ.")
        elif kind == "donor":
            donor = await self.store.add_donor(user_id, parse_ref(text))
            self._note(user_id, f"✅ Донор {donor.ref} в списке - рабочий аккаунт подпишется сам.")
        elif kind == "views":
            number = parse_views(text)
            await self.store.set_views(user_id, number)
            self._note(user_id, f"✅ Просмотры: {number} на каждый пост.")
        elif kind == "sum":
            request_id = await self.store.request_topup(user_id, self._rubles(text))
            self._note(user_id, f"✅ Заявка №{request_id} отправлена. Начислю баланс, как только увижу платёж.")

    @staticmethod
    def _rubles(text: str) -> int:
        digits = "".join(char for char in text if char.isdigit())
        if not digits:
            raise ValueError(f"Напиши сумму цифрами, например 600 - от {MIN_TOPUP} ₽.")
        return int(digits)

    # ==== экраны ====

    async def _view(self, user_id: int, kind: str) -> Tuple[str, object]:
        tenant = await self.store.ensure_tenant(user_id)
        now = datetime.datetime.now(datetime.timezone.utc)

        if kind == "channel":
            return self._notice(
                user_id,
                "Куда постить.\n\n"
                f"Сейчас: {channel_line(tenant)}\n\n"
                "1) Добавь в администраторы своего канала рабочий аккаунт: "
                f"{self.cfg.worker_contact or 'пришлю его в этом чате'}.\n"
                "   Права: публиковать сообщения и оставлять реакции.\n"
                "2) Напиши сюда ссылку на канал: @name или https://t.me/name.\n\n"
                "Аккаунт проверит доступ, и посты пойдут сами.",
            ), markup([("⬅️ Кабинет", "chome")])

        if kind == "add":
            return self._notice(
                user_id,
                "Пришли ссылку на канал-донор: @name или https://t.me/name.\n\n"
                f"Подписываемся сами, максимум {MAX_DONORS} доноров на кабинет.",
            ), markup([("⬅️ Доноры", "cdonors")])

        if kind == "donors":
            donors = await self.store.list_donors(user_id)
            lines = [f"Доноры ({len(donors)} из {MAX_DONORS}). Нажми 🗑, чтобы убрать:"]
            if donors:
                for donor in donors:
                    status = f"✅ {donor.title or 'подписаны'}" if donor.chat_id else "⏳ ждёт подписки"
                    lines.append(f"• {donor.ref} - {status}")
            else:
                lines.append("Пока никого. Нажми «➕ Добавить донора» и пришли ссылку на канал.")
            rows = [[(f"🗑 {donor.ref[:24]}", f"cdel|{index}")] for index, donor in enumerate(donors, 1)]
            return self._notice(user_id, "\n".join(lines)), markup(
                *rows, [("➕ Добавить донора", "cadd")], [("⬅️ Кабинет", "chome")]
            )

        if kind == "views":
            body = self._notice(
                user_id,
                f"Сейчас: {tenant.views if tenant.views else 'не задано'} просмотров на каждый пост.\n\n"
                f"Напиши одно число от 1 до {VIEWS_LIMIT} или выбери готовое кнопкой.\n\n"
                "Число остаётся в кабинете после перезапуска.",
            )
            presets = [[(str(count), f"cviews|{count}") for count in VIEWS_PRESETS[:2]],
                       [(str(count), f"cviews|{count}") for count in VIEWS_PRESETS[2:]]]
            return body, markup(*presets, [("⬅️ Кабинет", "chome")])

        if kind == "plans":
            return self._notice(
                user_id,
                "Сколько стоит\n\n"
                f"Неделя - {PLANS['week']['price']} ₽ ({PLANS['week']['days']} дней)\n"
                f"Месяц - {PLANS['month']['price']} ₽ ({PLANS['month']['days']} дней) - выходит дешевле\n\n"
                "Один кабинет: свой канал, до 10 доноров, до 50 постов в сутки, просмотры до "
                f"{VIEWS_LIMIT} на пост и бесплатные реакции.\n\n"
                f"На балансе: {tenant.balance} ₽\n"
                f"Тариф: {plan_line(tenant, now)}\n\n"
                "Покупка списывает деньги с баланса. Не хватает - сначала «Пополнить баланс»: я проверю "
                "платёж и начислю сумму. Дни текущей подписки не сгорают, новая покупка добавляется к остатку.",
            ), markup(
                [(f"Неделя {PLANS['week']['price']} ₽", "cbuy|week"), (f"Месяц {PLANS['month']['price']} ₽", "cbuy|month")],
                [("💳 Пополнить баланс", "ctopup")],
                [("📄 Условия", "cdocs"), ("⬅️ Кабинет", "chome")],
            )

        if kind == "sum":
            return self._notice(
                user_id,
                f"Сколько перевёл - напиши сумму цифрами (от {MIN_TOPUP} ₽).\n\n"
                f"{self.cfg.payment_details or 'Реквизиты пришлю в этом чате.'}\n\n"
                "Начислю на баланс, как только проверю платёж. Тогда и купишь тариф.",
            ), markup([("⬅️ Кабинет", "chome")])

        if kind == "docs":
            return self._notice(
                user_id,
                "Документы сервиса:\n\n"
                f"Пользовательское соглашение: {AGREEMENT_URL}\n"
                f"Политика конфиденциальности: {PRIVACY_URL}\n\n"
                "Оплачивая тариф, ты соглашаешься с обоими документами.",
            ), markup([("⬅️ Кабинет", "chome")])

        if kind == "agree":
            return (
                "Перед оплатой - документы сервиса.\n\n"
                f"Пользовательское соглашение: {AGREEMENT_URL}\n"
                f"Политика конфиденциальности: {PRIVACY_URL}",
                markup([("✅ Принимаю условия", "cagree")], [("⬅️ Кабинет", "chome")]),
            )

        donors = await self.store.list_donors(user_id)
        bound = sum(1 for donor in donors if donor.chat_id)
        lines = [
            "👤 Твой кабинет",
            "",
            f"Тариф: {plan_line(tenant, now)}",
            f"Канал: {channel_line(tenant)}",
            f"Доноры: {len(donors)} из {MAX_DONORS} (подписаны: {bound})",
            f"Просмотры: {tenant.views if tenant.views else 'не заданы'} на пост",
            f"Реакции: {'включены' if tenant.reactions else 'выключены'}",
            f"Сегодня: {await self.store.posts_today(user_id)} из {MAX_POSTS_PER_DAY} постов",
        ]
        payments = await self.store.list_payments(user_id, 3)
        if payments:
            lines.append("")
            lines.append("Последнее по оплате:")
            lines += [f"• {self._payment_line(row)}" for row in payments]
        if not tenant.target_ok or not donors:
            lines += [
                "",
                "Чтобы пост пошёл: укажи канал, добавь в администраторы рабочий аккаунт "
                f"({self.cfg.worker_contact or 'пришлю в этом чате'}), добавь доноров и купи тариф.",
            ]
        return self._notice(user_id, "\n".join(lines)), markup(
            [("📢 Мой канал", "cchannel"), (f"📥 Доноры ({len(donors)})", "cdonors")],
            [("👁 Просмотры", "cviews"), self._reactions_cell(tenant)],
            [("💳 Тариф и оплата", "cplans"), ("📄 Условия", "cdocs")],
        )

    def _payment_line(self, row: dict) -> str:
        title = PLANS.get(row.get("kind"), {}).get("title", "Пополнение")
        status = "" if row.get("status") == "done" else " (ждёт проверки)"
        return f"{title} {row.get('amount_rub')} ₽ {date(row.get('created_at'))}{status}".strip()

    def _reactions_cell(self, tenant: Tenant) -> Tuple[str, str]:
        return ("🎭 Реакции: выключить" if tenant.reactions else "🎭 Реакции: включить", "creactions")

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
        return {"cdel": "donors", "cadd": "donors", "cviews": "views", "cbuy": "plans", "ctopup": "plans", "cchannel": "channel"}.get(
            key, "home"
        )

    def _note(self, user_id: int, text: str) -> None:
        self.notice[user_id] = text

    def _notice(self, user_id: int, text: str) -> str:
        notice = self.notice.pop(user_id, "")
        return f"{notice}\n\n{text}" if notice else text
