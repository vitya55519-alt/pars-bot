import asyncio
import logging
import sys

import aiohttp
from dotenv import load_dotenv
from pyrogram import Client, idle
from pyrogram.errors import FloodWait, SessionExpired, SessionRevoked, Unauthorized

from ai import AiRewriter
from clients import Clients, SYNC_SECONDS
from config import Config, ConfigError
from handlers import ParsBot
from panel import Panel
from registry import Registry, RegistryError
from store import Store

log = logging.getLogger("pars")


async def start_panel(panel_client: Client, panel: Panel, owner_id: int, account_name: str) -> bool:
    """Входит ботом панели. FloodWait ждём на месте: перезапуск процесса только удлиняет бан."""
    while True:
        try:
            await panel_client.start()
            bot_me = await panel_client.get_me()
            await panel.start(owner_id, account_name)
            log.info("Панель управления: @%s - открой её и нажми /start", bot_me.username or bot_me.id)
            return True
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0) + 5
            log.warning(
                "Telegram просит паузу %s сек перед входом бота - пережду сам, кнопки появятся после неё. "
                "Автопостинг в это время работает.",
                wait,
            )
            await asyncio.sleep(wait)
        except Unauthorized:
            log.error("BOT_TOKEN не принял Telegram - кнопок не будет, автопостинг работает.")
            return False
        except Exception as exc:  # noqa: BLE001 - панель не должна ронять процесс
            log.error("Панель не поднялась (%s: %s) - кнопки молчат, автопостинг работает.", type(exc).__name__, exc)
            return False


async def clients_loop(clients: Clients, every: int = SYNC_SECONDS) -> None:
    """Клиентскую часть обслуживаем фоном: подписки и проверки прав не должны мешать постам."""
    while True:
        try:
            await clients.sync()
        except Exception as exc:  # noqa: BLE001 - без клиентов личный режим работает как раньше
            log.error("Клиенты не обслужены (%s: %s) - повторю через %s сек.", type(exc).__name__, exc, every)
        await asyncio.sleep(every)


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
    bot = ParsBot(client, cfg, AiRewriter(cfg, session), registry, session)

    store = None
    clients = None
    sync = None
    if cfg.clients_enabled:
        try:
            store = await Store.connect(cfg.database_url)
            clients = Clients(client, store)
            bot.attach(store, clients)
        except Exception as exc:  # noqa: BLE001 - без базы бот остаётся управляющим панелем
            log.error("База клиентов не подключилась (%s: %s) - кабинеты выключены, личный режим работает.", type(exc).__name__, exc)
            store = clients = None

    panel_client = None
    panel = None
    panel_task = None
    if cfg.panel_enabled:
        panel_client = Client(
            name="pars-panel",
            api_id=cfg.api_id,
            api_hash=cfg.api_hash,
            bot_token=cfg.bot_token,
            in_memory=True,
            workers=2,
        )
        panel = Panel(panel_client, cfg, registry, bot, store)
        panel.setup()

    try:
        await client.start()
        me = await client.get_me()
        log.info("Вход выполнен: %s (id %s)", me.first_name or me.username or "аккаунт", me.id)

        # Панель поднимается фоном: пауза Telegram на входе бота не должна держать
        # весь автопостинг - иначе в это время не ловится ни один пост.
        if panel:
            panel.claim_owner(me.id, me.first_name or "аккаунт")
            panel_task = asyncio.create_task(start_panel(panel_client, panel, me.id, me.first_name or "аккаунт"))

        while not registry.ready:
            try:
                await bot.setup()
            except RegistryError as exc:
                log.error("Список каналов не собрался: %s", exc)
                if cfg.panel_enabled:
                    log.info("Нажми «🏛 Создать группу» в панели бота - проверю заново через минуту.")
                else:
                    log.info("Без BOT_TOKEN кнопок нет: создай группу с темами и добавь туда этот аккаунт.")
                await asyncio.sleep(60)

        log.info("Настройки:\n%s", cfg.describe())
        log.info("Готов - жду новые посты из каналов-источников.")
        sync = asyncio.create_task(clients_loop(clients)) if clients else None
        await idle()
    except (Unauthorized, SessionExpired, SessionRevoked) as exc:
        log.error("Сессия недействительна (%s). Нужно создать SESSION_STRING заново: python make_session.py", type(exc).__name__)
        raise SystemExit(1)
    finally:
        if sync:
            sync.cancel()
        if panel_task:
            panel_task.cancel()
        if store:
            await store.close()
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
