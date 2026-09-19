"""
bot.py — কাস্টম পোল টেলিগ্রাম বট
----------------------------------
বাটন দিয়ে যত খুশি কাস্টম পোল বানানো যায় (নিজের অপশনের নাম দিয়ে),
আনলিমিটেড চ্যানেল/গ্রুপে (বট যেখানে এডমিন) পোস্ট করা যায়,
এবং শুধু সেই চ্যানেল/গ্রুপের এডমিনরাই পোল বন্ধ করতে পারবে (কনফার্মেশনসহ)
এবং পোল এডিট করতে পারবে (নতুন অপশন/প্রি-সেট ভোট যোগ, কাউকে ভোট থেকে বাদ দেওয়া)।

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

# Conversation states (পোল তৈরির জন্য)
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
                "✏️ পোল এডিট করুন (এডমিন)",
                callback_data=f"editmenu:{poll_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🔴 থামান এবং ফলাফল পান",
                callback_data=f"stopask:{poll_id}",
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


NOT_MEMBER_STATUSES = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)


async def user_is_chat_member(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """
    ভোট দেওয়ার আগে চেক করে ইউজার আসলেই সেই চ্যানেল/গ্রুপে জয়েন করা আছে কিনা।
    Returns: True (জয়েন করা আছে) / False (জয়েন করা নেই বা বের হয়ে গেছে) / None (যাচাই করা যায়নি)
    """
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        logger.warning("membership check failed for chat %s user %s: %s", chat_id, user_id, e)
        return None
    return member.status not in NOT_MEMBER_STATUSES


async def refresh_poll_message(context: ContextTypes.DEFAULT_TYPE, poll_id: int):
    """
    পোলের আসল মেসেজটা (গ্রুপ/চ্যানেলে থাকা) সবশেষ অপশন+কাউন্ট দিয়ে আপডেট করে।
    নতুন অপশন যোগ করা, কাউকে ভোট থেকে বাদ দেওয়া — দুই ক্ষেত্রেই এটা কল হয়।
    """
    poll = db.get_poll(poll_id)
    if not poll:
        return
    _, chat_id, message_id, question, creator_id, status = poll
    if status != "open" or not message_id:
        return
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
        if "not modified" not in str(e).lower():
            logger.warning("poll message refresh failed: %s", e)


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
# chat_member — সাধারণ ইউজারদের জয়েন/লিভ ট্র্যাক করে (my_chat_member থেকে আলাদা,
# ওটা শুধু বটের নিজের স্ট্যাটাস বদলালে ট্রিগার হয়)। কেউ চ্যানেল/গ্রুপ ছেড়ে গেলে
# সেই চ্যাটের সব 'open' পোল থেকে তার ভোট অটোমেটিক মুছে ফেলে ও লাইভ কাউন্ট আপডেট করে।
# ---------------------------------------------------------------------------

async def track_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result is None:
        return

    user = result.new_chat_member.user
    if user.is_bot:
        return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    was_member = old_status not in NOT_MEMBER_STATUSES
    now_left = new_status in NOT_MEMBER_STATUSES
    if not (was_member and now_left):
        return  # শুধু 'জয়েন করা ছিল -> এখন বের হয়ে গেছে/ব্যান হয়েছে' কেসেই কাজ করবে

    chat_id = result.chat.id
    for poll_id, message_id, question in db.list_open_polls_for_chat(chat_id):
        if not db.delete_vote(poll_id, user.id):
            continue  # এই পোলে সে ভোটই দেয়নি

        logger.info(
            "user %s left chat %s -> vote removed from poll %s", user.id, chat_id, poll_id
        )
        await refresh_poll_message(context, poll_id)


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
    "(একটা কনফার্মেশনের পর) পোল বন্ধ করতে পারবে ও ফলাফল দেখতে পারবে।\n"
    "৭. শুধু <b>এডমিনরাই</b> \"✏️ পোল এডিট করুন\" বাটন থেকে —\n"
    "   • নতুন অপশন যোগ করতে পারবেন। শুধু নাম লিখলে ০ ভোট দিয়ে শুরু হবে, আর "
    "<code>নাম,সংখ্যা</code> এভাবে লিখলে (যেমন <code>Mehedi,10</code>) সেই অপশন ওই "
    "সংখ্যা ভোট নিয়ে শুরু হবে এবং তারপর থেকে আসল ভোট যোগ হতে থাকবে।\n"
    "   • কাউকে ভোট থেকে বাদ দিতে (তার ভোট বাতিল করতে) পারবেন।\n\n"
    "🛡 <b>ফেক ভোট প্রতিরোধ:</b>\n"
    "• যে চ্যানেল/গ্রুপে পোল পোস্ট হয়েছে, সেখানে জয়েন করা না থাকলে ভোট দেওয়া যাবে না।\n"
    "• ভোট দেওয়ার পর কেউ চ্যানেল/গ্রুপ ছেড়ে গেলে তার ভোট অটোমেটিক মুছে যাবে ও লাইভ "
    "কাউন্ট আপডেট হবে।\n"
    "• প্রতি ইউজার প্রতি পোলে একবারই ভোট দিতে পারবে, বট অ্যাকাউন্ট ভোট দিতে পারবে না।\n"
    "• (একজন মানুষের একাধিক টেলিগ্রাম অ্যাকাউন্ট বা একাধিক ডিভাইস ব্যবহার করা টেলিগ্রামের "
    "নিয়মেই ঠেকানো সম্ভব নয় — এটা এই প্রযুক্তির সীমাবদ্ধতা।)\n\n"
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


async def cancel_cmd_generic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancel — যখন কোনো ConversationHandler-এর ভেতরে না থেকেও (যেমন 'নতুন অপশন
    যোগ করুন' এর টেক্সট আসার অপেক্ষায় থাকা অবস্থায়) কেউ বাতিল করতে চায়।
    """
    if context.user_data.pop("awaiting", None) is not None:
        await update.message.reply_text("বাতিল করা হয়েছে।")
    # ConversationHandler-এর ভেতরে থাকলে সেটার নিজের /cancel fallback আগেই ধরে ফেলবে,
    # তাই এখানে আর কিছু বলার দরকার নেই যদি awaiting-ও না থাকে।


