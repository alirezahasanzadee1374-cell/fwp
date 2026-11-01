"""
platinum_manager.py — دریافت وضعیت آنچین بیت‌کوین (کش ۳۰ دقیقه‌ای)
"""

import time
import logging
import requests
import pandas as pd

logger = logging.getLogger("FWP.OnchainManager")

_BTC_SENTIMENT_CACHE = {
    "sentiment": "خنثی ➖",
    "fetched_at": 0.0
}

def get_btc_onchain_sentiment() -> str:
    """
    بررسی وضعیت آنچین بیت‌کوین (صعودی/نزولی/خنثی) با کش ۳۰ دقیقه‌ای.
    """
    global _BTC_SENTIMENT_CACHE
    now = time.time()
    
    # اگه کش معتبره (کمتر از ۳۰ دقیقه گذشته)، همون رو برگردون
    if (now - _BTC_SENTIMENT_CACHE["fetched_at"]) < 1800:
        return _BTC_SENTIMENT_CACHE["sentiment"]

    # ۱. گرفتن تغییر قیمت ۱ ساعته بیت‌کوین از بایننس
    price_change_1h = 0.0
    try:
        url = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
        if len(data) >= 2:
            prev_close = float(data[-2][4])
            last_close = float(data[-1][4])
            if prev_close > 0:
                price_change_1h = (last_close - prev_close) / prev_close
    except Exception as e:
        logger.warning(f"btc price check failed | {e}")

    # ۲. گرفتن تعداد تراکنش‌های بزرگ بیت‌کوین از Blockchair
    large_tx_count = 0
    try:
        time.sleep(1)  # رعایت محدودیت Blockchair
        url = "https://api.blockchair.com/bitcoin/transactions"
        params = {"q": "value_usd(1000000..)", "s": "time(desc)", "limit": 20}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        txs = r.json().get("data", [])
        
        # چک می‌کنیم تو ۱ ساعت اخیر چند تا تراکنش بزرگ بوده
        now_dt = pd.Timestamp.now(tz="UTC")
        for tx in txs:
            tx_time = tx.get("time")
            if tx_time:
                tx_dt = pd.to_datetime(tx_time)
                if (now_dt - tx_dt).total_seconds() < 3600:
                    large_tx_count += 1
    except Exception as e:
        logger.warning(f"btc onchain check failed | {e}")

    # ۳. تصمیم‌گیری نهایی
    sentiment = "خنثی ➖"
    if large_tx_count >= 5:  # اگه حداقل ۵ تراکنش بزرگ تو ۱ ساعت اخیر بود
        if price_change_1h > 0.005:
            sentiment = "صعودی 📈 (فعالیت نهنگ‌ها)"
        elif price_change_1h < -0.005:
            sentiment = "نزولی 📉 (فشار فروش نهنگ‌ها)"
    
    _BTC_SENTIMENT_CACHE["sentiment"] = sentiment
    _BTC_SENTIMENT_CACHE["fetched_at"] = now
    logger.info(f"btc sentiment updated | {sentiment} | large_tx={large_tx_count} | change={price_change_1h*100:.2f}%")
    
    return sentiment
