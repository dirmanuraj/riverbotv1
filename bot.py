import os
import time
import logging
import asyncio
import websockets
import json
import requests
import hmac
import hashlib
from collections import deque
from urllib.parse import urlencode
from server import run_dashboard_thread

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SYMBOL        = "RIVERUSDT"
LEVERAGE      = 50
RISK_PCT      = 0.30
SL_PCT        = 0.0075
TP1_PCT       = 0.014
TP2_PCT       = 0.030
POLL_INTERVAL = 5
CANDLE_LIMIT  = 500      # fetch 500 candles = ~8hrs history, rock solid EMAs
EMA_FAST      = 20
EMA_SLOW      = 50
STOCH_K       = 7
STOCH_SMOOTH  = 3
STOCH_D       = 10

BASE_REST = "https://fapi.binance.com"
BASE_WS   = "wss://fstream.binance.com"
API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET", "")

# ─────────────────────────────────────────
# PRELOAD — fetches last 500 closed candles
# instantly on every startup via Binance REST
# ─────────────────────────────────────────
def preload_candles(opens, highs, lows, closes):
    log.info("Preloading 500 historical candles from Binance...")
    try:
        r = requests.get(f"{BASE_REST}/fapi/v1/klines", params={
            "symbol": SYMBOL, "interval": "1m", "limit": CANDLE_LIMIT
        })
        r.raise_for_status()
        for c in r.json():
            opens.append(float(c[1]))
            highs.append(float(c[2]))
            lows.append(float(c[3]))
            closes.append(float(c[4]))
        log.info(f"Preloaded {len(closes)} candles — ready to trade immediately!")
    except Exception as e:
        log.error(f"Preload failed: {e} — will build candles live")

# ─────────────────────────────────────────
# BINANCE HELPERS
# ─────────────────────────────────────────
def _sign(params):
    q = urlencode(params)
    return hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

def _h():
    return {"X-MBX-APIKEY": API_KEY}

def get_balance():
    ts = int(time.time() * 1000)
    p = {"timestamp": ts}
    p["signature"] = _sign(p)
    r = requests.get(f"{BASE_REST}/fapi/v2/balance", params=p, headers=_h())
    r.raise_for_status()
    for a in r.json():
        if a["asset"] == "USDT":
            return float(a["availableBalance"])
    return 0.0

def get_position():
    ts = int(time.time() * 1000)
    p = {"symbol": SYMBOL, "timestamp": ts}
    p["signature"] = _sign(p)
    r = requests.get(f"{BASE_REST}/fapi/v2/positionRisk", params=p, headers=_h())
    r.raise_for_status()
    for pos in r.json():
        if pos["symbol"] == SYMBOL:
            amt = float(pos["positionAmt"])
            if amt != 0:
                entry = float(pos["entryPrice"])
                pnl   = float(pos["unRealizedProfit"])
                return {
                    "side":  "SHORT" if amt < 0 else "LONG",
                    "qty":   abs(amt),
                    "entry": entry,
                    "pnl":   pnl,
                    "pct":   pnl / (abs(amt) * entry / LEVERAGE) * 100
                }
    return None

def set_leverage():
    ts = int(time.time() * 1000)
    p = {"symbol": SYMBOL, "leverage": LEVERAGE, "timestamp": ts}
    p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/leverage", params=p, headers=_h())

def get_precisions():
    r = requests.get(f"{BASE_REST}/fapi/v1/exchangeInfo")
    for s in r.json()["symbols"]:
        if s["symbol"] == SYMBOL:
            return s["quantityPrecision"], s["pricePrecision"]
    return 3, 4

