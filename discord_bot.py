"""Discord bot entry point — run with: python discord_bot.py

Behaviour:
  - Every new message in a watched channel starts a fresh Discord thread.
  - Follow-up messages inside that thread continue the same conversation.
  - When the AI produces files, they are uploaded as attachments in the thread.
  - !reset (inside a thread) clears that thread's conversation history.

Required .env vars:
  DISCORD_BOT_TOKEN           — bot token from Discord Developer Portal
  DISCORD_ALLOWED_CHANNEL_IDS — comma-separated channel IDs to listen in
                                 (leave empty to respond in ALL text channels)
"""

import asyncio
import logging
import os
import re
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("openswarm.discord")

# ── Config ────────────────────────────────────────────────────────────────────

DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")

_raw_channel_ids = os.getenv("DISCORD_ALLOWED_CHANNEL_IDS", "")
ALLOWED_CHANNEL_IDS: set[int] = {
    int(cid.strip()) for cid in _raw_channel_ids.split(",") if cid.strip()
}

# Repo root — used to resolve relative file paths produced by agents
REPO_ROOT = Path(__file__).resolve().parent

# ── Agency setup ──────────────────────────────────────────────────────────────

from swarm import create_agency  # noqa: E402
from agency_swarm import AgencyContext, ThreadManager  # noqa: E402

agency = create_agency()

# Per-thread conversation contexts:  discord_thread_id -> AgencyContext
_thread_contexts: dict[int, AgencyContext] = {}


def _get_or_create_context(thread_id: int) -> AgencyContext:
    if thread_id not in _thread_contexts:
        _thread_contexts[thread_id] = AgencyContext(
            agency_instance=agency,
            thread_manager=ThreadManager(),
        )
    return _thread_contexts[thread_id]


def _reset_context(thread_id: int) -> None:
    _thread_contexts.pop(thread_id, None)


# ── File detection ────────────────────────────────────────────────────────────

# Matches absolute or relative Unix/Windows paths with known output extensions
_FILE_RE = re.compile(
    r"(?:^|[\s`\"'(])("                          # preceded by whitespace / quote
    r"(?:/[\w./\-_ ]+|\.{0,2}/[\w./\-_ ]+|"      # absolute or relative Unix path
    r"[A-Za-z]:\\[\w.\\/ \-_]+)"                  # or Windows path
    r"\.(?:pptx|docx|pdf|png|jpg|jpeg|gif|webp|mp4|csv|xlsx|txt|md|html|zip)"
    r")(?:[\s`\"').,]|$)",
    re.MULTILINE | re.IGNORECASE,
)

_DISCORD_FILE_LIMIT = 25 * 1024 * 1024  # 25 MB per file (free server limit)


def _find_files_in_response(text: str) -> list[Path]:
    """Return a deduplicated list of existing file paths mentioned in the response."""
    found: list[Path] = []
    seen: set[Path] = set()
    for m in _FILE_RE.finditer(text):
        raw = m.group(1).strip()
        p = Path(raw)
        if not p.is_absolute():
            p = REPO_ROOT / p
        p = p.resolve()
        if p in seen:
            continue
        seen.add(p)
        if p.exists() and p.is_file():
            found.append(p)
    return found


async def _upload_files(
    channel: discord.abc.Messageable, paths: list[Path]
) -> None:
    """Upload files as Discord attachments, batching up to 10 per message."""
    batch: list[discord.File] = []
    batch_size = 0

    async def _flush() -> None:
        nonlocal batch, batch_size
        if batch:
            await channel.send(files=batch)
            batch = []
            batch_size = 0

    for p in paths:
        size = p.stat().st_size
        if size > _DISCORD_FILE_LIMIT:
            await channel.send(
                f"⚠️ `{p.name}` is too large to attach ({size // (1024*1024)} MB). "
                f"It was saved at: `{p}`"
            )
            continue
        if len(batch) >= 10 or batch_size + size > _DISCORD_FILE_LIMIT:
            await _flush()
        batch.append(discord.File(str(p)))
        batch_size += size

    await _flush()


# ── Helpers ───────────────────────────────────────────────────────────────────

_DISCORD_LIMIT = 1990


