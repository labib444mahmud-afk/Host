# Premium Telegram Hosting Bot

A button-driven Telegram hosting manager for Python, JavaScript and ZIP projects.

## Features

- No points system
- No referral system
- Normal users: 2 projects by default
- Owner: unlimited projects
- Owner can set a custom project limit per user or make a user unlimited
- Button-based project management
- Python/Node.js support
- ZIP path-traversal and extraction-size checks
- Per-project logs
- Start / stop / run again / delete / download
- Automatic project restore after service restart when persistent storage is attached
- SQLite database
- Railway/Docker ready

## Important security note

Uploaded code is arbitrary code and runs inside the same container/service as this bot. This project is **not a hardened multi-tenant sandbox**. Only allow trusted users or move untrusted execution to isolated containers/VMs before using this as a public hosting platform.

The hosting bot's own token and selected platform secrets are not passed into uploaded processes, but same-container code should still be treated as trusted code.

## Railway storage

Attach a Railway Volume and mount it at `/data`. The bot stores its SQLite database, deployments and logs under that path.

Without a persistent volume, deployments/database/logs can disappear when the service is recreated.

## Environment variables

Required:
- `BOT_TOKEN`
- `OWNER_USER_ID`

Optional:
- `CHANNEL_ID`
- `OWNER_CONTACT`
- `DATA_ROOT=/data`
- `DEFAULT_USER_LIMIT=2`
- `MAX_CONFIGURABLE_LIMIT=100`
- `MAX_UPLOAD_MB=50`
- `MAX_ZIP_EXTRACT_MB=200`
- `MAX_ZIP_FILES=1000`

## Local run

```bash
python -m pip install -r requirements.txt
python bot.py
```
