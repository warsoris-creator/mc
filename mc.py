"""
mc.py — Бот защиты сети чатов (aiogram 2.x) — УЛУЧШЕННАЯ ВЕРСИЯ

Новое:
  - Пакетное добавление запрещённых слов через Enter (/add_words + многострочный список)
  - /makeadmin — выдача прав администратора через reply или @username
  - Капча при входе: бот пишет в ЛС новому участнику, при провале — кик
  - Inline-кнопки в /settings, /help, /stats
  - Улучшенный /help с разделами
  - /rmadmin — снятие прав суперадмина бота
  - /bot_admins — список суперадминов бота

Запуск:
    BOT_TOKEN=<токен> python mc.py
"""

import logging
import aiogram
import sqlite3
import time
import asyncio
import re
import os
import random
import string
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import (
    ParseMode, ChatPermissions,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils import executor
from aiogram.utils.exceptions import (
    BadRequest, MessageToDeleteNotFound, BotBlocked, ChatNotFound
)

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════
TOKEN      = os.getenv("BOT_TOKEN", "6376776916:AAGnNP_GoorQS7wZkLhg0snutRPBJttmz70")
OWNER_ID   = 382254550
ADMIN_IDS  = {382254550}          # суперадмины бота (runtime-изменяемо)
CHANNEL_ID = "-1001672973157"     # канал для проверки подписки
DB_PATH    = "forbidden_words.db"

MAX_WARNINGS  = 3
MUTE_SECONDS  = 3600
FLOOD_LIMIT   = 5
FLOOD_WINDOW  = 10

# Капча: время на прохождение (сек)
CAPTCHA_TIMEOUT = 120

# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════
conn   = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()


def init_db():
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS forbidden_words (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            word     TEXT    NOT NULL,
            scope    TEXT    NOT NULL DEFAULT 'network',
            added_by INTEGER,
            added_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_word_scope
            ON forbidden_words(word, scope);

        CREATE TABLE IF NOT EXISTS warnings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            chat_id   INTEGER NOT NULL,
            reason    TEXT,
            warned_by INTEGER,
            warned_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS muted_users (
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            muted_until TEXT,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id      INTEGER PRIMARY KEY,
            sub_check    INTEGER DEFAULT 0,
            anti_flood   INTEGER DEFAULT 1,
            anti_forward INTEGER DEFAULT 0,
            anti_links   INTEGER DEFAULT 1,
            captcha      INTEGER DEFAULT 0,
            max_warnings INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS violation_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            chat_id  INTEGER,
            vtype    TEXT,
            msg_text TEXT,
            ts       TEXT DEFAULT (datetime('now'))
        );

        -- Хранение капч ожидающих пользователей
        CREATE TABLE IF NOT EXISTS captcha_pending (
            user_id    INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            code       TEXT    NOT NULL,
            created_at INTEGER NOT NULL,
            dm_msg_id  INTEGER,
            PRIMARY KEY (user_id, chat_id)
        );

        -- Суперадмины бота (персистентно)
        CREATE TABLE IF NOT EXISTS bot_admins (
            user_id  INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    # Загрузить сохранённых суперадминов в runtime-set
    for row in cursor.execute("SELECT user_id FROM bot_admins").fetchall():
        ADMIN_IDS.add(row[0])
    # Убедиться что владелец всегда в БД
    cursor.execute(
        "INSERT OR IGNORE INTO bot_admins(user_id, added_by) VALUES(?,?)",
        (OWNER_ID, OWNER_ID)
    )
    conn.commit()


# ══════════════════════════════════════════════════════════════
#  HELPERS — БД
# ══════════════════════════════════════════════════════════════

def get_forbidden_words(chat_id=None):
    if chat_id:
        cursor.execute(
            "SELECT word FROM forbidden_words WHERE scope='network' OR scope=?",
            (str(chat_id),)
        )
    else:
        cursor.execute("SELECT word FROM forbidden_words WHERE scope='network'")
    return [r[0].lower() for r in cursor.fetchall()]


def contains_forbidden(text: str, words: list):
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
                    anti_links=1, captcha=0, max_warnings=3)
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
    if user_id in ADMIN_IDS:
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ('administrator', 'creator')
    except Exception:
        return False


def parse_duration(s: str) -> int:
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


def mention_by_id(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{name}</a>'


async def get_target(message: types.Message):
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
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════

def settings_keyboard(chat_id):
    af  = '✅' if get_setting(chat_id, 'anti_flood')   else '❌'
    al  = '✅' if get_setting(chat_id, 'anti_links')   else '❌'
    afw = '✅' if get_setting(chat_id, 'anti_forward') else '❌'
    sc  = '✅' if get_setting(chat_id, 'sub_check')    else '❌'
    cap = '✅' if get_setting(chat_id, 'captcha')      else '❌'
    mw  = get_setting(chat_id, 'max_warnings')

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"{af} Антифлуд",       callback_data=f"toggle:anti_flood:{chat_id}"),
        InlineKeyboardButton(f"{al} Блок ссылок",    callback_data=f"toggle:anti_links:{chat_id}"),
        InlineKeyboardButton(f"{afw} Блок пересылок",callback_data=f"toggle:anti_forward:{chat_id}"),
        InlineKeyboardButton(f"{sc} Проверка подп.", callback_data=f"toggle:sub_check:{chat_id}"),
        InlineKeyboardButton(f"{cap} Капча",         callback_data=f"toggle:captcha:{chat_id}"),
        InlineKeyboardButton(f"⚠️ Макс. варн: {mw}", callback_data=f"warns_menu:{chat_id}"),
    )
    kb.add(InlineKeyboardButton("🔄 Обновить", callback_data=f"settings_refresh:{chat_id}"))
    return kb


def warns_keyboard(chat_id):
    kb = InlineKeyboardMarkup(row_width=3)
    for n in [1, 2, 3, 5, 7, 10]:
        kb.insert(InlineKeyboardButton(str(n), callback_data=f"set_warns:{n}:{chat_id}"))
    kb.add(InlineKeyboardButton("← Назад", callback_data=f"settings_refresh:{chat_id}"))
    return kb


def help_keyboard(is_adm: bool):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📖 Общие", callback_data="help:general"))
    if is_adm:
        kb.add(
            InlineKeyboardButton("🛡️ Модерация",     callback_data="help:mod"),
            InlineKeyboardButton("🚫 Слова",          callback_data="help:words"),
            InlineKeyboardButton("⚙️ Настройки",      callback_data="help:settings"),
            InlineKeyboardButton("👑 Суперадмины",    callback_data="help:admins"),
        )
    return kb


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ — ОБЩИЕ
# ══════════════════════════════════════════════════════════════

HELP_SECTIONS = {
    "general": (
        "📖 <b>Общие команды</b>\n\n"
        "/start — приветствие\n"
        "/status — ваш статус и предупреждения\n"
        "/view_words — список запрещённых слов\n"
        "/help — эта справка"
    ),
    "mod": (
        "🛡️ <b>Модерация</b>\n\n"
        "/warn [reply|@user] [причина] — предупреждение\n"
        "/warnings [reply|@user] — количество варнов\n"
        "/clearwarns [reply|@user] — сбросить предупреждения\n"
        "/mute [reply|@user] [время] — замутить (1h / 30m / 1d)\n"
        "/unmute [reply|@user] — размутить\n"
        "/kick [reply|@user] — кикнуть\n"
        "/ban [reply|@user] — забанить\n"
        "/unban [reply|@user] — разбанить\n"
        "/stats — статистика нарушений"
    ),
    "words": (
        "🚫 <b>Запрещённые слова</b>\n\n"
        "/add_word &lt;слово&gt; — добавить в сеть (90 чатов)\n"
        "/add_words — добавить пачкой (каждое слово с новой строки)\n"
        "/del_word &lt;слово&gt; — удалить из сети\n"
        "/add_word_here &lt;слово&gt; — только в этот чат\n"
        "/add_words_here — пачкой только в этот чат\n"
        "/del_word_here &lt;слово&gt; — удалить из этого чата\n"
        "/view_words — посмотреть список\n\n"
        "<i>Пример пакетного добавления:</i>\n"
        "<code>/add_words\nслово1\nслово2\nслово3</code>"
    ),
    "settings": (
        "⚙️ <b>Настройки чата</b>\n\n"
        "/settings — панель настроек с кнопками\n"
        "/anti_links on|off — блокировка ссылок\n"
        "/anti_flood on|off — защита от флуда\n"
        "/anti_forward on|off — блокировка пересылок\n"
        "/captcha on|off — капча при входе\n"
        "/on_sub | /off_sub — проверка подписки на канал"
    ),
    "admins": (
        "👑 <b>Управление суперадминами</b>\n\n"
        "/makeadmin [reply|@user] — выдать права суперадмина бота\n"
        "/rmadmin [reply|@user] — снять права суперадмина бота\n"
        "/bot_admins — список суперадминов бота\n\n"
        "<i>Только владелец бота может выдавать права.</i>"
    ),
}


@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if uid == OWNER_ID:
        role = "👑 Владелец"
    elif uid in ADMIN_IDS:
        role = "🌟 Суперадмин бота"
    elif await is_admin(message.chat.id, uid):
        role = "🛡️ Администратор чата"
    else:
        role = "👤 Пользователь"

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📖 Помощь", callback_data="help:general"))

    await message.reply(
        f"Привет! Я <b>бот-защитник</b> этой сети чатов.\n"
        f"Ваша роль: <b>{role}</b>\n\n"
        f"Используйте /help для справки по командам.",
        reply_markup=kb
    )


@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    is_adm = await is_admin(message.chat.id, message.from_user.id)
    await message.reply(
        "Выберите раздел помощи 👇",
        reply_markup=help_keyboard(is_adm)
    )


@dp.callback_query_handler(lambda c: c.data.startswith("help:"))
async def cb_help(call: types.CallbackQuery):
    section = call.data.split(":")[1]
    text = HELP_SECTIONS.get(section, "Раздел не найден.")
    is_adm = await is_admin(call.message.chat.id, call.from_user.id)
    await call.message.edit_text(text, reply_markup=help_keyboard(is_adm))
    await call.answer()


@dp.message_handler(commands=['status'])
async def cmd_status(message: types.Message):
    uid = message.from_user.id
    if uid == OWNER_ID:
        role = "👑 Владелец"
    elif uid in ADMIN_IDS:
        role = "🌟 Суперадмин бота"
    elif await is_admin(message.chat.id, uid):
        role = "🛡️ Администратор чата"
    else:
        role = "👤 Пользователь"

    warns = get_warnings(uid, message.chat.id)
    max_w = get_setting(message.chat.id, 'max_warnings')
    warns_bar = "🟥" * warns + "⬜" * (max_w - warns)

    await message.reply(
        f"<b>Ваш статус</b>\n"
        f"Роль: {role}\n"
        f"Предупреждения: {warns}/{max_w} {warns_bar}"
    )


# ══════════════════════════════════════════════════════════════
#  СУПЕРАДМИНЫ БОТА
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['makeadmin'])
async def cmd_makeadmin(message: types.Message):
    """Выдать права суперадмина бота — только для владельца."""
    if message.from_user.id != OWNER_ID:
        return await message.reply("❌ Только владелец бота может выдавать права суперадмина.")

    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение пользователя или укажите @username / ID.\n"
            "Пример: /makeadmin @username"
        )
    if target.id == OWNER_ID:
        return await message.reply("Владелец уже имеет все права.")
    if target.is_bot:
        return await message.reply("Нельзя выдать права боту.")

    ADMIN_IDS.add(target.id)
    cursor.execute(
        "INSERT OR IGNORE INTO bot_admins(user_id, added_by) VALUES(?,?)",
        (target.id, message.from_user.id)
    )
    conn.commit()

    await message.reply(
        f"✅ {mention(target)} назначен <b>суперадмином бота</b>.\n"
        f"Он получил доступ ко всем командам управления во всех чатах сети."
    )

    # Уведомить нового суперадмина в ЛС
    try:
        await bot.send_message(
            target.id,
            f"🎉 Вы назначены <b>суперадмином бота</b> в сети чатов!\n"
            f"Вам доступны все команды модерации. Используйте /help."
        )
    except (BotBlocked, Forbidden, BadRequest):
        pass


