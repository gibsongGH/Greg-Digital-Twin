import os
from datetime import datetime, timedelta, timezone

import httpx

CAL_API_BASE = "https://api.cal.com/v2"
SLOTS_API_VERSION = "2024-09-04"
BOOKINGS_API_VERSION = "2026-02-25"
DEFAULT_LOOKAHEAD_DAYS = 14
DEFAULT_TIMEOUT = 15.0


class CalComError(Exception):
    pass


def _api_key() -> str:
    key = os.getenv("CAL_API_KEY")
    if not key:
        raise CalComError("CAL_API_KEY is not set in environment.")
    return key


def _event_type_id() -> int:
    raw = os.getenv("CAL_EVENT_TYPE_ID")
    if not raw:
        raise CalComError("CAL_EVENT_TYPE_ID is not set in environment.")
    return int(raw)


def _username() -> str:
    return os.getenv("CAL_USERNAME", "")


def _event_slug() -> str:
    return os.getenv("CAL_EVENT_SLUG", "")


def public_booking_url() -> str:
    u, s = _username(), _event_slug()
    if u and s:
        return f"https://cal.com/{u}/{s}"
    return "https://cal.com"


def _auth_headers(api_version: str) -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "cal-api-version": api_version,
        "Content-Type": "application/json",
    }


def _evenly_spaced(items: list, k: int) -> list:
    """Return k items spread evenly across `items`, preserving order and
    always including the first and last element.

    Cal.com lists each day's slots earliest-first, so an even sample surfaces
    a morning -> afternoon range rather than just the opening slots.
    """
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    if k == 1:
        return [items[0]]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def list_available_slots(
    days_ahead: int = DEFAULT_LOOKAHEAD_DAYS,
    timezone_name: str = "UTC",
    max_slots: int = 10,
) -> list[dict]:
    """Fetch available slots for the configured event type via Cal.com v2 API.
    Returns a flat list of {"start": ISO8601 str, "day": "YYYY-MM-DD"}.
    """
    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    params = {
        "start": start,
        "end": end,
        "eventTypeId": _event_type_id(),
        "timeZone": timezone_name,
    }

    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.get(
            f"{CAL_API_BASE}/slots",
            params=params,
            headers=_auth_headers(SLOTS_API_VERSION),
        )

    if resp.status_code != 200:
        raise CalComError(f"Cal.com GET /v2/slots returned {resp.status_code}: {resp.text}")

    body = resp.json()
    slots_by_day = body.get("data", {})

    # Don't just take the first `max_slots` chronologically — that fills the
    # budget with the earliest (morning) slots and hides the afternoon. Take
    # an evenly spaced sample within each day so both halves of the day are
    # represented, then spread the budget across the upcoming days.
    days = [
        (day, [e["start"] for e in entries if e.get("start")])
        for day, entries in sorted(slots_by_day.items())
    ]
    days = [(day, starts) for day, starts in days if starts]
    if not days:
        return []

    per_day = max(3, max_slots // len(days))

    flat = []
    for day, starts in days:
        for start_time in _evenly_spaced(starts, per_day):
            flat.append({"start": start_time, "day": day})
            if len(flat) >= max_slots:
                return flat
    return flat


def create_booking(
    start_iso: str,
    invitee_name: str,
    invitee_email: str,
    timezone_name: str = "UTC",
    topic: str = "",
) -> dict:
    """Book the configured event type at start_iso via Cal.com v2 API.
    Returns a dict with {id, uid, status, start, end, location, public_url}.
    """
    payload = {
        "start": start_iso,
        "eventTypeId": _event_type_id(),
        "attendee": {
            "name": invitee_name,
            "email": invitee_email,
            "timeZone": timezone_name,
        },
        "metadata": {"source": "digital-twin-bot"},
    }
    if topic:
        payload["bookingFieldsResponses"] = {"notes": topic}

    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.post(
            f"{CAL_API_BASE}/bookings",
            json=payload,
            headers=_auth_headers(BOOKINGS_API_VERSION),
        )

    if resp.status_code not in (200, 201):
        raise CalComError(f"Cal.com POST /v2/bookings returned {resp.status_code}: {resp.text}")

    body = resp.json()
    booking = body.get("data", body)
    return {
        "id": booking.get("id"),
        "uid": booking.get("uid"),
        "status": booking.get("status", "accepted"),
        "start": booking.get("start", start_iso),
        "end": booking.get("end"),
        "location": booking.get("location"),
        "public_url": public_booking_url(),
    }