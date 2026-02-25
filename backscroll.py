# public_backscroll_v5_1.py
# Discord bot with /backscroll and /backscroll_private (+ admin metrics scoped to 2 guilds)

import os
import io
import csv
import time
import sqlite3
import asyncio
import threading
from typing import List, Optional
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from openai import OpenAI

# ---- Render keepalive ----
from http.server import BaseHTTPRequestHandler, HTTPServer
class _Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def _keepalive():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _Ping)
    server.serve_forever()

threading.Thread(target=_keepalive, daemon=True).start()

# ----------------- Config -----------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not DISCORD_TOKEN or not OPENAI_API_KEY:
    raise SystemExit("❌ Missing DISCORD_TOKEN or OPENAI_API_KEY in environment or .env file.")

client = OpenAI(api_key=OPENAI_API_KEY)

MAX_BACKSCROLL = 500

# Change this whenever you want the "update notice" to show again in every server.
BOT_VERSION = "v5.1"

# Support server invite (already inside your code)
SUPPORT_LINK = "https://discord.gg/kKSeZU37dy"

ADMIN_ID = 710963340360417300

# Rate & quota controls
COOLDOWN_SECONDS = 60               # per-user cooldown
MAX_DAILY_PER_GUILD = 30            # per-guild 24h cap across both commands (kept)
MAX_DAILY_PER_USER = 10             # NEW: per-user per-day cap across both commands

# Concurrency protection to reduce request spikes (helps avoid global rate limits)
MAX_CONCURRENT_SUMMARIES_GLOBAL = 3  # keep small
_global_summary_sem = asyncio.Semaphore(MAX_CONCURRENT_SUMMARIES_GLOBAL)
_guild_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Timezone for "per day"
LOCAL_TZ = ZoneInfo("America/New_York")

# Privileged users (unlimited)
try:
    from privileged_users import PRIVILEGED_USER_IDS
except Exception:
    PRIVILEGED_USER_IDS = set()

def is_privileged(user_id: int) -> bool:
    try:
        return int(user_id) in set(PRIVILEGED_USER_IDS)
    except Exception:
        return False

# Scope admin commands ONLY to these 2 guilds
CONTROL_GUILDS = [discord.Object(id=782572577260175420), discord.Object(id=912451366839013396)]

# ----------------- Discord Setup -----------------
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- Metrics (SQLite) -----------------
DB_PATH = "metrics.db"
_db_lock = threading.Lock()

