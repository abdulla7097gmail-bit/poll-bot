"""
bot.py — কাস্টম পোল টেলিগ্রাম বট
----------------------------------
বাটন দিয়ে যত খুশি কাস্টম পোল বানানো যায় (নিজের অপশনের নাম দিয়ে),
আনলিমিটেড চ্যানেল/গ্রুপে (বট যেখানে এডমিন) পোস্ট করা যায়,
এবং শুধু সেই চ্যানেল/গ্রুপের এডমিনরাই পোল বন্ধ করতে পারবে।

চালানোর নিয়ম README.md এ বিস্তারিত আছে।
"""

import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "pollbot.db")
MAX_OPTIONS = 20
MIN_OPTIONS = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
CHOOSE_CHAT, ASK_QUESTION, ASK_OPTIONS = range(3)


# ---------------------------------------------------------------------------
# সাহায্যকারী ফাংশন
# ---------------------------------------------------------------------------

def build_poll_keyboard(poll_id: int, options: list, counts: dict) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, (option_id, option_text) in enumerate(options):
        count = counts.get(option_id, 0)
        # বাটন সবসময় আকাশি (sky blue) থাকবে — কে ভোট দিয়েছে তার ভিত্তিতে রঙ বদলাবে না,
        # একই অপশনে একাধিক মানুষ ভোট দিতে পারবে
        style = "primary"
        label = f"🔷 {option_text} ({count})" if count else f"🔷 {option_text}"
        row.append(
            InlineKeyboardButton(
                label, callback_data=f"vote:{poll_id}:{option_id}", style=style
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                "🔍 আমার ভোট দেখুন",
                callback_data=f"myvote:{poll_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🔴 থামান এবং ফলাফল পান",
                callback_data=f"stop:{poll_id}",
                style="danger",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_personal_poll_keyboard(options: list, counts: dict, voted_option_id: int) -> InlineKeyboardMarkup:
    """
    একজন ইউজারের ব্যক্তিগত (DM) পোল-স্ন্যাপশট — শুধু সেই ইউজারই এই মেসেজটা পায়,
    তাই এখানে তার ভোট দেওয়া অপশনটা আলাদা রঙে (সবুজ) হাইলাইট করা নিরাপদ।
    গ্রুপের আসল পোল মেসেজে এই রঙ কেউ দেখবে না, সেটা সবার জন্য আকাশি-ই থাকবে।
    """
    rows = []
    row = []
    for option_id, option_text in options:
        count = counts.get(option_id, 0)
        is_mine = option_id == voted_option_id
        style = "success" if is_mine else "primary"
        label = f"✅ {option_text} ({count})" if is_mine else f"🔷 {option_text} ({count})" if count else f"🔷 {option_text}"
        row.append(InlineKeyboardButton(label, callback_data="noop", style=style))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_poll_text(question: str, total_votes: int) -> str:
    return (
        f"🗳 <b>{question}</b>\n\n"
        f"📈 মোট ভোট: {total_votes}\n\n"
        f"নিচের বাটনে চেপে আপনার পছন্দের অপশনে ভোট দিন 👇"
    )


def build_result_text(question: str, options: list, counts: dict) -> str:
    total = sum(counts.values())
    lines = [f"🔒 <b>{question}</b> — পোল বন্ধ হয়েছে\n", f"📈 মোট ভোট: {total}\n"]
    ranked = sorted(options, key=lambda o: counts.get(o[0], 0), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    for idx, (option_id, option_text) in enumerate(ranked):
        count = counts.get(option_id, 0)
        prefix = medals[idx] if idx < 3 and count > 0 else "▫️"
        lines.append(f"{prefix} {option_text} — {count} ভোট")
    return "\n".join(lines)


async def user_is_chat_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception as e:
        logger.warning("admin check failed for chat %s user %s: %s", chat_id, user_id, e)
        return False


# ---------------------------------------------------------------------------
# my_chat_member — বট কোন চ্যানেল/গ্রুপে এডমিন/মেম্বার হলে/বাদ পড়লে রেজিস্টার করে
# ---------------------------------------------------------------------------

async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    chat = result.chat
    new_status = result.new_chat_member.status
    db.upsert_chat(chat.id, chat.title or chat.full_name or str(chat.id), chat.type, new_status)
    logger.info("chat %s (%s) -> status %s", chat.id, chat.title, new_status)


# ---------------------------------------------------------------------------
# /start ও মূল মেনু — এখন এটা চ্যাটের নিচে সবসময় থাকা একটা মেনু (বড় বড় বটের মতো),
# ইনলাইন বাটনের বদলে টেক্সট-কিবোর্ড (ReplyKeyboardMarkup) ব্যবহার করা হচ্ছে
# ---------------------------------------------------------------------------

MENU_NEWPOLL_TEXT = "📊 নতুন পোল তৈরি করুন"
MENU_MYCHATS_TEXT = "📋 আমার চ্যানেল/গ্রুপ"
MENU_HELP_TEXT = "❓ সাহায্য"

_menu_filter = lambda text: filters.Regex(f"^{re.escape(text)}$")

MAIN_MENU_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [MENU_NEWPOLL_TEXT],
        [MENU_MYCHATS_TEXT, MENU_HELP_TEXT],
    ],
    resize_keyboard=True,       # বাটনগুলো ছোট/ফিট হয়ে থাকবে
    is_persistent=True,         # চ্যাট থেকে কিবোর্ড আইকনে ক্লিক না করেও সবসময় দেখা যাবে
)

HELP_TEXT = (
    "🤖 <b>পোল বট ব্যবহারের নিয়ম</b>\n\n"
    "১. এই বটকে যেই চ্যানেল/গ্রুপে পোল বানাতে চান, সেখানে <b>এডমিন</b> হিসেবে যোগ করুন "
    "(মেসেজ পাঠানোর পারমিশন সহ)।\n"
    "২. নিচের মেনু থেকে \"📊 নতুন পোল তৈরি করুন\" চাপুন।\n"
    "৩. কোন চ্যানেল/গ্রুপে পোল যাবে সেটা বেছে নিন — শুধু সেই চ্যাটগুলোই দেখাবে যেখানে "
    "আপনি নিজে এডমিন এবং বটও এডমিন।\n"
    "৪. পোলের প্রশ্ন লিখুন, তারপর একটার পর একটা অপশনের নাম লিখুন। শেষ হলে "
    "\"✅ শেষ করুন\" চাপুন (নূন্যতম ২টি অপশন লাগবে)।\n"
    "৫. পোল পোস্ট হয়ে যাবে — যে কেউ বাটনে চেপে ভোট দিতে পারবে।\n"
    "৬. শুধু ওই চ্যানেল/গ্রুপের <b>এডমিনরাই</b> \"🔴 থামান এবং ফলাফল পান\" বাটনে চেপে "
    "পোল বন্ধ করতে পারবে ও ফলাফল দেখতে পারবে।\n\n"
    "কমান্ড: /start /newpoll /mychats /cancel /help\n"
    "অথবা নিচের মেনু থেকে সরাসরি বেছে নিন 👇"
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "স্বাগতম! এখান থেকে যত খুশি কাস্টম পোল বানাতে পারবেন এবং যেকোনো "
        "চ্যানেল/গ্রুপে পোস্ট করতে পারবেন (যেখানে আপনি ও বট দুজনেই এডমিন)।\n\n"
        "নিচে মেনু থেকে যা করতে চান বেছে নিন 👇",
        reply_markup=MAIN_MENU_REPLY_KEYBOARD,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def mychats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_my_chats(update.effective_message, context, update.effective_user.id)


async def show_my_chats(message, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    chats = db.list_active_chats()
    admin_chats = []
    for chat_id, title, chat_type in chats:
        if await user_is_chat_admin(context, chat_id, user_id):
            admin_chats.append((chat_id, title, chat_type))

    if not admin_chats:
        await message.reply_text(
            "আপনি কোনো চ্যানেল/গ্রুপে এডমিন নন যেখানে এই বটও এডমিন আছে।\n\n"
            "প্রথমে বটকে আপনার চ্যানেল/গ্রুপে এডমিন হিসেবে যোগ করুন, তারপর আবার চেষ্টা করুন।"
        )
        return

    lines = ["📋 <b>আপনার চ্যানেল/গ্রুপ যেখানে পোল বানাতে পারবেন:</b>\n"]
    for chat_id, title, chat_type in admin_chats:
        kind = "চ্যানেল" if chat_type == "channel" else "গ্রুপ"
        lines.append(f"• {title} ({kind})")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# পোল তৈরির Conversation
# ---------------------------------------------------------------------------

async def newpoll_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        message = query.message
    else:
        message = update.message

    user_id = update.effective_user.id
    chats = db.list_active_chats()
    admin_chats = []
    for chat_id, title, chat_type in chats:
        if await user_is_chat_admin(context, chat_id, user_id):
            admin_chats.append((chat_id, title, chat_type))

    if not admin_chats:
        await message.reply_text(
            "আপনি কোনো চ্যানেল/গ্রুপে এডমিন নন যেখানে এই বটও এডমিন আছে।\n\n"
            "প্রথমে বটকে আপনার চ্যানেল/গ্রুপে এডমিন হিসেবে যোগ করে আবার /newpoll দিন।"
        )
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(title, callback_data=f"chat:{chat_id}")]
        for chat_id, title, chat_type in admin_chats
    ]
    buttons.append([InlineKeyboardButton("❌ বাতিল", callback_data="cancel")])
    await message.reply_text(
        "কোন চ্যানেল/গ্রুপে পোল পোস্ট করতে চান?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CHOOSE_CHAT


async def choose_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.message.reply_text("বাতিল করা হয়েছে।")
        return ConversationHandler.END

    chat_id = int(query.data.split(":", 1)[1])
    context.user_data["target_chat_id"] = chat_id
    await query.message.reply_text(
        "✏️ পোলের প্রশ্ন / টাইটেল লিখুন:",
    )
    return ASK_QUESTION


async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["question"] = update.message.text.strip()
    context.user_data["options"] = []
    await update.message.reply_text(
        "এবার একটা একটা করে অপশনের নাম লিখে পাঠান (যেমন: PHP Prottay, Mehedi, ADI FF...)।\n"
        "নূন্যতম ২টা অপশন দিতে হবে। শেষ হলে নিচের বাটনে চাপুন।",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ শেষ করুন", callback_data="options_done")],
                [InlineKeyboardButton("❌ বাতিল", callback_data="cancel")],
            ]
        ),
    )
    return ASK_OPTIONS


