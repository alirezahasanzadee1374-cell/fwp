"""
platinum_manager.py — مدیریت صف سیگنال‌های پلاتینیوم (آنچین)
"""

import time
import logging
import requests
import pandas as pd
from typing import Optional

logger = logging.getLogger("FWP.PlatinumManager")

def _base_asset(symbol: str) -> str:
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if symbol.upper().endswith(quote):
            return symbol[: -len(quote)]
    return symbol

def get_blockchair_onchain_data(symbol: str, min_tx_usd: float = 1_000_000) -> bool:
    """
    بررسی تراکنش‌های بزرگ (بالای ۱ میلیون دلار) در ۱ ساعت اخیر.
    فقط برای BTC و ETH کار می‌کنه.
    """
    base = _base_asset(symbol).lower()
    
    if base == "btc":
        blockchain = "bitcoin"
    elif base == "eth":
        blockchain = "ethereum"
    else:
        return False 
        
    try:
        url = f"https://api.blockchair.com/{blockchain}/transactions"
        params = {
            "q": f"value_usd({min_tx_usd}..)",
            "s": "time(desc)",
            "limit": 10
        }
        
        # رعایت محدودیت ۱ درخواست در ثانیه
        time.sleep(1)
        
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        
        now_dt = pd.Timestamp.now(tz="UTC")
        for tx in data:
            tx_time = tx.get("time")
            if tx_time:
                tx_dt = pd.to_datetime(tx_time)
                if (now_dt - tx_dt).total_seconds() < 3600:
                    logger.info(f"onchain confirmed | {symbol} | tx_usd={tx.get('value_usd')}")
                    return True
                    
        logger.info(f"onchain NOT confirmed | {symbol} | no large tx in last 1h")
        return False
        
    except Exception as e:
        logger.warning(f"blockchair failed | {symbol} | {e}")
        return False

def add_to_queue(symbol: str, signal: dict, queue: dict) -> None:
    """اضافه کردن کاندید جدید به صف"""
    queue[symbol] = {
        "signal": signal,
        "last_checked": 0,
        "added_at": time.time()
    }
    logger.info(f"platinum queued | {symbol} | waiting for onchain")

def process_queue(queue: dict) -> Optional[dict]:
    """
    پردازش چرخشی صف (هر بار فقط ۱ ارز).
    خروجی: سیگنال تاییدشده (اگر پیدا شد) یا None
    """
    if not queue:
        return None

    symbol = next(iter(queue))
    
    logger.info(f"checking onchain | {symbol} | queue_size={len(queue)}")
    
    onchain_ok = get_blockchair_onchain_data(symbol)
    
    if onchain_ok:
        signal = queue[symbol]["signal"]
        del queue[symbol]
        logger.info(f"platinum CONFIRMED | {symbol}")
        return signal
    else:
        queue[symbol]["last_checked"] = time.time()
        queue[symbol] = queue.pop(symbol)
        logger.info(f"platinum pending | {symbol} | moved to end")
        return None
