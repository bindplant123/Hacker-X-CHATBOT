# VIP-ID-CHATBOT

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/bindplant123/Hacker-X-CHATBOT)

Modern 2026 Telegram userbot/chatbot based on Telethon and MongoDB.

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
- Environment-based secrets
- No hard-coded credentials

## Quick Deploy on Render

1. Click the Render button above.
2. Sign in to Render.
3. Select the repository `bindplant123/Hacker-X-CHATBOT`.
4. Add the required environment variables.
5. Deploy the service.

Render automatically uses the included `render.yaml` configuration and exposes a health check at `/healthz`.

## Required Environment Variables

```text
API_ID=12345678
API_HASH=your_api_hash_here
SESSION_STRING=your_telegram_session_string
MONGO_URL=mongodb+srv://username:password@cluster.mongodb.net/yourdb
DATABASE_NAME=AlexaDb
REPLY_DELAY=60
REPLY_TO_NORMAL_MESSAGES=true
MAX_PENDING_JOBS=1000
LOG_LEVEL=INFO
APP_NAME=VIP-ID-CHATBOT
PORT=10000
```

## How to Get Telegram Credentials

1. Open https://my.telegram.org
2. Create an app to get:
   - `API_ID`
   - `API_HASH`
3. Generate a Telethon session string using Python:

```bash
python -c "from telethon.sync import TelegramClient
from telethon.sessions import StringSession
with TelegramClient(StringSession(), 12345678, 'your_api_hash') as client:
    print(client.session.save())"
```

Replace `12345678` and `your_api_hash` with your actual values.

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