# ---------------------------------------------------------------------------
# ভোট দেওয়া (callback_query — যেকোনো চ্যাটে কাজ করে)
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

    voter = update.effective_user

    # ---- ফেক ভোট ঠেকানোর চেক ১: বট অ্যাকাউন্ট দিয়ে ভোট দেওয়া বন্ধ ----
    if voter.is_bot:
        await query.answer("বট অ্যাকাউন্ট দিয়ে ভোট দেওয়া যাবে না।", show_alert=True)
        return

    # ---- ফেক ভোট ঠেকানোর চেক ২: চ্যানেল/গ্রুপে জয়েন করা আছে কিনা যাচাই ----
    is_member = await user_is_chat_member(context, chat_id, voter.id)
    if is_member is None:
        await query.answer(
            "মেম্বারশিপ যাচাই করা যাচ্ছে না, একটু পর আবার চেষ্টা করুন।", show_alert=True
        )
        return
    if is_member is False:
        await query.answer(
            "❗ আপনি এখনো এই চ্যানেল/গ্রুপে জয়েন করেননি।\nনিয়ম অনুযায়ী ভোট দিতে হলে আগে জয়েন করতে হবে।",
            show_alert=True,
        )
        return

    voter_name = voter.full_name or (f"@{voter.username}" if voter.username else str(voter.id))
    result = db.try_claim_option(poll_id, voter.id, option_id, voter_name)

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


# ---------------------------------------------------------------------------
# পোল বন্ধ করা — এখন শুধুমাত্র চ্যাট-এডমিনরাই পারবে (আগে পোল-তৈরিকারীও পারতো,
# সেই বাগ ফিক্স করা হয়েছে), এবং বন্ধ করার আগে একটা কনফার্মেশন ধাপ থাকবে।
# ---------------------------------------------------------------------------

