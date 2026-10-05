# smt-bot Telegram deployment

This build includes the locked trading rules and the responsiveness/account-mode fixes.

## Locked rules
- Minimum strategy confidence: 80%
- Auto-Run: 1 hour
- Maximum 20 trades per cycle
- Multiple simultaneous trades allowed
- User-selected expiry
- BUY / SELL signals
- Final pre-entry rescan
- Cycle does not restart automatically

## Responsiveness fix
Market scans run as background tasks and Telegram processes updates concurrently, so STOP and other buttons remain responsive while scanning.

## Account mode
- DEMO is the default account mode.
- Settings > Account provides DEMO and REAL options.
- The bot displays DEMO to the user; the underlying IQ Option API uses its PRACTICE balance identifier internally.
- Switching accounts is blocked while a scan or Auto-Run is active.
- Real-account trading carries financial risk; test the complete workflow in DEMO first.

## Railway environment variable
Set:
`SMT_BOT_TOKEN=<your Telegram BotFather token>`

Never send the Telegram bot token or IQ Option password to anyone.
