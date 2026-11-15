"""Команды управления в форум-группе + автоматическое подхватывание ссылок."""

import logging
import re
from typing import Optional

from pyrogram import filters
from pyrogram.handlers import DeletedMessagesHandler, MessageHandler

from ai import cut
from registry import RegistryError, extract_refs
from utils import call

log = logging.getLogger("control")

COMMANDS = {
    "список": "show",
    "list": "show",
    "обнови": "rebuild",
    "update": "rebuild",
    "статус": "status",
    "status": "status",
    "пауза": "pause",
    "pause": "pause",
    "старт": "resume",
    "start": "resume",
    "resume": "resume",
    "убери": "remove",
    "remove": "remove",
    "помощь": "help",
    "help": "help",
}

HELP = (
    "Основное управление - кнопками в твоём боте (@bot_post_sam_bot): там добавляются каналы и доноры.\n"
    "Эти команды - запасной способ, если бот недоступен:\n"
    "1) /список - что сейчас в списке, /статус - как работает.\n"
    "2) /обнови - перечитать все темы заново.\n"
    "3) /убери ответом на сообщение со ссылкой - вычеркнуть донора.\n"
    "4) /пауза - ничего не публиковать, /старт - возобновить.\n"
    "Ссылки в темах = доноры этой темы; название темы = канал, куда постить."
)


class Controls:
    def __init__(self, client, cfg, registry, bot):
        self.client = client
        self.cfg = cfg
        self.registry = registry
        self.bot = bot
        self._rebuilding = False

    def setup(self) -> None:
        group = self.registry.group_id
        self.client.add_handler(
            MessageHandler(self.on_group_message, filters.chat(group) & ~filters.outgoing),
        )
        self.client.add_handler(DeletedMessagesHandler(self.on_deleted))

    async def on_group_message(self, client, message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        if text.startswith("/"):
            await self._run(message, text)
            return

        if not extract_refs(text) or not await self.registry.may_manage(message):
            return

        for donor in await self.registry.note(message):
            log.info("Донор добавлён: %s (тема «%s»).", donor.name, self._topic_name(message))

    def _topic_name(self, message) -> str:
        category = self.registry.categories.get(int(getattr(message, "message_thread_id", 0) or 0))
        return category.name if category else "?"

    async def on_deleted(self, client, messages) -> None:
        group = self.registry.group_id
        ids = {message.id for message in messages if message.chat.id == group}
        if not group or self._rebuilding or not ids:
            return
        if {message_id for message_id in ids if self.registry.took_out(message_id)} == ids:
            return
        self._rebuilding = True
        try:
            log.info("В группе удалили сообщение - перечитываю список доноров.")
            await self.registry.rebuild()
        except Exception as exc:  # noqa: BLE001 - удаление не должно ронять бота
            log.error("Перечитать список не удалось: %s: %s", type(exc).__name__, exc)
        finally:
            self._rebuilding = False

    # ==== разбор команды ====

    async def _run(self, message, text: str) -> None:
        word = re.split(r"[\s@:]", text.lstrip("/"), maxsplit=1)[0].lower()
        action = COMMANDS.get(word)
        if not action:
            return

        if not await self.registry.may_manage(message):
            await self._reply(message, "Команды принимает только владелец группы или админ.")
            return

        try:
            await getattr(self, f"_cmd_{action}")(message)
        except RegistryError as exc:
            await self._reply(message, f"Не вышло: {exc}")
        except Exception as exc:  # noqa: BLE001 - команду всегда нужно завершить ответом
            log.error("Команда /%s упала: %s: %s", word, type(exc).__name__, exc)
            await self._reply(message, f"Ошибка: {type(exc).__name__}")

    async def _cmd_show(self, message) -> None:
        state = "на паузе" if self.registry.paused else "работает"
        await self._reply(message, f"{self.registry.summary()}\n\nСостояние: {state}")

    async def _cmd_rebuild(self, message) -> None:
        await self._reply(message, "Перечитываю темы группы...")
        summary = await self.registry.rebuild()
        problems = self.registry.problems()
        note = "\n\nЗамечания:\n- " + "\n- ".join(problems) if problems else ""
        await self._reply(message, f"{summary}{note}")

    async def _cmd_status(self, message) -> None:
        stats = self.bot.stats
        await self._reply(
            message,
            "\n".join(
                [
                    f"Группа управления: {self.registry.group_name}",
                    f"Тем: {len(self.registry.categories)}, доноров: {self.registry.donor_count}",
                    f"Состояние: {'пауза' if self.registry.paused else 'работает'}",
                    (
                        f"Поймано постов: {stats['caught']}, опубликовано: {stats['published']}, "
                        f"переслано в промо: {stats['forwarded']}, сбоев: {stats['errors']}"
                    ),
                    f"Нейросеть: {self.cfg.ai_model} ({'ключ задан' if self.cfg.minimax_api_key else 'ключа нет - публикуем оригинал'})",
                    f"Промо-бот: {self.cfg.promo_bot or 'не задан'}, просмотров добавляю: {self.cfg.promo_views or 'не задано'}",
                    f"Последняя публикация: {self.bot.last_publish or 'ещё не было'}",
                ]
            ),
        )

    async def _cmd_pause(self, message) -> None:
        self.registry.paused = True
        await self._reply(message, "Пауза: посты ловлю, но не публикую. /старт - возобновить.")

    async def _cmd_resume(self, message) -> None:
        self.registry.paused = False
        await self._reply(message, "Работаю дальше.")

    async def _cmd_remove(self, message) -> None:
        origin: Optional[object] = message.reply_to_message
        if origin is None:
            await self._reply(message, "Ответь /убери на сообщение со ссылкой донора.")
            return

        removed = await self.registry.forget(origin)
        if not removed:
            await self._reply(message, "В этом сообщении нет донора из списка.")
            return

        try:
            await call(origin.delete)
        except Exception as exc:  # noqa: BLE001 - права на удаление могут отсутствовать
            await self._reply(
                message,
                f"Вычеркнул: {', '.join(d.name for d in removed)}. "
                f"Сообщение удалить не смог ({type(exc).__name__}) - удали сам и отправь /обнови.",
            )
            return

        await self._reply(message, f"Убрал: {', '.join(d.name for d in removed)}")

    async def _cmd_help(self, message) -> None:
        await self._reply(message, HELP)

    async def _reply(self, message, text: str) -> None:
        try:
            await call(message.reply, cut(text, 4000))
        except Exception as exc:  # noqa: BLE001
            log.error("Ответ в группу не ушёл: %s: %s", type(exc).__name__, exc)
