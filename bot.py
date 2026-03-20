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
from server import run_dashboard_thread, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_REST = "https://fapi.binance.com"
BASE_WS   = "wss://fstream.binance.com"
API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET", "")

# ─────────────────────────────────────────
# CONFIG — reloaded live on every candle
# Change via dashboard UI, no redeploy needed
# ─────────────────────────────────────────
def cfg():
    return load_config()

# ─────────────────────────────────────────
# PRELOAD
# ─────────────────────────────────────────
def preload_candles(symbol, opens, highs, lows, closes, limit=500):
    log.info(f"Preloading {limit} candles for {symbol}...")
    try:
        r = requests.get(f"{BASE_REST}/fapi/v1/klines",
                         params={"symbol": symbol, "interval": "1m", "limit": limit})
        r.raise_for_status()
        opens.clear(); highs.clear(); lows.clear(); closes.clear()
        for c in r.json():
            opens.append(float(c[1])); highs.append(float(c[2]))
            lows.append(float(c[3]));  closes.append(float(c[4]))
        log.info(f"Preloaded {len(closes)} candles — ready!")
    except Exception as e:
        log.error(f"Preload failed: {e}")

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
    p  = {"timestamp": ts}; p["signature"] = _sign(p)
    r  = requests.get(f"{BASE_REST}/fapi/v2/balance", params=p, headers=_h())
    r.raise_for_status()
    for a in r.json():
        if a["asset"] == "USDT": return float(a["availableBalance"])
    return 0.0

def get_position(symbol):
    ts = int(time.time() * 1000)
    p  = {"symbol": symbol, "timestamp": ts}; p["signature"] = _sign(p)
    r  = requests.get(f"{BASE_REST}/fapi/v2/positionRisk", params=p, headers=_h())
    r.raise_for_status()
    for pos in r.json():
        if pos["symbol"] == symbol:
            amt = float(pos["positionAmt"])
            if amt != 0:
                entry = float(pos["entryPrice"]); pnl = float(pos["unRealizedProfit"])
                lev   = cfg()["LEVERAGE"]
                return {"side": "SHORT" if amt < 0 else "LONG", "qty": abs(amt),
                        "entry": entry, "pnl": pnl,
                        "pct": pnl / (abs(amt) * entry / lev) * 100}
    return None

def set_leverage(symbol, leverage):
    ts = int(time.time() * 1000)
    p  = {"symbol": symbol, "leverage": leverage, "timestamp": ts}; p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/leverage", params=p, headers=_h())

def get_precisions(symbol):
    r = requests.get(f"{BASE_REST}/fapi/v1/exchangeInfo")
    for s in r.json()["symbols"]:
        if s["symbol"] == symbol:
            qp   = s["quantityPrecision"]
            tick = "0.0001"
            for f in s.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    tick = f["tickSize"]
                    break
            tick_str  = tick.rstrip("0")
            tick_prec = len(tick_str.split(".")[-1]) if "." in tick_str else 0
            log.info(f"tick_size={tick} | price_prec={tick_prec}")
            return qp, tick_prec
    return 3, 4

def round_to_tick(price, tick_prec):
    """Round price to correct decimal places for Binance"""
    return round(price, tick_prec)

def place_protect_order(symbol, side, order_type, stop_price, pp, retries=3):
    """Place SL or TP with retries and full error logging"""
    for attempt in range(retries):
        try:
            ts = int(time.time() * 1000)
            p  = {
                "symbol":        symbol,
                "side":          side,
                "type":          order_type,
                "stopPrice":     f"{stop_price:.{pp}f}",
                "closePosition": "true",
                "workingType":   "MARK_PRICE",   # required by Binance futures
                "priceProtect":  "true",          # prevents bad fills
                "timeInForce":   "GTE_GTC",
                "timestamp":     ts
            }
            p["signature"] = _sign(p)
            r = requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h())
            if r.status_code == 200:
                log.info(f"{order_type} set @ {stop_price:.{pp}f}")
                return True
            else:
                log.error(f"{order_type} attempt {attempt+1} failed: {r.status_code} | {r.text}")
                time.sleep(1)
        except Exception as e:
            log.error(f"{order_type} attempt {attempt+1} exception: {e}")
            time.sleep(1)
    log.error(f"FAILED to place {order_type} after {retries} attempts!")
    return False

