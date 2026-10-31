"""
capital_manager.py — مدیریت سرمایه و پله‌ها (قانون ۱۹ تا ۳۴)

توابع اصلی:
  - calculate_avg_price       — میانگین وزنی قیمت خرید (قانون ۳۱)
  - calculate_loss_usd        — ضرر فعلی به دلار
  - calculate_profit_pct      — درصد سود/ضرر نسبت به میانگین
  - should_enter_step2        — تصمیم‌گیری برای ورود پله دوم (قانون ۲۴)
  - check_max_loss            — بررسی سقف ضرر ۵ دلاری (قانون ۲۰، ۳۴)
  - check_profit_targets      — بررسی رسیدن به اهداف سود (قانون ۲۷)
  - calculate_step2_price     — محاسبه قیمت پله دوم (حداقل ۷.۵٪)
  - calculate_step2_max_price — محاسبه حداکثر قیمت پله دوم (۱۵٪)
  - calculate_qty             — حجم خرید بر اساس دلار
"""

import logging
from typing import Optional

from config import (
    STEP1_USD,
    STEP2_USD,
    MAX_LOSS_USD,
    STEP2_MIN_GAP_PCT,
    STEP2_MAX_GAP_PCT,
    PROFIT_TARGETS,
    FINAL_TARGET,
    AVERAGE_PRICE_DECIMALS,
    SILENT_UNTIL_PROFIT,
)

logger = logging.getLogger("FWP.CapitalManager")


# ============================================================
# ۱. میانگین وزنی قیمت خرید (قانون ۳۱)
# ============================================================
def calculate_avg_price(entries: list) -> Optional[float]:
    """
    محاسبه میانگین وزنی قیمت خرید.
    entries: [{"price": float, "usd": float, "qty": float}, ...]
    خروجی: میانگین قیمت (یا None اگه entries خالی باشه).
    """
    if not entries:
        return None

    total_qty = sum(e["qty"] for e in entries)
    if total_qty <= 0:
        return None

    total_cost = sum(e["price"] * e["qty"] for e in entries)
    avg = total_cost / total_qty
    return round(avg, AVERAGE_PRICE_DECIMALS)


# ============================================================
# ۲. ضرر فعلی به دلار
# ============================================================
def calculate_loss_usd(entries: list, current_price: float) -> float:
    """
    محاسبه ضرر فعلی به دلار.
    خروجی: مقدار ضرر (مثبت = در ضرر، منفی = در سود).
    """
    if not entries:
        return 0.0

    total_qty = sum(e["qty"] for e in entries)
    total_cost = sum(e["price"] * e["qty"] for e in entries)
    current_value = total_qty * current_price
    loss = total_cost - current_value
    return round(loss, 2)


# ============================================================
# ۳. درصد سود/ضرر نسبت به میانگین
# ============================================================
def calculate_profit_pct(avg_price: float, current_price: float) -> float:
    """درصد سود/ضرر نسبت به میانگین. خروجی: +0.10 (۱۰٪ سود) یا -0.05."""
    if avg_price <= 0:
        return 0.0
    return round((current_price - avg_price) / avg_price, 6)


# ============================================================
# ۴. بررسی سقف ضرر ۵ دلاری (قانون ۲۰، ۳۴)
# ============================================================
def check_max_loss(entries: list, current_price: float) -> bool:
    """آیا به سقف ضرر ۵ دلاری رسیدیم؟ True = باید خارج بشیم."""
    loss = calculate_loss_usd(entries, current_price)
    if loss >= MAX_LOSS_USD:
        logger.warning(f"MAX_LOSS hit | loss={loss:.2f} USD (cap={MAX_LOSS_USD})")
        return True
    return False


# ============================================================
# ۵. محاسبه قیمت پله دوم (حداقل ۷.۵٪ ریزش)
# ============================================================
def calculate_step2_price(step1_price: float) -> float:
    """قیمت پله دوم = قیمت پله اول × (1 - STEP2_MIN_GAP_PCT)"""
    return round(step1_price * (1 - STEP2_MIN_GAP_PCT), AVERAGE_PRICE_DECIMALS)


def calculate_step2_max_price(step1_price: float) -> float:
    """حداکثر قیمت پله دوم = قیمت پله اول × (1 - STEP2_MAX_GAP_PCT)"""
    return round(step1_price * (1 - STEP2_MAX_GAP_PCT), AVERAGE_PRICE_DECIMALS)


