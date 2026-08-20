import asyncio
import logging
import os
import random
import re
import signal
from contextlib import suppress
from typing import Optional

from aiohttp import web
from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

# Pyrogram's compatibility layer expects a current event loop at import time.
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait, RPCError
from pyrogram.handlers import MessageHandler, RawUpdateHandler


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "VIP-ID-CHATBOT")

API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

MONGO_URL = os.getenv("MONGO_URL", "").strip()
DATABASE_NAME = os.getenv("DATABASE_NAME", "AlexaDb").strip()

REPLY_DELAY = int(os.getenv("REPLY_DELAY", "0"))
PORT = int(os.getenv("PORT", "10000"))

# Maximum number of delayed jobs allowed in memory.
# MongoDB remains the source of truth for learned data.
MAX_PENDING_JOBS = int(os.getenv("MAX_PENDING_JOBS", "1000"))

# Optional:
# true  = reply to normal matching messages
# false = only reply when mentioned/replied to
REPLY_TO_NORMAL_MESSAGES = (
    os.getenv("REPLY_TO_NORMAL_MESSAGES", "true").lower() == "true"
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DEFAULT_GREETING_RESPONSES = (
    "Hello! Main online hoon.",
    "Hi! Kaise help karun?",
    "Hii! Bot active hai.",
)

DEFAULT_CHAT_RESPONSES = (
    "Ji, main online hoon. Bataiye.",
    "Message mila. Kaise help karun?",
    "Haan ji, boliye.",
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(APP_NAME)


# ============================================================
# VALIDATION
# ============================================================

def validate_config() -> None:
    missing = []

    if not API_ID_RAW:
        missing.append("API_ID")

    if not API_HASH:
        missing.append("API_HASH")

    if not SESSION_STRING:
        missing.append("SESSION_STRING")

    if not MONGO_URL:
        missing.append("MONGO_URL")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
        )

    try:
        int(API_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("API_ID must be an integer.") from exc

    if REPLY_DELAY < 0:
        raise RuntimeError("REPLY_DELAY cannot be negative.")

    if MAX_PENDING_JOBS < 1:
        raise RuntimeError("MAX_PENDING_JOBS must be greater than 0.")


validate_config()

API_ID = int(API_ID_RAW)

# ============================================================
# PYROGRAM
# ============================================================

client = Client(
    name="vip_id_chatbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
    no_updates=False,
)


# ============================================================
# MONGODB
# ============================================================

mongo_client = MongoClient(
    MONGO_URL,
    appname=APP_NAME,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=20000,
    maxPoolSize=20,
    minPoolSize=1,
    retryWrites=True,
)

db = mongo_client[DATABASE_NAME]

# Preserve the original Word / WordDb concept.
responses: Collection = db["WordDb"]

# Preserve original AlexaDb concept.
chat_settings: Collection = db["Alexa"]


def setup_database() -> None:
    """
    Creates indexes once at startup.
    Existing data remains compatible with the old schema.
    """

    responses.create_index(
        [("word", ASCENDING)],
        name="word_lookup",
    )

    responses.create_index(
        [("word", ASCENDING), ("check", ASCENDING)],
        name="word_type_lookup",
    )

    responses.create_index(
        [("word", ASCENDING), ("text", ASCENDING)],
        name="duplicate_response_lookup",
        unique=True,
    )

    chat_settings.create_index(
        [("chat_id", ASCENDING)],
        name="chat_id_lookup",
        unique=True,
    )

    logger.info("MongoDB indexes ready.")


# ============================================================
# STATE
# ============================================================

me_id: Optional[int] = None
me_username: Optional[str] = None

pending_jobs: set[asyncio.Task] = set()
processed_messages: set[tuple[int, int]] = set()


# ============================================================
# HELPERS
# ============================================================

def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""

    value = value.strip()

    # Keep Telegram/userbot behaviour simple and predictable.
    value = re.sub(r"\s+", " ", value)

    return value


def get_message_key(message) -> tuple[int, int]:
    return (int(message.chat.id), int(message.id))


def is_self_message(message) -> bool:
    return bool(message.outgoing or message.from_user and message.from_user.id == me_id)


def user_label(user) -> str:
    if not user:
        return "unknown"

    name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    username = f"@{user.username}" if user.username else "no_username"

    return f"{name or 'no_name'} ({username}, id={user.id})"


def message_preview(message) -> str:
    content = message.text or message.caption or "[sticker/media]"
    return content.replace("\n", " ")[:200]


async def is_chat_disabled(chat_id: int) -> bool:
    """
    Compatible with the old Alexa collection:
        {"chat_id": chat_id}

    If the document exists, replies are disabled.
    """

    try:
        return chat_settings.find_one(
            {"chat_id": chat_id},
            {"_id": 1},
        ) is not None

    except PyMongoError:
        logger.exception("Failed checking chat settings.")
        return False


def get_trigger_key(message) -> Optional[str]:
    """
    Converts incoming message into the old database's `word` key.

    Text:
        message.text

    Sticker:
        sticker.file_unique_id
    """

    if message.sticker:
        return message.sticker.file_unique_id

    text = normalize_text(message.text or message.caption)

    return text or None


def get_reply_trigger_key(message) -> Optional[str]:
    """
    Determines what the user is responding to.

    If the message replies to our bot, use the incoming text/sticker
    as the lookup key, matching the old behaviour.

    If the user replies to another user's message, the old bot used
    that message as a learning trigger.
    """

    return None


def contains_mention(message) -> bool:
    """
    Detect @username mentions.

    Also supports Telegram's entity-based mention.
    """

    text = message.text or message.caption

    if not text:
        return False

    text = text.lower()

    if me_username:
        username = me_username.lower().lstrip("@")

        if f"@{username}" in text:
            return True

    return False


def get_response_candidates(trigger: str) -> list[dict]:
    """
    Fetch all responses for a trigger.

    The old implementation randomly selected from all matching
    documents. This preserves that behaviour.
    """

    if not trigger:
        return []

    try:
        return list(
            responses.find(
                {"word": trigger},
                {
                    "_id": 0,
                    "text": 1,
                    "check": 1,
                },
            ).limit(100)
        )

    except PyMongoError:
        logger.exception("MongoDB lookup failed.")
        return []


def choose_response(trigger: str) -> Optional[dict]:
    candidates = get_response_candidates(trigger)

    if candidates:
        return random.choice(candidates)

    if re.fullmatch(r"(?:hi|hii+|hello|hey)[!.?, ]*", trigger.casefold()):
        return {
            "text": random.choice(DEFAULT_GREETING_RESPONSES),
            "check": "none",
            "instant": True,
        }

    return None


async def send_response(message, response: dict) -> None:
    response_type = response.get("check", "none")
    response_text = response.get("text")

    if not response_text:
        return

    logger.info(
        "Reply sending | to=%s | chat=%s | reply_to=%s | type=%s | text=%s",
        user_label(message.from_user),
        message.chat.id,
        message.id,
        response_type,
        str(response_text)[:200],
    )

    try:
        if response_type == "sticker":
            await client.send_sticker(
                message.chat.id,
                response_text,
                reply_to_message_id=message.id,
            )
        else:
            await client.send_message(
                message.chat.id,
                str(response_text),
                reply_to_message_id=message.id,
            )

        logger.info(
            "Reply sent successfully | to=%s | chat=%s | message=%s",
            user_label(message.from_user),
            message.chat.id,
            message.id,
        )

    except FloodWait as exc:
        logger.warning(
            "Telegram FloodWait: sleeping %s seconds.",
            exc.value,
        )

        await asyncio.sleep(exc.value)

        try:
            if response_type == "sticker":
                await client.send_sticker(
                    message.chat.id,
                    response_text,
                    reply_to_message_id=message.id,
                )
            else:
                await client.send_message(
                    message.chat.id,
                    str(response_text),
                    reply_to_message_id=message.id,
                )

        except RPCError:
            logger.exception("Failed sending after FloodWait.")

    except RPCError:
        logger.exception("Telegram RPC error while sending response.")

    except Exception:
        logger.exception("Unexpected error while sending response.")


async def delayed_reply(message, response: dict) -> None:
    """
    Delayed response worker.

    The delay is deliberately performed in a separate asyncio task,
    so one 60-second delay does NOT block other incoming messages.
    """

    try:
        await asyncio.sleep(REPLY_DELAY)

        # Don't send if the process is shutting down.
        if client.is_connected:
            await send_response(message, response)

    except asyncio.CancelledError:
        logger.debug(
            "Delayed reply cancelled for chat=%s message=%s",
            message.chat.id,
            message.id,
        )
        raise

    except Exception:
        logger.exception("Delayed reply failed.")


def schedule_reply(message, response: dict) -> None:
    if len(pending_jobs) >= MAX_PENDING_JOBS:
        logger.warning(
            "Pending reply limit reached; dropping delayed reply "
            "for chat=%s message=%s",
            message.chat.id,
            message.id,
        )
        return

    task = asyncio.create_task(delayed_reply(message, response))
    pending_jobs.add(task)

    def cleanup(completed_task: asyncio.Task) -> None:
        pending_jobs.discard(completed_task)

        with suppress(asyncio.CancelledError):
            completed_task.exception()

    task.add_done_callback(cleanup)


# ============================================================
# LEARNING
# ============================================================

def learn_text(trigger: str, text: str) -> None:
    trigger = normalize_text(trigger)
    text = normalize_text(text)

    if not trigger or not text:
        return

    try:
        responses.update_one(
            {
                "word": trigger,
                "text": text,
            },
            {
                "$setOnInsert": {
                    "word": trigger,
                    "text": text,
                    "check": "none",
                }
            },
            upsert=True,
        )

    except PyMongoError:
        logger.exception("Failed learning text response.")


def learn_sticker(trigger: str, sticker_file_id: str) -> None:
    trigger = normalize_text(trigger)

    if not trigger or not sticker_file_id:
        return

    try:
        responses.update_one(
            {
                "word": trigger,
                "text": sticker_file_id,
            },
            {
                "$setOnInsert": {
                    "word": trigger,
                    "text": sticker_file_id,
                    "check": "sticker",
                }
            },
            upsert=True,
        )

    except PyMongoError:
        logger.exception("Failed learning sticker response.")


# ============================================================
# MESSAGE LEARNING
# ============================================================

async def learn_reply_to_other_user(message) -> None:
    """
    Preserve the original bot's learning behaviour:

    User replies to another user's message:
        trigger = replied message content
        response = current message

    This does NOT create a reply to the user immediately.
    """

    if not message.reply_to_message_id:
        return

    try:
        replied = message.reply_to_message

        if not replied:
            return

        # Don't learn from our own messages.
        if replied.from_user and replied.from_user.id == me_id:
            return

        trigger = get_trigger_key(replied)

        if not trigger:
            return

        if message.sticker:
            learn_sticker(trigger, message.sticker.file_id)
            return

        text = normalize_text(message.text or message.caption)

        if text:
            learn_text(trigger, text)

    except Exception:
        logger.exception("Failed learning reply relationship.")


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def message_handler(client, message):

    logger.info(
        "Telegram update received | from=%s | chat=%s | message=%s | outgoing=%s | text=%s",
        user_label(message.from_user),
        getattr(message.chat, "id", "unknown"),
        getattr(message, "id", "unknown"),
        message.outgoing,
        message_preview(message),
    )

    # Ignore service messages, empty messages and our own messages.
    if not message:
        return

    if message.chat.type not in {
        ChatType.PRIVATE,
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        return

    if message.service:
        return

    command = (message.text or "").strip().casefold()

    if command in {".ping", "/ping", "-ping", "?ping"}:
        logger.info(
            "Ping command received | from=%s | chat=%s",
            user_label(message.from_user),
            message.chat.id,
        )
        try:
            await client.send_message(
                message.chat.id,
                "Pong! Bot is online.",
                reply_to_message_id=message.id,
            )
            logger.info(
                "Ping reply sent | to=%s | chat=%s",
                user_label(message.from_user),
                message.chat.id,
            )
        except RPCError:
            logger.exception("Failed to send ping response.")
        return

    if is_self_message(message):
        logger.info(
            "Ignoring self message | chat=%s | message=%s",
            message.chat.id,
            message.id,
        )
        return

    if re.fullmatch(r"^[/.?\-]alive(?:\s+.*)?$", command):
        if message.chat.type == ChatType.PRIVATE:
            return
        try:
            await client.send_message(
                message.chat.id,
                "**ᴀʟᴇxᴀ ᴀɪ ᴜsᴇʀʙᴏᴛ ғᴏʀ ᴄʜᴀᴛᴛɪɴɢ ɪs ᴡᴏʀᴋɪɴɢ**",
                reply_to_message_id=message.id,
            )
        except RPCError:
            logger.exception("Failed to send alive response.")
        return

    # Ignore other bots.
    try:
        sender = message.from_user

        if sender and sender.is_bot:
            return

    except Exception:
        # Do not fail the complete handler just because sender lookup
        # failed.
        sender = None

    message_key = get_message_key(message)

    if message_key in processed_messages:
        return

    processed_messages.add(message_key)

    # Prevent unlimited in-memory growth.
    if len(processed_messages) > 10000:
        processed_messages.clear()

    # Learn relationships first.
    if message.reply_to_message_id:
        await learn_reply_to_other_user(message)

    trigger = get_trigger_key(message)

    if not trigger:
        return

    logger.info(
        "Message received | chat=%s | message=%s | trigger=%s",
        message.chat.id,
        message.id,
        trigger,
    )

    chat_id = int(message.chat.id)

    # Old AlexaDb behaviour.
    if await is_chat_disabled(chat_id):
        logger.info("Replies disabled for chat=%s", chat_id)
        return

    is_private = message.chat.type == ChatType.PRIVATE
    is_mention = contains_mention(message)

    logger.info(
        "Processing %s message | chat=%s | private=%s",
        "private" if is_private else "group",
        chat_id,
        is_private,
    )

    # --------------------------------------------------------
    # Determine whether this message should receive a reply.
    # --------------------------------------------------------

    should_reply = False

    if is_private:
        should_reply = True

    elif message.reply_to_message_id:
        try:
            replied = message.reply_to_message

            if replied and replied.from_user and replied.from_user.id == me_id:
                should_reply = True

        except Exception:
            logger.exception("Failed reading replied message.")

    elif is_mention:
        should_reply = True

    elif REPLY_TO_NORMAL_MESSAGES or message.chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }:
        should_reply = True

    if not should_reply:
        return

    # --------------------------------------------------------
    # Find learned response.
    # --------------------------------------------------------

    response = choose_response(trigger)

    if not response and (is_private or is_mention):
        response = {
            "text": random.choice(DEFAULT_CHAT_RESPONSES),
            "check": "none",
            "instant": True,
        }

    if not response:
        logger.info(
            "No response configured | chat=%s | trigger=%s",
            chat_id,
            trigger,
        )
        return

    if response.get("instant") or REPLY_DELAY == 0:
        await send_response(message, response)
        logger.info(
            "Reply sent | chat=%s | message=%s | mode=%s",
            chat_id,
            message.id,
            "instant" if response.get("instant") else "immediate",
        )
        return

    # --------------------------------------------------------
    # Delayed learned reply.
    # --------------------------------------------------------

    schedule_reply(message, response)

    logger.info(
        "Reply queued | chat=%s | message=%s | delay=%ss",
        chat_id,
        message.id,
        REPLY_DELAY,
    )


async def raw_update_logger(client, update, users, chats):
    logger.info("Telegram raw update received: %s", type(update).__name__)


client.add_handler(RawUpdateHandler(raw_update_logger), group=-1)
client.add_handler(MessageHandler(message_handler), group=0)
logger.info("Telegram handlers registered: raw=-1 message=0")


# ============================================================
# HEALTH SERVER FOR RENDER
# ============================================================

async def health_handler(request):
    return web.json_response(
        {
            "status": "ok",
            "service": APP_NAME,
            "telegram_connected": client.is_connected,
            "pending_jobs": len(pending_jobs),
        }
    )


async def start_health_server():
    app = web.Application()

    app.router.add_get("/", health_handler)
    app.router.add_get("/healthz", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    logger.info("Health server listening on port %s", PORT)

    return runner


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown(health_runner=None):
    logger.info("Shutdown requested.")

    for task in list(pending_jobs):
        task.cancel()

    if pending_jobs:
        await asyncio.gather(
            *pending_jobs,
            return_exceptions=True,
        )

    pending_jobs.clear()

    if health_runner:
        with suppress(Exception):
            await health_runner.cleanup()

    with suppress(Exception):
        mongo_client.close()

        with suppress(Exception):
            await client.stop()

    logger.info("Shutdown complete.")


# ============================================================
# MAIN
# ============================================================

async def main():
    global me_id, me_username

    health_runner = await start_health_server()

    setup_database()

    logger.info("Starting Telegram client...")

    await client.start()

    me = await client.get_me()

    me_id = me.id
    me_username = me.username

    logger.info(
        "Logged in as id=%s username=%s is_bot=%s connected=%s",
        me_id,
        me_username or "none",
        me.is_bot,
        client.is_connected,
    )

    logger.info(
        "VIP-ID-CHATBOT started successfully. "
        "Reply delay=%ss",
        REPLY_DELAY,
    )

    try:
        logger.info("Telegram dispatcher idle; waiting for messages.")
        await idle()

    finally:
        await shutdown(health_runner)


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass

    except Exception:
        logger.exception("Fatal application error.")
        raise