async def handle_stopask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    user_id = update.effective_user.id
    is_admin = await user_is_chat_admin(context, chat_id, user_id)
    if not is_admin:
        await query.answer(
            "শুধুমাত্র এই চ্যানেল/গ্রুপের এডমিনরাই পোল বন্ধ করতে পারবেন।", show_alert=True
        )
        return

    if status != "open":
        await query.answer("পোলটি আগেই বন্ধ হয়ে গেছে।")
        return

    await query.answer()
    await query.message.reply_text(
        f"⚠️ আপনি কি নিশ্চিত \"{question}\" পোলটি বন্ধ করে দিতে চান?\n"
        "বন্ধ করার পর আর কেউ ভোট দিতে পারবে না।",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ হ্যাঁ, বন্ধ করুন", callback_data=f"stopyes:{poll_id}", style="danger"),
                    InlineKeyboardButton("❌ না, থাক", callback_data="stopno"),
                ]
            ]
        ),
    )


async def handle_stopyes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    user_id = update.effective_user.id
    is_admin = await user_is_chat_admin(context, chat_id, user_id)
    if not is_admin:
        await query.answer(
            "শুধুমাত্র এই চ্যানেল/গ্রুপের এডমিনরাই পোল বন্ধ করতে পারবেন।", show_alert=True
        )
        return

    if status != "open":
        await query.answer("পোলটি আগেই বন্ধ হয়ে গেছে।")
    else:
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

    # কনফার্মেশনের মেসেজটা মুছে দেওয়া হচ্ছে যাতে চ্যাট পরিষ্কার থাকে
    try:
        await query.message.delete()
    except Exception:
        pass


async def handle_stopno(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("বাতিল করা হয়েছে")
    try:
        await query.message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# পোল এডিট করা (শুধু এডমিন) — নতুন অপশন যোগ (প্রি-সেট ভোট সহ) ও কাউকে ভোট
# থেকে বাদ দেওয়া
# ---------------------------------------------------------------------------

async def handle_editmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    is_admin = await user_is_chat_admin(context, chat_id, update.effective_user.id)
    if not is_admin:
        await query.answer(
            "শুধুমাত্র এই চ্যানেল/গ্রুপের এডমিনরাই পোল এডিট করতে পারবেন।", show_alert=True
        )
        return

    if status != "open":
        await query.answer("বন্ধ হয়ে যাওয়া পোল এডিট করা যাবে না।", show_alert=True)
        return

    await query.answer()
    await query.message.reply_text(
        f"✏️ \"{question}\" — এডিট মেনু। কী করতে চান?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ নতুন অপশন যোগ করুন", callback_data=f"addopt:{poll_id}")],
                [InlineKeyboardButton("🚫 কাউকে ভোট থেকে বাদ দিন", callback_data=f"kickmenu:{poll_id}")],
                [InlineKeyboardButton("❌ বন্ধ করুন", callback_data="editclose")],
            ]
        ),
    )


async def handle_addopt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    is_admin = await user_is_chat_admin(context, chat_id, update.effective_user.id)
    if not is_admin:
        await query.answer(
            "শুধুমাত্র এই চ্যানেল/গ্রুপের এডমিনরাই এই কাজ করতে পারবেন।", show_alert=True
        )
        return
    if status != "open":
        await query.answer("বন্ধ হয়ে যাওয়া পোল এডিট করা যাবে না।", show_alert=True)
        return

    context.user_data["awaiting"] = {"action": "add_option", "poll_id": poll_id}
    await query.answer()
    await query.message.reply_text(
        "নতুন অপশনের নাম লিখে পাঠান।\n\n"
        "শুধু নাম লিখলে সেটা ০ ভোট নিয়ে শুরু হবে।\n"
        "আগে থেকেই একটা ভোট সংখ্যা বসিয়ে দিতে চাইলে এভাবে লিখুন:\n"
        "<code>নাম,সংখ্যা</code>  (যেমন: <code>Mehedi,10</code>)\n"
        "এতে অপশনটা ১০ ভোট নিয়ে শুরু হবে এবং তারপর থেকে আসল ভোট এর সাথে যোগ হতে থাকবে।\n\n"
        "বাতিল করতে /cancel লিখুন।",
        parse_mode=ParseMode.HTML,
    )


