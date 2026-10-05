
"""
IQ Option Confidence Bot
------------------------
A configurable scanner / manual / auto-run bot for IQ Option using the
community-maintained iqoptionapi package.

IMPORTANT:
- This project defaults to PRACTICE mode.
- IQ Option does not publish an official public trading API; the execution
  adapter uses a community-maintained interface and may break when the
  platform changes.
- The displayed confidence is a strategy score, NOT a guaranteed probability
  of winning.
- The bot does not use martingale.

Core user-approved rules:
1. Minimum confidence: 80%.
2. Auto-run duration: 1 hour; user chooses stake and expiry/timeframe.
3. Multiple simultaneous trades allowed; maximum 20 trades per cycle.
4. At cycle end, stop new entries, settle/report the cycle, and wait for
   user refresh/re-entry.
5. Manual mode displays multiple qualifying opportunities for user selection.
6. Direction is BUY or SELL.
7. User-selected timeframe/expiry is used by the scan.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

try:
    from iqoptionapi.stable_api import IQ_Option
except ImportError:
    IQ_Option = None


# ---------------------------- Configuration ----------------------------

MIN_CONFIDENCE = 80.0
MAX_TRADES_PER_CYCLE = 20
DEFAULT_CYCLE_MINUTES = 60

# Weights are deliberately explicit and editable.
WEIGHTS = {
    "trend": 20,
    "ema": 15,
    "rsi": 10,
    "macd": 15,
    "adx": 10,
    "structure": 10,
    "candle": 10,
    "volatility": 5,
    "volume": 5,
}

# Conservative defaults. The user can change these later.
DEFAULT_LOOKBACK = 120
SCAN_INTERVAL_SECONDS = 2
MIN_CANDLES_REQUIRED = 80


@dataclass
class Signal:
    asset: str
    direction: str                 # BUY / SELL
    confidence: float
    timeframe_minutes: int
    reasons: List[str] = field(default_factory=list)
    payout: Optional[float] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def action(self) -> str:
        return "call" if self.direction == "BUY" else "put"


@dataclass
class TradeRecord:
    asset: str
    direction: str
    confidence: float
    amount: float
    expiry_minutes: int
    opened_at: float
    order_id: object = None
    result: Optional[float] = None
    settled: bool = False


# ---------------------------- Indicator math ----------------------------

def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: List[float], period: int) -> List[float]:
    if not values or len(values) < period:
        return []
    seed = sum(values[:period]) / period
    out = [seed]
    alpha = 2.0 / (period + 1)
    prev = seed
    for v in values[period:]:
        prev = alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)
    if not fast or not slow:
        return None, None, None

    # Align by actual EMA timestamps. fast starts at index 12, slow at 26.
    offset = 26 - 12
    aligned_fast = fast[offset:]
    if len(aligned_fast) != len(slow):
        n = min(len(aligned_fast), len(slow))
        aligned_fast = aligned_fast[-n:]
        slow = slow[-n:]
    line = [a - b for a, b in zip(aligned_fast, slow)]
    signal = ema_series(line, 9)
    if not line or not signal:
        return None, None, None
    hist = line[-1] - signal[-1]
    return line[-1], signal[-1], hist


def true_ranges(highs, lows, closes):
    out = []
    for i in range(len(closes)):
        if i == 0:
            out.append(highs[i] - lows[i])
        else:
            out.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    return out


def atr(highs, lows, closes, period=14) -> Optional[float]:
    trs = true_ranges(highs, lows, closes)
    return sma(trs, period)


def adx(highs, lows, closes, period=14) -> Optional[float]:
    if len(closes) < period * 2 + 1:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    if len(trs) < period:
        return None

    atr_v = sum(trs[:period]) / period
    plus = sum(plus_dm[:period]) / period
    minus = sum(minus_dm[:period]) / period
    dxs = []

    for i in range(period, len(trs)):
        atr_v = ((atr_v * (period - 1)) + trs[i]) / period
        plus = ((plus * (period - 1)) + plus_dm[i]) / period
        minus = ((minus * (period - 1)) + minus_dm[i]) / period
        plus_di = 100 * plus / atr_v if atr_v else 0
        minus_di = 100 * minus / atr_v if atr_v else 0
        denom = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / denom if denom else 0
        dxs.append(dx)

    if len(dxs) < period:
        return None
    return sum(dxs[-period:]) / period


def candle_score(opens, highs, lows, closes) -> Tuple[str, float]:
    """Return directional candle score in [-1, +1]."""
    if len(closes) < 3:
        return "NEUTRAL", 0.0
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    prev_c = closes[-2]
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    body_ratio = body / rng

    if c > o and c > prev_c and body_ratio >= 0.55:
        return "BUY", min(1.0, 0.55 + body_ratio * 0.45)
    if c < o and c < prev_c and body_ratio >= 0.55:
        return "SELL", min(1.0, 0.55 + body_ratio * 0.45)

    # Simple rejection patterns.
    upper = h - max(o, c)
    lower = min(o, c) - l
    if lower > body * 1.8 and lower > upper * 1.2:
        return "BUY", 0.65
    if upper > body * 1.8 and upper > lower * 1.2:
        return "SELL", 0.65
    return "NEUTRAL", 0.0


def market_structure(closes: List[float], lookback: int = 20) -> Tuple[str, float]:
    if len(closes) < lookback * 2:
        return "NEUTRAL", 0.0
    recent = closes[-lookback:]
    prior = closes[-lookback * 2:-lookback]
    recent_mean = sum(recent) / len(recent)
    prior_mean = sum(prior) / len(prior)
    spread = max(abs(prior_mean), 1e-12)
    change = (recent_mean - prior_mean) / spread

    if change > 0.0015:
        return "BUY", min(1.0, 0.55 + change * 50)
    if change < -0.0015:
        return "SELL", min(1.0, 0.55 + abs(change) * 50)
    return "NEUTRAL", 0.0


def score_direction(opens, highs, lows, closes, volumes) -> Signal:
    """
    Build a strategy score. This is a confidence score, not a calibrated
    probability. It intentionally requires directional agreement.
    """
    n = len(closes)
    close = closes[-1]

    ema20s = ema_series(closes, 20)
    ema50s = ema_series(closes, 50)
    if not ema20s or not ema50s:
        raise ValueError("Not enough candles")

    ema20 = ema20s[-1]
    ema50 = ema50s[-1]
    ema20_prev = ema20s[-2] if len(ema20s) > 1 else ema20
    ema50_prev = ema50s[-2] if len(ema50s) > 1 else ema50

    rv = rsi(closes, 14)
    ml, ms, mh = macd(closes)
    av = adx(highs, lows, closes, 14)
    at = atr(highs, lows, closes, 14)
    struct_dir, struct_strength = market_structure(closes)
    candle_dir, candle_strength = candle_score(opens, highs, lows, closes)

    buy_points = 0.0
    sell_points = 0.0
    reasons_buy, reasons_sell = [], []

    # Trend / EMA
    if ema20 > ema50:
        buy_points += WEIGHTS["trend"]
        reasons_buy.append("EMA20 above EMA50")
    elif ema20 < ema50:
        sell_points += WEIGHTS["trend"]
        reasons_sell.append("EMA20 below EMA50")

    if close > ema20:
        buy_points += WEIGHTS["ema"]
        reasons_buy.append("Price above EMA20")
    elif close < ema20:
        sell_points += WEIGHTS["ema"]
        reasons_sell.append("Price below EMA20")

    # RSI
    if rv is not None:
        if 52 <= rv <= 70:
            buy_points += WEIGHTS["rsi"]
            reasons_buy.append(f"RSI bullish ({rv:.1f})")
        elif 30 <= rv <= 48:
            sell_points += WEIGHTS["rsi"]
            reasons_sell.append(f"RSI bearish ({rv:.1f})")

    # MACD
    if ml is not None and ms is not None:
        if ml > ms and mh > 0:
            buy_points += WEIGHTS["macd"]
            reasons_buy.append("MACD bullish")
        elif ml < ms and mh < 0:
            sell_points += WEIGHTS["macd"]
            reasons_sell.append("MACD bearish")

    # ADX + directional relationship
    if av is not None and av >= 20:
        if ema20 > ema50:
            buy_points += WEIGHTS["adx"]
            reasons_buy.append(f"ADX trend strength ({av:.1f})")
        elif ema20 < ema50:
            sell_points += WEIGHTS["adx"]
            reasons_sell.append(f"ADX trend strength ({av:.1f})")

    # Structure
    if struct_dir == "BUY":
        buy_points += WEIGHTS["structure"] * struct_strength
        reasons_buy.append("Bullish market structure")
    elif struct_dir == "SELL":
        sell_points += WEIGHTS["structure"] * struct_strength
        reasons_sell.append("Bearish market structure")

    # Candle
    if candle_dir == "BUY":
        buy_points += WEIGHTS["candle"] * candle_strength
        reasons_buy.append("Bullish price action")
    elif candle_dir == "SELL":
        sell_points += WEIGHTS["candle"] * candle_strength
        reasons_sell.append("Bearish price action")

    # Volatility: penalize extremely tiny ATR relative to price.
    if at is not None and close:
        atr_pct = at / close * 100
        if 0.01 <= atr_pct <= 0.8:
            # Award a modest score to usable volatility.
            if ema20 > ema50:
                buy_points += WEIGHTS["volatility"]
                reasons_buy.append("Usable volatility")
            elif ema20 < ema50:
                sell_points += WEIGHTS["volatility"]
                reasons_sell.append("Usable volatility")

    # Tick-volume direction (only if meaningful).
    if volumes and len(volumes) >= 10:
        avg_v = sum(volumes[-10:]) / 10
        if avg_v > 0 and volumes[-1] > avg_v * 1.15:
            if closes[-1] > opens[-1]:
                buy_points += WEIGHTS["volume"]
                reasons_buy.append("Above-average bullish tick volume")
            elif closes[-1] < opens[-1]:
                sell_points += WEIGHTS["volume"]
                reasons_sell.append("Above-average bearish tick volume")

    max_score = sum(WEIGHTS.values())
    buy_conf = min(100.0, 100.0 * buy_points / max_score)
    sell_conf = min(100.0, 100.0 * sell_points / max_score)

    if buy_conf >= sell_conf:
        return Signal("", "BUY", round(buy_conf, 1), 1, reasons_buy)
    return Signal("", "SELL", round(sell_conf, 1), 1, reasons_sell)


# ---------------------------- IQ Option adapter ----------------------------

class IQOptionAdapter:
    def __init__(self, email: str, password: str, practice: bool = True):
        if IQ_Option is None:
            raise RuntimeError(
                "iqoptionapi is not installed. Install requirements.txt first."
            )
        self.email = email
        self.password = password
        self.practice = practice
        self.api = IQ_Option(email, password)

    def connect(self) -> bool:
        ok, reason = self.api.connect()
        if not ok:
            raise RuntimeError(f"IQ Option connection failed: {reason}")
        self.api.change_balance("PRACTICE" if self.practice else "REAL")
        return True

    def check_connection(self) -> bool:
        try:
            return bool(self.api.check_connect())
        except Exception:
            return False

    def reconnect(self):
        try:
            self.api.connect()
            self.api.change_balance("PRACTICE" if self.practice else "REAL")
        except Exception:
            pass

    def switch_account(self, practice: bool) -> bool:
        """Switch the already-authenticated IQ Option session between Demo and Real."""
        mode = "PRACTICE" if practice else "REAL"
        ok = self.api.change_balance(mode)
        self.practice = practice
        return True if ok is None else bool(ok)

    def balance(self) -> float:
        return float(self.api.get_balance())

    def currency(self) -> str:
        """Return the currency code for the currently selected IQ Option account."""
        try:
            value = self.api.get_currency()
            if value:
                return str(value).upper()
        except Exception:
            pass
        # Fallback: inspect the balances returned by the API.
        try:
            balances = self.api.get_balances()
            if isinstance(balances, dict):
                rows = balances.get("msg", [])
            else:
                rows = balances or []
            for row in rows:
                if isinstance(row, dict) and row.get("currency"):
                    return str(row["currency"]).upper()
        except Exception:
            pass
        return "UNKNOWN"

    def open_assets(self) -> List[str]:
        data = self.api.get_all_open_time()
        assets = set()
        for market_type in ("binary", "turbo", "digital"):
            group = data.get(market_type, {})
            for asset, info in group.items():
                if isinstance(info, dict) and info.get("open"):
                    assets.add(asset)
        return sorted(assets)

    def candles(self, asset: str, seconds: int, count: int = DEFAULT_LOOKBACK):
        return self.api.get_candles(
            asset, seconds, count, self.api.get_server_timestamp()
        )

    def payout(self, asset: str) -> Optional[float]:
        try:
            value = self.api.get_digital_current_profit(asset, 1)
            if value is False or value is None:
                return None
            return float(value)
        except Exception:
            return None

    def place_binary(self, asset: str, amount: float, direction: str, expiry: int):
        action = "call" if direction == "BUY" else "put"
        # Community API's binary order method uses minutes for expiration.
        return self.api.buy(amount, asset, action, expiry)

    def wait_result(self, order_id):
        # check_win_v3 is the community API's blocking result helper.
        return float(self.api.check_win_v3(order_id))


# ---------------------------- Application ----------------------------

class BotApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("IQ Option Confidence Bot")
        self.root.geometry("1150x760")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.adapter: Optional[IQOptionAdapter] = None
        self.running = False
        self.mode = "MANUAL"
        self.cycle_start = None
        self.cycle_end = None
        self.trades_opened = 0
        self.cycle_pnl = 0.0
        self.trade_records: List[TradeRecord] = []
        self.signals: Dict[str, Signal] = {}
        self.lock = threading.Lock()

        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="IQ Option Confidence Bot",
                  font=("Segoe UI", 18, "bold")).grid(row=0, column=0, columnspan=8, sticky="w")

        ttk.Label(top, text="Email").grid(row=1, column=0, sticky="w", pady=5)
        self.email = ttk.Entry(top, width=28)
        self.email.grid(row=1, column=1, padx=5)

        ttk.Label(top, text="Password").grid(row=1, column=2, sticky="w")
        self.password = ttk.Entry(top, width=24, show="*")
        self.password.grid(row=1, column=3, padx=5)

        self.practice_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Practice/Demo", variable=self.practice_var).grid(
            row=1, column=4, padx=5
        )

        ttk.Label(top, text="Amount / trade").grid(row=2, column=0, sticky="w", pady=5)
        self.amount = ttk.Entry(top, width=12)
        self.amount.insert(0, "500")
        self.amount.grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(top, text="Expiry (minutes)").grid(row=2, column=2, sticky="w")
        self.expiry = ttk.Entry(top, width=12)
        self.expiry.insert(0, "5")
        self.expiry.grid(row=2, column=3, sticky="w", padx=5)

        ttk.Label(top, text="Auto-run (minutes)").grid(row=2, column=4, sticky="w")
        self.cycle_minutes = ttk.Entry(top, width=12)
        self.cycle_minutes.insert(0, "60")
        self.cycle_minutes.grid(row=2, column=5, sticky="w", padx=5)

        ttk.Label(top, text="Min confidence").grid(row=2, column=6, sticky="w")
        self.confidence = ttk.Entry(top, width=10)
        self.confidence.insert(0, "80")
        self.confidence.configure(state="disabled")
        self.confidence.grid(row=2, column=7, sticky="w", padx=5)

        buttons = ttk.Frame(self.root, padding=(10, 0))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="CONNECT", command=self.connect).pack(side="left", padx=4)
        ttk.Button(buttons, text="SCAN", command=self.scan_once).pack(side="left", padx=4)
        ttk.Button(buttons, text="MANUAL MODE", command=self.set_manual).pack(side="left", padx=4)
        ttk.Button(buttons, text="START AUTO-RUN", command=self.start_auto).pack(side="left", padx=4)
        ttk.Button(buttons, text="STOP AUTO-RUN", command=self.stop_auto).pack(side="left", padx=4)
        ttk.Button(buttons, text="REFRESH / NEW CYCLE", command=self.refresh_cycle).pack(side="left", padx=4)

        self.status = tk.StringVar(value="Not connected")
        ttk.Label(self.root, textvariable=self.status, padding=10).pack(fill="x")

        cols = ("pair", "direction", "confidence", "expiry", "reason", "action")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", height=20)
        headings = {
            "pair": "Pair",
            "direction": "BUY / SELL",
            "confidence": "Confidence",
            "expiry": "Expiry",
            "reason": "Analysis",
            "action": "Manual Action",
        }
        widths = {"pair": 110, "direction": 100, "confidence": 100,
                  "expiry": 90, "reason": 500, "action": 120}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="center" if c != "reason" else "w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.tree.bind("<Double-1>", self.manual_trade_selected)

        footer = ttk.Frame(self.root, padding=10)
        footer.pack(fill="x")
        self.metrics = tk.StringVar(value="Trades: 0/20 | Cycle P/L: 0.00")
        ttk.Label(footer, textvariable=self.metrics).pack(side="left")

        ttk.Label(
            footer,
            text="Double-click a qualifying row in Manual Mode to place the selected trade.",
        ).pack(side="right")

    def ui(self, fn):
        self.root.after(0, fn)

    def connect(self):
        try:
            self.status.set("Connecting...")
            self.adapter = IQOptionAdapter(
                self.email.get().strip(),
                self.password.get(),
                self.practice_var.get(),
            )
            self.adapter.connect()
            bal = self.adapter.balance()
            self.status.set(
                f"Connected | {'PRACTICE' if self.practice_var.get() else 'REAL'} | Balance: {bal:.2f}"
            )
        except Exception as e:
            messagebox.showerror("Connection error", str(e))
            self.status.set("Connection failed")

    def expiry_minutes(self) -> int:
        value = int(self.expiry.get())
        if value <= 0:
            raise ValueError("Expiry must be greater than zero.")
        return value

    def amount_value(self) -> float:
        value = float(self.amount.get())
        if value <= 0:
            raise ValueError("Amount must be greater than zero.")
        return value

    def get_candle_seconds(self) -> int:
        # Scan granularity follows the user's expiry, with enough resolution
        # to evaluate short expiries.
        expiry = self.expiry_minutes()
        if expiry <= 1:
            return 60
        if expiry <= 5:
            return 60
        if expiry <= 15:
            return 300
        if expiry <= 30:
            return 300
        return 900

    def scan_market(self) -> List[Signal]:
        if not self.adapter:
            raise RuntimeError("Connect first.")

        expiry = self.expiry_minutes()
        seconds = self.get_candle_seconds()
        assets = self.adapter.open_assets()
        results = []

        for asset in assets:
            try:
                candles = self.adapter.candles(asset, seconds, DEFAULT_LOOKBACK)
                if not candles or len(candles) < MIN_CANDLES_REQUIRED:
                    continue

                candles = sorted(candles, key=lambda x: x["from"])
                opens = [float(c["open"]) for c in candles]
                highs = [float(c["max"]) for c in candles]
                lows = [float(c["min"]) for c in candles]
                closes = [float(c["close"]) for c in candles]
                volumes = [float(c.get("volume", 0) or 0) for c in candles]

                signal = score_direction(opens, highs, lows, closes, volumes)
                signal.asset = asset
                signal.timeframe_minutes = expiry

                # Only qualifying signals are exposed.
                if signal.confidence >= MIN_CONFIDENCE:
                    results.append(signal)
            except Exception:
                continue

        results.sort(key=lambda s: s.confidence, reverse=True)
        return results

    def populate(self, signals: List[Signal]):
        for item in self.tree.get_children():
            self.tree.delete(item)

        self.signals.clear()
        expiry = self.expiry_minutes()

        for i, s in enumerate(signals):
            self.signals[s.asset] = s
            reason = "; ".join(s.reasons[:5])
            self.tree.insert(
                "", "end", iid=s.asset,
                values=(s.asset, s.direction, f"{s.confidence:.1f}%",
                        f"{expiry} min", reason, "DOUBLE-CLICK"),
            )

        self.status.set(
            f"Scan complete: {len(signals)} qualifying pairs (minimum {MIN_CONFIDENCE:.0f}%)."
        )

    def scan_once(self):
        def worker():
            try:
                signals = self.scan_market()
                self.ui(lambda: self.populate(signals))
            except Exception as e:
                self.ui(lambda: messagebox.showerror("Scan error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def set_manual(self):
        self.mode = "MANUAL"
        self.status.set("Manual Mode: choose a displayed qualifying opportunity.")

    def manual_trade_selected(self, _event=None):
        if self.mode != "MANUAL":
            self.status.set("Switch to Manual Mode first.")
            return
        selection = self.tree.selection()
        if not selection:
            return
        asset = selection[0]
        signal = self.signals.get(asset)
        if not signal or signal.confidence < MIN_CONFIDENCE:
            return

        if not self.adapter:
            messagebox.showwarning("Not connected", "Connect to IQ Option first.")
            return

        if not messagebox.askyesno(
            "Confirm trade",
            f"{asset} {signal.direction} at {signal.confidence:.1f}% confidence\n"
            f"Amount: {self.amount_value():.2f}\n"
            f"Expiry: {self.expiry_minutes()} minutes\n\nPlace trade?"
        ):
            return

        self.place_trade(signal)

    def place_trade(self, signal: Signal) -> bool:
        if not self.adapter:
            return False
        try:
            amount = self.amount_value()
            expiry = self.expiry_minutes()

            # Final pre-entry confirmation.
            latest = self.rescan_asset(signal.asset)
            if latest is None or latest.confidence < MIN_CONFIDENCE:
                self.status.set(f"{signal.asset}: signal no longer qualifies; trade cancelled.")
                return False

            check, order_id = self.adapter.place_binary(
                signal.asset, amount, latest.direction, expiry
            )
            if not check:
                self.status.set(f"Trade failed: {signal.asset}")
                return False

            record = TradeRecord(
                asset=signal.asset,
                direction=latest.direction,
                confidence=latest.confidence,
                amount=amount,
                expiry_minutes=expiry,
                opened_at=time.time(),
                order_id=order_id,
            )
            with self.lock:
                self.trade_records.append(record)
                self.trades_opened += 1
            self.update_metrics()

            threading.Thread(
                target=self.settle_trade, args=(record,), daemon=True
            ).start()
            self.status.set(
                f"OPENED {latest.direction} {signal.asset} | "
                f"{latest.confidence:.1f}% | {expiry} min"
            )
            return True
        except Exception as e:
            self.status.set(f"Trade error: {e}")
            return False

    def rescan_asset(self, asset: str) -> Optional[Signal]:
        try:
            seconds = self.get_candle_seconds()
            candles = self.adapter.candles(asset, seconds, DEFAULT_LOOKBACK)
            candles = sorted(candles, key=lambda x: x["from"])
            opens = [float(c["open"]) for c in candles]
            highs = [float(c["max"]) for c in candles]
            lows = [float(c["min"]) for c in candles]
            closes = [float(c["close"]) for c in candles]
            volumes = [float(c.get("volume", 0) or 0) for c in candles]
            s = score_direction(opens, highs, lows, closes, volumes)
            s.asset = asset
            s.timeframe_minutes = self.expiry_minutes()
            return s
        except Exception:
            return None

    def settle_trade(self, record: TradeRecord):
        try:
            pnl = self.adapter.wait_result(record.order_id)
            record.result = pnl
            record.settled = True
            with self.lock:
                self.cycle_pnl += pnl
            self.update_metrics()
        except Exception:
            pass

    def start_auto(self):
        if self.running:
            return
        try:
            self.amount_value()
            self.expiry_minutes()
            cycle = int(self.cycle_minutes.get())
            if cycle <= 0:
                raise ValueError("Auto-run duration must be positive.")
        except Exception as e:
            messagebox.showerror("Settings error", str(e))
            return

        if not self.adapter:
            messagebox.showwarning("Not connected", "Connect first.")
            return

        self.mode = "AUTO"
        self.running = True
        self.cycle_start = time.time()
        self.cycle_end = self.cycle_start + cycle * 60
        self.trades_opened = 0
        self.cycle_pnl = 0.0
        self.trade_records.clear()
        self.status.set("AUTO-RUN started. Scanning for qualifying opportunities...")
        threading.Thread(target=self.auto_loop, daemon=True).start()

    def auto_loop(self):
        seen_this_candle = set()

        while self.running and time.time() < self.cycle_end:
            if self.trades_opened >= MAX_TRADES_PER_CYCLE:
                self.status.set("20-trade maximum reached. Waiting for cycle end.")
                break

            try:
                signals = self.scan_market()
                self.ui(lambda sigs=signals: self.populate(sigs))

                # Scan all qualifying pairs. Avoid duplicate entries for the
                # same asset while its current candle is unchanged.
                for signal in signals:
                    if not self.running or time.time() >= self.cycle_end:
                        break
                    if self.trades_opened >= MAX_TRADES_PER_CYCLE:
                        break

                    candle_key = (signal.asset, int(signal.timestamp // 30))
                    if candle_key in seen_this_candle:
                        continue

                    # Prevent stacking the same pair repeatedly while an
                    # earlier trade is still active.
                    with self.lock:
                        already_open = any(
                            r.asset == signal.asset and not r.settled
                            for r in self.trade_records
                        )
                    if already_open:
                        continue

                    if self.place_trade(signal):
                        seen_this_candle.add(candle_key)

                time.sleep(SCAN_INTERVAL_SECONDS)
            except Exception as e:
                self.status.set(f"Auto-run scan error: {e}")
                time.sleep(3)

        self.running = False
        self.ui(self.finish_cycle)

    def stop_auto(self):
        self.running = False
        self.status.set("Auto-Run stopped. Existing trades are not forcibly closed.")

    def finish_cycle(self):
        # No new trades are opened after the cycle deadline. Existing trades
        # may still be settling.
        self.status.set(
            "1-hour cycle completed: no more new trades. Waiting for active "
            "cycle trades to settle before final result."
        )
        self.wait_for_settlement()

    def wait_for_settlement(self):
        with self.lock:
            pending = [r for r in self.trade_records if not r.settled]
        if pending:
            self.root.after(1000, self.wait_for_settlement)
            return

        pnl = self.cycle_pnl
        result = "PROFIT" if pnl > 0 else "LOSS" if pnl < 0 else "BREAK-EVEN"
        self.status.set(
            f"TRADE CYCLE COMPLETED — {result} | Cycle P/L: {pnl:.2f} | "
            f"Trades: {self.trades_opened}/{MAX_TRADES_PER_CYCLE}"
        )
        messagebox.showinfo(
            "Trade cycle completed",
            f"1-hour Auto-Run cycle completed.\n\n"
            f"Result: {result}\n"
            f"Cycle P/L: {pnl:.2f}\n"
            f"Trades opened: {self.trades_opened}\n\n"
            f"Use REFRESH / NEW CYCLE when you want to re-enter."
        )

    def refresh_cycle(self):
        self.running = False
        self.trades_opened = 0
        self.cycle_pnl = 0.0
        self.trade_records.clear()
        self.signals.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.status.set("Ready for a new 1-hour cycle. Connect/scan or start Auto-Run.")
        self.update_metrics()

    def update_metrics(self):
        self.metrics.set(
            f"Trades: {self.trades_opened}/{MAX_TRADES_PER_CYCLE} | "
            f"Cycle P/L: {self.cycle_pnl:.2f}"
        )

    def close(self):
        self.running = False
        self.root.destroy()


def main():
    root = tk.Tk()
    BotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