@dp.message_handler(commands=['rmadmin'])
async def cmd_rmadmin(message: types.Message):
    """Снять права суперадмина бота — только для владельца."""
    if message.from_user.id != OWNER_ID:
        return await message.reply("❌ Только владелец бота может снимать права.")

    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @username.")
    if target.id == OWNER_ID:
        return await message.reply("Нельзя снять права с владельца.")

    ADMIN_IDS.discard(target.id)
    cursor.execute("DELETE FROM bot_admins WHERE user_id=?", (target.id,))
    conn.commit()

    await message.reply(f"✅ Права суперадмина бота у {mention(target)} сняты.")


@dp.message_handler(commands=['bot_admins'])
async def cmd_bot_admins(message: types.Message):
    """Список суперадминов бота."""
    if not await is_admin(message.chat.id, message.from_user.id):
        return

    rows = cursor.execute(
        "SELECT user_id, added_at FROM bot_admins ORDER BY added_at"
    ).fetchall()

    if not rows:
        return await message.reply("Список суперадминов пуст.")

    lines = ["<b>👑 Суперадмины бота:</b>"]
    for uid, added_at in rows:
        mark = "👑" if uid == OWNER_ID else "🌟"
        date = added_at[:10] if added_at else "?"
        lines.append(f"{mark} <a href=\"tg://user?id={uid}\">{uid}</a> — с {date}")

    await message.reply('\n'.join(lines))


