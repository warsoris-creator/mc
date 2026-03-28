"""
mc.py — Бот защиты сети чатов (aiogram 2.x)

Запуск:
    BOT_TOKEN=<токен> python mc.py
"""

import logging
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
    BadRequest, MessageToDeleteNotFound,
    BotBlocked, ChatNotFound, Unauthorized
)
try:
    from aiogram.utils.exceptions import Forbidden
except ImportError:
    Forbidden = (BotBlocked, Unauthorized)

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════
TOKEN      = os.getenv("BOT_TOKEN", "6376776916:AAGnNP_GoorQS7wZkLhg0snutRPBJttmz70")
OWNER_ID   = 382254550
ADMIN_IDS  = {382254550}
CHANNEL_ID = "-1001672973157"
DB_PATH    = "forbidden_words.db"

MAX_WARNINGS    = 3
MUTE_SECONDS    = 3600
FLOOD_LIMIT     = 5
FLOOD_WINDOW    = 10
CAPTCHA_TIMEOUT = 120

DEFAULT_CHAT_SETTINGS = {
    'sub_check': 0,
    'anti_flood': 1,
    'anti_forward': 1,
    'anti_links': 1,
    'captcha': 0,
    'max_warnings': 3,
}

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
            chat_title   TEXT    DEFAULT '',
            sub_check    INTEGER DEFAULT 0,
            anti_flood   INTEGER DEFAULT 1,
            anti_forward INTEGER DEFAULT 1,
            anti_links   INTEGER DEFAULT 1,
            captcha      INTEGER DEFAULT 0,
            max_warnings INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS bot_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS violation_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            chat_id  INTEGER,
            vtype    TEXT,
            msg_text TEXT,
            ts       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS captcha_pending (
            user_id    INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            code       TEXT    NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS bot_admins (
            user_id  INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    for row in cursor.execute("SELECT user_id FROM bot_admins").fetchall():
        ADMIN_IDS.add(row[0])
    cursor.execute(
        "INSERT OR IGNORE INTO bot_admins(user_id, added_by) VALUES(?,?)",
        (OWNER_ID, OWNER_ID)
    )

    migrated = cursor.execute(
        "SELECT value FROM bot_meta WHERE key='defaults_v2_applied'"
    ).fetchone()
    if not migrated:
        cursor.execute(
            """
            UPDATE chat_settings
            SET anti_links=1,
                anti_flood=1,
                anti_forward=1,
                captcha=0,
                sub_check=0
            """
        )
        cursor.execute(
            "INSERT OR REPLACE INTO bot_meta(key, value) VALUES('defaults_v2_applied', '1')"
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
    if key == 'chat_title':
        return ''
    return DEFAULT_CHAT_SETTINGS.get(key, 0)


def set_setting(chat_id, key, value):
    cursor.execute(
        """
        INSERT OR IGNORE INTO chat_settings(
            chat_id, sub_check, anti_flood, anti_forward, anti_links, captcha, max_warnings
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            chat_id,
            DEFAULT_CHAT_SETTINGS['sub_check'],
            DEFAULT_CHAT_SETTINGS['anti_flood'],
            DEFAULT_CHAT_SETTINGS['anti_forward'],
            DEFAULT_CHAT_SETTINGS['anti_links'],
            DEFAULT_CHAT_SETTINGS['captcha'],
            DEFAULT_CHAT_SETTINGS['max_warnings'],
        )
    )
    cursor.execute(
        f"UPDATE chat_settings SET {key}=? WHERE chat_id=?", (value, chat_id)
    )
    conn.commit()


def register_chat(chat_id, title):
    """Регистрируем/обновляем чат в БД."""
    cursor.execute(
        """
        INSERT OR IGNORE INTO chat_settings(
            chat_id, chat_title, sub_check, anti_flood, anti_forward, anti_links, captcha, max_warnings
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            chat_id,
            title,
            DEFAULT_CHAT_SETTINGS['sub_check'],
            DEFAULT_CHAT_SETTINGS['anti_flood'],
            DEFAULT_CHAT_SETTINGS['anti_forward'],
            DEFAULT_CHAT_SETTINGS['anti_links'],
            DEFAULT_CHAT_SETTINGS['captcha'],
            DEFAULT_CHAT_SETTINGS['max_warnings'],
        )
    )
    cursor.execute(
        "UPDATE chat_settings SET chat_title=? WHERE chat_id=?",
        (title, chat_id)
    )
    conn.commit()


def get_all_chats():
    return cursor.execute(
        "SELECT chat_id, chat_title FROM chat_settings ORDER BY chat_title"
    ).fetchall()


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
            "INSERT OR REPLACE INTO muted_users(user_id,chat_id,muted_until) VALUES(?,?,?)",
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

def e(val) -> str:
    return '✅' if val else '❌'


def settings_text(chat_id, title=None) -> str:
    af  = e(get_setting(chat_id, 'anti_flood'))
    al  = e(get_setting(chat_id, 'anti_links'))
    afw = e(get_setting(chat_id, 'anti_forward'))
    sc  = e(get_setting(chat_id, 'sub_check'))
    cap = e(get_setting(chat_id, 'captcha'))
    mw  = get_setting(chat_id, 'max_warnings')
    header = "<b>⚙️ Настройки</b>"
    if title:
        header += f": {title}"
    return (
        f"{header}\n\n"
        f"Команды (показывают текущее состояние):\n"
        f"/anti_links {al} on|off - 🔗 блокировка ссылок\n"
        f"/anti_flood {af} on|off - 💧 защита от флуда\n"
        f"/anti_forward {afw} on|off - 📨 блокировка пересылок\n"
        f"/captcha {cap} on|off - 🔐 капча при входе\n"
        f"/sub {sc} - 📢 проверка подписки\n\n"
        f"⚠️ Макс. предупреждений: <b>{mw}</b>\n"
        f"<i>✅ - включено  ❌ - выключено</i>"
    )


def settings_keyboard(chat_id, back_to_list=False):
    af  = e(get_setting(chat_id, 'anti_flood'))
    al  = e(get_setting(chat_id, 'anti_links'))
    afw = e(get_setting(chat_id, 'anti_forward'))
    sc  = e(get_setting(chat_id, 'sub_check'))
    cap = e(get_setting(chat_id, 'captcha'))
    mw  = get_setting(chat_id, 'max_warnings')

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"{af} Антифлуд",        callback_data=f"toggle:anti_flood:{chat_id}"),
        InlineKeyboardButton(f"{al} Блок ссылок",     callback_data=f"toggle:anti_links:{chat_id}"),
        InlineKeyboardButton(f"{afw} Блок пересылок", callback_data=f"toggle:anti_forward:{chat_id}"),
        InlineKeyboardButton(f"{sc} Проверка подп.",  callback_data=f"toggle:sub_check:{chat_id}"),
        InlineKeyboardButton(f"{cap} Капча",          callback_data=f"toggle:captcha:{chat_id}"),
        InlineKeyboardButton(f"⚠️ Макс. варн: {mw}",  callback_data=f"warns_menu:{chat_id}"),
    )
    row = [InlineKeyboardButton("🔄 Обновить", callback_data=f"settings_refresh:{chat_id}")]
    if back_to_list:
        row.append(InlineKeyboardButton("← Группы", callback_data="pm:groups"))
    kb.add(*row)
    return kb


def warns_keyboard(chat_id, back_to_list=False):
    kb = InlineKeyboardMarkup(row_width=3)
    for n in [1, 2, 3, 5, 7, 10]:
        kb.insert(InlineKeyboardButton(str(n), callback_data=f"set_warns:{n}:{chat_id}"))
    row = [InlineKeyboardButton("← Назад", callback_data=f"settings_refresh:{chat_id}")]
    if back_to_list:
        row.append(InlineKeyboardButton("🏠 Группы", callback_data="pm:groups"))
    kb.add(*row)
    return kb


def help_keyboard(is_adm: bool):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📖 Общие", callback_data="help:general"))
    if is_adm:
        kb.add(
            InlineKeyboardButton("🛡️ Модерация",  callback_data="help:mod"),
            InlineKeyboardButton("🚫 Слова",       callback_data="help:words"),
            InlineKeyboardButton("⚙️ Настройки",   callback_data="help:settings"),
            InlineKeyboardButton("👑 Суперадмины", callback_data="help:admins"),
        )
    return kb


def groups_list_keyboard(chats: list):
    kb = InlineKeyboardMarkup(row_width=1)
    for chat_id, title in chats:
        label = title or str(chat_id)
        kb.add(InlineKeyboardButton(f"💬 {label}", callback_data=f"pm:chat:{chat_id}"))
    kb.add(InlineKeyboardButton("🔄 Обновить список", callback_data="pm:groups"))
    return kb


def chat_menu_keyboard(chat_id):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("⚙️ Настройки",     callback_data=f"pm:settings:{chat_id}"),
        InlineKeyboardButton("📊 Статистика",     callback_data=f"pm:stats:{chat_id}"),
        InlineKeyboardButton("🚫 Слова сети",    callback_data=f"pm:words:network"),
        InlineKeyboardButton("🚫 Слова чата",    callback_data=f"pm:words:{chat_id}"),
    )
    kb.add(InlineKeyboardButton("← Все группы", callback_data="pm:groups"))
    return kb


