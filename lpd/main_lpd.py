"""
main_lpd.py — اجراکننده اصلی LPD با لاگ‌های کامل
"""
import os, sys, time, json, logging
from typing import Optional
import requests
from lpd.lpd_config import *
from lpd.lpd_data import get_klines_binance, get_orderbook_okx_futures, get_whale_tape, get_liqmap
import lpd.lpd_strategy as strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("LPD.Main")

def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e: logger.error(f"❌ Telegram failed | {e}")

def load_state() -> dict:
    if os.path.exists(LPD_STATE_FILE):
        try: return json.load(open(LPD_STATE_FILE, "r"))
        except: pass
    return {"focus_list": {}, "scan_index": 0, "active_position": None, "last_pos_check": 0}

def save_state(state: dict):
    try: json.dump(state, open(LPD_STATE_FILE, "w"), indent=2)
    except Exception as e: logger.error(f"❌ Save state failed | {e}")

def get_top_symbols():
    try:
        # لیست توبیت
        r = requests.get("https://api.toobit.com/api/v1/exchangeInfo", timeout=15).json()
        wl = {s["symbol"] for s in r.get("symbols", []) if s.get("status") == "TRADING" and s["symbol"].endswith("USDT") and not s["symbol"].endswith("ONUSDT")}
        
        # تاپ ۵۰۰ بایننس
        t = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr", timeout=15).json()
        vol = {x["symbol"]: float(x.get("quoteVolume", 0)) for x in t if x.get("symbol") in wl}
        return [k for k, v in sorted(vol.items(), key=lambda i: i[1], reverse=True)[:500]]
    except Exception as e:
        logger.error(f"❌ Get top symbols failed | {e}")
        return []

# ==========================================
# ۱. اسکن کامل (هر ۳۰ دقیقه)
# ==========================================
def run_full_scan(state: dict):
    if len(state["focus_list"]) >= MAX_FOCUS_LIST_SIZE:
        logger.info(f"⏸ Focus list full ({MAX_FOCUS_LIST_SIZE}) - skipping scan")
        return
    if state.get("active_position"): return

    symbols = get_top_symbols()
    if not symbols: return
    
    idx = state.get("scan_index", 0)
    if idx >= len(symbols): idx = 0
    batch = symbols[idx:idx+100]
    
    logger.info(f"🔍 Scanning batch {idx}-{idx+len(batch)} of {len(symbols)}")
    new_count = 0

    for sym in batch:
        if sym in state["focus_list"]: continue
        df_4h = get_klines_binance(sym, "4h")
        ob = get_orderbook_okx_futures(sym)
        if df_4h is None or ob is None: continue
        
        setup = strategy.evaluate_lpd_setup(sym, df_4h, ob, float(df_4h["close"].iloc[-1]))
        if setup:
            setup["added_at"] = time.time()
            state["focus_list"][sym] = setup
            new_count += 1
            send_telegram(f"➕ <b>به لیست اضافه شد</b>\nارز: {sym}\nهدف: {setup['target_price']}\n📋 لیست: {len(state['focus_list'])}/20")
            if len(state["focus_list"]) >= MAX_FOCUS_LIST_SIZE:
                logger.info("🎯 Focus list full - stopping scan")
                break
        time.sleep(0.1)

    state["scan_index"] = idx + 100
    save_state(state)
    logger.info(f"✅ Scan finished | added {new_count} | total {len(state['focus_list'])}/20")