def place_order(symbol, side, qty, price, sl, tp2, qp, pp):
    cs  = "BUY"  if side == "SHORT" else "SELL"
    os_ = "SELL" if side == "SHORT" else "BUY"
    qs  = f"{qty:.{qp}f}"

    # Step 1: Market entry
    ts = int(time.time() * 1000)
    p  = {"symbol": symbol, "side": os_, "type": "MARKET", "quantity": qs, "timestamp": ts}
    p["signature"] = _sign(p)
    r  = requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h())
    if r.status_code != 200:
        log.error(f"ENTRY failed: {r.status_code} {r.text}")
        r.raise_for_status()
    log.info(f"ENTRY {side} {qs} @ ~{price:.{pp}f}")
    time.sleep(1)  # wait for position to register on Binance

    # Step 2: Stop Loss with retry
    sl_rounded = round_to_tick(sl, pp)
    log.info(f"Placing SL @ {sl_rounded:.{pp}f} (pp={pp})")
    place_protect_order(symbol, cs, "STOP_MARKET", sl_rounded, pp)
    time.sleep(0.5)

    # Step 3: Take Profit with retry
    tp2_rounded = round_to_tick(tp2, pp)
    log.info(f"Placing TP2 @ {tp2_rounded:.{pp}f} (pp={pp})")
    place_protect_order(symbol, cs, "TAKE_PROFIT_MARKET", tp2_rounded, pp)

def close_partial(symbol, side, qty, qp):
    cs   = "BUY" if side == "SHORT" else "SELL"
    half = f"{qty/2:.{qp}f}"
    ts   = int(time.time() * 1000)
    p    = {"symbol": symbol, "side": cs, "type": "MARKET",
            "quantity": half, "reduceOnly": "true", "timestamp": ts}
    p["signature"] = _sign(p)
    requests.post(f"{BASE_REST}/fapi/v1/order", params=p, headers=_h()).raise_for_status()
    log.info(f"PARTIAL CLOSE 50% — {half}")

def move_sl_breakeven(symbol, side, entry, pp):
    # Cancel ONLY the stop loss order — leave TP2 untouched
    try:
        ts = int(time.time() * 1000)
        p  = {"symbol": symbol, "timestamp": ts}; p["signature"] = _sign(p)
        r  = requests.get(f"{BASE_REST}/fapi/v1/openOrders", params=p, headers=_h())
        for o in r.json():
            if o.get("type") == "STOP_MARKET":
                ts2 = int(time.time() * 1000)
                cp  = {"symbol": symbol, "orderId": o["orderId"], "timestamp": ts2}
                cp["signature"] = _sign(cp)
                requests.delete(f"{BASE_REST}/fapi/v1/order", params=cp, headers=_h())
                log.info(f"Cancelled old SL order {o['orderId']}")
    except Exception as e:
        log.error(f"Cancel SL error: {e}")
    time.sleep(0.3)
    # Place new SL at entry price (breakeven)
    cs = "BUY" if side == "SHORT" else "SELL"
    place_protect_order(symbol, cs, "STOP_MARKET", round(entry, pp), pp)
    log.info(f"SL moved to BREAKEVEN @ {entry:.{pp}f}")

# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_ema(cl, period):
    if len(cl) < period: return 0.0
    k = 2 / (period + 1); v = sum(cl[:period]) / period
    for p in cl[period:]: v = p * k + v * (1 - k)
    return v