# ══════════════════════════════════════════════════════════════
#  HELP TEXTS
# ══════════════════════════════════════════════════════════════

HELP_SECTIONS = {
    "general": (
        "📖 <b>Общие команды</b>\n\n"
        "/start — приветствие\n"
        "/panel — панель управления группами (ЛС)\n"
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
        "/add_word &lt;слово&gt; — добавить в сеть (все чаты)\n"
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
        "/settings — панель настроек с кнопками\n\n"
        "Команды (показывают текущее состояние):\n"
        "/anti_links ✅ on|off — 🔗 блокировка ссылок\n"
        "/anti_flood ✅ on|off — 💧 защита от флуда\n"
        "/anti_forward ✅ on|off — 📨 блокировка пересылок\n"
        "/captcha ❌ on|off — 🔐 капча при входе\n"
        "/sub ✅ — 📢 проверка подписки\n\n"
        "<i>✅ — включено  ❌ — выключено</i>"
    ),
    "admins": (
        "👑 <b>Управление суперадминами</b>\n\n"
        "/makeadmin [reply|@user] — выдать права суперадмина бота\n"
        "/rmadmin [reply|@user] — снять права суперадмина бота\n"
        "/bot_admins — список суперадминов бота\n\n"
        "<i>Только владелец бота может выдавать права.</i>"
    ),
}


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ — СТАРТ И ПОМОЩЬ
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    uid   = message.from_user.id
    is_pm = message.chat.type == types.ChatType.PRIVATE

    if uid == OWNER_ID:
        role = "👑 Владелец"
    elif uid in ADMIN_IDS:
        role = "🌟 Суперадмин бота"
    elif not is_pm and await is_admin(message.chat.id, uid):
        role = "🛡️ Администратор чата"
    else:
        role = "👤 Пользователь"

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📖 Помощь", callback_data="help:general"))
    if is_pm and uid in ADMIN_IDS:
        kb.add(InlineKeyboardButton("💬 Управление группами", callback_data="pm:groups"))

    text = (
        f"Привет! Я <b>бот-защитник</b> сети чатов.\n"
        f"Ваша роль: <b>{role}</b>\n\n"
    )
    if is_pm and uid in ADMIN_IDS:
        text += "Управляйте всеми группами прямо здесь 👇"
    else:
        text += "Используйте /help для справки."

    await message.reply(text, reply_markup=kb)


