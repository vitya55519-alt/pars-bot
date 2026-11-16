"""Офлайн-проверка логики бота на фейковом клиенте. Запуск: python selftest.py

Telegram и нейросеть не трогаем: подменяем клиент и aiohttp-сессию и проверяем
сборку списка из тем, маршрутизацию, публикацию по file_id, промо и команды.
"""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import List

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import ChatForwardsRestricted, FloodWait, Unauthorized
from pyrogram.types import InputMediaPhoto
from pyrogram.types import Message as _PyrogramMessage

from ai import AiRewriter
from config import Config
from handlers import ParsBot
from panel import Panel
from registry import Registry, RegistryError, extract_refs

CHECKS = []
FAILED = []


def check(name, condition, detail=""):
    CHECKS.append(name)
    if condition:
        print(f"  ok  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}  {detail}")


class Photo:
    def __init__(self, file_id):
        self.file_id = file_id


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
                 group=None, thread=None, service=False, reply_to=None, author=None):
        self.id = message_id
        self.chat = chat
        self.text = text
        self.caption = caption
        self.photo = photo
        self.media_group_id = group
        self.message_thread_id = thread
        self.service = service
        self.reply_to_message = reply_to
        self.from_user = author or User(111)
        self.reply_markup = None
        self.outgoing = False
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
        self.donor_messages = {}
        self.posted = []
        self.albums = []
        self.forwarded = []
        self.replies = []
        self.deleted = []
        self.sent = []
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
                return self.chats[self.usernames[key]]
            raise ValueError(f"PeerIdInvalid {ref}")
        if ref in self.chats:
            return self.chats[ref]
        if ref in self.users:
            return self.users[ref]
        raise ValueError(f"PeerIdInvalid {ref}")

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
        if user_id not in self.admins:
            status = ChatMemberStatus.MEMBER
        elif self.owned is None or chat_id in self.owned:
            status = ChatMemberStatus.OWNER
        else:
            status = ChatMemberStatus.ADMINISTRATOR
        return type("Member", (), {"status": status})()

    async def send_photo(self, chat, file_id, caption=None, **kwargs):
        if self.flood_left > 0:
            self.flood_left -= 1
            raise FloodWait(1)
        if not isinstance(file_id, str):
            raise AssertionError("send_photo получил объект фото - значит файл скачивается с Telegram")
        posted = Message(self, 500 + len(self.posted), await self.get_chat(chat), caption=caption, photo=Photo(file_id))
        self.posted.append(posted)
        return posted

    async def send_media_group(self, chat, media):
        if not all(isinstance(item, InputMediaPhoto) and isinstance(item.media, str) for item in media):
            raise AssertionError("в альбоме попался объект фото - скачивание")
        target = await self.get_chat(chat)
        posted = [
            Message(self, 700 + index, target, caption=item.caption or None, photo=Photo(item.media))
            for index, item in enumerate(media)
        ]
        self.albums.append(posted)
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
        return Message(self, message_id, await self.get_chat(chat), text=text)

    async def set_bot_commands(self, commands, *args, **kwargs):
        self.commands += list(commands)
        return True

    async def get_messages(self, chat, limit=None):
        return []

    async def get_chat_history(self, chat, limit=None):
        newest_first = list(reversed(self.promo_messages))
        for message in newest_first[: (limit or len(newest_first))]:
            yield message


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
        return self.responses.pop(0) if self.responses else FakeResponse(500, text="нет ответа")


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
    await check_flood_and_ai()
    await check_commands()
    await check_live_and_delete()
    await check_promo_flow()
    await check_failures()
    await check_panel()
    check_startup_wiring()
    await check_panel_login_flood()
    await check_reactions()
    await check_views()
    await check_signature()

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
        "фильтр отсекает пост без фото",
        not await donor_filter(client, Message(client, 37, world["donor_sport_1"], caption="текст")),
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


