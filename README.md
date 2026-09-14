# LABIB HOSTING BOT — Railway + GitHub

## Features
- Python `.py` and `.zip` hosting
- Background deployment queue so pip installs do not block Telegram polling
- Per-project virtual environments
- Safe ZIP extraction with file-count and expanded-size limits
- Run / Stop / Restart / Download / Delete
- Full original ZIP download
- Automatic crash recovery
- Recovery after Railway service restart
- Process and memory watchdog
- Log rotation
- Owner-only Admin Panel
- Add Points, Broadcast, Welcome Video, Maintenance, Server Stats
- Users list with username + running bot count
- Referral system
- Reply Keyboard `/start` UI

## Railway setup
1. Push these files to a GitHub repository.
2. Create a Railway service from the repository.
3. Add Railway Variables:
   - `BOT_TOKEN`
   - `OWNER_USER_ID`
   - `CHANNEL_ID` (optional)
4. Deploy. Railway uses `python bot.py` as the start command.

Never commit a real bot token to GitHub. If a token has previously been exposed, revoke it with BotFather and create a new one.

## Important
The hosted code runs as child processes of the same Railway service. This is process-level hosting, not a security sandbox. Do not host untrusted/malicious code on the same service. For hard isolation, use separate containers/VMs/services.

The number of bots that can run at once depends on the Railway plan and the actual CPU/RAM usage of each hosted bot. `MAX_RUNNING_BOTS=30` is a software cap, not a guarantee that 30 heavy bots will fit in a Railway instance.
