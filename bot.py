import os
import time
import math
import logging
import asyncio
import websockets
import json
import requests
import hmac
import hashlib
from collections import deque
from urllib.parse import urlencode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CONFIG — edit these if needed
# ─────────────────────────────────────────
SYMBOL          = "RIVERUSDT"
LEVERAGE        = 50
RISK_PCT        = 0.30        # 30% of balance per trade
SL_PCT          = 0.0075      # 0.75% stop loss
TP1_PCT         = 0.014       # 1.4% take profit 1 (50% close)
TP2_PCT         = 0.030       # 3.0% take profit 2 (full close)
POLL_INTERVAL   = 5           # seconds between PnL checks
CANDLE_LIMIT    = 220         # candles to keep in memory

# EMA periods
EMA_FAST        = 20
EMA_SLOW        = 50

# Stochastic settings
STOCH_K         = 7
STOCH_SMOOTH_K  = 3
STOCH_D         = 10

# Binance Futures base URLs
BASE_REST  = "https://fapi.binance.com"
BASE_WS    = "wss://fstream.binance.com"

# API keys from environment variables (set in Railway dashboard)
API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET", "")

# ─────────────────────────────────────────
# BINANCE REST HELPERS
# ─────────────────────────────────────────
def _sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def _headers():
    return {"X-MBX-APIKEY": API_KEY}

def get_balance() -> float:
    ts = int(time.time() * 1000)
    params = {"timestamp": ts}
    params["signature"] = _sign(params)
    r = requests.get(f"{BASE_REST}/fapi/v2/balance", params=params, headers=_headers())
    r.raise_for_status()
    for asset in r.json():
        if asset["asset"] == "USDT":
            return float(asset["availableBalance"])
    return 0.0

def get_position() -> dict | None:
    ts = int(time.time() * 1000)
    params = {"symbol": SYMBOL, "timestamp": ts}
    params["signature"] = _sign(params)
    r = requests.get(f"{BASE_REST}/fapi/v2/positionRisk", params=params, headers=_headers())
    r.raise_for_status()
    for p in r.json():
        if p["symbol"] == SYMBOL:
            amt = float(p["positionAmt"])
            if amt != 0:
                return {
                    "side": "SHORT" if amt < 0 else "LONG",
                    "qty": abs(amt),
                    "entry": float(p["entryPrice"]),
                    "pnl": float(p["unRealizedProfit"]),
                    "pct": float(p["unRealizedProfit"]) / (abs(amt) * float(p["entryPrice"]) / LEVERAGE) * 100
                }
    return None

def set_leverage():
    ts = int(time.time() * 1000)
    params = {"symbol": SYMBOL, "leverage": LEVERAGE, "timestamp": ts}
    params["signature"] = _sign(params)
    requests.post(f"{BASE_REST}/fapi/v1/leverage", params=params, headers=_headers())

def get_price_precision() -> tuple[int, int]:
    r = requests.get(f"{BASE_REST}/fapi/v1/exchangeInfo")
    for s in r.json()["symbols"]:
        if s["symbol"] == SYMBOL:
            qty_prec  = s["quantityPrecision"]
            price_prec = s["pricePrecision"]
            return qty_prec, price_prec
    return 3, 4

def place_order(side: str, qty: float, price: float,
                sl: float, tp1: float, tp2: float,
                qty_prec: int, price_prec: int):
    ts = int(time.time() * 1000)
    close_side = "BUY" if side == "SHORT" else "SELL"
    order_side  = "SELL" if side == "SHORT" else "BUY"
    qty_str = f"{qty:.{qty_prec}f}"

    # Market entry
    params = {
        "symbol": SYMBOL, "side": order_side, "type": "MARKET",
        "quantity": qty_str, "timestamp": ts
    }
    params["signature"] = _sign(params)
    r = requests.post(f"{BASE_REST}/fapi/v1/order", params=params, headers=_headers())
    r.raise_for_status()
    log.info(f"ENTRY {side} {qty_str} @ ~{price:.4f}")

    time.sleep(0.5)

    # Stop loss
    ts = int(time.time() * 1000)
    sl_str = f"{sl:.{price_prec}f}"
    params = {
        "symbol": SYMBOL, "side": close_side, "type": "STOP_MARKET",
        "stopPrice": sl_str, "closePosition": "true",
        "timeInForce": "GTE_GTC", "timestamp": ts
    }
    params["signature"] = _sign(params)
    r = requests.post(f"{BASE_REST}/fapi/v1/order", params=params, headers=_headers())
    r.raise_for_status()
    log.info(f"SL set at {sl_str}")

    # TP2 — full close (remaining 50% after TP1 partial)
    time.sleep(0.3)
    ts = int(time.time() * 1000)
    tp2_str = f"{tp2:.{price_prec}f}"
    half_qty = f"{qty/2:.{qty_prec}f}"
    params = {
        "symbol": SYMBOL, "side": close_side, "type": "TAKE_PROFIT_MARKET",
        "stopPrice": tp2_str, "closePosition": "true",
        "timeInForce": "GTE_GTC", "timestamp": ts
    }
    params["signature"] = _sign(params)
    r = requests.post(f"{BASE_REST}/fapi/v1/order", params=params, headers=_headers())
    r.raise_for_status()
    log.info(f"TP2 set at {tp2_str}")