def _split_message(text: str) -> list[str]:
    if len(text) <= _DISCORD_LIMIT:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= _DISCORD_LIMIT:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, _DISCORD_LIMIT)
        if split_at <= 0:
            split_at = _DISCORD_LIMIT
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _thread_name(author_name: str, content: str) -> str:
    """Build a short thread title from the author and their message."""
    preview = content[:40].replace("\n", " ").strip()
    label = f"{author_name}: {preview}"
    return label[:100]  # Discord thread name limit


# ── Bot ───────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
    if ALLOWED_CHANNEL_IDS:
        logger.info("Restricted to channel IDs: %s", ALLOWED_CHANNEL_IDS)
    else:
        logger.info("Listening in ALL text channels.")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    prefix = bot.command_prefix if isinstance(bot.command_prefix, str) else "!"

    # ── Message arrives in a plain text channel ───────────────────────────────
    if isinstance(message.channel, discord.TextChannel):
        if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
            return

        if message.content.startswith(prefix):
            await bot.process_commands(message)
            return

        user_message = message.content.strip()
        if not user_message:
            return

        # Create a new thread for this conversation
        thread = await message.create_thread(
            name=_thread_name(message.author.display_name, user_message),
            auto_archive_duration=1440,  # archive after 24 h of inactivity
        )
        await _process_and_reply(thread, message.author.id, thread.id, user_message)

    # ── Message arrives inside an existing thread ─────────────────────────────
    elif isinstance(message.channel, discord.Thread):
        # Only respond in threads that belong to an allowed parent channel
        parent_id = message.channel.parent_id
        if ALLOWED_CHANNEL_IDS and parent_id not in ALLOWED_CHANNEL_IDS:
            return

        if message.content.startswith(prefix):
            await bot.process_commands(message)
            return

        user_message = message.content.strip()
        if not user_message:
            return

        await _process_and_reply(message.channel, message.author.id, message.channel.id, user_message)


async def _process_and_reply(
    channel: discord.abc.Messageable,
    user_id: int,
    context_key: int,
    user_message: str,
) -> None:
    """Run the agency call and send text + file results to `channel`."""
    async with channel.typing():
        try:
            ctx = _get_or_create_context(context_key)
            result = await asyncio.to_thread(
                agency.get_response_sync,
                user_message,
                agency_context_override=ctx,
            )
            response_text = str(result.final_output).strip() if result.final_output else "_(no response)_"
        except Exception:
            logger.exception("Error processing message from user %s", user_id)
            response_text = "Something went wrong while processing your request. Please try again."

    # Send text response
    for chunk in _split_message(response_text):
        await channel.send(chunk)

    # Detect and upload any files produced by the agents
    files = _find_files_in_response(response_text)
    if files:
        await channel.send("📎 **Attaching generated files:**")
        await _upload_files(channel, files)


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name="reset")
async def cmd_reset(ctx: commands.Context) -> None:
    """Clear conversation history for the current thread."""
    if isinstance(ctx.channel, discord.Thread):
        _reset_context(ctx.channel.id)
        await ctx.send("✅ Conversation history for this thread has been cleared.")
    else:
        await ctx.send("Use `!reset` inside a thread to clear that conversation.")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context) -> None:
    await ctx.send(
        "**OpenSwarm — Discord Interface**\n"
        "Type any message in a watched channel and the bot will open a thread for your conversation.\n\n"
        "**Commands**\n"
        "`!reset` — clear this thread's conversation history\n"
        "`!help`  — show this message\n\n"
        "**Team available**\n"
        "• Director — routes your request to the right department\n"
        "• Intelligence — web research and synthesis\n"
        "• Analytics — data analysis and visualisations\n"
        "• Deck Studio — PowerPoint / presentation creation\n"
        "• Editorial — document creation and editing\n"
        "• Creative Studio — AI image creation\n"
        "• Media Studio — AI video creation\n"
        "• Operations — email, calendar, Slack, and more\n"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError(
            "DISCORD_BOT_TOKEN is not set. "
            "Add it to your .env file and re-run."
        )
    bot.run(DISCORD_BOT_TOKEN)

        
    bot.run(DISCORD_BOT_TOKEN)