@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    uid    = message.from_user.id
    is_pm  = message.chat.type == types.ChatType.PRIVATE
    is_adm = (uid in ADMIN_IDS or
              (not is_pm and await is_admin(message.chat.id, uid)))
    kb = help_keyboard(is_adm)
    if is_pm and uid in ADMIN_IDS:
        kb.add(InlineKeyboardButton("💬 Управление группами", callback_data="pm:groups"))
    await message.reply("Выберите раздел помощи 👇", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("help:"))
async def cb_help(call: types.CallbackQuery):
    section = call.data.split(":")[1]
    text    = HELP_SECTIONS.get(section, "Раздел не найден.")
    uid     = call.from_user.id
    is_pm   = call.message.chat.type == types.ChatType.PRIVATE
    is_adm  = (uid in ADMIN_IDS or await is_admin(call.message.chat.id, uid))
    kb = help_keyboard(is_adm)
    if is_pm and uid in ADMIN_IDS:
        kb.add(InlineKeyboardButton("💬 Управление группами", callback_data="pm:groups"))
    await call.message.edit_text(text, reply_markup=kb)
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

    warns     = get_warnings(uid, message.chat.id)
    max_w     = get_setting(message.chat.id, 'max_warnings')
    warns_bar = "🟥" * warns + "⬜" * (max_w - warns)

    await message.reply(
        f"<b>Ваш статус</b>\n"
        f"Роль: {role}\n"
        f"Предупреждения: {warns}/{max_w} {warns_bar}"
    )


