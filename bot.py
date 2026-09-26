import os
import re
import json
import time
import threading

import telebot
from telebot import types
from flask import Flask
from google import genai

# ─── কনফিগারেশন ───
BOT_TOKEN = os.environ.get("MOD_BOT_TOKEN")

GEMINI_KEYS = [os.environ.get(f"GEMINI_KEY_{i}") for i in range(1, 16)]  # GEMINI_KEY_1 থেকে GEMINI_KEY_15
GEMINI_KEYS = [k for k in GEMINI_KEYS if k]  # যেগুলো এখনো সেট করা হয়নি সেগুলো বাদ

if not BOT_TOKEN:
    raise ValueError("MOD_BOT_TOKEN environment variable সেট করতে হবে")
if not GEMINI_KEYS:
    raise ValueError("কমপক্ষে একটা GEMINI_KEY_1 environment variable সেট করতে হবে")

GROUP_ID = -1003144160463          # বট শুধু এই গ্রুপেই কাজ করবে
ADMIN_ID = 6625019627              # শুধু এই আইডি অ্যাডমিন প্যানেল ব্যবহার করতে পারবে
INCOME_CHANNEL_LINK = "https://t.me/+LZrutZRrpbRkNDVl"

BOT_NAME = "Mamun Assistant"
CREATOR_REPLY = "আমাকে মামুন ভাই বানিয়েছে। কোনো সাহায্য লাগলে এখানে বলবেন। 🙂"
CREATOR_PATTERNS = [
    "কে বানিয়েছে", "কে তৈরি করেছে", "কে বানাইছে", "কে বানাইলো", "কে বানালো", "কে ডেভেলপ করেছে",
    "who made you", "who created you", "your creator", "who developed you", "your developer",
]

default_clients = [genai.Client(api_key=k) for k in GEMINI_KEYS]
current_client_index = 0
MODEL = "gemini-3.6-flash"
GEMINI_TIMEOUT = 20  # সেকেন্ড

bot = telebot.TeleBot(BOT_TOKEN, num_threads=30)

# বটের নিজের আইডি/ইউজারনেম, মেনশন/রিপ্লাই চেনার জন্য
try:
    _me = bot.get_me()
    BOT_ID = _me.id
    BOT_USERNAME = (_me.username or "").lower()
except Exception:
    BOT_ID = None
    BOT_USERNAME = ""

# ─── অ্যাডমিন প্যানেল থেকে বদলানো যায় এমন সেটিংস (মেমোরিতে, রিস্টার্টে ডিফল্টে ফিরবে) ───
settings = {
    "bad_language_on": True,
    "scam_on": True,
    "delete_scam": True,
    "income_redirect_on": True,
    "qa_on": True,
}
stats = {
    "bad_language_warnings": 0,
    "scam_deleted": 0,
    "scam_warned": 0,
    "income_redirects": 0,
    "questions_answered": 0,
}


# ─── Gemini call: timeout + key rotation ───
class GeminiTimeout(Exception):
    pass


def run_with_timeout(fn, timeout):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise GeminiTimeout("timeout")
    if "error" in box:
        raise box["error"]
    return box["value"]


def is_rate_limit_error(err_text):
    low = err_text.lower()
    return (
        re.search(r"\b429\b", err_text) is not None
        or "quota" in low
        or "resource_exhausted" in low
        or "rate limit" in low
        or "rate_limit" in low
    )


def call_gemini(prompt):
    global current_client_index
    deadline = time.time() + GEMINI_TIMEOUT
    for i in range(len(default_clients)):
        time_left = deadline - time.time()
        if time_left <= 0:
            break
        idx = (current_client_index + i) % len(default_clients)
        try:
            interaction = run_with_timeout(
                lambda c=default_clients[idx]: c.interactions.create(model=MODEL, input=prompt),
                time_left,
            )
            current_client_index = idx
            return interaction.output_text.strip()
        except GeminiTimeout:
            break
        except Exception as e:
            if is_rate_limit_error(str(e)):
                continue
            raise
    return None


