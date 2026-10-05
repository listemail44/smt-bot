"""
smt-bot — Telegram IQ Option Confidence Bot

Practice-first Telegram interface for the user's locked trading rules.

IMPORTANT:
- Uses the community-maintained iqoptionapi package. Its repository warns it is
  for study and not for real-account use.
- The displayed confidence is a strategy score, NOT a calibrated probability.
- Passwords are kept only in memory for the current bot process and the bot
  attempts to delete the Telegram password message immediately after receipt.
- This first Telegram build is intended for PRACTICE/DEMO testing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, ConversationHandler, filters
)

# Reuse the tested indicator/scoring engine from the desktop build.
from iqoption_confidence_bot import (
    IQOptionAdapter, Signal, score_direction, MIN_CONFIDENCE,
    MAX_TRADES_PER_CYCLE, DEFAULT_LOOKBACK
)

LOG = logging.getLogger("smt-bot")

EMAIL, PASSWORD = range(2)

DEFAULT_STAKE = 500.0
DEFAULT_EXPIRY = 5
DEFAULT_CYCLE_MINUTES = 60

@dataclass
class UserState:
    chat_id: int
    email: Optional[str] = None
    adapter: Optional[IQOptionAdapter] = None
    practice: bool = True
    stake: float = DEFAULT_STAKE
    expiry: int = DEFAULT_EXPIRY
    min_confidence: float = MIN_CONFIDENCE
    cycle_minutes: int = DEFAULT_CYCLE_MINUTES
    max_trades: int = MAX_TRADES_PER_CYCLE
    daily_loss_limit: float = 0.0  # 0 = disabled until user sets it
    min_payout: float = 0.0        # 0 = no payout filter until user sets it
    auto_running: bool = False
    auto_started_at: Optional[float] = None
    cycle_trades: List[dict] = field(default_factory=list)
    all_results: List[dict] = field(default_factory=list)
    active_tasks: set = field(default_factory=set)

STATES: Dict[int, UserState] = {}

def state_for(chat_id: int) -> UserState:
    if chat_id not in STATES:
        STATES[chat_id] = UserState(chat_id=chat_id)
    return STATES[chat_id]

def fmt_money(v: float) -> str:
    return f"₦{v:,.2f}"

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 SCAN MARKET", callback_data="scan"),
         InlineKeyboardButton("🎯 MANUAL MODE", callback_data="manual")],
        [InlineKeyboardButton("🚀 AUTO-RUN", callback_data="auto"),
         InlineKeyboardButton("🛑 STOP", callback_data="stop")],
        [InlineKeyboardButton("⚙️ SETTINGS", callback_data="settings"),
         InlineKeyboardButton("📈 RESULTS", callback_data="results")],
        [InlineKeyboardButton("🔄 REFRESH / NEW CYCLE", callback_data="refresh")],
    ])

def settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Stake", callback_data="set_stake"),
         InlineKeyboardButton("⏱ Expiry", callback_data="set_expiry")],
        [InlineKeyboardButton("🛡 Daily Loss Limit", callback_data="set_loss"),
         InlineKeyboardButton("💵 Min Payout", callback_data="set_payout")],
        [InlineKeyboardButton("🏦 Practice / Real", callback_data="set_account")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="main")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = state_for(chat_id)
    if not s.adapter:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 CONNECT IQ OPTION", callback_data="connect")]
        ])
        await update.message.reply_text(
            "🤖 *smt-bot*\n\n"
            "IQ Option Confidence Trading Bot\n\n"
            "Minimum confidence: *80%*\n"
            "Auto-Run: *1 hour*\n"
            "Maximum trades/cycle: *20*\n\n"
            "Practice/Demo is the default.\n\n"
            "Press CONNECT to begin.",
            parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text("🤖 *smt-bot* is connected.", parse_mode="Markdown",
                                        reply_markup=main_menu())

async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "🔐 Enter the *IQ Option email* you want smt-bot to connect to.",
        parse_mode="Markdown")
    return EMAIL

async def got_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email:
        await update.message.reply_text("That does not look like a valid email. Please enter it again.")
        return EMAIL
    context.user_data["pending_email"] = email
    await update.message.reply_text(
        "✅ Email received.\n\n"
        "Now enter your *IQ Option password*.\n\n"
        "I will attempt to remove the password message immediately after receiving it.",
        parse_mode="Markdown")
    return PASSWORD

async def got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    password = update.message.text or ""
    email = context.user_data.get("pending_email")
    try:
        await update.message.delete()
    except Exception:
        pass

    if not email or not password:
        await update.effective_chat.send_message("❌ Missing login information. Start again with /start.")
        return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await context.bot.send_message(chat_id, "🔄 Connecting to IQ Option Practice account...")

    def do_connect():
        adapter = IQOptionAdapter(email, password, practice=True)
        adapter.connect()
        return adapter, adapter.balance()

    try:
        adapter, balance = await asyncio.to_thread(do_connect)
    except Exception as e:
        LOG.exception("Connection failed")
        await context.bot.send_message(
            chat_id,
            "❌ *Connection failed.*\n\n"
            "The IQ Option account could not be authenticated.\n"
            "No trade was placed.\n\n"
            f"Technical message: `{str(e)[:300]}`",
            parse_mode="Markdown")
        context.user_data.pop("pending_email", None)
        return ConversationHandler.END

    s = state_for(chat_id)
    s.email = email
    s.adapter = adapter
    s.practice = True
    context.user_data.pop("pending_email", None)

    await context.bot.send_message(
        chat_id,
        f"✅ *IQ OPTION CONNECTED*\n\n"
        f"Account: PRACTICE\n"
        f"Balance: `{balance:,.2f}`\n\n"
        f"Minimum confidence: *{s.min_confidence:.0f}%*\n"
        f"Auto-Run cycle: *{s.cycle_minutes} minutes*\n"
        f"Maximum trades: *{s.max_trades}*\n\n"
        "Choose an action:",
        parse_mode="Markdown", reply_markup=main_menu())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

def candle_seconds(expiry: int) -> int:
    return max(60, expiry * 60)

async def scan_signals(s: UserState, limit: int = 6):
    if not s.adapter:
        raise RuntimeError("Not connected.")
    assets = await asyncio.to_thread(s.adapter.open_assets)
    signals = []

    # Scan a bounded set to keep Telegram responsive.
    for asset in assets[:80]:
        try:
            candles = await asyncio.to_thread(
                s.adapter.candles, asset, candle_seconds(s.expiry), DEFAULT_LOOKBACK
            )
            if not candles or len(candles) < 80:
                continue

            opens = [float(x["open"]) for x in candles]
            highs = [float(x["max"]) for x in candles]
            lows = [float(x["min"]) for x in candles]
            closes = [float(x["close"]) for x in candles]
            volumes = [float(x.get("volume", 0) or 0) for x in candles]

            sig = score_direction(opens, highs, lows, closes, volumes)
            sig.asset = asset
            sig.timeframe_minutes = s.expiry

            if sig.confidence < s.min_confidence:
                continue

            # Optional user-set payout filter. Zero means disabled.
            if s.min_payout > 0:
                payout = await asyncio.to_thread(s.adapter.payout, asset)
                sig.payout = payout
                if payout is None or payout < s.min_payout:
                    continue

            signals.append(sig)
        except Exception:
            continue

    signals.sort(key=lambda x: x.confidence, reverse=True)
    return signals[:limit]

def signal_text(sig: Signal) -> str:
    reasons = "; ".join(sig.reasons[:4])
    payout = f"\nPayout: {sig.payout:.1f}%" if sig.payout is not None else ""
    return (f"*{sig.asset}* — *{sig.direction}* — *{sig.confidence:.1f}%*\n"
            f"Expiry: {sig.timeframe_minutes} min{payout}\n"
            f"Analysis: {reasons}")

async def scan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    if not s.adapter:
        await q.message.reply_text("Please connect IQ Option first.")
        return
    await q.message.reply_text("📊 Scanning available pairs...")
    try:
        signals = await scan_signals(s)
    except Exception as e:
        await q.message.reply_text(f"❌ Scan failed: {str(e)[:250]}")
        return
    if not signals:
        await q.message.reply_text(
            f"🔎 No qualifying setup found at or above {s.min_confidence:.0f}%.\n"
            "No trade will be suggested.")
        return
    text = "📊 *QUALIFYING OPPORTUNITIES*\n\n" + "\n\n".join(signal_text(x) for x in signals)
    await q.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu())

async def manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    if not s.adapter:
        await q.message.reply_text("Please connect IQ Option first.")
        return
    await q.message.reply_text("🎯 Scanning for manual opportunities...")
    try:
        signals = await scan_signals(s)
    except Exception as e:
        await q.message.reply_text(f"❌ Scan failed: {str(e)[:250]}")
        return
    if not signals:
        await q.message.reply_text("No manual opportunity currently meets the 80% minimum.")
        return

    buttons = []
    for i, sig in enumerate(signals):
        context.user_data[f"manual_{i}"] = sig
        buttons.append([InlineKeyboardButton(
            f"{sig.asset} {sig.direction} {sig.confidence:.0f}%",
            callback_data=f"manual_trade_{i}")])
    await q.message.reply_text(
        "🎯 *MANUAL MODE*\n\nSelect a qualifying opportunity:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def manual_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    idx = q.data.rsplit("_", 1)[-1]
    sig = context.user_data.get(f"manual_{idx}")
    if not sig:
        await q.message.reply_text("Signal expired. Scan again.")
        return

    # Final confirmation scan — do not blindly reuse an old signal.
    try:
        fresh = await scan_signals(s, limit=20)
        current = next((x for x in fresh if x.asset == sig.asset), None)
    except Exception:
        current = None

    if not current or current.confidence < s.min_confidence or current.direction != sig.direction:
        await q.message.reply_text("⚠️ Trade cancelled: the setup no longer meets the locked conditions.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ CONFIRM TRADE", callback_data=f"confirm_manual_{idx}")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="main")],
    ])
    await q.message.reply_text(
        f"⚠️ *FINAL CONFIRMATION*\n\n{signal_text(current)}\n"
        f"Stake: {fmt_money(s.stake)}\n\n"
        "Execute this trade?",
        parse_mode="Markdown", reply_markup=kb)
    context.user_data[f"manual_{idx}"] = current

async def confirm_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    idx = q.data.rsplit("_", 1)[-1]
    sig = context.user_data.get(f"manual_{idx}")
    if not sig:
        await q.message.reply_text("Signal expired.")
        return

    # Do not allow manual trading if a daily loss limit has already been reached.
    if s.daily_loss_limit > 0:
        today_loss = sum(-min(0, r.get("result", 0)) for r in s.all_results
                         if r.get("day") == datetime.now().date().isoformat())
        if today_loss >= s.daily_loss_limit:
            await q.message.reply_text("🛑 Daily loss limit reached. No new trade will be opened.")
            return

    await q.message.reply_text("🔄 Executing...")
    try:
        ok, order_id = await asyncio.to_thread(
            s.adapter.place_binary, sig.asset, s.stake, sig.direction, s.expiry)
    except Exception as e:
        await q.message.reply_text(f"❌ Execution failed: {str(e)[:250]}")
        return
    if not ok:
        await q.message.reply_text("❌ IQ Option did not confirm the trade.")
        return

    record = dict(asset=sig.asset, direction=sig.direction,
                  confidence=sig.confidence, amount=s.stake,
                  expiry=s.expiry, opened_at=time.time(),
                  order_id=order_id, day=datetime.now().date().isoformat())
    s.all_results.append(record)
    await q.message.reply_text(
        f"🟢 *TRADE OPENED*\n\n{sig.asset} — {sig.direction}\n"
        f"Confidence: {sig.confidence:.1f}%\nStake: {fmt_money(s.stake)}\n"
        f"Expiry: {s.expiry} min",
        parse_mode="Markdown")
    task = asyncio.create_task(settle_trade(q.message.chat_id, record))
    s.active_tasks.add(task)
    task.add_done_callback(lambda t: s.active_tasks.discard(t))

async def settle_trade(chat_id: int, record: dict):
    s = state_for(chat_id)
    try:
        result = await asyncio.to_thread(s.adapter.wait_result, record["order_id"])
        record["result"] = float(result)
        if result > 0:
            label = f"✅ WIN +{fmt_money(result)}"
        elif result < 0:
            label = f"🔴 LOSS {fmt_money(result)}"
        else:
            label = "⚪ BREAK-EVEN"
        await app_instance.bot.send_message(
            chat_id,
            f"📌 *TRADE RESULT*\n\n{record['asset']} — {record['direction']}\n{label}",
            parse_mode="Markdown")
    except Exception as e:
        await app_instance.bot.send_message(chat_id, f"⚠️ Could not retrieve trade result: {str(e)[:250]}")

async def auto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    if not s.adapter:
        await q.message.reply_text("Please connect IQ Option first.")
        return
    if s.auto_running:
        await q.message.reply_text("Auto-Run is already running.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ START 1-HOUR AUTO-RUN", callback_data="start_auto")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="main")],
    ])
    await q.message.reply_text(
        f"🚀 *AUTO-RUN*\n\nStake: {fmt_money(s.stake)}\n"
        f"Expiry: {s.expiry} min\nCycle: 1 hour\n"
        f"Minimum confidence: {s.min_confidence:.0f}%\n"
        f"Maximum trades: {s.max_trades}\n\nStart?",
        parse_mode="Markdown", reply_markup=kb)

async def start_auto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    if s.auto_running:
        return
    s.auto_running = True
    s.auto_started_at = time.time()
    s.cycle_trades = []
    await q.message.reply_text("🟢 *AUTO-RUN STARTED*\nCycle: 1 hour\nTrades: 0/20",
                               parse_mode="Markdown")
    task = asyncio.create_task(auto_loop(q.message.chat_id))
    s.active_tasks.add(task)
    task.add_done_callback(lambda t: s.active_tasks.discard(t))

async def auto_loop(chat_id: int):
    s = state_for(chat_id)
    end = time.time() + s.cycle_minutes * 60
    last_assets = set()

    while s.auto_running and time.time() < end and len(s.cycle_trades) < s.max_trades:
        try:
            signals = await scan_signals(s, limit=10)
            for sig in signals:
                if len(s.cycle_trades) >= s.max_trades or time.time() >= end:
                    break
                # Avoid opening the same pair repeatedly while a previous trade is active.
                active_assets = {r["asset"] for r in s.cycle_trades
                                 if not r.get("settled", False)}
                if sig.asset in active_assets:
                    continue
                # Final re-scan of this pair.
                fresh = await scan_signals(s, limit=20)
                current = next((x for x in fresh if x.asset == sig.asset), None)
                if not current or current.confidence < s.min_confidence:
                    continue
                ok, order_id = await asyncio.to_thread(
                    s.adapter.place_binary, current.asset, s.stake,
                    current.direction, s.expiry)
                if not ok:
                    continue
                record = dict(asset=current.asset, direction=current.direction,
                              confidence=current.confidence, amount=s.stake,
                              expiry=s.expiry, opened_at=time.time(),
                              order_id=order_id, day=datetime.now().date().isoformat(),
                              settled=False)
                s.cycle_trades.append(record)
                s.all_results.append(record)
                await app_instance.bot.send_message(
                    chat_id,
                    f"🟢 *TRADE #{len(s.cycle_trades)} OPENED*\n\n"
                    f"{current.asset} — {current.direction}\n"
                    f"Confidence: {current.confidence:.1f}%\n"
                    f"Stake: {fmt_money(s.stake)}\nExpiry: {s.expiry} min",
                    parse_mode="Markdown")
                task = asyncio.create_task(settle_auto_trade(chat_id, record))
                s.active_tasks.add(task)
                task.add_done_callback(lambda t: s.active_tasks.discard(t))
            await asyncio.sleep(2)
        except Exception as e:
            LOG.exception("Auto loop error")
            await asyncio.sleep(3)

    s.auto_running = False
    await app_instance.bot.send_message(
        chat_id,
        "⏱️ *AUTO-RUN PERIOD ENDED*\n\n"
        "No new trades will be opened. Existing cycle trades will be allowed to finish.",
        parse_mode="Markdown")
    while any(not r.get("settled", False) for r in s.cycle_trades):
        await asyncio.sleep(2)

    wins = sum(1 for r in s.cycle_trades if (r.get("result") or 0) > 0)
    losses = sum(1 for r in s.cycle_trades if (r.get("result") or 0) < 0)
    pnl = sum(r.get("result", 0) or 0 for r in s.cycle_trades)
    await app_instance.bot.send_message(
        chat_id,
        f"🏁 *CYCLE COMPLETED*\n\nTrades: {len(s.cycle_trades)}\n"
        f"Wins: {wins}\nLosses: {losses}\n"
        f"Overall result: `{fmt_money(pnl)}`\n\n"
        "The bot will NOT start another cycle automatically.\n"
        "Use Refresh / New Cycle when you decide to continue.",
        parse_mode="Markdown", reply_markup=main_menu())

async def settle_auto_trade(chat_id: int, record: dict):
    s = state_for(chat_id)
    try:
        result = await asyncio.to_thread(s.adapter.wait_result, record["order_id"])
        record["result"] = float(result)
        record["settled"] = True
        if result > 0:
            label = f"✅ WIN +{fmt_money(result)}"
        elif result < 0:
            label = f"🔴 LOSS {fmt_money(result)}"
        else:
            label = "⚪ BREAK-EVEN"
        await app_instance.bot.send_message(
            chat_id,
            f"📌 *TRADE RESULT*\n{record['asset']} — {record['direction']}\n{label}",
            parse_mode="Markdown")
    except Exception as e:
        record["settled"] = True
        await app_instance.bot.send_message(chat_id, f"⚠️ Result check failed: {str(e)[:250]}")

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    s.auto_running = False
    await q.message.reply_text("🛑 New trade entries have been stopped. Existing trades are not falsely marked as cancelled.")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    await q.message.reply_text(
        f"⚙️ *SETTINGS*\n\nStake: {fmt_money(s.stake)}\nExpiry: {s.expiry} min\n"
        f"Minimum confidence: {s.min_confidence:.0f}%\nAuto-Run: {s.cycle_minutes} min\n"
        f"Max trades: {s.max_trades}\nDaily loss limit: {fmt_money(s.daily_loss_limit)}\n"
        f"Minimum payout filter: {s.min_payout:.1f}% (0 = off)\n"
        f"Account: {'PRACTICE' if s.practice else 'REAL'}",
        parse_mode="Markdown", reply_markup=settings_menu())

async def setting_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    q = update.callback_query
    await q.answer()
    context.user_data["setting_kind"] = kind
    prompts = {
        "stake": "Enter stake amount in Naira, e.g. 500",
        "expiry": "Enter expiry in minutes, e.g. 1, 5, 15, 30, 60",
        "loss": "Enter daily loss limit in Naira. Enter 0 to disable.",
        "payout": "Enter minimum payout percentage. Enter 0 to disable the payout filter.",
    }
    await q.message.reply_text(prompts[kind])

async def setting_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind = context.user_data.get("setting_kind")
    if not kind:
        return
    s = state_for(update.effective_chat.id)
    try:
        value = float(update.message.text.strip())
        if kind == "stake":
            if value <= 0: raise ValueError
            s.stake = value
        elif kind == "expiry":
            value = int(value)
            if value <= 0: raise ValueError
            s.expiry = value
        elif kind == "loss":
            if value < 0: raise ValueError
            s.daily_loss_limit = value
        elif kind == "payout":
            if value < 0 or value > 100: raise ValueError
            s.min_payout = value
        context.user_data.pop("setting_kind", None)
        await update.message.reply_text("✅ Setting updated.", reply_markup=main_menu())
    except Exception:
        await update.message.reply_text("❌ Invalid value. Please enter a valid number.")

async def account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "⚠️ This build is Practice-first. Real-account switching is intentionally disabled "
        "until the demo workflow is fully tested.")

async def results_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    trades = s.all_results
    wins = sum(1 for r in trades if (r.get("result") or 0) > 0)
    losses = sum(1 for r in trades if (r.get("result") or 0) < 0)
    pnl = sum(r.get("result", 0) or 0 for r in trades)
    await q.message.reply_text(
        f"📈 *RESULTS*\n\nTrades: {len(trades)}\nWins: {wins}\nLosses: {losses}\n"
        f"Win rate: {(wins/len(trades)*100 if trades else 0):.1f}%\n"
        f"Net result: `{fmt_money(pnl)}`",
        parse_mode="Markdown", reply_markup=main_menu())

async def refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = state_for(q.message.chat_id)
    if s.auto_running:
        await q.message.reply_text("Stop the current Auto-Run before refreshing.")
        return
    s.cycle_trades = []
    await q.message.reply_text("🔄 New cycle is ready. No new cycle has been started automatically.",
                               reply_markup=main_menu())

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data == "connect":
        return await connect_start(update, context)
    if data == "scan":
        return await scan_callback(update, context)
    if data == "manual":
        return await manual_callback(update, context)
    if data.startswith("manual_trade_"):
        return await manual_trade_callback(update, context)
    if data.startswith("confirm_manual_"):
        return await confirm_manual_callback(update, context)
    if data == "auto":
        return await auto_callback(update, context)
    if data == "start_auto":
        return await start_auto_callback(update, context)
    if data == "stop":
        return await stop_callback(update, context)
    if data == "settings":
        return await settings_callback(update, context)
    if data == "results":
        return await results_callback(update, context)
    if data == "refresh":
        return await refresh_callback(update, context)
    if data == "main":
        await q.answer()
        await q.message.reply_text("Main menu:", reply_markup=main_menu())
        return
    if data == "set_stake":
        return await setting_prompt(update, context, "stake")
    if data == "set_expiry":
        return await setting_prompt(update, context, "expiry")
    if data == "set_loss":
        return await setting_prompt(update, context, "loss")
    if data == "set_payout":
        return await setting_prompt(update, context, "payout")
    if data == "set_account":
        return await account_callback(update, context)

async def post_init(app: Application):
    global app_instance
    app_instance = app

def build_app():
    token = os.getenv("SMT_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set SMT_BOT_TOKEN to the token generated by Telegram BotFather.")

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(connect_start, pattern=r"^connect$")],
        states={
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_email)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(conversation)
    application.add_handler(CallbackQueryHandler(button_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, setting_message))
    return application

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = build_app()
    print("smt-bot is running. Press Ctrl+C to stop.")
    app.run_polling()
