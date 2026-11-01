"""
main_lpd.py — اجراکننده اصلی LPD با لاگ‌های کامل
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

from lpd.lpd_config import *
# ایمپورت کردن توابع دیتا از فایل جدید
from lpd.lpd_data import get_klines_binance, get_orderbook_okx_futures, get_whale_tape, get_liqmap

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("LPD.Main")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# ============================================================
# 🔐 بارگذاری استراتژی رمزنگاری‌شده
# ============================================================
def load_lpd_strategy():
    from cryptography.fernet import Fernet
    enc_path = os.path.join(ROOT_DIR, "lpd", "lpd_strategy.enc")
    if not os.path.exists(enc_path):
        logger.error("❌ strategy.enc not found")
        sys.exit(1)
    if not MASTER_KEY:
        logger.error("❌ MASTER_KEY is empty")
        sys.exit(1)
    
    digest = hashlib.sha256(MASTER_KEY.encode("utf-8")).digest()
    fernet = Fernet(base64.urlsafe_b64encode(digest))
    try:
        plaintext = fernet.decrypt(open(enc_path, "rb").read())
    except Exception as e:
        logger.error(f"❌ decrypt failed: {e}")
        sys.exit(1)
        
    tmp_path = os.path.join(tempfile.gettempdir(), "lpd_strategy.py")
    with open(tmp_path, "wb") as f:
        f.write(plaintext)
    sys.path.insert(0, tempfile.gettempdir())
    sys.modules.pop("lpd_strategy", None)
    logger.info("✅ strategy decrypted and loaded")
    return importlib.import_module("lpd_strategy")

# ============================================================
# ابزارهای کمکی
# ============================================================
def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        logger.info("📨 Telegram sent")
    except Exception as e:
        logger.error(f"❌ Telegram failed | {e}")

def load_state() -> dict:
    if os.path.exists(LPD_STATE_FILE):
        try:
            return json.load(open(LPD_STATE_FILE, "r", encoding="utf-8"))
        except Exception: pass
    return {"focus_list": {}, "scan_index": 0, "active_position": None, "last_pos_check": 0}

def save_state(state: dict):
    try:
        with open(LPD_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"❌ Save state failed | {e}")

# ============================================================
# ۱. اسکن کامل (هر ۳۰ دقیقه)
# ============================================================
def run_full_scan(state, strategy):
    if len(state["focus_list"]) >= MAX_FOCUS_LIST_SIZE:
        logger.info(f"⏸ Focus full ({MAX_FOCUS_LIST_SIZE})")
        return
    if state.get("active_position"): return

    # گرفتن لیست ارزها (این تابع رو نگه داشتم تو مین، چون فقط برای شکاره)
    try:
        import requests
        r = requests.get("https://api.toobit.com/api/v1/exchangeInfo", timeout=15).json()
        wl = {s["symbol"] for s in r.get("symbols", []) if s.get("status") == "TRADING" and s["symbol"].endswith("USDT") and not s["symbol"].endswith("ONUSDT")}
        t = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr", timeout=15).json()
        vol = {x["symbol"]: float(x.get("quoteVolume", 0)) for x in t if x.get("symbol") in wl}
        symbols = [k for k, v in sorted(vol.items(), key=lambda i: i[1], reverse=True)[:500]]
    except Exception as e:
        logger.error(f"❌ get_top_symbols failed | {e}")
        return
        
    if not symbols: return
    
    idx = state.get("scan_index", 0)
    if idx >= len(symbols): idx = 0
    batch = symbols[idx:idx+100]
    
    logger.info(f"🔍 Scan batch {idx}-{idx+len(batch)} | focus={len(state['focus_list'])}")
    new_setups = 0

    for sym in batch:
        if sym in state["focus_list"]: continue
        
        df = get_klines_binance(sym)
        ob = get_orderbook_okx_futures(sym)
        if df is None or ob is None: continue
        
        setup = strategy.evaluate_lpd_setup(sym, df, ob, float(df["close"].iloc[-1]))
        if setup:
            setup["added_at"] = time.time()
            state["focus_list"][sym] = setup
            new_setups += 1
            send_telegram(f"➕ <b>به لیست اضافه شد</b>\nارز: {sym}\nهدف: {setup['target_price']}\n📋 لیست: {len(state['focus_list'])}/20")
            if len(state["focus_list"]) >= MAX_FOCUS_LIST_SIZE:
                logger.info("🎯 Focus full, stopping.")
                send_telegram(f"🎯 <b>لیست تمرکز پر شد!</b>\nاسکن متوقف شد.")
                break
        time.sleep(0.1)

    state["scan_index"] = idx + 100
    save_state(state)
    logger.info(f"✅ Scan done | +{new_setups} | total {len(state['focus_list'])}")

# ============================================================
# ۲. پایش (۱ دقیقه) + پوزیشن (۳ دقیقه)
# ============================================================
def run_active_scan(state, strategy):
    pos = state.get("active_position")
    
    # اگر پوزیشن داریم: ۳ دقیقه یکبار چک کن
    if pos:
        if time.time() - state.get("last_pos_check", 0) < POSITION_CHECK_INTERVAL:
            logger.info(f"⏸ Pos throttled ({int(time.time()-state.get('last_pos_check',0))}s)")
            return
        
        logger.info(f"🎯 MONITOR | {pos['symbol']}")
        df = get_klines_binance(pos['symbol'], "4h", 5)
        if df is None: return
        price = float(df["close"].iloc[-1])
        pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100
        
        logger.info(f"📊 {pos['symbol']} | P={price} | PnL={round(pnl,2)}%")
        
        # ۱. هدف
        if price >= pos["target_price"]:
            send_telegram(f"🎯 <b>TARGET HIT</b>\n{pos['symbol']} | سود: {round(pnl,2)}%")
            del state["focus_list"][pos['symbol']]; state["active_position"] = None
            
        # ۲. سقف ضرر ۷ درصد
        elif pnl <= -7:
            send_telegram(f"🚨 <b>HARD STOP</b>\n{pos['symbol']} | ضرر: {round(pnl,2)}%")
            del state["focus_list"][pos['symbol']]; state["active_position"] = None
            
        # ۳. هشدار ۳ درصد + زور سلرها
        elif pnl <= -3:
            ob = get_orderbook_okx_futures(pos['symbol'])
            if ob and sum(p*q for p,q in ob["asks"]) > sum(p*q for p,q in ob["bids"]) * 1.2:
                send_telegram(f"🚨 <b>Emergency Exit</b>\n{pos['symbol']} | ضرر: {round(pnl,2)}% (فشار فروش)")
                del state["focus_list"][pos['symbol']]; state["active_position"] = None
                
        # ۴. سوخت شورت‌ها تموم شد (از دیتای جدید)
        else:
            liq = get_liqmap(pos['symbol'])
            if liq and liq.get("fuel_exhausted"):
                send_telegram(f"⛽ <b>سوخت تموم شد</b>\n{pos['symbol']} | سود: {round(pnl,2)}%\n✅ خارج شو!")
                del state["focus_list"][pos['symbol']]; state["active_position"] = None

        if state.get("active_position"): state["last_pos_check"] = time.time()
        save_state(state)
        return

    # اگر پوزیشن نداریم: لیست رو چک کن (جدیدترین‌ها اول)
    if not state["focus_list"]: return
    
    sorted_focus = sorted(state["focus_list"].items(), key=lambda x: x[1].get("added_at", 0), reverse=True)
    for sym, setup in sorted_focus:
        df = get_klines_binance(sym, "4h", 100)
        if df is None: continue
        price = float(df["close"].iloc[-1])
        
        if not strategy.detect_nouveau_trend(df):
            logger.info(f"➖ {sym} | Trend down")
            del state["focus_list"][sym]
            send_telegram(f"➖ <b>حذف از لیست</b>\n{sym} | روند نزولی شد")
            continue

        dist = abs(price - setup["entry_price"]) / setup["entry_price"]
        if dist > 0.05:
            logger.info(f"➖ {sym} | Too far ({round(dist*100,2)}%)")
            del state["focus_list"][sym]
            send_telegram(f"➖ <b>حذف از لیست</b>\n{sym} | دور شدن از OB")
            continue

        # ورود
        if price <= setup["entry_price"]:
            logger.info(f"🎯 {sym} | Reached OB! Verifying...")
            ob = get_orderbook_okx_futures(sym)
            if not ob: continue
            
            cur_wall = max([p*q for p,q in ob["bids"] if (setup["ob_low"]*0.99) <= p <= (setup["ob_high"]*1.01)], default=0)
            if cur_wall == 0:
                logger.info(f"🚫 {sym} | Wall vanished (Spoof)")
                setup["status"] = "waiting"
                continue
                
            if cur_wall >= setup["initial_whale_usd"]:
                logger.info(f"⏳ {sym} | Wall intact (${cur_wall:.0f}), waiting for fill...")
                continue
            
            whale = get_whale_tape(sym)
            if whale and whale.get("pressure") == "sell":
                logger.info(f"🚫 {sym} | Whales selling")
                continue
            
            logger.info(f"🚀🚀 ENTRY | {sym} | Filling started (${cur_wall:.0f})")
            send_telegram(f"🚀🔥 <b>LPD ENTRY</b>\n{sym}\nورود: {price}\nهدف: {setup['target_price']}\n📈 سود: {setup['profit_pct']}%")
            state["active_position"] = {"symbol": sym, "entry_price": price, "target_price": setup["target_price"]}
            state["last_pos_check"] = time.time()
            save_state(state)
            return

    save_state(state)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    logger.info(f"▶️ LPD {mode.upper()} started")
    strategy = load_lpd_strategy()
    state = load_state()
    
    if mode == "active": run_active_scan(state, strategy)
    else: run_full_scan(state, strategy)

if __name__ == "__main__":
    main()
