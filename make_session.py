"""Интерактивный вход в Telegram: создаёт SESSION_STRING и сам сохраняет её в .env.

Запуск:  python make_session.py            (ввод прямо в этом окне)
         python make_session.py --window   (открыть отдельное окно входа)

Спрашиваем номер, код и пароль сами, а не через Pyrogram: его prompts читают
ввод из пула потоков и на шаге кода теряют консоль (EOFError).
В журнал и на экран попадают только шаги - номер, код, пароль и строка сессии
нигде не печатаются.
"""

import asyncio
import ctypes
import datetime
import os
import subprocess
import sys

from dotenv import dotenv_values, set_key
from pyrogram import Client
from pyrogram.errors import AuthTokenExpired, FloodWait, SessionPasswordNeeded

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, ".env")
JOURNAL = os.path.join(ROOT, "login_journal.txt")
SESSION_FILE = os.path.join(ROOT, "login.session")
MAX_CODE_TRIES = 6


def step(text: str) -> None:
    print(text, flush=True)
    with open(JOURNAL, "a", encoding="utf-8") as handle:
        handle.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | {text}\n")


def use_console_codepage() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass


def attach_console() -> None:
    """Привязываемся к окну только если ввод реально приглушён (запуск из pipe)."""
    if os.name != "nt":
        return
    use_console_codepage()
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return
    except Exception:  # noqa: BLE001
        pass

    conin = os.open("CONIN$", os.O_RDWR)
    conout = os.open("CONOUT$", os.O_WRONLY)
    os.dup2(conin, 0)
    os.dup2(conout, 1)
    os.dup2(conout, 2)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except Exception:  # noqa: BLE001
            pass


def read_line(prompt: str) -> str:
    """Читаем строку сами: пустой ввод переспрашиваем, обрыв ввода - понятная ошибка."""
    while True:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line == "":
            raise EOFError("консоль закрыла ввод")
        value = line.strip()
        if value:
            return value
        print("Пусто - напиши значение.")


def ask(label: str) -> str:
    value = read_line(f"{label}: ")
    print(f"{label}: принято ({len(value)} символов).")
    return value


def read_secret(prompt: str) -> str:
    """Пароль вводим скрыто; если скрытый ввод недоступен - обычным способом."""
    try:
        import getpass

        value = getpass.getpass(prompt).strip()
        if value:
            return value
    except Exception:  # noqa: BLE001
        pass
    return read_line(prompt)


def read_env(name: str) -> str:
    return (dotenv_values(ENV_FILE).get(name) or os.environ.get(name) or "").strip()


def save_env(**values: str) -> None:
    if not os.path.exists(ENV_FILE):
        open(ENV_FILE, "w", encoding="utf-8").close()
    for key, value in values.items():
        set_key(ENV_FILE, key, value, quote_mode="never")


def describe_delivery(sent_code) -> str:
    media = getattr(sent_code, "type", None)
    marker = f"{type(media).__name__} {getattr(media, 'name', '')}".lower()
    if "sms" in marker and "flash" not in marker:
        return "СМС на номер - проверь СМС от Telegram"
    if "flash" in marker:
        return "пропущенным звонком"
    if "call" in marker:
        return "звонком"
    if "email" in marker:
        return "письмом на почту аккаунта - проверь почту от Telegram"
    if "app" in marker:
        return (
            "сообщением в самом Telegram: открой чат «Telegram» на устройстве, где уже выполнен вход. "
            "Если кода там нет - напиши в поле кода sms, и я запрошу обычную СМС"
        )
    return f"неизвестным способом ({marker.strip()}) - напиши в поле кода sms"


async def flood_aware(func, *args, **kwargs):
    """Telegram может попросить паузу между запросами кода - ждём молча и повторяем."""
    while True:
        try:
            return await func(*args, **kwargs)
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0)
            step(f"Telegram просит подождать {wait} сек между запросами кода - жду, потом запрошу заново")
            await asyncio.sleep(wait + 1)


