"""Бесплатные реакции на опубликованный пост.

Сервис принимает заказ так: посетитель пять минут ждёт на странице, его браузер
спрашивает у smmbakparse.ru разрешение и получает permissionKey, и только с ним
форма уходит в Tilda. Без ключа заказ доходит до формы, но реакции не приходят.
Мы делаем те же два шага и держим паузу, которую просит сам сервис: заказ не
чаще одного в час. Идентификаторы формы взяты из разметки страницы - если их
поменяют, в журнале станет видно ответ сервиса.

После заказа бот сам читает реакции под постом (через 20 минут и через два
часа) и пишет в журнал конкретное число: только так видно, отвечает сервис или
заказ ушёл в пустоту.
"""

import asyncio
import json
import logging
import random
import re
import string
import time
from typing import List, Set, Tuple

import aiohttp

from utils import call

log = logging.getLogger("reactions")

FORM_URL = "https://forms.tildacdn.com/procces/"
PAGE_URL = "https://smmnakrutka.ru/free-telegram-reactions"
PERMIT_URL = "https://smmbakparse.ru/php/countTimePermission.php"
FORM_ID = "form510249937"
FORMS_KEY = "ab3463c0005d093966efa328a079f94f"
PROJECT_ID = "4412478"
PAGE_ID = "31547689"
SERVICE_ID = "680"
SERVICE_KEY = "c8d6dc745fa5773025b1d741ce72f2ca"
PRODUCT = "Реакции Телеграм"
QUANTITY = "50"

