"""
database.py
SQLite persistence layer for the Telegram Poll Bot.
Kept as plain sqlite3 (sync) with a global lock — traffic level of a
poll bot does not need async DB drivers, and this keeps deployment simple
(single file, no external DB service required).
"""

import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = "pollbot.db"
_lock = threading.Lock()


def init_db(path: str = DB_PATH):
    global DB_PATH
    DB_PATH = path
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id     INTEGER PRIMARY KEY,
                title       TEXT NOT NULL,
                chat_type   TEXT NOT NULL,
                bot_status  TEXT NOT NULL,           -- 'administrator' | 'member' | 'left' | 'kicked'
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS polls (
                poll_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                message_id  INTEGER,
                question    TEXT NOT NULL,
                creator_id  INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS poll_options (
                option_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id       INTEGER NOT NULL REFERENCES polls(poll_id) ON DELETE CASCADE,
                option_text   TEXT NOT NULL,
                position      INTEGER NOT NULL,
                manual_votes  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS votes (
                poll_id     INTEGER NOT NULL REFERENCES polls(poll_id) ON DELETE CASCADE,
                user_id     INTEGER NOT NULL,
                option_id   INTEGER NOT NULL REFERENCES poll_options(option_id) ON DELETE CASCADE,
                voter_name  TEXT,
                voted_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (poll_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_options_poll ON poll_options(poll_id);
            CREATE INDEX IF NOT EXISTS idx_votes_poll ON votes(poll_id);
            """
        )
        # --- migration guard: আগের ভার্সনে বানানো পুরনো DB ফাইলে নতুন কলাম
        # যোগ করে দেয় (নতুন feature গুলোর জন্য দরকার), না থাকলে চুপচাপ এগিয়ে যায়।
        for alter_sql in (
            "ALTER TABLE poll_options ADD COLUMN manual_votes INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE votes ADD COLUMN voter_name TEXT",
        ):
            try:
                conn.execute(alter_sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # কলাম আগে থেকেই আছে


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- chats ----------

def upsert_chat(chat_id: int, title: str, chat_type: str, bot_status: str):
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO chats (chat_id, title, chat_type, bot_status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                chat_type=excluded.chat_type,
                bot_status=excluded.bot_status,
                updated_at=CURRENT_TIMESTAMP
            """,
            (chat_id, title, chat_type, bot_status),
        )


def list_active_chats():
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, title, chat_type FROM chats "
            "WHERE bot_status IN ('administrator', 'member') ORDER BY title"
        ).fetchall()
    return rows


def get_chat(chat_id: int):
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT chat_id, title, chat_type, bot_status FROM chats WHERE chat_id=?",
            (chat_id,),
        ).fetchone()


# ---------- polls ----------

def create_poll(chat_id: int, question: str, creator_id: int, options: list[str]) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO polls (chat_id, question, creator_id) VALUES (?, ?, ?)",
            (chat_id, question, creator_id),
        )
        poll_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO poll_options (poll_id, option_text, position) VALUES (?, ?, ?)",
            [(poll_id, opt, i) for i, opt in enumerate(options)],
        )
    return poll_id


def set_poll_message(poll_id: int, message_id: int):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE polls SET message_id=? WHERE poll_id=?", (message_id, poll_id)
        )


def get_poll(poll_id: int):
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT poll_id, chat_id, message_id, question, creator_id, status "
            "FROM polls WHERE poll_id=?",
            (poll_id,),
        ).fetchone()


def get_options(poll_id: int):
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT option_id, option_text FROM poll_options "
            "WHERE poll_id=? ORDER BY position",
            (poll_id,),
        ).fetchall()


def add_option(poll_id: int, option_text: str, manual_votes: int = 0) -> int:
    """
    এডমিন পোল এডিট করার সময় নতুন অপশন যোগ করে। manual_votes দিয়ে অপশনটা
    শুরুতেই একটা প্রি-সেট ভোট সংখ্যা নিয়ে শুরু করতে পারে (যেমন "নাম,10"
    দিলে সেই অপশন ১০ ভোট নিয়ে শুরু হবে এবং তারপর থেকে আসল ভোট যোগ হতে থাকবে)।
    """
    with _lock, _connect() as conn:
        next_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM poll_options WHERE poll_id=?",
            (poll_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO poll_options (poll_id, option_text, position, manual_votes) "
            "VALUES (?, ?, ?, ?)",
            (poll_id, option_text, next_pos, manual_votes),
        )
        return cur.lastrowid