def close_partial(side: str, qty: float, qty_prec: int):
    close_side = "BUY" if side == "SHORT" else "SELL"
    half = f"{qty/2:.{qty_prec}f}"
    ts = int(time.time() * 1000)
    params = {
        "symbol": SYMBOL, "side": close_side, "type": "MARKET",
        "quantity": half, "reduceOnly": "true", "timestamp": ts
    }
    params["signature"] = _sign(params)
    r = requests.post(f"{BASE_REST}/fapi/v1/order", params=params, headers=_headers())
    r.raise_for_status()
    log.info(f"PARTIAL CLOSE 50% — {half} contracts")

def cancel_sl_and_reset(side: str, entry: float, price_prec: int):
    # Cancel all open orders
    ts = int(time.time() * 1000)
    params = {"symbol": SYMBOL, "timestamp": ts}
    params["signature"] = _sign(params)
    requests.delete(f"{BASE_REST}/fapi/v1/allOpenOrders", params=params, headers=_headers())
    log.info("Cancelled all open orders")
    time.sleep(0.3)

    # Re-set SL to breakeven (entry price)
    close_side = "BUY" if side == "SHORT" else "SELL"
    ts = int(time.time() * 1000)
    sl_str = f"{entry:.{price_prec}f}"
    params = {
        "symbol": SYMBOL, "side": close_side, "type": "STOP_MARKET",
        "stopPrice": sl_str, "closePosition": "true",
        "timeInForce": "GTE_GTC", "timestamp": ts
    }
    params["signature"] = _sign(params)
    r = requests.post(f"{BASE_REST}/fapi/v1/order", params=params, headers=_headers())
    r.raise_for_status()
    log.info(f"SL moved to BREAKEVEN @ {sl_str}")

# ─────────────────────────────────────────
# INDICATOR CALCULATIONS
# ─────────────────────────────────────────
def calc_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return 0.0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def calc_stoch(highs, lows, closes, k_period, smooth_k, d_period) -> tuple[float, float]:
    n = len(closes)
    if n < k_period + smooth_k + d_period:
        return 50.0, 50.0
    raw_k = []
    for i in range(k_period - 1, n):
        h = max(highs[i - k_period + 1: i + 1])
        l = min(lows[i - k_period + 1: i + 1])
        if h == l:
            raw_k.append(50.0)
        else:
            raw_k.append((closes[i] - l) / (h - l) * 100)
    # Smooth K
    smooth = []
    for i in range(smooth_k - 1, len(raw_k)):
        smooth.append(sum(raw_k[i - smooth_k + 1: i + 1]) / smooth_k)
    # D line
    if len(smooth) < d_period:
        return smooth[-1] if smooth else 50.0, 50.0
    d_vals = []
    for i in range(d_period - 1, len(smooth)):
        d_vals.append(sum(smooth[i - d_period + 1: i + 1]) / d_period)
    return smooth[-1], d_vals[-1]

# ─────────────────────────────────────────
# SIGNAL DETECTION
# ─────────────────────────────────────────
def check_signal(opens, highs, lows, closes) -> str | None:
    if len(closes) < CANDLE_LIMIT:
        return None

    cl = list(closes)
    hi = list(highs)
    lo = list(lows)

    ema_fast = calc_ema(cl, EMA_FAST)
    ema_slow = calc_ema(cl, EMA_SLOW)
    k, d = calc_stoch(hi, lo, cl, STOCH_K, STOCH_SMOOTH_K, STOCH_D)

    # Previous bar stoch
    k_prev, d_prev = calc_stoch(hi[:-1], lo[:-1], cl[:-1], STOCH_K, STOCH_SMOOTH_K, STOCH_D)

    price = cl[-1]
    high  = hi[-1]

    price_below_emas = price < ema_fast and price < ema_slow
    ema_rejection    = price < ema_fast and high >= ema_fast * 0.998
    stoch_cross_down = k < d and k_prev >= d_prev and k_prev > 60
    stoch_drop       = k > 65 and k < k_prev and d > k

    price_above_emas = price > ema_fast and price > ema_slow
    stoch_cross_up   = k > d and k_prev <= d_prev and k_prev < 30

    if (price_below_emas and stoch_cross_down) or (ema_rejection and stoch_drop):
        log.info(f"SHORT signal | price={price:.4f} EMA20={ema_fast:.4f} K={k:.1f} D={d:.1f}")
        return "SHORT"

    if price_above_emas and stoch_cross_up:
        log.info(f"LONG signal | price={price:.4f} EMA20={ema_fast:.4f} K={k:.1f} D={d:.1f}")
        return "LONG"

    return None