with sqlite3.connect(DB_PATH) as _conn:
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT,
            guild_name TEXT,
            command_name TEXT,
            ts INTEGER
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_joins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT,
            guild_name TEXT,
            owner_id TEXT,
            joined_at INTEGER
        )
    """)

    # Lightweight migration: add who/where columns if missing
    for col in ["user_id TEXT", "user_name TEXT", "channel_id TEXT", "channel_name TEXT"]:
        try:
            _conn.execute(f"ALTER TABLE usage_events ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    _conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_events(ts)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_guild ON usage_events(guild_id)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events(user_id)")

    # Per-guild settings
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT PRIMARY KEY,
            language TEXT DEFAULT '',
            update_notice_version TEXT DEFAULT ''
        )
    """)
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_settings_guild ON guild_settings(guild_id)")

    # NEW: per-user daily usage
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS user_daily_usage (
            user_id TEXT NOT NULL,
            day_key TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day_key)
        )
    """)
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_user_daily_day ON user_daily_usage(day_key)")

# Human-readable usage log
PLAIN_LOG_PATH = "usage.txt"

def _now() -> int:
    return int(time.time())

def _day_key_now() -> str:
    # Example: "2026-02-25" in America/New_York
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

def _append_plain_log(line: str):
    try:
        with open(PLAIN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass

def log_usage_inter(inter: discord.Interaction, command_name: str):
    if inter.guild is None or inter.channel is None:
        return
    ts = _now()
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO usage_events
            (guild_id, guild_name, command_name, ts, user_id, user_name, channel_id, channel_name)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            str(inter.guild.id), inter.guild.name, command_name, ts,
            str(inter.user.id), inter.user.display_name,
            str(inter.channel.id), getattr(inter.channel, "name", "DM")
        ))
        conn.commit()
    _append_plain_log(
        f"[{ts}] {inter.guild.name} ({inter.guild_id}) "
        f"#{getattr(inter.channel,'name','DM')} | {command_name} by {inter.user.display_name} ({inter.user.id})"
    )

def log_guild_join(guild: discord.Guild):
    ts = _now()
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO guild_joins (guild_id,guild_name,owner_id,joined_at) VALUES (?,?,?,?)",
            (str(guild.id), guild.name, str(guild.owner_id), ts)
        )
        conn.commit()
    _append_plain_log(f"[{ts}] Joined guild: {guild.name} ({guild.id}) owner={guild.owner_id}")

def is_admin(inter: discord.Interaction) -> bool:
    return inter.user.id == ADMIN_ID

# ----------------- Settings helpers -----------------
def _ensure_guild_settings_row(guild_id: int):
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (str(guild_id),))
        conn.commit()

def get_guild_language(guild_id: int) -> str:
    _ensure_guild_settings_row(guild_id)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT language FROM guild_settings WHERE guild_id = ?",
            (str(guild_id),)
        ).fetchone()
    return (row[0] or "").strip()

def set_guild_language(guild_id: int, lang: str):
    _ensure_guild_settings_row(guild_id)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE guild_settings SET language = ? WHERE guild_id = ?",
            (lang.strip(), str(guild_id))
        )
        conn.commit()

def reset_guild_language(guild_id: int):
    set_guild_language(guild_id, "")

def _has_seen_update_notice(guild_id: int) -> bool:
    _ensure_guild_settings_row(guild_id)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT update_notice_version FROM guild_settings WHERE guild_id = ?",
            (str(guild_id),)
        ).fetchone()
    seen_ver = (row[0] or "").strip()
    return seen_ver == BOT_VERSION

def _mark_update_notice_seen(guild_id: int):
    _ensure_guild_settings_row(guild_id)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE guild_settings SET update_notice_version = ? WHERE guild_id = ?",
            (BOT_VERSION, str(guild_id))
        )
        conn.commit()

async def maybe_send_update_notice(inter: discord.Interaction):
    """
    Send a one-time per-server update message per BOT_VERSION.
    """
    if inter.guild is None:
        return
    if _has_seen_update_notice(inter.guild.id):
        return

    # Mark seen first to prevent double sends if multiple users call at the same time
    _mark_update_notice_seen(inter.guild.id)

    msg = (
        "**Backscroll Update**\n"
        "We’re back online. We had a short disruption due to Discord API global rate limits. Thanks for your patience.\n\n"
        "**New usage limits (effective now)**\n"
        f"To keep service reliable as we scale, Backscroll is now limited to **{MAX_DAILY_PER_USER} backscrolls per user per day**. "
        f"**Premium users** get unlimited usage. For updates and support: {SUPPORT_LINK}"
    )

    # Try to post publicly
    try:
        if inter.channel and hasattr(inter.channel, "send"):
            await inter.channel.send(msg)
            return
    except Exception:
        pass

    # Fallback: send to the user (ephemeral)
    try:
        if not inter.response.is_done():
            await inter.response.send_message(msg, ephemeral=True)
        else:
            await inter.followup.send(msg, ephemeral=True)
    except Exception:
        pass

# ----------------- Daily usage helpers -----------------
def _get_user_daily_used(user_id: int, day_key: str) -> int:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT used FROM user_daily_usage WHERE user_id = ? AND day_key = ?",
            (str(user_id), day_key)
        ).fetchone()
    return int(row[0]) if row else 0

def _inc_user_daily_used(user_id: int, day_key: str, delta: int = 1):
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_daily_usage (user_id, day_key, used) VALUES (?,?,0)",
            (str(user_id), day_key)
        )
        conn.execute(
            "UPDATE user_daily_usage SET used = used + ? WHERE user_id = ? AND day_key = ?",
            (int(delta), str(user_id), day_key)
        )
        conn.commit()

# ----------------- Abuse Controls -----------------
_user_last_used: defaultdict[int, float] = defaultdict(float)

def _cooldown_remaining(user_id: int) -> int:
    last = _user_last_used[user_id]
    rem = COOLDOWN_SECONDS - int(_now() - last)
    return rem if rem > 0 else 0

def _bump_cooldown(user_id: int):
    _user_last_used[user_id] = _now()

def _guild_usage_24h(guild_id: int) -> int:
    since = _now() - 86400
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        c1 = conn.execute("""
            SELECT COUNT(*) FROM usage_events
            WHERE guild_id = ? AND ts > ? AND command_name IN ('backscroll','backscroll_private')
        """, (str(guild_id), since)).fetchone()[0]
    return int(c1 or 0)

async def _preflight_checks(inter: discord.Interaction) -> Optional[str]:
    """Return an error message string if not allowed; otherwise None."""
    if inter.guild is None:
        return "❌ This command must be used in a server channel."

    rem = _cooldown_remaining(inter.user.id)
    if rem > 0:
        return f"⏳ Cooldown: please wait **{rem}s** before using this again."

    # Per-guild 24h cap (kept from your code)
    used_guild = _guild_usage_24h(inter.guild.id)
    if used_guild >= MAX_DAILY_PER_GUILD:
        return (
            f"🚫 This server reached its 24-hour limit of **{MAX_DAILY_PER_GUILD}** summaries. "
            f"Try again later or contact support: {SUPPORT_LINK}"
        )

    # NEW: per-user daily cap (unless privileged)
    if not is_privileged(inter.user.id):
        day_key = _day_key_now()
        used_user = _get_user_daily_used(inter.user.id, day_key)
        if used_user >= MAX_DAILY_PER_USER:
            return (
                f"🚫 Daily limit reached (**{MAX_DAILY_PER_USER}/day**). "
                f"Premium users get unlimited usage. Support: {SUPPORT_LINK}"
            )

    return None

# ----------------- Helpers -----------------
async def fetch_messages(channel: discord.TextChannel, limit: int) -> List[discord.Message]:
    out: List[discord.Message] = []
    async for m in channel.history(limit=limit, oldest_first=False):
        if m.author.bot:
            continue
        if not (m.content and m.content.strip()):
            continue
        out.append(m)
    out.sort(key=lambda m: m.created_at)
    return out

def format_messages(msgs: List[discord.Message]) -> str:
    lines = []
    for m in msgs:
        safe = m.content.replace("\n", " ").replace("\r", " ").strip()
        name = m.author.display_name
        lines.append(f"{name}: {safe}")
    return "\n".join(lines)

# Language normalization (simple MVP)
LANG_ALIASES = {
    "english": "English",
    "en": "English",
    "arabic": "Arabic",
    "ar": "Arabic",
    "العربية": "Arabic",
    "عربي": "Arabic",
    "russian": "Russian",
    "ru": "Russian",
    "русский": "Russian",
    "spanish": "Spanish",
    "es": "Spanish",
    "español": "Spanish",
    "french": "French",
    "fr": "French",
    "français": "French",
    "german": "German",
    "de": "German",
    "deutsch": "German",
    "turkish": "Turkish",
    "tr": "Turkish",
    "türkçe": "Turkish",
}

def normalize_language(inp: str) -> str:
    if not inp:
        return ""
    key = inp.strip().lower()
    return LANG_ALIASES.get(key, inp.strip().title())

async def summarize_with_ai(formatted_msgs: str, include_topics: bool, language: str) -> str:
    lang = (language or "").strip() or "English"

    core_rules = f"""