def calc_stoch(hi, lo, cl, kp=5, sk=3, dp=3):
    n = len(cl)
    if n < kp + sk + dp: return 50.0, 50.0
    rk = []
    for i in range(kp-1, n):
        h = max(hi[i-kp+1:i+1]); l = min(lo[i-kp+1:i+1])
        rk.append(50.0 if h==l else (cl[i]-l)/(h-l)*100)
    sm = [sum(rk[i-sk+1:i+1])/sk for i in range(sk-1, len(rk))]
    if len(sm) < dp: return sm[-1] if sm else 50.0, 50.0
    dv = [sum(sm[i-dp+1:i+1])/dp for i in range(dp-1, len(sm))]
    return sm[-1], dv[-1]

def check_signal(highs, lows, closes, side_mode):
    cl = list(closes); hi = list(highs); lo = list(lows)
    e20 = calc_ema(cl, 20); e50 = calc_ema(cl, 50)
    k, d   = calc_stoch(hi, lo, cl)
    kp, _  = calc_stoch(hi[:-1], lo[:-1], cl[:-1])
    price  = cl[-1]; high = hi[-1]
    below  = price < e20 and price < e50
    rej    = price < e20 and high >= e20 * 0.998
    cdn    = k < d and kp >= d and kp > 60
    sdrop  = k > 65 and k < kp and d > k
    above  = price > e20 and price > e50
    cup    = k > d and kp <= d and kp < 30
    if side_mode != "LONG_ONLY":
        if (below and cdn) or (rej and sdrop):
            log.info(f"SHORT | price={price:.4f} EMA20={e20:.4f} K={k:.1f} D={d:.1f}")
            return "SHORT"
    if side_mode != "SHORT_ONLY":
        if above and cup:
            log.info(f"LONG | price={price:.4f} EMA20={e20:.4f} K={k:.1f} D={d:.1f}")
            return "LONG"
    return None