async def check_commands():
    print("\n[5] Команды в группе")
    world = build_world()
    bot, registry, _ = await make_bot(world)
    client, group = world["client"], world["group"]
    controls = bot.controls

    await controls.on_group_message(client, Message(client, 60, group, text="/список", thread=21))
    text = client.replies[-1][1]
    check("/список показывает темы и доноров", "Спорт" in text and "@donor_sport_1" in text, text)

    await controls.on_group_message(client, Message(client, 61, group, text="/обнови", thread=21))
    check("/обнови сначала отвечает", "Перечитываю" in client.replies[-2][1], client.replies[-2][1])
    check("/обнови вернул сводку", "Тема «Спорт»" in client.replies[-1][1], client.replies[-1][1])

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
    check("сводка пустой группы понятна", "нет тем" in registry3.summary().lower(), registry3.summary())

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
    broken_ref.topics[-10010] = [Topic(31, "Спорт"), Topic(32, "Нет такого канала")]
    broken_ref.group_messages = [Message(broken_ref, 1, group, text="@ghost_channel", thread=31)]
    registry6 = Registry(broken_ref, make_cfg())
    await registry6.rebuild()
    check("битая ссылка не рвёт сборку", registry6.ready and registry6.unreadable == ["ghost_channel"], str(registry6.unreadable))
    check("тема без канала помечена", "нет канала" in registry6.categories[32].problem, registry6.categories[32].problem)
    check("замечания видны в /статус", any("нет канала" in line for line in registry6.problems()), str(registry6.problems()))


async def check_panel():
    print("\n[9] Панель управления кнопками в боте")
    cfg = make_cfg(bot_token="900:AAstub")
    world = build_world(cfg)
    client = world["client"]
    group = world["group"]
    fitnes = client.add_chat(Chat(-10081, title="Фитнес", chat_type=ChatType.CHANNEL))
    yoga = client.add_chat(Chat(-10082, title="Йога", chat_type=ChatType.CHANNEL))
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
    check("меню знает группу и счётчики", "АвтоПост" in last_reply()[1] and "Каналов: 2" in last_reply()[1], last_reply()[1][:90])
    cells = [button.callback_data for row in last_reply()[2].inline_keyboard for button in row]
    check("в меню есть кнопка списков", "topics" in cells, str(cells))

    await panel.on_message(client, Message(client, 5003, stranger, text="/start"))
    check("чужому доступ закрыт", "только владельца" in last_reply()[1], last_reply()[1][:60])
    wrong = press("topics", stranger.id)
    await panel.on_button(client, wrong)
    check("чужая кнопка не исполняется", client.edits == [] and "Доступ" in wrong.answers[0][0], str(wrong.answers))

    await panel.on_button(client, press("cat|21"))
    check("экран канала показывает доноров", "Доноры (2)" in last_edit(), last_edit()[:90])
    await panel.on_button(client, press("topics"))
    check("список каналов назван", "Спорт" in last_edit() and "Юмор" in last_edit(), last_edit()[:90])
    await panel.on_button(client, press("status"))
    check("статус описывает настройки", "Промо-бот" in last_edit() and "promo_bot" in last_edit(), last_edit()[:90])
    await panel.on_button(client, press("help"))
    check("помощь объясняет кнопки", "Как пользоваться" in last_edit(), last_edit()[:60])

    await panel.on_button(client, press("del|21|1"))
    check("донор убран кнопкой", world["donor_sport_1"].id not in registry.categories[21].donors)
    check("сообщение со ссылкой удалено", 1 in client.deleted, str(client.deleted))
    check("убранное не перечитывает группу", rebuilt == [], str(rebuilt))
    check("экран подтверждает удаление", "Убрал донора" in last_edit(), last_edit()[:80])
    await panel.on_button(client, press("del|21|999"))
    check("несуществующий донор - понятный отказ", "уже нет" in last_edit(), last_edit()[:80])

    await panel.on_button(client, press("add|21"))
    check("кнопка донора просит ссылку", "Пришли ссылку" in last_edit(), last_edit()[:80])
    await panel.on_message(client, Message(client, 5004, owner, text="https://t.me/donor_run"))
    check("донор добавлен из бота", new_donor.id in registry.categories[21].donors)
    check("маршрут появился", registry.routes.get(new_donor.id) == 21, str(registry.routes))
    check("ссылка записана в тему группы", client.topic_posts[-1] == (group.id, "https://t.me/donor_run", 21), str(client.topic_posts[-1]))
    check("ответ подтверждает добавление", "Добавил доноров" in last_reply()[1], last_reply()[1][:80])

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
    check("добавление подтверждено", "добавлен" in last_reply()[1], last_reply()[1][:80])
    await panel.on_button(client, press("channel"))
    await panel.on_message(client, Message(client, 5008, owner, text="Фитнес"))
    check("дубликат канала отклонён", "уже есть" in last_reply()[1], last_reply()[1][:80])

    await panel.on_button(client, press(f"rename|{fitness_topic}"))
    await panel.on_message(client, Message(client, 5009, owner, text="Йога"))
    category = registry.categories[fitness_topic]
    check("канал переименован", category.name == "Йога" and category.target == yoga.id, f"{category.name} -> {category.target_name}")
    check("тема переименована в группе", client.topic_edits[-1] == (fitness_topic, "Йога"), str(client.topic_edits))

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
    check("кнопка склада просит название", "Напиши новое название склада" in last_edit(), last_edit()[:90])
    await panel.on_message(client, Message(client, 5013, owner, text="АвтоПост"))
    check("склад переименован кнопкой", registry.group_name == "АвтоПост" and group.title == "АвтоПост", registry.group_name)
    check("название записано в Telegram", client.titled[-1] == (group.id, "АвтоПост"), str(client.titled))
    check("переименование подтверждено", "Склад теперь называется" in last_reply()[1], last_reply()[1][:90])
    check("переименование склада не трогает темы", client.topic_edits and client.topic_deletes == [21], str(client.topic_deletes))

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


