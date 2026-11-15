import os
from dataclasses import dataclass, field
from typing import Union

MINIMAX_URL = "https://api.minimax.io/v1/chat/completions"
DEFAULT_MODEL = "MiniMax-M3"
EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_PROMPT = (
    "Ты — редактор Telegram-канала. Перепиши присланный рекламный или бизнес-пост своими словами. "
    "Сохрани смысл, все факты, цифры, цены и призыв к действию. "
    "Сделай текст вовлекающим: цепляющая первая строка, короткие абзацы, 1-3 эмодзи в меру. "
    "Полностью удали ссылки, юзернеймы, @упоминания и любые упоминания авторов, доноров и исходных каналов. "
    "Не придумывай ничего, чего нет в оригинале. "
    "Выведи только готовый текст поста, без пояснений, заголовков и кавычек."
)


class ConfigError(Exception):
    pass


def _chat(raw: str) -> Union[int, str]:
    value = raw.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value.lstrip("@")


def _text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _number(name: str, default: float) -> float:
    raw = _text(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Config:
    api_id: int
    api_hash: str
    session_string: str
    management_chat: Union[int, str, None] = None
    promo_bot: Union[int, str, None] = None
    minimax_api_key: str = ""
    ai_url: str = MINIMAX_URL
    ai_model: str = DEFAULT_MODEL
    ai_prompt: str = DEFAULT_PROMPT
    ai_max_tokens: int = 2000
    ai_reasoning_effort: str = "low"
    ai_timeout: int = 90
    ai_retries: int = 3
    ai_retry_delay: int = 5
    promo_button: str = "Просмотры"
    promo_views: int = 0
    promo_delay: int = 3
    album_wait: float = 2.0
    cache_size: int = 500
    history_limit: int = 200
    caption_limit: int = field(default=1024)

    @property
    def promo_enabled(self) -> bool:
        return bool(self.promo_bot)

    @classmethod
    def load(cls) -> "Config":
        missing = [
            name
            for name in ("API_ID", "API_HASH", "SESSION_STRING")
            if not _text(name)
        ]
        if missing:
            raise ConfigError("Не заданы переменные: " + ", ".join(missing))

        return cls(
            api_id=int(_number("API_ID", 0)),
            api_hash=_text("API_HASH"),
            session_string=_text("SESSION_STRING"),
            management_chat=_chat(_text("MANAGEMENT_CHAT")) or None,
            promo_bot=_chat(_text("PROMO_BOT")) or None,
            minimax_api_key=_text("MINIMAX_API_KEY"),
            ai_url=_text("MINIMAX_API_URL", MINIMAX_URL),
            ai_model=_text("AI_MODEL", DEFAULT_MODEL),
            ai_prompt=_text("AI_SYSTEM_PROMPT", DEFAULT_PROMPT),
            ai_max_tokens=int(_number("AI_MAX_TOKENS", 2000)),
            ai_reasoning_effort=_text("AI_REASONING_EFFORT", "low"),
            ai_timeout=int(_number("AI_TIMEOUT", 90)),
            ai_retries=int(_number("AI_RETRIES", 3)),
            ai_retry_delay=int(_number("AI_RETRY_DELAY", 5)),
            promo_button=_text("PROMO_BUTTON", "Просмотры"),
            promo_views=int(_number("PROMO_VIEWS", 0)),
            promo_delay=int(_number("PROMO_DELAY", 3)),
            album_wait=_number("ALBUM_WAIT", 2.0),
            cache_size=int(_number("CACHE_SIZE", 500)),
            history_limit=int(_number("HISTORY_LIMIT", 200)),
        )

    def describe(self) -> str:
        if self.promo_enabled:
            steps = []
            if self.promo_button:
                steps.append(f"жму «{self.promo_button}»")
            steps.append("пересылаю пост")
            steps.append(f"+{self.promo_views} просмотров" if self.promo_views else "число просмотров не задано")
            promo = f"{self.promo_bot}: " + " -> ".join(steps)
        else:
            promo = "выключено"
        group = self.management_chat or "найду сам среди групп с темами"
        return (
            f"Группа управления донорами: {group}\n"
            f"Промо-бот: {promo}\n"
            f"Нейросеть: {self.ai_model} (глубина {self.ai_reasoning_effort}), "
            f"попыток: {self.ai_retries}, пауза: {self.ai_retry_delay} сек"
        )