# ─── মডারেশন: ক্লাসিফিকেশন + রিপ্লাই, এক Gemini কলে ───
ANALYZE_PROMPT = """তুমি একটা Telegram গ্রুপ মডারেটর সহকারী। নিচের মেসেজটা বিশ্লেষণ করো এবং চারটা ক্যাটাগরির একটায় ফেলো:

মেসেজ: \"\"\"{text}\"\"\"

- "bad_language": গালি, অপমান, কাউকে ছোট করা বা অশ্লীল ভাষা আছে (যেকোনো ভাষায় হতে পারে — বাংলা, ইংরেজি, হিন্দি, Banglish ইত্যাদি)।
- "scam_lure": মেসেজদাতা নিজেই সহজ টাকা ইনকাম, গ্যারান্টিড প্রফিট, বিনিয়োগ করলেই ডাবল, জুয়া/বেটিং এর মতো প্রলোভনমূলক অফার প্রচার/পোস্ট করছে।
- "income_seeking": মেসেজদাতা নিজে কোনো অফার দিচ্ছে না, বরং নতুন ইনকামের উপায়, ইনকামের সাইট, বা কাজ/সাইট খুঁজছে বা জিজ্ঞেস করছে।
- "normal": উপরের কোনোটাই না।

শুধু নিচের JSON ফরম্যাটে উত্তর দাও, আর কিছু লিখো না, কোনো কোড ব্লক বা ব্যাখ্যা ছাড়া:
{{"category": "bad_language অথবা scam_lure অথবা income_seeking অথবা normal", "reply": "..."}}

reply ফিল্ডের নিয়ম (সবসময় বাংলায় লিখবে, মেসেজদাতা যে ভাষাতেই লিখুক না কেন):
- "bad_language" হলে: ৩-৫ লাইনে, খুবই নরম ও ভালোবাসাপূর্ণ ভাষায়, ইসলামের সাধারণ শিক্ষার আলোকে (নির্দিষ্ট আয়াত/হাদিস নম্বর উল্লেখ না করে) বুঝিয়ে লেখো কেন ভালো ও নম্র ভাষায় কথা বলা উচিত। অপমান করা যাবে না।
- "scam_lure" হলে: ২-৩ লাইনে বুঝিয়ে বলো কেন এই অফার সন্দেহজনক এবং সতর্ক থাকতে বলো।
- "income_seeking" হলে: ১-২ লাইনে বন্ধুত্বপূর্ণভাবে বলো নিচের বাটনে চ্যানেলে তথ্য পাওয়া যাবে।
- "normal" হলে: reply খালি স্ট্রিং ("") রাখো।
"""


def analyze_message(text):
    raw = call_gemini(ANALYZE_PROMPT.format(text=text.replace('"""', "'")))
    if not raw:
        return "normal", ""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned.strip())
        category = data.get("category", "normal")
        if category not in ("bad_language", "scam_lure", "income_seeking", "normal"):
            category = "normal"
        return category, (data.get("reply") or "").strip()
    except Exception:
        return "normal", ""


DEFAULT_ADVICE = "🌿 ভাই/বোন, আসুন আমরা একে অপরের সাথে নরম ও সুন্দর ভাষায় কথা বলি। ভালো ও নম্র কথা বলা ইসলামের একটা গুরুত্বপূর্ণ শিক্ষা। 🤲"
DEFAULT_SCAM_WARNING = "⚠️ এই ধরনের \"সহজে বেশি টাকা ইনকাম\" অফার প্রায়ই প্রতারণা হয়ে থাকে। টাকা পাঠানো বা লিংকে ক্লিক করার আগে অবশ্যই যাচাই করে নিন।"
DEFAULT_INCOME_REPLY = "📢 নিচের চ্যানেলে ইনকাম ও ভালো সাইট সংক্রান্ত তথ্য পাবেন।"

SCAM_TEMPLATE = "⚠️ *সতর্কতা: সম্ভাব্য প্রতারণা / লোভনীয় অফার*\n\n{reply}{delete_note}"
DELETED_NOTE = "\n\n🗑 মেসেজটি নিরাপত্তার জন্য মুছে ফেলা হলো।"
NOT_DELETED_NOTE = "\n\n⚠️ মেসেজটি মুছতে পারিনি — আমাকে গ্রুপে *Admin* করে *Delete Messages* পারমিশন দিন।"


