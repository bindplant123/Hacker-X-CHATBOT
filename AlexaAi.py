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
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession


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

REPLY_DELAY = int(os.getenv("REPLY_DELAY", "60"))
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
# TELEGRAM
# ============================================================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    sequential_updates=False,
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
    return (int(message.chat_id), int(message.id))


def is_self_message(message) -> bool:
    return bool(message.out or message.sender_id == me_id)


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

    text = normalize_text(message.raw_text)

    return text or None


def get_reply_trigger_key(message) -> Optional[str]:
    """
    Determines what the user is responding to.

    If the message replies to our bot, use the incoming text/sticker
    as the lookup key, matching the old behaviour.

    If the user replies to another user's message, the old bot used
    that message as a learning trigger.
    """

    reply = message.reply_to_msg_id

    if not reply:
        return None

    # The actual replied message is fetched by Telethon when required.
    return None


def contains_mention(message) -> bool:
    """
    Detect @username mentions.

    Also supports Telegram's entity-based mention.
    """

    if not message.raw_text:
        return False

    text = message.raw_text.lower()

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

    if not candidates:
        return None

    return random.choice(candidates)


async def send_response(message, response: dict) -> None:
    response_type = response.get("check", "none")
    response_text = response.get("text")

    if not response_text:
        return

    try:
        if response_type == "sticker":
            await client.send_file(
                message.chat_id,
                response_text,
                reply_to=message.id,
            )
        else:
            await client.send_message(
                message.chat_id,
                str(response_text),
                reply_to=message.id,
            )

    except FloodWaitError as exc:
        logger.warning(
            "Telegram FloodWait: sleeping %s seconds.",
            exc.seconds,
        )

        await asyncio.sleep(exc.seconds)

        try:
            if response_type == "sticker":
                await client.send_file(
                    message.chat_id,
                    response_text,
                    reply_to=message.id,
                )
            else:
                await client.send_message(
                    message.chat_id,
                    str(response_text),
                    reply_to=message.id,
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
        if client.is_connected():
            await send_response(message, response)

    except asyncio.CancelledError:
        logger.debug(
            "Delayed reply cancelled for chat=%s message=%s",
            message.chat_id,
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
            message.chat_id,
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

    if not message.is_reply:
        return

    try:
        replied = await message.get_reply_message()

        if not replied:
            return

        # Don't learn from our own messages.
        if replied.sender_id == me_id:
            return

        trigger = get_trigger_key(replied)

        if not trigger:
            return

        if message.sticker:
            learn_sticker(trigger, message.sticker.file_id)
            return

        text = normalize_text(message.raw_text)

        if text:
            learn_text(trigger, text)

    except Exception:
        logger.exception("Failed learning reply relationship.")


# ============================================================
# ALIVE
# ============================================================

@client.on(events.NewMessage(pattern=r"^[/.?\-]alive(?:\s+.*)?$"))
async def alive_handler(event):
    if event.is_private:
        return

    try:
        await event.reply(
            "**ᴀʟᴇxᴀ ᴀɪ ᴜsᴇʀʙᴏᴛ ғᴏʀ ᴄʜᴀᴛᴛɪɴɢ ɪs ᴡᴏʀᴋɪɴɢ**"
        )

    except RPCError:
        logger.exception("Failed to send alive response.")


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

@client.on(events.NewMessage(incoming=True))
async def message_handler(event):
    message = event.message

    # Ignore service messages, empty messages and our own messages.
    if not message:
        return

    if message.action:
        return

    if is_self_message(message):
        return

    # Ignore other bots.
    try:
        sender = await message.get_sender()

        if sender and getattr(sender, "bot", False):
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
    if message.is_reply:
        await learn_reply_to_other_user(message)

    trigger = get_trigger_key(message)

    if not trigger:
        return

    chat_id = int(message.chat_id)

    # Old AlexaDb behaviour.
    if await is_chat_disabled(chat_id):
        return

    is_private = bool(event.is_private)
    is_mention = contains_mention(message)

    # --------------------------------------------------------
    # Determine whether this message should receive a reply.
    # --------------------------------------------------------

    should_reply = False

    if is_private:
        should_reply = True

    elif message.is_reply:
        try:
            replied = await message.get_reply_message()

            if replied and replied.sender_id == me_id:
                should_reply = True

        except Exception:
            logger.exception("Failed reading replied message.")

    elif is_mention:
        should_reply = True

    elif REPLY_TO_NORMAL_MESSAGES:
        should_reply = True

    if not should_reply:
        return

    # --------------------------------------------------------
    # Find learned response.
    # --------------------------------------------------------

    response = choose_response(trigger)

    if not response:
        return

    # --------------------------------------------------------
    # 60-second delayed reply.
    # --------------------------------------------------------

    schedule_reply(message, response)

    logger.info(
        "Reply queued | chat=%s | message=%s | delay=%ss",
        chat_id,
        message.id,
        REPLY_DELAY,
    )


# ============================================================
# HEALTH SERVER FOR RENDER
# ============================================================

async def health_handler(request):
    return web.json_response(
        {
            "status": "ok",
            "service": APP_NAME,
            "telegram_connected": client.is_connected(),
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
        await client.disconnect()

    logger.info("Shutdown complete.")


# ============================================================
# MAIN
# ============================================================

async def main():
    global me_id, me_username

    setup_database()

    health_runner = await start_health_server()

    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def request_shutdown():
        if not stop_event.is_set():
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown)

    logger.info("Starting Telegram client...")

    await client.start()

    me = await client.get_me()

    me_id = me.id
    me_username = me.username

    logger.info(
        "Logged in as id=%s username=%s",
        me_id,
        me_username or "none",
    )

    logger.info(
        "VIP-ID-CHATBOT started successfully. "
        "Reply delay=%ss",
        REPLY_DELAY,
    )

    try:
        await stop_event.wait()

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