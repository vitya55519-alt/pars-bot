import asyncio
import logging
from typing import List, Optional, Tuple

from utils import call, norm_title

log = logging.getLogger("promo")


class Promoter:
    """Сценарий промо-бота: нажать «Просмотры» -> переслать пост -> отправить число просмотров."""

    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    async def push(self, message, views: Optional[int] = None) -> bool:
        """Продвинуть пост: `views` - число этого клиента, без него - сколько стоит в PROMO_VIEWS."""
        if not self.cfg.promo_enabled:
            return False

        await self._press_button()

        try:
            await call(self.client.forward_messages, self.cfg.promo_bot, message.chat.id, message.id)
            log.info("Переслано в промо-бота %s.", self.cfg.promo_bot)
        except Exception as exc:  # noqa: BLE001 - продвижение не должно ронять публикацию
            log.error("Не удалось переслать пост промо-боту: %s: %s", type(exc).__name__, exc)
            return False

        await self._send_views(self.cfg.promo_views if views is None else views)
        return True

    async def _dialog(self, limit: int = 12) -> List:
        messages: List = []
        async for item in self.client.get_chat_history(self.cfg.promo_bot, limit=limit):
            messages.append(item)
        return messages

    async def _press_button(self) -> None:
        if not self.cfg.promo_button:
            return

        await asyncio.sleep(self.cfg.promo_delay)
        try:
            dialog = await self._dialog()
        except Exception as exc:  # noqa: BLE001 - без диалога просто перешлём пост
            log.error("Диалог с промо-ботом не прочитан: %s: %s", type(exc).__name__, exc)
            return

        for message in dialog:
            found = self._find_button(message)
            if found is None:
                continue
            index, button = found
            try:
                await self._press(message, index, button)
                log.info("Промо-бот: нажата кнопка «%s».", getattr(button, "text", ""))
            except Exception as exc:  # noqa: BLE001 - кнопка могла устареть, дальше пойдёт пересылка
                log.warning("Промо-бот: кнопка «%s» не нажалась (%s).", getattr(button, "text", ""), type(exc).__name__)
            return

        log.warning("Промо-бот: кнопки «%s» среди последних сообщений нет - перешлю пост сразу.", self.cfg.promo_button)

    async def _press(self, message, index: int, button) -> None:
        """Kurigram у обычной кнопки отправляет в чат объект кнопки - в диалоге появляется JSON. Надпись шлём сами."""
        if getattr(button, "callback_data", None):
            await message.click(index, timeout=self.cfg.promo_delay)
            return
        label = getattr(button, "text", "") or ""
        if not isinstance(label, str) or not label.strip():
            raise ValueError("у кнопки нет надписи")
        await call(self.client.send_message, self.cfg.promo_bot, label)

    def _find_button(self, message) -> Optional[Tuple[int, object]]:
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
        if not rows:
            return None

        flat = [button for row in rows for button in row]
        needle = norm_title(self.cfg.promo_button)
        labels = [norm_title(getattr(button, "text", "") or "") for button in flat]
        for index, label in enumerate(labels):
            if label == needle:
                return index, flat[index]
        for index, label in enumerate(labels):
            if needle and needle in label:
                return index, flat[index]
        return None

    async def _send_views(self, views: int) -> None:
        """Число уходит тем же способом, что и всегда: сообщением промо-боту, затем ответ в журнал."""
        if not views:
            log.warning("Промо-бот: просмотров 0 - число не отправлял, пост идёт без накрутки.")
            return

        await asyncio.sleep(self.cfg.promo_delay)
        try:
            await call(self.client.send_message, self.cfg.promo_bot, str(views))
            log.info("Промо-боту отправлено количество просмотров: %s.", views)
        except Exception as exc:  # noqa: BLE001
            log.error("Количество просмотров не отправилось: %s: %s", type(exc).__name__, exc)
            return

        await self._report_answer()

    async def _report_answer(self) -> None:
        """В журнал - что ответил бот: «Готово!» или отказ, чтобы видеть, пошли ли просмотры."""
        await asyncio.sleep(self.cfg.promo_delay)
        try:
            dialog = await self._dialog(limit=3)
        except Exception as exc:  # noqa: BLE001 - ответ только для журнала
            log.warning("Ответ промо-бота не прочитан: %s", type(exc).__name__)
            return

        for message in dialog:
            if getattr(message, "outgoing", False):
                continue
            text = (message.text or message.caption or "").strip()
            if text:
                log.info("Ответ промо-бота: %s", " ".join(text.split())[:160])
                return