# ==========================================
# ۲. رصد لیست (هر ۱ دقیقه) و ۳. پوزیشن (هر ۳ دقیقه)
# ==========================================
def run_active_scan(state: dict):
    pos = state.get("active_position")
    
    # اگر پوزیشن فعال داریم: فقط هر ۳ دقیقه چک کن
    if pos:
        if time.time() - state.get("last_pos_check", 0) < POSITION_CHECK_INTERVAL:
            logger.info(f"⏸ Position throttled ({int(time.time() - state.get('last_pos_check',0))}s / {POSITION_CHECK_INTERVAL}s)")
            return
        
        logger.info(f"🎯 Monitoring Active Position: {pos['symbol']}")
        df = get_klines_binance(pos['symbol'], "4h", 5)
        if df is None: return
        price = float(df["close"].iloc[-1])
        pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100
        
        logger.info(f"📊 {pos['symbol']} | Price={price} | PnL={round(pnl, 2)}%")
        
        # خروج‌ها
        if price >= pos["target_price"]:
            send_telegram(f"🎯 <b>هدف زده شد!</b>\n{pos['symbol']} | سود: {round(pnl,2)}%")
            del state["focus_list"][pos["symbol"]]; state["active_position"] = None
        elif pnl <= -7:
            send_telegram(f"🚨 <b>حد ضرر ۷٪!</b>\n{pos['symbol']} | ضرر: {round(pnl,2)}%")
            del state["focus_list"][pos["symbol"]]; state["active_position"] = None
        elif pnl <= -3:
            ob = get_orderbook_okx_futures(pos['symbol'])
            if ob and sum(p*q for p,q in ob["asks"]) > sum(p*q for p,q in ob["bids"]) * 1.2:
                send_telegram(f"🚨 <b>خروج اضطراری!</b>\n{pos['symbol']} | ضرر: {round(pnl,2)}% (سلرها قوی شدن)")
                del state["focus_list"][pos["symbol"]]; state["active_position"] = None
            else:
                send_telegram(f"⚠️ <b>هشدار ضرر ۳٪</b>\n{pos['symbol']} | پوزیشن حفظ شد")
        
        if state.get("active_position"): state["last_pos_check"] = time.time()
        save_state(state)
        return

    # اگر پوزیشن نداریم: لیست ۲۰ تایی رو چک کن (اولویت با جدیدترین‌ها)
    if not state["focus_list"]:
        logger.info("📭 Focus list empty")
        return
        
    # مرتب‌سازی بر اساس زمان اضافه شدن (نزولی)
    sorted_focus = sorted(state["focus_list"].items(), key=lambda x: x[1]["added_at"], reverse=True)
    logger.info(f"👁 Active scan on {len(sorted_focus)} symbols (Newest first)")
    
    for sym, setup in sorted_focus:
        df = get_klines_binance(sym, "4h", 100)
        if df is None: continue
        price = float(df["close"].iloc[-1])
        
        if not strategy.detect_nouveau_trend(df):
            logger.info(f"➖ {sym} | Removed: trend turned down")
            del state["focus_list"][sym]
            send_telegram(f"➖ <b>حذف از لیست</b>\n{sym} | روند نزولی شد")
            continue

        dist = abs(price - setup["entry_price"]) / setup["entry_price"]
        if dist > 0.05:
            logger.info(f"➖ {sym} | Removed: too far from OB ({round(dist*100,2)}%)")
            del state["focus_list"][sym]
            send_telegram(f"➖ <b>حذف از لیست</b>\n{sym} | دور شدن از OB")
            continue

        # ورود
        if price <= setup["entry_price"]:
            logger.info(f"🎯 {sym} | Price reached OB ({price} <= {setup['entry_price']}) - Verifying entry...")
            ob = get_orderbook_okx_futures(sym)
            if not ob: continue
            
            # چک دیوار
            cur_wall = max([p*q for p,q in ob["bids"] if (setup["ob_low"]*0.99) <= p <= (setup["ob_high"]*1.01)], default=0)
            if cur_wall == 0:
                logger.info(f"🚫 {sym} | Entry rejected: Whale wall vanished (Spoof)")
                setup["status"] = "waiting"
                continue
            
            if cur_wall >= setup["initial_whale_usd"]:
                logger.info(f"⏳ {sym} | Whale wall intact (${cur_wall:.0f}) - waiting for fill")
                continue
            
            # چک Whale Tape
            whale = get_whale_tape(sym)
            if whale and whale["pressure"] == "sell":
                logger.info(f"🚫 {sym} | Entry rejected: Whales selling (${whale['sell_usd']:.0f})")
                continue
            
            # ورود موفق
            logger.info(f"🚀🚀 ENTRY SIGNAL | {sym} | ${cur_wall:.0f} filling started")
            send_telegram(f"🚀🔥 <b>LPD ENTRY</b>\n{sym} | Entry: {price}\nهدف: {setup['target_price']}\nسود: {setup['profit_pct']}%")
            state["active_position"] = {"symbol": sym, "entry_price": price, "target_price": setup["target_price"]}
            state["last_pos_check"] = time.time()
            save_state(state)
            return

    save_state(state)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    logger.info(f"▶️ Mode: {mode.upper()}")
    state = load_state()
    
    if mode == "active":
        run_active_scan(state)
    else:
        run_full_scan(state)

if __name__ == "__main__":
    main()