async def add_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    options = context.user_data.setdefault("options", [])
    if len(options) >= MAX_OPTIONS:
        await update.message.reply_text(
            f"সর্বোচ্চ {MAX_OPTIONS}টি অপশন দেওয়া যাবে। এখন \"✅ শেষ করুন\" চাপুন।"
        )
        return ASK_OPTIONS

    text = update.message.text.strip()
    if text:
        options.append(text)

    await update.message.reply_text(
        f"যোগ হয়েছে: {text}\n\nমোট অপশন: {len(options)}\n\n"
        "আরও অপশন লিখুন, অথবা শেষ হলে নিচের বাটনে চাপুন।",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ শেষ করুন", callback_data="options_done")],
                [InlineKeyboardButton("❌ বাতিল", callback_data="cancel")],
            ]
        ),
    )
    return ASK_OPTIONS


async def finish_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.message.reply_text("বাতিল করা হয়েছে।")
        context.user_data.clear()
        return ConversationHandler.END

    options = context.user_data.get("options", [])
    if len(options) < MIN_OPTIONS:
        await query.message.reply_text(
            f"নূন্যতম {MIN_OPTIONS}টি অপশন লাগবে। আরও অপশন লিখুন।"
        )
        return ASK_OPTIONS

    chat_id = context.user_data["target_chat_id"]
    question = context.user_data["question"]
    creator_id = update.effective_user.id

    poll_id = db.create_poll(chat_id, question, creator_id, options)
    option_rows = db.get_options(poll_id)  # [(option_id, text), ...]
    counts = db.get_vote_counts(poll_id)

    text = build_poll_text(question, 0)
    markup = build_poll_keyboard(poll_id, option_rows, counts)

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
        db.set_poll_message(poll_id, sent.message_id)
        await query.message.reply_text("✅ পোল সফলভাবে পোস্ট হয়েছে!")
    except Exception as e:
        logger.exception("poll পোস্ট করতে ব্যর্থ")
        await query.message.reply_text(
            f"❌ পোল পোস্ট করা যায়নি: {e}\n\nবট ওই চ্যাটে মেসেজ পাঠানোর পারমিশনসহ "
            "এডমিন কিনা যাচাই করুন।"
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("বাতিল করা হয়েছে।")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ভোট দেওয়া ও পোল বন্ধ করা (callback_query — যেকোনো চ্যাটে কাজ করে)
# ---------------------------------------------------------------------------

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, poll_id_str, option_id_str = query.data.split(":")
    poll_id, option_id = int(poll_id_str), int(option_id_str)

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    if status != "open":
        await query.answer("এই পোলটি ইতিমধ্যে বন্ধ হয়ে গেছে।", show_alert=True)
        return

    result = db.try_claim_option(poll_id, update.effective_user.id, option_id)

    if result == "already_voted":
        await query.answer(
            "আপনি ইতিমধ্যে ভোট দিয়েছেন। একটা পোলে একবারই সুযোগ পাবেন।", show_alert=True
        )
        return

    await query.answer("✅ আপনার ভোট গৃহীত হয়েছে")

    option_rows = db.get_options(poll_id)
    counts = db.get_vote_counts(poll_id)
    total = sum(counts.values())

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=build_poll_text(question, total),
            reply_markup=build_poll_keyboard(poll_id, option_rows, counts),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        # message not modified হলে telegram error দেয়, সেটা উপেক্ষা করা নিরাপদ
        if "not modified" not in str(e).lower():
            logger.warning("poll message update failed: %s", e)

    # গ্রুপের পোল মেসেজ সবার জন্য একই আকাশি রঙেই থাকবে (Telegram একই মেসেজে
    # ভিন্ন ভিন্ন ইউজারকে ভিন্ন বাটন-রঙ দেখানো সাপোর্ট করে না)। তাই ভোটার নিজে
    # কোনটায় ভোট দিয়েছে সেটা রঙসহ দেখতে চাইলে তাকে আলাদাভাবে DM করা হচ্ছে।
    try:
        personal_markup = build_personal_poll_keyboard(option_rows, counts, option_id)
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=(
                f"✅ আপনি \"{question}\" পোলে ভোট দিয়েছেন।\n\n"
                "নিচে আপনার পছন্দটা সবুজ রঙে হাইলাইট করা আছে — এই রঙ শুধু আপনিই দেখছেন, "
                "গ্রুপে সবার কাছে বাটনগুলো আকাশি-ই দেখাবে।"
            ),
            reply_markup=personal_markup,
        )
    except Exception as e:
        # ইউজার বটকে প্রাইভেটে /start করেনি বলে DM যায়নি হয়ত — এটা এড়িয়ে যাওয়া নিরাপদ,
        # কারণ query.answer() দিয়ে ইতিমধ্যে একটা নিশ্চিতকরণ পপ-আপ পেয়ে গেছে
        logger.info("personal vote DM failed for user %s: %s", update.effective_user.id, e)


