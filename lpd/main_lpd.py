"""
main_lpd.py — اجراکننده اصلی پروژه LPD (سکوی پرتاب)
"""

import os
import sys
import time
import json
import base64
import hashlib
import logging
import importlib
import tempfile
from typing import Optional

import requests
import pandas as pd

# اضافه کردن روت پروژه به مسیر ایمپورت
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from lpd.lpd_config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    MASTER_KEY,
    BYBIT_PROXY_URL,
    ORDER_BOOK_EXCHANGES,
    MAX_FOCUS_LIST_SIZE,
    ALERT_DISTANCE_PCT,
    LPD_STATE_FILE,
)

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("LPD.Main")

# ============================================================
# 🔐 رمزگشایی استراتژی LPD
# ============================================================
def load_lpd_strategy():
    from cryptography.fernet import Fernet
    enc_path = os.path.join(ROOT_DIR, "lpd", "lpd_strategy.enc")
    
    if not os.path.exists(enc_path):
        logger.error(f"lpd_strategy.enc not found at {enc_path}")
        sys.exit(1)
    if not MASTER_KEY:
        logger.error("MASTER_KEY is empty")
        sys.exit(1)
        
    digest = hashlib.sha256(MASTER_KEY.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    fernet = Fernet(key)
    
    with open(enc_path, "rb") as f:
        ciphertext = f.read()
        
    try:
        plaintext = fernet.decrypt(ciphertext)
    except Exception as e:
        logger.error(f"decryption failed: {e}")
        sys.exit(1)
        
    tmp_dir = tempfile.gettempdir()
    tmp_path = os.path.join(tmp_dir, "lpd_strategy.py")
    
    with open(tmp_path, "wb") as f:
        f.write(plaintext)
        
    if tmp_dir not in sys.path:
        sys.path.insert(0, tmp_dir)
        
    sys.modules.pop("lpd_strategy", None)
    strategy_module = importlib.import_module("lpd_strategy")
    logger.info("lpd_strategy.enc decrypted and loaded successfully")
    return strategy_module

# ============================================================
# حافظه (State)
# ============================================================
def load_state() -> dict:
    if os.path.exists(LPD_STATE_FILE):
        try:
            with open(LPD_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"load state failed | {e}")
    return {"focus_list": {}}

def save_state(state: dict):
    try:
        with open(LPD_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info(f"state saved | {len(state.get('focus_list', {}))} symbols in focus list")
    except Exception as e:
        logger.error(f"save state failed | {e}")

# ============================================================
# تلگرام
# ============================================================
def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        return True
    except Exception:
        return False

# ============================================================
# لیست سفید توبیت
# ============================================================
def load_toobit_whitelist() -> set:
    url = "https://api.toobit.com/api/v1/exchangeInfo"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        symbols = set()
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING": continue
            sym = s.get("symbol", "")
            if not sym.endswith("USDT"): continue
            if sym.endswith("ONUSDT") or sym.endswith("XUSDT") or sym.endswith("USDC"): continue
            symbols.add(sym)
        return symbols
    except Exception as e:
        logger.error(f"toobit whitelist failed | {e}")
        return set()

# ============================================================
# انتخاب ۱۰۰ ارز برتر
# ============================================================
def get_top_symbols(whitelist: set, limit: int = 100) -> list:
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        tickers = r.json()
        volumes = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers if t.get("symbol") in whitelist}
        sorted_syms = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
        return [s for s, _ in sorted_syms[:limit]]
    except Exception as e:
        logger.error(f"top symbols failed | {e}")
        return []

# ============================================================
# دریافت کندل و اردربوک
# ============================================================
def fetch_klines(symbol: str, interval: str, limit: int = 300) -> Optional[pd.DataFrame]:
    url = "https://data-api.binance.vision/api/v3/klines"
    try:
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data: return None
        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"])
        df = df[["time", "open", "high", "low", "close", "volume"]]
        for c in ["open", "high", "low", "close", "volume"]: df[c] = df[c].astype(float)
        return df
    except Exception:
        return None

def fetch_order_book(symbol: str) -> Optional[dict]:
    # OKX
    try:
        base = symbol[:-4]
        r = requests.get("https://www.okx.com/api/v5/market/books", params={"instId": f"{base}-USDT", "sz": 100}, timeout=5)
        js = r.json()
        if js.get("data"):
            res = js["data"][0]
            return {"bids": [[float(p), float(q)] for p, q, *_ in res["bids"]], "asks": [[float(p), float(q)] for p, q, *_ in res["asks"]]}
    except Exception: pass
    
    # Bybit (Proxy)
    try:
        r = requests.get(f"{BYBIT_PROXY_URL}/v5/market/orderbook", params={"category": "linear", "symbol": symbol, "limit": 100}, timeout=5)
        js = r.json()
        if js.get("retCode") == 0 and js.get("result"):
            res = js["result"]
            return {"bids": [[float(p), float(q)] for p, q in res["b"]], "asks": [[float(p), float(q)] for p, q in res["a"]]}
    except Exception: pass
    
    # MEXC
    try:
        r = requests.get("https://api.mexc.com/api/v3/depth", params={"symbol": symbol, "limit": 100}, timeout=5)
        js = r.json()
        if js.get("bids"):
            return {"bids": [[float(p), float(q)] for p, q in js["bids"]], "asks": [[float(p), float(q)] for p, q in js["asks"]]}
    except Exception: pass
    
    return None

# ============================================================
# ۱. اسکن کامل (برای پر کردن لیست ۲۰ تایی)
# ============================================================
def run_full_scan(state: dict, lpd_strategy):
    logger.info(">>> Starting FULL SCAN to find setups")
    whitelist = load_toobit_whitelist()
    symbols = get_top_symbols(whitelist, 100)
    if not symbols: 
        logger.error("no symbols found")
        return

    focus_list = state.get("focus_list", {})
    new_setups = 0

    for i, symbol in enumerate(symbols, 1):
        try:
            df_4h = fetch_klines(symbol, "4h", 300)
            df_1d = fetch_klines(symbol, "1d", 300)
            if df_4h is None or df_1d is None: continue
            
            current_price = float(df_4h["close"].iloc[-1])
            ob = fetch_order_book(symbol)
            if not ob: continue
            
            setup = lpd_strategy.evaluate_lpd_setup(df_4h, df_1d, ob, current_price)
            if setup:
                setup["symbol"] = symbol
                setup["status"] = "waiting"
                setup["current_price"] = current_price
                focus_list[symbol] = setup
                new_setups += 1
                logger.info(f"⭐ setup found | {symbol} | dist={setup['distance_to_entry']}% | profit={setup['profit_pct']}%")
        except Exception as e:
            logger.error(f"scan {symbol} failed | {e}")

        # چاپ پیشرفت هر ۱۰ ارز
        if i % 10 == 0:
            logger.info(f"progress | {i}/{len(symbols)} | setups={new_setups}")

        time.sleep(0.1)

    # مرتب کردن و نگه داشتن ۲۰ تای برتر (نزدیک‌ترین به ورود)
    sorted_focus = sorted(focus_list.items(), key=lambda x: x[1]["distance_to_entry"])
    top_20 = dict(sorted_focus[:MAX_FOCUS_LIST_SIZE])
    
    state["focus_list"] = top_20
    save_state(state)
    logger.info(f"FULL SCAN finished | added {new_setups} new setups | total in focus: {len(top_20)}")

# ============================================================
# ۲. اسکن سریع (پایش لیست ۲۰ تایی)
# ============================================================
def run_active_scan(state: dict):
    logger.info(">>> Starting ACTIVE SCAN on Focus List")
    focus_list = state.get("focus_list", {})
    if not focus_list:
        logger.info("focus list is empty — skipping")
        return

    for symbol, setup in list(focus_list.items()):
        try:
            # گرفتن قیمت لحظه‌ای
            df = fetch_klines(symbol, "4h", 5)
            if df is None: continue
            current_price = float(df["close"].iloc[-1])
            entry_price = setup["entry_price"]
            
            # فاصله تا ورود
            dist = abs(current_price - entry_price) / entry_price
            
            # ۱. هشدار ۱٪
            if dist <= ALERT_DISTANCE_PCT and setup["status"] == "waiting":
                msg = (
                    f"⏳ <b>LPD Ready Alert</b>\n"
                    f"ارز: {symbol}\n"
                    f"قیمت فعلی: {current_price}\n"
                    f"قیمت ورود (OB): {entry_price}\n"
                    f"فاصله: {round(dist*100, 2)}%\n"
                    f"🎯 هدف: {setup['target_price']} (سود {setup['profit_pct']}%)"
                )
                send_telegram(msg)
                setup["status"] = "ready"
                logger.info(f"READY ALERT sent | {symbol} | dist={round(dist*100, 2)}%")
            
            # ۲. تریگر ورود (قیمت به OB رسید)
            elif current_price <= entry_price and setup["status"] == "ready":
                msg = (
                    f"🚀🔥 <b>LPD ENTRY SIGNAL</b>\n"
                    f"ارز: {symbol}\n"
                    f"قیمت ورود: {current_price}\n"
                    f"🎯 هدف: {setup['target_price']}\n"
                    f"📈 سود بالقوه: {setup['profit_pct']}%"
                )
                send_telegram(msg)
                setup["status"] = "entered"
                logger.info(f"ENTRY SIGNAL sent | {symbol}")

            # ۳. ریست (اگر قیمت خیلی از OB دور شد)
            if dist > 0.05: # بیشتر از ۵٪ فاصله
                del focus_list[symbol]
                logger.info(f"removed from focus list | {symbol} | too far from OB")

        except Exception as e:
            logger.error(f"active scan {symbol} failed | {e}")

    save_state(state)

# ============================================================
# تابع اصلی
# ============================================================
def main():
    lpd_strategy = load_lpd_strategy()
    state = load_state()
    
    if len(sys.argv) > 1 and sys.argv[1] == "active":
        run_active_scan(state)
    else:
        run_full_scan(state, lpd_strategy)

if __name__ == "__main__":
    main()
