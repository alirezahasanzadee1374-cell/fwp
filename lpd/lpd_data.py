def check_api_health() -> None:
    """هر ۱۰ دقیقه یک‌بار هر سه کلید رو چک می‌کنه و پیام سلامت می‌ده."""
    cache = _load_cache()
    now = time.time()
    if now - cache.get("health_last", 0) < _API_HEALTH_COOLDOWN:
        return
    cache["health_last"] = now

    pairs = [
        ("LPDtop", BYKARANTELI_KEY_LPD),
        ("FWP-Bot", BYKARANTELI_KEY_FWP),
        ("Backup", BYKARANTELI_KEY_BACKUP),
    ]

    results = {}
    for name, key in pairs:
        if not key:
            status = "missing"
        else:
            try:
                r = requests.get(
                    "https://bykaranteli.com/api/v1/usage",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10,
                )
                if r.status_code == 200:
                    status = "ok"
                elif r.status_code == 401:
                    status = "invalid_key"
                elif r.status_code == 429:
                    status = "rate_limit"
                else:
                    status = f"HTTP {r.status_code}"
            except Exception:
                status = "timeout"
        results[name] = status

        prev = cache["health_state"].get(name)
        cache["health_state"][name] = status

        if status != "ok":
            notify_api_failure(f"ByKaranteli ({name} key)", status)
        else:
            if prev is not None and prev != "ok":
                notify_api_recovered(f"ByKaranteli ({name} key)")

    # اگه هر سه سالم بودن → پیام سلامت
    if all(v == "ok" for v in results.values()):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        msg = (
            f"✅ <b>API Health OK (3/3)</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• LPDtop: ✅ ok\n"
            f"• FWP-Bot: ✅ ok\n"
            f"• Backup: ✅ ok\n"
            f"• زمان (UTC): {ts}"
        )
        _send_telegram_simple(msg)
        logger.info("API health OK | 3/3 keys ok")

    _save_cache(cache)
