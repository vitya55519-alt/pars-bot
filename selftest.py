"""Офлайн-проверка логики бота на фейковом клиенте. Запуск: python selftest.py

Telegram и нейросеть не трогаем: подменяем клиент и aiohttp-сессию и проверяем
сборку списка из тем, маршрутизацию, публикацию по file_id, промо и команды.
"""

import aiohttp
import asyncio
import datetime
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import List

from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChatForwardsRestricted, FloodWait, Unauthorized
from pyrogram.types import InputMediaPhoto, InputMediaVideo
from pyrogram.types import Message as _PyrogramMessage

from ai import AiRewriter
from config import Config
from handlers import ParsBot
from panel import Panel
from registry import Registry, RegistryError, extract_refs
from utils import TEXT_LIMIT, as_utc

CHECKS = []
FAILED = []


def naive_now() -> datetime.datetime:
    """Как Telegram: дата без пояса времени, но это UTC.

    Фейковый клиент обязан вести себя как настоящий: сравнение наивной даты
    сообщения с датой старта с поясом времени роняет живой процесс на Railway.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def check(name, condition, detail=""):
    CHECKS.append(name)
    if condition:
        print(f"  ok  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}  {detail}")


class Photo:
    def __init__(self, file_id, file_unique_id=None):
        self.file_id = file_id
        self.file_unique_id = file_unique_id or file_id


class Video:
    def __init__(self, file_id, file_unique_id=None):
        self.file_id = file_id
        self.file_unique_id = file_unique_id or file_id


class Chat:
    def __init__(self, chat_id, title=None, username=None, chat_type=ChatType.CHANNEL, is_forum=False):
        self.id = chat_id
        self.title = title
        self.username = username
        self.type = chat_type
        self.is_forum = is_forum


class User:
    def __init__(self, user_id, name="Витя"):
        self.id = user_id
        self.first_name = name


class Topic:
    def __init__(self, topic_id, name):
        self.id = topic_id
        self.name = name


class Dialog:
    def __init__(self, chat):
        self.chat = chat


class Button:
    def __init__(self, text):
        self.text = text


class ReplyKeyboard:
    def __init__(self, rows):
        self.keyboard = rows


class InlineKeyboard:
    def __init__(self, rows):
        self.inline_keyboard = rows


class CallbackButton(Button):
    def __init__(self, text, data):
        super().__init__(text)
        self.callback_data = data


class Message(_PyrogramMessage):
    """Настоящий класс Message, но без тяжёлого __init__: тогда фильтры куриграма видят тип."""

    def __init__(self, client, message_id, chat, text=None, caption=None, photo=None,
                 group=None, thread=None, service=False, reply_to=None, author=None, date=None, video=None):
        self.id = message_id
        self.chat = chat
        self.text = text
        self.caption = caption
        self.photo = photo
        self.video = video
        self.media_group_id = group
        self.message_thread_id = thread
        self.service = service
        self.reply_to_message = reply_to
        self.from_user = author or User(111)
        self.reply_markup = None
        self.outgoing = False
        self.date = date if date is not None else naive_now()
        self.client = client

    async def reply(self, text, *args, **kwargs):
        self.client.replies.append((self.chat.id, text, kwargs.get("reply_markup")))
        return Message(self.client, 9000 + len(self.client.replies), self.chat, text=text)

    async def delete(self, *args, **kwargs):
        if self.client.delete_fails:
            raise RuntimeError("не админ")
        self.client.deleted.append(self.id)


class Clickable(Message):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clicks = []

    async def click(self, x=0, y=None, timeout=None, **kwargs):
        self.clicks.append((x, y))
        return None


class Query:
    """Клик по inline-кнопке: data, кто нажал, на каком сообщении."""

    def __init__(self, client, data, user_id, message):
        self.data = data
        self.from_user = User(user_id)
        self.message = message
        self.answers = []

    async def answer(self, text="", show_alert=False, **kwargs):
        self.answers.append((text, show_alert))
        return True


class FakeClient:
    def __init__(self):
        self.chats = {}
        self.usernames = {}
        self.users = {}
        self.topics = {}
        self.group_messages = []
        self.promo_messages = []
        self.histories = {}
        self.members = {}
        self.donor_messages = {}
        self.posted = []
        self.albums = []
        self.forwarded = []
        self.replies = []
        self.deleted = []
        self.sent = []
        self.sent_messages = []
        self.topic_posts = []
        self.edits = []
        self.commands = []
        self.topic_edits = []
        self.topic_deletes = []
        self.titled = []
        self.next_topic = 90
        self.delete_fails = False
        self.flood_left = 0
        self.handlers = []
        self.admins = {111}
        self.owned = None
        self.unjoined = set()
        self.joinings = []
        self.join_fails = set()
        self.join_flood = 0
        self.me = User(111, "Я")
        self.executor = ThreadPoolExecutor(max_workers=2)

    async def get_me(self):
        return self.me

    def add_handler(self, handler, group: int = 0):
        self.handlers.append(handler)

    def add_chat(self, chat):
        self.chats[chat.id] = chat
        if chat.username:
            self.usernames[chat.username.lower()] = chat.id
        return chat

    def add_user(self, user):
        self.users[user.id] = user
        return user

    async def get_chat(self, ref):
        if isinstance(ref, str):
            key = ref.lstrip("@").lower()
            if key in self.usernames:
                chat = self.chats[self.usernames[key]]
                if chat.id in self.unjoined:
                    raise ValueError(f"PeerIdInvalid {ref} - аккаунт в канале не состоит")
                return chat
            raise ValueError(f"PeerIdInvalid {ref}")
        if ref in self.chats:
            return self.chats[ref]
        if ref in self.users:
            return self.users[ref]
        raise ValueError(f"PeerIdInvalid {ref}")

    async def join_chat(self, ref):
        """Подписка на публичный канал: после неё канал перестаёт быть чужим."""
        key = str(ref).lstrip("@").lower()
        chat_id = self.usernames.get(key, ref)
        if chat_id not in self.chats:
            raise ValueError(f"PeerIdInvalid {ref} - канала с такой ссылкой нет")
        if chat_id in self.join_fails:
            raise ValueError("ChannelPrivate")
        if self.join_flood:
            raise FloodWait(self.join_flood)
        self.joinings.append(key)
        self.unjoined.discard(chat_id)
        return SimpleNamespace(chat=self.chats[chat_id])

    async def get_dialogs(self):
        for chat in list(self.chats.values()) + list(self.users.values()):
            yield Dialog(chat)

    async def get_forum_topics(self, group_id):
        for topic in self.topics.get(group_id, []):
            yield topic

    async def get_discussion_replies(self, group_id, message_id, limit=0):
        for message in self.group_messages:
            if message.chat.id == group_id and message.message_thread_id == message_id:
                yield message

    async def get_chat_member(self, chat_id, user_id):
        forced = self.members.get(chat_id)
        chat = self.chats.get(chat_id)
        if forced:
            status, can_send = forced
            return type("Member", (), {"status": status, "can_send_messages": can_send, "chat": chat})()
        if user_id not in self.admins:
            status = ChatMemberStatus.MEMBER
        elif self.owned is None or chat_id in self.owned:
            status = ChatMemberStatus.OWNER
        else:
            status = ChatMemberStatus.ADMINISTRATOR
        return type("Member", (), {"status": status, "chat": chat})()

    async def get_chat_history(self, chat, limit=None):
        chat_id = getattr(chat, "id", chat)
        if chat_id in self.histories:
            newest_first = list(reversed(self.histories[chat_id]))
        else:
            newest_first = list(reversed(self.promo_messages))
        for message in newest_first[: (limit or len(newest_first))]:
            yield message

    async def send_photo(self, chat, file_id, caption=None, **kwargs):
        if self.flood_left > 0:
            self.flood_left -= 1
            raise FloodWait(1)
        if not isinstance(file_id, str):
            raise AssertionError("send_photo получил объект фото - значит файл скачивается с Telegram")
        posted = Message(
            self, 500 + len(self.posted), await self.get_chat(chat), caption=caption, photo=Photo(file_id, self.unique_of(file_id))
        )
        self.posted.append(posted)
        if posted.chat.id in self.histories:
            self.histories[posted.chat.id].append(posted)
        return posted

    async def send_video(self, chat, file_id, caption=None, **kwargs):
        if self.flood_left > 0:
            self.flood_left -= 1
            raise FloodWait(1)
        if not isinstance(file_id, str):
            raise AssertionError("send_video получил объект видео вместо file_id")
        posted = Message(
            self, 500 + len(self.posted), await self.get_chat(chat),
            caption=caption, video=Video(file_id, self.unique_of(file_id)),
        )
        self.posted.append(posted)
        self.histories.setdefault(posted.chat.id, []).append(posted)
        return posted

    def unique_of(self, file_id: str) -> str:
        """Telegram для одного и того же файла отдаёт один file_unique_id - воспроизводим это."""
        for messages in self.histories.values():
            for message in messages:
                media = message.photo or message.video
                if media and media.file_id == file_id:
                    return media.file_unique_id
        return file_id

    async def send_media_group(self, chat, media):
        if not 2 <= len(media) <= 10:
            raise AssertionError("send_media_group требует от 2 до 10 карточек")
        if not all(isinstance(item, (InputMediaPhoto, InputMediaVideo)) and isinstance(item.media, str) for item in media):
            raise AssertionError("в альбоме нужны фото или видео по строковому file_id")
        target = await self.get_chat(chat)
        first_id = 700 + sum(len(album) for album in self.albums)
        posted = [
            Message(
                self, first_id + index, target, caption=item.caption or None, group=f"sent-{len(self.albums)}",
                photo=Photo(item.media, self.unique_of(item.media)) if isinstance(item, InputMediaPhoto) else None,
                video=Video(item.media, self.unique_of(item.media)) if isinstance(item, InputMediaVideo) else None,
            )
            for index, item in enumerate(media)
        ]
        self.albums.append(posted)
        self.histories.setdefault(target.id, []).extend(posted)
        return posted

    async def get_media_group(self, chat_id, message_id):
        for item in self.donor_messages.get(chat_id, []):
            if item.id == message_id:
                group = item.media_group_id
                return [m for m in self.donor_messages[chat_id] if m.media_group_id == group]
        return []

    async def forward_messages(self, chat, chat_id, message_id):
        self.forwarded.append((chat, chat_id, message_id))
        return Message(self, 800 + len(self.forwarded), await self.get_chat(chat))

    async def send_message(self, chat, text, *args, **kwargs):
        self.sent.append((chat, text))
        thread = kwargs.get("message_thread_id")
        self.topic_posts.append((chat, text, thread))
        message = Message(self, 1000 + len(self.sent), await self.get_chat(chat), text=text, thread=thread)
        message.reply_markup = kwargs.get("reply_markup")
        self.sent_messages.append(message)
        if thread:
            self.group_messages.append(message)
        return message

    async def delete_messages(self, chat, message_ids, *args, **kwargs):
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        if self.delete_fails:
            raise RuntimeError("не админ")
        self.deleted += ids
        return len(ids)

    async def create_forum_topic(self, chat_id, name, *args, **kwargs):
        topic = Topic(self.next_topic, name)
        self.next_topic += 1
        self.topics.setdefault(chat_id, []).append(topic)
        return topic

    async def create_supergroup(self, title, description="", is_forum=None, **kwargs):
        self.next_group = getattr(self, "next_group", -1009000)
        chat = Chat(self.next_group, title=title, chat_type=ChatType.SUPERGROUP, is_forum=bool(is_forum))
        self.next_group -= 1
        self.add_chat(chat)
        self.created_groups = getattr(self, "created_groups", [])
        self.created_groups.append(chat)
        return chat

    async def edit_forum_topic(self, chat_id, message_thread_id, name, *args, **kwargs):
        for topic in self.topics.get(chat_id, []):
            if topic.id == message_thread_id:
                topic.name = name
        self.topic_edits.append((message_thread_id, name))
        return True

    async def delete_forum_topic(self, chat_id, message_thread_id, *args, **kwargs):
        self.topics[chat_id] = [topic for topic in self.topics.get(chat_id, []) if topic.id != message_thread_id]
        self.topic_deletes.append(message_thread_id)
        return True

    async def set_chat_title(self, chat_id, title, *args, **kwargs):
        chat = self.chats[chat_id]
        chat.title = title
        self.titled.append((chat_id, title))
        return Message(self, 7000 + len(self.titled), chat, text="группа переименована")

    async def edit_message_text(self, chat, message_id, text, reply_markup=None, **kwargs):
        self.edits.append((chat, message_id, text, reply_markup))
        chat_id = getattr(chat, "id", chat)
        for message in self.group_messages:
            if message.chat.id == chat_id and message.id == message_id:
                message.text = text
        return Message(self, message_id, await self.get_chat(chat), text=text)

    async def set_bot_commands(self, commands, *args, **kwargs):
        self.commands += list(commands)
        return True

    async def get_messages(self, chat, message_id=None):
        if message_id is None:
            return []
        target = await self.get_chat(chat)
        for stored in self.histories.get(target.id, []) + self.donor_messages.get(target.id, []):
            if stored.id == message_id:
                return stored
        return None


class Counter:
    """Как pyrogram.MessageReactions: список реакций с эмодзи и числом."""

    def __init__(self, *pairs):
        self.reactions = [
            type("Reaction", (), {"emoji": emoji, "count": count, "are_tags": False})()
            for emoji, count in pairs
        ]


class Emoji:
    def __init__(self, sign):
        self.emoji = sign


class Allowed:
    """Как pyrogram.ChatReactions: либо «разрешены все», либо список конкретных эмодзи."""

    def __init__(self, all_are_enabled=False, reactions=None):
        self.all_are_enabled = all_are_enabled
        self.allow_custom_emoji = False
        self.reactions = reactions


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append((url, json, data, headers))
        item = self.responses.pop(0) if self.responses else FakeResponse(500, text="нет ответа")
        if isinstance(item, BaseException):
            raise item
        return item


class StubAI:
    def __init__(self, result="переписанный текст"):
        self.result = result
        self.calls = 0

    async def rewrite(self, text):
        self.calls += 1
        return self.result or text


def make_cfg(**kwargs):
    base = dict(
        api_id=1,
        api_hash="h",
        session_string="1s",
        management_chat=None,
        promo_bot="promo_bot",
        minimax_api_key="",
        # тестовые миры живут с включённой нейросетью: почти все проверки механики
        # поста идут через рерайт. Отдельный блок [4в] проверяет REWRITE=0.
        rewrite_default=True,
        ai_retries=3,
        ai_retry_delay=0,
        album_wait=0,
        cache_size=50,
        history_limit=50,
        reactions_enabled=False,
        reactions_interval=0,
        reactions_timeout=5,
    )
    base.update(kwargs)
    return Config(**base)


def build_world(cfg=None):
    """Группа с темами «Спорт» и «Юмор @yumor_ch», каналы и доноры."""
    cfg = cfg or make_cfg()
    client = FakeClient()
    group = client.add_chat(Chat(-10010, title="Панель", chat_type=ChatType.SUPERGROUP, is_forum=True))
    sport = client.add_chat(Chat(-10020, title="Спорт", chat_type=ChatType.CHANNEL))
    yumor = client.add_chat(Chat(-10030, title="Юмор канала", username="yumor_ch", chat_type=ChatType.CHANNEL))
    donor_sport_1 = client.add_chat(Chat(-10040, title="Про спорт", username="donor_sport_1"))
    donor_sport_2 = client.add_chat(Chat(-10050, title="Хоккей", username="donor_sport_2"))
    donor_other = client.add_chat(Chat(-10060, title="Мемы", username="donor_other"))
    stranger = client.add_chat(Chat(-10070, title="Соседний чат", username="stranger"))
    client.add_user(User(7637421472, "Аня"))
    client.add_chat(Chat(-10099, title="promo", username="promo_bot", chat_type=ChatType.BOT))

    client.topics[group.id] = [Topic(21, "Спорт"), Topic(22, "Юмор @yumor_ch")]
    client.group_messages = [
        Message(client, 1, group, text="Вот доноры: @donor_sport_1", thread=21),
        Message(client, 2, group, text="https://t.me/donor_sport_2 и ещё https://t.me/+abcINVITE", thread=21),
        Message(client, 3, group, text="@donor_other", thread=22),
        Message(client, 4, group, text="обычное обсуждение без ссылок", thread=21),
        Message(client, 5, group, text="создание темы", thread=21, service=True),
    ]
    return {
        "client": client,
        "cfg": cfg,
        "group": group,
        "sport": sport,
        "yumor": yumor,
        "donor_sport_1": donor_sport_1,
        "donor_sport_2": donor_sport_2,
        "donor_other": donor_other,
        "stranger": stranger,
    }


async def make_bot(world, ai=None, session=None):
    cfg, client = world["cfg"], world["client"]
    registry = Registry(client, cfg)
    ai = ai or StubAI()
    bot = ParsBot(client, cfg, ai, registry, session or FakeSession([]))
    await bot.setup()
    return bot, registry, ai


async def main():
    await check_registry()
    await check_routing()
    await check_album()
    await check_videos()
    await check_video_restart()
    await check_flood_and_ai()
    await check_ai_clean_answer()
    await check_ai_switched_off()
    await check_commands()
    await check_live_and_delete()
    await check_promo_flow()
    await check_failures()
    await check_panel()
    check_startup_wiring()
    await check_panel_login_flood()
    await check_reactions()
    await check_reaction_measure()
    await check_views()
    await check_legal()
    await check_signature()
    await check_store()
    await check_cabinet()
    await check_cabinet_launch()
    await check_tenants()
    await check_clients_sync()
    await check_money()
    await check_dead_topic()
    await check_diagnose()
    await check_text_posts()
    await check_manual_reaction()
    await check_liveness_signals()

    print(f"\nПроверок: {len(CHECKS)}, неудач: {len(FAILED)}")
    if FAILED:
        print("Не прошло: " + ", ".join(FAILED))
        sys.exit(1)


async def check_registry():
    print("\n[1] Сборка списка из тем группы")
    world = build_world()
    bot, registry, _ = await make_bot(world)

    check("найдена единственная группа с темами", registry.group_id == world["group"].id, str(registry.group_id))
    check("темы разобраны", sorted(registry.categories) == [21, 22], str(sorted(registry.categories)))
    sport = registry.categories[21]
    check(
        "доноры из двух сообщений",
        sorted(sport.donors) == sorted([world["donor_sport_1"].id, world["donor_sport_2"].id]),
        str(sorted(sport.donors)),
    )
    check("ссылка-приглашение проигнорирована", all("+" not in donor.link for donor in sport.donors.values()))
    check("сервисные сообщения не читаем", 5 not in [donor.message_id for donor in sport.donors.values()])
    check("канал по названию темы", sport.target == world["sport"].id and sport.target_name == "Спорт", str(sport.target))
    check("канал по ссылке в названии темы", registry.categories[22].target == world["yumor"].id)
    check(
        "маршруты доноров",
        registry.routes
        == {world["donor_sport_1"].id: 21, world["donor_sport_2"].id: 21, world["donor_other"].id: 22},
        str(registry.routes),
    )
    check("числовой -100 id распознаётся", extract_refs("@Abc -1001234567890")[-1] == -1001234567890)
    check("t.me/c превращается в -100", extract_refs("https://t.me/c/1234567890/5")[0] == -1001234567890)
    check("юзернейм из t.me/s берётся", extract_refs("https://t.me/s/donor_sport_1") == ["donor_sport_1"])


async def check_dead_topic():
    print("\n[17] Тема без целевого канала не глотает чужие посты")
    from registry import RegistryError

    world = build_world()
    client, group = world["client"], world["group"]
    client.topics[group.id].append(Topic(23, "Банк"))
    client.group_messages.append(Message(client, 6, group, text="@donor_sport_1", thread=23))
    client.group_messages.append(Message(client, 7, group, text="@stranger", thread=23))
    bot, registry, _ = await make_bot(world)

    dead = registry.categories[23]
    check("тема без канала помечена", dead.target is None and bool(dead.problem), dead.describe())
    check(
        "общий донор остался в валидной теме",
        registry.routes.get(world["donor_sport_1"].id) == 21,
        str(registry.routes),
    )
    check("донор только битой темы не ловится", world["stranger"].id not in registry.routes, str(registry.routes))

    await bot.on_message(client, Message(client, 41, world["donor_sport_1"], caption="текст", photo=Photo("f41")))
    check(
        "пост ушёл в канал валидной темы",
        len(client.posted) == 1 and client.posted[0].chat.id == world["sport"].id,
        str([(m.chat.id, m.photo.file_id) for m in client.posted]),
    )
    check("сбоев при публикации нет", bot.stats["errors"] == 0, str(bot.stats))

    donor_filter = client.handlers[0].filters
    check(
        "фильтр не берёт пост битой темы",
        not await donor_filter(client, Message(client, 42, world["stranger"], caption="текст", photo=Photo("f42"))),
    )
    check("в канале не появилось мусора", len(client.posted) == 1)

    blocked = False
    try:
        await registry.add_donor(23, "@donor_other")
    except RegistryError:
        blocked = True
    check("донора в тему без канала не добавить", blocked)
    check("кнопка ничего лишнего в группу не записала", 8 not in [m.id for m in client.group_messages])


def ago(minutes: int):
    return naive_now() - datetime.timedelta(minutes=minutes)


async def check_diagnose():
    print("\n[18] 🩺 Проверка доноров и догон пропущенных постов")
    world = build_world()
    client, sport, donor = world["client"], world["sport"], world["donor_sport_1"]
    bot, registry, _ = await make_bot(world)

    client.histories[sport.id] = [
        Message(client, 900, sport, caption="то уже было", photo=Photo("f_old", "u_old"), date=ago(120)),
        Message(client, 905, sport, caption="самый свежий", photo=Photo("f_seen", "u_seen"), date=ago(10)),
    ]
    client.histories[sport.id][0].reactions = Counter(("🔥", 3), ("⭐", 1))
    sport.available_reactions = Allowed(all_are_enabled=True)
    client.histories[donor.id] = [
        Message(client, 901, donor, caption="уже в канале", photo=Photo("f_dup", "u_old"), date=ago(120)),
        Message(client, 902, donor, caption="пропущенный пост", photo=Photo("f_new", "u_new"), date=ago(20)),
        Message(client, 903, donor, text="просто текст без картинки", date=ago(5)),
    ]
    client.histories[world["donor_sport_2"].id] = [Message(client, 904, world["donor_sport_2"], photo=Photo("f_bare", "u_bare"), date=ago(10))]

    report = await bot.diagnose()
    check("проверка называет канал темы", "-> Спорт" in report, report)
    check("проверка считает посты донора", "постов с текстом 3, не дошло 1" in report, report)
    check("проверка меряет свежесть поста", "последний пост 5 мин назад" in report, report)
    check("проверка меряет свежесть без постов", "последнее сообщение 10 мин назад" in report, report)
    check("проверка отвечает про права", "пишем свободно" in report, report)
    check("проверка видит пустого донора", "постов с текстом 0" in report, report)
    check(
        "проверка честно считает непроверяемое",
        "текстовых до запуска не проверить: 1" in report,
        [line for line in report.splitlines() if "не проверить" in line],
    )
    check(
        "проверка считает реакции под постами канала",
        "пост 905: нет · пост 900: 🔥3 ⭐1 (всего 4)" in report,
        [line for line in report.splitlines() if "реакции" in line],
    )
    check("счётчики постов не меняются", (bot.stats["caught"], bot.stats["published"]) == (0, 0), str(bot.stats))

    answer = await bot.catch_up()
    check("догнал ровно пропущенный", "Догнал 1" in answer and len(client.posted) == 1, f"{answer} / {len(client.posted)}")
    check("догнанный пост ушёл в канал темы", client.posted[0].chat.id == sport.id)
    check("догон публикует по file_id", client.posted[0].photo.file_id == "f_new", client.posted[0].photo.file_id)
    check("догнанный уходит в промо", len(client.forwarded) == 1, str(client.forwarded))
    check("второй раз догонять нечего", "Пропущенного нет" in await bot.catch_up())
    check("сбой не появился", bot.stats["errors"] == 0, str(bot.stats))

    client.members[sport.id] = (ChatMemberStatus.MEMBER, True)
    check("не админ - так и пишет", "не админ" in await bot.diagnose())
    client.members[sport.id] = (ChatMemberStatus.ADMINISTRATOR, False)
    check("запрет писать виден", "писать запрещено" in await bot.diagnose())
    client.members[sport.id] = (ChatMemberStatus.BANNED, True)
    check("выгнанного аккаунта видно", "не подписан" in await bot.diagnose())
    del client.members[sport.id]

    check("условие сервиса про реакции видно", "разрешены все реакции" in report, report)
    sport.available_reactions = Allowed(reactions=[Emoji("👍"), Emoji("❤️")])
    check(
        "канал с запретом реакций назван",
        "разрешено только 2 эмодзи (👍 ❤️) - по условию сервиса нужно все" in await bot.diagnose(),
        [line for line in (await bot.diagnose()).splitlines() if "настройки канала" in line],
    )
    del sport.available_reactions
    check("канал без настроек назван честно", "ограничений по реакциям" in await bot.diagnose())

    broken = build_world()
    broken["client"].topics[broken["group"].id].append(Topic(23, "Банк"))
    broken["client"].group_messages.append(Message(broken["client"], 6, broken["group"], text="@donor_sport_1", thread=23))
    bot_broken, _, _ = await make_bot(broken)
    check("тема без канала названа и в проверке", "ловить нечего" in await bot_broken.diagnose())

    cfg = make_cfg(bot_token="900:AAstub")
    desk = build_world(cfg)
    panel_client, panel_group = desk["client"], desk["group"]
    owner = panel_client.add_user(User(777, "Витя"))
    panel_bot, panel_registry, _ = await make_bot(desk)
    panel = Panel(panel_client, cfg, panel_registry, panel_bot)
    panel.setup()
    await panel.start(owner.id, "Фогель")
    panel_client.histories[desk["sport"].id] = []
    panel_client.histories[desk["donor_sport_1"].id] = [
        Message(panel_client, 950, desk["donor_sport_1"], caption="пост", photo=Photo("pf", "pu"), date=ago(3))
    ]

    await panel.on_button(panel_client, Query(panel_client, "check", owner.id, Message(panel_client, 5001, owner, text="x")))
    cells = [button.callback_data for row in panel_client.edits[-1][3].inline_keyboard for button in row]
    check("кнопка «Проверка» отвечает текстом", "🩺 Проверка" in panel_client.edits[-1][2], panel_client.edits[-1][2][:80])
    check("из проверки есть кнопка догона", "catchup" in cells, str(cells))
    await panel.on_button(panel_client, Query(panel_client, "catchup", owner.id, Message(panel_client, 5002, owner, text="x")))
    check("догон из панели публикует", len(panel_client.posted) == 1, str(panel_client.posted))
    check("панель отчиталась о догоне", "Догнал 1" in panel_client.edits[-1][2], panel_client.edits[-1][2][:120])
    main_cells = [button.callback_data for row in panel_client.edits[-1][3].inline_keyboard for button in row]
    check("в меню появилась проверка", "check" in main_cells, str(main_cells))


async def check_text_posts():
    print("\n[20] Посты без картинки: публикуем текстом")
    world = build_world(make_cfg(promo_delay=0))
    bot, registry, _ = await make_bot(world)
    client, sport, donor = world["client"], world["sport"], world["donor_sport_1"]

    def sent_texts():
        return [text for chat, text in client.sent if chat == sport.id]

    seen_kwargs = {}
    original_send = client.send_message

    async def spy(chat, text, *args, **kwargs):
        seen_kwargs.update(kwargs)
        return await original_send(chat, text, *args, **kwargs)

    client.send_message = spy

    await bot.on_message(client, Message(client, 61, donor, text="Расписание матчей на сегодня"))
    check("пост без картинки опубликован", len(sent_texts()) == 1, str(client.sent))
    check("текст поста от нейросети", sent_texts() == ["переписанный текст"], str(sent_texts()))
    check("картинку выдумывать не стали", not client.posted, str(client.posted))
    check("разметку в текст не подмешиваем", seen_kwargs.get("parse_mode") is ParseMode.DISABLED, str(seen_kwargs))
    check("промо получило текстовый пост", client.forwarded[0][1] == sport.id, str(client.forwarded))
    check("счётчики текстового поста", (bot.stats["caught"], bot.stats["published"], bot.stats["errors"]) == (1, 1, 0), str(bot.stats))

    client.histories[donor.id] = [Message(client, 61, donor, text="Расписание матчей на сегодня", date=ago(1))]
    answer = await bot.catch_up()
    check("отправленный текстовый пост догон не повторяет", "Пропущенного нет" in answer and len(sent_texts()) == 1, answer)

    bot.started = ago(60)
    client.histories[donor.id] = [Message(client, 62, donor, text="Таблица трансферов", date=ago(5))]
    check("текстовый пост после старта догоняется", "Догнал 1" in await bot.catch_up(), answer)
    check("догнанный текст ушёл в канал", len(sent_texts()) == 2, str(sent_texts()))

    bot.started = datetime.datetime.now(datetime.timezone.utc)
    client.histories[donor.id].append(Message(client, 63, donor, text="Пост до нашего запуска", date=ago(40)))
    check("текстовый пост до запуска дублем не выгоняет", "Пропущенного нет" in await bot.catch_up(), str(sent_texts()))
    check("проверка о нём честно говорит", "текстовых до запуска не проверить: 1" in await bot.diagnose(), await bot.diagnose())

    clip = Message(client, 64, donor, caption="Обзор тура", video=Video("tour-video"))
    await bot.on_message(client, clip)
    check("видео копируется по file_id с переписанной подписью",
          len(client.posted) == 1 and client.posted[0].video
          and client.posted[0].video.file_id == "tour-video"
          and client.posted[0].caption == "переписанный текст", str(bot.stats))
    check("подпись видео не дублируется текстовым постом", len(sent_texts()) == 2, str(sent_texts()))

    document = Message(client, 71, donor, caption="Документ с обзором тура")
    document.document = SimpleNamespace(file_id="tour-document")
    await bot.on_message(client, document)
    check("документ по-прежнему публикует только свой текст",
          len(sent_texts()) == 3 and sent_texts()[-1] == "переписанный текст", str(sent_texts()))
    check("документ не попал в фото, видео или альбом",
          len(client.posted) == 1 and not client.albums and bot.stats["errors"] == 0, str(bot.stats))

    client.send_message = original_send

    deep = build_world(make_cfg(promo_delay=0))
    deep_bot, _, _ = await make_bot(deep, ai=StubAI("слово " * 400))
    await deep_bot.on_message(deep["client"], Message(deep["client"], 65, deep["donor_sport_1"], text="длинный пост"))
    published = [text for chat, text in deep["client"].sent if chat == deep["sport"].id][0]
    check("лимит подписи картинки тексту не мешает", len(published) > 1024, str(len(published)))

    deep_bot.ai = StubAI("слово " * 2000)
    await deep_bot.on_message(deep["client"], Message(deep["client"], 66, deep["donor_sport_1"], text="очень длинный пост"))
    over = [text for chat, text in deep["client"].sent if chat == deep["sport"].id][-1]
    check("длиннее лимита сообщения не пишем", len(over) <= TEXT_LIMIT, str(len(over)))

    fans = client.add_chat(Chat(-10080, title="Обсуждение спорта", username="fan_chat", chat_type=ChatType.SUPERGROUP))
    await registry.add_donor(21, "@fan_chat")
    await bot.on_message(client, Message(client, 77, fans, text="а кто вратаря купил?"))
    check("реплика из группы-донора в канал не идёт", len(sent_texts()) == 3, str(client.sent))
    check("переписку не считаем постом", bot.stats["caught"] == 3, str(bot.stats))

    announcement = Message(client, 78, fans, text="Официально: матч перенесён")
    announcement.sender_chat = fans
    await bot.on_message(client, announcement)
    check("пост группы-донора публикуется текстом", len(sent_texts()) == 4, str(sent_texts()))


async def check_manual_reaction():
    print("\n[21] 🎯 Ручной заказ реакций по ссылке из панели")
    import time

    from reactions import Entry, Reactor, short, wanted_link

    def permit(key="kluch"):
        return FakeResponse(200, text='{"data": "%s"}' % key)

    cfg = make_cfg(bot_token="900:AAstub", reactions_enabled=True)
    world = build_world(cfg)
    client, yumor = world["client"], world["yumor"]
    session = FakeSession([permit(), FakeResponse(200, text="OK"), permit(), FakeResponse(200, text="OK")])
    bot, registry, _ = await make_bot(world, session=session)
    reactor = bot.reactions
    reactor.wait = reactor.poll = 0
    reactor.permit_limit = 0.01
    reactor.check_delays = ()

    standing = Message(client, 77, yumor, caption="текст", photo=Photo("old1"))
    client.histories[yumor.id] = [standing]

    owner = client.add_user(User(777, "Витя"))
    panel = Panel(client, cfg, registry, bot)
    panel.setup()
    await panel.start(owner.id, "Витя")

    def press(data):
        return Query(client, data, owner.id, Message(client, 9100, owner, text="меню"))

    check("ссылка разворачивается в канал и номер", wanted_link("t.me/proprime_chan/896") == ("proprime_chan", 896))
    check("пустая очередь названа", "заказов никто не ждёт" in reactor.queue_lines()[0], str(reactor.queue_lines()))

    await panel.on_message(client, Message(client, 9101, owner, text="/start"))
    cells = [button.callback_data for row in client.replies[-1][2].inline_keyboard for button in row]
    check("в меню есть кнопка ручного заказа", "react" in cells, str(cells))

    await panel.on_button(client, press("react"))
    body = client.edits[-1][2]
    check("кнопка просит ссылку на пост", "Пришли ссылку на пост" in body, body[:120])
    check("экран показывает очередь", "Ждут заказа" in body and "заказов никто не ждёт" in body, body[-120:])

    await panel.on_message(client, Message(client, 9102, owner, text="https://t.me/yumor_ch/77"))
    body = client.replies[-1][1]
    check("ссылка принята в очередь", "yumor_ch/77" in body and "поставил в очередь" in body, body[:160])
    check("заказ помечен ручным", any(item.manual for item in reactor._queue.values()), str(list(reactor._queue.items())))
    await asyncio.sleep(0.2)
    check("ручной заказ ушёл в сервис", reactor.ordered == 1 and reactor.failed == 0, f"ручной заказ пропущен: {reactor.last_error}")
    link = session.calls[1][2]["link"] if len(session.calls) > 1 else ""
    check("сервис получил ссылку из кнопки", link == "https://t.me/yumor_ch/77?single", str(link))
    told = [text for _, text in client.sent if "реакц" in text.lower()]
    check("о результате написали в чат панели", any("Заказ реакций ушёл" in text for text in told), str(client.sent[-1:]))
    check("в докладе есть ответ сервиса", any("сервис ответил «OK»" in text for text in told), str(told[:1]))

    # окно сервиса занято прошлым заказом: новый стоит в очереди, тогда видно повтор
    reactor._next_slot = time.monotonic() + 3600
    await panel.on_button(client, press("react"))
    await panel.on_message(client, Message(client, 9103, owner, text="t.me/yumor_ch/77"))
    body = client.replies[-1][1]
    check("повторный заказ встал в очередь", "поставил в очередь" in body and len(reactor._queue) == 1, body[:160])
    await panel.on_button(client, press("react"))
    await panel.on_message(client, Message(client, 9104, owner, text="https://t.me/yumor_ch/77"))
    body = client.replies[-1][1]
    check("повтор той же ссылки не плодит заказ", "уже ждёт" in body and len(reactor._queue) == 1, str(list(reactor._queue.items())))

    newer = Message(client, 90, yumor, caption="текст", photo=Photo("new1"))
    reactor.schedule(newer)
    check("ручной заказ новым постом не вытесняется", short(reactor._queue["https://t.me/yumor_ch/77?single"].link) == "yumor_ch/77", str(list(reactor._queue.items())))
    check("ручной заказ идёт первым", reactor._next_key() == "https://t.me/yumor_ch/77?single", reactor._next_key())
    check("уже заказанный вручную пост не встаёт вторым", reactor.schedule(standing) is None and len(reactor._queue) == 2, str(list(reactor._queue.items())))
    check("очередь видна целиком", len(reactor.queue_lines()) == 2, str(reactor.queue_lines()))
    check("статус считает ручные заказы", "ручных 1" in reactor.describe(), reactor.describe())

    async def refuses(raw, actor=reactor):
        try:
            await actor.request(raw)
        except ValueError as exc:
            return str(exc)
        return ""

    check("текст без ссылки отклонён", "Не вижу ссылки" in await refuses("привет"), await refuses("привет"))
    check(
        "ссылка закрытого канала отклонена",
        "закрытого канала" in await refuses("https://t.me/c/123456/77"),
        await refuses("https://t.me/c/123456/77"),
    )
    check("несуществующий пост отклонён", "Поста 999" in await refuses("https://t.me/yumor_ch/999"), await refuses("https://t.me/yumor_ch/999"))
    check(
        "канал, который аккаунт не видит, отклонён",
        "не открылся" in await refuses("https://t.me/no_such_channel/5"),
        await refuses("https://t.me/no_such_channel/5"),
    )
    check("при отказе очередь не тронулась", len(reactor._queue) == 2, str(list(reactor._queue.items())))

    off = Reactor(make_cfg(reactions_enabled=False), FakeSession([]), client)
    off_text = await refuses("https://t.me/yumor_ch/77", off)
    check("при выключенных реакциях заказ не принимается", "Реакции выключены" in off_text, off_text)

    await panel.on_button(client, press("react"))
    await panel.on_message(client, Message(client, 9105, owner, text="привет"))
    check("панель после отказа снова просит ссылку", "Пришли ссылку на пост" in client.replies[-1][1], client.replies[-1][1][:120])

    reactor._worker.cancel()
    # пока ждали окна сервиса, пост переехал из порядка в ручные: заказ не должен потеряться
    reactor._queue.clear()
    reactor._queue["https://t.me/yumor_ch/77?single"] = Entry("https://t.me/yumor_ch/77?single", manual=True)
    taken = reactor._take("yumor_ch")
    check("продвинутый вручную заказ не теряется", taken is not None and taken.manual, str(taken))
    reactor._queue.clear()
    check("пустая очередь в докладе панели", "заказов никто не ждёт" in reactor.report(), reactor.report()[:160])


async def check_routing():
    print("\n[2] Публикация поста в канал темы")
    world = build_world()
    bot, registry, ai = await make_bot(world)
    client = world["client"]
    post = Message(client, 31, world["donor_sport_1"], caption="Крутая реклама @author http://t.me/x", photo=Photo("file_id_1"))

    await bot.on_message(client, post)
    check("опубликован один пост", len(client.posted) == 1, str(len(client.posted)))
    check("в канал темы «Спорт»", client.posted[0].chat.id == world["sport"].id)
    check("по file_id без скачивания", client.posted[0].photo.file_id == "file_id_1")
    check("текст от нейросети", client.posted[0].caption == "переписанный текст", client.posted[0].caption)
    check("промо: переслано", client.forwarded == [("promo_bot", world["sport"].id, client.posted[0].id)], str(client.forwarded))
    check("статистика", (bot.stats["caught"], bot.stats["published"], bot.stats["forwarded"]) == (1, 1, 1), str(bot.stats))
    check("время последней публикации записано", bool(bot.last_publish), bot.last_publish)

    donor_filter = client.handlers[0].filters
    check(
        "фильтр пропускает фото донора",
        await donor_filter(client, Message(client, 35, world["donor_sport_1"], caption="текст", photo=Photo("f5"))),
    )
    check(
        "фильтр отсекает чужой чат",
        not await donor_filter(client, Message(client, 36, world["stranger"], caption="текст", photo=Photo("f6"))),
    )
    check(
        "фильтр берёт пост без картинки",
        await donor_filter(client, Message(client, 37, world["donor_sport_1"], text="просто текст")),
    )
    check(
        "фильтр берёт подпись без картинки",
        await donor_filter(client, Message(client, 371, world["donor_sport_1"], caption="текст")),
    )
    check(
        "картинку без текста фильтр берёт, но публикация её отсекает",
        await donor_filter(client, Message(client, 372, world["donor_sport_1"], photo=Photo("f8"))),
    )
    check(
        "сервисное сообщение не пост",
        not await donor_filter(client, Message(client, 373, world["donor_sport_1"], text="вошёл", service=True)),
    )
    mine = Message(client, 38, world["donor_sport_1"], caption="текст", photo=Photo("f7"))
    mine.outgoing = True
    check("фильтр отсекает наши собственные сообщения", not await donor_filter(client, mine))
    check("обработчики повешены: посты + группа + удаления", len(client.handlers) == 3, str(len(client.handlers)))

    await bot.on_message(client, Message(client, 32, world["stranger"], caption="чужое", photo=Photo("f2")))
    check("фото из чужого чата игнорируется", len(client.posted) == 1 and bot.stats["caught"] == 1)

    await bot.on_message(client, Message(client, 33, world["donor_sport_1"], caption="", photo=Photo("f3")))
    await bot.on_message(client, Message(client, 34, world["donor_sport_1"], caption=None, photo=Photo("f4")))
    check("пост без текста пропущен", len(client.posted) == 1)


async def check_album():
    print("\n[3] Альбом: одна обработка, текст на первой карточке")
    world = build_world()
    bot, registry, ai = await make_bot(world)
    client = world["client"]
    donor = world["donor_sport_1"]
    cards = [
        Message(client, 41, donor, photo=Photo("a1"), group="g1"),
        Message(client, 42, donor, photo=Photo("a2"), group="g1", caption="исходный текст альбома"),
        Message(client, 43, donor, photo=Photo("a3"), group="g1"),
    ]
    client.donor_messages[donor.id] = cards

    await bot.on_message(client, cards[0])
    await bot.on_message(client, cards[2])
    await bot.on_message(client, cards[1])

    check("альбом опубликован один раз", len(client.albums) == 1, str(len(client.albums)))
    posted = client.albums[0]
    check("все карточки на месте", len(posted) == 3, str(len(posted)))
    check(
        "подпись только на первой карточке",
        posted[0].caption == "переписанный текст" and not posted[1].caption and not posted[2].caption,
        str([item.caption for item in posted]),
    )
    check("нейросеть вызвана один раз", ai.calls == 1, str(ai.calls))
    check("карточки по file_id", [item.photo.file_id for item in posted] == ["a1", "a2", "a3"])
    check("альбом ушёл в канал темы", posted[0].chat.id == world["sport"].id)
    check("промо по карточке с текстом", client.forwarded[0][2] == posted[0].id, str(client.forwarded))


async def check_videos():
    print("\n[3б] Видео и смешанные альбомы сохраняют носители и подписи")
    world = build_world(make_cfg(promo_delay=0))
    bot, registry, ai = await make_bot(world)
    client, donor, target = world["client"], world["donor_sport_1"], world["sport"]
    donor_filter = client.handlers[0].filters

    bare = Message(client, 101, donor, video=Video("bare-video"))
    check("настоящий фильтр обработчика берёт видео без подписи", await donor_filter(client, bare))
    outgoing = Message(client, 102, donor, video=Video("outgoing-video"))
    outgoing.outgoing = True
    check("фильтр отсекает исходящее видео без подписи", not await donor_filter(client, outgoing))
    service = Message(client, 103, donor, video=Video("service-video"), service=True)
    check("фильтр отсекает сервисное видео", not await donor_filter(client, service))
    stranger = Message(client, 104, world["stranger"], video=Video("stranger-video"))
    check("фильтр не берёт видео чужого канала", not await donor_filter(client, stranger))
    for kind in ("animation", "document", "video_note"):
        unsupported = Message(client, 105, donor)
        setattr(unsupported, kind, SimpleNamespace(file_id=f"unsupported-{kind}"))
        check(f"{kind} без подписи не считается обычным видео", not await donor_filter(client, unsupported))

    fans = client.add_chat(Chat(-10085, title="Обсуждение видео", username="video_fans", chat_type=ChatType.SUPERGROUP))
    await registry.add_donor(21, "@video_fans")
    reply = Message(client, 106, fans, video=Video("reply-video"), reply_to=Message(client, 100, fans, text="обсуждение"))
    # Проверяем зарегистрированную цепочку: фильтр, затем защита от переписки в on_message.
    if await donor_filter(client, reply):
        await bot.on_message(client, reply)
    check("групповая реплика с видео не публикуется", not client.posted and bot.stats["caught"] == 0)

    await registry.toggle_category_rewrite(21)
    await bot.on_message(client, Message(client, 107, donor, video=Video("author-video"), caption="Обзор матча: 2:1"))
    check("выключенный рерайт сохраняет подпись видео",
          len(client.posted) == 1 and client.posted[0].video
          and client.posted[0].video.file_id == "author-video"
          and client.posted[0].caption == "Обзор матча: 2:1" and ai.calls == 0)
    await registry.toggle_category_rewrite(21)

    before = len(client.posted)
    await bot.on_message(client, bare)
    await bot.on_message(client, Message(client, 108, donor, video=Video("empty-caption-video"), caption=""))
    check("видео с None и пустой подписью публикуются без ИИ",
          len(client.posted) == before + 2 and ai.calls == 0
          and all(item.video and not item.caption and item.chat.id == target.id for item in client.posted[before:]))

    def media_ids(items):
        return [("video", item.video.file_id) if item.video else ("photo", item.photo.file_id) for item in items]

    for group, cards, expected in (
        ("mixed-video", [
            Message(client, 111, donor, video=Video("mix-v1"), group="mixed-video"),
            Message(client, 112, donor, photo=Photo("mix-p1"), group="mixed-video", caption="Подпись второй карточки"),
            Message(client, 113, donor, video=Video("mix-v2"), group="mixed-video"),
        ], [("video", "mix-v1"), ("photo", "mix-p1"), ("video", "mix-v2")]),
        ("video-only", [
            Message(client, 121, donor, video=Video("only-v1"), group="video-only"),
            Message(client, 122, donor, video=Video("only-v2"), group="video-only", caption="Вторая часть обзора"),
        ], [("video", "only-v1"), ("video", "only-v2")]),
    ):
        client.donor_messages[donor.id] = cards
        albums_before, calls_before, promo_before = len(client.albums), ai.calls, len(client.forwarded)
        for card in (cards[0], cards[-1], cards[1], cards[0]):
            await bot.on_message(client, card)
        check(f"{group}: повторные карточки не дублируют альбом", len(client.albums) == albums_before + 1)
        posted = client.albums[-1] if len(client.albums) > albums_before else []
        check(f"{group}: все фото и видео сохранены по порядку", media_ids(posted) == expected, str(media_ids(posted)))
        check(f"{group}: подпись второй карточки перенесена только на первую",
              [item.caption or "" for item in posted] == ["переписанный текст"] + [""] * (len(cards) - 1))
        check(f"{group}: рерайт и промо ровно один раз",
              ai.calls == calls_before + 1 and len(client.forwarded) == promo_before + 1
              and posted and client.forwarded[-1][2] == posted[0].id)

    calls_before, albums_before = ai.calls, len(client.albums)
    for group, cards, expected in (
        ("bare-mixed", [
            Message(client, 131, donor, photo=Photo("bare-photo"), group="bare-mixed"),
            Message(client, 132, donor, video=Video("bare-mixed-video"), group="bare-mixed"),
        ], [("photo", "bare-photo"), ("video", "bare-mixed-video")]),
        ("bare-videos", [
            Message(client, 133, donor, video=Video("bare-only-v1"), group="bare-videos"),
            Message(client, 134, donor, video=Video("bare-only-v2"), group="bare-videos"),
        ], [("video", "bare-only-v1"), ("video", "bare-only-v2")]),
    ):
        client.donor_messages[donor.id] = cards
        before = len(client.albums)
        for card in cards:
            await bot.on_message(client, card)
        posted = client.albums[-1] if len(client.albums) > before else []
        check(f"{group}: альбом без текста сохраняет все носители",
              len(client.albums) == before + 1 and media_ids(posted) == expected and all(not item.caption for item in posted))
    check("альбомы с видео без текста не вызывают ИИ", ai.calls == calls_before and len(client.albums) == albums_before + 2)

    before = len(client.posted)
    albums_before, calls_before = len(client.albums), ai.calls
    document = Message(client, 141, donor, group="one-video", caption="Подпись со второй карточки")
    document.document = SimpleNamespace(file_id="album-document")
    cards = [Message(client, 140, donor, video=Video("one-supported-video"), group="one-video"), document]
    client.donor_messages[donor.id] = cards
    # Событие неподдержанной карточки не должно определять тип итогового поста.
    await bot.on_message(client, cards[1])
    await bot.on_message(client, cards[0])
    check("одно поддержанное видео альбома отправляется через send_video",
          len(client.posted) == before + 1 and len(client.albums) == albums_before
          and client.posted[-1].video and client.posted[-1].video.file_id == "one-supported-video")
    check("единственное видео получает подпись альбома один раз",
          len(client.posted) == before + 1 and client.posted[-1].caption == "переписанный текст" and ai.calls == calls_before + 1)

    before, albums_before, calls_before = len(client.posted), len(client.albums), ai.calls
    cards = [Message(client, 151 + index, donor, photo=Photo(f"bare-photo-{index}"), group="bare-photos") for index in range(2)]
    client.donor_messages[donor.id] = cards
    for card in cards:
        await bot.on_message(client, card)
    await bot.on_message(client, Message(client, 153, donor, photo=Photo("bare-single-photo")))
    check("фото и фотоальбом без подписи по-прежнему пропускаются",
          len(client.posted) == before and len(client.albums) == albums_before and ai.calls == calls_before)
    check("видео и альбомы не создают текстовых дублей", not [text for chat, text in client.sent if chat == target.id])
    check("видеопубликации не вызвали ошибок", bot.stats["errors"] == 0, str(bot.stats))


async def check_video_restart():
    print("\n[3в] Видео без подписи узнаётся после перезапуска по file_unique_id")
    world = build_world(make_cfg(promo_delay=0))
    client, donor, target = world["client"], world["donor_sport_1"], world["sport"]
    source = Message(client, 161, donor, video=Video("source-video-id", "stable-video-id"), date=ago(120))
    client.histories[donor.id] = [source]
    client.histories[target.id] = []
    bot, _, ai = await make_bot(world)
    await bot.on_message(client, source)
    check("видео записано в историю с исходным file_unique_id",
          len(client.histories[target.id]) == 1 and client.histories[target.id][0].video
          and client.histories[target.id][0].video.file_unique_id == "stable-video-id")

    delivered = Message(client, 161, donor, video=Video("refreshed-video-id", "stable-video-id"), date=ago(120))
    missed = Message(client, 162, donor, video=Video("missed-video-id", "missed-unique-id"), date=ago(90))
    client.histories[donor.id] = [delivered, missed]
    restarted, registry, restart_ai = await make_bot(world)
    published = await restarted._published_ids(registry.categories[21], 50)
    check("после перезапуска индекс содержит video.file_unique_id", "stable-video-id" in published, str(published))
    check("смена file_id не дублирует доставленное видео", restarted._carried(delivered, published))
    check("видео до запуска проверяемо, в отличие от старого текста", not restarted._unverifiable(missed))
    check("догон видит только недоставленное видео без подписи",
          restarted._not_carried([delivered, missed], published) == [missed])
    before = len(client.posted)
    result = await restarted.catch_up()
    check("после перезапуска догон публикует ровно пропущенное видео",
          "Догнал 1" in result and len(client.posted) == before + 1
          and client.posted[-1].video and client.posted[-1].video.file_id == "missed-video-id", result)
    again, _, again_ai = await make_bot(world)
    result = await again.catch_up()
    check("повторный перезапуск не дублирует видео без подписи",
          "Пропущенного нет" in result and len(client.posted) == before + 1, result)
    check("догон видео без подписи обходится без ИИ", ai.calls == restart_ai.calls == again_ai.calls == 0)


async def check_flood_and_ai():
    print("\n[4] FloodWait и отказ нейросети")
    world = build_world()
    bot, registry, _ = await make_bot(world)
    client = world["client"]
    client.flood_left = 2
    await asyncio.wait_for(bot.on_message(client, Message(client, 51, world["donor_sport_1"], caption="текст", photo=Photo("flood_id"))), 10)
    check("FloodWait пройден автоматически", len(client.posted) == 1 and client.flood_left == 0)

    session = FakeSession([
        FakeResponse(500, text="boom"),
        FakeResponse(200, {"base_resp": {"status_code": 1002, "status_msg": "нехорошо"}}),
        FakeResponse(200, {"choices": [{"message": {"content": "готовый текст"}}]}),
    ])
    cfg = make_cfg(minimax_api_key="key")
    world2 = build_world(cfg)
    bot2 = ParsBot(world2["client"], cfg, AiRewriter(cfg, session), Registry(world2["client"], cfg), session)
    await bot2.setup()
    await bot2.on_message(world2["client"], Message(world2["client"], 52, world2["donor_sport_1"], caption="про спорт", photo=Photo("k1")))
    check("нейросеть повторила до успеха", len(session.calls) == 3, str(len(session.calls)))
    check("опубликован ответ нейросети", world2["client"].posted[0].caption == "готовый текст", world2["client"].posted[0].caption)

    dead = FakeSession([FakeResponse(502, text="") for _ in range(5)])
    cfg3 = make_cfg(minimax_api_key="key")
    world3 = build_world(cfg3)
    bot3 = ParsBot(world3["client"], cfg3, AiRewriter(cfg3, dead), Registry(world3["client"], cfg3), dead)
    await bot3.setup()
    long_text = "слово " * 400
    await bot3.on_message(world3["client"], Message(world3["client"], 53, world3["donor_sport_1"], caption=long_text, photo=Photo("k2")))
    published = world3["client"].posted[0].caption
    check("после 3 осечек публируем оригинал", published.startswith("слово"), published[:20])
    check(
        "подпись урезана до 1024 по границе слова",
        len(published) <= 1024 and len(dead.calls) == 3 and not published.endswith(" "),
        str(len(published)),
    )


    broke = FakeSession([FakeResponse(402, text="insufficient balance (1008)") for _ in range(5)])
    cfg4 = make_cfg(minimax_api_key="key")
    world4 = build_world(cfg4)
    bot4 = ParsBot(world4["client"], cfg4, AiRewriter(cfg4, broke), Registry(world4["client"], cfg4), broke)
    await bot4.setup()
    await bot4.on_message(world4["client"], Message(world4["client"], 54, world4["donor_sport_1"], caption="исходный текст", photo=Photo("k3")))
    check(
        "пустой баланс ИИ: одна попытка и оригинал",
        len(broke.calls) == 1 and world4["client"].posted[0].caption == "исходный текст",
        f"{len(broke.calls)} попыток, {world4['client'].posted[0].caption[:20]}",
    )


async def check_ai_clean_answer():
    print("\n[4б] Ответ нейросети: монолог вырезается, параметр thinking не ломает запрос")
    import logging

    from ai import MAX_OUTPUT, AiRewriter, answered_in_foreign_language, cut, strip_thinking

    monologue = "The user wants a rewritten sports post in Russian. " * 4
    with_tags = f"<think>{monologue}</think>\n\n«Зенит» выиграл"
    check("закрытый монолог вырезается", strip_thinking(with_tags) == "«Зенит» выиграл", strip_thinking(with_tags)[:60])
    check("незакрытый монолог - готового текста нет", strip_thinking(f"<think>{monologue}") == "", strip_thinking(f"<think>{monologue}")[:60])
    check("текст без тегов не портится", strip_thinking("Обычный пост без тегов") == "Обычный пост без тегов")
    check("ответ латиницей отклоняется", answered_in_foreign_language(monologue))
    check("русский пост с латинскими названиями проходит",
          not answered_in_foreign_language("«Зенит» обыграл «Спартак» 2:1 в матче РПЛ"))
    long_answer = cut("пост " * 2000)
    check("длинный ответ не режется до 1024", 1024 < len(long_answer) <= MAX_OUTPUT, str(len(long_answer)))

    cfg = make_cfg(minimax_api_key="key")
    answered = {"choices": [{"message": {"content": with_tags}}]}
    session = FakeSession([FakeResponse(400, text="unsupported parameter: thinking"), FakeResponse(200, answered)])
    rewriter = AiRewriter(cfg, session)
    text = await rewriter.rewrite("исходный текст")
    check("размышления вслух просим выключить", session.calls[0][1].get("thinking") == {"type": "disabled"},
          str(session.calls[0][1].get("thinking")))
    check("адрес без такого параметра - шлём без него",
          session.calls[1][1].get("thinking") is None and rewriter.supports_thinking is False,
          str(session.calls[1][1].get("thinking")))
    check("публикуем русский ответ, а не монолог", text == "«Зенит» выиграл", text[:80])

    only_mono = FakeSession([FakeResponse(200, {"choices": [{"message": {"content": f"<think>{monologue}"}}]}) for _ in range(3)])
    out = await AiRewriter(cfg, only_mono).rewrite("пост автора")
    check("если в ответе только монолог - публикуем оригинал",
          out == "пост автора" and len(only_mono.calls) == 3, out[:60])

    # ==== человек выбирает, переписывать или нет ====
    cfg2 = make_cfg(promo_delay=0)
    world = build_world(cfg2)
    client = world["client"]
    stub = StubAI("переписанный текст")
    bot = ParsBot(client, cfg2, stub, Registry(client, cfg2), FakeSession([]))
    await bot.setup()
    await bot.on_message(client, Message(client, 61, world["donor_sport_1"], caption="текст автора", photo=Photo("w1")))
    check("по умолчанию пост переписывается", stub.calls == 1 and client.posted[0].caption == "переписанный текст",
          client.posted[0].caption)
    await bot.registry.toggle_category_rewrite(21)
    await bot.on_message(client, Message(client, 62, world["donor_sport_1"], caption="второй текст автора", photo=Photo("w2")))
    check("канал с выключенным переписыванием ждёт оригинал",
          stub.calls == 1 and client.posted[1].caption == "второй текст автора", f"{stub.calls} / {client.posted[1].caption}")

    # ==== отчёт на старте: что аккаунт видит в каждом канале ====
    client.histories[world["donor_sport_1"].id] = [
        Message(client, 91, world["donor_sport_1"], text="пост с текстом", date=ago(7))
    ]
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Grab()
    pars_log = logging.getLogger("pars")
    old_level = pars_log.level
    pars_log.addHandler(handler)
    pars_log.setLevel(logging.INFO)
    original_history = bot._history

    async def flaky(chat, limit=50):
        if getattr(chat, "id", chat) == world["donor_sport_2"].id:
            raise Unauthorized("NO_CHAT_MEMBER")
        return await original_history(chat, limit)

    bot._history = flaky
    try:
        await bot.setup()
    finally:
        bot._history = original_history
        pars_log.setLevel(old_level)
        pars_log.removeHandler(handler)
    report = "\n".join(lines)
    check("на старте видно каждый канал-источник", "Источник @donor_sport_1 -> постим в Спорт:" in report, report[:200])
    check("и почему канал молчит", "не открылся (Unauthorized)" in report, [line for line in lines if "donor_sport_2" in line][:2])
    check("в отчёте есть возраст поста", "7 мин назад" in report, [line for line in lines if "donor_sport_1" in line][:2])

    # ==== даты: Telegram присылает их без пояса времени ====
    plain = datetime.datetime(2026, 11, 18, 9, 0)
    checked = as_utc(plain)
    check("наивную дату считаем utc", checked is not None and checked.tzinfo is datetime.timezone.utc
          and checked.hour == 9, str(checked))
    aware = datetime.datetime(2026, 11, 18, 9, 0, tzinfo=datetime.timezone.utc)
    check("дату с поясом не портим", as_utc(aware) == aware and as_utc(None) is None, str(as_utc(aware)))
    stamp = bot._ago(Message(client, 77, world["donor_sport_1"], text="пост", date=ago(7)))
    check("возраст наивной даты считается", stamp == "7 мин назад", stamp)
    bot.started = datetime.datetime.now(datetime.timezone.utc)
    later = Message(client, 78, world["donor_sport_1"], text="пост после старта",
                    date=naive_now() + datetime.timedelta(minutes=5))
    check("свежий текстовый пост отличим от старого", bot._unverifiable(later) is False, str(bot._unverifiable(later)))
    older = Message(client, 79, world["donor_sport_1"], text="пост до старта",
                    date=naive_now() - datetime.timedelta(minutes=5))
    check("старый текстовый пост дублем не выгоняет", bot._unverifiable(older) is True, str(bot._unverifiable(older)))


async def check_ai_switched_off():
    print("\n[4в] REWRITE=0: рабочая механика «пост -> просмотры -> реакции» без нейросети")
    import logging
    import os

    from ai import AiRewriter
    from cabinet import Cabinet

    # ==== по умолчанию нейросети в боте нет ====
    bare = Config(api_id=1, api_hash="h", session_string="1s")
    check("без REWRITE нейросеть выключена", bare.ai_enabled is False, str(bare.ai_enabled))
    check("и новый канал ждёт текст автора", bare.rewrite_default is False, str(bare.rewrite_default))

    real_env = dict(os.environ)
    try:
        os.environ.update({"API_ID": "1", "API_HASH": "h", "SESSION_STRING": "1s"})
        os.environ.pop("REWRITE", None)
        check("переменной нет - Config.load выключает нейросеть", Config.load().ai_enabled is False)
        os.environ["REWRITE"] = "1"
        check("REWRITE=1 возвращает нейросеть", Config.load().ai_enabled is True)
        os.environ["REWRITE"] = "выкл"
        check("REWRITE=выкл снова выключает", Config.load().ai_enabled is False)
    finally:
        os.environ.clear()
        os.environ.update(real_env)

    # ==== механика без ИИ: публикация, просмотры, реакции ====
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Grab()
    pars_log = logging.getLogger("pars")
    old_level = pars_log.level
    pars_log.addHandler(handler)
    pars_log.setLevel(logging.INFO)

    session = FakeSession([
        FakeResponse(200, text='{"data": "kluch123"}'),
        FakeResponse(200, text="OK"),
        FakeResponse(200, text="OK"),
    ])
    cfg = make_cfg(
        minimax_api_key="ключ",
        rewrite_default=False,
        promo_views=700,
        promo_delay=0,
        reactions_enabled=True,
    )
    world = build_world(cfg)
    client = world["client"]
    stub = StubAI("переписанный текст")
    bot, registry, _ = await make_bot(world, ai=stub, session=session)
    bot.reactions.wait = 0
    bot.reactions.poll = 0
    bot.reactions.permit_limit = 0.01
    try:
        await bot.on_message(
            client, Message(client, 81, world["donor_other"], caption="текст автора", photo=Photo("off1"))
        )
        await asyncio.sleep(0.05)
    finally:
        pars_log.setLevel(old_level)
        pars_log.removeHandler(handler)

    report = "\n".join(lines)
    check("с ключом внутри, но без запросов к ИИ", stub.calls == 0 and "Рерайт завершён" not in report, str(stub.calls))
    check("в канал уходит текст автора", client.posted[0].caption == "текст автора", client.posted[0].caption[:60])
    check("пост встал в нужный канал", client.posted[0].chat.id == world["yumor"].id, str(client.posted[0].chat.id))
    check("журнал объясняет, почему без нейросети", "REWRITE" in report, [line for line in lines if "REWRITE" in line][:1])
    check("просмотры ушли промо-боту", len(client.forwarded) == 1, str(client.forwarded))
    check("реакции заказаны без ИИ", bot.reactions.ordered == 1, bot.reactions.last_error)
    check("сбоев в цепочке нет", bot.stats["errors"] == 0, str(bot.stats["errors"]))

    # ==== выбор человека сохраняется, а слова честные ====
    off_cfg = make_cfg(bot_token="900:AAstub", minimax_api_key="ключ", rewrite_default=False)
    off_world = build_world(off_cfg)
    off_client = off_world["client"]
    off_bot, off_registry, _ = await make_bot(off_world)
    panel = Panel(off_client, off_cfg, off_registry, off_bot)
    check("меню называет общий выключатель", "REWRITE" in panel.ai_line(), panel.ai_line())
    await off_registry.toggle_category_rewrite(21)
    view, _ = panel._category_view(21)
    check("выбор канала сохранён", off_registry.rewrite_for(off_registry.categories[21]) is True,
          str(off_registry.rewrite_for(off_registry.categories[21])))
    check("но экран канала говорит, что ИИ выключен", "REWRITE" in view and "текст автора" in view, view[:300])
    status, _ = panel._status_view()
    check("статус: текст для новых каналов - словами автора", "словами автора" in status, [l for l in status.splitlines() if "Текст" in l][:1])

    quiet = FakeSession([FakeResponse(200, {"choices": [{"message": {"content": "ответ, который не нужен"}}]})])
    out = await AiRewriter(off_cfg, quiet).rewrite("пост автора")
    check("даже прямой вызов ИИ при REWRITE=0 молчит", not quiet.calls and out == "пост автора", str(len(quiet.calls)))

    cab = Cabinet(off_client, off_cfg, MemoryStore())
    tenant = await cab.store.ensure_tenant(5001)
    check("в кабинете выбор клиента описан честно", "нейросеть выключена" in cab._text_line(tenant), cab._text_line(tenant))
    check("кнопка кабинета остаётся переключателем выбора", cab._text_cell(tenant)[0] == "✏️ Текст: переписывает ИИ",
          cab._text_cell(tenant)[0])


async def check_commands():
    print("\n[5] Команды в группе")
    world = build_world()
    bot, registry, _ = await make_bot(world)
    client, group = world["client"], world["group"]
    controls = bot.controls

    await controls.on_group_message(client, Message(client, 60, group, text="/список", thread=21))
    text = client.replies[-1][1]
    check("/список показывает каналы и источники", "Спорт" in text and "@donor_sport_1" in text, text)

    await controls.on_group_message(client, Message(client, 61, group, text="/обнови", thread=21))
    check("/обнови сначала отвечает", "Перечитываю" in client.replies[-2][1], client.replies[-2][1])
    check("/обнови вернул сводку", "Канал «Спорт»" in client.replies[-1][1], client.replies[-1][1])

    await controls.on_group_message(client, Message(client, 62, group, text="/статус", thread=21))
    status = client.replies[-1][1]
    check("/статус с цифрами", "Поймано постов" in status and "Нейросеть" in status, status)

    await controls.on_group_message(client, Message(client, 63, group, text="/пауза", thread=21))
    check("/пауза включает паузу", registry.paused is True)
    await bot.on_message(client, Message(client, 64, world["donor_sport_1"], caption="текст", photo=Photo("p1")))
    check("на паузе не публикуем", not client.posted)
    await controls.on_group_message(client, Message(client, 65, group, text="/старт", thread=21))
    check("/старт снимает паузу", registry.paused is False)

    donor_message = client.group_messages[0]
    await controls.on_group_message(client, Message(client, 66, group, text="/убери", thread=21, reply_to=donor_message))
    check("/убери вычеркнул донора", world["donor_sport_1"].id not in registry.categories[21].donors)
    check("/убери удалил сообщение со ссылкой", donor_message.id in client.deleted, str(client.deleted))
    check("маршрут тоже убран", world["donor_sport_1"].id not in registry.routes)

    client.delete_fails = True
    await controls.on_group_message(client, Message(client, 67, group, text="/убери", thread=22, reply_to=client.group_messages[2]))
    check("без прав на удаление - подсказка", "удали сам" in client.replies[-1][1], client.replies[-1][1])
    client.delete_fails = False

    before = len(client.replies)
    await controls.on_group_message(client, Message(client, 68, group, text="/некоманда", thread=21))
    check("неизвестную команду не обсуждаем", len(client.replies) == before, str(len(client.replies)))

    await controls.on_group_message(client, Message(client, 69, group, text="/список", thread=21, author=User(999)))
    check("команду чужого человека не исполняем", "только владелец" in client.replies[-1][1], client.replies[-1][1])
    await controls.on_group_message(client, Message(client, 71, group, text="@donor_other", thread=21, author=User(999)))
    check("ссылку чужого человека не добавляем", world["donor_other"].id not in registry.categories[21].donors)

    await controls.on_group_message(client, Message(client, 70, group, text="/помощь", thread=21))
    check("/помощь объясняет", "кнопками" in client.replies[-1][1], client.replies[-1][1][:60])


async def check_live_and_delete():
    print("\n[6] Живое добавление ссылки и удаление сообщения")
    world = build_world()
    bot, registry, _ = await make_bot(world)
    client, group = world["client"], world["group"]
    new_donor = client.add_chat(Chat(-10080, title="Бег", username="donor_run"))

    await bot.controls.on_group_message(client, Message(client, 81, group, text="возьми https://t.me/donor_run", thread=21))
    client.group_messages.append(Message(client, 81, group, text="возьми https://t.me/donor_run", thread=21))
    check("новая ссылка сразу стала донором", new_donor.id in registry.categories[21].donors)
    check("появился маршрут", registry.routes.get(new_donor.id) == 21, str(registry.routes))
    await bot.on_message(client, Message(client, 82, new_donor, caption="пробежка", photo=Photo("r1")))
    check("пост нового донора опубликован", client.posted[-1].chat.id == world["sport"].id)

    rebuilds = []
    original_rebuild = registry.rebuild

    async def spy():
        rebuilds.append(1)
        return await original_rebuild()

    registry.rebuild = spy
    await bot.controls.on_deleted(client, [Message(client, 81, group, text="x", thread=21)])
    check("удаление в группе перечитывает список", rebuilds == [1], str(rebuilds))
    check("после перечитки новый донор остался", new_donor.id in registry.categories[21].donors)

    await bot.controls.on_deleted(client, [Message(client, 82, world["donor_sport_1"], text="x")])
    check("удаление в чужом чате игнорируем", rebuilds == [1], str(rebuilds))
    await bot.controls.on_deleted(client, [Message(client, 0, None)])
    check("уведомление без чата не роняет обработчик", rebuilds == [1], str(rebuilds))
    await bot.controls.on_deleted(client, [Message(client, 0, None), Message(client, 85, group, text="x", thread=21)])
    check("такое уведомление не мешает перечитать список", len(rebuilds) == 2, str(rebuilds))
    registry.rebuild = original_rebuild

    added = await registry.note(Message(client, 83, group, text="@stranger", thread=1))
    check("ссылки в General не добавляем", not added)


async def check_promo_flow():
    print("\n[7] Продвижение: Lucky Up (кнопка -> пересылка -> число)")
    cfg = make_cfg(promo_button="Просмотры", promo_views=4100, promo_delay=0)
    world = build_world(cfg)
    client = world["client"]
    bot = ParsBot(client, cfg, StubAI(), Registry(client, cfg), FakeSession([]))
    await bot.setup()

    menu = Clickable(client, 1201, await client.get_chat("promo_bot"), text="Выбери раздел")
    menu.reply_markup = ReplyKeyboard(
        [[Button("👀 Просмотры"), Button("🚀 Авто просмотры")], [Button("⚙️ Управление")], [Button("👤 Мой профиль")]]
    )
    client.promo_messages = [menu]

    await bot.on_message(client, Message(client, 91, world["donor_sport_1"], caption="текст", photo=Photo("m1")))
    done = Clickable(client, 1202, await client.get_chat("promo_bot"), text="Готово!")
    client.promo_messages.append(done)
    await bot.on_message(client, Message(client, 92, world["donor_sport_1"], caption="текст", photo=Photo("m2")))

    check("обычную кнопку не кликает вслепую", menu.clicks == [], str(menu.clicks))
    check("нажата «Просмотры», а не «Авто просмотры»",
          client.sent == [("promo_bot", "👀 Просмотры"), ("promo_bot", "4100"),
                          ("promo_bot", "👀 Просмотры"), ("promo_bot", "4100")], str(client.sent))
    check("объект кнопки в чат не уходит", not any("KeyboardButton" in text for _, text in client.sent), str(client.sent))
    check("посты пересланы промо-боту", [f[0] for f in client.forwarded] == ["promo_bot", "promo_bot"], str(client.forwarded))
    check("промо засчитано обоим постам", bot.stats["forwarded"] == 2, str(bot.stats))

    cfg3 = make_cfg(promo_button="Просмотры", promo_views=700, promo_delay=0)
    world3 = build_world(cfg3)
    client3 = world3["client"]
    bot3 = ParsBot(client3, cfg3, StubAI(), Registry(client3, cfg3), FakeSession([]))
    await bot3.setup()
    menu3 = Clickable(client3, 1301, await client3.get_chat("promo_bot"), text="Выбери раздел")
    menu3.reply_markup = InlineKeyboard([[CallbackButton("👀 Просмотры", "views")], [Button("⚙️ Настройки")]])
    client3.promo_messages = [menu3]
    await bot3.on_message(client3, Message(client3, 94, world3["donor_sport_1"], caption="текст", photo=Photo("m4")))
    check("callback-кнопку жмёт как есть", menu3.clicks == [(0, None)], str(menu3.clicks))
    check("после callback отправлено только число", client3.sent == [("promo_bot", "700")], str(client3.sent))

    cfg2 = make_cfg(promo_button="", promo_views=0, promo_delay=0)
    world2 = build_world(cfg2)
    client2 = world2["client"]
    bot2 = ParsBot(client2, cfg2, StubAI(), Registry(client2, cfg2), FakeSession([]))
    await bot2.setup()
    await bot2.on_message(client2, Message(client2, 93, world2["donor_sport_1"], caption="текст", photo=Photo("m3")))
    check("без кнопки и числа - только пересылка", client2.sent == [] and len(client2.forwarded) == 1, str(client2.sent))


async def check_failures():
    print("\n[8] Отказоустойчивость")
    world = build_world()
    bot, registry, _ = await make_bot(world)
    client = world["client"]

    async def restricted(*args, **kwargs):
        raise ChatForwardsRestricted()

    async def broken(*args, **kwargs):
        raise RuntimeError("внутренняя поломка")

    client.send_photo = restricted
    await bot.on_message(client, Message(client, 101, world["donor_sport_1"], caption="текст", photo=Photo("z1")))
    check("запрет копирования не считается сбоем", bot.stats["published"] == 0 and bot.stats["errors"] == 0, str(bot.stats))

    client.send_photo = broken
    await bot.on_message(client, Message(client, 102, world["donor_sport_1"], caption="текст", photo=Photo("z2")))
    check("любая ошибка поста учтена", bot.stats["errors"] == 1, str(bot.stats))

    no_topics = FakeClient()
    no_topics.add_chat(Chat(-10010, title="Панель", chat_type=ChatType.SUPERGROUP, is_forum=True))
    registry3 = Registry(no_topics, make_cfg())
    await registry3.rebuild()
    check("группа без тем запускается", registry3.ready and registry3.donor_count == 0)
    check(
        "сводка пустой группы понятна",
        "нет ни одного канала" in registry3.summary().lower() and "Добавить канал" in registry3.summary(),
        registry3.summary(),
    )

    plain = FakeClient()
    plain.add_chat(Chat(-10010, title="Обычная группа", chat_type=ChatType.SUPERGROUP, is_forum=False))
    try:
        await Registry(plain, make_cfg()).rebuild()
        check("группа без тем отклонена", False)
    except RegistryError as exc:
        check("группа без тем отклонена", "темы" in str(exc).lower(), str(exc))

    nothing = FakeClient()
    try:
        await Registry(nothing, make_cfg()).rebuild()
        check("без группы - понятная ошибка", False)
    except RegistryError as exc:
        check("без группы - понятная ошибка", "создать группу" in str(exc).lower(), str(exc))

    two_groups = FakeClient()
    two_groups.add_chat(Chat(-10010, title="Раз", chat_type=ChatType.SUPERGROUP, is_forum=True))
    two_groups.add_chat(Chat(-10011, title="Два", chat_type=ChatType.SUPERGROUP, is_forum=True))
    try:
        await Registry(two_groups, make_cfg()).rebuild()
        check("две группы без MANAGEMENT_CHAT - ошибка", False)
    except RegistryError as exc:
        check("две группы без MANAGEMENT_CHAT - ошибка", "MANAGEMENT_CHAT" in str(exc), str(exc))
        check("в ошибке видны id групп", "-10010" in str(exc) and "-10011" in str(exc), str(exc))

    two_groups.owned = {-10011}
    chosen = Registry(two_groups, make_cfg())
    await chosen.rebuild()
    check("своя группа угадывается по владению", chosen.group_id == -10011, str(chosen.group_id))

    broken_ref = FakeClient()
    group = broken_ref.add_chat(Chat(-10010, title="Панель", chat_type=ChatType.SUPERGROUP, is_forum=True))
    broken_ref.add_chat(Chat(-10020, title="Спорт", chat_type=ChatType.CHANNEL))
    broken_ref.add_chat(Chat(-10021, title="Прочее", username="other_ch", chat_type=ChatType.CHANNEL))
    broken_ref.topics[-10010] = [Topic(31, "Спорт"), Topic(32, "Нет такого канала"), Topic(33, "Зеркало")]
    broken_ref.group_messages = [
        Message(broken_ref, 1, group, text="@ghost_channel", thread=31),
        Message(broken_ref, 2, group, text="Куда постить: @other_ch", thread=33),
    ]
    registry6 = Registry(broken_ref, make_cfg())
    await registry6.rebuild()
    check("битая ссылка не рвёт сборку", registry6.ready and registry6.unreadable == ["ghost_channel"], str(registry6.unreadable))
    check("тема без канала помечена", "нет канала" in registry6.categories[32].problem, registry6.categories[32].problem)
    check("замечания видны в /статус", any("нет канала" in line for line in registry6.problems()), str(registry6.problems()))
    check("цель из темы важнее её названия",
          registry6.categories[33].target == -10021 and registry6.categories[33].target_name == "@other_ch",
          registry6.categories[33].describe())
    check("служебное сообщение не стало источником постов", not registry6.categories[33].donors,
          str(registry6.categories[33].donors))


async def check_panel():
    print("\n[9] Панель управления кнопками в боте")
    cfg = make_cfg(bot_token="900:AAstub")
    world = build_world(cfg)
    client = world["client"]
    group = world["group"]
    fitnes = client.add_chat(Chat(-10081, title="Фитнес", chat_type=ChatType.CHANNEL))
    yoga = client.add_chat(Chat(-10082, title="Йога", username="yoga_ch", chat_type=ChatType.CHANNEL))
    new_donor = client.add_chat(Chat(-10080, title="Бег", username="donor_run"))
    owner = client.add_user(User(777, "Витя"))
    stranger = client.add_user(User(888, "Чужой"))

    bot, registry, _ = await make_bot(world)
    panel = Panel(client, cfg, registry, bot)
    panel.setup()
    await panel.start(owner.id, "Фогель")

    rebuilt = []
    original_rebuild = registry.rebuild

    async def spy():
        rebuilt.append(1)
        return await original_rebuild()

    registry.rebuild = spy

    def press(data, user_id=None):
        return Query(client, data, user_id or owner.id, Message(client, 5001, owner, text="x"))

    def last_edit():
        return client.edits[-1][2]

    def last_buttons():
        buttons = client.edits[-1][3]
        return [] if buttons is None else [b.callback_data for row in buttons.inline_keyboard for b in row]

    def last_reply():
        return client.replies[-1]

    check("подсказки команд заданы", len(client.commands) == 2, str(len(client.commands)))
    check("у бота два обработчика", len(client.handlers) == 5, str(len(client.handlers)))

    await panel.on_message(client, Message(client, 5002, owner, text="/start"))
    check("меню знает группу и счётчики", "АвтоПост" in last_reply()[1] and "Твоих каналов: 2" in last_reply()[1], last_reply()[1][:90])
    cells = [button.callback_data for row in last_reply()[2].inline_keyboard for button in row]
    check("в меню есть кнопка списков", "topics" in cells, str(cells))

    await panel.on_message(client, Message(client, 5003, stranger, text="/start"))
    check("чужому доступ закрыт", "только владельца" in last_reply()[1], last_reply()[1][:60])
    wrong = press("topics", stranger.id)
    await panel.on_button(client, wrong)
    check("чужая кнопка не исполняется", client.edits == [] and "Доступ" in wrong.answers[0][0], str(wrong.answers))

    await panel.on_button(client, press("cat|21"))
    check("экран канала показывает, откуда берём посты", "Откуда берём посты" in last_edit() and "@donor_sport_1" in last_edit(), last_edit()[:200])
    check("экран канала называет канал публикации", "Куда постим: Спорт" in last_edit() and "Тема в группе: «Спорт»" in last_edit(), last_edit()[:160])
    check("экран канала показывает текст, просмотры и реакции", "Что делаем с текстом:" in last_edit() and "3. Просмотры:" in last_edit() and "4. Реакции:" in last_edit(), last_edit()[:300])
    check("у канала свои кнопки настроек", "cv|21" in last_buttons() and "cr|21" in last_buttons() and "cw|21" in last_buttons(), str(last_buttons()))
    check("у канала есть кнопка выбора адреса и кнопки названия", "target|21" in last_buttons() and "rename|21" in last_buttons(), str(last_buttons()))
    check("источник отключается кнопкой ✖", "del|21|1" in last_buttons(), str(last_buttons()))
    await panel.on_button(client, press("topics"))
    check("список каналов назван", "Спорт" in last_edit() and "@yumor_ch" in last_edit(), last_edit()[:200])
    check("список каналов считает каналы", "канал(ов)" in last_edit(), last_edit()[:200])
    await panel.on_button(client, press("status"))
    check("статус описывает настройки", "Промо-бот" in last_edit() and "promo_bot" in last_edit(), last_edit()[:90])
    await panel.on_button(client, press("help"))
    check("помощь объясняет кнопки", "Как пользоваться" in last_edit(), last_edit()[:60])

    await panel.on_button(client, press("del|21|1"))
    check("донор убран кнопкой", world["donor_sport_1"].id not in registry.categories[21].donors)
    check("сообщение со ссылкой удалено", 1 in client.deleted, str(client.deleted))
    check("убранное не перечитывает группу", rebuilt == [], str(rebuilt))
    check("экран подтверждает отключение", "Перестал читать" in last_edit(), last_edit()[:80])
    await panel.on_button(client, press("del|21|999"))
    check("уже убранный канал - понятный отказ", "уже убрано" in last_edit(), last_edit()[:80])

    await panel.on_button(client, press("add|21"))
    check("кнопка донора просит ссылку", "Пришли ссылку" in last_edit(), last_edit()[:80])
    await panel.on_message(client, Message(client, 5004, owner, text="https://t.me/donor_run"))
    check("донор добавлен из бота", new_donor.id in registry.categories[21].donors)
    check("маршрут появился", registry.routes.get(new_donor.id) == 21, str(registry.routes))
    check("ссылка записана в тему группы", client.topic_posts[-1] == (group.id, "https://t.me/donor_run", 21), str(client.topic_posts[-1]))
    check("ответ подтверждает добавление", "Добавил каналы, откуда берём посты" in last_reply()[1], last_reply()[1][:80])

    await panel.on_button(client, press("add|21"))
    await panel.on_message(client, Message(client, 5005, owner, text="привет"))
    check("текст без ссылки отклонён", "не ссылка" in last_reply()[1], last_reply()[1][:80])
    await panel.on_button(client, press("add|21"))
    await panel.on_message(client, Message(client, 5006, owner, text="@ghost_channel"))
    check("битая ссылка отклонена", "не открылся" in last_reply()[1], last_reply()[1][:80])
    check("мусорная запись из темы удалена", client.deleted[-1] >= 1000, str(client.deleted[-3:]))

    await panel.on_button(client, press("channel"))
    check("кнопка канала просит название", "Напиши название" in last_edit(), last_edit()[:80])
    await panel.on_message(client, Message(client, 5007, owner, text="Фитнес"))
    fitness_topic = next(id_ for id_, category in registry.categories.items() if category.name == "Фитнес")
    check("тема для нового канала создана", any(topic.id == fitness_topic for topic in client.topics[group.id]))
    check("канал привязан по названию", registry.categories[fitness_topic].target == fitnes.id)
    check("добавление подтверждено", "создана" in last_reply()[1], last_reply()[1][:80])
    await panel.on_button(client, press("channel"))
    await panel.on_message(client, Message(client, 5008, owner, text="Фитнес"))
    check("дубликат канала отклонён", "уже есть" in last_reply()[1], last_reply()[1][:80])

    await panel.on_button(client, press(f"rename|{fitness_topic}"))
    check("кнопка названия обещает не трогать канал", "не изменится" in last_edit(), last_edit()[:140])
    await panel.on_message(client, Message(client, 5009, owner, text="Йога"))
    category = registry.categories[fitness_topic]
    check("переименование темы оставило канал для постов",
          category.name == "Йога" and category.target == fitnes.id, f"{category.name} -> {category.target_name}")
    check("тема переименована в группе", client.topic_edits[-1] == (fitness_topic, "Йога"), str(client.topic_edits))
    check("канал для постов записан в тему",
          any("Куда постить" in text for _chat, text, thread in client.topic_posts if thread == fitness_topic),
          str(client.topic_posts[-3:]))

    await panel.on_button(client, press(f"target|{fitness_topic}"))
    check("кнопка куда постить просит канал", "@юзернейм канала" in last_edit(), last_edit()[:140])
    await panel.on_message(client, Message(client, 5014, owner, text="привет"))
    check("не канал для постов отклонён", "Нужен @юзернейм" in last_reply()[1], last_reply()[1][:120])
    await panel.on_button(client, press(f"target|{fitness_topic}"))
    await panel.on_message(client, Message(client, 5015, owner, text="@ghost_channel"))
    check("неоткрытый канал для постов отклонён", "не подошёл" in last_reply()[1], last_reply()[1][:120])
    check("и прежняя цель не сбросилась", registry.categories[fitness_topic].target == fitnes.id,
          str(registry.categories[fitness_topic].target))
    await panel.on_button(client, press(f"target|{fitness_topic}"))
    await panel.on_message(client, Message(client, 5016, owner, text="@yoga_ch"))
    check("канал для постов задан вручную", registry.categories[fitness_topic].target == yoga.id,
          str(registry.categories[fitness_topic].target))
    check("название темы при этом наше", registry.categories[fitness_topic].name == "Йога",
          registry.categories[fitness_topic].name)

    await panel.on_button(client, press("pause"))
    check("пауза кнопкой", registry.paused is True and "пауза" in last_edit(), last_edit()[:80])
    await bot.on_message(client, Message(client, 5010, world["donor_sport_1"], caption="текст", photo=Photo("pa1")))
    check("на паузе не публикуем", not client.posted)
    await panel.on_button(client, press("start"))
    check("пуск кнопкой", registry.paused is False, last_edit()[:80])

    await panel.on_button(client, press("update"))
    check("обновление перечитывает группу", rebuilt == [1], str(rebuilt))
    check("после обновления виден результат", "Список обновлён" in last_edit(), last_edit()[:90])
    check("добавленный из бота донор пережил перечитку", new_donor.id in registry.categories[21].donors)
    registry.rebuild = original_rebuild

    await panel.on_button(client, press("drop|21"))
    check("убрать канал - сначала спрашивает", "Убрать его целиком" in last_edit() and client.topic_deletes == [], last_edit()[:90])
    await panel.on_button(client, press("cat|21"))
    check("отмена ничего не удалила", client.topic_deletes == [] and 21 in registry.categories, str(client.topic_deletes))
    await panel.on_button(client, press("drop_yes|21"))
    check("канал убран кнопкой", 21 not in registry.categories, str(list(registry.categories)))
    check("тема удалена в группе", client.topic_deletes == [21], str(client.topic_deletes))
    check("доноры убранного канала больше не ловятся",
          registry.route(world["donor_sport_2"].id) is None and registry.route(new_donor.id) is None,
          str(registry.routes))
    check("в списке остался только второй канал", "cat|22" in last_buttons() and "cat|21" not in last_buttons(), str(last_buttons()))

    await panel.on_button(client, press("group_rename"))
    check("кнопка группы просит название", "Напиши новое название группы" in last_edit(), last_edit()[:90])
    await panel.on_message(client, Message(client, 5013, owner, text="АвтоПост"))
    check("склад переименован кнопкой", registry.group_name == "АвтоПост" and group.title == "АвтоПост", registry.group_name)
    check("название записано в Telegram", client.titled[-1] == (group.id, "АвтоПост"), str(client.titled))
    check("переименование подтверждено", "Группа теперь называется" in last_reply()[1], last_reply()[1][:90])
    check("переименование группы не трогает темы каналов", client.topic_edits and client.topic_deletes == [21], str(client.topic_deletes))

    await panel.on_button(client, press("main"))
    check("в меню есть кнопки документов", "doc|1" in last_buttons() and "doc|2" in last_buttons(), str(last_buttons()))
    check("общих кнопок просмотров и реакций больше нет", "views" not in last_buttons() and "reactions" not in last_buttons(), str(last_buttons()))
    sent_before = len(client.sent)
    await panel.on_button(client, press("doc|1"))
    check("соглашение приходит сообщением", len(client.sent) > sent_before and "Пользовательское соглашение" in client.sent[sent_before][1],
          str(client.sent[sent_before:])[:120])
    doc_markup = client.sent_messages[-1].reply_markup
    doc_cells = [] if doc_markup is None else [b.callback_data for row in doc_markup.inline_keyboard for b in row]
    check("у последней страницы документа есть возврат в меню", "main" in doc_cells, str(doc_cells))

    lonely = FakeClient()
    boss = lonely.add_user(User(777, "Витя"))
    lonely_cfg = make_cfg(bot_token="900:AAstub")
    lonely_registry = Registry(lonely, lonely_cfg)
    lonely_bot = ParsBot(lonely, lonely_cfg, StubAI(), lonely_registry, FakeSession([]))
    try:
        await lonely_bot.setup()
    except RegistryError:
        pass
    remote = Panel(lonely, lonely_cfg, lonely_registry, lonely_bot)
    await remote.start(boss.id, "Фогель")
    await remote.on_message(lonely, Message(lonely, 5011, boss, text="/start"))
    check("без группы предлагается создать её кнопкой", "Создать группу" in lonely.replies[-1][1], lonely.replies[-1][1][:120])

    def lonely_press(data):
        msg = Message(lonely, 6001, boss, text="меню")
        return Query(lonely, data, boss.id, msg)

    await remote.on_button(lonely, lonely_press("group"))
    await remote.on_message(lonely, Message(lonely, 5012, boss, text="АвтоПост"))
    check("группа-хранилище создана кнопкой", lonely_registry.ready and lonely_registry.group_name == "АвтоПост", f"{lonely_registry.ready} {lonely_registry.group_name}")
    check("созданная группа - форум", bool(lonely.created_groups) and lonely.created_groups[-1].is_forum, str(lonely.created_groups[-1].is_forum) if lonely.created_groups else "нет")
    check("после создания видно меню", "АвтоПост" in lonely.replies[-1][1], lonely.replies[-1][1][:120])


def check_startup_wiring() -> None:
    print("\n[10] Запуск процесса")
    from pyrogram import Client, idle

    source = open("main.py", encoding="utf-8").read()
    check("у Kurigram нет client.idle", not hasattr(Client, "idle"))
    check("main ждёт через pyrogram.idle", callable(idle) and "await idle()" in source, str(hasattr(idle, "__call__")))
    check("в main не осталось client.idle", "client.idle()" not in source)
    check(
        "панель поднимается фоном, а не блокирует старт",
        "asyncio.create_task(start_panel" in source and "await start_panel(" not in source,
        "await start_panel" in source,
    )
    check("панель не перекрывает остальных", "panel_live" not in source, "panel_live")
    check("ловля постов докладывает о себе сама", "asyncio.create_task(bot.heartbeat())" in source, "heartbeat")
    check(
        "отозванный вход описан словами, а не трейсбеком",
        "except SESSION_ERRORS as exc" in source and "make_session.py --window" in source,
        "SESSION_ERRORS",
    )


async def check_panel_login_flood():
    print("\n[11] Вход бота панели под FloodWait")
    import main as main_module

    waits: List[int] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        waits.append(int(seconds))
        await real_sleep(0)

    class PanelClient:
        def __init__(self, errors):
            self.errors = list(errors)
            self.starts = 0

        async def start(self):
            self.starts += 1
            if self.errors:
                raise self.errors.pop(0)

        async def get_me(self):
            return SimpleNamespace(username="panel_bot", id=777)

    class PanelStub:
        def __init__(self):
            self.started = 0

        async def start(self, owner_id, account_name):
            self.started += 1

    asyncio.sleep = fake_sleep
    try:
        client = PanelClient([FloodWait(2037, "auth.ImportBotAuthorization"), FloodWait(5, "auth.ImportBotAuthorization")])
        panel = PanelStub()
        live = await main_module.start_panel(client, panel, 1, "аккаунт")
        check("панель дождалась пауз и вошла", live and client.starts == 3 and panel.started == 1, f"{live} {client.starts}")
        check("пауза равна тому, что просит Telegram", waits == [2042, 10], str(waits))

        broken = PanelClient([Unauthorized("x")])
        check("плохой BOT_TOKEN не роняет процесс", await main_module.start_panel(broken, PanelStub(), 1, "аккаунт") is False)

        crashed = PanelClient([RuntimeError("что-то не так")])
        check(
            "любая ошибка входа панели - только журнал",
            await main_module.start_panel(crashed, PanelStub(), 1, "аккаунт") is False and crashed.starts == 1,
            str(crashed.starts),
        )

        # ==== вход бота панели живёт фоном: пауза Telegram не держит автопостинг ====
        waits.clear()

        class AlwaysFlood:
            def __init__(self):
                self.starts = 0

            async def start(self):
                self.starts += 1
                raise FloodWait(3000, "auth.ImportBotAuthorization")

        stuck = AlwaysFlood()
        login = asyncio.create_task(main_module.start_panel(stuck, PanelStub(), 1, "аккаунт"))
        caught = []

        async def pipeline():
            await real_sleep(0.05)
            caught.append("пост дошёл")

        await pipeline()
        check("пока бот панели ждёт паузу, посты ловятся", caught == ["пост дошёл"] and not login.done(),
              f"{caught} done={login.done()}")
        login.cancel()
        try:
            await login
        except asyncio.CancelledError:
            pass

        owner_panel = Panel(None, make_cfg(), None, None)
        owner_panel.claim_owner(500, "Фогель")
        check("владелец известен до входа бота панели", owner_panel.owner == 500 and 500 in owner_panel.admins,
              str(sorted(owner_panel.admins)))

    finally:
        asyncio.sleep = real_sleep


async def check_reactions():
    print("\n[12] Бесплатные реакции на посты")
    from reactions import FORM_URL, PERMIT_URL, Reactor, ReactionsError, post_link, short

    client = FakeClient()
    yumor = client.add_chat(Chat(-10030, title="Юмор канала", username="yumor_ch", chat_type=ChatType.CHANNEL))
    secret = client.add_chat(Chat(-10031, title="Закрытый", chat_type=ChatType.CHANNEL))
    published = Message(client, 77, yumor, caption="текст", photo=Photo("r1"))

    def permit(key="kluch123"):
        return FakeResponse(200, text='{"data": "%s"}' % key)

    def reactor(responses, **kwargs):
        cfg = make_cfg(reactions_enabled=True, **kwargs)
        instance = Reactor(cfg, FakeSession(responses))
        instance.wait = 0
        instance.poll = 0
        instance.permit_limit = 0.01
        return instance

    check("ссылка на пост публичного канала", post_link(published) == "https://t.me/yumor_ch/77?single", post_link(published))
    check("у закрытого канала ссылки нет", post_link(Message(client, 78, secret)) == "")

    session = FakeSession([permit(), FakeResponse(200, text="OK")])
    reactor_ = Reactor(make_cfg(reactions_enabled=True), session)
    reactor_.wait = 0
    reactor_.poll = 0
    reactor_.permit_limit = 0.01
    reactor_.schedule(published)
    await asyncio.sleep(0.05)
    check("заказ уходит только через разрешение", reactor_.ordered == 1 and reactor_.failed == 0, reactor_.last_error)
    check(
        "сначала просьба о разрешении, потом форма",
        [call[0] for call in session.calls] == [PERMIT_URL, FORM_URL],
        str([call[0] for call in session.calls]),
    )
    token = session.calls[0][1]["sessionToken"]
    check("сессия случайная, как на сайте", len(token) >= 16 and token.isalnum(), token)
    data = session.calls[1][2]
    check("форма уходит с ключом разрешения", data["permissionKey"] == "kluch123" and data["sessionToken"] == token, str(data["permissionKey"]))
    check("в заказе есть ссылка поста", data["link"] == "https://t.me/yumor_ch/77?single", str(data["link"]))
    check("заказаны именно реакции", data["quantity-text"] == "Реакции Телеграм" and data["service"] == "680", str(data["service"]))
    check("в форме название страницы, а не домен", data["url_page"] == "free-telegram-reactions", str(data["url_page"]))
    check(
        "набор полей как в браузере",
        data["formservices[]"] and data["tildaspec-formid"] == "form510249937" and data["tildaspec-projectid"] == "4412478",
        str(sorted(data)[:5]),
    )
    check("просьба выглядит браузерной", session.calls[1][3]["Referer"].startswith("https://smmnakrutka.ru"), str(session.calls[1][3].get("Referer")))

    patient = reactor([permit(""), permit("kluch2"), FakeResponse(200, text="OK")])
    patient.poll = 0
    patient.permit_limit = 5
    patient.schedule(published)
    await asyncio.sleep(0.1)
    check("пока разрешения нет - ждём и спрашиваем снова", patient.ordered == 1 and len(patient.session.calls) == 3, str(len(patient.session.calls)))

    silent = reactor([permit(""), permit("")])
    silent.permit_limit = 0
    silent.schedule(published)
    await asyncio.sleep(0.05)
    check(
        "без разрешения заказа не будет",
        silent.failed == 1 and silent.ordered == 0 and "не дал разрешение" in silent.last_error,
        silent.last_error,
    )

    broken = reactor([permit(), FakeResponse(502, text="Bad Gateway"), permit(), FakeResponse(200, text="OK")])
    broken.schedule(published)
    await asyncio.sleep(0.05)
    check(
        "сбой сервиса не теряет пост: вторая проба его добирает",
        broken.ordered == 1 and broken.failed == 0 and not broken._queue and len(broken.session.calls) == 4,
        f"{broken.ordered}/{broken.failed} {broken.last_error}",
    )

    drop = aiohttp.ClientError("обрыв соединения")
    again = reactor([permit(), drop, FakeResponse(200, text="OK")])
    again.schedule(published)
    await asyncio.sleep(0.05)
    check(
        "обрыв формы повторяется, а не теряет заказ",
        again.ordered == 1 and again.failed == 0 and len(again.session.calls) == 3,
        f"{again.ordered}/{again.failed} {again.last_error}",
    )
    never = reactor([permit(), drop, drop, permit(), drop, drop])
    never.schedule(published)
    await asyncio.sleep(0.05)
    check(
        "после двух неудач пост отпускаем с объяснением",
        never.failed == 1 and not never._queue and "ClientError" in never.last_error and len(never.session.calls) == 6,
        f"{never.ordered}/{never.failed} {never.last_error}",
    )

    off_session = FakeSession([permit(), FakeResponse(200, text="OK")])
    off = Reactor(make_cfg(reactions_enabled=False), off_session)
    off.schedule(published)
    await asyncio.sleep(0.05)
    check("выключенные реакции не заказывают", off_session.calls == [] and off.ordered == 0, str(off_session.calls))

    private = reactor([])
    private.schedule(Message(client, 80, secret))
    await asyncio.sleep(0.05)
    check("пост без публичной ссылки пропущен", private.skipped == 1 and private.session.calls == [], str(private.session.calls))

    bank = client.add_chat(Chat(-10032, title="Банк", username="bank_ch", chat_type=ChatType.CHANNEL))
    queued = FakeSession([permit(), FakeResponse(200, text="OK"), permit(), FakeResponse(200, text="OK")])
    hour = Reactor(make_cfg(reactions_enabled=True, reactions_interval=3600), queued)
    hour.wait = 0
    hour.poll = 0
    hour.permit_limit = 0.01
    hour.check_delays = ()
    hour.schedule(published)
    hour.schedule(Message(client, 79, yumor, caption="текст", photo=Photo("r2")))
    hour.schedule(Message(client, 81, bank, caption="текст", photo=Photo("b1")))
    await asyncio.sleep(0.05)
    check("первый пост заказал сразу", len(queued.calls) == 2, str(len(queued.calls)))
    check(
        "каждый свежий пост ждёт своего окна отдельно",
        len(hour._queue) == 2
        and sorted(short(entry.link) for entry in hour._queue.values()) == ["bank_ch/81", "yumor_ch/79"],
        str(list(hour._queue)),
    )
    hour._worker.cancel()
    await asyncio.sleep(0.05)
    check("отменённая очередь не успела заказать", len(queued.calls) == 2, str(len(queued.calls)))

    pile = reactor([], reactions_interval=3600)
    for index in range(10):
        pile.schedule(Message(client, 100 + index, bank, caption="текст", photo=Photo(f"p{index}")))
    pile.schedule(published)
    check(
        "посты ждут все вместе, новый не вытесняет старый",
        len(pile._queue) == 11 and "https://t.me/bank_ch/100?single" in pile._queue
        and "https://t.me/bank_ch/109?single" in pile._queue,
        str(len(pile._queue)),
    )
    check("посты не теряются молча", pile.skipped == 0, str(pile.skipped))
    pile._worker.cancel()
    await asyncio.sleep(0.05)

    crowd = reactor([], reactions_interval=3600)
    for index in range(25):
        crowd.schedule(Message(client, 200 + index, bank, caption="текст", photo=Photo(f"c{index}")))
    check(
        "очередь держит 20 самых свежих постов",
        len(crowd._queue) == 20 and crowd.dropped == 5 and "https://t.me/bank_ch/204?single" not in crowd._queue,
        f"{len(crowd._queue)} / снято {crowd.dropped}",
    )
    check("и снятые посты видны в докладе", "снято со старья 5" in crowd.describe(), crowd.describe())
    crowd._worker.cancel()
    await asyncio.sleep(0.05)

    live_cfg = make_cfg(reactions_enabled=True)
    world = build_world(live_cfg)
    live_session = FakeSession([permit(), FakeResponse(200, text="OK")])
    bot, _, _ = await make_bot(world, session=live_session)
    bot.reactions.wait = 0
    bot.reactions.poll = 0
    bot.reactions.permit_limit = 0.01
    bot.reactions.check_delays = ()
    await bot.on_message(
        world["client"], Message(world["client"], 61, world["donor_other"], caption="пост про мемы", photo=Photo("live1"))
    )
    await asyncio.sleep(0.1)
    link = live_session.calls[1][2]["link"] if len(live_session.calls) > 1 else ""
    check("после публикации заказ встаёт в очередь", link.startswith("https://t.me/yumor_ch/"), str(link))

    check("по умолчанию заказ раз в 5 минут", Config(api_id=1, api_hash="h", session_string="s").reactions_interval == 300,
          str(Config(api_id=1, api_hash="h", session_string="s").reactions_interval))

    refused = reactor([permit(), FakeResponse(200, text="Лимит: один заказ в час, подождите"),
                       permit(), FakeResponse(200, text="OK")])
    refused.schedule(published)
    await asyncio.sleep(0.05)
    check(
        "ответ про лимит не откатывает на час: тот же пост пробуется снова",
        refused.ordered == 1 and refused.failed == 0 and not refused._queue
        and refused._next_slot - time.monotonic() < 600,
        f"{refused.ordered}/{refused.failed} {refused.last_error} / {refused._next_slot - time.monotonic():.0f}",
    )

    denied = reactor([ReactionsError("Лимит: попробуй позже")], reactions_interval=300)
    denied.schedule(published)
    await asyncio.sleep(0.05)
    check(
        "отказ сервиса - не час, а вторая проба в следующем окне",
        denied.ordered == 0 and denied.failed == 0 and len(denied._queue) == 1
        and 0 < denied._next_slot - time.monotonic() < 600,
        f"{denied.ordered}/{denied.failed} {denied.last_error} / {denied._next_slot - time.monotonic():.0f}",
    )
    denied._worker.cancel()
    plain = reactor([permit(), FakeResponse(502, text="Bad Gateway")])
    plain.schedule(published)
    await asyncio.sleep(0.05)
    check("обычный сбой окно не трогает", plain.failed == 1 and plain._next_slot - time.monotonic() < 60,
          f"{plain.last_error} / {plain._next_slot - time.monotonic():.0f}")
    check("отказ без разрешения тоже не час", silent._next_slot - time.monotonic() < 60,
          str(silent._next_slot - time.monotonic()))

    panel_cfg = make_cfg(bot_token="900:AAstub", reactions_enabled=True)
    panel_world = build_world(panel_cfg)
    panel_bot, panel_registry, _ = await make_bot(panel_world)
    pclient = panel_world["client"]
    owner = pclient.add_user(User(777, "Витя"))
    panel = Panel(pclient, panel_cfg, panel_registry, panel_bot)
    panel.setup()
    await panel.start(owner.id, "Витя")
    await panel.on_message(pclient, Message(pclient, 9001, owner, text="/start"))

    def press(data):
        return Query(pclient, data, owner.id, Message(pclient, 9002, owner, text="меню"))

    body = pclient.replies[-1][1]
    rows = pclient.replies[-1][2].inline_keyboard
    cells = [button.callback_data for row in rows for button in row]
    check("меню рассказывает про реакции", "Реакции: включены" in body, body[:120])
    check("общей кнопки реакций больше нет", "reactions" not in cells, str(cells))

    await panel.on_button(pclient, press("cat|22"))
    check("канал показывает свои реакции", "4. Реакции:" in pclient.edits[-1][2] and "ставим" in pclient.edits[-1][2], pclient.edits[-1][2][:260])
    await panel.on_button(pclient, press("cw|22"))
    check("переключатель текста выключает переписывание", panel_registry.categories[22].rewrite is False
          and "словами автора" in pclient.edits[-1][2], pclient.edits[-1][2][:260])
    check("переписывание записано в тему канала",
          any("Переписывание: выкл" in text for _, text, _thread in pclient.topic_posts),
          str(pclient.topic_posts[-1]))
    check("у другого канала текст не тронут", panel_registry.rewrite_for(panel_registry.categories[21]) is True,
          str(panel_registry.categories[21].rewrite))
    await panel.on_button(pclient, press("cw|22"))
    check("и возвращается обратно", panel_registry.categories[22].rewrite is True
          and "переписывает нейросеть" in pclient.edits[-1][2], pclient.edits[-1][2][:260])
    await panel.on_button(pclient, press("cr|22"))
    check("кнопка выключает реакции канала", panel_registry.categories[22].reactions is False
          and "выключены" in pclient.edits[-1][2], pclient.edits[-1][2][:160])
    check("другой канал не тронут", panel_registry.reactions_for(panel_registry.categories[21]) is True,
          str(panel_registry.categories[21].reactions))
    await panel.on_button(pclient, press("cr|22"))
    check("кнопка снова включает реакции канала", panel_registry.categories[22].reactions is True
          and "включены" in pclient.edits[-1][2], pclient.edits[-1][2][:160])
    await panel.on_button(pclient, press("status"))
    check("статус показывает очередь реакций", "Реакции:" in pclient.edits[-1][2] and "smmnakrutka" in pclient.edits[-1][2], pclient.edits[-1][2][:200])


async def check_reaction_measure():
    print("\n[19] Замер: сколько реакций реально встало под постом")
    from reactions import MEASURE_KEEP, Reactor, baseline_word, count_reactions, parse_link, reactions_total, short, verdict

    cfg = make_cfg(reactions_enabled=True)
    client = FakeClient()
    channel = client.add_chat(Chat(-10040, title="ПРАЙМ СПОРТ", username="proprime_chan", chat_type=ChatType.CHANNEL))
    arrived = Message(client, 895, channel, caption="текст", photo=Photo("f1"))
    arrived.reactions = Counter(("🔥", 3), ("⭐", 1))
    silent = Message(client, 896, channel, caption="текст", photo=Photo("f2"))
    client.histories[channel.id] = [arrived, silent]

    check("ссылка разворачивается обратно", parse_link("https://t.me/proprime_chan/895?single") == ("proprime_chan", 895))
    check("в журнале ссылка короткая", short("https://t.me/proprime_chan/895?single") == "proprime_chan/895", short("x"))
    check("реакции считаются по эмодзи", count_reactions(arrived) == "🔥3 ⭐1 (всего 4)", count_reactions(arrived))
    check("без реакций честно пишем «нет»", count_reactions(silent) == "нет", count_reactions(silent))
    check("для сравнения нужно одно число", reactions_total(arrived) == 4 and reactions_total(silent) == 0, str(reactions_total(silent)))
    check("итог заказа - дельта, а не догадка", verdict(4, 7) == "сервис добавил 3" and verdict(4, 4) == "сервис не добавил ничего", verdict(4, 7))
    check("меньше стало - тоже пишется прямо", verdict(5, 3) == "реакций стало меньше на 2", verdict(5, 3))
    check("без замера до заказа врать нечем", verdict(-1, 4) == "сравнить не с чем" and baseline_word(-1) == "до заказа не измерено", verdict(-1, 4))

    def permit(key="kluch"):
        return FakeResponse(200, text='{"data": "%s"}' % key)

    link = "https://t.me/proprime_chan/895?single"
    reactor = Reactor(cfg, FakeSession([permit(), FakeResponse(200, text="OK")]), client)
    reactor.wait = reactor.poll = 0
    reactor.permit_limit = 0.01
    reactor.check_delays = (0.01, 0.03)
    check("пока заказов не было - панель это говорит", "не измерено" in reactor.report(), reactor.report())

    await reactor.order(link)
    await asyncio.sleep(0.1)
    check("после заказа начинается замер", len(reactor.measures) == 2, str(reactor.measures))
    check(
        "замер пишет конкретные числа",
        "proprime_chan/895" in reactor.measures[0] and "🔥3 ⭐1 (всего 4)" in reactor.measures[0],
        str(reactor.measures),
    )
    check("замер помнит, через сколько смотрел", "через" in reactor.measures[0], str(reactor.measures))
    check("до заказа пост уже посчитан", reactor.last_baseline == 4, str(reactor.last_baseline))
    check("в замере видно число до заказа", "до заказа было 4" in reactor.measures[0], reactor.measures[0])
    check("ничего не добавилось - так и пишем", reactor.last_verdict == "сервис не добавил ничего", reactor.last_verdict)
    arrived.reactions = Counter(("🔥", 5), ("⭐", 2), ("❤️", 1))
    await reactor._measure(link, 0.01, 4)
    check("прирост виден как работа сервиса", "сервис добавил 4" in reactor.measures[0], reactor.measures[0])
    check("панель повторяет итог заказа", "сервис добавил 4" in reactor.describe(), reactor.describe())
    check("замер виден в панели", "🔥3 ⭐1" in reactor.report(), reactor.report()[:120])
    check("сервис не начал - замер говорит «нет»", await reactor.count("https://t.me/proprime_chan/896?single") == "нет")
    check("поста нет - так и пишем", await reactor.count("https://t.me/proprime_chan/9999?single") == "поста там нет")

    for index in range(MEASURE_KEEP + 4):
        reactor.measures.insert(0, f"канал/{index}")
        del reactor.measures[MEASURE_KEEP:]
    check("замеров не копить", len(reactor.measures) == MEASURE_KEEP, str(len(reactor.measures)))

    broken = Reactor(cfg, FakeSession([permit(), FakeResponse(200, text="OK")]), client)
    broken.wait = broken.poll = 0
    broken.permit_limit = 0.01
    broken.check_delays = (0.01,)
    await broken.order("https://t.me/no_such_chan/12?single")
    await asyncio.sleep(0.1)
    check("не открытый пост - только журнал", broken.measures and "не измерить" in broken.measures[0], str(broken.measures))

    world = build_world(make_cfg(reactions_enabled=True))
    bot, _, _ = await make_bot(world)
    bot.reactions.measures = ["proprime_chan/895 через 20 мин: 🔥3 ⭐1 (всего 4)"]
    check("кнопка «🩺 Проверка» показывает замеры", "🔥3 ⭐1" in await bot.diagnose(), (await bot.diagnose())[-160:])


async def check_views():
    print("\n[13] 👁 Просмотры и реакции - свои у каждого канала")
    from config import VIEWS_LIMIT, parse_views

    def rejects(raw):
        try:
            parse_views(raw)
        except ValueError as exc:
            return str(exc)
        return ""

    check("число берётся из строки", parse_views("1000") == 1000, str(parse_views("1000")))
    check("пробелы и точки не мешают", parse_views(" 1 500 ") == 1500 and parse_views("2.000") == 2000, str(parse_views(" 1 500 ")))
    check("потолок сервиса разрешён", parse_views(str(VIEWS_LIMIT)) == VIEWS_LIMIT, str(VIEWS_LIMIT))
    def rejects_low(raw):
        try:
            parse_views(raw, low=0)
        except ValueError as exc:
            return str(exc)
        return ""

    check("ноль отклонён", rejects("0").startswith("Просмотров должно быть"), rejects("0"))
    check("больше потолка отклонено", rejects(str(VIEWS_LIMIT + 1)) != "", rejects(str(VIEWS_LIMIT + 1)))
    check("в кабинете ноль - выбор, а не ошибка", parse_views("0", low=0) == 0, str(parse_views("0", low=0)))
    check("без числа не принять и в кабинете", rejects_low("много").startswith("Напиши одно число"), rejects_low("много"))
    check("потолок и в кабинете общий", rejects_low(str(VIEWS_LIMIT + 1)).startswith("Просмотров должно быть"),
          rejects_low(str(VIEWS_LIMIT + 1)))
    check("не число отклонено", rejects("много").startswith("Напиши одно число"), rejects("много"))

    cfg = make_cfg(promo_views=1000)
    world = build_world(cfg)
    client, group = world["client"], world["group"]
    registry = Registry(client, cfg)
    await registry.rebuild()
    check("в группе числа нет - остаётся переменная", registry.views == 0 and cfg.promo_views == 1000, str(registry.views))
    check("канал без своей настройки берёт число по умолчанию", registry.views_for(registry.categories[21]) == 1000,
          str(registry.views_for(registry.categories[21])))

    client.group_messages.append(Message(client, 6, group, text="Просмотры: 3000", thread=1))
    await registry.rebuild()
    check("число по умолчанию прочитано из общей темы", registry.views == 3000 and cfg.promo_views == 3000, str(registry.views))
    check("каналы без своих настроек берут новое число", registry.views_for(registry.categories[21]) == 3000,
          str(registry.views_for(registry.categories[21])))

    number = await registry.save_category_views(21, "144")
    check("свои просмотры канала применены", number == 144 and registry.views_for(registry.categories[21]) == 144, str(number))
    check("другой канал не тронут", registry.views_for(registry.categories[22]) == 3000,
          str(registry.views_for(registry.categories[22])))
    check("настройка записана в тему канала", client.topic_posts[-1] == (group.id, "Просмотры: 144", 21), str(client.topic_posts[-1]))
    check("служебная строка не стала донором", registry.donor_count == 3, str(registry.donor_count))

    await registry.rebuild()
    check("свои просмотры пережили перечитку", registry.views_for(registry.categories[21]) == 144,
          str(registry.views_for(registry.categories[21])))
    await registry.save_category_views(21, "2000")
    check("второй раз то же сообщение правится", client.edits[-1][2] == "Просмотры: 2000", str(client.edits[-1][2]))
    check("служебных сообщений не плодится",
          sum(1 for _, text, thread in client.topic_posts if "Просмотры" in text and thread == 21) == 1,
          str([post for post in client.topic_posts if "Просмотры" in post[1]]))
    check("плохое число не меняет настройку",
          (await _raises(registry.save_category_views, 21, "много")) and registry.views_for(registry.categories[21]) == 2000,
          str(registry.views_for(registry.categories[21])))
    check("ноль - осознанный выбор канала", await registry.save_category_views(21, "0") == 0
          and registry.views_for(registry.categories[21]) == 0, str(registry.views_for(registry.categories[21])))

    enabled = await registry.toggle_category_reactions(22)
    check("реакции канала включились", enabled is True and registry.reactions_for(registry.categories[22]) is True, str(enabled))
    check("флаг записан в тему канала", client.topic_posts[-1] == (group.id, "Реакции: вкл", 22), str(client.topic_posts[-1]))
    await registry.rebuild()
    check("флаг реакций пережил перечитку", registry.reactions_for(registry.categories[22]) is True,
          str(registry.reactions_for(registry.categories[22])))
    await registry.toggle_category_reactions(22)
    check("повторное нажатие выключает", registry.reactions_for(registry.categories[22]) is False,
          str(registry.reactions_for(registry.categories[22])))
    check("правится то же сообщение темы", client.edits[-1][2] == "Реакции: выкл", str(client.edits[-1][2]))
    await registry.save_category_views(22, "500")
    check("в сообщении оба поля канала", client.edits[-1][2] == "Просмотры: 500\nРеакции: выкл", str(client.edits[-1][2]))
    await registry.rebuild()
    check("оба поля восстановились после перечитки",
          registry.views_for(registry.categories[22]) == 500 and registry.reactions_for(registry.categories[22]) is False,
          f"{registry.views_for(registry.categories[22])} / {registry.reactions_for(registry.categories[22])}")

    off = await registry.toggle_category_rewrite(22)
    check("переписывание канала выключилось", off is False and registry.rewrite_for(registry.categories[22]) is False, str(off))
    check("в сообщении все три поля канала",
          client.edits[-1][2] == "Просмотры: 500\nРеакции: выкл\nПереписывание: выкл", str(client.edits[-1][2]))
    await registry.rebuild()
    check("третье поле пережило перечитку", registry.rewrite_for(registry.categories[22]) is False,
          str(registry.rewrite_for(registry.categories[22])))
    check("и нелезет в доноры", registry.donor_count == 3, str(registry.donor_count))

    live_cfg = make_cfg(promo_views=1000, promo_delay=0, reactions_enabled=True)
    live = build_world(live_cfg)
    bot, live_registry, _ = await make_bot(live)
    bot.reactions.wait = 0
    bot.reactions.poll = 0
    bot.reactions.permit_limit = 0.01
    bot.reactions.check_delays = ()
    await live_registry.save_category_views(22, "4500")
    await live_registry.toggle_category_reactions(22)  # включены по умолчанию -> выключаем этому каналу
    await bot.on_message(live["client"], Message(live["client"], 61, live["donor_other"], caption="пост", photo=Photo("v1")))
    check("промо-бот получает число канала", ("promo_bot", "4500") in live["client"].sent, str(live["client"].sent[-1]))
    check("выключенные реакции канала не ставят пост в очередь", len(bot.reactions._queue) == 0, str(list(bot.reactions._queue)))

    panel_cfg = make_cfg(bot_token="900:AAstub", promo_views=1000, promo_delay=0, reactions_enabled=False)
    panel_world = build_world(panel_cfg)
    panel_bot, panel_registry, _ = await make_bot(panel_world)
    pclient = panel_world["client"]
    owner = pclient.add_user(User(777, "Витя"))
    panel = Panel(pclient, panel_cfg, panel_registry, panel_bot)
    panel.setup()
    await panel.start(owner.id, "Витя")

    def press(data):
        return Query(pclient, data, owner.id, Message(pclient, 9002, owner, text="меню"))

    def panel_buttons():
        keyboard = pclient.edits[-1][3]
        return [] if keyboard is None else [b.callback_data for row in keyboard.inline_keyboard for b in row]

    await panel.on_message(pclient, Message(pclient, 9001, owner, text="/start"))
    cells = [button.callback_data for row in pclient.replies[-1][2].inline_keyboard for button in row]
    check("общей кнопки просмотров больше нет", "views" not in cells, str(cells))
    check("в меню есть «Мои каналы»", "topics" in cells, str(cells))

    await panel.on_button(pclient, press("cat|21"))
    check("экран канала показывает число по умолчанию", "1000 на каждый пост" in pclient.edits[-1][2], pclient.edits[-1][2][:200])
    check("у канала своя кнопка просмотров", "cv|21" in panel_buttons(), str(panel_buttons()))

    await panel.on_button(pclient, press("cv|21"))
    body = pclient.edits[-1][2]
    check("экран объясняет диапазон", f"от 0 до {VIEWS_LIMIT}" in body, body[:160])
    check("экран показывает текущее число", "Сейчас: 1000 на каждый пост" in body, body[:100])

    await panel.on_message(pclient, Message(pclient, 9003, owner, text="144"))
    check("введённое число применено к каналу", panel_registry.views_for(panel_registry.categories[21]) == 144,
          str(panel_registry.views_for(panel_registry.categories[21])))
    check("другой канал остался при своём", panel_registry.views_for(panel_registry.categories[22]) == 1000,
          str(panel_registry.views_for(panel_registry.categories[22])))
    check("панель это подтверждает", "Просмотры канала" in pclient.replies[-1][1] and "144" in pclient.replies[-1][1],
          str(pclient.replies[-1][1][:80]))

    await panel.on_button(pclient, press("cv|21"))
    await panel.on_message(pclient, Message(pclient, 9004, owner, text="много"))
    check("не число - подсказка, старое значение целое",
          "Напиши одно число" in pclient.replies[-1][1] and panel_registry.views_for(panel_registry.categories[21]) == 144,
          str(pclient.replies[-1][1][:80]))

    await panel.on_button(pclient, press("cv|21|2000"))
    check("готовая кнопка ставит число", panel_registry.views_for(panel_registry.categories[21]) == 2000,
          str(panel_registry.views_for(panel_registry.categories[21])))
    await panel.on_button(pclient, press(f"cv|21|{VIEWS_LIMIT + 1}"))
    check("кнопка выше потолка отклонена",
          panel_registry.views_for(panel_registry.categories[21]) == 2000 and "Просмотров должно быть" in pclient.edits[-1][2],
          pclient.edits[-1][2][:80])
    await panel.on_button(pclient, press("cv|21|0"))
    check("ноль кнопкой - не накручивать", panel_registry.views_for(panel_registry.categories[21]) == 0,
          str(panel_registry.views_for(panel_registry.categories[21])))

    await panel.on_button(pclient, press("status"))
    check("статус показывает число по умолчанию", "Просмотры для новых каналов" in pclient.edits[-1][2], pclient.edits[-1][2][:200])


async def check_legal():
    print("\n[27] Документы сервиса: соглашение и политика прямо в боте")
    import legal

    check("в соглашении назван сервис", legal.SERVICE in legal.AGREEMENT, legal.SERVICE)
    check("в политике назван сервис", legal.SERVICE in legal.PRIVACY, legal.SERVICE)
    check("у обоих документов есть дата редакции", legal.EDITION in legal.AGREEMENT and legal.EDITION in legal.PRIVACY, legal.EDITION)
    for kind in (1, 2):
        title, pages = legal.document(kind)
        raw = legal.DOCS[kind][1]
        check(f"документ {kind} разбит на страницы", bool(pages) and bool(title), str(len(pages)))
        check(f"страницы документа {kind} не длиннее лимита", all(len(page) <= legal.PAGE_LIMIT for page in pages),
              str([len(page) for page in pages]))
        check(f"нарезка документа {kind} ничего не потеряла",
              "".join(raw.split()) == "".join("".join(pages).split()),
              f"{len(''.join(raw.split()))} / {len(''.join(''.join(pages).split()))}")


async def _raises(action, *args):
    try:
        await action(*args)
    except ValueError:
        return True
    return False


async def check_signature():
    print("\n[14] Подпись донора не попадает в наш канал")
    from ai import AiRewriter
    from utils import strip_source

    client = FakeClient()
    donor = client.add_chat(Chat(-10080, title="Банки, деньги, два офшора", username="bankrollo", chat_type=ChatType.CHANNEL))
    private = client.add_chat(Chat(-10081, title="Приват без юзернейма", chat_type=ChatType.CHANNEL))

    post = "Офисы в Абу-Даби подешевели.\n\nСтавка $1026 за кв. м.\n@bankrollo"
    out = strip_source(post, donor)
    check("строка с @юзернеймом убрана", "@bankrollo" not in out and "Офисы" in out, out)
    check("текст поста цел", "Ставка $1026 за кв. м." in out and out.endswith("кв. м."), out)
    check("абзац не схлопнулся", "подешевели.\n\nСтавка" in out, repr(out))
    check("регистр не важен", "@BankRollo" not in strip_source("См. @BankRollo сегодня", donor))
    check("ссылка на донора убрана", "bankrollo" not in strip_source("Подробнее https://t.me/bankrollo/512", donor))
    check("чужой юзернейм остаётся", "@partner_ch" in strip_source("Партнёр @partner_ch и @bankrollo", donor))
    check("у приватного донора чистить нечего", strip_source("текст @bankrollo", private) == "текст @bankrollo")

    world = build_world(make_cfg(reactions_enabled=False, promo_delay=0))
    bot, registry, _ = await make_bot(world, ai=AiRewriter(world["cfg"], FakeSession([])))
    await bot.on_message(
        world["client"], Message(world["client"], 71, world["donor_other"], caption="Мемы дня.\n@donor_other", photo=Photo("s1"))
    )
    caption = world["client"].posted[-1].caption
    check("без нейросети публикуется чистый оригинал", caption == "Мемы дня.", repr(caption))

    cards = [
        Message(world["client"], 81, world["donor_sport_1"], photo=Photo("s2"), group="gs1"),
        Message(world["client"], 82, world["donor_sport_1"], photo=Photo("s3"), group="gs1", caption="Хоккей завтра.\n@donor_sport_1"),
    ]
    world["client"].donor_messages[world["donor_sport_1"].id] = cards
    await bot.on_message(world["client"], cards[0])
    album = world["client"].albums[-1]
    check("альбом тоже без подписи", album[0].caption == "Хоккей завтра." and not album[1].caption, repr(album[0].caption))


def _schema_tables():
    """Колонки из SCHEMA модуля store: фейк базы сверяет запросы с настоящим описанием таблиц."""
    import re

    from store import SCHEMA

    tables = {}
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S):
        columns = set()
        for line in body.splitlines():
            first = line.strip().split(" ")[0].rstrip(",")
            if first.upper() in ("PRIMARY", "CONSTRAINT", "UNIQUE", "FOREIGN", ""):
                continue
            if re.match(r"^[a-z_][a-z0-9_]*$", first):
                columns.add(first)
        tables[name] = columns
    return tables


SCHEMA_TABLES = _schema_tables()


def _tenant_row(tenant_id: int = 777, balance: int = 0, until=None, target: str = "@client_channel", **over):
    import datetime

    moment = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)
    values = {
        "id": tenant_id,
        "target": target,
        "target_id": -1005554443332,
        "target_title": "Канал клиента",
        "target_ok": False,
        "views": 0,
        "reactions": True,
        "rewrite": True,
        "balance_rub": balance,
        "plan_until": until,
        "agreed_at": None,
        "last_post_id": 0,
        "last_post_at": None,
        "reactions_added": None,
        "created_at": moment,
        "updated_at": moment,
    }
    # ключи берём из схемы: новый столбец без значения упадёт KeyError и заставит обновить фейк
    row = {name: values[name] for name in SCHEMA_TABLES["tenants"]}
    row.update(over)
    return row


class MemoryStore:
    """Та же база, только в памяти: кабинеты проверяются без Postgres, по тем же правилам."""

    def __init__(self):
        from store import Donor, Tenant

        self.donor_class = Donor
        self.tenant_class = Tenant
        self.tenants = {}
        self.donors = {}
        self.payments = []
        self.usage = {}
        self.next_payment = 1

    async def get_tenant(self, tenant_id):
        return self.tenants.get(tenant_id)

    async def ensure_tenant(self, tenant_id):
        import datetime

        return self.tenants.setdefault(tenant_id, self.tenant_class(id=tenant_id))

    async def list_tenants(self):
        return [tenant for _, tenant in sorted(self.tenants.items())]

    async def list_donors(self, tenant_id):
        return sorted(
            [donor for (owner, _), donor in self.donors.items() if owner == tenant_id],
            key=lambda donor: donor.ref,
        )

    async def add_donor(self, tenant_id, ref):
        from store import MAX_DONORS, StoreError, ref_key

        key = ref_key(ref)
        if (tenant_id, key) not in self.donors:
            if len(await self.list_donors(tenant_id)) >= MAX_DONORS:
                raise StoreError(f"Больше {MAX_DONORS} каналов-источников нельзя - убери лишний.")
            self.donors[(tenant_id, key)] = self.donor_class(key)
        return self.donors[(tenant_id, key)]

    async def remove_donor(self, tenant_id, ref):
        from store import ref_key

        self.donors.pop((tenant_id, ref_key(ref)), None)

    async def bind_donor(self, ref, chat_id, title=""):
        from store import ref_key

        key = ref_key(ref)
        bound = 0
        for (owner, donor_ref), donor in self.donors.items():
            if donor_ref == key:
                donor.chat_id, donor.title = chat_id, title
                bound += 1
        return bound

    async def set_target(self, tenant_id, target, target_id, title):
        tenant = await self.ensure_tenant(tenant_id)
        tenant.target, tenant.target_id, tenant.target_title, tenant.target_ok = target, target_id, title or "", False

    async def set_views(self, tenant_id, views):
        (await self.ensure_tenant(tenant_id)).views = views

    async def set_reactions(self, tenant_id, enabled):
        (await self.ensure_tenant(tenant_id)).reactions = enabled

    async def set_rewrite(self, tenant_id, enabled):
        (await self.ensure_tenant(tenant_id)).rewrite = enabled

    async def set_target_ok(self, tenant_id, ok, title=""):
        tenant = await self.ensure_tenant(tenant_id)
        tenant.target_ok = bool(ok)
        if title:
            tenant.target_title = title

    async def note_publish(self, tenant_id, message_id):
        tenant = await self.ensure_tenant(tenant_id)
        tenant.last_post_id, tenant.last_post_at = message_id, _utcnow()

    async def note_reactions(self, tenant_id, added):
        (await self.ensure_tenant(tenant_id)).reactions_added = added

    async def set_agreed(self, tenant_id):
        (await self.ensure_tenant(tenant_id)).agreed = True

    async def add_balance(self, tenant_id, amount, note=""):
        tenant = await self.ensure_tenant(tenant_id)
        tenant.balance += amount
        self._row(tenant_id, "topup", amount, status="done", note=note)
        return tenant

    def _row(self, tenant_id, kind, amount, days=0, status="done", note=""):
        """Строка платежей с настоящим id: по ним панель и находит заявку."""
        row = {"id": self.next_payment, "tenant_id": tenant_id, "kind": kind, "amount_rub": amount,
               "days": days, "status": status, "note": note, "created_at": _utcnow()}
        self.next_payment += 1
        self.payments.append(row)
        return row

    async def request_topup(self, tenant_id, amount):
        from store import StoreError, check_topup

        check_topup(amount)
        await self.ensure_tenant(tenant_id)
        return self._row(tenant_id, "topup", amount, status="pending")["id"]

    async def credit(self, tenant_id, amount, note="начислено из панели"):
        from store import check_topup

        check_topup(amount)
        return await self.add_balance(tenant_id, amount, note)

    async def pending_topups(self, limit=20):
        rows = [row for row in self.payments if row["status"] == "pending"][:limit]
        return [dict(row, target=self.tenants[row["tenant_id"]].target) for row in rows]

    async def confirm_topup(self, payment_id):
        from store import StoreError

        row = next((item for item in self.payments if item["id"] == payment_id and item["status"] == "pending"), None)
        if row is None:
            raise StoreError("Эта заявка уже разобрана - обнови экран.")
        row["status"] = "done"
        row["note"] = "начислено из панели"
        await self.ensure_tenant(row["tenant_id"])
        self.tenants[row["tenant_id"]].balance += row["amount_rub"]
        return {"tenant_id": row["tenant_id"], "amount_rub": row["amount_rub"]}

    async def reject_topup(self, payment_id):
        from store import StoreError

        row = next((item for item in self.payments if item["id"] == payment_id and item["status"] == "pending"), None)
        if row is None:
            raise StoreError("Этой заявки уже нет в очереди.")
        row["status"] = "rejected"
        row["note"] = "платёж не найден"
        return row["tenant_id"]

    async def buy(self, tenant_id, kind):
        from store import PLANS, StoreError

        if kind not in PLANS:
            raise StoreError(f"Нет такого тарифа: {kind}")
        tenant = await self.ensure_tenant(tenant_id)
        price = PLANS[kind]["price"]
        if tenant.balance < price:
            raise StoreError(f"На балансе {tenant.balance} ₽, а {PLANS[kind]['title']} стоит {price} ₽.")
        tenant.balance -= price
        tenant.renew(kind)
        self._row(tenant_id, kind, price, days=PLANS[kind]["days"])
        return tenant

    async def list_payments(self, tenant_id, limit=10):
        return [row for row in reversed(self.payments) if row["tenant_id"] == tenant_id][:limit]

    async def posts_today(self, tenant_id):
        return self.usage.get(tenant_id, 0)

    async def count_post(self, tenant_id):
        self.usage[tenant_id] = self.usage.get(tenant_id, 0) + 1
        return self.usage[tenant_id]

    async def quota_left(self, tenant_id):
        from store import MAX_POSTS_PER_DAY

        return await self.posts_today(tenant_id) < MAX_POSTS_PER_DAY

    async def donors_by_chat(self, chat_id):
        now = _utcnow()
        found = []
        for (tenant_id, _), donor in self.donors.items():
            if donor.chat_id != chat_id:
                continue
            tenant = self.tenants.get(tenant_id)
            if tenant and tenant.active(now) and tenant.target_ok and tenant.target:
                found.append(tenant)
        return found

    async def active_donor_chats(self):
        """Сколько каналов ловим по-клиентски - те же условия, что у donors_by_chat."""
        now = _utcnow()
        chats = set()
        for (tenant_id, _), donor in self.donors.items():
            tenant = self.tenants.get(tenant_id)
            if donor.chat_id and tenant and tenant.active(now) and tenant.target_ok and tenant.target:
                chats.add(donor.chat_id)
        return sorted(chats)

    async def unbound_refs(self):
        seen = {ref for (_, ref), donor in self.donors.items() if donor.chat_id is None}
        return sorted(seen)

    async def targets_to_check(self):
        return [tenant for tenant in self.tenants.values() if tenant.target and not tenant.target_ok]


def _utcnow():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc)


class FakeConn:
    """Проверяет таблицы и колонки запросов и отдаёт заготовленные строки."""

    def __init__(self, pool):
        self.pool = pool

    def _validate(self, query: str) -> None:
        import re

        sql = " ".join(query.split())
        for table in re.findall(r"(?:FROM|UPDATE|INTO|JOIN)\s+(?!SET\b)(\w+)", sql, re.I):
            if table not in SCHEMA_TABLES:
                raise AssertionError(f"запрос к несуществующей таблице {table}: {sql[:90]}")
        for table, assignments in re.findall(r"UPDATE (\w+) SET (.*?) WHERE", sql, re.I):
            for item in assignments.split(","):
                column = item.split("=")[0].strip().split(".")[-1]
                if column and column not in SCHEMA_TABLES[table]:
                    raise AssertionError(f"у таблицы {table} нет колонки {column}: {sql[:90]}")
        for table, columns in re.findall(r"INSERT INTO (\w+) \(([^)]*)\)", sql, re.I):
            for column in (part.strip() for part in columns.split(",")):
                if column and column not in SCHEMA_TABLES[table]:
                    raise AssertionError(f"у таблицы {table} нет колонки {column}: {sql[:90]}")

    async def execute(self, query, *args):
        self._validate(query)
        self.pool.queries.append((" ".join(query.split()), args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        self._validate(query)
        self.pool.queries.append((" ".join(query.split()), args))
        answer = self.pool.pop()
        if answer is not None:
            return answer
        return _tenant_row() if "FROM tenants" in query else None

    async def fetch(self, query, *args):
        self._validate(query)
        self.pool.queries.append((" ".join(query.split()), args))
        answer = self.pool.pop()
        return answer if answer is not None else []

    async def fetchval(self, query, *args):
        self._validate(query)
        self.pool.queries.append((" ".join(query.split()), args))
        answer = self.pool.pop()
        if isinstance(answer, dict):
            return next(iter(answer.values()))
        return answer if answer is not None else 0


class FakePool:
    def __init__(self):
        self.conn = FakeConn(self)
        self.answers: List[dict] = []
        self.queries: List[tuple] = []

    def pop(self):
        return self.answers.pop(0) if self.answers else None

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def check_store():
    print("\n[15] База клиентов: ссылки, тариф, баланс")
    import datetime as dt

    from store import MAX_DONORS, MONTH_RUB, PLANS, WEEK_RUB, Store, StoreError, Tenant, parse_ref, ref_key

    check("неделя 600, месяц 2000", WEEK_RUB == 600 and MONTH_RUB == 2000 and PLANS["month"]["days"] == 30,
          f"{WEEK_RUB}/{MONTH_RUB}")
    check("ссылка t.me", parse_ref("https://t.me/bankrollo") == "bankrollo", str(parse_ref("https://t.me/bankrollo")))
    check("собачка и голое имя", parse_ref("@BankRollo") == "bankrollo" and parse_ref("bankrollo") == "bankrollo")
    check("ссылка просмотра t.me/s", parse_ref("t.me/s/bankrollo") == "bankrollo")
    check("числовой id канала", parse_ref("-1002345678901") == -1002345678901, str(parse_ref("-1002345678901")))
    bad = ""
    try:
        parse_ref("ну возьми отсюда")
    except ValueError as exc:
        bad = str(exc)
    check("бессмыслица отклонена", "Не понимаю ссылку" in bad, bad)
    check("ключ не зависит от собачки", ref_key("@BankRollo") == ref_key("bankrollo") == "bankrollo", ref_key("@BankRollo"))
    check("числовой ключ строковый", ref_key(-1002345678901) == "-1002345678901", ref_key(-1002345678901))

    now = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)
    fresh = Tenant(id=1)
    check("без тарифа клиент не активен", not fresh.active(now) and fresh.days_left(now) == 0)
    cost = fresh.renew("week", now)
    check("первая неделя: цена и срок", cost == 600 and fresh.plan_until == now + dt.timedelta(days=7), str(fresh.plan_until))
    check("активен до конца срока", fresh.active(now + dt.timedelta(days=6)) and not fresh.active(now + dt.timedelta(days=8)))
    fresh.renew("week", now + dt.timedelta(days=6))
    check("продление до конца срока сохраняет остаток", fresh.plan_until == now + dt.timedelta(days=14), str(fresh.plan_until))
    month = Tenant(id=2, plan_until=now + dt.timedelta(days=20))
    month.renew("month", now)
    check("месяц добавляется к остатку", month.plan_until == now + dt.timedelta(days=50) and month.price("month") == 2000,
          str(month.plan_until))
    check("лимит доноров объявлен", MAX_DONORS == 10, str(MAX_DONORS))

    pool = FakePool()
    store = Store(pool)
    poor = await store.ensure_tenant(777)
    check("новый клиент: нулевой баланс", poor.balance == 0 and poor.plan_until is None, str(poor))
    short = ""
    try:
        await store.buy(777, "week")
    except StoreError as exc:
        short = str(exc)
    check("без денег покупка отклонена", "На балансе 0" in short, short)
    check("чужого тарифа нет", isinstance(await _raises_store(store.buy, 777, "god"), StoreError))

    pool.answers = [_tenant_row(balance=1000)]
    paid = await store.buy(777, "week")
    check("покупка продлевает срок", paid.days_left(dt.datetime.now(dt.timezone.utc)) >= 6, str(paid.plan_until))
    update = [call for call in pool.queries if "UPDATE tenants SET balance_rub" in call[0]]
    check("со списка списано 600", update and update[-1][1][1] == 600, str(update[-1][1] if update else ""))
    check("покупка записана в журнал", any("INSERT INTO payments" in call[0] and call[1][1] == "week" for call in pool.queries))

    pool.answers = [{"count": MAX_DONORS}]
    full = await _raises_store(store.add_donor, 777, "some_channel")
    check("сверх лимита доноров отказ", isinstance(full, StoreError) and "Больше 10" in str(full), str(full))

    pool.answers = [{"posts": 3}]
    check("счётчик дня читается", await store.posts_today(777) == 3)
    pool.answers = [{"posts": 4}]
    check("пост учтён", await store.count_post(777) == 4)
    check("обращение к таблице дня", any("day_usage" in call[0] for call in pool.queries))


async def check_cabinet():
    print("\n[22] Кабинет клиента: четыре шага, человеческие слова, честные числа")
    cfg = make_cfg(bot_token="900:AAstub", worker_contact="@worker_acc", payment_details="карта 2202 0000 0000 0000")
    world = build_world(cfg)
    client = world["client"]
    bot, registry, _ = await make_bot(world)
    store = MemoryStore()
    panel = Panel(client, cfg, registry, bot, store)
    panel.setup()
    await panel.start(777, "Фогель")

    owner = client.add_user(User(777, "Витя"))
    petr = client.add_user(User(5001, "Пётр"))
    ivan = client.add_user(User(5002, "Иван"))

    def tap(data, user=petr):
        return Query(client, data, user.id, Message(client, 6000, user, text="x"))

    def text():
        return client.edits[-1][2]

    def cells():
        keyboard = client.edits[-1][3]
        return [] if keyboard is None else [button.callback_data for row in keyboard.inline_keyboard for button in row]

    def reply():
        return client.replies[-1]

    def reply_text():
        return reply()[1]

    def reply_cells():
        keyboard = reply()[2]
        return [] if keyboard is None else [button.callback_data for row in keyboard.inline_keyboard for button in row]

    # ---- новичок: первое окно ----
    await panel.on_message(client, Message(client, 6001, petr, text="/start"))
    check("новичок попадает в пошаговый запуск", "Пошаговый запуск" in reply_text() and "Шаг 1 из 5" in reply_text(), reply_text()[:90])
    check("первый шаг назван человеческим словом", "Откуда брать посты" in reply_text(), reply_text()[:160])
    check("первый экран начинается с инструкции", "Что сделать" in reply_text() and "➕ Добавить канал" in reply_text(), reply_text()[:200])
    check("в кабинете свои кнопки", "cadd" in reply_cells() and "cnext" in reply_cells(), str(reply_cells()))
    check("клиент не видит панель владельца", "🤖 АвтоПост" not in reply_text() and "донор" not in reply_text().lower(), reply_text()[:120])
    await panel.on_message(client, Message(client, 6002, owner, text="/start"))
    check("владелец по-прежнему в своей панели", "АвтоПост" in reply_text(), reply_text()[:60])

    # ---- шаг 1: каналы-источники ----
    await panel.on_button(client, tap("cadd"))
    check("кнопка объясняет, где взять ссылку", "Где её взять" in text() and "до 10 каналов" in text(), text()[:220])
    await panel.on_message(client, Message(client, 6003, petr, text="@bankrollo"))
    check("источник записан в базу", (petr.id, "bankrollo") in store.donors, str(list(store.donors)))
    check("подписка обещана самим ботом", "Бот подпишется сам" in reply_text(), reply_text()[:200])
    check("новый канал пока не открыт", "⏳ подписываюсь" in reply_text(), reply_text()[:260])
    store.donors[(petr.id, "bankrollo")].chat_id = -100900
    await panel.on_button(client, tap("cdonors"))
    check("прочитанный канал отмечен галочкой", "✅ читаем" in text() and "@bankrollo" in text(), text()[:240])
    check("канал можно убрать кнопкой", cells()[1].startswith("cdel|"), str(cells()))
    await panel.on_button(client, tap("cdel|1"))
    check("канал убран", (petr.id, "bankrollo") not in store.donors, str(list(store.donors)))
    await panel.on_button(client, tap("cdel|7"))
    check("уже убранный не ломает экран", "уже убран" in text(), text()[:90])
    await panel.on_button(client, tap("cadd"))
    await panel.on_message(client, Message(client, 6004, petr, text="@bankrollo"))
    check("канал вернулся в список", (petr.id, "bankrollo") in store.donors, str(list(store.donors)))

    # ---- шаг 2: куда публиковать ----
    await panel.on_button(client, tap("cchannel"))
    check("шаг 2 объясняет про администратора", "администраторы" in text() and "@worker_acc" in text(), text()[:200])
    check("нужные права названы", "публиковать сообщения" in text(), text()[:280])
    check("ссылку просят отдельной кнопкой", "cset_target" in cells(), str(cells()))
    await panel.on_button(client, tap("cset_target"))
    check("перед вводом говорят про публичную ссылку", "Публичную ссылку" in text(), text()[:240])
    await panel.on_message(client, Message(client, 6005, petr, text="https://t.me/petr_ch"))
    check("канал записан в базу", store.tenants[petr.id].target == "petr_ch", store.tenants[petr.id].target)
    check("доступ ещё не подтверждён", "⏳ проверяю доступ" in reply_text(), reply_text()[:200])
    check("срок проверки обещан коротким", "пара минут" in reply_text(), reply_text()[:260])
    await panel.on_button(client, tap("cset_target"))
    await panel.on_message(client, Message(client, 6006, petr, text="куда-то туда"))
    check("текст без ссылки отклонён", "Не понимаю ссылку" in reply_text(), reply_text()[:130])
    await panel.on_button(client, tap("cset_target"))
    await panel.on_message(client, Message(client, 6007, petr, text="https://t.me/c/123456/78"))
    check("закрытая ссылка отклонена с объяснением", "закрытый канал" in reply_text().lower(), reply_text()[:170])
    check("и сказано, где взять публичную", "Публичная ссылка" in reply_text(), reply_text()[:300])
    await panel.on_button(client, tap("cset_target"))
    await panel.on_message(client, Message(client, 6008, petr, text="https://t.me/petr_ch"))
    check("после отказа ввод принимается снова", store.tenants[petr.id].target == "petr_ch", store.tenants[petr.id].target)

    # ---- шаг 4: просмотры ----
    await panel.on_button(client, tap("cviews"))
    check("диапазон просмотров назван", "10000" in text(), text()[:300])
    check("ноль назван выбором человека", "не накручивать" in text(), text()[:320])
    await panel.on_button(client, tap("cviews|1000"))
    check("просмотры с готовой кнопки", store.tenants[petr.id].views == 1000, str(store.tenants[petr.id].views))
    check("экран просмотров показывает число", "Сейчас: 1000 на каждый пост" in text(), text()[:200])
    await panel.on_button(client, tap("cviews|0"))
    check("ноль принимается как выбор", store.tenants[petr.id].views == 0 and "не накручиваем (0)" in text(), text()[:240])
    await panel.on_button(client, tap("cviews|999999"))
    check("просмотры выше потолка отклонены", store.tenants[petr.id].views == 0 and "Просмотров должно быть от 0" in text(), text()[:150])
    await panel.on_button(client, tap("cviews"))
    await panel.on_message(client, Message(client, 6009, petr, text="2500"))
    check("просмотры введены текстом", store.tenants[petr.id].views == 2500, str(store.tenants[petr.id].views))

    # ---- реакции ----
    await panel.on_button(client, tap("cboost"))
    check("экран реакций ставит условие", "ВСЕ реакции" in text(), text()[:280])
    check("без замера фразу не выдумываем", "ждём первый замер" in text(), text()[:340])
    await panel.on_button(client, tap("creactions"))
    check("реакции выключаются", store.tenants[petr.id].reactions is False and "выключены" in text(), text()[:200])
    await panel.on_button(client, tap("creactions"))
    check("реакции включаются обратно", store.tenants[petr.id].reactions is True and "включены" in text(), text()[:200])

    # ---- шаг 5: тариф и оплата ----
    await panel.on_button(client, tap("cplans"))
    check("цены названы", "Неделя - 600 ₽" in text() and "Месяц - 2000 ₽" in text(), text()[:200])
    check("что входит - перечислено", "постов в сутки" in text(), text()[:280])
    check("дни подписки не сгорают", "не сгорают" in text(), text()[-200:])
    await panel.on_button(client, tap("cbuy|week"))
    check("без согласия покупка не идёт", store.tenants[petr.id].plan_until is None and "cagree" in cells(), str(cells()))
    await panel.on_button(client, tap("ctopup"))
    check("пополнение тоже просит согласие", "cagree" in cells(), str(cells()))
    check("в согласии есть обе кнопки документов", "cdoc|1" in cells() and "cdoc|2" in cells(), str(cells()))
    await panel.on_button(client, tap("cagree"))
    check("согласие записано", store.tenants[petr.id].agreed is True, str(store.tenants[petr.id].agreed))
    await panel.on_button(client, tap("cbuy|week"))
    check("без денег покупка отклонена", store.tenants[petr.id].plan_until is None and "На балансе 0" in text(), text()[:150])
    await panel.on_button(client, tap("ctopup"))
    check("реквизиты взяты из настроек", "карта 2202" in text(), text()[:240])
    await panel.on_message(client, Message(client, 6010, petr, text="600"))
    check("заявка на оплату создана", any(row["status"] == "pending" and row["amount_rub"] == 600 for row in store.payments),
          str(store.payments))
    check("кабинет подтвердил заявку", "Заявка №1" in reply_text() and "начислю сумму" in reply_text(), reply_text()[:200])
    await panel.on_button(client, tap("ctopup"))
    await panel.on_message(client, Message(client, 6011, petr, text="50"))
    check("меньшая сумма отклонена", "от 100" in reply_text(), reply_text()[:150])
    await store.add_balance(petr.id, 600)
    await panel.on_button(client, tap("cbuy|week"))
    tenant = store.tenants[petr.id]
    check("неделя списана с баланса", tenant.active() and tenant.balance == 0, f"{tenant.plan_until} / {tenant.balance}")
    check("кабинет показывает срок", "активен ещё" in text(), text()[:200])
    check("покупка попала в историю", "Последнее по оплате" in text(), text()[-220:])
    await panel.on_button(client, tap("chome"))
    check("заявка без подтверждения помечена", "ждёт проверки" in text(), text()[-260:])
    check("настроенному клиенту не предлагают запуск", "Всё настроено" in text() and "cresult" in cells(), text()[-260:])
    check("обещание сервиса читается в кабинете",
          "Текст поста:" in text() and "просмотры и реакции" in text() and "Бот читает каналы" in text(), text()[:300])
    check("кабинет даёт все четыре шага кнопками", {"cdonors", "cchannel", "cviews", "cplans"} <= set(cells()), str(cells()))
    check("и помощь с кнопками документов", {"cguide", "cdoc|1", "cdoc|2"} <= set(cells()), str(cells()))

    sent_before = len(client.sent)
    await panel.on_button(client, tap("cdoc|1"))
    check("соглашение читается прямо в боте",
          any("Пользовательское соглашение" in text for _, text in client.sent[sent_before:]),
          str(client.sent[sent_before:])[:120])
    sent_before = len(client.sent)
    await panel.on_button(client, tap("cdoc|2"))
    check("политика тоже приходит сообщением",
          any("Политика конфиденциальности" in text for _, text in client.sent[sent_before:]),
          str(client.sent[sent_before:])[:120])
    await panel.on_button(client, tap("cdonors", ivan))
    check("у второго клиента свой пустой список", "Сейчас в списке: 0 из 10" in text(), text()[:240])
    await panel.on_button(client, tap("chome", ivan))
    check("чужие настройки не видны", "не накручиваем (0)" in text() and "@bankrollo" not in text(), text()[:240])
    wrong = tap("neknopka")
    await panel.on_button(client, wrong)
    check("несуществующая кнопка отвечает подсказкой", "Такой кнопки нет" in wrong.answers[-1][0], str(wrong.answers))


async def check_cabinet_launch():
    print("\n[23] Пошаговый запуск и экран «Результат»: идёт либо названа причина")
    from store import MAX_POSTS_PER_DAY

    cfg = make_cfg(bot_token="900:AAstub", minimax_api_key="key")
    world = build_world(cfg)
    client = world["client"]
    bot, registry, _ = await make_bot(world)
    store = MemoryStore()
    panel = Panel(client, cfg, registry, bot, store)
    panel.setup()
    await panel.start(777, "Фогель")

    nina = client.add_user(User(5010, "Нина"))

    def tap(data):
        return Query(client, data, nina.id, Message(client, 6100, nina, text="x"))

    def text():
        return client.edits[-1][2]

    def cells():
        keyboard = client.edits[-1][3]
        return [] if keyboard is None else [button.callback_data for row in keyboard.inline_keyboard for button in row]

    def reply():
        return client.replies[-1]

    await panel.on_message(client, Message(client, 6101, nina, text="/start"))
    check("первый экран ведёт за руку", "Шаг 1 из 5 - Откуда брать посты" in reply()[1], reply()[1][:80])
    check("у шага есть кнопка «Дальше»", "cnext" in reply()[2].inline_keyboard[-1][0].callback_data, reply()[1][:60])

    await panel.on_button(client, tap("cstart"))
    check("«Пошаговый запуск» открывает шаг 1", "Шаг 1 из 5" in text(), text()[:70])
    await panel.on_button(client, tap("cnext"))
    check("«Дальше» ведёт к каналу публикации", "Шаг 2 из 5 - Куда публиковать" in text(), text()[:90])
    check("без рабочего аккаунта просят помощь", "нажми «🆘 Помощь»" in text(), text()[-200:])
    await panel.on_button(client, tap("cnext"))
    check("шаг про выбор человека", "Шаг 3 из 5 - Текст поста" in text(), text()[:90])
    check("и объясняет переключатель", "Сейчас: переписывает нейросеть" in text() and "cretext" in cells(), text()[:260])
    await panel.on_button(client, tap("cretext"))
    check("переключатель выключает переписывание", store.tenants[nina.id].rewrite is False
          and "Сейчас: публикуем словами автора" in text(), text()[:300])
    await panel.on_button(client, tap("cretext"))
    check("и включает обратно", store.tenants[nina.id].rewrite is True and "cretext" in cells(), str(cells()))
    await panel.on_button(client, tap("cnext"))
    check("просмотры остаются в walkthrough", "Шаг 4 из 5 - Просмотры на пост" in text(), text()[:90])
    await panel.on_button(client, tap("cnext"))
    check("последний шаг - тариф", "Шаг 5 из 5 - Тариф и оплата" in text(), text()[:90])
    await panel.on_button(client, tap("cnext"))
    check("пройдены все шаги - «Готово»", "Готово - все шаги пройдены" in text(), text()[:90])
    check("и предлагают результат", "cresult" in cells(), str(cells()))

    # ---- экран результата: по одной настоящей причине за раз ----
    await panel.on_button(client, tap("cresult"))
    check("без тарифа названа причина", "нужен оплаченный тариф" in text(), text()[:180])
    check("и сказано, что нажать", "нажми «5️⃣ Тариф»" in text(), text()[:220])
    check("числа видены всегда", "Постов сегодня: 0 из 50" in text() and "Последний пост: постов ещё не было" in text(), text()[-300:])

    await panel.on_button(client, tap("cadd"))
    await panel.on_message(client, Message(client, 6102, nina, text="@sport0"))
    await panel.on_button(client, tap("cset_target"))
    await panel.on_message(client, Message(client, 6103, nina, text="https://t.me/nina_ch"))
    await store.add_balance(nina.id, 600)
    await store.buy(nina.id, "week")
    await panel.on_button(client, tap("cresult"))
    check("источник ещё не открыт - это видно", "ни один канал-источник не открылся" in text(), text()[:200])
    check("не открывшиеся ссылки перечислены", "Не открылись: @sport0" in text(), text()[-200:])
    store.donors[(nina.id, "sport0")].chat_id = -1004307467493
    await panel.on_button(client, tap("cresult"))
    check("без прав админа пост не выйдет", "не админ" in text(), text()[:220])
    check("рабочий аккаунт назван или обещан", "пришлю по запросу" in text(), text()[-260:])
    await store.set_target_ok(nina.id, True, "НИНА")
    await panel.on_button(client, tap("cresult"))
    check("всё готово - «Всё идёт»", "✅ Всё идёт" in text() and "⛔" not in text(), text()[:200])
    check("доступ подтверждён показан", "✅ доступ подтверждён" in text(), text()[-300:])
    await store.note_publish(nina.id, 42)
    await panel.on_button(client, tap("cresult"))
    check("после поста видно «сколько назад»", "Последний пост: только что" in text(), text()[-320:])
    store.tenants[nina.id].last_post_at = _utcnow() - datetime.timedelta(minutes=95)
    await panel.on_button(client, tap("cresult"))
    check("время считается по-человечески", "Последний пост: 1 ч назад" in text(), text()[-320:])
    store.usage[nina.id] = MAX_POSTS_PER_DAY
    await panel.on_button(client, tap("cresult"))
    check("исчерпанный лимит назван", "лимит 50 постов" in text() and "после полуночи" in text(), text()[:240])
    store.usage[nina.id] = 0
    store.tenants[nina.id].reactions_added = 0
    await panel.on_button(client, tap("cresult"))
    check("пустой замер реакций пишется прямо", "сервис не добавил ничего" in text(), text()[-300:])
    store.tenants[nina.id].reactions_added = 12
    await panel.on_button(client, tap("cresult"))
    check("добавленные реакции тоже", "сервис добавил 12" in text(), text()[-300:])

    # ---- помощник и оплата без реквизитов ----
    await panel.on_button(client, tap("cguide"))
    check("помощь объясняет каждую кнопку", "Что делает каждая кнопка" in text() and "📊 Результат" in text(), text()[:200])
    check("и порядок шагов", "1️⃣ Откуда брать посты" in text() and "5️⃣ Тариф" in text(), text()[:300])
    await store.set_agreed(nina.id)
    await panel.on_button(client, tap("ctopup"))
    check("без реквизитов обещают прислать их", "Реквизиты пришлю" in text() and "cguide" in cells(), text()[-200:])


async def check_tenants():
    print("\n[24] Клиенты в горячем пути: один рерайт - несколько каналов")
    from clients import Clients
    from store import MAX_POSTS_PER_DAY

    # интервал 3600: рабочий поток ждёт своё окно и не вынимает заказы из очереди до наших проверок
    cfg = make_cfg(promo_bot="promo_bot", promo_views=25, promo_delay=0, reactions_enabled=True, reactions_interval=3600)
    world = build_world(cfg)
    client = world["client"]
    bot, registry, ai = await make_bot(world)

    chan_a = client.add_chat(Chat(-10080, title="Клиент А", username="client_a_ch"))
    chan_b = client.add_chat(Chat(-10081, title="Клиент Б", username="client_b_ch"))
    chan_c = client.add_chat(Chat(-10082, title="Клиент В", username="client_c_ch"))
    chan_d = client.add_chat(Chat(-10083, title="Клиент Г", username="client_d_ch"))

    store = MemoryStore()
    clients = Clients(client, store)
    bot.attach(store, clients)
    # окно сервиса занято чужим заказом: рабочий поток спит, и очередь до наших проверок не разбирается
    bot.reactions._next_slot = time.monotonic() + 3600
    donor = world["donor_sport_1"]

    async def tenant(user_id, chan, views=0, reactions=True, paid=True, quota=0):
        await store.ensure_tenant(user_id)
        await store.set_target(user_id, chan.username, chan.id, chan.title)
        store.tenants[user_id].target_ok = True
        await store.set_views(user_id, views)
        await store.set_reactions(user_id, reactions)
        await store.add_donor(user_id, donor.username)
        store.donors[(user_id, donor.username)].chat_id = donor.id
        if paid:
            await store.add_balance(user_id, 600)
            await store.buy(user_id, "week")
        store.usage[user_id] = quota

    await tenant(5001, chan_a, views=1000)
    await tenant(5002, chan_b, views=0, reactions=False)
    await tenant(5003, chan_c, paid=False)
    await tenant(5004, chan_d, views=500, quota=MAX_POSTS_PER_DAY)
    await clients.sync()
    check("ловим только готовые связки", clients.chats == {donor.id}, str(clients.chats))

    first = len(client.posted)
    await bot.on_message(client, Message(client, 301, donor, caption="исходный текст поста", photo=Photo("fid-tenants")))

    check("рерайт общего поста считается один раз", ai.calls == 1, str(ai.calls))
    check("склад и два клиента получили пост", len(client.posted) - first == 3, str(len(client.posted) - first))
    targets = [message.chat.id for message in client.posted[first:]]
    check("личный склад не потерялся", world["sport"].id in targets, str(targets))
    check("у каждого клиента свой канал", {chan_a.id, chan_b.id} <= set(targets), str(targets))
    check("без тарифа пост не уходит", chan_c.id not in targets, str(targets))
    check("с исчерпанным лимитом тоже", chan_d.id not in targets, str(targets))

    numbers = [value for chat, value in client.sent if chat == "promo_bot"]
    check("клиенту идут его просмотры", "1000" in numbers, str(numbers))
    check("складу - число из переменной", "25" in numbers, str(numbers))
    check("нулевые просмотры не отправляются", "0" not in numbers, str(numbers))
    queued_links = list(bot.reactions._queue)
    check("в порядке реакций только согласившийся",
          any("client_a_ch" in link for link in queued_links) and not any("client_b_ch" in link for link in queued_links),
          str(queued_links))
    entry = next((item for item in bot.reactions._queue.values() if "client_a_ch" in item.link), None)
    check("клиенту обещан замер: есть кому вернуть дельту", entry is not None and entry.on_added is not None, str(entry))
    check("пост клиента засчитан в суточный лимит", store.usage[5001] == 1 and store.usage[5002] == 1, str(store.usage))
    check("кабинету виден номер и время поста", store.tenants[5001].last_post_id > 0 and store.tenants[5001].last_post_at is not None,
          str(store.tenants[5001].last_post_id))
    check("отказавшему клиенту ничего не записано", store.tenants[5003].last_post_at is None, str(store.tenants[5003].last_post_at))
    check("лимит не съедает промо", store.usage[5004] == MAX_POSTS_PER_DAY, str(store.usage[5004]))

    from panel import Panel

    status = Panel(client, cfg, registry, bot, store)._status_view()[0]
    check("в статусе панели видно клиентскую часть", "всего 4" in status and "действующим тарифом 3" in status, status[-260:])
    bot.clients = None
    plain = Panel(client, cfg, registry, bot, None)._status_view()[0]
    check("без базы клиентских цифр в статусе нет", "Кабинеты:" not in plain, plain[-200:])
    bot.clients = clients

    before = len(client.posted)
    await bot.on_message(client, Message(client, 302, world["stranger"], caption="пост чужого канала"))
    check("чужой канал не публикуется никому", len(client.posted) == before and ai.calls == 1, f"{len(client.posted)} / {ai.calls}")

    cards = [
        Message(client, 311, donor, video=Video("tenant-album-v1"), group="tenant-mixed"),
        Message(client, 312, donor, photo=Photo("tenant-album-p1"), group="tenant-mixed", caption="Общий видеообзор"),
        Message(client, 313, donor, video=Video("tenant-album-v2"), group="tenant-mixed"),
    ]
    client.donor_messages[donor.id] = cards
    scheduled = []
    original_schedule = bot.reactions.schedule

    def record_schedule(message, *args, **kwargs):
        scheduled.append((message.chat.id, message.id))
        return original_schedule(message, *args, **kwargs)

    bot.reactions.schedule = record_schedule
    albums_before, promo_before, ai_before = len(client.albums), len(client.forwarded), ai.calls
    published_before, sent_before = bot.stats["published"], len(client.sent)
    quota_before = dict(store.usage)
    try:
        for card in (cards[0], cards[2], cards[1], cards[0], cards[2]):
            await bot.on_message(client, card)
    finally:
        bot.reactions.schedule = original_schedule
        if bot.reactions._worker:
            bot.reactions._worker.cancel()

    albums = client.albums[albums_before:]
    check("смешанный альбом ушёл ровно по одному разу складу и двум клиентам",
          len(albums) == 3 and {album[0].chat.id for album in albums} == {world["sport"].id, chan_a.id, chan_b.id})
    check("клиентские альбомы сохранили видео, фото, видео и одну подпись",
          len(albums) == 3 and all(
              len(album) == 3 and album[0].video and album[0].video.file_id == "tenant-album-v1"
              and album[1].photo and album[1].photo.file_id == "tenant-album-p1"
              and album[2].video and album[2].video.file_id == "tenant-album-v2"
              and [item.caption or "" for item in album] == ["переписанный текст", "", ""]
              for album in albums))
    check("повтор карточек не повторяет клиентский рерайт и публикации",
          ai.calls == ai_before + 1 and bot.stats["published"] == published_before + 3)
    check("каждый клиент платит один пост квоты за весь альбом",
          store.usage[5001] == quota_before[5001] + 1 and store.usage[5002] == quota_before[5002] + 1
          and store.usage[5003] == quota_before[5003] and store.usage[5004] == quota_before[5004], str(store.usage))
    first_cards = {(album[0].chat.id, album[0].id) for album in albums}
    forwarded = client.forwarded[promo_before:]
    check("промо получает только первую карточку каждого альбома",
          len(forwarded) == 3 and {(chat_id, message_id) for _, chat_id, message_id in forwarded} == first_cards,
          str(forwarded))
    reacted = {(chat_id, message_id) for chat_id, message_id in first_cards if chat_id != chan_b.id}
    check("реакции вызываются один раз на альбом, а не на каждую карточку",
          len(scheduled) == 2 and set(scheduled) == reacted, str(scheduled))
    check("кабинет запомнил первую карточку клиентского альбома",
          any(album[0].chat.id == chan_a.id and store.tenants[5001].last_post_id == album[0].id for album in albums))
    check("клиентские видеоальбомы не дублируются одиночными или текстовыми постами",
          len(client.posted) == before and not [text for chat, text in client.sent[sent_before:]
                                                if chat in {world["sport"].id, chan_a.id, chan_b.id}])
    check("смешанные клиентские альбомы без ошибок", bot.stats["errors"] == 0, str(bot.stats))


async def check_clients_sync():
    print("\n[25] Обслуживание клиентов: подписка на источники и проверка прав")
    from clients import Clients

    cfg = make_cfg()
    world = build_world(cfg)
    client = world["client"]
    store = MemoryStore()
    clients = Clients(client, store)

    chan = world["yumor"]
    donor = world["donor_sport_2"]
    other = world["donor_other"]
    closed = client.add_chat(Chat(-10084, title="Закрытый канал", username="closed_ch"))
    client.unjoined = {donor.id}

    await store.ensure_tenant(6001)
    await store.set_target(6001, chan.username, chan.id, chan.title)
    await store.add_donor(6001, donor.username)
    await store.add_balance(6001, 600)
    await store.buy(6001, "week")

    report = await clients.sync()
    check("источник открыт подпиской", store.donors[(6001, "donor_sport_2")].chat_id == donor.id,
          str(store.donors[(6001, "donor_sport_2")].chat_id))
    check("подписка сделана ровно один раз", client.joinings == ["donor_sport_2"], str(client.joinings))
    check("имя источника взято из Telegram", store.donors[(6001, "donor_sport_2")].title == "Хоккей",
          store.donors[(6001, "donor_sport_2")].title)
    check("без заявки права не выдумываются", store.tenants[6001].target_ok is True, str(store.tenants[6001].target_ok))
    check("название канала клиента подтверждено", store.tenants[6001].target_title == "Юмор канала",
          store.tenants[6001].target_title)
    check("источник попал в ловлю", clients.chats == {donor.id}, str(clients.chats))
    check("отчёт панели конкретный", "ловим каналов: 1" in report, report)
    check("второй проход ничего не подписывает", await clients.sync() and client.joinings == ["donor_sport_2"], str(client.joinings))

    # канал, где аккаунт не админ: отметку не ставим и говорим причину
    await store.ensure_tenant(6002)
    await store.set_target(6002, "closed_ch", closed.id, "")
    client.members[closed.id] = (ChatMemberStatus.MEMBER, True)
    await clients.sync()
    check("без прав доступа не подтверждаем", store.tenants[6002].target_ok is False, str(store.tenants[6002].target_ok))
    check("причина записана человеческая", clients.problems and "не админ" in clients.problems[-1], str(clients.problems))
    check("чужой канал в ловлю не взят", clients.chats == {donor.id}, str(clients.chats))

    # права появились - та же проверка снимает проблему сама
    client.members.pop(closed.id, None)
    await clients.sync()
    check("права появились - канал подтверждён", store.tenants[6002].target_ok is True, str(store.tenants[6002].target_ok))
    check("и проблема ушла из списка", not any("closed_ch" in line for line in clients.problems), str(clients.problems))
    check("название канала клиента записано", store.tenants[6002].target_title == "Закрытый канал",
          store.tenants[6002].target_title)

    # битая ссылка и пауза Telegram: id не присваиваем, подписку откладываем
    await store.add_donor(6002, "no_such_ch")
    await clients.sync()
    check("битой ссылке не приписываем id", store.donors[(6002, "no_such_ch")].chat_id is None,
          str(store.donors[(6002, "no_such_ch")].chat_id))
    check("о битой ссылке сказано прямо", any("no_such_ch" in line for line in clients.problems), str(clients.problems))
    client.unjoined = {other.id}
    client.join_flood = 120
    await store.add_donor(6002, other.username)
    await clients.sync()
    check("Telegram просит паузу - подписку откладываем", any("паузу" in line for line in clients.problems),
          str(clients.problems))
    check("без подписки id источника не присваиваем", store.donors[(6002, "donor_other")].chat_id is None,
          str(store.donors[(6002, "donor_other")].chat_id))
    check("доклад панели начинается с времени", "последняя проверка" in clients.report(), clients.report())
    check("в докладе кабинеты и тарифы настоящие",
          "всего 2" in clients.report() and "действующим тарифом 1" in clients.report(), clients.report())
    check("неоткрытые каналы перечислены в докладе", "⚠" in clients.report(), clients.report())


async def check_money():
    print("\n[26] Деньги: заявка клиента, начисление в панели и ручной счёт")
    cfg = make_cfg(bot_token="900:AAstub", payment_details="карта 2202 0000 0000 0000")
    world = build_world(cfg)
    client = world["client"]
    bot, registry, _ = await make_bot(world)
    store = MemoryStore()
    panel = Panel(client, cfg, registry, bot, store)
    panel.setup()
    await panel.start(777, "Витя")

    owner = client.add_user(User(777, "Витя"))
    petr = client.add_user(User(5001, "Пётр"))

    def tap(data, user=petr):
        return Query(client, data, user.id, Message(client, 7000, user, text="x"))

    def text():
        return client.edits[-1][2]

    def cells():
        keyboard = client.edits[-1][3]
        return [] if keyboard is None else [button.callback_data for row in keyboard.inline_keyboard for button in row]

    def reply_text():
        return client.replies[-1][1]

    def reply_cells():
        keyboard = client.replies[-1][2]
        return [] if keyboard is None else [button.callback_data for row in keyboard.inline_keyboard for button in row]

    async def balance():
        return (await store.get_tenant(petr.id)).balance

    # ---- клиент заявляет сумму ----
    await panel.on_button(client, tap("cagree"))
    check("согласие записано", (await store.get_tenant(petr.id)).agreed is True)
    await panel.on_button(client, tap("ctopup"))
    await panel.on_message(client, Message(client, 7101, petr, text="600"))
    pending = await store.pending_topups()
    check("заявка встала в очередь", len(pending) == 1 and pending[0]["amount_rub"] == 600, str(pending))
    check("заявка денег ещё не дала", await balance() == 0, str(await balance()))

    # ---- панель владельца ----
    await panel.on_message(client, Message(client, 7102, owner, text="/start"))
    check("в меню владельца есть деньги и клиенты", "money" in reply_cells() and "clients" in reply_cells(), str(reply_cells()))
    await panel.on_button(client, tap("money", owner))
    check("экран заявок называет клиента и сумму", "клиент 5001" in text() and "600 ₽" in text(), text()[:220])
    check("у заявки есть два решения", "mpay|1" in cells() and "mno|1" in cells(), str(cells()))
    await panel.on_button(client, tap("mpay|1", owner))
    check("начислено ровно один раз", await balance() == 600 and not await store.pending_topups(), str(await balance()))
    check("очередь сказала, что пуста", "Очередь пуста" in text(), text()[:160])
    await panel.on_button(client, tap("mpay|1", owner))
    check("повторной кнопкой денег не добавить", await balance() == 600 and "уже разобрана" in text(), f"{await balance()} / {text()[:120]}")

    await panel.on_button(client, tap("ctopup"))
    await panel.on_message(client, Message(client, 7103, petr, text="300"))
    await panel.on_button(client, tap("money", owner))
    await panel.on_button(client, tap("mno|2", owner))
    row = [item for item in store.payments if item["id"] == 2][0]
    check("отклонённая заявка осталась без денег", row["status"] == "rejected" and await balance() == 600, f"{row['status']} / {await balance()}")
    history = await store.list_payments(petr.id, 5)
    check("клиент увидит статус в истории", history[0]["status"] == "rejected", str(history))

    # ---- экран клиентов и ручное начисление ----
    await panel.on_button(client, tap("clients", owner))
    check("список клиентов конкретный", "5001" in text() and "без тарифа" in text() and "600 ₽" in text(), text()[:220])
    check("у клиента есть кнопка начисления", "credit|5001" in cells(), str(cells()))
    await panel.on_button(client, tap("credit|5001", owner))
    check("панель просит сумму цифрами", "напиши сумму" in text().lower(), text()[:160])
    await panel.on_message(client, Message(client, 7104, owner, text="четыреста"))
    check("без цифр ничего не начислено", await balance() == 600 and "Нужна сумма" in reply_text(), reply_text()[:120])
    await panel.on_message(client, Message(client, 7105, owner, text="50"))
    check("сумка меньше ста отклонена", await balance() == 600 and "от 100" in reply_text(), reply_text()[:140])
    await panel.on_message(client, Message(client, 7106, owner, text="400"))
    check("ручное начисление легло на баланс", await balance() == 1000, str(await balance()))
    check("и сказано, что дальше делает клиент", "купит сам" in reply_text(), reply_text()[:200])
    check("начисление попало в историю платежей", store.payments[-1]["note"] == "начислено из панели", str(store.payments[-1]))

    # ---- клиент купил тариф начисленными деньгами ----
    await store.set_target(petr.id, "petr_ch", -100777, "Канал Петра")
    await panel.on_button(client, tap("cbuy|week"))
    check("после начисления тариф покупается", (await store.get_tenant(petr.id)).active() is True, str(await balance()))

    # ---- без базы таких экранов нет ----
    plain = Panel(client, cfg, registry, bot, None)
    await plain.start(777, "Витя")
    await plain.on_message(client, Message(client, 7107, owner, text="/start"))
    check("без базы кнопок про деньги нет", "money" not in reply_cells() and "clients" not in reply_cells(), str(reply_cells()))
    query = tap("money", owner)
    await plain.on_button(client, query)
    check("старая кнопка без базы не роняет панель", "DATABASE_URL" in query.answers[-1][0], str(query.answers))


async def check_liveness_signals():
    """Живой журнал вместо тишины: видно, доходят ли посты, и что делать, если Telegram отозвал вход."""
    print("\n[30] Доклад о ловле постов и об отзыве сессии")
    import logging

    import main as main_module
    from clients import Clients
    from pyrogram.errors import AuthKeyDuplicated

    cfg = make_cfg()
    world = build_world(cfg)
    client = world["client"]
    bot, registry, ai = await make_bot(world)

    lines: List[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Grab()
    pars_log = logging.getLogger("pars")
    pars_log.addHandler(handler)
    old_level = pars_log.level
    pars_log.setLevel(logging.INFO)

    real_sleep = asyncio.sleep
    waits: List[int] = []

    def armed_sleep():
        """Первый sleep отпускаем сразу, второй вешаем до отмены: один виток цикла за раз."""
        state = {"n": 0}

        async def fake_sleep(seconds):
            waits.append(int(seconds))
            state["n"] += 1
            if state["n"] > 1:
                await asyncio.Event().wait()
            await real_sleep(0)

        return fake_sleep

    async def run_once(coro):
        asyncio.sleep = armed_sleep()
        task = asyncio.create_task(coro)
        try:
            await real_sleep(0.05)
        finally:
            task.cancel()
            asyncio.sleep = real_sleep
            await real_sleep(0)

    original_me = client.get_me
    try:
        # ---- посты идут: считаем их, никаких проверок аккаунта не нужно ----
        bot.stats["seen"] = 3
        bot.stats["published"] = 1
        lines.clear()
        waits.clear()
        await run_once(bot.heartbeat(every=60))
        report = "\n".join(lines)
        check("виток heartbeat считает сообщения и публикации",
              "сообщений от источников 3" in report and "опубликовано постов 1" in report, report[:200])
        check("когда посты идут, аккаунт попусту не дёргаем", "Аккаунт" not in report, report[:200])
        check("heartbeat спит заданный интервал", waits[:1] == [60], str(waits[:3]))

        # ---- тишина, но аккаунт отвечает ----
        bot.stats["seen"] = 0
        bot.stats["published"] = 0
        lines.clear()
        waits.clear()
        await run_once(bot.heartbeat(every=60))
        report = "\n".join(lines)
        check("пустое окно видно в журнале", "не пришло ни одного сообщения" in report, report[:200])
        check("и сказано, что аккаунт жив", "Аккаунт отвечает" in report, report[:200])

        # ---- тишина, потому что Telegram отозвал ключ сессии ----
        async def dead_me():
            raise AuthKeyDuplicated()

        client.get_me = dead_me
        lines.clear()
        waits.clear()
        await run_once(bot.heartbeat(every=60))
        report = "\n".join(lines)
        check("отзыв сессии назван своим именем", "Аккаунт не отвечает Telegram (AuthKeyDuplicated)" in report,
              report[:250])
        check("и сказано, что делать человеку",
              "make_session.py" in report and "SESSION_STRING" in report, report[:250])
        client.get_me = original_me

        # ---- кабинеты: отозванная сессия не должна стучаться каждые пять минут ----
        class DyingClients:
            def __init__(self):
                self.tries = 0

            async def sync(self):
                self.tries += 1
                raise AuthKeyDuplicated()

        dying = DyingClients()
        waits.clear()
        lines.clear()
        await run_once(main_module.clients_loop(dying, every=300))
        report = "\n".join(lines)
        check("один отзыв - одно сообщение в журнале", report.count("Аккаунт не отвечает Telegram") == 1, report[:250])
        check("повтор не через пять минут, а через час", waits[:1] == [3600], str(waits[:3]))

        # ---- пустая клиентская очередь не дёргает Telegram вовсе ----
        calls: List[int] = []

        async def counting_me():
            calls.append(1)
            return await original_me()

        client.get_me = counting_me
        quiet = Clients(client, MemoryStore())
        confirmed = await quiet.check_targets()
        client.get_me = original_me
        check("проверять нечего - запроса к Telegram нет", confirmed == 0 and not calls, str(calls))
    finally:
        pars_log.setLevel(old_level)
        pars_log.removeHandler(handler)
        asyncio.sleep = real_sleep


async def _raises_store(action, *args):
    from store import StoreError

    try:
        await action(*args)
    except StoreError as exc:
        return exc
    except Exception as exc:  # noqa: BLE001 - нужен сам факт отказа
        return exc
    return None


if __name__ == "__main__":
    asyncio.run(main())
