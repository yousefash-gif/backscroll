# public_backscroll_v3.py
# Discord bot with /backscroll and /backscroll_private (+ admin metrics scoped to 2 guilds)

import os
import io
import csv
import time
import sqlite3
import asyncio
import threading
from typing import List, Optional

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
    server = HTTPServer(('0.0.0.0', port), _Ping)
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
SUPPORT_LINK = "https://discord.gg/B3tb9nv8"
ADMIN_ID = 710963340360417300

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

def _now(): return int(time.time())

def log_usage(guild: Optional[discord.Guild], command_name: str):
    if not guild: return
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO usage_events (guild_id,guild_name,command_name,ts) VALUES (?,?,?,?)",
                     (str(guild.id), guild.name, command_name, _now()))
        conn.commit()

def log_guild_join(guild: discord.Guild):
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO guild_joins (guild_id,guild_name,owner_id,joined_at) VALUES (?,?,?,?)",
                     (str(guild.id), guild.name, str(guild.owner_id), _now()))
        conn.commit()

def is_admin(inter: discord.Interaction) -> bool:
    return inter.user.id == ADMIN_ID

# ----------------- Helpers -----------------
async def fetch_messages(channel: discord.TextChannel, limit: int) -> List[discord.Message]:
    out: List[discord.Message] = []
    async for m in channel.history(limit=limit, oldest_first=False):
        if m.author.bot: continue
        if not (m.content and m.content.strip()): continue
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

async def summarize_with_ai(formatted_msgs: str) -> str:
    instruction_prompt = instruction_prompt = """
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
- NEVER describe the one of the conversation!
- Do not label any part of the chat as jokes, humor, or banter. Only state what was said.
- Focus only on the content and topics of the messages, not the mood or style.
- Summarize all main themes that appeared in the chat, even if they are sensitive, awkward, or uncomfortable.
- Do not default to only one subject. If multiple distinct conversations happened, mention each
- The purpose is coverage of content, not judgment or filtering.

Length & format:
- One short paragraph (2–6 simple sentences).
- prefer shorter over longer, but if there is content to talk about make it longer
- Then a Topics list (2–6 items), each a few words.
- No emojis. No decorative lines. Use exactly these labels:

**Summary**
<paragraph here>

**Topics**
- <topic>
- <topic>

Content rules:
- Consider the whole window equally; don’t overweight first or last messages.
- Mention names only if needed for clarity.
- Keep wording concrete and literal; don’t generalize or interpret.
- If clear topics aren’t present, keep the Topics list very short or omit it.
"""
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that summarizes Discord chats."},
            {"role": "user", "content": instruction_prompt},
            {"role": "user", "content": f"Messages:\n{formatted_msgs}"}
        ],
        temperature=0.3,
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()

# ----------------- Public Slash Commands -----------------
@bot.tree.command(name="backscroll", description="Summarize the last N messages in this channel.")
@app_commands.describe(count="How many messages to fetch (1–800)")
async def backscroll(inter: discord.Interaction, count: Optional[int] = 100):
    await inter.response.defer(thinking=True)
    if not isinstance(inter.channel, discord.TextChannel):
        return await inter.followup.send("❌ This command can only be used in text channels.", ephemeral=True)

    requested = count or 100
    count = max(1, min(MAX_BACKSCROLL, requested))

    try:
        msgs = await fetch_messages(inter.channel, count)
        if not msgs: return await inter.followup.send("No messages found.", ephemeral=True)
        formatted = format_messages(msgs)
        summary = await summarize_with_ai(formatted)
        log_usage(inter.guild, "backscroll")
        await inter.followup.send(f"📜 **Summary of the last {requested} messages:**\n\n{summary}")
    except Exception:
        await inter.followup.send(f"❌ I couldn’t complete the summary. Need help? {SUPPORT_LINK}", ephemeral=True)