async def handle_myvote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    'আমার ভোট দেখুন' বাটন — শুধু যে চাপবে, শুধু তাকেই একটা প্রাইভেট পপ-আপে
    জানানো হয় সে কোন অপশনে ভোট দিয়েছে। Telegram-এর alert পপ-আপে কোনো রঙ/স্টাইল
    দেখানো যায় না (শুধু লেখা), তাই আসল রঙসহ ভোট দেখতে DM মেসেজটা কাজে লাগবে।
    """
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])

    voted_option_id = db.get_user_vote(poll_id, update.effective_user.id)
    if voted_option_id is None:
        await query.answer("আপনি এখনো এই পোলে ভোট দেননি।", show_alert=True)
        return

    option_rows = db.get_options(poll_id)
    option_text = next(
        (text for oid, text in option_rows if oid == voted_option_id), "?"
    )
    await query.answer(f"আপনি ভোট দিয়েছেন: {option_text}", show_alert=True)


async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ব্যক্তিগত (DM) স্ন্যাপশটের বাটনগুলো শুধু দেখানোর জন্য, চাপলে কিছু হবে না
    await update.callback_query.answer()


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    user_id = update.effective_user.id
    is_admin = await user_is_chat_admin(context, chat_id, user_id)
    if not (is_admin or user_id == creator_id):
        await query.answer(
            "শুধুমাত্র এই চ্যানেল/গ্রুপের এডমিন পোল বন্ধ করতে পারবেন।", show_alert=True
        )
        return

    if status != "open":
        await query.answer("পোলটি আগেই বন্ধ হয়ে গেছে।")
        return

    db.close_poll(poll_id)
    option_rows = db.get_options(poll_id)
    counts = db.get_vote_counts(poll_id)

    await query.answer("🔒 পোল বন্ধ করা হয়েছে")
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=build_result_text(question, option_rows, counts),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("poll close-edit failed: %s", e)


# ---------------------------------------------------------------------------
# Health-check সার্ভার — শুধুমাত্র Render/Railway-এর মতো হোস্টের জন্য।
# এই বট আসলে HTTP সার্ভ করে না (Telegram-এর সাথে long-polling করে), কিন্তু
# Render-এর ফ্রি "Web Service" টাইপ কিছু একটা পোর্টে সাড়া না পেলে ডিপ্লয়মেন্ট
# ব্যর্থ ধরে নেয়। তাই একটা আলাদা থ্রেডে সামান্য "OK" রেসপন্স দেওয়ার সার্ভার
# চালানো হচ্ছে — এটা বটের মূল লজিকের সাথে সম্পর্কহীন।
# ---------------------------------------------------------------------------

class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Poll bot is running \u2705".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # health-check হিটে টার্মিনাল স্প্যাম বন্ধ রাখার জন্য


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("health-check সার্ভার চালু হয়েছে পোর্ট %s এ", port)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN পাওয়া যায়নি। .env ফাইলে BOT_TOKEN=xxxx বসিয়ে আবার চালান।"
        )

    start_health_server()
    db.init_db(DB_PATH)

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("newpoll", newpoll_entry),
            MessageHandler(_menu_filter(MENU_NEWPOLL_TEXT), newpoll_entry),
        ],
        states={
            CHOOSE_CHAT: [CallbackQueryHandler(choose_chat)],
            ASK_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_question)],
            ASK_OPTIONS: [
                CallbackQueryHandler(finish_options, pattern="^(options_done|cancel)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_option),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("mychats", mychats_cmd))
    application.add_handler(conv_handler)
    # নিচের মেনু-বাটনের টেক্সট মেসেজগুলো ধরার হ্যান্ডলার (conv_handler এর পরে, যাতে
    # /newpoll চলাকালীন প্রশ্ন/অপশন টাইপ করার সময় এগুলো বাধা না দেয়)
    application.add_handler(MessageHandler(_menu_filter(MENU_MYCHATS_TEXT), mychats_cmd))
    application.add_handler(MessageHandler(_menu_filter(MENU_HELP_TEXT), help_cmd))
    application.add_handler(CallbackQueryHandler(handle_vote, pattern="^vote:"))
    application.add_handler(CallbackQueryHandler(handle_myvote, pattern="^myvote:"))
    application.add_handler(CallbackQueryHandler(handle_noop, pattern="^noop$"))
    application.add_handler(CallbackQueryHandler(handle_stop, pattern="^stop:"))
    application.add_handler(
        ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    logger.info("বট চালু হচ্ছে...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