Language:
- Write natively in {lang}.
- Think and summarize directly in {lang}.
- Do NOT translate from English.
- Use natural phrasing as a fluent speaker would.

Summarize the Discord messages for a friend who missed the chat.

Style:
- Plain, direct, casual. Lay it out; let the reader infer.
- Use simple verbs: said, talked about, asked.
- Do not use meta phrases like "shared opinions," "discussed their thoughts," "talked about various," or "sent short messages."
- Use simple, everyday verbs
- Focus on what was said, not how it was said.
- Do not describe message length, order, or structure (e.g., "sent short messages," "the chat started with," "later shifted").
- Avoid academic/formal wording and abstract nouns.
- Do NOT use timeline/meta phrasing: “started with”, “then”, “shifted”, “throughout”, “ended with”, “shared a mix”, “the conversation revolved around”, “delved into”, “analyzed”.
- No speculation about feelings or intentions unless explicitly stated.
- Do not describe the tone of the conversation (no "joked," "humorous," "playful," "lighthearted," "banter," "teasing," "funny," etc.).
- NEVER describe the tone of the conversation!
- Do not label any part of the chat as jokes, humor, or banter. Only state what was said.
- Focus only on the content and topics of the messages, not the mood or style.
- Summarize all main themes that appeared in the chat, even if they are sensitive, awkward, or uncomfortable.
- Do not default to only one subject. If multiple distinct conversations happened, mention each
- The purpose is coverage of content, not judgment or filtering.

