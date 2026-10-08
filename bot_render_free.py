import os
import sqlite3
import asyncio
import time
import html
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import feedparser

from bale import (
    Bot,
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from openai import OpenAI


# =========================================================
# 🔐 تنظیمات
# =========================================================

BALE_TOKEN = os.getenv("BALE_TOKEN")

GAPGPT_API_KEY = os.getenv("GAPGPT_API_KEY")

GAPGPT_BASE_URL = "https://api.gapgpt.app/v1"

DEFAULT_MODEL = "gpt-4o-mini"

# آیدی عددی خودت
ADMIN_IDS = {
    "955311935"
}


# =========================================================
# ⚙️ محدودیت
# =========================================================

MAX_AI_MESSAGES = 6
LIMIT_SECONDS = 4 * 60 * 60

MAX_HISTORY = 20


# =========================================================
# 🗃️ DATABASE
# =========================================================

DB_FILE = "bot_v3.db"

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    first_name TEXT,
    username TEXT,
    model TEXT DEFAULT 'gpt-4o-mini',
    created_at REAL,
    last_seen REAL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    role TEXT,
    content TEXT,
    created_at REAL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    created_at REAL
)
""")

db.commit()


# =========================================================
# 🤖 GAPGPT
# =========================================================

if not BALE_TOKEN or not GAPGPT_API_KEY:
    raise RuntimeError("BALE_TOKEN و GAPGPT_API_KEY را در Environment Variables تنظیم کنید.")

ai = OpenAI(
    api_key=GAPGPT_API_KEY,
    base_url=GAPGPT_BASE_URL
)


# =========================================================
# 🕐 زمان
# =========================================================

def now():
    return time.time()


# =========================================================
# 👤 اطلاعات کاربر
# =========================================================

def get_uid(message):
    try:
        return str(message.author.id)
    except Exception:
        return ""


def register_user(message):

    uid = get_uid(message)

    if not uid:
        return

    try:
        first_name = message.author.first_name or ""
    except Exception:
        first_name = ""

    try:
        username = message.author.username or ""
    except Exception:
        username = ""

    db.execute("""
    INSERT INTO users
    (
        user_id,
        first_name,
        username,
        model,
        created_at,
        last_seen
    )
    VALUES (?, ?, ?, ?, ?, ?)

    ON CONFLICT(user_id)
    DO UPDATE SET
        first_name = excluded.first_name,
        username = excluded.username,
        last_seen = excluded.last_seen
    """, (
        uid,
        first_name,
        username,
        DEFAULT_MODEL,
        now(),
        now()
    ))

    db.commit()


# =========================================================
# 🤖 MODEL
# =========================================================

def get_model(uid):

    row = db.execute("""
    SELECT model
    FROM users
    WHERE user_id = ?
    """, (uid,)).fetchone()

    if row:
        return row["model"]

    return DEFAULT_MODEL


def set_model(uid, model):

    db.execute("""
    UPDATE users
    SET model = ?
    WHERE user_id = ?
    """, (
        model,
        uid
    ))

    db.commit()


# =========================================================
# 🧠 MEMORY
# =========================================================

def save_message(
    uid,
    role,
    content
):

    db.execute("""
    INSERT INTO messages
    (
        user_id,
        role,
        content,
        created_at
    )
    VALUES (?, ?, ?, ?)
    """, (
        uid,
        role,
        content,
        now()
    ))

    db.commit()


def get_history(uid):

    rows = db.execute("""
    SELECT role, content
    FROM messages
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT ?
    """, (
        uid,
        MAX_HISTORY
    )).fetchall()

    rows = list(reversed(rows))

    return [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in rows
    ]


def clear_memory(uid):

    db.execute("""
    DELETE FROM messages
    WHERE user_id = ?
    """, (uid,))

    db.commit()


# =========================================================
# ⏱️ 6 پیام در 4 ساعت
# =========================================================

def get_usage(uid):

    cutoff = now() - LIMIT_SECONDS

    row = db.execute("""
    SELECT COUNT(*) AS total
    FROM usage
    WHERE user_id = ?
    AND created_at >= ?
    """, (
        uid,
        cutoff
    )).fetchone()

    return int(row["total"])


def can_use_ai(uid):

    return get_usage(uid) < MAX_AI_MESSAGES


def add_usage(uid):

    db.execute("""
    INSERT INTO usage
    (
        user_id,
        created_at
    )
    VALUES (?, ?)
    """, (
        uid,
        now()
    ))

    db.commit()


def remove_last_usage(uid):

    row = db.execute("""
    SELECT id
    FROM usage
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT 1
    """, (uid,)).fetchone()

    if row:

        db.execute("""
        DELETE FROM usage
        WHERE id = ?
        """, (
            row["id"],
        ))

        db.commit()


def usage_text(uid):

    used = get_usage(uid)

    remaining = max(
        0,
        MAX_AI_MESSAGES - used
    )

    return (
        "🤖 سهمیه هوش مصنوعی\n\n"
        f"📨 مصرف‌شده: {used}/{MAX_AI_MESSAGES}\n"
        f"🟢 باقی‌مانده: {remaining}\n\n"
        "⏱️ محدودیت: ۶ پیام در هر ۴ ساعت"
    )


# =========================================================
# 🧠 AI
# =========================================================

async def ask_ai(
    uid,
    user_text
):

    if not can_use_ai(uid):

        return (
            "⛔ سهمیه شما تمام شده.\n\n"
            "در هر ۴ ساعت حداکثر "
            "۶ پیام هوش مصنوعی مجاز است."
        )

    model = get_model(uid)

    history = get_history(uid)

    messages = [
        {
            "role": "system",
            "content": (
                "تو یک دستیار هوش مصنوعی فارسی هستی. "
                "طبیعی، دقیق و دوستانه پاسخ بده. "
                "اگر کاربر فارسی صحبت کرد، فارسی جواب بده."
            )
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": user_text
    })

    # قبل از درخواست واقعی
    add_usage(uid)

    try:

        response = await asyncio.to_thread(
            ai.chat.completions.create,
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=2000
        )

        answer = response.choices[0].message.content

        if not answer:
            answer = "❌ مدل پاسخی برنگرداند."

        save_message(
            uid,
            "user",
            user_text
        )

        save_message(
            uid,
            "assistant",
            answer
        )

        return answer

    except Exception as e:

        # درخواست شکست خورد → سهمیه برگردد
        remove_last_usage(uid)

        print(
            "GAPGPT ERROR:",
            repr(e)
        )

        return (
            "❌ اتصال به هوش مصنوعی با مشکل مواجه شد.\n\n"
            "مدل یا کلید GapGPT را بررسی کن."
        )


# =========================================================
# 🪟 منوی اصلی
# =========================================================

def main_menu():

    keyboard = InlineKeyboardMarkup()

    keyboard.add(
        InlineKeyboardButton(
            text="🤖 هوش مصنوعی",
            callback_data="ai"
        ),
        row=1
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🧠 حافظه",
            callback_data="memory"
        ),
        row=1
    )

    keyboard.add(
        InlineKeyboardButton(
            text="📰 اخبار",
            callback_data="news"
        ),
        row=2
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🌤️ هواشناسی",
            callback_data="weather"
        ),
        row=2
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🔎 جستجو",
            callback_data="search"
        ),
        row=3
    )

    keyboard.add(
        InlineKeyboardButton(
            text="📊 سهمیه من",
            callback_data="usage"
        ),
        row=3
    )

    keyboard.add(
        InlineKeyboardButton(
            text="⚙️ تنظیمات",
            callback_data="settings"
        ),
        row=4
    )

    return keyboard


# =========================================================
# ⚙️ منوی تنظیمات
# =========================================================

def settings_menu():

    keyboard = InlineKeyboardMarkup()

    keyboard.add(
        InlineKeyboardButton(
            text="🤖 GPT-4o Mini",
            callback_data="model:gpt-4o-mini"
        ),
        row=1
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🧠 DeepSeek",
            callback_data="model:deepseek-chat"
        ),
        row=2
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="home"
        ),
        row=3
    )

    return keyboard


# =========================================================
# 👑 ADMIN
# =========================================================

def is_admin(uid):

    return uid in ADMIN_IDS


def admin_menu():

    keyboard = InlineKeyboardMarkup()

    keyboard.add(
        InlineKeyboardButton(
            text="📊 آمار",
            callback_data="admin_stats"
        ),
        row=1
    )

    keyboard.add(
        InlineKeyboardButton(
            text="📢 ارسال همگانی",
            callback_data="admin_broadcast"
        ),
        row=2
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="home"
        ),
        row=3
    )

    return keyboard


# =========================================================
# 📊 STATS
# =========================================================

def get_stats():

    users = db.execute("""
    SELECT COUNT(*) AS c
    FROM users
    """).fetchone()["c"]

    messages = db.execute("""
    SELECT COUNT(*) AS c
    FROM messages
    """).fetchone()["c"]

    ai_today = db.execute("""
    SELECT COUNT(*) AS c
    FROM usage
    WHERE created_at >= ?
    """, (
        now() - 86400,
    )).fetchone()["c"]

    active = db.execute("""
    SELECT COUNT(*) AS c
    FROM users
    WHERE last_seen >= ?
    """, (
        now() - 86400,
    )).fetchone()["c"]

    return (
        "📊 آمار ربات\n\n"
        f"👥 کل کاربران: {users}\n"
        f"💬 کل پیام‌های AI: {messages}\n"
        f"🤖 درخواست AI در ۲۴ ساعت: {ai_today}\n"
        f"🟢 کاربران فعال ۲۴ ساعت: {active}"
    )


# =========================================================
# 📰 NEWS
# =========================================================

NEWS_SOURCES = [

    (
        "ایسنا",
        "https://www.isna.ir/rss"
    ),

    (
        "مهر",
        "https://www.mehrnews.com/rss"
    ),

    (
        "خبرآنلاین",
        "https://www.khabaronline.ir/rss"
    ),

    (
        "عصر ایران",
        "https://www.asriran.com/fa/rss/allnews"
    ),

    (
        "تابناک",
        "https://www.tabnak.ir/fa/rss/allnews"
    ),

    (
        "باشگاه خبرنگاران",
        "https://www.yjc.ir/fa/rss/allnews"
    ),

    (
        "ایرنا",
        "https://www.irna.ir/rss"
    ),

    (
        "انتخاب",
        "https://www.entekhab.ir/fa/rss/allnews"
    ),

    (
        "ایلنا",
        "https://www.ilna.ir/rss"
    ),

    (
        "تجارت نیوز",
        "https://tejaratnews.com/feed"
    ),

    (
        "زومیت",
        "https://www.zoomit.ir/feed/"
    ),

    (
        "دیجیاتو",
        "https://digiato.com/feed"
    ),

    (
        "گجت نیوز",
        "https://gadgetnews.net/feed/"
    ),

    (
        "ورزش سه",
        "https://www.varzesh3.com/rss"
    ),

    (
        "اقتصاد آنلاین",
        "https://www.eghtesadonline.com/fa/rss"
    ),

]


def clean_html(text):

    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<[^>]*>",
        "",
        text
    )

    return text.strip()


def get_news():

    results = []

    seen = set()

    for source, url in NEWS_SOURCES:

        try:

            r = requests.get(
                url,
                timeout=8,
                headers={
                    "User-Agent":
                    "Mozilla/5.0"
                }
            )

            if r.status_code != 200:
                continue

            feed = feedparser.parse(
                r.content
            )

            for item in feed.entries[:6]:

                title = clean_html(
                    getattr(
                        item,
                        "title",
                        ""
                    )
                )

                link = getattr(
                    item,
                    "link",
                    ""
                )

                if not title:
                    continue

                key = link or title

                if key in seen:
                    continue

                seen.add(key)

                results.append(
                    (
                        title,
                        link,
                        source
                    )
                )

                if len(results) >= 12:
                    return results

        except Exception as e:

            print(
                "NEWS ERROR:",
                source,
                repr(e)
            )

    return results


async def news_text():

    news = await asyncio.to_thread(
        get_news
    )

    if not news:

        return (
            "❌ فعلاً خبری دریافت نشد."
        )

    result = "📰 آخرین اخبار\n\n"

    for i, (title, link, source) in enumerate(
        news,
        1
    ):

        result += (
            f"{i}. {title}\n"
            f"🗞 منبع: {source}\n"
        )

        if link:
            result += f"🔗 {link}\n"

        result += "\n"

    return result


# =========================================================
# 🌤 WEATHER
# =========================================================

def get_weather(city):

    try:

        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": city,
                "count": 1,
                "language": "fa",
                "format": "json"
            },
            timeout=10
        ).json()

        places = geo.get(
            "results",
            []
        )

        if not places:
            return "❌ شهر پیدا نشد."

        place = places[0]

        weather = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":
                    place["latitude"],

                "longitude":
                    place["longitude"],

                "current": (
                    "temperature_2m,"
                    "relative_humidity_2m,"
                    "apparent_temperature,"
                    "wind_speed_10m"
                ),

                "daily": (
                    "temperature_2m_max,"
                    "temperature_2m_min,"
                    "precipitation_probability_max"
                ),

                "forecast_days": 3,

                "timezone": "auto"
            },
            timeout=10
        ).json()

        current = weather.get(
            "current",
            {}
        )

        daily = weather.get(
            "daily",
            {}
        )

        mins = daily.get(
            "temperature_2m_min",
            ["?"]
        )

        maxs = daily.get(
            "temperature_2m_max",
            ["?"]
        )

        rain = daily.get(
            "precipitation_probability_max",
            ["?"]
        )

        return (
            f"🌤️ آب‌وهوای {place.get('name', city)}\n\n"
            f"🌡 دما: "
            f"{current.get('temperature_2m', '?')}°C\n"
            f"🤔 احساس‌شده: "
            f"{current.get('apparent_temperature', '?')}°C\n"
            f"💧 رطوبت: "
            f"{current.get('relative_humidity_2m', '?')}%\n"
            f"💨 باد: "
            f"{current.get('wind_speed_10m', '?')} km/h\n\n"
            f"📅 پیش‌بینی امروز:\n"
            f"{mins[0]}° تا {maxs[0]}°\n"
            f"🌧 احتمال بارش: {rain[0]}%"
        )

    except Exception as e:

        print(
            "WEATHER ERROR:",
            repr(e)
        )

        return (
            "❌ دریافت آب‌وهوا ناموفق بود."
        )


# =========================================================
# 🔎 SEARCH
# =========================================================

def search_web(query):

    try:

        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={
                "q": query
            },
            headers={
                "User-Agent":
                "Mozilla/5.0"
            },
            timeout=10
        )

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            r.text,
            "html.parser"
        )

        results = []

        for item in soup.select(
            ".result"
        )[:6]:

            a = item.select_one(
                ".result__a"
            )

            snippet = item.select_one(
                ".result__snippet"
            )

            if not a:
                continue

            results.append({
                "title":
                    a.get_text(
                        " ",
                        strip=True
                    ),

                "url":
                    a.get(
                        "href",
                        ""
                    ),

                "snippet":
                    snippet.get_text(
                        " ",
                        strip=True
                    )
                    if snippet else ""
            })

        return results

    except Exception as e:

        print(
            "SEARCH ERROR:",
            repr(e)
        )

        return []


async def search_text(query):

    results = await asyncio.to_thread(
        search_web,
        query
    )

    if not results:

        return "❌ نتیجه‌ای پیدا نشد."

    text = (
        f"🔎 نتایج جستجو برای:\n"
        f"«{query}»\n\n"
    )

    for i, item in enumerate(
        results,
        1
    ):

        text += (
            f"{i}. {item['title']}\n"
            f"{item['snippet']}\n"
            f"🔗 {item['url']}\n\n"
        )

    return text


# =========================================================
# 📢 BROADCAST
# =========================================================

async def broadcast(text):

    users = db.execute("""
    SELECT user_id
    FROM users
    """).fetchall()

    sent = 0
    failed = 0

    for user in users:

        try:

            await bot.send_message(
                user["user_id"],
                text
            )

            sent += 1

            await asyncio.sleep(
                0.1
            )

        except Exception as e:

            print(
                "BROADCAST ERROR:",
                repr(e)
            )

            failed += 1

    return sent, failed


# =========================================================
# 🤖 BOT
# =========================================================

bot = Bot(
    token=BALE_TOKEN
)


# =========================================================
# 🚀 STARTUP
# =========================================================

@bot.event
async def on_before_ready():

    try:
        await bot.delete_webhook()
    except Exception:
        pass


@bot.event
async def on_ready():

    print(
        "================================"
    )

    print(
        "🤖 گرشا AI V3"
    )

    print(
        "✅ Bot is ready!"
    )

    print(
        "================================"
    )


# =========================================================
# 💬 MESSAGE
# =========================================================

@bot.event
async def on_message(message: Message):

    try:

        register_user(message)

        uid = get_uid(message)

        if not uid:
            return

        # مهم:
        # در python-bale-bot متن پیام content است.
        text = message.content

        if not text:
            return

        text = text.strip()

        # =====================================================
        # START
        # =====================================================

        if text == "/start":

            await message.reply(
                "🤖 سلام داش! 👋\n\n"
                "به **هوش مصنوعی گرشا** خوش اومدی 🔥\n\n"
                "از منوی زیر انتخاب کن:",
                components=main_menu()
            )

            return


        # =====================================================
        # MENU
        # =====================================================

        if text == "/menu":

            await message.reply(
                "🪟 منوی اصلی:",
                components=main_menu()
            )

            return


        # =====================================================
        # USAGE
        # =====================================================

        if text == "/usage":

            await message.reply(
                usage_text(uid)
            )

            return


        # =====================================================
        # MEMORY
        # =====================================================

        if text == "/memory":

            clear_memory(uid)

            await message.reply(
                "🧹 حافظه گفتگوی شما پاک شد."
            )

            return


        # =====================================================
        # ADMIN
        # =====================================================

        if text == "/admin":

            if not is_admin(uid):

                await message.reply(
                    "⛔ دسترسی ندارید."
                )

                return

            await message.reply(
                "👑 پنل مدیریت",
                components=admin_menu()
            )

            return


        # =====================================================
        # STATS
        # =====================================================

        if text == "/stats":

            if not is_admin(uid):

                await message.reply(
                    "⛔ دسترسی ندارید."
                )

                return

            await message.reply(
                get_stats()
            )

            return


        # =====================================================
        # BROADCAST
        # =====================================================

        if text.startswith("/broadcast"):

            if not is_admin(uid):

                await message.reply(
                    "⛔ دسترسی ندارید."
                )

                return

            broadcast_text = text[
                len("/broadcast"):
            ].strip()

            if not broadcast_text:

                await message.reply(
                    "مثال:\n\n"
                    "/broadcast سلام 👋"
                )

                return

            waiting = await message.reply(
                "📢 ارسال همگانی شروع شد..."
            )

            sent, failed = await broadcast(
                broadcast_text
            )

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                "✅ ارسال تمام شد.\n\n"
                f"📨 موفق: {sent}\n"
                f"❌ ناموفق: {failed}"
            )

            return


        # =====================================================
        # WEATHER
        # =====================================================

        if text.startswith("/weather"):

            city = text[
                len("/weather"):
            ].strip()

            if not city:

                await message.reply(
                    "مثال:\n"
                    "/weather تهران"
                )

                return

            waiting = await message.reply(
                "🌤️ در حال دریافت آب‌وهوا..."
            )

            result = await asyncio.to_thread(
                get_weather,
                city
            )

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                result
            )

            return


        # =====================================================
        # NEWS
        # =====================================================

        if text == "/news":

            waiting = await message.reply(
                "📰 در حال دریافت اخبار..."
            )

            result = await news_text()

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                result
            )

            return


        # =====================================================
        # SEARCH
        # =====================================================

        if text.startswith("/search"):

            query = text[
                len("/search"):
            ].strip()

            if not query:

                await message.reply(
                    "مثال:\n"
                    "/search اخبار فناوری"
                )

                return

            waiting = await message.reply(
                "🔎 در حال جستجو..."
            )

            result = await search_text(
                query
            )

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                result
            )

            return


        # =====================================================
        # AI
        # =====================================================

        if not can_use_ai(uid):

            await message.reply(
                usage_text(uid)
                +
                "\n\n⛔ سهمیه فعلی تمام شده."
            )

            return

        # پیام انتظار
        waiting = await message.reply(
            "⏳ در حال فکر کردن... ✨👀"
        )

        answer = await ask_ai(
            uid,
            text
        )

        # حذف پیام انتظار
        try:
            await waiting.delete()
        except Exception:
            pass

        await message.reply(
            answer
        )

    except Exception as e:

        print(
            "MESSAGE ERROR:",
            repr(e)
        )


# =========================================================
# 🖱️ CALLBACK
# =========================================================

@bot.event
async def on_callback(
    callback: CallbackQuery
):

    try:

        uid = str(
            callback.from_user.id
        )

        data = callback.data or ""

        msg = callback.message

        # =====================================================
        # HOME
        # =====================================================

        if data == "home":

            await msg.edit(
                "🪟 منوی اصلی:",
                components=main_menu()
            )

            return


        # =====================================================
        # AI
        # =====================================================

        if data == "ai":

            await msg.edit(
                "🤖 هوش مصنوعی گرشا\n\n"
                "پیامت رو بفرست تا جواب بدم.\n\n"
                + usage_text(uid),
                components=main_menu()
            )

            return


        # =====================================================
        # MEMORY
        # =====================================================

        if data == "memory":

            await msg.edit(
                "🧠 حافظه فعال است.\n\n"
                "گفتگوهای اخیر برای ادامه بهتر مکالمه "
                "ذخیره می‌شوند.\n\n"
                "برای پاک کردن:\n"
                "/memory",
                components=main_menu()
            )

            return


        # =====================================================
        # NEWS
        # =====================================================

        if data == "news":

            await msg.edit(
                "📰 در حال دریافت اخبار..."
            )

            result = await news_text()

            await msg.edit(
                result,
                components=main_menu()
            )

            return


        # =====================================================
        # WEATHER
        # =====================================================

        if data == "weather":

            await msg.edit(
                "🌤️ برای دریافت آب‌وهوا بنویس:\n\n"
                "/weather تهران\n\n"
                "مثلاً:\n"
                "/weather مشهد",
                components=main_menu()
            )

            return


        # =====================================================
        # SEARCH
        # =====================================================

        if data == "search":

            await msg.edit(
                "🔎 برای جستجو بنویس:\n\n"
                "/search آخرین اخبار فناوری",
                components=main_menu()
            )

            return


        # =====================================================
        # USAGE
        # =====================================================

        if data == "usage":

            await msg.edit(
                usage_text(uid),
                components=main_menu()
            )

            return


        # =====================================================
        # SETTINGS
        # =====================================================

        if data == "settings":

            await msg.edit(
                "⚙️ تنظیمات مدل\n\n"
                f"مدل فعلی:\n"
                f"{get_model(uid)}\n\n"
                "یک مدل انتخاب کن:",
                components=settings_menu()
            )

            return


        # =====================================================
        # MODEL
        # =====================================================

        if data.startswith("model:"):

            model = data.split(
                ":",
                1
            )[1]

            set_model(
                uid,
                model
            )

            await msg.edit(
                "✅ مدل تغییر کرد.\n\n"
                f"مدل فعلی:\n{model}",
                components=main_menu()
            )

            return


        # =====================================================
        # ADMIN STATS
        # =====================================================

        if data == "admin_stats":

            if not is_admin(uid):
                return

            await msg.edit(
                get_stats(),
                components=admin_menu()
            )

            return


        # =====================================================
        # ADMIN BROADCAST
        # =====================================================

        if data == "admin_broadcast":

            if not is_admin(uid):
                return

            await msg.edit(
                "📢 ارسال همگانی\n\n"
                "بنویس:\n\n"
                "/broadcast متن پیام",
                components=admin_menu()
            )

            return

    except Exception as e:

        print(
            "CALLBACK ERROR:",
            repr(e)
        )


# =========================================================
# ▶️ RUN
# =========================================================


# =========================================================
# 🌐 RENDER FREE WEB-SERVICE HEALTH SERVER
# =========================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"GerSha AI Bot is running."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"🌐 Health server listening on 0.0.0.0:{port}")
    server.serve_forever()


threading.Thread(target=start_health_server, daemon=True).start()

print(
    "🚀 Starting GerSha AI V3..."
)

bot.run()
