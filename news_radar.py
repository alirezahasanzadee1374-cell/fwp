"""
news_radar.py — رادار اخبار FWP

توابع:
  - fetch_all_news       — دریافت اخبار از همه منابع
  - get_news_for_symbol  — اخبار مربوط به یه ارز خاص
  - is_blackout_time     — چک کردن زمان‌های پرخبر (اختیاری)
"""

import logging

from data_fetcher import get_news_rss

logger = logging.getLogger("FWP.NewsRadar")


# ============================================================
# ۱. دریافت همه اخبار
# ============================================================
def fetch_all_news(max_per_feed: int = 20) -> list:
    """دریافت اخبار از همه منابع RSS."""
    try:
        news = get_news_rss(max_per_feed=max_per_feed)
        logger.info(f"fetch_all_news | {len(news)} items")
        return news
    except Exception as e:
        logger.error(f"fetch_all_news failed | {e}")
        return []


# ============================================================
# ۲. اخبار مربوط به یک ارز خاص
# ============================================================
def get_news_for_symbol(news_list: list, symbol: str) -> list:
    """فیلتر اخبار برای یه ارز خاص (بر اساس اسم پایه)."""
    if not news_list:
        return []

    base = symbol.replace("USDT", "").upper()

    symbol_aliases = {
        "BTC": ["btc", "bitcoin"],
        "ETH": ["eth", "ethereum"],
        "SOL": ["sol", "solana"],
        "XRP": ["xrp", "ripple"],
        "ADA": ["ada", "cardano"],
        "DOGE": ["doge", "dogecoin"],
        "BNB": ["bnb", "binance coin"],
        "LINK": ["link", "chainlink"],
        "AVAX": ["avax", "avalanche"],
        "MATIC": ["matic", "polygon"],
        "DOT": ["dot", "polkadot"],
    }

    aliases = symbol_aliases.get(base, [base.lower()])

    filtered = []
    for news in news_list:
        title_lower = news.get("title", "").lower()
        if any(alias in title_lower for alias in aliases):
            filtered.append(news)

    logger.info(f"get_news_for_symbol | {symbol} | {len(filtered)} relevant")
    return filtered


# ============================================================
# ۳. زمان‌های پرخبر (اختیاری)
# ============================================================
def is_blackout_time() -> bool:
    """آیا الان تو یه بازه پرخبر هستیم؟ (فعلاً False)"""
    return False


# ============================================================
# تست پایانی
# ============================================================
if __name__ == "__main__":
    print("=== تست news_radar ===")

    news = fetch_all_news()
    print(f"fetch_all_news: {len(news)} items")
    if news:
        print(f"  sample: {news[0]['title'][:80]}")

    btc_news = get_news_for_symbol(news, "BTCUSDT")
    print(f"get_news_for_symbol(BTCUSDT): {len(btc_news)} items")

    print(f"is_blackout_time: {is_blackout_time()}")

    print("=== تست تمام شد ===")