def place_order(side, qty, price, sl, tp2, qp, pp):
    cs  = "BUY"  if side == "SHORT" else "SELL"
    os_ = "SELL" if side == "SHORT" else "BUY"
    qs  = f"{qty:.{qp}f}"

    # Market entry
    ts = int(time.time() * 1000)
    p = {"symbol": SYMBOL, "side": os_, "type": "MARKET", "quantity": qs, "timestamp": ts}
    p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h()).raise_for_status()
    log.info(f"ENTRY {side} {qs} @ ~{price:.4f}")
    time.sleep(0.5)

    # Stop loss
    ts = int(time.time() * 1000)
    p = {"symbol": SYMBOL, "side": cs, "type": "STOP_MARKET",
         "stopPrice": f"{sl:.{pp}f}", "closePosition": "true",
         "timeInForce": "GTE_GTC", "timestamp": ts}
    p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h()).raise_for_status()
    log.info(f"SL @ {sl:.{pp}f}")
    time.sleep(0.3)

    # TP2 full close
    ts = int(time.time() * 1000)
    p = {"symbol": SYMBOL, "side": cs, "type": "TAKE_PROFIT_MARKET",
         "stopPrice": f"{tp2:.{pp}f}", "closePosition": "true",
         "timeInForce": "GTE_GTC", "timestamp": ts}
    p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h()).raise_for_status()
    log.info(f"TP2 @ {tp2:.{pp}f}")

def close_partial(side, qty, qp):
    cs   = "BUY" if side == "SHORT" else "SELL"
    half = f"{qty / 2:.{qp}f}"
    ts   = int(time.time() * 1000)
    p = {"symbol": SYMBOL, "side": cs, "type": "MARKET",
         "quantity": half, "reduceOnly": "true", "timestamp": ts}
    p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h()).raise_for_status()
    log.info(f"PARTIAL CLOSE 50% — {half} contracts")

def move_sl_breakeven(side, entry, pp):
    # Cancel all existing orders first
    ts = int(time.time() * 1000)
    p  = {"symbol": SYMBOL, "timestamp": ts}
    p["signature"] = _sign(p)
    requests.delete(f"{BASE_REST}/fapi/v1/allOpenOrders", params=p, headers=_h())
    log.info("All orders cancelled")
    time.sleep(0.3)

    # New SL at entry (breakeven)
    cs = "BUY" if side == "SHORT" else "SELL"
    ts = int(time.time() * 1000)
    p  = {"symbol": SYMBOL, "side": cs, "type": "STOP_MARKET",
          "stopPrice": f"{entry:.{pp}f}", "closePosition": "true",
          "timeInForce": "GTE_GTC", "timestamp": ts}
    p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h()).raise_for_status()
    log.info(f"SL moved to BREAKEVEN @ {entry:.{pp}f}")

# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_ema(cl, period):
    if len(cl) < period:
        return 0.0
    k   = 2 / (period + 1)
    val = sum(cl[:period]) / period
    for p in cl[period:]:
        val = p * k + val * (1 - k)
    return val

def calc_stoch(hi, lo, cl, kp, sk, dp):
    n = len(cl)
    if n < kp + sk + dp:
        return 50.0, 50.0
    rk = []
    for i in range(kp - 1, n):
        h = max(hi[i - kp + 1: i + 1])
        l = min(lo[i - kp + 1: i + 1])
        rk.append(50.0 if h == l else (cl[i] - l) / (h - l) * 100)
    sm = [sum(rk[i - sk + 1: i + 1]) / sk for i in range(sk - 1, len(rk))]
    if len(sm) < dp:
        return sm[-1] if sm else 50.0, 50.0
    dv = [sum(sm[i - dp + 1: i + 1]) / dp for i in range(dp - 1, len(sm))]
    return sm[-1], dv[-1]

# ─────────────────────────────────────────
# SIGNAL DETECTION
# ─────────────────────────────────────────
def check_signal(opens, highs, lows, closes):
    if len(closes) < CANDLE_LIMIT:
        return None

    cl  = list(closes)
    hi  = list(highs)
    lo  = list(lows)

    e20   = calc_ema(cl, EMA_FAST)
    e50   = calc_ema(cl, EMA_SLOW)
    k, d  = calc_stoch(hi, lo, cl, STOCH_K, STOCH_SMOOTH, STOCH_D)
    kp, _ = calc_stoch(hi[:-1], lo[:-1], cl[:-1], STOCH_K, STOCH_SMOOTH, STOCH_D)

    price = cl[-1]
    high  = hi[-1]

    below = price < e20 and price < e50
    rej   = price < e20 and high >= e20 * 0.998
    cdn   = k < d and kp >= d and kp > 60
    sdrop = k > 65 and k < kp and d > k
    above = price > e20 and price > e50
    cup   = k > d and kp <= d and kp < 30

    if (below and cdn) or (rej and sdrop):
        log.info(f"SHORT signal | price={price:.4f} EMA20={e20:.4f} K={k:.1f} D={d:.1f}")
        return "SHORT"

    if above and cup:
        log.info(f"LONG signal | price={price:.4f} EMA20={e20:.4f} K={k:.1f} D={d:.1f}")
        return "LONG"

    return None