# ══════════════════════════════════════════════════════════════
#  ПАНЕЛЬ УПРАВЛЕНИЯ ГРУППАМИ В ЛС
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['panel'], chat_type=types.ChatType.PRIVATE)
async def cmd_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.reply("❌ Нет прав.")
    chats = get_all_chats()
    if not chats:
        return await message.reply(
            "Бот пока не зарегистрировал ни одной группы.\n"
            "Напишите что-нибудь в группе где есть бот — она появится здесь."
        )
    await message.reply(
        f"<b>💬 Управление группами</b>\n"
        f"Всего групп: <b>{len(chats)}</b>\n\n"
        f"Выберите группу:",
        reply_markup=groups_list_keyboard(chats)
    )


@dp.callback_query_handler(lambda c: c.data == "pm:groups")
async def cb_pm_groups(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer("❌ Нет прав.", show_alert=True)
    chats = get_all_chats()
    if not chats:
        await call.message.edit_text(
            "Групп пока нет. Добавьте бота в группу и напишите там что-нибудь.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔄 Обновить", callback_data="pm:groups")
            )
        )
        return await call.answer()
    await call.message.edit_text(
        f"<b>💬 Управление группами</b>\n"
        f"Всего групп: <b>{len(chats)}</b>\n\n"
        f"Выберите группу:",
        reply_markup=groups_list_keyboard(chats)
    )
    await call.answer()


@dp.my_chat_member_handler()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    chat = update.chat
    if chat.type in (types.ChatType.GROUP, types.ChatType.SUPERGROUP):
        register_chat(chat.id, chat.title or str(chat.id))


@dp.callback_query_handler(lambda c: c.data.startswith("pm:chat:"))
async def cb_pm_chat(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer("❌ Нет прав.", show_alert=True)
    chat_id = int(call.data.split(":")[2])
    title   = get_setting(chat_id, 'chat_title') or str(chat_id)
    await call.message.edit_text(
        f"<b>💬 {title}</b>\n\n"
        f"Выберите действие:",
        reply_markup=chat_menu_keyboard(chat_id)
    )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("pm:settings:"))
async def cb_pm_settings(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer("❌ Нет прав.", show_alert=True)
    chat_id = int(call.data.split(":")[2])
    title   = get_setting(chat_id, 'chat_title') or str(chat_id)
    await call.message.edit_text(
        settings_text(chat_id, title),
        reply_markup=settings_keyboard(chat_id, back_to_list=True)
    )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("pm:stats:"))
async def cb_pm_stats(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer("❌ Нет прав.", show_alert=True)
    chat_id = int(call.data.split(":")[2])
    title   = get_setting(chat_id, 'chat_title') or str(chat_id)

    rows = cursor.execute(
        "SELECT vtype, COUNT(*) FROM violation_log WHERE chat_id=? GROUP BY vtype",
        (chat_id,)
    ).fetchall()
    net_words  = cursor.execute(
        "SELECT COUNT(*) FROM forbidden_words WHERE scope='network'"
    ).fetchone()[0]
    chat_words = cursor.execute(
        "SELECT COUNT(*) FROM forbidden_words WHERE scope=?", (str(chat_id),)
    ).fetchone()[0]

    emoji_map = {'flood': '💧', 'forward': '📨', 'link': '🔗',
                 'forbidden_word': '🤬', 'warn': '⚠️', 'mute': '🔇',
                 'kick': '👢', 'ban': '🔨'}
    total = sum(c for _, c in rows)
    lines = [
        f"<b>📊 Статистика: {title}</b>\n",
        f"🚫 Слов в сети: <b>{net_words}</b>",
        f"📌 Слов в этом чате: <b>{chat_words}</b>",
        f"⚡ Всего нарушений: <b>{total}</b>",
    ]
    if rows:
        lines.append("")
        for vtype, cnt in sorted(rows, key=lambda x: -x[1]):
            em = emoji_map.get(vtype, '•')
            lines.append(f"{em} {vtype}: <b>{cnt}</b>")

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("← Назад", callback_data=f"pm:chat:{chat_id}"))
    await call.message.edit_text('\n'.join(lines), reply_markup=kb)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("pm:words:"))