# ─────────────────────────────────────────
# POSITION MONITOR
# ─────────────────────────────────────────
async def monitor_position(state, symbol, qp, pp):
    log.info(f"Monitor started for {symbol}")
    while state.get("in_trade"):
        await asyncio.sleep(5)
        try:
            pos = get_position(symbol)
            if not pos:
                log.info("Position closed — resetting state")
                state["in_trade"]    = False
                state["partial_done"] = False
                break
            log.info(f"PnL: {pos['pct']:.1f}% | entry={pos['entry']:.4f} | {pos['side']}")
            # Only do partial close + breakeven ONCE
            if pos["pct"] >= 50 and not state["partial_done"]:
                log.info("50% profit hit — closing half, moving SL to breakeven")
                state["partial_done"] = True   # set FIRST to prevent race condition
                close_partial(symbol, pos["side"], pos["qty"], qp)
                await asyncio.sleep(2)         # wait for partial to fill
                move_sl_breakeven(symbol, pos["side"], pos["entry"], pp)
        except Exception as e:
            log.error(f"Monitor error: {e}")

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
async def run_bot():
    log.info("Starting RIVER bot...")
    run_dashboard_thread()

    config     = cfg()
    symbol     = config["SYMBOL"]
    leverage   = config["LEVERAGE"]
    candle_lim = 500

    set_leverage(symbol, leverage)
    qp, pp = get_precisions(symbol)
    log.info(f"Symbol={symbol} | Leverage={leverage}x | qty_prec={qp} | price_prec={pp}")

    opens  = deque(maxlen=candle_lim)
    highs  = deque(maxlen=candle_lim)
    lows   = deque(maxlen=candle_lim)
    closes = deque(maxlen=candle_lim)

    preload_candles(symbol, opens, highs, lows, closes, candle_lim)

    # ── On startup: check if a position already exists ──
    # This prevents re-entering after a Railway restart
    existing = get_position(symbol)
    if existing:
        log.info(f"Existing position found on startup: {existing['side']} {existing['qty']} — resuming monitor")
        state = {"in_trade": True, "partial_done": False}
    else:
        state = {"in_trade": False, "partial_done": False}
    current_symbol = symbol

    while True:
        try:
            async with websockets.connect(
                f"{BASE_WS}/ws/{symbol.lower()}@kline_1m"
            ) as ws:
                log.info(f"WebSocket connected — watching {symbol}")
                async for msg in ws:
                    k = json.loads(msg)["k"]
                    if not k["x"]: continue

                    # Reload config on every candle — picks up UI changes instantly
                    config     = cfg()
                    new_symbol = config["SYMBOL"]
                    leverage   = config["LEVERAGE"]
                    side_mode  = config["SIDE_MODE"]

                    # If symbol changed via UI — restart with new coin
                    if new_symbol != current_symbol and not state["in_trade"]:
                        log.info(f"Symbol changed: {current_symbol} → {new_symbol}")
                        symbol = new_symbol
                        current_symbol = new_symbol
                        set_leverage(symbol, leverage)
                        qp, pp = get_precisions(symbol)
                        opens  = deque(maxlen=candle_lim)
                        highs  = deque(maxlen=candle_lim)
                        lows   = deque(maxlen=candle_lim)
                        closes = deque(maxlen=candle_lim)
                        preload_candles(symbol, opens, highs, lows, closes, candle_lim)
                        break  # reconnect WebSocket to new symbol

                    opens.append(float(k["o"])); highs.append(float(k["h"]))
                    lows.append(float(k["l"]));  closes.append(float(k["c"]))
                    log.info(f"Candle | C={k['c']} | bars={len(closes)} | {symbol}")

                    if state["in_trade"]: continue
                    if len(closes) < candle_lim: continue

                    sig = check_signal(highs, lows, closes, side_mode)
                    if not sig: continue

                    bal   = get_balance()
                    entry = float(k["c"])
                    rp    = config["RISK_PCT"]
                    sl_p  = config["SL_PCT"]
                    tp1_p = config["TP1_PCT"]
                    tp2_p = config["TP2_PCT"]

                    if sig == "SHORT":
                        sl  = round(entry*(1+sl_p),  pp)
                        tp1 = round(entry*(1-tp1_p), pp)
                        tp2 = round(entry*(1-tp2_p), pp)
                    else:
                        sl  = round(entry*(1-sl_p),  pp)
                        tp1 = round(entry*(1+tp1_p), pp)
                        tp2 = round(entry*(1+tp2_p), pp)

                    qty = round(bal * rp * leverage / entry, qp)
                    if qty <= 0: log.warning("Qty too small"); continue

                    log.info(f"Signal: {sig} | bal=${bal:.2f} | qty={qty} | SL={sl} | TP2={tp2}")
                    try:
                        place_order(symbol, sig, qty, entry, sl, tp2, qp, pp)
                        state["in_trade"] = True; state["partial_done"] = False

                        # Verify SL and TP were actually placed
                        await asyncio.sleep(2)
                        ts_check = int(time.time() * 1000)
                        p_check  = {"symbol": symbol, "timestamp": ts_check}
                        p_check["signature"] = _sign(p_check)
                        open_orders = requests.get(f"{BASE_REST}/fapi/v1/openOrders", params=p_check, headers=_h()).json()
                        has_sl  = any(o.get("type") == "STOP_MARKET"         for o in open_orders)
                        has_tp  = any(o.get("type") == "TAKE_PROFIT_MARKET"  for o in open_orders)
                        cs_side = "BUY" if sig == "SHORT" else "SELL"
                        if not has_sl:
                            log.warning("SL missing — retrying...")
                            place_protect_order(symbol, cs_side, "STOP_MARKET", sl, pp)
                        if not has_tp:
                            log.warning("TP2 missing — retrying...")
                            place_protect_order(symbol, cs_side, "TAKE_PROFIT_MARKET", tp2, pp)

                        asyncio.create_task(monitor_position(state, symbol, qp, pp))
                    except Exception as e:
                        log.error(f"Order error: {e}")

        except Exception as e:
            log.error(f"WS error: {e} — retry in 5s")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(run_bot())