Length & format:
- One short paragraph (2–6 simple sentences).
- Prefer shorter over longer, but if there is content to talk about make it longer.
"""

    if include_topics:
        core_rules += """

Then add a Topics list (2–6 items), each a few words. No emojis. Use exactly:

**Summary**
<paragraph here>

**Topics**
- <topic>
- <topic>
"""
    else:
        core_rules += """

Output only:

**Summary**
<paragraph here>
"""

    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that summarizes Discord chats."},
            {"role": "user", "content": core_rules},
            {"role": "user", "content": f"Messages:\n{formatted_msgs}"},
        ],
        temperature=0.3,
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()

# ----------------- Language commands -----------------
language_group = app_commands.Group(name="language", description="Set the bot language for this server.")

@language_group.command(name="set", description="Set the language for this server (example: arabic, russian).")
@app_commands.describe(language="Example: arabic, russian, spanish")
async def language_set(inter: discord.Interaction, language: str):
    if inter.guild is None:
        return await inter.response.send_message("❌ Use this in a server.", ephemeral=True)

    lang = normalize_language(language)
    set_guild_language(inter.guild.id, lang)
    await inter.response.send_message(
        f"✅ Server language set to **{lang}**.\nNext summaries will be written in that language.",
        ephemeral=True
    )

@language_group.command(name="current", description="Show the current language for this server.")
async def language_current(inter: discord.Interaction):
    if inter.guild is None:
        return await inter.response.send_message("❌ Use this in a server.", ephemeral=True)

    lang = get_guild_language(inter.guild.id)
    if not lang:
        return await inter.response.send_message("🌍 Server language is **default (English)**.", ephemeral=True)
    await inter.response.send_message(f"🌍 Server language is set to **{lang}**.", ephemeral=True)

@language_group.command(name="reset", description="Reset this server language back to default.")
async def language_reset(inter: discord.Interaction):
    if inter.guild is None:
        return await inter.response.send_message("❌ Use this in a server.", ephemeral=True)

    reset_guild_language(inter.guild.id)
    await inter.response.send_message("✅ Server language reset to **default (English)**.", ephemeral=True)

bot.tree.add_command(language_group)

# ----------------- Public Slash Commands -----------------
@bot.tree.command(name="backscroll", description="Summarize the last N messages in this channel.")
@app_commands.describe(count="How many messages to fetch (1–800)")
async def backscroll(inter: discord.Interaction, count: Optional[int] = 100):
    err = await _preflight_checks(inter)
    if err:
        return await inter.response.send_message(err, ephemeral=True)

    await maybe_send_update_notice(inter)

    _bump_cooldown(inter.user.id)
    await inter.response.defer(thinking=True)

    if not isinstance(inter.channel, discord.TextChannel):
        return await inter.followup.send("❌ This command can only be used in text channels.", ephemeral=True)

    requested = count or 100
    count = max(1, min(MAX_BACKSCROLL, requested))

    # Concurrency limits (global + per guild)
    guild_lock = _guild_locks[int(inter.guild.id)] if inter.guild else asyncio.Lock()

    async with _global_summary_sem:
        async with guild_lock:
            try:
                msgs = await fetch_messages(inter.channel, count)
                if not msgs:
                    return await inter.followup.send("No messages found.", ephemeral=True)

                formatted = format_messages(msgs)
                include_topics = requested > 100
                lang = get_guild_language(inter.guild.id) if inter.guild else ""
                summary = await summarize_with_ai(formatted, include_topics, lang)

                # Count usage (per-user daily) AFTER success
                if not is_privileged(inter.user.id):
                    _inc_user_daily_used(inter.user.id, _day_key_now(), 1)

                log_usage_inter(inter, "backscroll")
                await inter.followup.send(f"📜 **Summary of the last {requested} messages:**\n\n{summary}")
            except Exception:
                await inter.followup.send(f"❌ I couldn’t complete the summary. Need help? {SUPPORT_LINK}", ephemeral=True)

@bot.tree.command(name="backscroll_private", description="Summarize the last N messages and send privately.")
@app_commands.describe(count="How many messages to fetch (1–800)")
async def backscroll_private(inter: discord.Interaction, count: Optional[int] = 100):
    err = await _preflight_checks(inter)
    if err:
        return await inter.response.send_message(err, ephemeral=True)

    await maybe_send_update_notice(inter)

    _bump_cooldown(inter.user.id)
    await inter.response.defer(thinking=True, ephemeral=True)

    if not isinstance(inter.channel, discord.TextChannel):
        return await inter.followup.send("❌ This command can only be used in text channels.", ephemeral=True)

    requested = count or 100
    count = max(1, min(MAX_BACKSCROLL, requested))

    guild_lock = _guild_locks[int(inter.guild.id)] if inter.guild else asyncio.Lock()

    async with _global_summary_sem:
        async with guild_lock:
            try:
                msgs = await fetch_messages(inter.channel, count)
                if not msgs:
                    return await inter.followup.send("No messages found.", ephemeral=True)

                formatted = format_messages(msgs)
                include_topics = requested > 100
                lang = get_guild_language(inter.guild.id) if inter.guild else ""
                summary = await summarize_with_ai(formatted, include_topics, lang)

                if not is_privileged(inter.user.id):
                    _inc_user_daily_used(inter.user.id, _day_key_now(), 1)

                log_usage_inter(inter, "backscroll_private")
                try:
                    await inter.user.send(
                        f"📬 **Private summary of the last {requested} messages in #{inter.channel.name}:**\n\n{summary}"
                    )
                    await inter.followup.send("✅ Sent you a DM with the summary.", ephemeral=True)
                except discord.Forbidden:
                    await inter.followup.send("❌ Could not DM you.", ephemeral=True)
            except Exception:
                await inter.followup.send(f"❌ I couldn’t complete the summary. Need help? {SUPPORT_LINK}", ephemeral=True)

# ----------------- Admin-only Commands (scoped to CONTROL_GUILDS and ADMIN_ID) -----------------
for g in CONTROL_GUILDS:

    @bot.tree.command(name="usage", description="(Admin) Total usage in the last 24h.", guild=g)
    async def usage(inter: discord.Interaction):
        if not is_admin(inter):
            return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        since = _now() - 86400
        with sqlite3.connect(DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM usage_events WHERE ts > ?", (since,)).fetchone()[0]
        await inter.response.send_message(f"📊 Usage (last 24h): **{total}**", ephemeral=True)

    @bot.tree.command(name="top", description="(Admin) Top 5 servers by usage (last 7d).", guild=g)
    async def top(inter: discord.Interaction):
        if not is_admin(inter):
            return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        since = _now() - 7 * 86400
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT guild_name, COUNT(*) FROM usage_events
                WHERE ts > ?
                GROUP BY guild_id
                ORDER BY COUNT(*) DESC
                LIMIT 5
            """, (since,)).fetchall()
        if not rows:
            return await inter.response.send_message("No usage in last 7d.", ephemeral=True)
        out = "\n".join([f"{i+1}. {name} | {cnt} uses" for i, (name, cnt) in enumerate(rows)])
        await inter.response.send_message(f"🏆 Top 5 servers (7d):\n{out}", ephemeral=True)

    @bot.tree.command(name="export", description="(Admin) Export usage (last 7d) as CSV.", guild=g)
    async def export_cmd(inter: discord.Interaction):
        if not is_admin(inter):
            return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        since = _now() - 7 * 86400
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT guild_name, channel_name, user_name, user_id, command_name, ts
                FROM usage_events WHERE ts > ?
            """, (since,)).fetchall()
        if not rows:
            return await inter.response.send_message("No data to export.", ephemeral=True)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Guild", "Channel", "User", "UserID", "Command", "Timestamp"])
        for r in rows:
            writer.writerow(r)
        file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="usage.csv")
        await inter.response.send_message("📂 Exported usage (7d):", file=file, ephemeral=True)

    @bot.tree.command(name="joins", description="(Admin) Show last N servers joined.", guild=g)
    @app_commands.describe(n="How many servers to list (default 5)")
    async def joins(inter: discord.Interaction, n: Optional[int] = 5):
        if not is_admin(inter):
            return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT guild_name, joined_at FROM guild_joins ORDER BY joined_at DESC LIMIT ?",
                (n or 5,)
            ).fetchall()
        if not rows:
            return await inter.response.send_message("No join records.", ephemeral=True)
        out = "\n".join([f"{name} | joined <t:{ts}:R>" for name, ts in rows])
        await inter.response.send_message(f"📥 Last {len(rows)} joins:\n{out}", ephemeral=True)

    @bot.tree.command(name="who", description="(Admin) Who used the bot in the last 24h (top 10).", guild=g)
    async def who(inter: discord.Interaction):
        if not is_admin(inter):
            return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        since = _now() - 86400
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT user_name, user_id, COUNT(*) as c
                FROM usage_events
                WHERE ts > ?
                GROUP BY user_id
                ORDER BY c DESC
                LIMIT 10
            """, (since,)).fetchall()
        if not rows:
            return await inter.response.send_message("No usage in last 24h.", ephemeral=True)
        out = "\n".join([f"{i+1}. {name} | {cnt} calls (ID `{uid}`)"
                         for i, (name, uid, cnt) in enumerate(rows)])
        await inter.response.send_message(f"🕵️ Top users (24h):\n{out}", ephemeral=True)

    @bot.tree.command(name="whohere", description="(Admin) Last 10 calls in this guild.", guild=g)
    async def whohere(inter: discord.Interaction):
        if not is_admin(inter):
            return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        if inter.guild is None:
            return await inter.response.send_message("Use in a server.", ephemeral=True)
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT user_name, user_id, command_name, channel_name, ts
                FROM usage_events
                WHERE guild_id = ?
                ORDER BY ts DESC
                LIMIT 10
            """, (str(inter.guild.id),)).fetchall()
        if not rows:
            return await inter.response.send_message("No records for this guild.", ephemeral=True)
        out = "\n".join([f"<t:{ts}:R> | {user} (`{uid}`) ran **/{cmd}** in #{chan}"
                         for (user, uid, cmd, chan, ts) in rows])
        await inter.response.send_message(f"📜 Last 10 calls in **{inter.guild.name}**:\n{out}", ephemeral=True)

# ----------------- Events -----------------
@bot.event
async def on_guild_join(guild: discord.Guild):
    log_guild_join(guild)
    _ensure_guild_settings_row(guild.id)

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()  # global
        for g in CONTROL_GUILDS:
            await bot.tree.sync(guild=g)  # private guilds
    except Exception as e:
        print("❌ Sync error:", e)
    print(f"✅ Logged in as {bot.user}")

# ----------------- Run -----------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