async def cb_pm_words(call: types.CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer("❌ Нет прав.", show_alert=True)
    scope = call.data.split(":")[2]

    if scope == 'network':
        words   = [r[0] for r in cursor.execute(
            "SELECT word FROM forbidden_words WHERE scope='network' ORDER BY word"
        ).fetchall()]
        label   = "всей сети"
        back_cb = "pm:groups"
    else:
        chat_id = int(scope)
        words   = [r[0] for r in cursor.execute(
            "SELECT word FROM forbidden_words WHERE scope=? ORDER BY word", (scope,)
        ).fetchall()]
        label   = get_setting(chat_id, 'chat_title') or scope
        back_cb = f"pm:chat:{chat_id}"

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("← Назад", callback_data=back_cb))

    if not words:
        text = f"<b>🚫 Слова ({label})</b>\n\nСписок пуст."
    else:
        chunks = [words[i:i+30] for i in range(0, len(words), 30)]
        text = (
            f"<b>🚫 Слова ({label})</b> — {len(words)} шт.\n\n"
            + "\n".join(", ".join(f"<code>{w}</code>" for w in chunk) for chunk in chunks)
        )

    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


# ══════════════════════════════════════════════════════════════
#  CALLBACKS — НАСТРОЙКИ (группа + ЛС)
# ══════════════════════════════════════════════════════════════

SETTING_LABELS = {
    'anti_flood':   'Антифлуд',
    'anti_links':   'Блок ссылок',
    'anti_forward': 'Блок пересылок',
    'sub_check':    'Проверка подписки',
    'captcha':      'Капча',
}


@dp.callback_query_handler(lambda c: c.data.startswith("toggle:"))
async def cb_toggle(call: types.CallbackQuery):
    _, key, chat_id_str = call.data.split(":")
    chat_id = int(chat_id_str)

    if not await is_admin(chat_id, call.from_user.id):
        return await call.answer("❌ Нет прав.", show_alert=True)

    current = get_setting(chat_id, key)
    new_val = 0 if current else 1
    set_setting(chat_id, key, new_val)

    label = SETTING_LABELS.get(key, key)
    state = "включён ✅" if new_val else "выключен ❌"
    await call.answer(f"{label} {state}")

    is_pm = call.message.chat.type == types.ChatType.PRIVATE
    title = get_setting(chat_id, 'chat_title') if is_pm else None

    await call.message.edit_text(
        settings_text(chat_id, title),
        reply_markup=settings_keyboard(chat_id, back_to_list=is_pm)
    )