@bot.tree.command(name="backscroll_private", description="Summarize the last N messages and send privately.")
@app_commands.describe(count="How many messages to fetch (1–800)")
async def backscroll_private(inter: discord.Interaction, count: Optional[int] = 100):
    await inter.response.defer(thinking=True, ephemeral=True)
    if not isinstance(inter.channel, discord.TextChannel):
        return await inter.followup.send("❌ This command can only be used in text channels.", ephemeral=True)

    requested = count or 100
    count = max(1, min(MAX_BACKSCROLL, requested))

    try:
        msgs = await fetch_messages(inter.channel, count)
        if not msgs: return await inter.followup.send("No messages found.", ephemeral=True)
        formatted = format_messages(msgs)
        summary = await summarize_with_ai(formatted)
        log_usage(inter.guild, "backscroll_private")
        try:
            await inter.user.send(f"📬 **Private summary of the last {requested} messages in #{inter.channel.name}:**\n\n{summary}")
            await inter.followup.send("✅ Sent you a DM with the summary.", ephemeral=True)
        except discord.Forbidden:
            await inter.followup.send("❌ Could not DM you.", ephemeral=True)
    except Exception:
        await inter.followup.send(f"❌ I couldn’t complete the summary. Need help? {SUPPORT_LINK}", ephemeral=True)

# ----------------- Admin-only Commands (scoped) -----------------
for g in CONTROL_GUILDS:

    @bot.tree.command(name="usage", description="(Admin) Total usage in the last 24h.", guild=g)
    async def usage(inter: discord.Interaction):
        if not is_admin(inter): return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        since = _now() - 86400
        with sqlite3.connect(DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM usage_events WHERE ts > ?", (since,)).fetchone()[0]
        await inter.response.send_message(f"📊 Usage (last 24h): **{total}**", ephemeral=True)

    @bot.tree.command(name="top", description="(Admin) Top 5 servers by usage (last 7d).", guild=g)
    async def top(inter: discord.Interaction):
        if not is_admin(inter): return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        since = _now() - 7*86400
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT guild_name, COUNT(*) FROM usage_events
                WHERE ts > ?
                GROUP BY guild_id
                ORDER BY COUNT(*) DESC
                LIMIT 5
            """, (since,)).fetchall()
        if not rows: return await inter.response.send_message("No usage in last 7d.", ephemeral=True)
        out = "\n".join([f"{i+1}. {name} — {cnt} uses" for i,(name,cnt) in enumerate(rows)])
        await inter.response.send_message(f"🏆 Top 5 servers (7d):\n{out}", ephemeral=True)

    @bot.tree.command(name="export", description="(Admin) Export usage (last 7d) as CSV.", guild=g)
    async def export_cmd(inter: discord.Interaction):
        if not is_admin(inter): return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        since = _now() - 7*86400
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT guild_name, command_name, ts FROM usage_events WHERE ts > ?", (since,)).fetchall()
        if not rows: return await inter.response.send_message("No data to export.", ephemeral=True)
        buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(["Guild","Command","Timestamp"])
        for r in rows: writer.writerow(r)
        file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="usage.csv")
        await inter.response.send_message("📂 Exported usage (7d):", file=file, ephemeral=True)

    @bot.tree.command(name="joins", description="(Admin) Show last N servers joined.", guild=g)
    @app_commands.describe(n="How many servers to list (default 5)")
    async def joins(inter: discord.Interaction, n: Optional[int] = 5):
        if not is_admin(inter): return await inter.response.send_message("❌ Not allowed.", ephemeral=True)
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT guild_name, joined_at FROM guild_joins ORDER BY joined_at DESC LIMIT ?", (n or 5,)).fetchall()
        if not rows: return await inter.response.send_message("No join records.", ephemeral=True)
        out = "\n".join([f"{name} — joined <t:{ts}:R>" for name,ts in rows])
        await inter.response.send_message(f"📥 Last {len(rows)} joins:\n{out}", ephemeral=True)

# ----------------- Events -----------------
@bot.event
async def on_guild_join(guild: discord.Guild):
    log_guild_join(guild)

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
