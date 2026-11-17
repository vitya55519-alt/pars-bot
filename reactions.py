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

Заказ бывает и ручной: из панели приходят ссылкой на пост, Reactor проверяет,
что аккаунт этот пост видит, ставит его в ту же очередь - и по завершении сам
докладывает в чат панели. Без доклада человек не узнал бы, заказ пошёл или нет:
сам заказ длится несколько минут.
"""

import asyncio
import json
import logging
import random
import re
import string
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Set, Tuple

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

PERMIT_WAIT = 330
PERMIT_POLL = 10
PERMIT_LIMIT = 900
FORM_ATTEMPTS = 2
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


def wanted_link(raw: str) -> Tuple[str, int]:
    """Ссылка, которую человек прислал из панели: только публичная и только на конкретный пост."""
    username, message_id = parse_link(raw or "")
    if not message_id:
        raise ValueError("Не вижу ссылки на пост. Пришли адрес вида https://t.me/канал/123 - одной ссылкой.")
    if username.lower() == "c":
        raise ValueError(
            f"Это ссылка закрытого канала ({short(raw)}): сервис по ней пост не откроет. "
            "Нужен пост канала с публичным @юзернеймом."
        )
    return username, message_id


def count_reactions(message) -> str:
    """Что реально стоит под постом: «нет» означает, что сервис не начал накрутку."""
    info = getattr(message, "reactions", None)
    items = list(getattr(info, "reactions", None) or [])
    if not items:
        return "нет"
    parts = []
    total = 0
    for item in items:
        emoji = getattr(item, "emoji", None) or getattr(item, "emoticon", None) or "?"
        count = int(getattr(item, "count", 0) or 0)
        total += count
        parts.append(f"{emoji}{count}")
    return " ".join(parts) + (f" (всего {total})" if len(parts) > 1 else "")


class ReactionsError(Exception):
    pass


Notifier = Callable[[str], Awaitable[None]]


@dataclass
class Entry:
    """Один будущий заказ в очереди: пост, способ, как он туда попал, и кому доложить о результате."""

    link: str
    manual: bool = False
    on_done: Optional[Notifier] = field(default=None, repr=False)

    def __str__(self) -> str:
        return short(self.link)


class Reactor:
    """Один заказ в час на всех, как требует сервис: каналу достаётся заказ на его самый свежий пост."""

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
        # ключ: канал для заказа со страницы, сама ссылка для ручного - так ручной не вытесняется новым постом
        self._queue: "OrderedDict[str, Entry]" = OrderedDict()
        self._worker: asyncio.Task | None = None
        self._checks: Set[asyncio.Task] = set()

    def schedule(self, message) -> None:
        """Автозаказ: у канала в порядке держится один, самый свежий его пост."""
        if not self.enabled:
            return

        link = post_link(message)
        if not link:
            self.skipped += 1
            log.info("Реакции: у канала нет публичной ссылки - сервис не увидит пост, пропускаю.")
            return

        username, _ = parse_link(link)
        if self._find(link) is not None:
            log.info("Реакции: %s уже ждёт отдельного заказа - в общий порядок его не ставлю.", short(link))
            return

        old = self._queue.get(username)
        if old:
            log.info("Реакции: %s уже ждёт заказа - ставлю вместо него %s.", short(old.link), short(link))
        self._queue[username] = Entry(link)
        self._queue.move_to_end(username)
        self._ensure_worker()

    async def request(self, raw: str, on_done: Optional[Notifier] = None) -> str:
        """Ручной заказ из панели.

        Ссылку проверяем руками своего аккаунта: пост, которого аккаунт не
        видит, не увидит и сервис, - такой заказ лучше не начинать. Принятый
        заказ встаёт в ту же очередь, что и автоматические, и ждёт того же окна
        сервиса; о результате докладывает в чат панели через on_done.
        """
        if not self.enabled:
            raise ValueError("Реакции выключены - включи их кнопкой «🎭 Реакции», затем пришли ссылку на пост.")

        username, message_id = wanted_link(raw)
        await self._see_post(username, message_id)
        link = f"https://t.me/{username}/{message_id}?single"

        key = self._find(link)
        if key is not None and self._queue[key].manual:
            self._ensure_worker()
            return f"Пост {short(link)} уже ждёт своего часа в очереди. {self.eta_line()}."
        if key is not None:
            del self._queue[key]  # пост уже стоял как самый свежий канала - становится ручным заказом

        ahead = sum(1 for entry in self._queue.values() if entry.manual)
        self._queue[link] = Entry(link, manual=True, on_done=on_done)
        self._ensure_worker()
        log.info("Реакции: ручной заказ %s, перед ним в очереди %s.", short(link), ahead)
        return (
            f"Пост {short(link)} поставил в очередь на бесплатные реакции.\n"
            f"Заказов перед ним: {ahead}. {self.eta_line()}.\n"
            f"Сам заказ занимает {human(self.wait)}: сервис требует, чтобы страница была открыта всё это время.\n"
            f"О результате напишу сюда сам, а реакции под постом пересчитаю {self.measure_phrase()}."
        )

    async def _see_post(self, username: str, message_id: int) -> None:
        """Что не видит наш аккаунт, то не увидит и сервис: проверяем пост до заказа."""
        if self.client is None:
            return
        try:
            message = await call(self.client.get_messages, username, message_id)
        except Exception as exc:  # noqa: BLE001 - канал может быть закрыт для этого аккаунта
            raise ValueError(
                f"Пост {message_id} в канале {username} не открылся ({type(exc).__name__}) - "
                "пришли ссылку на пост канала, в котором аккаунт состоит."
            ) from exc
        if message is None:
            raise ValueError(f"Поста {message_id} в канале {username} нет - проверь номер в ссылке.")

    def _find(self, link: str) -> Optional[str]:
        """Ключ уже ждущего заказа на тот же пост: дублей не плодим."""
        return next((key for key, entry in self._queue.items() if entry.link == link), None)

    def _next_key(self) -> str:
        """Что заказывать следующим: о чём просили руками, идёт раньше порядка свежих постов."""
        for key, entry in self._queue.items():
            if entry.manual:
                return key
        return next(iter(self._queue))

    def _take(self, key: str) -> Optional[Entry]:
        """Заказ, дождавшийся окна сервиса.

        За ожидание пост канала мог измениться или переехать в ручные - тогда
        берём первый по порядку от того, что осталось: окно сервиса уже
        потрачено, ещё один час ожидания был бы обманом.
        """
        entry = self._queue.pop(key, None)
        if entry is None and self._queue:
            entry = self._queue.pop(self._next_key())
        return entry

    def _ensure_worker(self) -> None:
        """Очередь обслуживает одна задача: заводить вторую на тот же порядок незачем."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def eta_line(self) -> str:
        """Когда сервис примет следующий заказ: он даёт один заказ в час на всех."""
        remaining = self._next_slot - time.monotonic()
        if remaining <= 0:
            return "окно сервиса свободно"
        return f"следующее окно сервиса через {human(remaining)}"

    def queue_lines(self) -> List[str]:
        """Очередь глазами панели: что ждёт заказа и в каком порядке."""
        if not self._queue:
            return ["    заказов никто не ждёт"]
        order = sorted(self._queue.values(), key=lambda entry: not entry.manual)
        return [f"    {short(entry.link)} - {'ручной заказ' if entry.manual else 'самый свежий пост'}" for entry in order]

    def _reserve(self) -> float:
        now = time.monotonic()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self.cfg.reactions_interval
        return slot - now

    async def _run(self) -> None:
        """Ждём своей минуты, не вынимая пост из очереди: за ожидание мог выйти новый."""
        while self._queue:
            key = self._next_key()
            wait = self._reserve()
            if wait > 1:
                log.info("Реакции: жду %s перед заказом %s.", human(wait), short(self._queue[key].link))
                await asyncio.sleep(wait)
            entry = self._take(key)
            if entry is None:
                continue
            try:
                body = await self.order(entry.link)
                await self._tell(entry, self._done_text(entry.link, body))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - реакции не должны влиять на публикацию
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Реакции: заказ для %s не отправился (%s).", entry.link, self.last_error)
                await self._tell(entry, f"⚠ Реакции для {short(entry.link)} не заказались: {self.last_error}")

    def _done_text(self, link: str, body: str) -> str:
        return (
            f"✅ Заказ реакций ушёл: {short(link)} - сервис ответил «{body or 'пусто'}».\n"
            f"Начало накрутки 0-15 минут. Реакции под постом пересчитаю {self.measure_phrase()} - "
            "числа будут в «🩺 Проверка»."
        )

    def measure_phrase(self) -> str:
        """Как долго после заказа ждать замеров - теми же паузами, что стоят в коде."""
        parts = " и ".join(human(delay) for delay in self.check_delays)
        return f"через {parts}" if parts else "после старта накрутки"

    async def _tell(self, entry: Entry, text: str) -> None:
        """Ручной заказ человек ждёт в чате панели; автоматический живёт только в журнале."""
        if entry.on_done is None:
            return
        try:
            await entry.on_done(text)
        except Exception as exc:  # noqa: BLE001 - доклад не должен рвать очередь
            log.warning("Реакции: о результате заказа не доложил (%s).", type(exc).__name__)

    async def order(self, link: str) -> str:
        token, key = await self._permission()
        body = await self._send(link, token, key)
        self.ordered += 1
        self.last_link = link
        self.last_error = ""
        self.last_reply = body
        log.info("Реакции: заказ на %s отправлен (старт 0-15 минут), сервис ответил «%s».", link, body or "пусто")
        self._watch(link)
        return body

    async def _send(self, link: str, token: str, key: str) -> str:
        """Обрыв соединения у сервиса бывает часто - без повтора пост терял заказ молча."""
        last: Exception | None = None
        for attempt in range(1, FORM_ATTEMPTS + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=self.cfg.reactions_timeout)
                async with self.session.post(
                    FORM_URL, data=payload(link, self.cfg, token, key), headers=HEADERS, timeout=timeout
                ) as resp:
                    body = " ".join((await resp.text()).split())[:200]
                    if resp.status != 200 or not body.upper().startswith("OK"):
                        raise ReactionsError(f"HTTP {resp.status}: {body or 'пустой ответ'}")
                    return body
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = exc
                log.warning("Реакции: форма не дошла, попытка %s из %s (%s).", attempt, FORM_ATTEMPTS, type(exc).__name__)
        raise ReactionsError(f"сервис не принял форму ({type(last).__name__})")

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
        queue = f", в очереди {len(self._queue)}" if self._queue else ""
        manual = sum(1 for entry in self._queue.values() if entry.manual)
        hand = f", из них ручных {manual}" if manual else ""
        fails = f", сбоев {self.failed}" if self.failed else ""
        skipped = f", пропущено {self.skipped}" if self.skipped else ""
        return f"Реакции: {state}{done}{queue}{hand}{fails}{skipped}"

    def report(self) -> str:
        """Показывает очередь и замеры в панели: без журнала видно, дошли ли реакции до поста."""
        queue = "Очередь реакций:\n" + "\n".join(self.queue_lines()) + f"\n    {self.eta_line()}"
        if not self.measures:
            return queue + "\nРеакции: после заказа ещё ничего не измерено - замеры появятся через 20 мин."
        return queue + "\nРеакции под постами после заказа:\n" + "\n".join(f"    {line}" for line in self.measures)


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
