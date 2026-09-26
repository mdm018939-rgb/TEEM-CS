import os
import re
import json
import time
import threading

import telebot
from telebot import types
from google import genai

# ─── কনফিগারেশন ───
BOT_TOKEN = os.environ.get("MOD_BOT_TOKEN")
GEMINI_KEY_1 = os.environ.get("GEMINI_KEY_1")
GEMINI_KEY_2 = os.environ.get("GEMINI_KEY_2")

if not BOT_TOKEN or not GEMINI_KEY_1 or not GEMINI_KEY_2:
    raise ValueError("MOD_BOT_TOKEN, GEMINI_KEY_1, GEMINI_KEY_2 environment variable সেট করতে হবে")

GROUP_ID = -1003144160463          # বট শুধু এই গ্রুপেই কাজ করবে
ADMIN_ID = 6625019627              # শুধু এই আইডি অ্যাডমিন প্যানেল ব্যবহার করতে পারবে
INCOME_CHANNEL_LINK = "https://t.me/+LZrutZRrpbRkNDVl"

default_clients = [
    genai.Client(api_key=GEMINI_KEY_1),
    genai.Client(api_key=GEMINI_KEY_2),
]
current_client_index = 0
MODEL = "gemini-3.6-flash"
GEMINI_TIMEOUT = 20     # সেকেন্ড: ক্লাসিফিকেশন দ্রুত হওয়া দরকার

bot = telebot.TeleBot(BOT_TOKEN, num_threads=30)
last_warned = {}  # (chat_id, user_id) -> শেষ কবে উপদেশ পাঠানো হয়েছে