async def request_sms(client: Client, phone: str, sent):
    """Telegram сам выбирает, чем отправить код, поэтому просим выдачу заново."""
    try:
        return await flood_aware(client.resend_code, phone, sent.phone_code_hash)
    except Exception as exc:  # noqa: BLE001 - отказ повторной выдачи не должен обрывать вход
        step(f"Новый код запросить не удалось ({type(exc).__name__}) - попробуй тот, что уже приходил")
        return sent


async def authenticate(client: Client) -> None:
    phone = ask("Номер телефона (полностью, например +79001234567)")
    step(f"Запрашиваю код для номера оканчивающегося на {phone[-2:]}")
    sent = await flood_aware(client.send_code, phone)
    step(f"Код отправлен: {describe_delivery(sent)}")

    for try_number in range(1, MAX_CODE_TRIES + 1):
        code = read_line(f"Код из Telegram (попытка {try_number}/{MAX_CODE_TRIES}, «sms» - запросить новый код): ")
        if code.lower() in {"sms", "смс", "s"}:
            sent = await request_sms(client, phone, sent)
            step(f"Попросил Telegram выдать код СМС: {describe_delivery(sent)}")
            continue

        step(f"Код принят ({len(code)} символов), подтверждаю в Telegram")
        try:
            await client.sign_in(phone, sent.phone_code_hash, code)
            return
        except SessionPasswordNeeded:
            hint = await client.get_password_hint()
            step(f"Аккаунт защищает облачный пароль (подсказка: {hint or 'нет'})")
            password = read_secret("Облачный пароль: ")
            await client.check_password(password)
            step("Облачный пароль принят")
            return
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 0) or 0)
            step(f"Telegram просит подождать {wait} сек перед новой попыткой")
            await asyncio.sleep(wait + 1)
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if "Expired" in name:
                sent = await request_sms(client, phone, sent)
                step(f"Код устарел, запросил новый: {describe_delivery(sent)}")
                continue
            if "Invalid" in name or "Code" in name:
                step(f"Telegram отказал ({name}) - введи код ещё раз, он в уведомлении")
                continue
            step(f"Telegram отказал ({name}) на шаге подтверждения кода")
            raise

    step(f"Код так и не подтвердился после {MAX_CODE_TRIES} попыток")
    raise SystemExit(1)


QR_HINT = (
    "Сканируешь этим телефоном, под тем аккаунтом, который станет ботом:\n"
    "Telegram -> Настройки -> Конфиденциальность -> Активные сеансы -> Сканировать QR-код.\n"
    "Если в телефоне два аккаунта, Telegram спросит, под каким войти."
)


async def authenticate_by_qr(client: Client) -> None:
    """Вход по QR: Telegram не выдаёт коды, поэтому ждёт сканирования картинки."""
    try:
        import tkinter as tk
        from PIL import ImageTk
        from qrcode import QRCode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        step(f"Для входа по QR не хватает модуля ({type(exc).__name__}) - напиши мне, я доустановлю")
        raise SystemExit(1)

    from pyrogram.qrlogin import QRLogin

    qr_login = QRLogin(client)
    root = tk.Tk()
    root.title("Вход по QR-коду")
    picture = tk.Label(root)
    picture.pack(padx=12, pady=(12, 4))
    tk.Label(root, text=QR_HINT, wraplength=460, justify="left").pack(padx=12, pady=(0, 12))
    root.update()

    step("Показал окно с QR-кодом - сканируй его из приложения Telegram (не камерой телефона).")
    try:
        while True:
            await qr_login.recreate()
            code = QRCode(version=1, box_size=8, border=3, error_correction=ERROR_CORRECT_M)
            code.add_data(qr_login.url)
            code.make(fit=True)
            picture._photo = ImageTk.PhotoImage(code.make_image().convert("RGB"))
            picture.configure(image=picture._photo)
            root.update()

            left = max(15, int(qr_login.r.expires - datetime.datetime.now().timestamp()) - 3)
            task = asyncio.ensure_future(qr_login.wait(timeout=left))
            while not task.done():
                root.update()
                await asyncio.sleep(0.1)

            try:
                user = task.result()
            except (asyncio.TimeoutError, AuthTokenExpired):
                step("QR-код истёк - показываю новый, отсканируй его.")
                continue

            if user:
                step(f"QR подтверждён, вошли под аккаунтом id {user.id}")
                return
    finally:
        root.destroy()


