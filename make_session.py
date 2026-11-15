"""Интерактивный вход в Telegram: создаёт SESSION_STRING и сам сохраняет её в .env.

Запуск:  python make_session.py            (ввод прямо в этом окне)
         python make_session.py --window   (открыть отдельное окно входа)

В журнал и в чат попадают только шаги и имя аккаунта - номер, код, пароль и
строка сессии нигде не печатаются.
"""

import asyncio
import ctypes
import datetime
import os
import subprocess
import sys

from dotenv import dotenv_values, set_key
from pyrogram import Client

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, ".env")
JOURNAL = os.path.join(ROOT, "login_journal.txt")


def step(text: str) -> None:
    print(text, flush=True)
    with open(JOURNAL, "a", encoding="utf-8") as handle:
        handle.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | {text}\n")


def attach_console() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
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


def ask(label: str, secret: bool = False) -> str:
    if secret:
        import getpass

        value = getpass.getpass(f"{label}: ").strip()
    else:
        value = input(f"{label}: ").strip()
    if not value:
        step(f"{label}: пусто - прервано.")
        raise SystemExit(1)
    return value


def read_env(name: str) -> str:
    return (dotenv_values(ENV_FILE).get(name) or os.environ.get(name) or "").strip()


def save_env(**values: str) -> None:
    if not os.path.exists(ENV_FILE):
        open(ENV_FILE, "w", encoding="utf-8").close()
    for key, value in values.items():
        set_key(ENV_FILE, key, value, quote_mode="never")


async def run() -> None:
    api_id = read_env("API_ID") or ask("API_ID (с my.telegram.org)")
    api_hash = read_env("API_HASH") or ask("API_HASH")
    step("Получаю код у Telegram - проверь уведомления в установленном приложении Telegram.")

    client = Client(name=os.path.join(ROOT, "login"), api_id=int(api_id), api_hash=api_hash)
    try:
        await client.start()
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "PhoneNumberInvalid" in type(exc).__name__ or "PHONE_NUMBER" in str(exc).upper():
            hint = " Номер нужен в формате +79001234567."
        elif "CodeInvalid" in type(exc).__name__:
            hint = " Код из уведомления ввёлся неверно или истёк - повтори вход."
        elif "Password" in type(exc).__name__:
            hint = " Нужен облачный пароль от аккаунта."
        elif "SessionRevoke" in type(exc).__name__ or "Auth" in type(exc).__name__:
            hint = " Сбрось все сессии в настройках Telegram и повтори вход."
        step(f"Вход не удался: {type(exc).__name__}.{hint}")
        raise SystemExit(1)

    me = await client.get_me()
    step(f"Вошли как: {me.first_name or ''} @{me.username or '-'} (id {me.id})")
    session_string = await client.export_session_string()
    save_env(API_ID=api_id, API_HASH=api_hash, SESSION_STRING=session_string)
    step(f"SESSION_STRING сохранена в {ENV_FILE} ({len(session_string)} символов).")
    await client.stop()
    step("Готово. Окно можно закрывать.")


def main() -> None:
    if "--window" in sys.argv:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            cwd=ROOT,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        step("Открыл отдельное окно входа - вводи номер и код там.")
        return

    attach_console()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        step("Вход прерван (закрыто окно).")


if __name__ == "__main__":
    main()
