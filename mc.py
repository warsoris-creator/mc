"""
mc.py — Бот защиты сети чатов (aiogram 2.x)

Запуск:
    BOT_TOKEN=<токен> python mc.py
    или укажите токен напрямую в константе TOKEN ниже.
"""
import logging
import sqlite3
import time
import asyncio
import re
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import ParseMode, ChatPermissions
from aiogram.utils import executor
from aiogram.utils.exceptions import (
    BadRequest, Forbidden, MessageToDeleteNotFound
)

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════
TOKEN      = os.getenv("BOT_TOKEN", "6376776916:AAGnNP_GoorQS7wZkLhg0snutRPBJttmz70")
OWNER_ID   = 382254550
ADMIN_IDS  = {382254550}          # суперадмины бота (set для O(1) lookup)
CHANNEL_ID = "-1001672973157"     # канал для проверки подписки
DB_PATH    = "forbidden_words.db"

MAX_WARNINGS  = 3      # предупреждений до авто-мута
MUTE_SECONDS  = 3600   # 1 час — длительность мута по умолчанию
FLOOD_LIMIT   = 5      # макс сообщений...
FLOOD_WINDOW  = 10     # ...за N секунд

# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════
conn   = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()


def init_db():
    cursor.executescript("""
        -- Запрещённые слова: scope='network' — вся сеть, иначе chat_id конкретного чата
        CREATE TABLE IF NOT EXISTS forbidden_words (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            word     TEXT    NOT NULL,
            scope    TEXT    NOT NULL DEFAULT 'network',
            added_by INTEGER,
            added_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_word_scope
            ON forbidden_words(word, scope);

        -- Предупреждения пользователей
        CREATE TABLE IF NOT EXISTS warnings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            chat_id   INTEGER NOT NULL,
            reason    TEXT,
            warned_by INTEGER,
            warned_at TEXT DEFAULT (datetime('now'))
        );

        -- Активные муты
        CREATE TABLE IF NOT EXISTS muted_users (
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            muted_until TEXT,
            PRIMARY KEY (user_id, chat_id)
        );

        -- Настройки каждого чата
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id      INTEGER PRIMARY KEY,
            sub_check    INTEGER DEFAULT 0,
            anti_flood   INTEGER DEFAULT 1,
            anti_forward INTEGER DEFAULT 0,
            anti_links   INTEGER DEFAULT 1,
            max_warnings INTEGER DEFAULT 3
        );

        -- Лог нарушений
        CREATE TABLE IF NOT EXISTS violation_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            chat_id  INTEGER,
            vtype    TEXT,
            msg_text TEXT,
            ts       TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


# ══════════════════════════════════════════════════════════════
#  HELPERS — БД
# ══════════════════════════════════════════════════════════════

def get_forbidden_words(chat_id=None):
    """Сетевые слова + слова конкретного чата."""
    if chat_id:
        cursor.execute(
            "SELECT word FROM forbidden_words WHERE scope='network' OR scope=?",
            (str(chat_id),)
        )
    else:
        cursor.execute("SELECT word FROM forbidden_words WHERE scope='network'")
    return [r[0].lower() for r in cursor.fetchall()]


def contains_forbidden(text: str, words: list):
    """Поиск запрещённого слова с учётом пунктуации. Возвращает слово или None."""
    clean = re.sub(r'[^\w\s]', ' ', text.lower())
    for w in words:
        pattern = r'\b' + re.escape(w.lower()) + r'\b'
        if re.search(pattern, clean):
            return w
    return None


def get_warnings(user_id, chat_id):
    cursor.execute(
        "SELECT COUNT(*) FROM warnings WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    )
    return cursor.fetchone()[0]


def add_warning(user_id, chat_id, reason, warned_by):
    cursor.execute(
        "INSERT INTO warnings (user_id,chat_id,reason,warned_by) VALUES (?,?,?,?)",
        (user_id, chat_id, reason, warned_by)
    )
    conn.commit()
    return get_warnings(user_id, chat_id)


def clear_warnings(user_id, chat_id):
    cursor.execute(
        "DELETE FROM warnings WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    )
    conn.commit()


def get_setting(chat_id, key):
    cursor.execute(f"SELECT {key} FROM chat_settings WHERE chat_id=?", (chat_id,))
    row = cursor.fetchone()
    if row:
        return row[0]
    defaults = dict(sub_check=0, anti_flood=1, anti_forward=0,
                    anti_links=1, max_warnings=3)
    return defaults.get(key, 0)


def set_setting(chat_id, key, value):
    cursor.execute(
        "INSERT OR IGNORE INTO chat_settings(chat_id) VALUES(?)", (chat_id,)
    )
    cursor.execute(
        f"UPDATE chat_settings SET {key}=? WHERE chat_id=?", (value, chat_id)
    )
    conn.commit()


def log_violation(user_id, chat_id, vtype, text=''):
    cursor.execute(
        "INSERT INTO violation_log(user_id,chat_id,vtype,msg_text) VALUES(?,?,?,?)",
        (user_id, chat_id, vtype, text[:500])
    )
    conn.commit()


# ══════════════════════════════════════════════════════════════
#  BOT + HELPERS — TELEGRAM
# ══════════════════════════════════════════════════════════════
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp  = Dispatcher(bot)

# Флуд-трекер: {(user_id, chat_id): [timestamps]}
_flood: dict = {}


def is_flooding(user_id, chat_id) -> bool:
    key = (user_id, chat_id)
    now = time.time()
    _flood[key] = [t for t in _flood.get(key, []) if now - t < FLOOD_WINDOW]
    _flood[key].append(now)
    return len(_flood[key]) > FLOOD_LIMIT


async def safe_delete(chat_id, message_id):
    try:
        await bot.delete_message(chat_id, message_id)
    except (MessageToDeleteNotFound, BadRequest, Forbidden):
        pass


async def auto_delete(chat_id, message_id, delay=15):
    await asyncio.sleep(delay)
    await safe_delete(chat_id, message_id)


async def is_admin(chat_id, user_id) -> bool:
    """True если пользователь — суперадмин бота ИЛИ администратор/владелец чата."""
    if user_id in ADMIN_IDS:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ('administrator', 'creator')
    except Exception:
        return False


def parse_duration(s: str) -> int:
    """'30m' -> 1800, '2h' -> 7200, '1d' -> 86400. По умолчанию MUTE_SECONDS."""
    m = re.match(r'^(\d+)([mhd])$', s.strip().lower())
    if not m:
        return MUTE_SECONDS
    n, u = int(m.group(1)), m.group(2)
    return n * {'m': 60, 'h': 3600, 'd': 86400}[u]


async def do_mute(chat_id, user_id, seconds: int) -> bool:
    until = datetime.now() + timedelta(seconds=seconds)
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        cursor.execute(
            "INSERT OR REPLACE INTO muted_users(user_id,chat_id,muted_until)"
            " VALUES(?,?,?)",
            (user_id, chat_id, until.isoformat())
        )
        conn.commit()
        return True
    except (BadRequest, Forbidden):
        return False


def mention(user: types.User) -> str:
    name = user.full_name or user.username or str(user.id)
    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def get_target(message: types.Message):
    """Получить цель команды: из reply или из аргумента @username / id."""
    if message.reply_to_message:
        return message.reply_to_message.from_user
    args = message.get_args().split()
    if args:
        arg = args[0]
        try:
            member = await bot.get_chat_member(message.chat.id, arg)
            return member.user
        except Exception:
            pass
    return None


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ — ОБЩИЕ
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if uid in ADMIN_IDS:
        await message.reply("Привет, владелец! Бот активен и защищает сеть.")
    elif await is_admin(message.chat.id, uid):
        await message.reply("Привет, администратор!")
    else:
        await message.reply("Привет! Я бот-защитник этой сети чатов.")


@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    uid    = message.from_user.id
    is_adm = await is_admin(message.chat.id, uid)

    user_text = (
        "<b>Команды</b>\n"
        "/start — приветствие\n"
        "/status — ваш статус\n"
        "/view_words — список запрещённых слов\n"
        "/help — эта справка\n"
    )

    admin_text = (
        "\n<b>Модерация</b>\n"
        "/warn [reply|@user] [причина] — предупреждение\n"
        "/warnings [reply|@user] — кол-во предупреждений\n"
        "/clearwarns [reply|@user] — сбросить предупреждения\n"
        "/mute [reply|@user] [время] — замутить (1h / 30m / 1d)\n"
        "/unmute [reply|@user] — размутить\n"
        "/kick [reply|@user] — кикнуть\n"
        "/ban [reply|@user] — забанить\n"
        "/unban [reply|@user] — разбанить\n"
        "\n<b>Запрещённые слова</b>\n"
        "/add_word &lt;слово&gt; — добавить во ВСЮ сеть (90 чатов)\n"
        "/del_word &lt;слово&gt; — удалить из сети\n"
        "/add_word_here &lt;слово&gt; — только в этот чат\n"
        "/del_word_here &lt;слово&gt; — удалить из этого чата\n"
        "/view_words — список слов\n"
        "\n<b>Настройки чата</b>\n"
        "/settings — текущие настройки\n"
        "/anti_links on|off — блокировка ссылок\n"
        "/anti_flood on|off — защита от флуда\n"
        "/anti_forward on|off — блокировка пересылок\n"
        "/on_sub | /off_sub — проверка подписки на канал\n"
        "/stats — статистика нарушений\n"
    )

    await message.reply(user_text + (admin_text if is_adm else ""))


@dp.message_handler(commands=['status'])
async def cmd_status(message: types.Message):
    uid = message.from_user.id
    if uid in ADMIN_IDS:
        role = "Суперадмин бота"
    elif await is_admin(message.chat.id, uid):
        role = "Администратор чата"
    else:
        role = "Пользователь"
    warns = get_warnings(uid, message.chat.id)
    max_w = get_setting(message.chat.id, 'max_warnings')
    await message.reply(
        f"Статус: {role}\n"
        f"Предупреждения: {warns}/{max_w}"
    )


# ══════════════════════════════════════════════════════════════
#  ЗАПРЕЩЁННЫЕ СЛОВА
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['add_word'])
async def cmd_add_word(message: types.Message):
    """Добавить слово в сеть всех чатов."""
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /add_word &lt;слово&gt;")
    try:
        cursor.execute(
            "INSERT INTO forbidden_words(word,scope,added_by) VALUES(?,?,?)",
            (word, 'network', message.from_user.id)
        )
        conn.commit()
        await message.reply(
            f"Слово <b>{word}</b> добавлено во <b>всю сеть</b> (90 чатов)."
        )
    except sqlite3.IntegrityError:
        await message.reply(f"Слово <b>{word}</b> уже есть в сети.")


@dp.message_handler(commands=['del_word'])
async def cmd_del_word(message: types.Message):
    """Удалить слово из сети."""
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /del_word &lt;слово&gt;")
    cursor.execute(
        "DELETE FROM forbidden_words WHERE word=? AND scope='network'", (word,)
    )
    conn.commit()
    if cursor.rowcount:
        await message.reply(f"Слово <b>{word}</b> удалено из сети.")
    else:
        await message.reply(f"Слово <b>{word}</b> не найдено в сети.")


@dp.message_handler(commands=['add_word_here'])
async def cmd_add_word_here(message: types.Message):
    """Добавить слово только в этот чат."""
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /add_word_here &lt;слово&gt;")
    try:
        cursor.execute(
            "INSERT INTO forbidden_words(word,scope,added_by) VALUES(?,?,?)",
            (word, str(message.chat.id), message.from_user.id)
        )
        conn.commit()
        await message.reply(f"Слово <b>{word}</b> добавлено только в этот чат.")
    except sqlite3.IntegrityError:
        await message.reply(f"Слово <b>{word}</b> уже есть.")


@dp.message_handler(commands=['del_word_here'])
async def cmd_del_word_here(message: types.Message):
    """Удалить слово из этого чата."""
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /del_word_here &lt;слово&gt;")
    cursor.execute(
        "DELETE FROM forbidden_words WHERE word=? AND scope=?",
        (word, str(message.chat.id))
    )
    conn.commit()
    if cursor.rowcount:
        await message.reply(f"Слово <b>{word}</b> удалено из этого чата.")
    else:
        await message.reply(f"Слово <b>{word}</b> не найдено.")


@dp.message_handler(commands=['view_words'])
async def cmd_view_words(message: types.Message):
    words = get_forbidden_words(message.chat.id)
    if not words:
        resp = await message.reply("Список запрещённых слов пуст.")
    else:
        text = "<b>Запрещённые слова:</b>\n" + "\n".join(
            f"- {w}" for w in sorted(words)
        )
        resp = await message.reply(text)
    asyncio.create_task(auto_delete(message.chat.id, resp.message_id))


# ══════════════════════════════════════════════════════════════
#  МОДЕРАЦИЯ
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['warn'])
async def cmd_warn(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    if target.is_bot or target.id in ADMIN_IDS:
        return await message.reply("Нельзя предупредить бота или суперадмина.")

    args   = message.get_args().split()
    reason = ' '.join(args[1:] if not message.reply_to_message else args) or 'нарушение правил'

    max_w = get_setting(message.chat.id, 'max_warnings')
    count = add_warning(target.id, message.chat.id, reason, message.from_user.id)
    log_violation(target.id, message.chat.id, 'warn', reason)

    if count >= max_w:
        clear_warnings(target.id, message.chat.id)
        await do_mute(message.chat.id, target.id, MUTE_SECONDS)
        action = f"Автоматически замучен на 1 час (лимит {max_w} предупреждений)."
    else:
        action = f"Предупреждений: {count}/{max_w}"

    await message.reply(
        f"{mention(target)} получает предупреждение!\n"
        f"Причина: {reason}\n{action}"
    )


@dp.message_handler(commands=['warnings'])
async def cmd_warnings(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    count = get_warnings(target.id, message.chat.id)
    max_w = get_setting(message.chat.id, 'max_warnings')
    await message.reply(f"{mention(target)}: {count}/{max_w} предупреждений.")


@dp.message_handler(commands=['clearwarns'])
async def cmd_clearwarns(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    clear_warnings(target.id, message.chat.id)
    await message.reply(f"Предупреждения {mention(target)} сброшены.")


@dp.message_handler(commands=['mute'])
async def cmd_mute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    if target.id in ADMIN_IDS:
        return await message.reply("Нельзя замутить суперадмина.")

    args    = message.get_args().split()
    dur_str = args[-1] if args else '1h'
    seconds = parse_duration(dur_str)

    ok = await do_mute(message.chat.id, target.id, seconds)
    if ok:
        await message.reply(f"{mention(target)} замучен на <b>{dur_str}</b>.")
        log_violation(target.id, message.chat.id, 'mute')
    else:
        await message.reply(
            "Не удалось замутить. Проверьте права бота (нужен Ban users)."
        )


@dp.message_handler(commands=['unmute'])
async def cmd_unmute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        cursor.execute(
            "DELETE FROM muted_users WHERE user_id=? AND chat_id=?",
            (target.id, message.chat.id)
        )
        conn.commit()
        await message.reply(f"{mention(target)} размучен.")
    except (BadRequest, Forbidden):
        await message.reply("Не удалось размутить.")


@dp.message_handler(commands=['kick'])
async def cmd_kick(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    if target.id in ADMIN_IDS:
        return await message.reply("Нельзя кикнуть суперадмина.")
    try:
        await bot.kick_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)  # kick без перманентного бана
        await message.reply(f"{mention(target)} выгнан из чата.")
        log_violation(target.id, message.chat.id, 'kick')
    except (BadRequest, Forbidden):
        await message.reply("Не удалось кикнуть. Проверьте права бота.")


@dp.message_handler(commands=['ban'])
async def cmd_ban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    if target.id in ADMIN_IDS:
        return await message.reply("Нельзя забанить суперадмина.")
    try:
        await bot.kick_chat_member(message.chat.id, target.id)
        await message.reply(f"{mention(target)} заблокирован в чате.")
        log_violation(target.id, message.chat.id, 'ban')
    except (BadRequest, Forbidden):
        await message.reply("Не удалось забанить. Проверьте права бота.")


@dp.message_handler(commands=['unban'])
async def cmd_unban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @пользователя."
        )
    try:
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"{mention(target)} разбанен.")
    except (BadRequest, Forbidden):
        await message.reply("Не удалось разбанить.")


# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ ЧАТА
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['settings'])
async def cmd_settings(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    cid = message.chat.id
    lines = ["<b>Настройки чата</b>"]
    params = [
        ('sub_check',    'Проверка подписки'),
        ('anti_flood',   'Антифлуд'),
        ('anti_forward', 'Блок пересылок из каналов'),
        ('anti_links',   'Блок ссылок'),
    ]
    for key, label in params:
        val = get_setting(cid, key)
        lines.append(f"- {label}: {'вкл' if val else 'выкл'}")
    lines.append(f"- Макс. предупреждений: {get_setting(cid, 'max_warnings')}")
    await message.reply('\n'.join(lines))


def _make_toggle(command_key, label):
    async def handler(message: types.Message):
        if not await is_admin(message.chat.id, message.from_user.id):
            return
        arg = message.get_args().strip().lower()
        if arg in ('on', '1', 'вкл'):
            val = 1
        elif arg in ('off', '0', 'выкл'):
            val = 0
        else:
            return await message.reply(f"Использование: /{command_key} on|off")
        set_setting(message.chat.id, command_key, val)
        state = "включена" if val else "отключена"
        await message.reply(f"{label} {state}.")
    handler.__name__ = f"toggle_{command_key}"
    return handler


dp.message_handler(commands=['anti_links'])(
    _make_toggle('anti_links', 'Блокировка ссылок')
)
dp.message_handler(commands=['anti_flood'])(
    _make_toggle('anti_flood', 'Антифлуд')
)
dp.message_handler(commands=['anti_forward'])(
    _make_toggle('anti_forward', 'Блокировка пересылок')
)


@dp.message_handler(commands=['on_sub'])
async def cmd_on_sub(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    set_setting(message.chat.id, 'sub_check', 1)
    await message.reply("Проверка подписки включена.")


@dp.message_handler(commands=['off_sub'])
async def cmd_off_sub(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    set_setting(message.chat.id, 'sub_check', 0)
    await message.reply("Проверка подписки отключена.")


@dp.message_handler(commands=['stats'])
async def cmd_stats(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    cid = message.chat.id

    cursor.execute(
        "SELECT vtype, COUNT(*) FROM violation_log WHERE chat_id=? GROUP BY vtype",
        (cid,)
    )
    viol_rows = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM forbidden_words WHERE scope='network'")
    net_words = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM forbidden_words WHERE scope=?", (str(cid),)
    )
    chat_words = cursor.fetchone()[0]

    lines = [
        "<b>Статистика</b>",
        f"Слов в сети: <b>{net_words}</b>  |  В этом чате: <b>{chat_words}</b>",
    ]
    if viol_rows:
        lines.append("\n<b>Нарушения в этом чате:</b>")
        for vtype, cnt in viol_rows:
            lines.append(f"  {vtype}: {cnt}")
    else:
        lines.append("Нарушений не зафиксировано.")

    await message.reply('\n'.join(lines))


# ══════════════════════════════════════════════════════════════
#  ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ══════════════════════════════════════════════════════════════

ALL_CONTENT = [
    types.ContentType.TEXT,
    types.ContentType.PHOTO,
    types.ContentType.VIDEO,
    types.ContentType.DOCUMENT,
    types.ContentType.STICKER,
    types.ContentType.VOICE,
    types.ContentType.VIDEO_NOTE,
]


@dp.message_handler(content_types=ALL_CONTENT)
async def process_message(message: types.Message):
    user = message.from_user
    chat = message.chat

    if not user or user.is_bot:
        return
    if chat.type not in (types.ChatType.GROUP, types.ChatType.SUPERGROUP):
        return

    uid   = user.id
    cid   = chat.id
    admin = await is_admin(cid, uid)

    # -- Антифлуд --------------------------------------------------
    if not admin and get_setting(cid, 'anti_flood') and is_flooding(uid, cid):
        await safe_delete(cid, message.message_id)
        ok = await do_mute(cid, uid, 300)  # 5 минут
        if ok:
            resp = await bot.send_message(
                cid,
                f"{mention(user)}, флуд запрещён. Мут на 5 минут."
            )
            asyncio.create_task(auto_delete(cid, resp.message_id))
        log_violation(uid, cid, 'flood')
        return

    # -- Блок пересылок из каналов ---------------------------------
    if not admin and get_setting(cid, 'anti_forward') and message.forward_from_chat:
        await safe_delete(cid, message.message_id)
        resp = await bot.send_message(
            cid,
            f"{mention(user)}, пересылки из каналов запрещены в этом чате."
        )
        asyncio.create_task(auto_delete(cid, resp.message_id))
        log_violation(uid, cid, 'forward')
        return

    # Дальнейшие проверки только для текстовых сообщений
    if message.content_type != types.ContentType.TEXT:
        return

    text = message.text or ''

    # -- Блок ссылок -----------------------------------------------
    if not admin and get_setting(cid, 'anti_links'):
        entities = message.entities or []
        if any(e.type in ('url', 'text_link') for e in entities):
            await safe_delete(cid, message.message_id)
            resp = await bot.send_message(
                cid,
                f"{mention(user)}, ссылки запрещены.\n"
                f"По вопросам рекламы — @supx100"
            )
            asyncio.create_task(auto_delete(cid, resp.message_id))
            log_violation(uid, cid, 'link', text)
            return

    # -- Запрещённые слова -----------------------------------------
    if not admin:
        words = get_forbidden_words(cid)
        found = contains_forbidden(text, words)
        if found:
            await safe_delete(cid, message.message_id)
            max_w = get_setting(cid, 'max_warnings')
            count = add_warning(uid, cid, f'запрещённое слово: {found}', 0)
            log_violation(uid, cid, 'forbidden_word', text)

            if count >= max_w:
                clear_warnings(uid, cid)
                await do_mute(cid, uid, MUTE_SECONDS)
                resp = await bot.send_message(
                    cid,
                    f"{mention(user)}, сообщение удалено (запрещённое слово).\n"
                    f"Мут на 1 час — достигнут лимит {max_w} предупреждений."
                )
            else:
                resp = await bot.send_message(
                    cid,
                    f"{mention(user)}, сообщение содержит запрещённое слово и удалено.\n"
                    f"Предупреждение {count}/{max_w}. Список слов: /view_words"
                )
            asyncio.create_task(auto_delete(cid, resp.message_id))
            return

    # -- Проверка подписки -----------------------------------------
    if not admin and get_setting(cid, 'sub_check'):
        try:
            member     = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=uid)
            subscribed = member.status not in ('left', 'kicked')
        except Exception:
            subscribed = True  # если канал недоступен — не блокируем

        if not subscribed:
            await safe_delete(cid, message.message_id)
            resp = await bot.send_message(
                cid,
                f"{mention(user)}, для отправки сообщений подпишитесь на канал:\n"
                f"https://t.me/aktive_chats"
            )
            asyncio.create_task(auto_delete(cid, resp.message_id, 20))


# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    init_db()
    dp.middleware.setup(LoggingMiddleware())
    logging.info("Бот запущен. Защита сети активна.")
    executor.start_polling(dp, skip_updates=True)