async def check_panel_login_flood():
    print("\n[11] Вход бота панели под FloodWait")
    import main as main_module

    waits: List[int] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        waits.append(int(seconds))

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
    finally:
        asyncio.sleep = real_sleep


async def check_reactions():
    print("\n[12] Бесплатные реакции на посты")
    from reactions import MAX_PENDING, FORM_URL, PERMIT_URL, Reactor, post_link

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

    broken = reactor([permit(), FakeResponse(502, text="Bad Gateway")])
    broken.schedule(published)
    await asyncio.sleep(0.05)
    check("сбой сервиса - только журнал", broken.failed == 1 and "Bad Gateway" in broken.last_error, broken.last_error)

    off_session = FakeSession([permit(), FakeResponse(200, text="OK")])
    off = Reactor(make_cfg(reactions_enabled=False), off_session)
    off.schedule(published)
    await asyncio.sleep(0.05)
    check("выключенные реакции не заказывают", off_session.calls == [] and off.ordered == 0, str(off_session.calls))

    private = reactor([])
    private.schedule(Message(client, 80, secret))
    await asyncio.sleep(0.05)
    check("пост без публичной ссылки пропущен", private.skipped == 1 and private.session.calls == [], str(private.session.calls))

    queued = FakeSession([permit(), FakeResponse(200, text="OK")])
    hour = Reactor(make_cfg(reactions_enabled=True, reactions_interval=3600), queued)
    hour.wait = 0
    hour.poll = 0
    hour.permit_limit = 0.01
    hour.schedule(published)
    hour.schedule(Message(client, 79, yumor, caption="текст", photo=Photo("r2")))
    await asyncio.sleep(0.05)
    check("первый пост заказал сразу", len(queued.calls) == 2, str(len(queued.calls)))
    check("второй ждёт своего часа", len(hour._tasks) == 1, str(len(hour._tasks)))
    for task in list(hour._tasks):
        task.cancel()
    await asyncio.sleep(0.05)
    check("отменённая очередь не успела заказать", len(queued.calls) == 2, str(len(queued.calls)))

    pile = reactor([], reactions_interval=3600)
    for index in range(MAX_PENDING + 3):
        pile.schedule(Message(client, 100 + index, yumor, caption="текст", photo=Photo(f"p{index}")))
    check(
        "очередь не растёт бесконечно",
        len(pile._tasks) == MAX_PENDING and pile.skipped == 3,
        f"{len(pile._tasks)} задач, пропущено {pile.skipped}",
    )
    for task in list(pile._tasks):
        task.cancel()
    await asyncio.sleep(0.05)

    live_cfg = make_cfg(reactions_enabled=True)
    world = build_world(live_cfg)
    live_session = FakeSession([permit(), FakeResponse(200, text="OK")])
    bot, _, _ = await make_bot(world, session=live_session)
    bot.reactions.wait = 0
    bot.reactions.poll = 0
    bot.reactions.permit_limit = 0.01
    await bot.on_message(
        world["client"], Message(world["client"], 61, world["donor_other"], caption="пост про мемы", photo=Photo("live1"))
    )
    await asyncio.sleep(0.1)
    link = live_session.calls[1][2]["link"] if len(live_session.calls) > 1 else ""
    check("после публикации заказ встаёт в очередь", link.startswith("https://t.me/yumor_ch/"), str(link))

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
    check("в меню есть кнопка реакций", "reactions" in cells, str(cells))

    await panel.on_button(pclient, press("reactions"))
    body = pclient.edits[-1][2]
    check("кнопка выключает реакции", panel_bot.reactions.enabled is False and "Реакции: выключены" in body, body[:120])
    await panel.on_button(pclient, press("reactions"))
    check("кнопка снова включает реакции", panel_bot.reactions.enabled is True and "Реакции: включены" in pclient.edits[-1][2], pclient.edits[-1][2][:120])
    await panel.on_button(pclient, press("status"))
    check("статус показывает очередь реакций", "Реакции:" in pclient.edits[-1][2] and "smmnakrutka" in pclient.edits[-1][2], pclient.edits[-1][2][:200])


