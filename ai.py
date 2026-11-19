import asyncio
import logging
import re

import aiohttp

log = logging.getLogger("ai")

# Подпись под картинкой - 1024 символа (ее режет уже публикация), ответ модели
# целиком сохраняем до лимита текстового поста: обрезать всё до 1024 было ошибкой.
MAX_OUTPUT = 3900
# Ключ не подошёл или закончился баланс: повторять бессмысленно, ждём только пополнения счёта.
FATAL_STATUSES = (401, 402, 403)

# MiniMax-M3 умеет класть свои размышления прямо в поле content вместо отдельного
# reasoning. Теги бывают разные - срезаем любой.
THINK_BLOCK_RE = re.compile(
    r"<\s*(think|thinking|mm-think|reasoning)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
THINK_OPEN_RE = re.compile(r"<\s*(think|thinking|mm-think|reasoning)\b", re.IGNORECASE)
CYRILLIC_RE = re.compile(r"[а-яёыэжщф]", re.IGNORECASE)
LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)


def cut(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    if " " in head:
        head = head[: head.rfind(" ")]
    return head.rstrip()


def strip_thinking(text: str) -> str:
    """Вырезает монолог модели. Незакрытый «думать» блок означает, что текст не дошёл."""
    cleaned = THINK_BLOCK_RE.sub("", text)
    if THINK_OPEN_RE.search(cleaned):
        return ""
    return cleaned.strip()


def answered_in_foreign_language(text: str) -> bool:
    """Пост для русскоязычного канала: если букв латиницы больше, чем кириллицы, ответ не годится."""
    cyrillic = len(CYRILLIC_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    return latin > 40 and latin > cyrillic


def language_mismatch(source: str, answer: str) -> bool:
    """Ответ должен быть на том же языке, что и оригинал.

    Русский оригинал - латинский ответ не годится, английский оригинал - русский
    ответ не годится. Короткие тексты и тексты на других языках не судим строго:
    для них проверка молчит, чтобы не зарубить нормальный пост.
    """
    src_cyrillic = len(CYRILLIC_RE.findall(source))
    src_latin = len(LATIN_RE.findall(source))
    if src_cyrillic >= 20 and src_cyrillic > src_latin:
        return answered_in_foreign_language(answer)
    if src_latin > 40 and src_latin > src_cyrillic:
        cyrillic = len(CYRILLIC_RE.findall(answer))
        latin = len(LATIN_RE.findall(answer))
        return cyrillic > 40 and cyrillic > latin
    return False


def clean(text: str) -> str:
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```", "", text)
    return cut(text.strip())


class AiRewriter:
    def __init__(self, cfg, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        # параметр thinking понимают не все адреса MiniMax: при отказе перестаём его слать
        self.supports_thinking = True

    def payload(self, text: str) -> dict:
        body = {
            "model": self.cfg.ai_model,
            "messages": [
                {"role": "system", "content": self.cfg.ai_prompt},
                {"role": "user", "content": text},
            ],
            "max_completion_tokens": self.cfg.ai_max_tokens,
        }
        if self.cfg.ai_thinking and self.supports_thinking:
            body["thinking"] = {"type": self.cfg.ai_thinking}
        return body

    async def rewrite(self, text: str) -> str:
        if not self.cfg.ai_enabled:
            # Общий выключатель REWRITE=0: запрос к MiniMax не уходит ни при каких
            # настройках канала - выбор человека сохранён и заработает вместе с REWRITE=1.
            return cut(text)
        if not self.cfg.minimax_api_key:
            log.warning("MINIMAX_API_KEY пуст -> публикуем оригинальный текст.")
            return cut(text)

        headers = {
            "Authorization": f"Bearer {self.cfg.minimax_api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.cfg.ai_timeout)
        reason = "неизвестно"

        for attempt in range(1, self.cfg.ai_retries + 1):
            try:
                async with self.session.post(
                    self.cfg.ai_url, json=self.payload(text), headers=headers, timeout=timeout
                ) as resp:
                    body = await resp.text()
                    if resp.status in FATAL_STATUSES:
                        log.warning(
                            "ИИ ответил HTTP %s (%s) -> публикуем оригинальный текст. "
                            "Нужен рабочий ключ или баланс MiniMax.",
                            resp.status,
                            brief(body),
                        )
                        return cut(text)
                    if resp.status == 400 and "thinking" in body.lower() and self.supports_thinking:
                        self.supports_thinking = False
                        log.warning("Этот адрес MiniMax не принимает параметр thinking - дальше шлю без него.")
                        continue
                    if resp.status != 200:
                        reason = f"HTTP {resp.status}: {brief(body)}"
                        log.warning("ИИ attempt %s/%s: %s", attempt, self.cfg.ai_retries, reason)
                    else:
                        data = await resp.json(content_type=None)
                        content, reason = self.parse(data, text)
                        if content:
                            return clean(content)
                        log.warning("ИИ attempt %s/%s: %s", attempt, self.cfg.ai_retries, reason)
            except asyncio.TimeoutError:
                reason = f"таймаут {self.cfg.ai_timeout} сек"
                log.warning("ИИ attempt %s/%s: %s", attempt, self.cfg.ai_retries, reason)
            except aiohttp.ClientError as exc:
                reason = f"ошибка сети ({type(exc).__name__})"
                log.warning("ИИ attempt %s/%s: %s", attempt, self.cfg.ai_retries, reason)
            except Exception as exc:  # noqa: BLE001 - рерайт не должен ронять процесс
                reason = f"{type(exc).__name__}: {exc}"
                log.warning("ИИ attempt %s/%s: %s", attempt, self.cfg.ai_retries, reason)

            if attempt < self.cfg.ai_retries:
                await asyncio.sleep(self.cfg.ai_retry_delay)

        log.warning("ИИ недоступен после %s попыток (%s) -> публикуем оригинальный текст.", self.cfg.ai_retries, reason)
        return cut(text)

    def parse(self, data: dict, source: str = ""):
        # MiniMax отдаёт свой код ошибки прямо в теле ответа с HTTP 200.
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code") not in (0, None):
            return "", f"ошибка MiniMax {base_resp.get('status_code')}: {base_resp.get('status_msg')}"

        choices = data.get("choices") or []
        if not choices:
            return "", "пустой ответ (нет choices)"

        message = choices[0].get("message") or {}
        raw = (message.get("content") or "").strip()
        if not raw:
            return "", "пустой ответ (модель не вернула текст)"

        content = strip_thinking(raw)
        if not content:
            return "", "в ответе только размышления модели - готового текста нет"
        if language_mismatch(source, content):
            return "", "модель ответила на другом языке, не как в оригинале"
        return content, ""


def brief(body: str) -> str:
    return " ".join((body or "").split())[:200]