@dp.callback_query_handler(lambda c: c.data.startswith("settings_refresh:"))
async def cb_settings_refresh(call: types.CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    if not await is_admin(chat_id, call.from_user.id):
        return await call.answer("❌ Нет прав.", show_alert=True)
    is_pm = call.message.chat.type == types.ChatType.PRIVATE
    title = get_setting(chat_id, 'chat_title') if is_pm else None
    await call.message.edit_text(
        settings_text(chat_id, title),
        reply_markup=settings_keyboard(chat_id, back_to_list=is_pm)
    )
    await call.answer("Обновлено")


@dp.callback_query_handler(lambda c: c.data.startswith("warns_menu:"))
async def cb_warns_menu(call: types.CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    if not await is_admin(chat_id, call.from_user.id):
        return await call.answer("❌ Нет прав.", show_alert=True)
    is_pm = call.message.chat.type == types.ChatType.PRIVATE
    await call.message.edit_text(
        "<b>⚠️ Выберите максимальное количество предупреждений:</b>",
        reply_markup=warns_keyboard(chat_id, back_to_list=is_pm)
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
    is_pm = call.message.chat.type == types.ChatType.PRIVATE
    title = get_setting(chat_id, 'chat_title') if is_pm else None
    await call.message.edit_text(
        settings_text(chat_id, title),
        reply_markup=settings_keyboard(chat_id, back_to_list=is_pm)
    )


# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ — НАСТРОЙКИ ЧАТА (в группе с отображением состояния)
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['settings'])
async def cmd_settings(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    cid   = message.chat.id
    is_pm = message.chat.type == types.ChatType.PRIVATE
    await message.reply(
        settings_text(cid),
        reply_markup=settings_keyboard(cid, back_to_list=is_pm)
    )


def _make_toggle_cmd(command_key, label):
    async def handler(message: types.Message):
        if not await is_admin(message.chat.id, message.from_user.id):
            return
        cid = message.chat.id
        arg = message.get_args().strip().lower()
        cur = get_setting(cid, command_key)

        if not arg:
            val = 0 if cur else 1
        elif arg in ('on', '1', 'вкл'):
            val = 1
        elif arg in ('off', '0', 'выкл'):
            val = 0
        else:
            state = "✅ включена" if cur else "❌ выключена"
            return await message.reply(
                f"{label}: <b>{state}</b>\n"
                f"Использование: /{command_key} on|off"
            )
        set_setting(cid, command_key, val)
        state = "✅ включена" if val else "❌ выключена"
        await message.reply(f"{label}: <b>{state}</b>")
    handler.__name__ = f"toggle_{command_key}"
    return handler


dp.message_handler(commands=['anti_links'])(_make_toggle_cmd('anti_links',   '🔗 Блокировка ссылок'))
dp.message_handler(commands=['anti_flood'])(_make_toggle_cmd('anti_flood',   '💧 Антифлуд'))
dp.message_handler(commands=['anti_forward'])(_make_toggle_cmd('anti_forward','📨 Блокировка пересылок'))
dp.message_handler(commands=['captcha'])(_make_toggle_cmd('captcha',         '🔐 Капча при входе'))


@dp.message_handler(commands=['sub'])
async def cmd_sub(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return

    arg = message.get_args().strip().lower()
    cur = get_setting(message.chat.id, 'sub_check')

    if not arg:
        val = 0 if cur else 1
    elif arg in ('on', '1', 'вкл'):
        val = 1
    elif arg in ('off', '0', 'выкл'):
        val = 0
    else:
        state = "✅ включена" if cur else "❌ выключена"
        return await message.reply(
            f"📢 Проверка подписки: <b>{state}</b>\n"
            f"Использование: /sub on|off"
        )

    set_setting(message.chat.id, 'sub_check', val)
    state = "✅ включена" if val else "❌ выключена"
    await message.reply(f"📢 Проверка подписки: <b>{state}</b>")


@dp.message_handler(commands=['on_sub'])
async def cmd_on_sub(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    set_setting(message.chat.id, 'sub_check', 1)
    await message.reply("📢 Проверка подписки: ✅ включена")


@dp.message_handler(commands=['off_sub'])
async def cmd_off_sub(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    set_setting(message.chat.id, 'sub_check', 0)
    await message.reply("📢 Проверка подписки: ❌ выключена")


# ══════════════════════════════════════════════════════════════
#  СУПЕРАДМИНЫ БОТА
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['makeadmin'])
async def cmd_makeadmin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.reply("❌ Только владелец бота может выдавать права суперадмина.")
    target = await get_target(message)
    if not target:
        return await message.reply(
            "Ответьте на сообщение или укажите @username / ID.\n"
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
        f"Ему доступны все команды управления во всех чатах сети."
    )
    try:
        await bot.send_message(
            target.id,
            f"🎉 Вы назначены <b>суперадмином бота</b>!\n"
            f"Напишите мне /panel для управления группами."
        )
    except Exception:
        pass


@dp.message_handler(commands=['rmadmin'])
async def cmd_rmadmin(message: types.Message):
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
#  ЗАПРЕЩЁННЫЕ СЛОВА
# ══════════════════════════════════════════════════════════════

def _parse_bulk_words(text: str) -> list:
    lines = text.strip().splitlines()
    return [line.strip().lower() for line in lines
            if line.strip() and not line.strip().startswith('/')]


@dp.message_handler(commands=['add_word'])
async def cmd_add_word(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /add_word &lt;слово&gt;\nДля пачки: /add_words")
    try:
        cursor.execute(
            "INSERT INTO forbidden_words(word,scope,added_by) VALUES(?,?,?)",
            (word, 'network', message.from_user.id)
        )
        conn.commit()
        await message.reply(f"✅ Слово <code>{word}</code> добавлено во <b>всю сеть</b>.")
    except sqlite3.IntegrityError:
        await message.reply(f"⚠️ Слово <code>{word}</code> уже есть в сети.")


@dp.message_handler(commands=['del_word'])
async def cmd_del_word(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    word = message.get_args().strip().lower()
    if not word:
        return await message.reply("Использование: /del_word &lt;слово&gt;")
    cursor.execute("DELETE FROM forbidden_words WHERE word=? AND scope='network'", (word,))
    conn.commit()
    if cursor.rowcount:
        await message.reply(f"✅ Слово <code>{word}</code> удалено из сети.")
    else:
        await message.reply(f"⚠️ Слово <code>{word}</code> не найдено в сети.")


@dp.message_handler(commands=['add_words'])
async def cmd_add_words_bulk(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return await message.reply("❌ Нет прав.")
    raw = message.text.partition('\n')[2]
    if not raw.strip():
        return await message.reply(
            "Укажите слова — каждое с новой строки:\n\n"
            "<code>/add_words\nшлюха\nпизда\nслово3</code>"
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
        lines.append("⚠️ Уже были: " + ", ".join(f"<code>{w}</code>" for w in skipped))
    await message.reply('\n'.join(lines))


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
        await message.reply(f"✅ Слово <code>{word}</code> добавлено только в этот чат.")
    except sqlite3.IntegrityError:
        await message.reply(f"⚠️ Слово <code>{word}</code> уже есть.")


@dp.message_handler(commands=['add_words_here'])
async def cmd_add_words_here_bulk(message: types.Message):
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
        await message.reply(f"✅ Слово <code>{word}</code> удалено из этого чата.")
    else:
        await message.reply(f"⚠️ Слово <code>{word}</code> не найдено.")


@dp.message_handler(commands=['view_words'])
async def cmd_view_words(message: types.Message):
    cid   = message.chat.id
    net   = [r[0] for r in cursor.execute(
        "SELECT word FROM forbidden_words WHERE scope='network' ORDER BY word"
    ).fetchall()]
    local = [r[0] for r in cursor.execute(
        "SELECT word FROM forbidden_words WHERE scope=? ORDER BY word", (str(cid),)
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
    max_w  = get_setting(message.chat.id, 'max_warnings')
    count  = add_warning(target.id, message.chat.id, reason, message.from_user.id)
    log_violation(target.id, message.chat.id, 'warn', reason)
    bar    = "🟥" * count + "⬜" * (max_w - count)
    if count >= max_w:
        clear_warnings(target.id, message.chat.id)
        await do_mute(message.chat.id, target.id, MUTE_SECONDS)
        action = f"🔇 Автоматически замучен на 1 час (лимит {max_w})."
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
#  СТАТИСТИКА
# ══════════════════════════════════════════════════════════════

@dp.message_handler(commands=['stats'])
async def cmd_stats(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        return
    cid  = message.chat.id
    rows = cursor.execute(
        "SELECT vtype, COUNT(*) FROM violation_log WHERE chat_id=? GROUP BY vtype", (cid,)
    ).fetchall()
    net_words  = cursor.execute(
        "SELECT COUNT(*) FROM forbidden_words WHERE scope='network'"
    ).fetchone()[0]
    chat_words = cursor.execute(
        "SELECT COUNT(*) FROM forbidden_words WHERE scope=?", (str(cid),)
    ).fetchone()[0]
    emoji_map = {'flood': '💧', 'forward': '📨', 'link': '🔗',
                 'forbidden_word': '🤬', 'warn': '⚠️', 'mute': '🔇',
                 'kick': '👢', 'ban': '🔨'}
    total = sum(c for _, c in rows)
    lines = [
        "<b>📊 Статистика чата</b>\n",
        f"🚫 Слов в сети: <b>{net_words}</b>",
        f"📌 Слов в этом чате: <b>{chat_words}</b>",
        f"⚡ Всего нарушений: <b>{total}</b>",
    ]
    if rows:
        lines.append("")
        for vtype, cnt in sorted(rows, key=lambda x: -x[1]):
            em = emoji_map.get(vtype, '•')
            lines.append(f"{em} {vtype}: <b>{cnt}</b>")
    await message.reply('\n'.join(lines))


# ══════════════════════════════════════════════════════════════
#  КАПЧА
# ══════════════════════════════════════════════════════════════

def _gen_captcha_code(length=6) -> str:
    return ''.join(random.choices(string.digits, k=length))


async def _captcha_timeout(user_id, chat_id, code):
    await asyncio.sleep(CAPTCHA_TIMEOUT)
    row = cursor.execute(
        "SELECT code FROM captcha_pending WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    ).fetchone()
    if row and row[0] == code:
        cursor.execute(
            "DELETE FROM captcha_pending WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        conn.commit()
        try:
            await bot.kick_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
        except (BadRequest, Forbidden):
            pass
        try:
            msg = await bot.send_message(
                chat_id,
                f"🚫 Пользователь не прошёл капчу за {CAPTCHA_TIMEOUT} сек и был кикнут."
            )
            asyncio.create_task(auto_delete(chat_id, msg.message_id, 30))
        except Exception:
            pass
        try:
            await bot.send_message(
                user_id,
                f"❌ Вы не прошли капчу за {CAPTCHA_TIMEOUT} секунд и были удалены из чата.\n"
                f"Вы можете вернуться и попробовать снова."
            )
        except Exception:
            pass


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def on_new_member(message: types.Message):
    chat_id = message.chat.id
    register_chat(chat_id, message.chat.title or str(chat_id))

    if not get_setting(chat_id, 'captcha'):
        return

    for user in message.new_chat_members:
        if user.is_bot:
            continue
        uid  = user.id
        code = _gen_captcha_code()
        try:
            await bot.restrict_chat_member(
                chat_id, uid,
                permissions=ChatPermissions(can_send_messages=False)
            )
        except (BadRequest, Forbidden):
            pass

        cursor.execute(
            "INSERT OR REPLACE INTO captcha_pending(user_id,chat_id,code,created_at)"
            " VALUES(?,?,?,?)",
            (uid, chat_id, code, int(time.time()))
        )
        conn.commit()

        dm_sent = False
        try:
            await bot.send_message(
                uid,
                f"👋 Привет! Вы вступили в чат <b>{message.chat.title}</b>.\n\n"
                f"Введите этот код в группе для верификации:\n\n"
                f"<code>{code}</code>\n\n"
                f"⏱ У вас есть <b>{CAPTCHA_TIMEOUT} секунд</b>.\n"
                f"Если не введёте — вас кикнут автоматически."
            )
            dm_sent = True
        except Exception:
            pass

        if dm_sent:
            chat_msg = await message.reply(
                f"👋 {mention(user)}, добро пожаловать!\n\n"
                f"🔐 Проверьте <b>личные сообщения от бота</b> — введите там указанный код здесь.\n"
                f"⏱ Время: <b>{CAPTCHA_TIMEOUT} сек</b>."
            )
        else:
            chat_msg = await message.reply(
                f"👋 {mention(user)}, добро пожаловать!\n\n"
                f"🔐 Введите код для верификации:\n<code>{code}</code>\n\n"
                f"⏱ У вас есть <b>{CAPTCHA_TIMEOUT} секунд</b>."
            )

        asyncio.create_task(_captcha_timeout(uid, chat_id, code))
        asyncio.create_task(auto_delete(chat_id, chat_msg.message_id, CAPTCHA_TIMEOUT + 5))


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

    # Регистрируем/обновляем чат
    register_chat(cid, chat.title or str(cid))

    # -- Проверка капчи -------------------------------------------
    row = cursor.execute(
        "SELECT code FROM captcha_pending WHERE user_id=? AND chat_id=?",
        (uid, cid)
    ).fetchone()
    if row:
        expected = row[0]
        text_raw = (message.text or '').strip()
        if text_raw == expected:
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
                cid, f"✅ {mention(user)} прошёл верификацию! Добро пожаловать!"
            )
            asyncio.create_task(auto_delete(cid, resp.message_id, 15))
            try:
                await bot.send_message(uid, "✅ Вы успешно прошли капчу! Можете писать в чате.")
            except Exception:
                pass
            return
        else:
            await safe_delete(cid, message.message_id)
            return

    # -- Антифлуд --------------------------------------------------
    if not admin and get_setting(cid, 'anti_flood') and is_flooding(uid, cid):
        await safe_delete(cid, message.message_id)
        ok = await do_mute(cid, uid, 300)
        if ok:
            resp = await bot.send_message(
                cid, f"💧 {mention(user)}, флуд запрещён. Мут на 5 минут."
            )
            asyncio.create_task(auto_delete(cid, resp.message_id))
        log_violation(uid, cid, 'flood')
        return

    # -- Блок пересылок --------------------------------------------
    if not admin and get_setting(cid, 'anti_forward') and message.forward_from_chat:
        await safe_delete(cid, message.message_id)
        resp = await bot.send_message(
            cid, f"📨 {mention(user)}, пересылки из каналов запрещены."
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
        if any(ent.type in ('url', 'text_link') for ent in entities):
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
