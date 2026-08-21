# VIP-ID-CHATBOT

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/bindplant123/Hacker-X-CHATBOT)

Modern 2026 Telegram userbot/chatbot based on Pyrogram and MongoDB.

## Features

- Telegram userbot
- Group chat responses
- Private chat responses
- Reply-to-user learning
- Reply-to-bot responses
- Text response learning
- Sticker response learning
- @mention support
- Random learned responses
- Configurable delayed replies
- Default 60-second reply delay
- MongoDB persistence
- Render deployment
- Docker deployment
- Health endpoint
- Graceful shutdown
- Voice-chat music playback with `.play`, `.pause`, `.resume`, `.skip`, and `.stop`
- Cookie-free YouTube audio downloads through Arc API
- Environment-based secrets
- No hard-coded credentials

## Quick Deploy on Render

1. Click the Render button above.
2. Sign in to Render.
3. Select the repository `bindplant123/Hacker-X-CHATBOT`.
4. Add the required environment variables. `SESSION_STRING` must be a complete
   Pyrogram session string, not an API hash, phone number, or placeholder text.
5. Deploy the service.

Render automatically uses the included `render.yaml` configuration and exposes a health check at `/healthz`.

## Required Environment Variables

```text
API_ID=12345678
API_HASH=your_api_hash_here
SESSION_STRING=your_complete_pyrogram_string_session
MONGO_URL=mongodb+srv://username:password@cluster.mongodb.net/yourdb
DATABASE_NAME=AlexaDb
REPLY_DELAY=0
REPLY_TO_NORMAL_MESSAGES=true
MAX_PENDING_JOBS=1000
LOG_LEVEL=INFO
APP_NAME=VIP-ID-CHATBOT
PORT=10000
ARC_API_URL=https://api.arcmusic.fun
ARC_API_KEY=your_arc_api_key_here
PYROGRAM_REQUEST_TIMEOUT=60
MUSIC_DOWNLOAD_TIMEOUT=180
MUSIC_COMMAND_TIMEOUT=300
```

## Voice-Chat Music

Add the user account to a Telegram group voice chat, then send `.play song name`
or `.play YouTube URL`. The player searches YouTube and downloads audio through
Arc API, so YouTube browser cookies are not required. Create an API key at
`portal.arcmusic.fun`, add it as `ARC_API_KEY` in Render, and use PyTgCalls and
FFmpeg for playback. The timeout settings allow slow Telegram media downloads;
increase them if the Render instance has a slow connection. Use `.pause`,
`.resume`, `.skip`, or `.stop` to control it.

## How to Get Telegram Credentials

1. Open https://my.telegram.org
2. Create an app to get:
   - `API_ID`
   - `API_HASH`
3. Generate a Pyrogram session string using Python. Run this locally, complete
   the Telegram login prompts, and copy the full printed value to Render's
   `SESSION_STRING` variable:

```bash
python -c "from pyrogram import Client; client = Client('session_generator', api_id=12345678, api_hash='your_api_hash'); client.start(); print(client.export_session_string()); client.stop()"
```

Replace `12345678` and `your_api_hash` with your actual values.

Do not include quotes, backticks, or the `SESSION_STRING=` prefix when pasting
the generated value into Render. After saving the variable, manually trigger
a new deploy so Render restarts the service with the updated secret.

## MongoDB Setup

Use MongoDB Atlas or any MongoDB instance reachable from Render.

Example connection string:

```text
mongodb+srv://username:password@cluster.mongodb.net/AlexaDb
```

## Local Development

```bash
python -m venv .venv
. .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then fill in your `.env` values and run:

```bash
python AlexaAi.py
```

## Render Health Check

The app exposes:

- `/` - basic health response
- `/healthz` - Render health endpoint

## Deployment Notes

- This project is designed to run as a long-lived background service.
- Render should keep the service running with the included Docker configuration.
- Do not commit your real `.env` file to GitHub.

## Repository

- GitHub: https://github.com/bindplant123/Hacker-X-CHATBOT

## License

This project is provided for educational and personal use. Follow Telegram and MongoDB service terms when using it in production.
