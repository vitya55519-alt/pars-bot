"""Платные реакции через API smm.plus (https://smm.plus/api/v2).

Под каждый новый пост заказываем реакции: случайное число от REACTIONS_MIN до
REACTIONS_MAX. Меньше REACTIONS_MIN (10) сервис 84 не принимает, поэтому ниже
границы не опускаемся - иначе заказ упадёт с ошибкой.
"""

import logging
import random
from typing import Optional

import aiohttp

log = logging.getLogger("smmplus")
API_URL = "https://smm.plus/api/v2"
REACTIONS_MAX = 50


def roll_reactions(cfg) -> int:
    low = max(1, min(cfg.smmplus_min, REACTIONS_MAX))
    return random.randint(low, REACTIONS_MAX)


async def order(session: aiohttp.ClientSession, cfg, link: str, quantity: int) -> Optional[str]:
    """Создать заказ; вернуть номер или None (причина - в журнале)."""
    data = {
        "key": cfg.smmplus_key,
        "action": "add",
        "service": str(cfg.smmplus_service),
        "link": link,
        "quantity": str(quantity),
    }
    try:
        async with session.post(API_URL, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            body = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - реакции не должны ронять публикацию
        log.error("smm.plus: заказ на %s не ушёл (%s: %s).", link, type(exc).__name__, exc)
        return None
    if isinstance(body, dict) and body.get("order"):
        log.info("smm.plus: заказ %s - %s реакций на %s.", body["order"], quantity, link)
        return str(body["order"])
    log.error("smm.plus: отказ по %s: %s", link, body)
    return None