async def check_views():
    print("\n[13] 👁 Число просмотров из кнопки панели")
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
    check("ноль отклонён", rejects("0").startswith("Просмотров должно быть"), rejects("0"))
    check("больше потолка отклонено", rejects(str(VIEWS_LIMIT + 1)) != "", rejects(str(VIEWS_LIMIT + 1)))
    check("не число отклонено", rejects("много").startswith("Напиши одно число"), rejects("много"))

    cfg = make_cfg(promo_views=1000)
    world = build_world(cfg)
    client, group = world["client"], world["group"]
    registry = Registry(client, cfg)
    await registry.rebuild()
    check("в складе числа нет - остаётся переменная", registry.views == 0 and cfg.promo_views == 1000, str(registry.views))

    number = await registry.save_views("3000")
    check("число применено сразу", number == 3000 and cfg.promo_views == 3000, str(cfg.promo_views))
    check("записано в общую тему склада", client.topic_posts[-1] == (group.id, "Просмотры: 3000", 1), str(client.topic_posts[-1]))
    check("служебная строка не стала донором", registry.donor_count == 3, str(registry.donor_count))

    await registry.rebuild()
    check("перезапуск берёт число из склада", registry.views == 3000 and cfg.promo_views == 3000, str(registry.views))
    await registry.save_views("2000")
    check("второй раз то же сообщение правится", client.edits[-1][2] == "Просмотры: 2000", str(client.edits[-1][2]))
    check("служебных сообщений не плодится", sum(1 for _, text, _ in client.topic_posts if "Просмотры" in text) == 1,
          str([post for post in client.topic_posts if "Просмотры" in post[1]]))
    check("плохое число не меняет настройку", (await _raises(registry.save_views, "0")) and cfg.promo_views == 2000, str(cfg.promo_views))

    live_cfg = make_cfg(promo_views=1000, promo_delay=0, reactions_enabled=False)
    live = build_world(live_cfg)
    bot, live_registry, _ = await make_bot(live)
    await live_registry.save_views("4500")
    await bot.on_message(live["client"], Message(live["client"], 61, live["donor_other"], caption="пост", photo=Photo("v1")))
    check("промо-бот получает число из панели", ("promo_bot", "4500") in live["client"].sent, str(live["client"].sent[-1]))

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

    await panel.on_message(pclient, Message(pclient, 9001, owner, text="/start"))
    cells = [button.callback_data for row in pclient.replies[-1][2].inline_keyboard for button in row]
    check("в меню есть кнопка просмотров", "views" in cells, str(cells))
    check("меню показывает текущее число", "👁 Просмотров: 1000" in str(pclient.replies[-1][2]), str(pclient.replies[-1][2])[:120])

    await panel.on_button(pclient, press("views"))
    body = pclient.edits[-1][2]
    check("экран объясняет диапазон", f"от 1 до {VIEWS_LIMIT}" in body, body[:160])
    check("экран показывает текущее число", "Сейчас: 1000 просмотров" in body, body[:80])

    await panel.on_message(pclient, Message(pclient, 9003, owner, text="3500"))
    check("введённое число применено", panel_cfg.promo_views == 3500 and panel_registry.views == 3500, str(panel_cfg.promo_views))
    check("панель это подтверждает", "Просмотры: 3500" in pclient.replies[-1][1], str(pclient.replies[-1][1][:80]))

    await panel.on_button(pclient, press("views"))
    await panel.on_message(pclient, Message(pclient, 9004, owner, text="много"))
    check("не число - подсказка, старое значение целое", "Напиши одно число" in pclient.replies[-1][1] and panel_cfg.promo_views == 3500,
          str(pclient.replies[-1][1][:80]))

    await panel.on_button(pclient, press("views|2000"))
    check("готовая кнопка ставит число", panel_cfg.promo_views == 2000 and panel_registry.views == 2000, str(panel_cfg.promo_views))
    await panel.on_button(pclient, press(f"views|{VIEWS_LIMIT + 1}"))
    check("кнопка выше потолка отклонена", panel_cfg.promo_views == 2000 and "Просмотров должно быть" in pclient.edits[-1][2],
          pclient.edits[-1][2][:80])

    await panel.on_button(pclient, press("status"))
    check("статус показывает число склада", "Просмотры в складе: 2000" in pclient.edits[-1][2], pclient.edits[-1][2][:200])


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


if __name__ == "__main__":
    asyncio.run(main())
