import os
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode

API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_SECRET", "")
BASE       = "https://fapi.binance.com"
SYMBOL     = "RIVERUSDT"

def sign(params):
    q = urlencode(params)
    return hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

def h():
    return {"X-MBX-APIKEY": API_KEY}

def test():
    print("=== STEP 1: Get exchange info ===")
    r = requests.get(f"{BASE}/fapi/v1/exchangeInfo")
    for s in r.json()["symbols"]:
        if s["symbol"] == SYMBOL:
            for f in s.get("filters", []):
                print(f"  {f['filterType']}: {f}")
            break

    print("\n=== STEP 2: Get current price ===")
    r = requests.get(f"{BASE}/fapi/v1/ticker/price", params={"symbol": SYMBOL})
    price = float(r.json()["price"])
    print(f"  Current price: {price}")

    print("\n=== STEP 3: Get open position ===")
    ts = int(time.time() * 1000)
    p  = {"symbol": SYMBOL, "timestamp": ts}
    p["signature"] = sign(p)
    r  = requests.get(f"{BASE}/fapi/v2/positionRisk", params=p, headers=h())
    pos = None
    for x in r.json():
        if x["symbol"] == SYMBOL and float(x["positionAmt"]) != 0:
            pos = x
            print(f"  Position: {x['positionAmt']} @ {x['entryPrice']}")
    if not pos:
        print("  No open position")

    print("\n=== STEP 4: Get open orders ===")
    ts = int(time.time() * 1000)
    p  = {"symbol": SYMBOL, "timestamp": ts}
    p["signature"] = sign(p)
    r  = requests.get(f"{BASE}/fapi/v1/openOrders", params=p, headers=h())
    orders = r.json()
    if orders:
        for o in orders:
            print(f"  Order: {o['type']} side={o['side']} stopPrice={o.get('stopPrice')} status={o['status']}")
    else:
        print("  No open orders")

    print("\n=== STEP 5: Try placing STOP_MARKET (CONTRACT price) ===")
    sl_price = round(price * 1.008, 3)  # 0.8% above for short
    ts = int(time.time() * 1000)
    p  = {
        "symbol":        SYMBOL,
        "side":          "BUY",
        "type":          "STOP_MARKET",
        "stopPrice":     f"{sl_price:.3f}",
        "closePosition": "true",
        "workingType":   "CONTRACT_PRICE",
        "timeInForce":   "GTE_GTC",
        "timestamp":     ts
    }
    p["signature"] = sign(p)
    r = requests.post(f"{BASE}/fapi/v1/order", params=p, headers=h())
    print(f"  CONTRACT_PRICE result: {r.status_code} | {r.text[:300]}")

    print("\n=== STEP 6: Try placing STOP_MARKET (MARK_PRICE) ===")
    ts = int(time.time() * 1000)
    p  = {
        "symbol":        SYMBOL,
        "side":          "BUY",
        "type":          "STOP_MARKET",
        "stopPrice":     f"{sl_price:.3f}",
        "closePosition": "true",
        "workingType":   "MARK_PRICE",
        "timeInForce":   "GTE_GTC",
        "timestamp":     ts
    }
    p["signature"] = sign(p)
    r = requests.post(f"{BASE}/fapi/v1/order", params=p, headers=h())
    print(f"  MARK_PRICE result: {r.status_code} | {r.text[:300]}")

    print("\n=== STEP 7: Try TAKE_PROFIT_MARKET ===")
    tp_price = round(price * 0.97, 3)  # 3% below for short
    ts = int(time.time() * 1000)
    p  = {
        "symbol":        SYMBOL,
        "side":          "BUY",
        "type":          "TAKE_PROFIT_MARKET",
        "stopPrice":     f"{tp_price:.3f}",
        "closePosition": "true",
        "workingType":   "CONTRACT_PRICE",
        "timeInForce":   "GTE_GTC",
        "timestamp":     ts
    }
    p["signature"] = sign(p)
    r = requests.post(f"{BASE}/fapi/v1/order", params=p, headers=h())
    print(f"  TP CONTRACT_PRICE result: {r.status_code} | {r.text[:300]}")

if __name__ == "__main__":
    test()
