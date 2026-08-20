# Security Policy

## Important

Never commit any of the following files or values:

- API_ID
- API_HASH
- SESSION_STRING
- MongoDB connection strings
- Telegram session files
- `.env`

## Credential Rotation

If Telegram API credentials or a Telegram session are exposed:

1. Revoke/rotate the affected credentials.
2. Generate a new Telegram session.
3. Remove the leaked values from the repository.
4. Force-push history cleanup if necessary.
5. Redeploy using Render environment variables.

## MongoDB

Use a dedicated MongoDB database user with the minimum required permissions.

Do not use a MongoDB administrator account for the bot.

Recommended:

- separate database user
- strong generated password
- TLS enabled
- IP/network restrictions where possible

## Logging

Do not log:

- API_HASH
- SESSION_STRING
- MONGO_URL
- passwords
- authentication tokens

## Session Security

The Telethon StringSession is effectively a login credential.

Treat SESSION_STRING like a password.

Never paste it into:

- GitHub issues
- README files
- public chat
- screenshots
- public logs