# ══════════════════════════════════════════════════════════════
#  ЗАПРЕЩЁННЫЕ СЛОВА — ОДИНОЧНЫЕ
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['add_word'])
async def cmd_add_word(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply(
            "Использование: /add_word &lt;слово&gt;\n"
            "Для пачки слов используйте /add_words"
        )
    try:
        cursor.execute(
            "INSERT INTO forbidden_words(word,scope,added_by) VALUES(?,?,?)",
            (word, 'network', message.from_user.id)
        )
        conn.commit()
        await message.reply(f"✅ Слово <b>{word}</b> добавлено во <b>всю сеть</b>.")
    except sqlite3.IntegrityError:
        await message.reply(f"⚠️ Слово <b>{word}</b> уже есть в сети.")


@dp.message_handler(commands=['del_word'])
async def cmd_del_word(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /del_word &lt;слово&gt;")
    cursor.execute(
        "DELETE FROM forbidden_words WHERE word=? AND scope='network'", (word,)
    )
    conn.commit()
    if cursor.rowcount:
        await message.reply(f"✅ Слово <b>{word}</b> удалено из сети.")
    else:
        await message.reply(f"⚠️ Слово <b>{word}</b> не найдено в сети.")


@dp.message_handler(commands=['add_word_here'])
async def cmd_add_word_here(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /add_word_here &lt;слово&gt;")
    try:
        cursor.execute(
            "INSERT INTO forbidden_words(word,scope,added_by) VALUES(?,?,?)",
            (word, str(message.chat.id), message.from_user.id)
        )
        conn.commit()
        await message.reply(f"✅ Слово <b>{word}</b> добавлено только в этот чат.")
    except sqlite3.IntegrityError:
        await message.reply(f"⚠️ Слово <b>{word}</b> уже есть.")


@dp.message_handler(commands=['del_word_here'])
async def cmd_del_word_here(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /del_word_here &lt;слово&gt;")
    cursor.execute(
        "DELETE FROM forbidden_words WHERE word=? AND scope=?",
        (word, str(message.chat.id))
    )
    conn.commit()
    if cursor.rowcount:
        await message.reply(f"✅ Слово <b>{word}</b> удалено из этого чата.")
    else:
        await message.reply(f"⚠️ Слово <b>{word}</b> не найдено.")


# ══════════════════════════════════════════════════════════════
#  ЗАПРЕЩЁННЫЕ СЛОВА — ПАЧКОЙ (через Enter)
# ══════════════════════════════════════════════════════════════

def _parse_bulk_words(text: str) -> list:
    """Разбить текст на список слов — каждое слово с новой строки."""
    lines = text.strip().splitlines()
    words = []
    for line in lines:
        w = line.strip().lower()
        if w and not w.startswith('/'):
            words.append(w)
    return words


@dp.message_handler(commands=['add_words'])
async def cmd_add_words_bulk(message: types.Message):
    """
    Добавить сразу несколько слов в сеть.
    Формат: /add_words (новая строка) слово1 (новая строка) слово2 ...
    """
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")

    # Всё, что после команды (включая многострочное)
    raw = message.text.partition('\n')[2]  # всё после первой строки
    if not raw.strip():
        return await message.reply(
            "Укажите слова — каждое с новой строки:\n\n"
            "<code>/add_words\nшлюха\nпизда\nматерное_слово</code>"
        )

    words = _parse_bulk_words(raw)
    if not words:
        return await message.reply("Не нашёл слов для добавления.")

    added, skipped = [], []
    for word in words:
        try:
            cursor.execute(
                "INSERT INTO forbidden_words(word,scope,added_by) VALUES(?,?,?)",
                (word, 'network', message.from_user.id)
            )
            added.append(word)
        except sqlite3.IntegrityError:
            skipped.append(word)
    conn.commit()

    lines = [f"✅ Добавлено во всю сеть: <b>{len(added)}</b> слов"]
    if added:
        lines.append("Добавлены: " + ", ".join(f"<code>{w}</code>" for w in added))
    if skipped:
        lines.append(f"⚠️ Уже были: " + ", ".join(f"<code>{w}</code>" for w in skipped))

    await message.reply('\n'.join(lines))


@dp.message_handler(commands=['add_words_here'])
async def cmd_add_words_here_bulk(message: types.Message):
    """Добавить пачку слов только в этот чат."""
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")

    raw = message.text.partition('\n')[2]
    if not raw.strip():
        return await message.reply(
            "Укажите слова — каждое с новой строки:\n\n"
            "<code>/add_words_here\nслово1\nслово2</code>"
        )

    words = _parse_bulk_words(raw)
    if not words:
        return await message.reply("Не нашёл слов для добавления.")

    added, skipped = [], []
    for word in words:
        try:
            cursor.execute(
                "INSERT INTO forbidden_words(word,scope,added_by) VALUES(?,?,?)",
                (word, str(message.chat.id), message.from_user.id)
            )
            added.append(word)
        except sqlite3.IntegrityError:
            skipped.append(word)
    conn.commit()

    lines = [f"✅ Добавлено в этот чат: <b>{len(added)}</b> слов"]
    if added:
        lines.append("Добавлены: " + ", ".join(f"<code>{w}</code>" for w in added))
    if skipped:
        lines.append("⚠️ Уже были: " + ", ".join(f"<code>{w}</code>" for w in skipped))

    await message.reply('\n'.join(lines))


@dp.message_handler(commands=['view_words'])
async def cmd_view_words(message: types.Message):
    cid   = message.chat.id
    net   = [r[0] for r in cursor.execute(
        "SELECT word FROM forbidden_words WHERE scope='network' ORDER BY word"
    ).fetchall()]
    local = [r[0] for r in cursor.execute(
        "SELECT word FROM forbidden_words WHERE scope=? ORDER BY word",
        (str(cid),)
    ).fetchall()]

    if not net and not local:
        resp = await message.reply("📋 Список запрещённых слов пуст.")
    else:
        lines = ["<b>🚫 Запрещённые слова</b>"]
        if net:
            lines.append(f"\n<b>Вся сеть ({len(net)}):</b>")
            lines.append(", ".join(f"<code>{w}</code>" for w in net))
        if local:
            lines.append(f"\n<b>Только этот чат ({len(local)}):</b>")
            lines.append(", ".join(f"<code>{w}</code>" for w in local))
        resp = await message.reply('\n'.join(lines))

    asyncio.create_task(auto_delete(cid, resp.message_id, 30))


# ══════════════════════════════════════════════════════════════
#  КАПЧА
# ══════════════════════════════════════════════════════════════

def _gen_captcha_code(length=6) -> str:
    """Генерирует случайный код из цифр."""
    return ''.join(random.choices(string.digits, k=length))


async def _captcha_timeout(user_id, chat_id, code):
    """Через CAPTCHA_TIMEOUT секунд проверяем — прошёл ли пользователь капчу."""
    await asyncio.sleep(CAPTCHA_TIMEOUT)

    row = cursor.execute(
        "SELECT code FROM captcha_pending WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    ).fetchone()

    if row and row[0] == code:
        # Не прошёл — кикаем
        cursor.execute(
            "DELETE FROM captcha_pending WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        conn.commit()
        try:
            await bot.kick_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)  # разбан чтобы мог вернуться
        except (BadRequest, Forbidden):
            pass

        # Уведомление в чате
        try:
            chat = await bot.get_chat(chat_id)
            msg = await bot.send_message(
                chat_id,
                f"🚫 Пользователь не прошёл капчу за {CAPTCHA_TIMEOUT} сек и был кикнут."
            )
            asyncio.create_task(auto_delete(chat_id, msg.message_id, 30))
        except Exception:
            pass

        # Пишем в ЛС что кикнули
        try:
            await bot.send_message(
                user_id,
                f"❌ Вы не прошли капчу за {CAPTCHA_TIMEOUT} секунд и были удалены из чата.\n"
                f"Вы можете вернуться и попробовать снова."
            )
        except (BotBlocked, Forbidden, BadRequest):
            pass


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def on_new_member(message: types.Message):
    """Обработчик входа новых участников."""
    chat_id = message.chat.id

    if not get_setting(chat_id, 'captcha'):
        return  # капча выключена

    for user in message.new_chat_members:
        if user.is_bot:
            continue

        uid  = user.id
        code = _gen_captcha_code()

        # Замутить до прохождения капчи
        try:
            await bot.restrict_chat_member(
                chat_id, uid,
                permissions=ChatPermissions(can_send_messages=False)
            )
        except (BadRequest, Forbidden):
            pass

        # Сохранить в БД
        cursor.execute(
            "INSERT OR REPLACE INTO captcha_pending(user_id,chat_id,code,created_at)"
            " VALUES(?,?,?,?)",
            (uid, chat_id, code, int(time.time()))
        )
        conn.commit()

        # Написать в ЛС
        dm_sent = False
        try:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton(
                "✅ Я не бот — ввожу код в группе",
                url=f"https://t.me/{(await bot.get_me()).username}?start=captcha"
            ))
            await bot.send_message(
                uid,
                f"👋 Привет! Вы вступили в чат.\n\n"
                f"Для подтверждения того, что вы не бот, введите в <b>группе</b> следующий код:\n\n"
                f"<code>{code}</code>\n\n"
                f"⏱ У вас есть <b>{CAPTCHA_TIMEOUT} секунд</b>.\n"
                f"Если не введёте — вас кикнут автоматически.",
                reply_markup=kb
            )
            dm_sent = True
        except (BotBlocked, Forbidden, BadRequest):
            dm_sent = False

        # Сообщение в чате
        if dm_sent:
            chat_msg = await message.reply(
                f"👋 {mention(user)}, добро пожаловать!\n\n"
                f"🔐 Для верификации проверьте <b>личные сообщения от бота</b> — "
                f"там есть код, который нужно написать здесь.\n"
                f"⏱ Время: {CAPTCHA_TIMEOUT} сек."
            )
        else:
            # ЛС заблокированы — показываем код прямо в чате
            chat_msg = await message.reply(
                f"👋 {mention(user)}, добро пожаловать!\n\n"
                f"🔐 Введите этот код для верификации:\n<code>{code}</code>\n\n"
                f"⏱ У вас есть {CAPTCHA_TIMEOUT} секунд."
            )

        asyncio.create_task(_captcha_timeout(uid, chat_id, code))
        asyncio.create_task(auto_delete(chat_id, chat_msg.message_id, CAPTCHA_TIMEOUT + 5))


# ══════════════════════════════════════════════════════════════
#  МОДЕРАЦИЯ
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['warn'])
async def cmd_warn(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
    if target.is_bot or target.id in ADMIN_IDS:
        return await message.reply("Нельзя предупредить бота или суперадмина.")

    args   = message.get_args().split()
    reason = ' '.join(args[1:] if not message.reply_to_message else args) or 'нарушение правил'

    max_w = get_setting(message.chat.id, 'max_warnings')
    count = add_warning(target.id, message.chat.id, reason, message.from_user.id)
    log_violation(target.id, message.chat.id, 'warn', reason)
    bar = "🟥" * count + "⬜" * (max_w - count)

    if count >= max_w:
        clear_warnings(target.id, message.chat.id)
        await do_mute(message.chat.id, target.id, MUTE_SECONDS)
        action = f"🔇 Автоматически замучен на 1 час (достигнут лимит {max_w})."
    else:
        action = f"Варны: {count}/{max_w} {bar}"

    await message.reply(
        f"⚠️ {mention(target)} получает предупреждение!\n"
        f"Причина: <i>{reason}</i>\n{action}"
    )


@dp.message_handler(commands=['warnings'])
async def cmd_warnings(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
    count = get_warnings(target.id, message.chat.id)
    max_w = get_setting(message.chat.id, 'max_warnings')
    bar   = "🟥" * count + "⬜" * (max_w - count)
    await message.reply(f"⚠️ {mention(target)}: {count}/{max_w} {bar}")


@dp.message_handler(commands=['clearwarns'])
async def cmd_clearwarns(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
    clear_warnings(target.id, message.chat.id)
    await message.reply(f"✅ Предупреждения {mention(target)} сброшены.")


@dp.message_handler(commands=['mute'])
async def cmd_mute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
    if target.id in ADMIN_IDS:
        return await message.reply("Нельзя замутить суперадмина.")

    args    = message.get_args().split()
    dur_str = args[-1] if args else '1h'
    seconds = parse_duration(dur_str)

    ok = await do_mute(message.chat.id, target.id, seconds)
    if ok:
        await message.reply(f"🔇 {mention(target)} замучен на <b>{dur_str}</b>.")
        log_violation(target.id, message.chat.id, 'mute')
    else:
        await message.reply("Не удалось замутить. Проверьте права бота (нужен Ban users).")


@dp.message_handler(commands=['unmute'])
async def cmd_unmute(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
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
        await message.reply(f"🔊 {mention(target)} размучен.")
    except (BadRequest, Forbidden):
        await message.reply("Не удалось размутить.")


@dp.message_handler(commands=['kick'])
async def cmd_kick(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
    if target.id in ADMIN_IDS:
        return await message.reply("Нельзя кикнуть суперадмина.")
    try:
        await bot.kick_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {mention(target)} выгнан из чата.")
        log_violation(target.id, message.chat.id, 'kick')
    except (BadRequest, Forbidden):
        await message.reply("Не удалось кикнуть. Проверьте права бота.")


@dp.message_handler(commands=['ban'])
async def cmd_ban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
    if target.id in ADMIN_IDS:
        return await message.reply("Нельзя забанить суперадмина.")
    try:
        await bot.kick_chat_member(message.chat.id, target.id)
        await message.reply(f"🔨 {mention(target)} заблокирован в чате.")
        log_violation(target.id, message.chat.id, 'ban')
    except (BadRequest, Forbidden):
        await message.reply("Не удалось забанить. Проверьте права бота.")


@dp.message_handler(commands=['unban'])
async def cmd_unban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение или укажите @пользователя.")
    try:
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {mention(target)} разбанен.")
    except (BadRequest, Forbidden):
        await message.reply("Не удалось разбанить.")


# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ ЧАТА — Inline панель
# ══════════════════════════════════════════════════════════════

SETTING_LABELS = {
    'anti_flood':   'Антифлуд',
    'anti_links':   'Блок ссылок',
    'anti_forward': 'Блок пересылок',
    'sub_check':    'Проверка подписки',
    'captcha':      'Капча',
}


@dp.message_handler(commands=['settings'])
async def cmd_settings(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    cid = message.chat.id
    await message.reply(
        "<b>⚙️ Настройки чата</b>\nНажмите на кнопку чтобы переключить:",
        reply_markup=settings_keyboard(cid)
    )


@dp.callback_query_handler(lambda c: c.data.startswith("toggle:"))
async def cb_toggle(call: types.CallbackQuery):
    _, key, chat_id_str = call.data.split(":")
    chat_id = int(chat_id_str)

    if not await is_admin(chat_id, call.from_user.id):
        return await call.answer("❌ Нет прав.", show_alert=True)

    current = get_setting(chat_id, key)
    new_val  = 0 if current else 1
    set_setting(chat_id, key, new_val)

    label = SETTING_LABELS.get(key, key)
    state = "включён ✅" if new_val else "выключен ❌"
    await call.answer(f"{label} {state}")

    await call.message.edit_reply_markup(reply_markup=settings_keyboard(chat_id))


@dp.callback_query_handler(lambda c: c.data.startswith("settings_refresh:"))
async def cb_settings_refresh(call: types.CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    if not await is_admin(chat_id, call.from_user.id):
        return await call.answer("❌ Нет прав.", show_alert=True)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(chat_id))
    await call.answer("Обновлено")


@dp.callback_query_handler(lambda c: c.data.startswith("warns_menu:"))
async def cb_warns_menu(call: types.CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    if not await is_admin(chat_id, call.from_user.id):
        return await call.answer("❌ Нет прав.", show_alert=True)
    await call.message.edit_text(
        "<b>⚠️ Выберите максимальное количество предупреждений:</b>",
        reply_markup=warns_keyboard(chat_id)
    )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("set_warns:"))
async def cb_set_warns(call: types.CallbackQuery):
    _, n_str, chat_id_str = call.data.split(":")
    chat_id = int(chat_id_str)
    n = int(n_str)

    if not await is_admin(chat_id, call.from_user.id):
        return await call.answer("❌ Нет прав.", show_alert=True)

    set_setting(chat_id, 'max_warnings', n)
    await call.answer(f"Лимит установлен: {n}")
    await call.message.edit_text(
        "<b>⚙️ Настройки чата</b>\nНажмите на кнопку чтобы переключить:",
        reply_markup=settings_keyboard(chat_id)
    )


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
        state = "включена ✅" if val else "отключена ❌"
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
dp.message_handler(commands=['captcha'])(
    _make_toggle('captcha', 'Капча при входе')
)


@dp.message_handler(commands=['on_sub'])
async def cmd_on_sub(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    set_setting(message.chat.id, 'sub_check', 1)
    await message.reply("✅ Проверка подписки включена.")


@dp.message_handler(commands=['off_sub'])
async def cmd_off_sub(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    set_setting(message.chat.id, 'sub_check', 0)
    await message.reply("❌ Проверка подписки отключена.")


# ══════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ══════════════════════════════════════════════════════════════

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

    total_viol = sum(c for _, c in viol_rows)
    emoji_map  = {'flood': '💧', 'forward': '📨', 'link': '🔗',
                  'forbidden_word': '🤬', 'warn': '⚠️', 'mute': '🔇',
                  'kick': '👢', 'ban': '🔨'}

    lines = [
        "<b>📊 Статистика чата</b>",
        f"",
        f"🚫 Слов в сети: <b>{net_words}</b>",
        f"📌 Слов в этом чате: <b>{chat_words}</b>",
        f"",
        f"⚡ Всего нарушений: <b>{total_viol}</b>",
    ]
    if viol_rows:
        lines.append("")
        for vtype, cnt in sorted(viol_rows, key=lambda x: -x[1]):
            em = emoji_map.get(vtype, '•')
            lines.append(f"{em} {vtype}: <b>{cnt}</b>")

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
        # В ЛС — проверка кода капчи
        if chat.type == types.ChatType.PRIVATE:
            return  # капча вводится в группе, не в лс
        return

    uid   = user.id
    cid   = chat.id
    admin = await is_admin(cid, uid)

    # -- Проверка капчи (пользователь вводит код в чате) --------
    row = cursor.execute(
        "SELECT code FROM captcha_pending WHERE user_id=? AND chat_id=?",
        (uid, cid)
    ).fetchone()

    if row:
        expected = row[0]
        text_raw  = (message.text or '').strip()
        if text_raw == expected:
            # Верно! Снять мут, удалить из pending
            cursor.execute(
                "DELETE FROM captcha_pending WHERE user_id=? AND chat_id=?",
                (uid, cid)
            )
            conn.commit()
            try:
                await bot.restrict_chat_member(
                    cid, uid,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True
                    )
                )
            except (BadRequest, Forbidden):
                pass
            await safe_delete(cid, message.message_id)
            resp = await bot.send_message(
                cid,
                f"✅ {mention(user)} прошёл верификацию! Добро пожаловать!"
            )
            asyncio.create_task(auto_delete(cid, resp.message_id, 15))

            try:
                await bot.send_message(uid, "✅ Вы успешно прошли капчу! Можете писать в чате.")
            except (BotBlocked, Forbidden, BadRequest):
                pass
            return
        else:
            # Неверный код — удалить сообщение, ничего не говорить
            await safe_delete(cid, message.message_id)
            return

    # -- Антифлуд --------------------------------------------------
    if not admin and get_setting(cid, 'anti_flood') and is_flooding(uid, cid):
        await safe_delete(cid, message.message_id)
        ok = await do_mute(cid, uid, 300)
        if ok:
            resp = await bot.send_message(
                cid,
                f"💧 {mention(user)}, флуд запрещён. Мут на 5 минут."
            )
            asyncio.create_task(auto_delete(cid, resp.message_id))
        log_violation(uid, cid, 'flood')
        return

    # -- Блок пересылок из каналов ---------------------------------
    if not admin and get_setting(cid, 'anti_forward') and message.forward_from_chat:
        await safe_delete(cid, message.message_id)
        resp = await bot.send_message(
            cid,
            f"📨 {mention(user)}, пересылки из каналов запрещены в этом чате."
        )
        asyncio.create_task(auto_delete(cid, resp.message_id))
        log_violation(uid, cid, 'forward')
        return

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
                f"🔗 {mention(user)}, ссылки запрещены.\n"
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
            bar = "🟥" * count + "⬜" * (max_w - count)

            if count >= max_w:
                clear_warnings(uid, cid)
                await do_mute(cid, uid, MUTE_SECONDS)
                resp = await bot.send_message(
                    cid,
                    f"🤬 {mention(user)}, сообщение удалено.\n"
                    f"🔇 Мут на 1 час — достигнут лимит {max_w} предупреждений."
                )
            else:
                resp = await bot.send_message(
                    cid,
                    f"🤬 {mention(user)}, сообщение содержит запрещённое слово и удалено.\n"
                    f"⚠️ Предупреждение {count}/{max_w} {bar}"
                )
            asyncio.create_task(auto_delete(cid, resp.message_id))
            return

    # -- Проверка подписки -----------------------------------------
    if not admin and get_setting(cid, 'sub_check'):
        try:
            member     = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=uid)
            subscribed = member.status not in ('left', 'kicked')
        except Exception:
            subscribed = True

        if not subscribed:
            await safe_delete(cid, message.message_id)
            resp = await bot.send_message(
                cid,
                f"📢 {mention(user)}, для отправки сообщений подпишитесь на канал:\n"
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
