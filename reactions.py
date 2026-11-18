"""Бесплатные реакции на опубликованный пост.

Сервис принимает заказ так: посетитель пять минут ждёт на странице, его браузер
спрашивает у smmbakparse.ru разрешение и получает permissionKey, и только с ним
форма уходит в Tilda. Без ключа заказ доходит до формы, но реакции не приходят.
Мы делаем те же два шага. Про частоту у сервиса написано двояко: на странице
«побудьте 5 минут - после можете заказать снова», а в правилах «один раз в
час». Пробуем раз в 5 минут и не отступаем после слова «лимит»: отказ - это
потерянное окно сервиса, а не вина поста, поэтому тот же пост попробует ещё раз
в следующем окне. Что именно ответил сервис, каждый раз видно в журнале.
Идентификаторы формы взяты из разметки страницы - если их поменяют, в журнале
станет видно ответ сервиса.

Заказываем каждый новый пост отдельно: чья ссылка ушла раньше, тот пост и
получит реакции, поэтому никто никого не вытесняет. Очередь не бесконечна -
когда в ней 20 постов, самый старый снимаем: реакции на вчерашний пост уже не
нужны.

После заказа бот сам читает реакции под постом (через 20 минут и через два
часа) и пишет в журнал конкретное число. Число реакций считается и ДО заказа:
без него «одна реакция под постом» может оказаться подписчиком, а не сервисом.
Сравнение двух чисел - единственный честный ответ на вопрос «идут реакции или
нет»: в журнале и в кабинете появляется «сервис добавил 3» или «сервис не
добавил ничего». На то, ушёл ли заказ, это не влияет: замер только докладывает.

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
# один пост пробуем два раза: отказ сервиса - не вина поста, в следующем окне попробуем снова
MAX_TRIES = 2
# очередь не резина: держим 20 самых свежих постов, старый снимаем - реакции ему уже не нужны
MAX_QUEUE = 20
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


def reactions_total(message) -> int:
    """Одно число реакций под постом: только сравнив два числа, видно, работал ли сервис."""
    items = list(getattr(getattr(message, "reactions", None), "reactions", None) or [])
    return sum(int(getattr(item, "count", 0) or 0) for item in items)


def verdict(baseline: int, total: int) -> str:
    """Итог заказа человеческим языком: сколько сервис добавил сверх того, что уже было."""
    if baseline < 0 or total < 0:
        return "сравнить не с чем"
    added = total - baseline
    if added > 0:
        return f"сервис добавил {added}"
    if added == 0:
        return "сервис не добавил ничего"
    return f"реакций стало меньше на {-added}"


def baseline_word(baseline: int) -> str:
    return f"до заказа было {baseline}" if baseline >= 0 else "до заказа не измерено"


class ReactionsError(Exception):
    pass


Notifier = Callable[[str], Awaitable[None]]
DeltaNotifier = Callable[[int], Awaitable[None]]


@dataclass
class Entry:
    """Один будущий заказ в порядке: пост, способ, как он туда попал, и кому доложить о результате."""

    link: str
    manual: bool = False
    tries: int = 0
    on_done: Optional[Notifier] = field(default=None, repr=False)
    on_added: Optional[DeltaNotifier] = field(default=None, repr=False)

    def __str__(self) -> str:
        return short(self.link)


class Reactor:
    """Очередь заказов на всех: каждый пост ждёт отдельно, проба раз в 5 минут, отказ - не приговор."""

    def __init__(self, cfg, session, client=None):
        self.cfg = cfg
        self.session = session
        self.client = client
        self.enabled = cfg.reactions_enabled
        self.ordered = 0
        self.failed = 0
        self.skipped = 0
        self.dropped = 0
        self.last_link = ""
        self.last_error = ""
        self.last_reply = ""
        self.last_baseline = -1
        self.last_verdict = ""
        self.measures: List[str] = []
        self.check_delays = CHECK_DELAYS
        self.wait = PERMIT_WAIT
        self.poll = PERMIT_POLL
        self.permit_limit = PERMIT_LIMIT
        self._next_slot = 0.0
        # ключ очереди - ссылка поста: постов в порядке столько, сколько выходило, никто не вытесняет
        self._queue: "OrderedDict[str, Entry]" = OrderedDict()
        self._worker: asyncio.Task | None = None
        self._checks: Set[asyncio.Task] = set()

    def schedule(self, message, on_added: Optional[DeltaNotifier] = None) -> None:
        """Автозаказ: под каждый новый пост - своя ссылка в очередь.

        `on_added` - callback клиента: сколько реакций добавил сервис по замеру
        «до/после». Без него живёт только журнал, с ним - цифра в кабинете.
        """
        if not self.enabled:
            return

        link = post_link(message)
        if not link:
            self.skipped += 1
            log.info("Реакции: у канала нет публичной ссылки - сервис не увидит пост, пропускаю.")
            return

        if self._find(link) is not None:
            log.info("Реакции: %s уже ждёт заказа - второй такой же заказ не ставлю.", short(link))
            return

        self._queue[link] = Entry(link, on_added=on_added)
        self._trim()
        self._ensure_worker()

    def _trim(self) -> None:
        """Очередь ограничена: самый старый пост уступает место свежему, ручные заказы не трогаем."""
        while len(self._queue) > MAX_QUEUE:
            key = next((k for k, entry in self._queue.items() if not entry.manual), None)
            if key is None:
                return
            entry = self._queue.pop(key)
            self.dropped += 1
            log.info(
                "Реакции: очередь из %s постов переполнена - снимаю %s, он уже не свежий.",
                MAX_QUEUE,
                short(entry.link),
            )

    async def request(self, raw: str, on_done: Optional[Notifier] = None) -> str:
        """Ручной заказ из панели.

        Ссылку проверяем руками своего аккаунта: пост, которого аккаунт не
        видит, не увидит и сервис, - такой заказ лучше не начинать. Принятый
        заказ встаёт в ту же очередь, что и автоматические, и ждёт того же окна
        сервиса; о результате докладывает в чат панели через on_done.
        """
        if not self.enabled:
            raise ValueError("Реакции выключены полностью (переменная REACTIONS в Railway) - заказ не приму.")

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
        self._trim()
        self._ensure_worker()
        log.info("Реакции: ручной заказ %s, перед ним в очереди %s.", short(link), ahead)
        return (
            f"Пост {short(link)} поставил в очередь на бесплатные реакции.\n"
            f"Заказов перед ним: {ahead}. {self.eta_line()}.\n"
            f"Сам заказ занимает {human(self.wait)}: сервис требует, чтобы страница была открыта всё это время.\n"
            f"О результате напишу сюда сам, а реакции под постом пересчитаю {self.measure_phrase()} "
            "и сравню с тем, что было до заказа."
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
        потрачено, ещё одно ожидание было бы обманом.
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
        """Когда сервис примет следующий заказ: окно занято прошлым заказом или отказом."""
        remaining = self._next_slot - time.monotonic()
        if remaining <= 0:
            return "окно сервиса свободно"
        return f"следующее окно сервиса через {human(remaining)}"

    def queue_lines(self) -> List[str]:
        """Очередь глазами панели: что ждёт заказа и в каком порядке (ручные - первыми)."""
        if not self._queue:
            return ["    заказов никто не ждёт"]
        order = sorted(self._queue.values(), key=lambda entry: not entry.manual)
        lines = [
            f"    {short(entry.link)} - {'ручной заказ' if entry.manual else 'ждёт своей очереди'}"
            for entry in order[:10]
        ]
        if len(order) > 10:
            lines.append(f"    ...и ещё {len(order) - 10}")
        return lines

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
            entry.tries += 1
            try:
                body = await self.order(entry.link, entry.on_added)
                await self._tell(entry, self._done_text(entry.link, body, self.last_baseline))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - реакции не должны влиять на публикацию
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Реакции: заказ для %s не отправился (%s).", entry.link, self.last_error)
                if entry.tries < MAX_TRIES:
                    # «лимит» от сервиса - не вина поста: ставим в конец и пробуем в следующем окне
                    self._queue[entry.link] = entry
                    log.info(
                        "Реакции: %s попробую ещё раз (попытка %s из %s) - окно сервиса освободится через %s.",
                        short(entry.link),
                        entry.tries + 1,
                        MAX_TRIES,
                        human(self.cfg.reactions_interval),
                    )
                else:
                    self.failed += 1
                    await self._tell(entry, f"⚠ Реакции для {short(entry.link)} не заказались: {self.last_error}")

    def _done_text(self, link: str, body: str, baseline: int = -1) -> str:
        return (
            f"✅ Заказ реакций ушёл: {short(link)} - сервис ответил «{body or 'пусто'}».\n"
            f"{baseline_word(baseline).capitalize()}.\n"
            f"Начало накрутки 0-15 минут, {self.measure_phrase()} посчитаю заново - "
            "по разнице двух чисел и будет видно, добавил ли сервис что-то."
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

    async def order(self, link: str, on_added: Optional[DeltaNotifier] = None) -> str:
        token, key = await self._permission()
        self.last_baseline = await self._total(link)
        body = await self._send(link, token, key)
        self.ordered += 1
        self.last_link = link
        self.last_error = ""
        self.last_reply = body
        log.info(
            "Реакции: заказ на %s отправлен (старт 0-15 минут), сервис ответил «%s», %s.",
            link,
            body or "пусто",
            baseline_word(self.last_baseline),
        )
        self._watch(link, self.last_baseline, on_added)
        return body

    async def _total(self, link: str) -> int:
        """Точка отсчёта: сколько реакций под постом прямо перед заказом.

        Без неё «одна реакция» нераспознать - её мог оставить подписчик, а
        сервис не поставить ни одной. Снятие не отменяет заказ: не удалось -
        честно скажем «сравнить не с чем», вместо выдуманного числа.
        """
        if self.client is None:
            return -1
        try:
            _, total = await self.snapshot(link)
        except Exception as exc:  # noqa: BLE001 - базовое число не повод не заказывать
            log.info("Реакции: число до заказа не снять (%s) - заказ всё равно уйдёт.", type(exc).__name__)
            return -1
        return total

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

    def _watch(self, link: str, baseline: int = -1, on_added: Optional[DeltaNotifier] = None) -> None:
        """Замер вместо гаданий: читаем реакции под этим же постом и сравниваем с «до»."""
        if self.client is None:
            return
        for delay in self.check_delays:
            task = asyncio.create_task(self._measure(link, delay, baseline, on_added))
            self._checks.add(task)
            task.add_done_callback(self._checks.discard)

    async def _measure(
        self, link: str, delay: float, baseline: int = -1, on_added: Optional[DeltaNotifier] = None
    ) -> None:
        try:
            await asyncio.sleep(delay)
            shown, total = await self.snapshot(link)
            self.last_verdict = verdict(baseline, total)
            line = f"{short(link)} через {human(delay)}: {shown} - {baseline_word(baseline)}, {self.last_verdict}"
            if on_added is not None and baseline >= 0 and total >= 0:
                await self._report_added(on_added, total - baseline, link)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - замер не должен влиять на заказы
            line = f"{short(link)} через {human(delay)}: не измерить ({type(exc).__name__})"
        self.measures.insert(0, line)
        del self.measures[MEASURE_KEEP:]
        log.info("Реакции: замер - %s.", line)

    async def _report_added(self, on_added: DeltaNotifier, added: int, link: str) -> None:
        """Дельта заказа - клиенту в базу: кабинету нужно конкретное число, а не «работает/нет»."""
        try:
            await on_added(added)
        except Exception as exc:  # noqa: BLE001 - запись клиенту не рвёт замер
            log.warning("Реакции: итог заказа для %s не записан (%s).", short(link), type(exc).__name__)

    async def snapshot(self, link: str) -> Tuple[str, int]:
        """Что под постом сейчас: надпись для человека и число для сравнения «до/после»."""
        username, message_id = parse_link(link)
        if not message_id:
            return "ссылка непонятна", -1
        message = await call(self.client.get_messages, username, message_id)
        if message is None:
            return "поста там нет", -1
        return count_reactions(message), reactions_total(message)

    async def count(self, link: str) -> str:
        text, _ = await self.snapshot(link)
        return text

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
        result = f", {self.last_verdict}" if self.last_verdict else ""
        queue = f", в очереди {len(self._queue)}" if self._queue else ""
        manual = sum(1 for entry in self._queue.values() if entry.manual)
        hand = f", из них ручных {manual}" if manual else ""
        fails = f", сбоев {self.failed}" if self.failed else ""
        skipped = f", пропущено {self.skipped}" if self.skipped else ""
        dropped = f", снято со старья {self.dropped}" if self.dropped else ""
        return f"Реакции: {state}{done}{result}{queue}{hand}{fails}{skipped}{dropped}"

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