def try_claim_option(poll_id: int, user_id: int, option_id: int, voter_name: str = None) -> str:
    """
    একজন ইউজার একটা পোলে জীবনে একবারই ভোট দিতে পারবে (যেকোনো একটা অপশনে),
    কিন্তু একটা অপশনে একাধিক ভিন্ন ইউজার ভোট দিতে পারবে — এটা এখন সবার জন্য
    খোলা অপশন, শুধু একজন ইউজার দুইবার/দুইটা অপশনে ভোট দিতে পারবে না।
    Returns: "ok" | "already_voted"
    """
    with _lock, _connect() as conn:
        already = conn.execute(
            "SELECT 1 FROM votes WHERE poll_id=? AND user_id=?", (poll_id, user_id)
        ).fetchone()
        if already:
            return "already_voted"

        try:
            conn.execute(
                "INSERT INTO votes (poll_id, user_id, option_id, voter_name) VALUES (?, ?, ?, ?)",
                (poll_id, user_id, option_id, voter_name),
            )
        except sqlite3.IntegrityError:
            # দুইজন ঠিক একই মুহূর্তে চাপলে race condition — একজন ইউজার একবারই ভোট দিতে পারবে
            return "already_voted"
    return "ok"


def get_vote_counts(poll_id: int) -> dict:
    """
    প্রতিটা অপশনের মোট ভোট = আসল ভোট (votes টেবিল থেকে) + manual_votes
    (এডমিন এডিট করার সময় প্রি-সেট করা সংখ্যা)।
    """
    with _lock, _connect() as conn:
        manual_rows = conn.execute(
            "SELECT option_id, manual_votes FROM poll_options WHERE poll_id=?",
            (poll_id,),
        ).fetchall()
        real_rows = conn.execute(
            "SELECT option_id, COUNT(*) FROM votes WHERE poll_id=? GROUP BY option_id",
            (poll_id,),
        ).fetchall()
    counts = {option_id: manual for option_id, manual in manual_rows}
    for option_id, real_count in real_rows:
        counts[option_id] = counts.get(option_id, 0) + real_count
    return counts


def get_user_vote(poll_id: int, user_id: int):
    """একজন ইউজার এই পোলে কোন option_id-তে ভোট দিয়েছে, না দিলে None।"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT option_id FROM votes WHERE poll_id=? AND user_id=?",
            (poll_id, user_id),
        ).fetchone()
    return row[0] if row else None


def list_voters(poll_id: int):
    """
    এই পোলে এখন পর্যন্ত যারা (আসল ভোট দিয়ে) ভোট দিয়েছে তাদের তালিকা —
    "ভোট থেকে বাদ দিন" মেনুতে দেখানোর জন্য। manual_votes এখানে আসবে না,
    কারণ সেটা কোনো নির্দিষ্ট ইউজারের ভোট না।
    Returns: [(user_id, voter_name, option_id, option_text), ...]
    """
    with _lock, _connect() as conn:
        return conn.execute(
            """
            SELECT v.user_id, v.voter_name, v.option_id, o.option_text
            FROM votes v
            JOIN poll_options o ON o.option_id = v.option_id
            WHERE v.poll_id = ?
            ORDER BY v.voted_at
            """,
            (poll_id,),
        ).fetchall()


def close_poll(poll_id: int):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE polls SET status='closed' WHERE poll_id=?", (poll_id,)
        )


def list_open_polls_for_chat(chat_id: int):
    """
    কোনো ইউজার একটা চ্যাট থেকে বের হয়ে গেলে, সেই চ্যাটের যত 'open' পোল আছে
    সবগুলো থেকে তার ভোট মুছে ফেলতে হবে — এই ফাংশন সেই পোলগুলোর তালিকা দেয়।
    """
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT poll_id, message_id, question FROM polls "
            "WHERE chat_id=? AND status='open'",
            (chat_id,),
        ).fetchall()


def delete_vote(poll_id: int, user_id: int) -> bool:
    """
    একটা ভোট মুছে দেয় — ইউজার চ্যানেল/গ্রুপ ছেড়ে গেলে অটোমেটিক এই ফাংশন
    কল হয়, আবার এডমিন 'ভোট থেকে বাদ দিন' মেনু থেকে ম্যানুয়ালি কাউকে বাদ
    দিলেও এই একই ফাংশন ব্যবহার হয়। মুছা হলে True, না থাকলে False।
    """
    with _lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM votes WHERE poll_id=? AND user_id=?", (poll_id, user_id)
        )
    return cur.rowcount > 0