# ─────────────────────────────────────────
# POSITION MONITOR LOOP
# ─────────────────────────────────────────
async def monitor_position(state: dict, qty_prec: int, price_prec: int):
    while state.get("in_trade"):
        await asyncio.sleep(POLL_INTERVAL)
        try:
            pos = get_position()
            if pos is None:
                log.info("Position closed.")
                state["in_trade"] = False
                state["partial_done"] = False
                break

            pct = pos["pct"]
            log.info(f"PnL: {pct:.1f}% | entry={pos['entry']:.4f} | side={pos['side']}")

            # At 50% profit — close half and move SL to breakeven
            if pct >= 50 and not state.get("partial_done"):
                log.info("50% profit hit — closing half position")
                close_partial(pos["side"], pos["qty"], qty_prec)
                await asyncio.sleep(1)
                cancel_sl_and_reset(pos["side"], pos["entry"], price_prec)
                state["partial_done"] = True

        except Exception as e:
            log.error(f"Monitor error: {e}")

# ─────────────────────────────────────────
# MAIN BOT LOOP — WebSocket
# ─────────────────────────────────────────
async def run_bot():
    log.info("Starting RIVER bot...")
    set_leverage()
    qty_prec, price_prec = get_price_precision()
    log.info(f"Leverage set to {LEVERAGE}x | qty_prec={qty_prec} price_prec={price_prec}")

    opens  = deque(maxlen=CANDLE_LIMIT)
    highs  = deque(maxlen=CANDLE_LIMIT)
    lows   = deque(maxlen=CANDLE_LIMIT)
    closes = deque(maxlen=CANDLE_LIMIT)

    state = {"in_trade": False, "partial_done": False}

    stream = f"{BASE_WS}/ws/{SYMBOL.lower()}@kline_1m"

    while True:
        try:
            async with websockets.connect(stream) as ws:
                log.info(f"Connected to {stream}")
                async for msg in ws:
                    data = json.loads(msg)
                    k = data["k"]

                    # Only process closed candles
                    if not k["x"]:
                        continue

                    opens.append(float(k["o"]))
                    highs.append(float(k["h"]))
                    lows.append(float(k["l"]))
                    closes.append(float(k["c"]))

                    log.info(f"Candle closed | O={k['o']} H={k['h']} L={k['l']} C={k['c']} | bars={len(closes)}")

                    if state["in_trade"]:
                        continue

                    signal = check_signal(opens, highs, lows, closes)
                    if signal is None:
                        continue

                    # Calculate position size
                    balance = get_balance()
                    risk_amount = balance * RISK_PCT
                    entry = float(k["c"])

                    if signal == "SHORT":
                        sl  = round(entry * (1 + SL_PCT), price_prec)
                        tp1 = round(entry * (1 - TP1_PCT), price_prec)
                        tp2 = round(entry * (1 - TP2_PCT), price_prec)
                    else:
                        sl  = round(entry * (1 - SL_PCT), price_prec)
                        tp1 = round(entry * (1 + TP1_PCT), price_prec)
                        tp2 = round(entry * (1 + TP2_PCT), price_prec)

                    notional = risk_amount * LEVERAGE
                    qty = round(notional / entry, qty_prec)

                    if qty <= 0:
                        log.warning("Qty too small, skipping trade")
                        continue

                    log.info(f"Placing {signal} | balance=${balance:.2f} | notional=${notional:.2f} | qty={qty} | SL={sl} TP1={tp1} TP2={tp2}")

                    try:
                        place_order(signal, qty, entry, sl, tp1, tp2, qty_prec, price_prec)
                        state["in_trade"] = True
                        state["partial_done"] = False

                        # Start position monitor in background
                        asyncio.create_task(monitor_position(state, qty_prec, price_prec))

                    except Exception as e:
                        log.error(f"Order failed: {e}")

        except Exception as e:
            log.error(f"WebSocket error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)

# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_bot())
