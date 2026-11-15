import asyncio
import logging
import re

import aiohttp

log = logging.getLogger("ai")

HARD_LIMIT = 1024


def cut(text: str, limit: int = HARD_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    if " " in head:
        head = head[: head.rfind(" ")]
    return head.rstrip()


def clean(text: str) -> str:
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```", "", text)
    return cut(text.strip())


class AiRewriter:
    def __init__(self, cfg, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session

    async def rewrite(self, text: str) -> str:
        if not self.cfg.minimax_api_key:
            log.warning("MINIMAX_API_KEY пуст -> публикуем оригинальный текст.")
            return cut(text)

        payload = {
            "model": self.cfg.ai_model,
            "messages": [
                {"role": "system", "content": self.cfg.ai_prompt},
                {"role": "user", "content": text},
            ],
            "max_completion_tokens": self.cfg.ai_max_tokens,
            "reasoning_effort": self.cfg.ai_reasoning_effort,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.minimax_api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.cfg.ai_timeout)

        for attempt in range(1, self.cfg.ai_retries + 1):
            try:
                async with self.session.post(self.cfg.ai_url, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        log.warning("ИИ attempt %s/%s: HTTP %s %s", attempt, self.cfg.ai_retries, resp.status, brief(await resp.text()))
                    else:
                        data = await resp.json(content_type=None)
                        content, error = self.parse(data)
                        if content:
                            return clean(content)
                        log.warning("ИИ attempt %s/%s: %s", attempt, self.cfg.ai_retries, error)
            except asyncio.TimeoutError:
                log.warning("ИИ attempt %s/%s: таймаут %s сек", attempt, self.cfg.ai_retries, self.cfg.ai_timeout)
            except aiohttp.ClientError as exc:
                log.warning("ИИ attempt %s/%s: ошибка сети (%s)", attempt, self.cfg.ai_retries, type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - рерайт не должен ронять процесс
                log.warning("ИИ attempt %s/%s: %s: %s", attempt, self.cfg.ai_retries, type(exc).__name__, exc)

            if attempt < self.cfg.ai_retries:
                await asyncio.sleep(self.cfg.ai_retry_delay)

        log.warning("ИИ недоступен после %s попыток -> публикуем оригинальный текст.", self.cfg.ai_retries)
        return cut(text)

    def parse(self, data: dict):
        # MiniMax отдаёт свой код ошибки прямо в теле ответа с HTTP 200.
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code") not in (0, None):
            return "", f"ошибка MiniMax {base_resp.get('status_code')}: {base_resp.get('status_msg')}"

        choices = data.get("choices") or []
        if not choices:
            return "", "пустой ответ (нет choices)"

        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            return "", "пустой ответ (модель не вернула текст)"
        return content, ""


def brief(body: str) -> str:
    return " ".join((body or "").split())[:200]
