import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Union

MINIMAX_URL = "https://api.minimax.io/v1/chat/completions"
DEFAULT_MODEL = "MiniMax-M3"
THINKING_MODES = ("disabled", "adaptive")

# потолок сервиса накрутки: больше десяти тысяч просмотров за заказ он не даёт
VIEWS_LIMIT = 10000
POSTS_LIMIT = 1000

DEFAULT_PROMPT = (
    "Ты — редактор Telegram-канала. Перепиши присланный пост своими словами, "
    "чтобы текст стал уникальным и не совпадал с оригиналом дословно. "
    "Отвечай строго на том же языке, на котором написан оригинал: "
    "русский текст — по-русски, английский — по-английски, любой другой язык — как в оригинале. "
    "Не переводи текст на другой язык. "
    "Сохрани смысл, все факты, цифры, цены, названия и призыв к действию — ничего не придумывай. "
    "Пиши как живой человек, автор канала, а не как машина: пост должен цеплять с первой строки "
    "и читаться легко и интересно. Избегай шаблонных оборотов вроде «в современном мире», "
    "«не пропустите», «давайте разберёмся», одинаковых списков и рекламного канцелярита. "
    "Короткие абзацы, живой ритм, 1-3 эмодзи в меру, переносы строк как в посте. "
    "Полностью удали ссылки, юзернеймы, @упоминания и любые упоминания авторов, доноров и исходных каналов. "
    "Уложись примерно в 900 знаков: если оригинал длиннее, сожми его, не теряя фактов. "
    "Выведи только готовый текст поста — без размышлений, без служебных тегов, без пояснений, "
    "заголовков, кавычек и слова «Вот»."
)


def thinking_mode(raw: str) -> str:
    """Как просить MiniMax не рассуждать вслух: по умолчанию «выключено», «adaptive» - по желанию."""
    value = (raw or "").strip().casefold()
    return "adaptive" if value in THINKING_MODES else "disabled"


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


def _ids(name: str) -> list:
    return [int(part) for part in _text(name).replace(";", ",").split(",") if part.strip().lstrip("-").isdigit()]


def _flag(name: str, default: bool = True) -> bool:
    raw = _text(name).casefold()
    if not raw:
        return default
    return raw not in ("0", "off", "no", "false", "нет", "выкл", "выключено")


def parse_views(raw: str, low: int = 1) -> int:
    """Число просмотров из строки пользователя: пробелы и точки не мешают, ноль и потолок - нет.

    Кабинет клиента вызывает с low=0: там «ноль просмотров» - осознанный выбор
    человека («накручивать не надо»), а не ошибка ввода.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        raise ValueError("Напиши одно число - сколько просмотров добавлять, например 1000.")
    value = int(digits)
    if not low <= value <= VIEWS_LIMIT:
        raise ValueError(f"Просмотров должно быть от {low} до {VIEWS_LIMIT} - больше не даёт сервис.")
    return value


def parse_post_limit(raw: str) -> int:
    """Постов в сутки: число от 0 до POSTS_LIMIT. Ноль - не ограничивать."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        raise ValueError("Напиши число постов в сутки, например 10. Ноль - не ограничивать.")
    value = int(digits)
    if value > POSTS_LIMIT:
        raise ValueError(f"Лимит от 0 до {POSTS_LIMIT} постов в сутки. Ноль - не ограничивать.")
    return value


def parse_reactions_range(raw: str, low: int = 10, high: int = 50):
    """Реакции на пост: число («20») или диапазон («10-30») от low до high; для одного числа «до» - None."""
    try:
        number, top = parse_views_range(raw, low=1)
    except ValueError:
        raise ValueError(f"Напиши число от {low} до {high} или диапазон, например 10-30.") from None
    if number < low or number > high or (top is not None and top > high):
        raise ValueError(f"Реакций на пост от {low} до {high}: меньше {low} сервис не выполняет.")
    return number, top


