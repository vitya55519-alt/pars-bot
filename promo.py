import asyncio
import logging

from utils import call

log = logging.getLogger("promo")


class Promoter:
    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg

    async def push(self, message) -> None:
        if not self.cfg.promo_enabled:
            return

        try:
            await call(self.client.forward_messages, self.cfg.promo_bot, message.chat.id, message.id)
            log.info("Переслано в промо-бота %s.", self.cfg.promo_bot)
        except Exception as exc:  # noqa: BLE001 - продвижение не должно ронять публикацию
            log.error("Не удалось переслать в промо-бота: %s: %s", type(exc).__name__, exc)
            return

        if self.cfg.promo_mode == "command":
            await self._send_command()
        elif self.cfg.promo_mode == "button":
            await self._click_button()

    async def _send_command(self) -> None:
        if not self.cfg.promo_command:
            log.warning("PROMO_MODE=command, но PROMO_COMMAND пуст -> команду не отправлял.")
            return
        await asyncio.sleep(self.cfg.promo_delay)
        try:
            await call(self.client.send_message, self.cfg.promo_bot, self.cfg.promo_command)
            log.info("Промо-боту отправлена команда: %s", self.cfg.promo_command)
        except Exception as exc:  # noqa: BLE001
            log.error("Команда промо-боту не прошла: %s: %s", type(exc).__name__, exc)

    async def _click_button(self) -> None:
        needle = self.cfg.promo_button.lower()
        if not needle:
            log.warning("PROMO_MODE=button, но PROMO_BUTTON пуст -> кнопку не нажимал.")
            return

        await asyncio.sleep(self.cfg.promo_delay)
        try:
            messages = await self.client.get_messages(self.cfg.promo_bot, limit=10)
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось прочитать диалог с промо-ботом: %s: %s", type(exc).__name__, exc)
            return

        for message in messages:
            keyboard = getattr(getattr(message, "reply_markup", None), "inline_keyboard", None)
            if not keyboard:
                continue
            for row, buttons in enumerate(keyboard):
                for column, button in enumerate(buttons):
                    if needle in (button.text or "").lower():
                        try:
                            await message.click(x=column, y=row, timeout=self.cfg.promo_delay)
                            log.info("Нажата кнопка промо-бота: «%s».", button.text)
                            return
                        except Exception as exc:  # noqa: BLE001
                            log.error("Кнопка «%s» не нажалась: %s: %s", button.text, type(exc).__name__, exc)
                            return

        log.warning("Среди последних сообщений промо-бота кнопки со словом «%s» нет.", self.cfg.promo_button)