async def save_bot_token() -> None:
    """Один раз сохраняем ключ управления-бота и проверяем его без показа значения."""
    import aiohttp

    token = read_secret("Ключ бота из BotFather (вводимые символы не видны): ")
    if ":" not in token:
        step("Похоже, это не ключ: в ключе есть двоеточие, например 123456:AA...")
        return

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url) as response:
                data = await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - адрес содержит ключ, поэтому показываем только тип
        step(f"Telegram не ответил ({type(exc).__name__}) - повтори позже")
        return

    if not data.get("ok"):
        step(f"Telegram отверг ключ: {data.get('description', 'без описания')}")
        return

    user = data["result"]
    save_env(BOT_TOKEN=token)
    step(f"Ключ сохранён в {ENV_FILE}. Бот: {user.get('first_name')} @{user.get('username')} (id {user.get('id')})")
    step("Готово. Окно можно закрывать.")


async def run() -> None:
    if "--token" in sys.argv:
        await save_bot_token()
        return

    api_id = read_env("API_ID") or ask("API_ID (с my.telegram.org)")
    api_hash = read_env("API_HASH") or ask("API_HASH")
    # Сохраняем сразу: при сбое входа эти две строки можно будет не вводить заново.
    save_env(API_ID=api_id, API_HASH=api_hash)

    client = Client(name=os.path.join(ROOT, "login"), api_id=int(api_id), api_hash=api_hash)
    try:
        is_authorized = await client.connect()
        if is_authorized:
            step("Эта сессия уже авторизована - использую её")
        else:
            if "--qr" in sys.argv:
                await authenticate_by_qr(client)
            else:
                await authenticate(client)
        await client.initialize()

        me = await client.get_me()
        step(f"Вошли как: {me.first_name or ''} @{me.username or '-'} (id {me.id})")
        session_string = await client.export_session_string()
        save_env(SESSION_STRING=session_string)
        step(f"SESSION_STRING сохранена в {ENV_FILE} ({len(session_string)} символов).")
        step("Готово. Окно можно закрывать.")
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        hint = {
            "PhoneNumberInvalid": " - номер нужен в формате +79187947858",
            "SessionExpired": " - Telegram выдал новый код, повтори вход",
            "ApiIdInvalid": " - API_ID или API_HASH не те, проверь на my.telegram.org",
            "SessionRevoked": " - сессию отозвали, повтори вход",
            "AuthKeyUnregistered": " - этот аккаунт разлогинен, повтори вход",
        }.get(name, "")
        step(f"Вход не удался: {name}{hint}")
        if "AuthKey" in name or "Session" in name:
            try:
                os.remove(SESSION_FILE)
                step("Удалил испорченный файл локальной сессии - следующая попытка начнётся чисто")
            except OSError:
                pass
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    flags = [arg for arg in sys.argv[1:] if arg.startswith("--") and arg != "--window"]
    if "--window" in sys.argv:
        use_console_codepage()
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)] + flags,
            cwd=ROOT,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        step("Открыл отдельное окно входа - вводи номер и код там.")
        return

    attach_console()
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, EOFError) as exc:
        step(f"Вход прерван ({type(exc).__name__}) - окно можно закрыть и запустить заново")
    finally:
        try:
            read_line("\nОкно можно закрыть (нажми Enter). ")
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    main()