def parse_views_range(raw: str, low: int = 1):
    """Просмотры из строки пользователя: одно число («144») или диапазон («1200-1600», «1200 до 1600»).

    Возвращает пару (от, до); для одного числа «до» - None. Диапазон начинается
    минимум с 1: ноль остаётся одиночным осознанным «не накручивать».
    """
    text = re.sub(r"[–—]", "-", str(raw or "").strip())
    text = re.sub(r"\s*до\s*", "-", text, flags=re.IGNORECASE)
    chunks = [chunk.strip() for chunk in text.split("-") if chunk.strip()]
    if len(chunks) == 1:
        return parse_views(chunks[0], low=low), None
    if len(chunks) != 2:
        raise ValueError("Напиши одно число или два через черточку, например 1200 или 1200-1600.")
    minimum = parse_views(chunks[0], low=max(low, 1))
    maximum = parse_views(chunks[1], low=1)
    if maximum < minimum:
        raise ValueError("В диапазоне первое число должно быть не больше второго, например 1200-1600.")
    if maximum == minimum:
        return minimum, None
    return minimum, maximum


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
    ai_thinking: str = "disabled"
    rewrite_switch: bool = False
    rewrite_default: bool = False
    ai_timeout: int = 90
    ai_retries: int = 3
    ai_retry_delay: int = 5
    promo_button: str = "Просмотры"
    promo_views: int = 0
    promo_views_max: Optional[int] = None
    promo_delay: int = 3
    reactions_enabled: bool = True
    reactions_interval: int = 300
    reactions_timeout: int = 30
    reactions_email: str = ""
    album_wait: float = 2.0
    cache_size: int = 500
    history_limit: int = 200
    caption_limit: int = field(default=1024)
    bot_token: str = ""
    panel_admins: List[int] = field(default_factory=list)
    database_url: str = ""
    worker_contact: str = ""
    smmplus_key: str = ""
    smmplus_service: int = 84
    smmplus_min: int = 10
    support_url: str = "https://t.me/Anya12965"
    payment_details: str = ""

    @property
    def ai_enabled(self) -> bool:
        """Общий выключатель нейросети: REWRITE=0 - и ни один пост не уходит через MiniMax.

        Кнопка выбора текста при этом не ломается: она по-прежнему хранит выбор
        человека, просто он не действует, пока общая настройка выключена.
        REWRITE=1 включает нейросеть, а где она переписывает текст - решает выбор
        в каждой теме (по умолчанию текст автора, REWRITE_DEFAULT=1 - переписывание).
        """
        return self.rewrite_switch

    @property
    def promo_enabled(self) -> bool:
        return bool(self.promo_bot)

    @property
    def panel_enabled(self) -> bool:
        return bool(self.bot_token)

    @property
    def clients_enabled(self) -> bool:
        """Кабинеты клиентов включаются сами, как только в Railway появляется база."""
        return bool(self.database_url)

    @classmethod
    def load(cls) -> "Config":
        missing = [
            name
            for name in ("API_ID", "API_HASH", "SESSION_STRING")
            if not _text(name)
        ]
        if missing:
            raise ConfigError("Не заданы переменные: " + ", ".join(missing))

        try:
            promo_views, promo_views_max = parse_views_range(_text("PROMO_VIEWS"), low=0)
        except ValueError:
            promo_views, promo_views_max = int(_number("PROMO_VIEWS", 0)), None

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
            ai_thinking=thinking_mode(_text("AI_THINKING")),
            rewrite_switch=_flag("REWRITE", False),
            rewrite_default=_flag("REWRITE_DEFAULT", False),
            ai_timeout=int(_number("AI_TIMEOUT", 90)),
            ai_retries=int(_number("AI_RETRIES", 3)),
            ai_retry_delay=int(_number("AI_RETRY_DELAY", 5)),
            promo_button=_text("PROMO_BUTTON", "Просмотры"),
            promo_views=promo_views,
            promo_views_max=promo_views_max,
            promo_delay=int(_number("PROMO_DELAY", 3)),
            reactions_enabled=_flag("REACTIONS", True),
            reactions_interval=max(60, int(_number("REACTIONS_INTERVAL", 300))),
            reactions_timeout=int(_number("REACTIONS_TIMEOUT", 30)),
            reactions_email=_text("REACTIONS_EMAIL"),
            album_wait=_number("ALBUM_WAIT", 2.0),
            cache_size=int(_number("CACHE_SIZE", 500)),
            history_limit=int(_number("HISTORY_LIMIT", 200)),
            bot_token=_text("BOT_TOKEN"),
            panel_admins=_ids("PANEL_ADMINS"),
            database_url=_text("DATABASE_URL"),
            worker_contact=_text("WORKER_CONTACT"),
            smmplus_key=_text("SMMPLUS_API_KEY"),
            smmplus_service=int(_number("SMMPLUS_REACTIONS_SERVICE", 84)),
            smmplus_min=int(_number("SMMPLUS_REACTIONS_MIN", 10)),
            support_url=_text("SUPPORT_URL") or "https://t.me/Anya12965",
            payment_details=_text("PAYMENT_DETAILS"),
        )

    def describe(self) -> str:
        if self.promo_enabled:
            steps = []
            if self.promo_button:
                steps.append(f"жму «{self.promo_button}»")
            steps.append("пересылаю пост")
            steps.append(
                f"+{self.promo_views}-{self.promo_views_max} просмотров (случайное число)"
                if self.promo_views_max else
                (f"+{self.promo_views} просмотров" if self.promo_views else "число просмотров не задано")
            )
            promo = f"{self.promo_bot}: " + " -> ".join(steps)
        else:
            promo = "выключено"
        reactions = (
            f"включены бесплатно на smmnakrutka.ru (не чаще раза в {self.reactions_interval // 60} мин)"
            if self.reactions_enabled
            else "выключены (REACTIONS=0)"
        )
        group = self.management_chat or "найду сам среди групп с темами"
        panel = "ключ задан - управляй кнопками в своём боте" if self.panel_enabled else "нет BOT_TOKEN - команды в группе"
        clients = (
            "кабинеты включены (база подключена)"
            if self.clients_enabled
            else "кабинетов нет: DATABASE_URL не задан - работает только твоя панель"
        )
        thinking = "размышления вслух выключены" if self.ai_thinking == "disabled" else "размышления вслух включены"
        if not self.ai_enabled:
            ai = "REWRITE=0 - к текстам нейросеть не подключаем вообще"
        elif not self.minimax_api_key:
            ai = "ключа MINIMAX_API_KEY нет - публикуем оригинальный текст"
        else:
            ai = f"{self.ai_model} ({thinking}), попыток: {self.ai_retries}, пауза: {self.ai_retry_delay} сек"
        if not self.ai_enabled:
            rewrite = "везде текст автора, как в источнике (REWRITE=0 - нейросеть не работает ни у одного канала)"
        elif self.rewrite_default:
            rewrite = "новые темы по умолчанию переписывает нейросеть; выбор кнопки в теме сохраняется"
        else:
            rewrite = "по умолчанию текст автора; нейросеть переписывает только там, где это включено кнопкой в теме"
        return (
            f"Группа, где лежит список каналов: {group}\n"
            f"Панель кнопок: {panel}\n"
            f"Клиенты: {clients}\n"
            f"Промо-бот: {promo}\n"
            f"Реакции: {reactions}\n"
            f"Нейросеть: {ai}\n"
            f"Текст поста: {rewrite}"
        )