# ─────────────────────────────────────────
# POSITION MONITOR LOOP
# ─────────────────────────────────────────
async def monitor_position(state, qp, pp):
    while state.get("in_trade"):
        await asyncio.sleep(POLL_INTERVAL)
        try:
            pos = get_position()
            if not pos:
                log.info("Position closed.")
                state["in_trade"]    = False
                state["partial_done"] = False
                break

            log.info(f"PnL: {pos['pct']:.1f}% | entry={pos['entry']:.4f} | {pos['side']}")

            if pos["pct"] >= 50 and not state.get("partial_done"):
                log.info("50% profit hit — closing half + moving SL to breakeven")
                close_partial(pos["side"], pos["qty"], qp)
                await asyncio.sleep(1)
                move_sl_breakeven(pos["side"], pos["entry"], pp)
                state["partial_done"] = True

        except Exception as e:
            log.error(f"Monitor error: {e}")

# ─────────────────────────────────────────
# MAIN BOT LOOP
# ─────────────────────────────────────────
async def run_bot():
    log.info("Starting RIVER bot...")
    run_dashboard_thread()          # serve dashboard.html on port 8080

    set_leverage()
    qp, pp = get_precisions()
    log.info(f"Leverage={LEVERAGE}x | qty_prec={qp} | price_prec={pp}")

    opens  = deque(maxlen=CANDLE_LIMIT)
    highs  = deque(maxlen=CANDLE_LIMIT)
    lows   = deque(maxlen=CANDLE_LIMIT)
    closes = deque(maxlen=CANDLE_LIMIT)

    preload_candles(opens, highs, lows, closes)   # loads 500 candles in ~1 second

    state = {"in_trade": False, "partial_done": False}

    while True:
        try:
            async with websockets.connect(f"{BASE_WS}/ws/{SYMBOL.lower()}@kline_1m") as ws:
                log.info("WebSocket connected — monitoring live candles")
                async for msg in ws:
                    k = json.loads(msg)["k"]
                    if not k["x"]:
                        continue   # skip unfinished candles

                    opens.append(float(k["o"]))
                    highs.append(float(k["h"]))
                    lows.append(float(k["l"]))
                    closes.append(float(k["c"]))
                    log.info(f"Candle closed | C={k['c']} | total bars={len(closes)}")

                    if state["in_trade"]:
                        continue   # already in a trade, monitor loop handles it

                    sig = check_signal(opens, highs, lows, closes)
                    if not sig:
                        continue

                    # Calculate order params
                    bal   = get_balance()
                    entry = float(k["c"])

                    if sig == "SHORT":
                        sl  = round(entry * (1 + SL_PCT),  pp)
                        tp1 = round(entry * (1 - TP1_PCT), pp)
                        tp2 = round(entry * (1 - TP2_PCT), pp)
                    else:
                        sl  = round(entry * (1 - SL_PCT),  pp)
                        tp1 = round(entry * (1 + TP1_PCT), pp)
                        tp2 = round(entry * (1 + TP2_PCT), pp)

                    qty = round(bal * RISK_PCT * LEVERAGE / entry, qp)

                    if qty <= 0:
                        log.warning("Position size too small — skipping trade")
                        continue

                    log.info(f"Signal: {sig} | bal=${bal:.2f} | qty={qty} | SL={sl} | TP1={tp1} | TP2={tp2}")

                    try:
                        place_order(sig, qty, entry, sl, tp2, qp, pp)
                        state["in_trade"]    = True
                        state["partial_done"] = False
                        asyncio.create_task(monitor_position(state, qp, pp))
                    except Exception as e:
                        log.error(f"Order placement failed: {e}")

        except Exception as e:
            log.error(f"WebSocket dropped: {e} — reconnecting in 5s")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(run_bot())