def income_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 চ্যানেলে জয়েন করুন", url=INCOME_CHANNEL_LINK))
    return markup


# ─── Q&A সহকারী: "Mamun Assistant" ───
ASSISTANT_PROMPT = """তুমি একটা Telegram গ্রুপের AI সহকারী, তোমার নাম "Mamun Assistant"। তোমাকে "মামুন ভাই" বানিয়েছেন।

নিচের প্রশ্নের উত্তর বাংলায়, সহজ, বন্ধুত্বপূর্ণ ও সংক্ষিপ্তভাবে দাও (দরকার হলে কয়েক লাইনে বিস্তারিত লিখতে পারো, কিন্তু অকারণে লম্বা কোরো না):

প্রশ্ন: \"\"\"{text}\"\"\"

নিয়ম:
- নিজের পরিচয় জিজ্ঞেস করলে বলবে তোমার নাম "Mamun Assistant"।
- উত্তর সঠিক ও সহায়ক হতে হবে; নিশ্চিত না হলে সততার সাথে বলবে যে নিশ্চিত না।
- কোনো গালি বা অসম্মানজনক ভাষা ব্যবহার করবে না।
"""


def is_creator_question(text):
    low = text.lower()
    return any(p.lower() in low for p in CREATOR_PATTERNS)


def get_assistant_answer(text):
    if is_creator_question(text):
        return CREATOR_REPLY
    raw = call_gemini(ASSISTANT_PROMPT.format(text=text.replace('"""', "'")))
    return raw if raw else "দুঃখিত, এই মুহূর্তে উত্তর দিতে পারছি না। একটু পরে আবার চেষ্টা করুন।"


def strip_mention(text):
    if BOT_USERNAME:
        text = re.sub(r"@" + re.escape(BOT_USERNAME), "", text, flags=re.IGNORECASE)
    return re.sub(r"\b" + re.escape(BOT_NAME) + r"\b", "", text, flags=re.IGNORECASE).strip()


def is_addressed_to_bot(message):
    reply = getattr(message, "reply_to_message", None)
    if reply is not None and BOT_ID is not None and getattr(getattr(reply, "from_user", None), "id", None) == BOT_ID:
        return True
    text = (message.text or "").lower()
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
        return True
    if BOT_NAME.lower() in text:
        return True
    return False


