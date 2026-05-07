"""Discord bot entry point — run with: python discord_bot.py

Each Discord user gets an isolated conversation context (separate thread history).
Messages in allowed channels are routed through the full OpenSwarm agency.

Required .env vars:
  DISCORD_BOT_TOKEN          — bot token from Discord Developer Portal
  DISCORD_ALLOWED_CHANNEL_IDS — comma-separated channel IDs to listen in
                                 (leave empty to respond in ALL channels)
"""

import asyncio
import logging
import os

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

# ── Agency setup ──────────────────────────────────────────────────────────────

# Imported lazily so patches in swarm.py run before any agency_swarm import.
from swarm import create_agency  # noqa: E402
from agency_swarm import AgencyContext, ThreadManager  # noqa: E402

agency = create_agency()

# Per-user conversation contexts:  discord_user_id -> AgencyContext
_user_contexts: dict[int, AgencyContext] = {}


def _get_or_create_context(user_id: int) -> AgencyContext:
    """Return (creating if needed) an isolated AgencyContext for this user."""
    if user_id not in _user_contexts:
        _user_contexts[user_id] = AgencyContext(
            agency_instance=agency,
            thread_manager=ThreadManager(),
        )
    return _user_contexts[user_id]


def _reset_context(user_id: int) -> None:
    """Discard conversation history for a user."""
    _user_contexts.pop(user_id, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

_DISCORD_LIMIT = 1990  # Discord message character limit is 2000; leave margin


def _split_message(text: str) -> list[str]:
    """Split a response into Discord-safe chunks, breaking at newlines where possible."""
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


# ── Bot ───────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # required to read message text

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
    if ALLOWED_CHANNEL_IDS:
        logger.info("Restricted to channel IDs: %s", ALLOWED_CHANNEL_IDS)
    else:
        logger.info("Listening in ALL channels (set DISCORD_ALLOWED_CHANNEL_IDS to restrict).")


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore messages from bots (including self)
    if message.author.bot:
        return

    # Channel filtering
    if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
        return

    # Let command handlers run for messages starting with the prefix
    prefix = bot.command_prefix if isinstance(bot.command_prefix, str) else "!"
    if message.content.startswith(prefix):
        await bot.process_commands(message)
        return

    user_message = message.content.strip()
    if not user_message:
        return

    async with message.channel.typing():
        try:
            ctx = _get_or_create_context(message.author.id)
            # Run synchronous agency call in a thread pool to avoid blocking the event loop
            result = await asyncio.to_thread(
                agency.get_response_sync,
                user_message,
                agency_context_override=ctx,
            )
            response_text = str(result.final_output).strip() if result.final_output else "_(no response)_"
        except Exception:
            logger.exception("Error processing message from user %s", message.author.id)
            response_text = "Something went wrong while processing your request. Please try again."

    for chunk in _split_message(response_text):
        await message.channel.send(chunk)


@bot.command(name="reset")
async def cmd_reset(ctx: commands.Context) -> None:
    """Reset your conversation history with the swarm."""
    _reset_context(ctx.author.id)
    await ctx.send("Your conversation history has been cleared.")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context) -> None:
    """Show available commands."""
    await ctx.send(
        "**OpenSwarm — Discord Interface**\n"
        "Just type any message to chat with the AI swarm.\n\n"
        "**Commands**\n"
        "`!reset` — clear your conversation history and start fresh\n"
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