MAX_PENDING = 6
PERMIT_WAIT = 330
PERMIT_POLL = 10
PERMIT_LIMIT = 900
CHECK_DELAYS = (1200, 7200)
MEASURE_KEEP = 8
LINK_RE = re.compile(r"t\.me/([^/?#]+)/(\d+)", re.IGNORECASE)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Origin": "https://smmnakrutka.ru",
    "Referer": PAGE_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def new_token() -> str:
    """Как на сайте: два случайных base36-хвоста подряд."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=11)) + "".join(random.choices(alphabet, k=11))


def post_link(message) -> str:
    """Ссылка на пост для сервиса: закрытый канал без юзернейма он открыть не сможет."""
    username = getattr(getattr(message, "chat", None), "username", None) or ""
    message_id = getattr(message, "id", 0)
    if not username or not message_id:
        return ""
    return f"https://t.me/{username}/{message_id}?single"


def parse_link(link: str) -> Tuple[str, int]:
    """Обратный ход: из ссылки, отданной сервису, достаём канал и номер поста."""
    match = LINK_RE.search(link or "")
    if not match:
        return "", 0
    return match.group(1), int(match.group(2))


def short(link: str) -> str:
    username, message_id = parse_link(link)
    return f"{username}/{message_id}" if message_id else (link or "?")


def count_reactions(message) -> str:
    """Что реально стоит под постом: «нет» означает, что сервис не начал накрутку."""
    reactions = getattr(message, "reactions", None)
    results = list(getattr(reactions, "results", None) or [])
    if not results:
        return "нет"
    total = getattr(reactions, "total_count", None)
    parts = []
    for item in results:
        emoji = getattr(getattr(item, "reaction", None), "emoji", None) or "?"
        parts.append(f"{emoji}{getattr(item, 'total_count', 0)}")
    head = " ".join(parts)
    if total is not None and len(parts) > 1:
        head += f" (всего {total})"
    return head


class ReactionsError(Exception):
    pass


class Reactor:
    """Заказы уходят по одному в час, как требует сервис: каждому посту достаётся своя очередь."""

    def __init__(self, cfg, session, client=None):
        self.cfg = cfg
        self.session = session
        self.client = client
        self.enabled = cfg.reactions_enabled
        self.ordered = 0
        self.failed = 0
        self.skipped = 0
        self.last_link = ""
        self.last_error = ""
        self.last_reply = ""
        self.measures: List[str] = []
        self.check_delays = CHECK_DELAYS
        self.wait = PERMIT_WAIT
        self.poll = PERMIT_POLL
        self.permit_limit = PERMIT_LIMIT
        self._next_slot = 0.0
        self._tasks: Set[asyncio.Task] = set()
        self._checks: Set[asyncio.Task] = set()

    def schedule(self, message) -> None:
        if not self.enabled:
            return

        link = post_link(message)
        if not link:
            self.skipped += 1
            log.info("Реакции: у канала нет публичной ссылки - сервис не увидит пост, пропускаю.")
            return

        if len(self._tasks) >= MAX_PENDING:
            self.skipped += 1
            log.warning("Реакции: очередь из %s заказов уже ждёт своей минуты - этот пост пропускаю.", MAX_PENDING)
            return

        wait = self._reserve()
        task = asyncio.create_task(self._later(link, wait))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _reserve(self) -> float:
        now = time.monotonic()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self.cfg.reactions_interval
        return slot - now

    async def _later(self, link: str, wait: float) -> None:
        if wait > 1:
            log.info("Реакции: жду %s перед заказом %s.", human(wait), link)
            await asyncio.sleep(wait)
        try:
            await self.order(link)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - реакции не должны влиять на публикацию
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Реакции: заказ для %s не отправился (%s).", link, self.last_error)

    async def order(self, link: str) -> None:
        token, key = await self._permission()
        timeout = aiohttp.ClientTimeout(total=self.cfg.reactions_timeout)
        async with self.session.post(
            FORM_URL, data=payload(link, self.cfg, token, key), headers=HEADERS, timeout=timeout
        ) as resp:
            body = " ".join((await resp.text()).split())[:200]
            if resp.status != 200 or not body.upper().startswith("OK"):
                raise ReactionsError(f"HTTP {resp.status}: {body or 'пустой ответ'}")

        self.ordered += 1
        self.last_link = link
        self.last_error = ""
        self.last_reply = body
        log.info("Реакции: заказ на %s отправлен (старт 0-15 минут), сервис ответил «%s».", link, body or "пусто")
        self._watch(link)

    def _watch(self, link: str) -> None:
        """Замер вместо гаданий: читаем реакции под этим же постом позже."""
        if self.client is None:
            return
        for delay in self.check_delays:
            task = asyncio.create_task(self._measure(link, delay))
            self._checks.add(task)
            task.add_done_callback(self._checks.discard)

    async def _measure(self, link: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            line = f"{short(link)} через {human(delay)}: {await self.count(link)}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - замер не должен влиять на заказы
            line = f"{short(link)} через {human(delay)}: не измерить ({type(exc).__name__})"
        self.measures.insert(0, line)
        del self.measures[MEASURE_KEEP:]
        log.info("Реакции: замер - %s.", line)

    async def count(self, link: str) -> str:
        username, message_id = parse_link(link)
        if not message_id:
            return "ссылка непонятна"
        message = await call(self.client.get_messages, username, message_id)
        if message is None:
            return "поста там нет"
        return count_reactions(message)

    async def _permission(self):
        """Пять минут ждём честно - сервис выдаёт ключ сам, когда время выйдет."""
        token = new_token()
        await asyncio.sleep(self.wait)
        deadline = time.monotonic() + self.permit_limit
        while True:
            key = await self._ask(token)
            if key:
                return token, key
            if time.monotonic() >= deadline:
                raise ReactionsError("сервис не дал разрешение на заказ")
            await asyncio.sleep(self.poll)

    async def _ask(self, token: str) -> str:
        timeout = aiohttp.ClientTimeout(total=self.cfg.reactions_timeout)
        async with self.session.post(PERMIT_URL, json={"sessionToken": token}, headers=HEADERS, timeout=timeout) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise ReactionsError(f"разрешение: HTTP {resp.status}")
            try:
                data = json.loads(body or "{}")
            except ValueError:
                raise ReactionsError(f"разрешение: ответ не json ({body[:80]})")
        return str(data.get("data") or "") if isinstance(data, dict) else ""

    def describe(self) -> str:
        state = f"включены, заказ раз в {human(self.cfg.reactions_interval)}" if self.enabled else "выключены"
        done = f", отправлено {self.ordered}" if self.ordered else ""
        queue = f", в очереди {len(self._tasks)}" if self._tasks else ""
        fails = f", сбоев {self.failed}" if self.failed else ""
        skipped = f", пропущено {self.skipped}" if self.skipped else ""
        return f"Реакции: {state}{done}{queue}{fails}{skipped}"

    def report(self) -> str:
        """Показывает замеры в панели: без журнала видно, дошли ли реакции до поста."""
        if not self.measures:
            return "Реакции: после заказа ещё ничего не измерено - замеры появятся через 20 мин."
        return "Реакции под постами после заказа:\n" + "\n".join(f"    {line}" for line in self.measures)


def payload(link: str, cfg, token: str, key: str) -> dict:
    return {
        "formservices[]": SERVICE_KEY,
        "quantity": QUANTITY,
        "quantity-text": PRODUCT,
        "Email": cfg.reactions_email,
        "link": link,
        "hours": "3",
        "rand": "",
        "endtime": "",
        "verify": "",
        "url_page": "smmnakrutka.ru",
        "sessionToken": token,
        "permissionKey": key,
        "service": SERVICE_ID,
        "form-spec-comments": "",
        "tildaspec-cookie": "",
        "tildaspec-referer": PAGE_URL,
        "tildaspec-formid": FORM_ID,
        "tildaspec-formskey": FORMS_KEY,
        "tildaspec-version-lib": "2.2",
        "tildaspec-pageid": PAGE_ID,
        "tildaspec-projectid": PROJECT_ID,
        "tildaspec-lang": "RU",
        "tildaspec-fp": "st0w1280h800ft0",
    }


def human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} сек"
    if seconds < 5400:
        return f"{round(seconds / 60)} мин"
    return f"{round(seconds / 3600, 1)} ч"
