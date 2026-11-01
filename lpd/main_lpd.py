"""
main_lpd.py — اجراکننده اصلی پروژه LPD (سکوی پرتاب)
"""

import os
import sys
import time
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
    """فایل lpd_strategy.enc رو رمزگشایی و ایمپورت می‌کنه"""
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
        
    # ذخیره تو حافظه موقت سرور گیت‌هاب
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
# تلگرام
# ============================================================
def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("telegram credentials missing")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"telegram failed | {e}")
        return False

# ============================================================
# لیست سفید توبیت (فقط ارزهای اسپات USDT)
# ============================================================
def load_toobit_whitelist() -> set:
    url = "https://api.toobit.com/api/v1/exchangeInfo"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        symbols = set()
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            sym = s.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            if sym.endswith("ONUSDT") or sym.endswith("XUSDT") or sym.endswith("USDC"):
                continue
            symbols.add(sym)
        logger.info(f"toobit whitelist loaded | {len(symbols)} symbols")
        return symbols
    except Exception as e:
        logger.error(f"toobit whitelist failed | {e}")
        return set()

# ============================================================
# انتخاب ۱۰۰ ارز برتر (بین لیست سفید توبیت)
# ============================================================
def get_top_symbols(whitelist: set, limit: int = 100) -> list:
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        tickers = r.json()
        volumes = {}
        for t in tickers:
            sym = t.get("symbol", "")
            if sym in whitelist:
                try:
                    volumes[sym] = float(t.get("quoteVolume", 0))
                except (ValueError, TypeError):
                    volumes[sym] = 0.0
        sorted_syms = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
        top = [s for s, _ in sorted_syms[:limit]]
        logger.info(f"top symbols selected | {len(top)}")
        return top
    except Exception as e:
        logger.error(f"top symbols failed | {e}")
        return []

# ============================================================
# کندل‌ها (از میرور بایننس)
# ============================================================
def fetch_klines(symbol: str, interval: str, limit: int = 300) -> Optional[pd.DataFrame]:
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        df = pd.DataFrame(
            data,
            columns=[
                "time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore",
            ],
        )
        df = df[["time", "open", "high", "low", "close", "volume"]]
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        logger.warning(f"klines failed | {symbol} | {e}")
        return None

# ============================================================
# اردربوک: OKX / Bybit (پروکسی) / MEXC
# ============================================================
def fetch_order_book_okx(symbol: str) -> Optional[dict]:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    inst = f"{base}-USDT"
    url = "https://www.okx.com/api/v5/market/books"
    params = {"instId": inst, "sz": 100}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        js = r.json()
        raw = js.get("data", [])
        if not raw:
            return None
        res = raw[0]
        bids = [[float(p), float(q)] for p, q, *_ in res.get("bids", [])]
        asks = [[float(p), float(q)] for p, q, *_ in res.get("asks", [])]
        if bids or asks:
            logger.info(f"order book OK (okx) | {symbol}")
            return {"bids": bids, "asks": asks}
    except Exception:
        return None
    return None

def fetch_order_book_bybit_proxy(symbol: str) -> Optional[dict]:
    url = f"{BYBIT_PROXY_URL}/v5/market/orderbook"
    params = {"category": "linear", "symbol": symbol, "limit": 100}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        js = r.json()
        if js.get("retCode") != 0:
            return None
        res = js.get("result", {})
        bids = [[float(p), float(q)] for p, q in res.get("b", [])]
        asks = [[float(p), float(q)] for p, q in res.get("a", [])]
        if bids or asks:
            logger.info(f"order book OK (bybit proxy) | {symbol}")
            return {"bids": bids, "asks": asks}
    except Exception:
        return None
    return None

def fetch_order_book_mexc(symbol: str) -> Optional[dict]:
    url = "https://api.mexc.com/api/v3/depth"
    params = {"symbol": symbol, "limit": 100}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        js = r.json()
        bids = [[float(p), float(q)] for p, q in js.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in js.get("asks", [])]
        if bids or asks:
            logger.info(f"order book OK (mexc) | {symbol}")
            return {"bids": bids, "asks": asks}
    except Exception:
        return None
    return None

def fetch_order_book(symbol: str) -> Optional[dict]:
    for ex in ORDER_BOOK_EXCHANGES:
        if ex == "okx":
            ob = fetch_order_book_okx(symbol)
        elif ex == "bybit":
            ob = fetch_order_book_bybit_proxy(symbol)
        elif ex == "mexc":
            ob = fetch_order_book_mexc(symbol)
        else:
            ob = None
        if ob:
            return ob
    return None

# ============================================================
# تحلیل یک ارز
# ============================================================
def analyze_symbol(symbol: str, lpd_strategy) -> Optional[dict]:
    df_4h = fetch_klines(symbol, "4h", 300)
    df_1d = fetch_klines(symbol, "1d", 300)
    if df_4h is None or df_1d is None:
        return None
    current_price = float(df_4h["close"].iloc[-1])
    ob = fetch_order_book(symbol)
    if not ob:
        return None
    setup = lpd_strategy.evaluate_lpd_setup(df_4h, df_1d, ob, current_price)
    if setup:
        setup["symbol"] = symbol
        setup["current_price"] = current_price
    return setup

# ============================================================
# ساخت پیام سیگنال
# ============================================================
def format_signal(setup: dict) -> str:
    return (
        f"🚀🔥 <b>LPD Setup Ready</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"ارز: {setup['symbol']}\n"
        f"قیمت فعلی: {setup['current_price']}\n"
        f"قیمت ورود (OB High): {setup['entry_price']}\n"
        f"فاصله تا ورود: {setup['distance_to_entry']}%\n"
        f"🎯 هدف: {setup['target_price']}\n"
        f"📈 سود بالقوه: {setup['profit_pct']}%\n"
        f"🐋 قدرت خرید: {setup['bid_power']}x\n"
        f"⛽ سوخت: {setup['fuel_count']} دیوار خورد\n"
        f"⏱ تایم: {setup['trend']}\n"
    )

# ============================================================
# اجرای اصلی (یک چرخه)
# ============================================================
def main():
    logger.info("=" * 50)
    logger.info("LPD cycle started")
    logger.info("=" * 50)

    # ۱. رمزگشایی استراتژی
    lpd_strategy = load_lpd_strategy()

    # ۲. لیست سفید توبیت
    whitelist = load_toobit_whitelist()
    if not whitelist:
        logger.error("no whitelist — aborting")
        return

    # ۳. انتخاب ۱۰۰ ارز برتر
    symbols = get_top_symbols(whitelist, 100)
    if not symbols:
        logger.error("no symbols — aborting")
        return

    setups_found = 0
    for i, symbol in enumerate(symbols, 1):
        try:
            setup = analyze_symbol(symbol, lpd_strategy)
            if setup:
                setups_found += 1
                send_telegram(format_signal(setup))
                logger.info(f"LPD setup | {symbol} | profit={setup['profit_pct']}%")
                if setups_found >= MAX_FOCUS_LIST_SIZE:
                    break
        except Exception as e:
            logger.error(f"analyze {symbol} failed | {e}")

        if i % 20 == 0:
            logger.info(f"progress | {i}/{len(symbols)} | setups={setups_found}")

        time.sleep(0.2)

    logger.info("=" * 50)
    logger.info(f"LPD cycle finished | setups={setups_found}")
    logger.info("=" * 50)

    # پاکسازی فایل موقت استراتژی
    try:
        tmp_path = os.path.join(tempfile.gettempdir(), "lpd_strategy.py")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        pass

if __name__ == "__main__":
    main()