# ─── অ্যাডমিন প্যানেল থেকে বদলানো যায় এমন সেটিংস (মেমোরিতে, রিস্টার্টে ডিফল্টে ফিরবে) ───
settings = {
    "bad_language_on": True,
    "scam_on": True,
    "delete_scam": True,
    "income_redirect_on": True,
    "cooldown": 60,  # সেকেন্ড: একই ইউজারকে বারবার লেকচার না দিতে
}
stats = {
    "bad_language_warnings": 0,
    "scam_deleted": 0,
    "scam_warned": 0,
    "income_redirects": 0,
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


# ─── ক্লাসিফিকেশন + রিপ্লাই, দুটোই এক Gemini কলে ───
ANALYZE_PROMPT = """তুমি একটা Telegram গ্রুপ মডারেটর সহকারী। নিচের মেসেজটা বিশ্লেষণ করো এবং চারটা ক্যাটাগরির একটায় ফেলো:

মেসেজ: \"\"\"{text}\"\"\"

- "bad_language": গালি, অপমান, কাউকে ছোট করা বা অশ্লীল ভাষা আছে (যেকোনো ভাষায় হতে পারে — বাংলা, ইংরেজি, হিন্দি, Banglish ইত্যাদি)।
- "scam_lure": মেসেজদাতা নিজেই সহজ টাকা ইনকাম, গ্যারান্টিড প্রফিট, বিনিয়োগ করলেই ডাবল, জুয়া/বেটিং এর মতো প্রলোভনমূলক অফার প্রচার/পোস্ট করছে।
- "income_seeking": মেসেজদাতা নিজে কোনো অফার দিচ্ছে না, বরং নতুন ইনকামের উপায়, ইনকামের সাইট, বা কাজ/সাইট খুঁজছে বা জিজ্ঞেস করছে (যেমন "কেউ ভালো ইনকামের সাইট বলবেন?", "নতুন একটা কাজ দরকার", "কোনো সাইট থাকলে বলেন")।
- "normal": উপরের কোনোটাই না।

শুধু নিচের JSON ফরম্যাটে উত্তর দাও, আর কিছু লিখো না, কোনো কোড ব্লক বা ব্যাখ্যা ছাড়া:
{{"category": "bad_language অথবা scam_lure অথবা income_seeking অথবা normal", "reply": "..."}}

reply ফিল্ডের নিয়ম (সবসময় বাংলায় লিখবে, মেসেজদাতা যে ভাষাতেই লিখুক না কেন):
- category "bad_language" হলে: ৩-৫ লাইনে, খুবই নরম ও ভালোবাসাপূর্ণ ভাষায়, ইসলামের সাধারণ শিক্ষার আলোকে (নির্দিষ্ট আয়াত/হাদিস নম্বর উল্লেখ না করে) বুঝিয়ে লেখো কেন ভালো ও নম্র ভাষায় কথা বলা উচিত। তাকে অপমান, "পাপী" বা এমন কিছু বলা যাবে না। কিছু ইমোজি ব্যবহার করতে পারো।
- category "scam_lure" হলে: ২-৩ লাইনে বুঝিয়ে বলো কেন এই ধরনের অফার সন্দেহজনক এবং টাকা পাঠানো বা লিংকে ক্লিক করার আগে সতর্ক থাকতে বলো।
- category "income_seeking" হলে: ১-২ লাইনে বন্ধুত্বপূর্ণভাবে বলো যে নিচের বাটনে ইনকাম/সাইট সংক্রান্ত তথ্যের চ্যানেলে জয়েন করতে পারে।
- category "normal" হলে: reply খালি স্ট্রিং ("") রাখো।
"""


def analyze_message(text):
    """রিটার্ন করে (category, reply_text)। কোনো সমস্যা হলে নিরাপদভাবে ("normal", "") দেয়,
    যাতে ভুল করে কোনো স্বাভাবিক মেসেজ ডিলিট/ফ্ল্যাগ না হয়।"""
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


def handle_message(message):
    text = message.text or ""
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
        key = (message.chat.id, message.from_user.id)
        now = time.time()
        if now - last_warned.get(key, 0) < settings["cooldown"]:
            return
        last_warned[key] = now
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
        f"⏱ Cooldown: {settings['cooldown']} সেকেন্ড\n"
        f"🔗 চ্যানেল লিংক: {INCOME_CHANNEL_LINK}\n\n"
        "📊 *Stats*\n"
        f"— উপদেশ দেওয়া হয়েছে: {stats['bad_language_warnings']} বার\n"
        f"— স্ক্যাম মেসেজ মুছা হয়েছে: {stats['scam_deleted']} বার\n"
        f"— স্ক্যাম সতর্ক (মুছা যায়নি): {stats['scam_warned']} বার\n"
        f"— ইনকাম-চ্যানেল সাজেস্ট করা হয়েছে: {stats['income_redirects']} বার\n"
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
    markup.add(types.InlineKeyboardButton("⏱ Cooldown বদলাও", callback_data="set_cooldown"))
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
        return  # গ্রুপে কমান্ড রিপ্লাই করার দরকার নেই
    if message.from_user.id == ADMIN_ID:
        send_panel(message.chat.id)
        return
    bot.reply_to(
        message,
        "🌿 *আসসালামু আলাইকুম!*\n\n"
        "আমি একটা নির্দিষ্ট গ্রুপে সাহায্যকারী হিসেবে কাজ করি:\n"
        "🗣 কেউ খারাপ/অশ্লীল কথা বললে নরমভাবে ইসলামিক শিক্ষা দিয়ে বুঝাই।\n"
        "⚠️ কেউ স্ক্যাম বা লোভনীয় অফার দিলে সতর্ক করি ও মেসেজ মুছে ফেলি।\n"
        "📢 কেউ ইনকাম/সাইট খুঁজলে সঠিক চ্যানেলের লিংক দিই।",
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda c: c.from_user.id == ADMIN_ID and (
    (c.data or "").startswith("tg:") or c.data in ("set_cooldown", "set_link", "reset_stats", "refresh_panel")
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

    elif data == "set_cooldown":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "⏱ নতুন cooldown কত সেকেন্ড হবে? সংখ্যা লিখো:")
        bot.register_next_step_handler(msg, save_cooldown)

    elif data == "set_link":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "🔗 নতুন চ্যানেল লিংক পেস্ট করো (http/https দিয়ে শুরু):")
        bot.register_next_step_handler(msg, save_link)


def save_cooldown(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = int((message.text or "").strip())
        if val < 0:
            raise ValueError
        settings["cooldown"] = val
        bot.send_message(message.chat.id, f"✅ Cooldown {val} সেকেন্ড করা হলো।")
    except (ValueError, TypeError):
        bot.send_message(message.chat.id, "⚠️ সঠিক একটা সংখ্যা দাও (যেমন 60)।")
    send_panel(message.chat.id)


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


if __name__ == "__main__":
    print("বট চালু হয়েছে...")
    bot.infinity_polling()
