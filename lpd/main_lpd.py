"""
main_lpd.py — اجراکننده اصلی پروژه LPD (سکوی پرتاب) - نسخه دیباگ کامل
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
    SWING_LOOKBACK_4H,
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
# ۱. اسکن کامل با دیباگ (X-Ray)
# ============================================================
def run_full_scan(state: dict, lpd_strategy):
    logger.info(">>> Starting FULL SCAN (DEBUG MODE)")
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
            if df_4h is None or df_1d is None: 
                logger.info(f"❌ {symbol} | data missing (4h or 1d)")
                continue
            
            current_price = float(df_4h["close"].iloc[-1])
            
            # --- شروع دیباگ ---
            logger.info(f"🔍 {symbol} | price={current_price}")
            
            # ۱. روند
            trend_4h = lpd_strategy.detect_nouveau_trend(df_4h)
            trend_1d = lpd_strategy.detect_nouveau_trend(df_1d)
            if not (trend_4h or trend_1d):
                logger.info(f"   ⛔ rejected: no uptrend (4h={trend_4h}, 1d={trend_1d})")
                continue
                
            # ۲. فیبوناچی
            swing_high, swing_low = lpd_strategy.find_swing_levels(df_4h, SWING_LOOKBACK_4H)
            is_discount, fib_05 = lpd_strategy.check_discount_zone(current_price, swing_high, swing_low)
            if not is_discount:
                logger.info(f"   ⛔ rejected: not in discount (price > fib 0.5 = {fib_05:.8f})")
                continue
                
            # ۳. اوردر بلاک
            ob = lpd_strategy.find_order_block(df_4h)
            if not ob:
                logger.info(f"   ⛔ rejected: no order block found")
                continue
            ob_high, ob_low = ob
            
            # ۴. اردربوک
            order_book = fetch_order_book(symbol)
            if not order_book:
                logger.info(f"   ⛔ rejected: order book unavailable")
                continue
            
            # ۵. تحلیل اردربوک
            ob_analysis = lpd_strategy.analyze_order_book(order_book, current_price, ob_low, ob_high)
            if not ob_analysis:
                logger.info(f"   ⛔ rejected: order book analysis failed")
                continue
                
            # جزئیات تحلیل
            logger.info(f"   📊 OB={ob_low:.8f}-{ob_high:.8f} | power={ob_analysis['bid_power']}x | whale={ob_analysis['has_whale']} | fuel={ob_analysis['retail_fuel_count']} | barrier={ob_analysis['heavy_barrier_count']}")
            
            if not ob_analysis["is_power_valid"]:
                logger.info(f"   ⛔ rejected: weak power ({ob_analysis['bid_power']}x < 1.5x)")
                continue
            if not ob_analysis["has_whale"]:
                logger.info(f"   ⛔ rejected: no whale wall (bid wall >= $100K)")
                continue
            if not ob_analysis["has_fuel"]:
                logger.info(f"   ⛔ rejected: not enough fuel (retail asks < 2)")
                continue
            if not ob_analysis["no_barrier"]:
                logger.info(f"   ⛔ rejected: heavy barrier in path")
                continue

            # ۶. سود و فضای پرواز
            profit_pct, is_valid_target = lpd_strategy.calculate_profit_target(current_price, swing_high)
            if not is_valid_target:
                logger.info(f"   ⛔ rejected: target invalid (profit={profit_pct}% < 10.5% or <7%)")
                continue

            # --- پایان دیباگ ---
            # همه چیز OK! ستاپ پیدا شد
            setup = {
                "symbol": symbol,
                "type": "LPD_Setup",
                "entry_price": ob_high,
                "target_price": swing_high,
                "profit_pct": profit_pct,
                "ob_low": ob_low,
                "ob_high": ob_high,
                "distance_to_entry": round(abs(current_price - ob_high) / ob_high * 100, 2),
                "bid_power": ob_analysis["bid_power"],
                "whale_walls": ob_analysis["whale_walls"],
                "fuel_count": ob_analysis["retail_fuel_count"],
                "trend": "4H" if trend_4h else "1D",
                "status": "waiting",
                "current_price": current_price,
            }
            focus_list[symbol] = setup
            new_setups += 1
            logger.info(f"⭐✅ SETUP FOUND | {symbol} | dist={setup['distance_to_entry']}% | profit={profit_pct}%")
            
        except Exception as e:
            logger.error(f"❌ scan {symbol} crashed | {e}")

        # چاپ پیشرفت هر ۱۰ ارز
        if i % 10 == 0:
            logger.info(f"=== progress | {i}/{len(symbols)} | setups={new_setups} ===")

        time.sleep(0.1)

    # مرتب کردن و نگه داشتن ۲۰ تای برتر
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
            df = fetch_klines(symbol, "4h", 5)
            if df is None: continue
            current_price = float(df["close"].iloc[-1])
            entry_price = setup["entry_price"]
            
            dist = abs(current_price - entry_price) / entry_price
            
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

            if dist > 0.05:
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
