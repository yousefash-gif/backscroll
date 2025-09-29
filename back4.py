# backscroll_bot.py
# Discord bot with /backscroll and /backscroll_private
# Summarizes last N messages with AI (OpenAI SDK >= 1.0).

import os
import asyncio
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from openai import OpenAI  # NEW SDK

# ----------------- Config -----------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not DISCORD_TOKEN or not OPENAI_API_KEY:
    raise SystemExit("❌ Missing DISCORD_TOKEN or OPENAI_API_KEY in environment or .env file.")

client = OpenAI(api_key=OPENAI_API_KEY)  # NEW client

MAX_BACKSCROLL = 500

# ----------------- Discord Setup -----------------
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True  # required to read messages

bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- Helpers -----------------
async def fetch_messages(channel: discord.TextChannel, limit: int) -> List[discord.Message]:
    """Fetch last N messages, oldest → newest, skipping bots/blank."""
    out: List[discord.Message] = []
    async for m in channel.history(limit=limit, oldest_first=False):
        if m.author.bot:
            continue
        if not (m.content and m.content.strip()):
            continue
        out.append(m)
    out.sort(key=lambda m: m.created_at)  # oldest → newest
    return out

def format_messages(msgs: List[discord.Message]) -> str:
    """Format as (user, message) lines: 'DisplayName: message'."""
    lines = []
    for m in msgs:
        safe = m.content.replace("\n", " ").replace("\r", " ").strip()
        name = m.author.display_name
        lines.append(f"{name}: {safe}")
    return "\n".join(lines)

async def summarize_with_ai(formatted_msgs: str) -> str:
    """Send to OpenAI with two prompts: instruction + messages."""
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


    # Use a thread to avoid blocking the event loop (SDK call is sync)
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

# ----------------- Slash Commands -----------------
@bot.tree.command(name="backscroll", description="Summarize the last N messages in this channel.")
@app_commands.describe(count="How many messages to fetch (1–800)")
async def backscroll(inter: discord.Interaction, count: Optional[int] = 100):
    await inter.response.defer(thinking=True)  # professional loading state

    if not isinstance(inter.channel, discord.TextChannel):
        return await inter.followup.send("❌ This command can only be used in text channels.", ephemeral=True)

    count = max(1, min(MAX_BACKSCROLL, count or 100))
    try:
        msgs = await fetch_messages(inter.channel, count)
        if not msgs:
            return await inter.followup.send("No messages found to summarize.", ephemeral=True)

        formatted = format_messages(msgs)
        summary = await summarize_with_ai(formatted)

        await inter.followup.send(
            f"📜 **Summary of the last {len(msgs)} messages:**\n\n{summary}"
        )
    except Exception as e:
        await inter.followup.send(f"❌ Error while summarizing: `{e}`", ephemeral=True)

@bot.tree.command(name="backscroll_private", description="Summarize the last N messages and send privately.")
@app_commands.describe(count="How many messages to fetch (1–800)")
async def backscroll_private(inter: discord.Interaction, count: Optional[int] = 100):
    await inter.response.defer(thinking=True, ephemeral=True)

    if not isinstance(inter.channel, discord.TextChannel):
        return await inter.followup.send("❌ This command can only be used in text channels.", ephemeral=True)

    count = max(1, min(MAX_BACKSCROLL, count or 100))
    try:
        msgs = await fetch_messages(inter.channel, count)
        if not msgs:
            return await inter.followup.send("No messages found to summarize.", ephemeral=True)

        formatted = format_messages(msgs)
        summary = await summarize_with_ai(formatted)

        try:
            await inter.user.send(
                f"📬 **Private summary of the last {len(msgs)} messages in #{inter.channel.name}:**\n\n{summary}"
            )
            await inter.followup.send("✅ Sent you a DM with the summary.", ephemeral=True)
        except discord.Forbidden:
            await inter.followup.send("❌ Could not send you a DM. Please check your privacy settings.", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"❌ Error while summarizing: `{e}`", ephemeral=True)

# ----------------- Ready Event -----------------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception as e:
        print("❌ Sync error:", e)
    print(f"✅ Logged in as {bot.user} (slash commands: /backscroll, /backscroll_private)")

# ----------------- Run -----------------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
