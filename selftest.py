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

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
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


async def make_bot(world, ai=None):
    cfg, client = world["cfg"], world["client"]
    registry = Registry(client, cfg)
    ai = ai or StubAI()
    bot = ParsBot(client, cfg, ai, registry)
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
    bot2 = ParsBot(world2["client"], cfg, AiRewriter(cfg, session), Registry(world2["client"], cfg))
    await bot2.setup()
    await bot2.on_message(world2["client"], Message(world2["client"], 52, world2["donor_sport_1"], caption="про спорт", photo=Photo("k1")))
    check("нейросеть повторила до успеха", len(session.calls) == 3, str(len(session.calls)))
    check("опубликован ответ нейросети", world2["client"].posted[0].caption == "готовый текст", world2["client"].posted[0].caption)

    dead = FakeSession([FakeResponse(502, text="") for _ in range(5)])
    cfg3 = make_cfg(minimax_api_key="key")
    world3 = build_world(cfg3)
    bot3 = ParsBot(world3["client"], cfg3, AiRewriter(cfg3, dead), Registry(world3["client"], cfg3))
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
    bot4 = ParsBot(world4["client"], cfg4, AiRewriter(cfg4, broke), Registry(world4["client"], cfg4))
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
    bot = ParsBot(client, cfg, StubAI(), Registry(client, cfg))
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

    check("нажата «Просмотры», а не «Авто просмотры»", menu.clicks == [(0, None), (0, None)], str(menu.clicks))
    check("посты пересланы промо-боту", [f[0] for f in client.forwarded] == ["promo_bot", "promo_bot"], str(client.forwarded))
    check("число просмотров отправлено", client.sent == [("promo_bot", "4100"), ("promo_bot", "4100")], str(client.sent))
    check("промо засчитано обоим постам", bot.stats["forwarded"] == 2, str(bot.stats))

    cfg2 = make_cfg(promo_button="", promo_views=0, promo_delay=0)
    world2 = build_world(cfg2)
    client2 = world2["client"]
    bot2 = ParsBot(client2, cfg2, StubAI(), Registry(client2, cfg2))
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

    lonely = FakeClient()
    boss = lonely.add_user(User(777, "Витя"))
    lonely_cfg = make_cfg(bot_token="900:AAstub")
    lonely_registry = Registry(lonely, lonely_cfg)
    lonely_bot = ParsBot(lonely, lonely_cfg, StubAI(), lonely_registry)
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


if __name__ == "__main__":
    asyncio.run(main())