async def handle_awaiting_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    'নতুন অপশন যোগ করুন' চাপার পর এডমিনের পরের টেক্সট মেসেজ এখানে ধরা হয়।
    ConversationHandler ব্যবহার না করে user_data-তে সাধারণ একটা "awaiting"
    ফ্ল্যাগ দিয়ে করা, যাতে গ্রুপ চ্যাটেই সরাসরি লেখা যায়।
    """
    awaiting = context.user_data.get("awaiting")
    if not awaiting or awaiting.get("action") != "add_option":
        return  # অন্য কোনো সাধারণ মেসেজ, এখানে কিছু করার নেই

    poll_id = awaiting["poll_id"]
    poll = db.get_poll(poll_id)
    if not poll or poll[5] != "open":
        context.user_data.pop("awaiting", None)
        await update.message.reply_text("এই পোলটি আর খোলা নেই, অপশন যোগ করা গেল না।")
        return

    raw_text = update.message.text.strip()
    if "," in raw_text:
        name_part, count_part = raw_text.rsplit(",", 1)
        name_part = name_part.strip()
        count_part = count_part.strip()
        if not count_part.isdigit():
            await update.message.reply_text(
                "❌ সংখ্যাটা বুঝা যায়নি। এভাবে লিখুন: নাম,সংখ্যা (যেমন Mehedi,10), "
                "অথবা শুধু নাম লিখুন। আবার চেষ্টা করুন, বা /cancel দিন।"
            )
            return
        manual_votes = int(count_part)
        option_name = name_part
    else:
        option_name = raw_text
        manual_votes = 0

    if not option_name:
        await update.message.reply_text("অপশনের নাম খালি রাখা যাবে না। আবার লিখুন, বা /cancel দিন।")
        return

    db.add_option(poll_id, option_name, manual_votes)
    context.user_data.pop("awaiting", None)

    if manual_votes:
        await update.message.reply_text(
            f"✅ নতুন অপশন \"{option_name}\" যোগ হয়েছে, শুরুতে {manual_votes} ভোট নিয়ে।"
        )
    else:
        await update.message.reply_text(f"✅ নতুন অপশন \"{option_name}\" যোগ হয়েছে।")

    await refresh_poll_message(context, poll_id)


async def handle_kickmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    is_admin = await user_is_chat_admin(context, chat_id, update.effective_user.id)
    if not is_admin:
        await query.answer(
            "শুধুমাত্র এই চ্যানেল/গ্রুপের এডমিনরাই এই কাজ করতে পারবেন।", show_alert=True
        )
        return

    voters = db.list_voters(poll_id)
    await query.answer()
    if not voters:
        await query.message.reply_text("এখনো কেউ (আসল ভোট দিয়ে) ভোট দেয়নি।")
        return

    rows = []
    for user_id, voter_name, option_id, option_text in voters:
        display = voter_name or str(user_id)
        rows.append(
            [
                InlineKeyboardButton(
                    f"❌ {display} — {option_text}",
                    callback_data=f"kick:{poll_id}:{user_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🔙 বাতিল", callback_data="editclose")])
    await query.message.reply_text(
        "কাকে ভোট থেকে বাদ দিতে চান? (তার ভোট বাতিল হয়ে যাবে)",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, poll_id_str, target_user_id_str = query.data.split(":")
    poll_id, target_user_id = int(poll_id_str), int(target_user_id_str)

    poll = db.get_poll(poll_id)
    if not poll:
        await query.answer("এই পোলটি পাওয়া যায়নি।", show_alert=True)
        return
    _, chat_id, message_id, question, creator_id, status = poll

    is_admin = await user_is_chat_admin(context, chat_id, update.effective_user.id)
    if not is_admin:
        await query.answer(
            "শুধুমাত্র এই চ্যানেল/গ্রুপের এডমিনরাই এই কাজ করতে পারবেন।", show_alert=True
        )
        return

    removed = db.delete_vote(poll_id, target_user_id)
    if removed:
        await query.answer("✅ ভোট বাতিল করা হয়েছে")
        await refresh_poll_message(context, poll_id)
        try:
            await query.message.edit_text("✅ ওই ইউজারের ভোট বাদ দেওয়া হয়েছে।")
        except Exception:
            pass
    else:
        await query.answer("এই ইউজারের ভোট আর খুঁজে পাওয়া যায়নি (হয়তো আগেই বাদ পড়েছে)।", show_alert=True)


async def handle_editclose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Health-check সার্ভার — শুধুমাত্র Render/Railway-এর মতো হোস্টের জন্য।
# এই বট আসলে HTTP সার্ভ করে না (Telegram-এর সাথে long-polling করে), কিন্তু
# Render-এর ফ্রি "Web Service" টাইপ কিছু একটা পোর্টে সাড়া না পেলে ডিপ্লয়মেন্ট
# ব্যর্থ ধরে নেয়। তাই একটা আলাদা থ্রেডে সামান্য "OK" রেসপন্স দেওয়ার সার্ভার
# চালানো হচ্ছে — এটা বটের মূল লজিকের সাথে সম্পর্কহীন।
#
# ⚠️ মনে রাখার ব্যাপারে: পোলের সব তথ্য (অপশন, ম্যানুয়াল ভোট, কে কোথায় ভোট
# দিয়েছে) pollbot.db নামের SQLite ফাইলে সেভ হয় (DB_PATH এনভায়রনমেন্ট ভ্যারিয়েবল
# দিয়ে পাল্টানো যায়)। Render/Railway এর ফ্রি প্ল্যানে এই ফাইলটা persistent disk
# ছাড়া প্রতি রিডিপ্লয়ে মুছে যেতে পারে — বট যেন সবকিছু স্থায়ীভাবে মনে রাখে,
# সেজন্য হোস্টে একটা persistent disk/volume মাউন্ট করে DB_PATH সেই পাথে সেট
# করে দেওয়া দরকার।
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

    # পোল এডিট ফিচারের জন্য "পরের টেক্সট মেসেজের অপেক্ষা" হ্যান্ডলার এবং /cancel —
    # conv_handler ও উপরের মেনু-হ্যান্ডলারগুলোর পরে, যাতে ওগুলোর সাথে সংঘর্ষ না হয়
    application.add_handler(CommandHandler("cancel", cancel_cmd_generic))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_awaiting_text))

    application.add_handler(CallbackQueryHandler(handle_vote, pattern="^vote:"))
    application.add_handler(CallbackQueryHandler(handle_myvote, pattern="^myvote:"))
    application.add_handler(CallbackQueryHandler(handle_noop, pattern="^noop$"))
    application.add_handler(CallbackQueryHandler(handle_stopask, pattern="^stopask:"))
    application.add_handler(CallbackQueryHandler(handle_stopyes, pattern="^stopyes:"))
    application.add_handler(CallbackQueryHandler(handle_stopno, pattern="^stopno$"))
    application.add_handler(CallbackQueryHandler(handle_editmenu, pattern="^editmenu:"))
    application.add_handler(CallbackQueryHandler(handle_addopt, pattern="^addopt:"))
    application.add_handler(CallbackQueryHandler(handle_kickmenu, pattern="^kickmenu:"))
    application.add_handler(CallbackQueryHandler(handle_kick, pattern="^kick:"))
    application.add_handler(CallbackQueryHandler(handle_editclose, pattern="^editclose$"))
    application.add_handler(
        ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(
        ChatMemberHandler(track_chat_members, ChatMemberHandler.CHAT_MEMBER)
    )

    logger.info("বট চালু হচ্ছে...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
