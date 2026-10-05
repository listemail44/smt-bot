# smt-bot — Telegram IQ Option Bot

This package is the first Telegram interface build for the agreed smt-bot design.

## What it does

- Telegram is the control panel.
- IQ Option email/password authentication flow.
- Practice account is the default and real-account switching is disabled in this build.
- Minimum confidence: 80%.
- User-selected expiry.
- Manual mode.
- 1-hour Auto-Run cycle.
- Multiple simultaneous trades.
- Maximum 20 trades per cycle.
- Final re-scan before entry.
- Stops new entries when the cycle ends.
- Existing trades are allowed to settle.
- Cycle result is reported.
- Refresh/New Cycle is manual.
- Trade result logging for the current process.
- Optional user-set daily loss limit and minimum payout filter.

## Important limitations of this first build

1. The confidence score is a strategy score, not a proven probability.
2. The IQ Option connection uses the community-maintained iqoptionapi project.
3. The project itself warns against real-account use; use Practice only while testing.
4. The news filter has NOT been activated because no news-data provider has been selected.
5. Results are currently kept in memory and are lost when the bot process stops.
6. Real-account switching is intentionally disabled.
7. Telegram password messages are deleted immediately when possible, but Telegram
   should not be treated as a dedicated secrets vault.

## Telegram bot setup

1. In Telegram, open the official @BotFather.
2. Create a new bot with `/newbot`.
3. Choose the display name `smt-bot` (or a close display name if Telegram requires
   a unique username; the username must end in `bot`).
4. Copy the BotFather token.
5. Set the environment variable `SMT_BOT_TOKEN`.
6. Install the requirements.
7. Run `python smt_bot.py`.

Example on Windows Command Prompt:

set SMT_BOT_TOKEN=YOUR_TOKEN_HERE
python smt_bot.py

Do not post the BotFather token in this chat or share it with anyone.