def _edit_or_fallback(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown")
    except Exception:
        try:
            bot.edit_message_text(text, chat_id, message_id)
        except Exception:
            pass


def handle_question(message):
    question = strip_mention(message.text or "")
    if not question:
        question = "সালাম, তুমি কেমন আছো?"
    try:
        sent = bot.send_message(
            message.chat.id, "*Typing...*", parse_mode="Markdown", reply_to_message_id=message.message_id
        )
    except Exception:
        return
    try:
        answer = get_assistant_answer(question)
    except Exception:
        answer = "দুঃখিত, এই মুহূর্তে উত্তর দিতে পারছি না। একটু পরে আবার চেষ্টা করুন।"
    stats["questions_answered"] += 1
    _edit_or_fallback(message.chat.id, sent.message_id, answer)


def handle_message(message):
    text = message.text or ""
    if not text.strip():
        return

    if settings["qa_on"] and is_addressed_to_bot(message):
        handle_question(message)
        return

    if len(text.strip()) < 4:
        return
    if not (settings["bad_language_on"] or settings["scam_on"] or settings["income_redirect_on"]):
        return  # সব ফিচার বন্ধ থাকলে অকারণে Gemini কল করার দরকার নেই

    try:
        category, reply = analyze_message(text)
    except Exception:
        return  # চুপচাপ থামা ভালো, গ্রুপে এরর মেসেজ দেখানো ঠিক না

    if category == "scam_lure" and settings["scam_on"]:
        deleted = False
        if settings["delete_scam"]:
            try:
                bot.delete_message(message.chat.id, message.message_id)
                deleted = True
            except Exception:
                deleted = False
        stats["scam_deleted" if deleted else "scam_warned"] += 1
        bot.send_message(
            message.chat.id,
            SCAM_TEMPLATE.format(
                reply=reply or DEFAULT_SCAM_WARNING,
                delete_note=DELETED_NOTE if deleted else NOT_DELETED_NOTE,
            ),
            parse_mode="Markdown",
        )

    elif category == "bad_language" and settings["bad_language_on"]:
        stats["bad_language_warnings"] += 1
        bot.reply_to(message, reply or DEFAULT_ADVICE)

    elif category == "income_seeking" and settings["income_redirect_on"]:
        stats["income_redirects"] += 1
        bot.reply_to(message, reply or DEFAULT_INCOME_REPLY, reply_markup=income_markup())


@bot.message_handler(
    func=lambda m: (
        m.chat.id == GROUP_ID
        and m.content_type == "text"
        and not (m.text or "").startswith("/")
        and not getattr(m.from_user, "is_bot", False)
    )
)
def moderate(message):
    threading.Thread(target=handle_message, args=(message,), daemon=True).start()


# ─── Admin Control Panel (শুধু প্রাইভেট চ্যাটে, শুধু ADMIN_ID) ───
def panel_text():
    def onoff(key):
        return "✅ চালু" if settings[key] else "❌ বন্ধ"

    return (
        "🛠 *Admin Control Panel*\n\n"
        f"🗣 গালি সনাক্তকরণ: {onoff('bad_language_on')}\n"
        f"⚠️ স্ক্যাম সনাক্তকরণ: {onoff('scam_on')}\n"
        f"🗑 স্ক্যাম মেসেজ ডিলিট: {onoff('delete_scam')}\n"
        f"📢 ইনকাম-চ্যানেল সাজেশন: {onoff('income_redirect_on')}\n"
        f"🤖 Q&A সহকারী: {onoff('qa_on')}\n"
        f"🔗 চ্যানেল লিংক: {INCOME_CHANNEL_LINK}\n"
        f"🔑 Gemini Key সংখ্যা: {len(default_clients)}\n\n"
        "📊 *Stats*\n"
        f"— উপদেশ দেওয়া হয়েছে: {stats['bad_language_warnings']} বার\n"
        f"— স্ক্যাম মেসেজ মুছা হয়েছে: {stats['scam_deleted']} বার\n"
        f"— স্ক্যাম সতর্ক (মুছা যায়নি): {stats['scam_warned']} বার\n"
        f"— ইনকাম-চ্যানেল সাজেস্ট করা হয়েছে: {stats['income_redirects']} বার\n"
        f"— প্রশ্নের উত্তর দেওয়া হয়েছে: {stats['questions_answered']} বার\n"
    )


def panel_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "🔴 গালি সনাক্তকরণ বন্ধ কর" if settings["bad_language_on"] else "🟢 গালি সনাক্তকরণ চালু কর",
        callback_data="tg:bad_language_on"))
    markup.add(types.InlineKeyboardButton(
        "🔴 স্ক্যাম সনাক্তকরণ বন্ধ কর" if settings["scam_on"] else "🟢 স্ক্যাম সনাক্তকরণ চালু কর",
        callback_data="tg:scam_on"))
    markup.add(types.InlineKeyboardButton(
        "🔴 স্ক্যাম ডিলিট বন্ধ কর" if settings["delete_scam"] else "🟢 স্ক্যাম ডিলিট চালু কর",
        callback_data="tg:delete_scam"))
    markup.add(types.InlineKeyboardButton(
        "🔴 ইনকাম-চ্যানেল সাজেশন বন্ধ কর" if settings["income_redirect_on"] else "🟢 ইনকাম-চ্যানেল সাজেশন চালু কর",
        callback_data="tg:income_redirect_on"))
    markup.add(types.InlineKeyboardButton(
        "🔴 Q&A সহকারী বন্ধ কর" if settings["qa_on"] else "🟢 Q&A সহকারী চালু কর",
        callback_data="tg:qa_on"))
    markup.add(types.InlineKeyboardButton("🔗 চ্যানেল লিংক বদলাও", callback_data="set_link"))
    markup.add(types.InlineKeyboardButton("♻️ Stats রিসেট করো", callback_data="reset_stats"))
    markup.add(types.InlineKeyboardButton("🔄 রিফ্রেশ", callback_data="refresh_panel"))
    return markup


