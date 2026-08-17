"""可选：Google Calendar 同步（免费）。
正常需要 OAuth 拿到 access_token 并存到 GOOGLE_TOKEN_FILE 指定的 JSON 文件里。
没开启或没配置时，这个函数直接跳过，不影响主流程（依然免费）。
设置步骤见 README。"""
import os
import json
import requests


def _access_token():
    path = os.getenv("GOOGLE_TOKEN_FILE")
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f).get("access_token")
    except Exception:
        return None


def push_event(service_name, customer_name, customer_email, start_utc_iso, end_utc_iso, calendar_id="primary"):
    if os.getenv("GOOGLE_CALENDAR_ENABLED") != "true":
        return None
    token = _access_token()
    if not token:
        return None
    desc = f"顾客: {customer_name or 'N/A'}"
    if customer_email:
        desc += f"\n邮箱: {customer_email}"
    event = {
        "summary": f"{service_name} 预约",
        "description": desc,
        "start": {"dateTime": start_utc_iso},
        "end": {"dateTime": end_utc_iso},
    }
    try:
        r = requests.post(
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json=event,
            timeout=10,
        )
        if r.ok:
            return r.json().get("id")
    except Exception as e:
        print(f"[Calendar 同步失败] {e}")
    return None