# ============================================================
# ۶. بررسی رسیدن به اهداف سود (قانون ۲۷)
# ============================================================
def check_profit_targets(avg_price: float, current_price: float) -> Optional[dict]:
    """
    بررسی اینکه آیا به یکی از اهداف سود (۱۰٪، ۲۰٪، ...) رسیدیم.
    بزرگ‌ترین target که بهش رسیدیم رو برمی‌گردونه.
    خروجی: {"target": 0.20, "profit_pct": 0.25, "is_final": False} یا None.
    """
    profit_pct = calculate_profit_pct(avg_price, current_price)
    if profit_pct < SILENT_UNTIL_PROFIT:
        return None  # هنوز به ۱۰٪ نرسیدیم، سکوت

    # پیدا کردن بزرگ‌ترین target که بهش رسیدیم
    reached_targets = [t for t in PROFIT_TARGETS if profit_pct >= t]
    if not reached_targets:
        return None

    highest_target = max(reached_targets)
    return {
        "target": highest_target,
        "profit_pct": profit_pct,
        "is_final": highest_target >= FINAL_TARGET,
    }


# ============================================================
# ۷. تصمیم‌گیری برای ورود پله دوم (قانون ۲۴)
# ============================================================
def should_enter_step2(
    step1_price: float,
    current_price: float,
    whale_confirmed: bool,
    already_in_step2: bool = False,
) -> bool:
    """
    آیا باید پله دوم رو بزنیم؟
    شرط: قیمت بین ۷.۵٪ تا ۱۵٪ از پله اول ریخته + نهنگ دوباره تایید کرده.
    """
    if already_in_step2:
        return False

    step2_min_price = calculate_step2_price(step1_price)      # ۷.۵٪ ریزش
    step2_max_price = calculate_step2_max_price(step1_price)  # ۱۵٪ ریزش

    # قیمت باید بین ۷.۵٪ تا ۱۵٪ ریزش کرده باشه
    if current_price > step2_min_price or current_price < step2_max_price:
        return False

    if not whale_confirmed:
        logger.info(f"step2 price reached ({current_price:.6f}) but whale not confirmed")
        return False

    logger.info(
        f"step2 ENTER | step1={step1_price:.6f} | current={current_price:.6f} | "
        f"range=[{step2_max_price:.6f}, {step2_min_price:.6f}]"
    )
    return True


# ============================================================
# ۸. محاسبه حجم خرید بر اساس دلار
# ============================================================
def calculate_qty(usd: float, price: float) -> float:
    """حجم خرید = دلار / قیمت"""
    if price <= 0:
        return 0.0
    return round(usd / price, AVERAGE_PRICE_DECIMALS)


# ============================================================
# تست پایانی
# ============================================================
if __name__ == "__main__":
    # ---- تست ۱: محاسبه میانگین ----
    entries = [
        {"price": 100.0, "usd": 24.0, "qty": 0.24},
        {"price": 92.5, "usd": 24.0, "qty": 0.25945945},
    ]
    avg = calculate_avg_price(entries)
    print(f"avg_price = {avg}")
    assert avg is not None and 95 < avg < 97, f"avg_price wrong: {avg}"

    # ---- تست ۲: ضرر ----
    loss = calculate_loss_usd(entries, current_price=91.0)
    print(f"loss at 91.0 = {loss}")
    assert loss > 0

    # ---- تست ۳: درصد سود ----
    profit = calculate_profit_pct(avg, 110.0)
    print(f"profit at 110.0 = {profit}")
    assert profit > 0.10

    # ---- تست ۴: سقف ضرر ----
    hit = check_max_loss(entries, current_price=85.0)
    print(f"max_loss hit at 85.0 = {hit}")
    assert hit is True

    # ---- تست ۵: قیمت پله دوم ----
    step2 = calculate_step2_price(100.0)
    step2_max = calculate_step2_max_price(100.0)
    print(f"step2 range = [{step2_max}, {step2}]")
    assert abs(step2 - 92.5) < 0.01
    assert abs(step2_max - 85.0) < 0.01

    # ---- تست ۶: هدف سود (باگ ۱ اصلاح شد) ----
    target = check_profit_targets(avg, 115.0)
    print(f"profit target at 115.0 = {target}")
    assert target is not None
    assert target["target"] == 0.10

    # ---- تست ۷: هدف سود بالاتر ----
    target2 = check_profit_targets(avg, 120.0)
    print(f"profit target at 120.0 = {target2}")
    assert target2 is not None and target2["target"] == 0.20

    # ---- تست ۸: پله دوم (باگ ۲) ----
    enter = should_enter_step2(100.0, 92.5, whale_confirmed=True)
    print(f"enter step2 at 92.5 = {enter}")
    assert enter is True

    enter2 = should_enter_step2(100.0, 90.0, whale_confirmed=True)
    print(f"enter step2 at 90.0 = {enter2}")
    assert enter2 is True

    enter3 = should_enter_step2(100.0, 84.0, whale_confirmed=True)
    print(f"enter step2 at 84.0 = {enter3}")
    assert enter3 is False

    enter4 = should_enter_step2(100.0, 95.0, whale_confirmed=True)
    print(f"enter step2 at 95.0 = {enter4}")
    assert enter4 is False

    print("\n✅ همه تست‌ها پاس شدند.")