def send_panel(chat_id, message_id=None):
    if message_id:
        try:
            bot.edit_message_text(panel_text(), chat_id, message_id, parse_mode="Markdown", reply_markup=panel_markup())
            return
        except Exception:
            pass
    bot.send_message(chat_id, panel_text(), parse_mode="Markdown", reply_markup=panel_markup())


@bot.message_handler(commands=["start", "admin", "panel"])
def start(message):
    if message.chat.type != "private":
        return
    if message.from_user.id == ADMIN_ID:
        send_panel(message.chat.id)
        return
    bot.reply_to(
        message,
        f"🌿 *আসসালামু আলাইকুম! আমি {BOT_NAME}।*\n\n"
        "আমি একটা নির্দিষ্ট গ্রুপে সাহায্যকারী হিসেবে কাজ করি:\n"
        "🗣 কেউ খারাপ/অশ্লীল কথা বললে নরমভাবে ইসলামিক শিক্ষা দিয়ে বুঝাই।\n"
        "⚠️ কেউ স্ক্যাম বা লোভনীয় অফার দিলে সতর্ক করি ও মেসেজ মুছে ফেলি।\n"
        "📢 কেউ ইনকাম/সাইট খুঁজলে সঠিক চ্যানেলের লিংক দিই।\n"
        "🤖 আমাকে মেনশন করে বা রিপ্লাই দিয়ে যেকোনো প্রশ্ন করলে উত্তর দিই।",
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda c: c.from_user.id == ADMIN_ID and (
    (c.data or "").startswith("tg:") or c.data in ("set_link", "reset_stats", "refresh_panel")
))
def admin_callback(call):
    data = call.data
    if data.startswith("tg:"):
        key = data.split(":", 1)[1]
        settings[key] = not settings[key]
        bot.answer_callback_query(call.id, "✅ পরিবর্তন হয়েছে।")
        send_panel(call.message.chat.id, call.message.message_id)

    elif data == "reset_stats":
        for k in stats:
            stats[k] = 0
        bot.answer_callback_query(call.id, "✅ Stats রিসেট হয়েছে।")
        send_panel(call.message.chat.id, call.message.message_id)

    elif data == "refresh_panel":
        bot.answer_callback_query(call.id)
        send_panel(call.message.chat.id, call.message.message_id)

    elif data == "set_link":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "🔗 নতুন চ্যানেল লিংক পেস্ট করো (http/https দিয়ে শুরু):")
        bot.register_next_step_handler(msg, save_link)


def save_link(message):
    global INCOME_CHANNEL_LINK
    if message.from_user.id != ADMIN_ID:
        return
    link = (message.text or "").strip()
    if not link.startswith("http"):
        bot.send_message(message.chat.id, "⚠️ সঠিক লিংক দাও, http/https দিয়ে শুরু হতে হবে।")
    else:
        INCOME_CHANNEL_LINK = link
        bot.send_message(message.chat.id, "✅ চ্যানেল লিংক আপডেট হয়েছে।")
    send_panel(message.chat.id)


# ─── বটকে জাগিয়ে রাখার জন্য ছোট্ট ওয়েব সার্ভার ───
# Render-এর মতো ফ্রি হোস্টিং ১৫ মিনিট নিষ্ক্রিয় থাকলে বন্ধ (sleep) হয়ে যায়।
# UptimeRobot / cron-job.org থেকে প্রতি ৫-১০ মিনিটে এই URL-এ পিং করলে বট সবসময় জাগ্রত থাকবে।
keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def _keep_alive_home():
    return "OK, bot is running."


def _run_keep_alive_server():
    port = int(os.environ.get("PORT", 8080))
    keep_alive_app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    threading.Thread(target=_run_keep_alive_server, daemon=True).start()
    print("বট চালু হয়েছে...")
    bot.infinity_polling()
