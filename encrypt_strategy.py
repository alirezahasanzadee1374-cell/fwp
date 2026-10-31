"""
encrypt_strategy.py — رمزنگاری فایل strategy.py

⚠️ این فایل فقط تو لوکال اجرا می‌شه (قبل از push به گیت‌هاب).
   تو گیت‌هاب اکشنز، این فایل اجرا نمی‌شه.

مراحل:
  ۱. فایل strategy.py رو می‌خونه.
  ۲. رمز عبور رو با getpass از کاربر می‌گیره (یا از پیش‌فرض alireza021 استفاده می‌کنه).
  ۳. با Fernet رمزنگاری می‌کنه.
  ۴. به صورت strategy.enc ذخیره می‌کنه.
  ۵. به کاربر می‌گه که استراتژی رمزنگاری شد.
"""

import os
import sys
import base64
import hashlib
import getpass

from cryptography.fernet import Fernet


STRATEGY_FILE = "strategy.py"
ENCRYPTED_FILE = "strategy.enc"
DEFAULT_MASTER_KEY = "alireza021"


def derive_key(password: str) -> bytes:
    """
    تبدیل رمز عبور به کلید Fernet (۳۲ بایت base64).
    از SHA-256 استفاده می‌کنه.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def main():
    # ---- چک وجود فایل ----
    if not os.path.exists(STRATEGY_FILE):
        print(f"❌ فایل {STRATEGY_FILE} پیدا نشد.")
        print("   مطمئن شو تو همون پوشه‌ای که strategy.py هست، اجرا می‌کنی.")
        sys.exit(1)

    # ---- خوندن فایل ----
    with open(STRATEGY_FILE, "rb") as f:
        plaintext = f.read()

    print(f"✅ فایل {STRATEGY_FILE} خونده شد ({len(plaintext)} بایت).")

    # ---- گرفتن رمز (با Enter از پیش‌فرض استفاده می‌شه) ----
    password = getpass.getpass("🔑 رمز عبور (Master Key) رو وارد کن (Enter = پیش‌فرض alireza021): ")
    if not password:
        password = DEFAULT_MASTER_KEY
        print("⚠️ از رمز پیش‌فرض استفاده می‌شه.")

    if len(password) < 8:
        print("⚠️ هشدار: رمز کمتر از ۸ کاراکتره. امنیت پایینه.")
        confirm = input("ادامه بدی؟ (yes/no): ").strip().lower()
        if confirm != "yes":
            print("لغو شد.")
            sys.exit(0)

    # ---- رمزنگاری ----
    key = derive_key(password)
    fernet = Fernet(key)
    ciphertext = fernet.encrypt(plaintext)

    # ---- ذخیره ----
    with open(ENCRYPTED_FILE, "wb") as f:
        f.write(ciphertext)

    print(f"✅ فایل {ENCRYPTED_FILE} ساخته شد ({len(ciphertext)} بایت).")
    print()
    print("📋 قدم‌های بعدی:")
    print("   ۱. رمز عبور رو تو GitHub Secrets با اسم MASTER_KEY ذخیره کن.")
    print("   ۲. فایل strategy.py رو حذف کن یا تو .gitignore بذار.")
    print("   ۳. فایل strategy.enc رو به گیت‌هاب push کن.")
    print()
    print("⚠️ اگه رمز رو فراموش کنی، strategy.enc دیگه باز نمی‌شه!")


if __name__ == "__main__":
    main()