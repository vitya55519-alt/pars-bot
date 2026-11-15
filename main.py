import asyncio
import logging
import sys

import aiohttp
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import SessionExpired, SessionRevoked, Unauthorized

from ai import AiRewriter
from config import Config, ConfigError
from handlers import ParsBot
from panel import Panel
from registry import Registry, RegistryError

log = logging.getLogger("pars")


async def amain(cfg: Config) -> None:
    session = aiohttp.ClientSession()
    client = Client(
        name="pars-bot",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        session_string=cfg.session_string,
        workers=4,
    )
    registry = Registry(client, cfg)
    bot = ParsBot(client, cfg, AiRewriter(cfg, session), registry)

    panel_client = None
    panel = None
    if cfg.panel_enabled:
        panel_client = Client(
            name="pars-panel",
            api_id=cfg.api_id,
            api_hash=cfg.api_hash,
            bot_token=cfg.bot_token,
            in_memory=True,
            workers=2,
        )
        panel = Panel(panel_client, cfg, registry, bot)
        panel.setup()

    try:
        await client.start()
        me = await client.get_me()
        log.info("Вход выполнен: %s (id %s)", me.first_name or me.username or "аккаунт", me.id)

        if panel_client:
            await panel_client.start()
            bot_me = await panel_client.get_me()
            await panel.start(me.id, me.first_name or "аккаунт")
            log.info("Панель управления: @%s - открой её и нажми /start", bot_me.username or bot_me.id)

        while not registry.ready:
            try:
                await bot.setup()
            except RegistryError as exc:
                log.error("Управление донорами не настроено: %s", exc)
                log.info("Создай группу с темами и добавь туда этот аккаунт - проверю заново через минуту.")
                await asyncio.sleep(60)

        log.info("Настройки:\n%s", cfg.describe())
        log.info("Готов - жду новые посты от доноров.")
        await client.idle()
    except (Unauthorized, SessionExpired, SessionRevoked) as exc:
        log.error("Сессия недействительна (%s). Нужно создать SESSION_STRING заново: python make_session.py", type(exc).__name__)
        raise SystemExit(1)
    finally:
        await session.close()
        for app in (client, panel_client):
            if not app:
                continue
            try:
                await app.stop()
            except Exception:  # noqa: BLE001 - клиент мог не стартовать
                pass


def main() -> None:
    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    try:
        cfg = Config.load()
    except ConfigError as exc:
        log.error("Не запускаюсь: %s", exc)
        raise SystemExit(1)

    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        log.info("Остановлено вручную.")


if __name__ == "__main__":
    main()
