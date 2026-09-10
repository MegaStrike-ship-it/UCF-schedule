#!/usr/bin/env python3
"""
Merge a hand-maintained baseline class schedule with live data from Canvas.

Reads   : data/baseline.json, data/overrides.json, data/schedule.json (previous run)
Writes  : data/schedule.json, data/changes.json, schedule.ics

Canvas is authoritative where it has data. Everything Canvas does not model
(most recurring room assignments) falls back to the baseline. See README.

Env:
  CANVAS_BASE_URL  default https://webcourses.ucf.edu
  CANVAS_TOKEN     Canvas personal access token. If absent, the script still
                   runs and produces a baseline-only schedule.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://webcourses.ucf.edu").rstrip("/")
TOKEN = os.environ.get("CANVAS_TOKEN", "").strip()

# Announcement text that usually means a meeting moved, or is not happening.
ALERT_PATTERNS = [
    (r"\bcancel(l?ed|ling|lation)?\b", "cancellation"),
    (r"\bno class\b", "cancellation"),
    (r"\bnot meet(ing)?\b", "cancellation"),
    (r"\broom change\b", "room"),
    (r"\brelocat(ed|ing|ion)\b", "room"),
    (r"\bmov(ed|ing) to\b", "room"),
    (r"\bmeet in\b", "room"),
    (r"\bnew (room|location)\b", "room"),
    (r"\b(zoom|online|remote|virtual)\b", "modality"),
    (r"\btime change\b", "time"),
    (r"\brescheduled?\b", "time"),
]

DAY_INDEX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# WMO weather codes, collapsed to what actually happens in central Florida.
WMO = {
    0: ("Clear", "sun"),            1: ("Mostly clear", "sun"),
    2: ("Partly cloudy", "cloud"),  3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),             48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"), 53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    61: ("Light rain", "rain"),     63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    71: ("Light snow", "snow"),     73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
    80: ("Showers", "rain"),        81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    95: ("Thunderstorms", "storm"),
    96: ("Thunderstorms", "storm"), 99: ("Thunderstorms", "storm"),
}


def wmo(code):
    return WMO.get(code, ("--", "cloud"))


# ---------------------------------------------------------------- Canvas HTTP

class Canvas:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token
        self.ok = bool(token)
        self.errors: list[str] = []

    def get(self, path: str, **params) -> list | dict | None:
        """GET a Canvas endpoint, following Link-header pagination."""
        if not self.ok:
            return None

        params.setdefault("per_page", 100)
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{query}" if query else url

        collected: list = []
        pages = 0
        while url and pages < 25:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    link = resp.headers.get("Link", "")
            except urllib.error.HTTPError as exc:
                self.errors.append(f"{path} -> HTTP {exc.code}")
                return None
            except Exception as exc:  # noqa: BLE001 - network is best-effort
                self.errors.append(f"{path} -> {type(exc).__name__}: {exc}")
                return None

            if not isinstance(body, list):
                return body
            collected.extend(body)

            url = None
            for part in link.split(","):
                m = re.search(r'<([^>]+)>;\s*rel="next"', part)
                if m:
                    url = m.group(1)
                    break
            pages += 1

        return collected


# ------------------------------------------------------------------- helpers

def norm_code(text: str | None) -> str | None:
    """Pull a UCF course code (e.g. 'PHY 2048L') out of arbitrary Canvas text."""
    if not text:
        return None
    m = re.search(r"\b([A-Z]{3})\s?-?\s?(\d{4})([A-Z]?)\b", text.upper())
    if not m:
        return None
    return f"{m.group(1)} {m.group(2)}{m.group(3)}"


def to_local(iso: str | None, tz: ZoneInfo) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = urllib.parse.unquote(text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"warn: {path.name} is not valid JSON, ignoring", file=sys.stderr)
        return default


# --------------------------------------------------------- outside services
# Everything below is best-effort. None of it may break the schedule, so each
# fetch swallows its own failures and the caller carries on without it.

USER_AGENT = "ucf-schedule/1.0 (personal class schedule)"


def fetch_json(url: str, timeout: int = 30):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"warn: {url.split('?')[0]} unavailable "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def fetch_air_quality(location: dict, tz_name: str) -> dict | None:
    """US AQI for campus. Mostly unremarkable in Orlando, but it is the number
    that moves during wildfire smoke or a burn."""
    params = urllib.parse.urlencode({
        "latitude": location["lat"], "longitude": location["lon"],
        "current": "us_aqi,pm2_5", "timezone": tz_name,
    })
    raw = fetch_json(f"https://air-quality-api.open-meteo.com/v1/air-quality?{params}")
    if not raw or "current" not in raw:
        return None

    aqi = raw["current"].get("us_aqi")
    if aqi is None:
        return None
    for ceiling, label in ((50, "Good"), (100, "Moderate"), (150, "Unhealthy for some"),
                           (200, "Unhealthy"), (300, "Very unhealthy")):
        if aqi <= ceiling:
            break
    else:
        label = "Hazardous"
    return {"aqi": round(aqi), "label": label, "pm25": raw["current"].get("pm2_5")}


def fetch_severe_alerts(location: dict, tz: ZoneInfo) -> list[dict]:
    """Active National Weather Service warnings for campus."""
    raw = fetch_json("https://api.weather.gov/alerts/active"
                     f"?point={location['lat']},{location['lon']}")
    if not raw:
        return []

    out = []
    for feature in raw.get("features", [])[:6]:
        props = feature.get("properties", {})
        expires = to_local(props.get("ends") or props.get("expires"), tz)
        out.append({
            "event": props.get("event"),
            "severity": props.get("severity"),
            "urgency": props.get("urgency"),
            "headline": props.get("headline"),
            "instruction": (props.get("instruction") or "")[:300] or None,
            "expires": expires.isoformat() if expires else None,
            "url": props.get("@id"),
        })
    return out


def fetch_football(tz: ZoneInfo) -> list[dict]:
    """UCF Knights schedule. Home games are the ones that reshape a weekend."""
    raw = fetch_json("https://site.api.espn.com/apis/site/v2/sports/football/"
                     "college-football/teams/2116/schedule")
    if not raw:
        return []

    games = []
    for event in raw.get("events", []):
        comps = (event.get("competitions") or [{}])[0]
        kickoff = to_local(event.get("date"), tz)
        if not kickoff:
            continue

        opponent, home = None, False
        for c in comps.get("competitors", []):
            team = c.get("team", {})
            if str(team.get("id")) == "2116":
                home = c.get("homeAway") == "home"
            else:
                opponent = team.get("displayName") or team.get("name")

        status = ((comps.get("status") or {}).get("type") or {})
        # ESPN parks unannounced kickoffs at midnight ET and flags timeValid.
        time_set = bool(comps.get("timeValid"))
        games.append({
            "date": kickoff.date().isoformat(),
            "time": kickoff.strftime("%H:%M") if time_set else None,
            "opponent": opponent,
            "home": home,
            "venue": (comps.get("venue") or {}).get("fullName"),
            "state": status.get("state"),
            "detail": status.get("shortDetail"),
        })
    return games


EVENT_IMG = "https://se-images.campuslabs.com/clink/images/{}?preset=small-sq"


def fetch_campus_events(tz: ZoneInfo, days: int = 21, cap: int = 120) -> list[dict]:
    """Student-org events from KnightConnect - club meetings, socials, and the
    residence-hall stuff (Tower lobbies included)."""
    start = datetime.now(timezone.utc)
    params = urllib.parse.urlencode({
        "endsAfter": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "orderByField": "startsOn",
        "orderByDirection": "ascending",
        "status": "Approved",
        "take": 250,
    })
    raw = fetch_json("https://ucf.campuslabs.com/engage/api/discovery/event/search"
                     f"?{params}")
    if not raw:
        return []

    horizon = (start + timedelta(days=days)).isoformat()
    today = datetime.now(tz).date()
    out = []
    for e in raw.get("value", []):
        starts = to_local(e.get("startsOn"), tz)
        ends = to_local(e.get("endsOn"), tz)
        if not starts or (e.get("startsOn") or "") > horizon:
            continue
        # Semester-long postings (a volunteering drive, say) still match
        # endsAfter=now but started weeks ago. Only show what is still to come.
        if starts.date() < today:
            continue
        out.append({
            "id": e.get("id"),
            "name": e.get("name"),
            "org": e.get("organizationName"),
            "theme": e.get("theme") or "Other",
            "location": e.get("location"),
            "date": starts.date().isoformat(),
            "start": starts.strftime("%H:%M"),
            "end": ends.strftime("%H:%M") if ends else None,
            "blurb": strip_html(e.get("description"))[:160] or None,
            "rsvps": e.get("rsvpTotal") or 0,
            "image": EVENT_IMG.format(e["imagePath"]) if e.get("imagePath") else None,
            "url": f"https://ucf.campuslabs.com/engage/event/{e.get('id')}",
        })
        if len(out) >= cap:
            break
    return out


# ------------------------------------------------------------------ weather

def fetch_weather(location: dict, tz_name: str) -> dict | None:
    """Forecast for campus. Best-effort: a failure here must not break the
    schedule, which is the part people actually depend on."""
    params = urllib.parse.urlencode({
        "latitude": location["lat"],
        "longitude": location["lon"],
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code,is_day",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max,sunrise,sunset,uv_index_max"),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": tz_name,
        "forecast_days": 7,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"warn: weather unavailable ({type(exc).__name__}: {exc})", file=sys.stderr)
        return None

    cur = raw.get("current", {})
    label, icon = wmo(cur.get("weather_code"))
    daily = raw.get("daily", {})

    days = []
    for i, day in enumerate(daily.get("time", [])):
        d_label, d_icon = wmo(daily["weather_code"][i])
        days.append({
            "date": day,
            "label": d_label,
            "icon": d_icon,
            "high": round(daily["temperature_2m_max"][i]),
            "low": round(daily["temperature_2m_min"][i]),
            "precip": daily["precipitation_probability_max"][i],
            "uv": round(daily["uv_index_max"][i] or 0),
            "sunrise": daily["sunrise"][i][11:16],
            "sunset": daily["sunset"][i][11:16],
        })

    hourly = raw.get("hourly", {})
    by_hour = {
        t: {
            "temp": round(hourly["temperature_2m"][i]),
            "precip": hourly["precipitation_probability"][i],
            "code": hourly["weather_code"][i],
        }
        for i, t in enumerate(hourly.get("time", []))
    }

    return {
        "place": location["name"],
        "current": {
            "temp": round(cur.get("temperature_2m", 0)),
            "feelsLike": round(cur.get("apparent_temperature", 0)),
            "label": label,
            "icon": icon,
            "isDay": bool(cur.get("is_day", 1)),
        },
        "days": days,
        "_hourly": by_hour,
    }


def attach_weather(occurrences: list[dict], weather: dict | None) -> None:
    """Tag each upcoming class with the conditions at the hour it starts, so
    you know whether the walk across campus needs an umbrella."""
    if not weather:
        return
    for o in occurrences:
        key = f"{o['date']}T{o['start'][:2]}:00"
        hour = weather["_hourly"].get(key)
        if not hour:
            continue
        label, icon = wmo(hour["code"])
        o["weather"] = {
            "temp": hour["temp"],
            "precip": hour["precip"],
            "label": label,
            "icon": icon,
        }


# --------------------------------------------------- expand baseline -> dates

def expand_baseline(baseline: dict) -> list[dict]:
    """Turn weekly recurrence rules into one record per actual class date."""
    term = baseline["term"]
    term_start = date.fromisoformat(term["start"])
    term_end = date.fromisoformat(term["end"])
    occurrences: list[dict] = []

    for meeting in baseline["meetings"]:
        wanted = {DAY_INDEX[d] for d in meeting["days"]}
        skip = set(meeting.get("except", []))
        first = date.fromisoformat(meeting.get("firstDate") or term["start"])
        last = date.fromisoformat(meeting.get("lastDate") or term["end"])

        cursor = max(term_start, first)
        stop = min(term_end, last)
        while cursor <= stop:
            if cursor.weekday() in wanted and cursor.isoformat() not in skip:
                occurrences.append({
                    "date": cursor.isoformat(),
                    "meetingId": meeting["id"],
                    "course": meeting["course"],
                    "kind": meeting["kind"],
                    "note": meeting.get("note"),
                    "start": meeting["start"],
                    "end": meeting["end"],
                    "location": meeting["location"],
                    "source": "baseline",
                    "canceled": False,
                    "changes": {},
                    "title": f"{meeting['course']} {meeting['kind']}",
                    "url": None,
                })
            cursor += timedelta(days=1)

    return occurrences


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


# --------------------------------------------------------------- Canvas merge

def apply_canvas_events(occurrences: list[dict], events: list[dict], tz: ZoneInfo,
                        course_by_id: dict[int, str]) -> list[dict]:
    """Canvas calendar events override the baseline occurrence they overlap."""
    extras: list[dict] = []

    for ev in events:
        if ev.get("workflow_state") == "deleted" or ev.get("hidden"):
            continue

        start = to_local(ev.get("start_at"), tz)
        end = to_local(ev.get("end_at"), tz)
        if not start:
            continue
        if not end:
            end = start + timedelta(hours=1)

        ctx = ev.get("context_code", "")
        canvas_course_id = int(ctx.split("_")[-1]) if ctx.startswith("course_") else None
        code = course_by_id.get(canvas_course_id) or norm_code(ev.get("title"))
        if not code:
            continue

        day = start.date().isoformat()
        s, e = start.strftime("%H:%M"), end.strftime("%H:%M")
        location = (ev.get("location_name") or "").strip() or None

        match = next(
            (o for o in occurrences
             if o["date"] == day and o["course"] == code
             and overlaps(s, e, o["start"], o["end"])),
            None,
        )

        if match:
            changes = {}
            if location and location != match["location"]:
                changes["location"] = {"from": match["location"], "to": location}
                match["location"] = location
            if s != match["start"] or e != match["end"]:
                changes["time"] = {
                    "from": f"{match['start']}-{match['end']}",
                    "to": f"{s}-{e}",
                }
                match["start"], match["end"] = s, e
            if changes:
                match["source"] = "canvas"
                match["changes"] = changes
                match["title"] = ev.get("title") or match["title"]
                match["url"] = ev.get("html_url")
        else:
            extras.append({
                "date": day,
                "meetingId": None,
                "course": code,
                "kind": "Canvas event",
                "note": strip_html(ev.get("description"))[:180] or None,
                "start": s,
                "end": e,
                "location": location or "See Canvas",
                "source": "canvas",
                "canceled": False,
                "changes": {},
                "title": ev.get("title") or f"{code} event",
                "url": ev.get("html_url"),
            })

    return extras


def apply_manual_overrides(occurrences: list[dict], overrides: list[dict]) -> list[dict]:
    """Hand-entered corrections in data/overrides.json always win."""
    extras: list[dict] = []

    for ov in overrides:
        day, code = ov.get("date"), ov.get("course")
        if not day or not code:
            continue

        targets = [o for o in occurrences if o["date"] == day and o["course"] == code]
        if ov.get("kind"):
            targets = [o for o in targets if o["kind"] == ov["kind"]] or targets

        if not targets:
            extras.append({
                "date": day, "meetingId": None, "course": code,
                "kind": ov.get("kind") or "Added", "note": ov.get("note"),
                "start": ov.get("start", "00:00"), "end": ov.get("end", "23:59"),
                "location": ov.get("location") or "TBA", "source": "manual",
                "canceled": bool(ov.get("canceled")), "changes": {},
                "title": f"{code} {ov.get('kind') or ''}".strip(), "url": None,
            })
            continue

        for target in targets:
            changes = dict(target.get("changes") or {})
            if ov.get("canceled"):
                target["canceled"] = True
                changes["canceled"] = True
            if ov.get("location") and ov["location"] != target["location"]:
                changes["location"] = {"from": target["location"], "to": ov["location"]}
                target["location"] = ov["location"]
            if ov.get("start") and ov.get("end"):
                if (ov["start"], ov["end"]) != (target["start"], target["end"]):
                    changes["time"] = {
                        "from": f"{target['start']}-{target['end']}",
                        "to": f"{ov['start']}-{ov['end']}",
                    }
                    target["start"], target["end"] = ov["start"], ov["end"]
            if ov.get("note"):
                target["note"] = ov["note"]
            if changes:
                target["source"] = "manual"
                target["changes"] = changes

    return extras


def scan_announcements(announcements: list[dict], tz: ZoneInfo,
                       course_by_id: dict[int, str]) -> list[dict]:
    """Room changes are usually prose in an announcement, not structured data."""
    alerts: list[dict] = []

    for ann in announcements:
        body = strip_html(ann.get("message"))
        title = ann.get("title") or ""
        haystack = f"{title} {body}".lower()

        kinds = sorted({kind for pattern, kind in ALERT_PATTERNS
                        if re.search(pattern, haystack)})
        if not kinds:
            continue

        ctx = ann.get("context_code", "")
        canvas_course_id = int(ctx.split("_")[-1]) if ctx.startswith("course_") else None
        posted = to_local(ann.get("posted_at") or ann.get("created_at"), tz)

        alerts.append({
            "course": course_by_id.get(canvas_course_id) or norm_code(title) or "Course",
            "kinds": kinds,
            "title": title,
            "excerpt": body[:320],
            "postedAt": posted.isoformat() if posted else None,
            "url": ann.get("html_url"),
        })

    alerts.sort(key=lambda a: a["postedAt"] or "", reverse=True)
    return alerts[:25]


# ---------------------------------------------------------------- change log

def occurrence_key(o: dict) -> str:
    return f"{o['date']}|{o['course']}|{o['kind']}"


def label(o: dict) -> str:
    return o.get("courseTitle") or o["course"]


def diff_schedules(old: list[dict], new: list[dict], now_iso: str) -> list[dict]:
    """What actually moved since the last run — this is the audit trail."""
    old_by_key = {occurrence_key(o): o for o in old}
    new_by_key = {occurrence_key(o): o for o in new}
    entries: list[dict] = []

    for key, curr in new_by_key.items():
        prev = old_by_key.get(key)
        if prev is None:
            if old:  # do not log the entire term on first run
                entries.append({
                    "at": now_iso, "type": "added", "date": curr["date"],
                    "course": curr["course"], "courseTitle": label(curr), "kind": curr["kind"],
                    "detail": f"{curr['start']}-{curr['end']} in {curr['location']}",
                })
            continue
        if prev["location"] != curr["location"]:
            entries.append({
                "at": now_iso, "type": "room", "date": curr["date"],
                "course": curr["course"], "courseTitle": label(curr), "kind": curr["kind"],
                "detail": f"{prev['location']} → {curr['location']}",
            })
        if (prev["start"], prev["end"]) != (curr["start"], curr["end"]):
            entries.append({
                "at": now_iso, "type": "time", "date": curr["date"],
                "course": curr["course"], "courseTitle": label(curr), "kind": curr["kind"],
                "detail": f"{prev['start']}-{prev['end']} → {curr['start']}-{curr['end']}",
            })
        if not prev.get("canceled") and curr.get("canceled"):
            entries.append({
                "at": now_iso, "type": "canceled", "date": curr["date"],
                "course": curr["course"], "courseTitle": label(curr), "kind": curr["kind"], "detail": "Canceled",
            })

    for key, prev in old_by_key.items():
        if key not in new_by_key:
            entries.append({
                "at": now_iso, "type": "removed", "date": prev["date"],
                "course": prev["course"], "courseTitle": label(prev), "kind": prev["kind"],
                "detail": f"was {prev['start']}-{prev['end']} in {prev['location']}",
            })

    return entries


# --------------------------------------------------------------------- output

def ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", r"\;")
                .replace(",", r"\,").replace("\n", r"\n"))


def build_ics(schedule: dict) -> str:
    tzid = schedule["term"]["timezone"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "PRODID:-//UCF-schedule//Canvas sync//EN",
        f"X-WR-CALNAME:{ics_escape(schedule['term']['name'])} Classes",
        f"X-WR-TIMEZONE:{tzid}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for o in schedule["occurrences"]:
        if o.get("canceled"):
            continue
        day = o["date"].replace("-", "")
        uid = re.sub(r"[^A-Za-z0-9]+", "-",
                     f"{o['date']}-{o['course']}-{o['kind']}").strip("-").lower()
        summary = f"{o.get('courseTitle') or o['course']} {o['kind']}"
        if o.get("changes", {}).get("location"):
            summary += " (room changed)"

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@ucf-schedule",
            f"DTSTAMP:{stamp}",
            f"SUMMARY:{ics_escape(summary)}",
            f"DTSTART;TZID={tzid}:{day}T{o['start'].replace(':', '')}00",
            f"DTEND;TZID={tzid}:{day}T{o['end'].replace(':', '')}00",
            f"LOCATION:{ics_escape(o['location'])}",
        ]
        description = " · ".join(filter(None, [o["course"], o.get("note")]))
        if description:
            lines.append(f"DESCRIPTION:{ics_escape(description)}")
        if o.get("url"):
            lines.append(f"URL:{o['url']}")
        lines += [
            "BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY",
            "DESCRIPTION:Class starts in 10 minutes", "END:VALARM",
            "END:VEVENT",
        ]

    # Homework deadlines ride along in the same feed, with a longer warning
    # than a class gets - a due date you learn about 10 minutes out is useless.
    for a in schedule.get("assignments", []):
        due = a["dueAt"]
        day, time = due[:10].replace("-", ""), due[11:16].replace(":", "")
        uid = re.sub(r"[^A-Za-z0-9]+", "-", f"due-{due[:10]}-{a['course']}-{a['title']}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid.strip('-').lower()[:120]}@ucf-schedule",
            f"DTSTAMP:{stamp}",
            f"SUMMARY:{ics_escape('Due: ' + (a['title'] or 'Assignment'))}",
            f"DTSTART;TZID={tzid}:{day}T{time}00",
            f"DTEND;TZID={tzid}:{day}T{time}00",
            f"DESCRIPTION:{ics_escape(a.get('courseTitle') or a['course'])}",
        ]
        if a.get("url"):
            lines.append(f"URL:{a['url']}")
        lines += [
            "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY",
            f"DESCRIPTION:{ics_escape((a['title'] or 'Assignment') + ' due in 2 hours')}",
            "END:VALARM", "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ----------------------------------------------------------------------- main

def main() -> int:
    baseline = load(DATA / "baseline.json", None)
    if baseline is None:
        print("error: data/baseline.json is missing", file=sys.stderr)
        return 1

    tz = ZoneInfo(baseline["term"]["timezone"])
    now = datetime.now(tz)
    term_start, term_end = baseline["term"]["start"], baseline["term"]["end"]

    previous = load(DATA / "schedule.json", {})
    occurrences = expand_baseline(baseline)

    canvas = Canvas(BASE_URL, TOKEN)
    course_by_id: dict[int, str] = {}
    canvas_courses: list[dict] = []
    alerts: list[dict] = []
    assignments: list[dict] = []

    if canvas.ok:
        raw_courses = canvas.get(
            "courses", enrollment_state="active",
            include=["term", "sections", "total_scores"],
        ) or []
        known = {c["code"] for c in baseline["courses"]}

        for course in raw_courses:
            if not isinstance(course, dict) or "id" not in course:
                continue
            code = norm_code(course.get("course_code")) or norm_code(course.get("name"))
            if code not in known:
                continue
            course_by_id[course["id"]] = code
            enrollment = next(
                (e for e in (course.get("enrollments") or [])
                 if e.get("type") == "student"), {})
            canvas_courses.append({
                "code": code,
                "canvasId": course["id"],
                "name": course.get("name"),
                "score": enrollment.get("computed_current_score"),
                "grade": enrollment.get("computed_current_grade"),
                "url": f"{BASE_URL}/courses/{course['id']}",
            })

        contexts = [f"course_{cid}" for cid in course_by_id]
        if contexts:
            events = canvas.get(
                "calendar_events", type="event", context_codes=contexts,
                start_date=term_start, end_date=term_end, all_events=True,
            ) or []
            occurrences += apply_canvas_events(occurrences, events, tz, course_by_id)

            anns = canvas.get(
                "announcements", context_codes=contexts,
                start_date=term_start, end_date=term_end, active_only=True,
            ) or []
            alerts = scan_announcements(anns, tz, course_by_id)

            seen_assignments: set[int] = set()
            for course in canvas_courses:
                # "upcoming" misses anything already past due, so ask for both.
                for bucket in ("upcoming", "overdue"):
                    items = canvas.get(
                        f"courses/{course['canvasId']}/assignments",
                        bucket=bucket, order_by="due_at", include=["submission"],
                    ) or []
                    for item in items:
                        due = to_local(item.get("due_at"), tz)
                        if not due or item.get("id") in seen_assignments:
                            continue
                        submission = item.get("submission") or {}
                        state = submission.get("workflow_state")
                        # Don't nag about work already handed in.
                        if state in ("submitted", "graded", "pending_review"):
                            continue
                        seen_assignments.add(item["id"])
                        assignments.append({
                            "course": course["code"],
                            "courseTitle": next(
                                (c.get("title") for c in baseline["courses"]
                                 if c["code"] == course["code"]), course["code"]),
                            "title": item.get("name"),
                            "dueAt": due.isoformat(),
                            "points": item.get("points_possible"),
                            "overdue": bucket == "overdue",
                            "url": item.get("html_url"),
                        })
            assignments.sort(key=lambda a: a["dueAt"])
            assignments = assignments[:40]
    else:
        print("note: CANVAS_TOKEN not set - producing baseline-only schedule",
              file=sys.stderr)

    occurrences += apply_manual_overrides(
        occurrences, load(DATA / "overrides.json", [])
    )
    location = baseline.get("location",
                            {"name": "Campus", "lat": 28.6024, "lon": -81.2001})
    weather = fetch_weather(location, baseline["term"]["timezone"])
    attach_weather(occurrences, weather)
    if weather:
        weather["air"] = fetch_air_quality(location, baseline["term"]["timezone"])

    severe = fetch_severe_alerts(location, tz)
    football = fetch_football(tz)
    campus_events = fetch_campus_events(tz)

    # Key dates are hand-maintained; surface only what is still ahead.
    today_iso = now.date().isoformat()
    key_dates = [k for k in baseline.get("keyDates", []) if k["date"] >= today_iso]

    titles = {c["code"]: c.get("title") or c["code"] for c in baseline["courses"]}
    for o in occurrences:
        o["courseTitle"] = titles.get(o["course"], o["course"])

    occurrences.sort(key=lambda o: (o["date"], o["start"], o["course"]))

    changes = diff_schedules(previous.get("occurrences", []), occurrences,
                             now.isoformat())
    log = load(DATA / "changes.json", [])
    log = (changes + log)[:200]

    courses = []
    for course in baseline["courses"]:
        live = next((c for c in canvas_courses if c["code"] == course["code"]), None)
        courses.append({
            "code": course["code"],
            "title": course.get("title") or course["code"],
            "score": (live or {}).get("score"),
            "grade": (live or {}).get("grade"),
            "name": (live or {}).get("name") or course.get("title"),
            "canvasId": (live or {}).get("canvasId"),
            "url": (live or {}).get("url"),
        })

    schedule = {
        "generatedAt": now.isoformat(),
        "term": baseline["term"],
        "canvas": {
            "connected": canvas.ok and not canvas.errors,
            "baseUrl": BASE_URL,
            "coursesMatched": len(canvas_courses),
            "errors": canvas.errors,
        },
        "courses": courses,
        "weather": {k: v for k, v in weather.items() if not k.startswith("_")} if weather else None,
        "severe": severe,
        "football": football,
        "events": campus_events,
        "keyDates": key_dates,
        "occurrences": occurrences,
        "alerts": alerts,
        "assignments": assignments,
        "recentChanges": changes,
    }

    (DATA / "schedule.json").write_text(json.dumps(schedule, indent=2) + "\n")
    (DATA / "changes.json").write_text(json.dumps(log, indent=2) + "\n")
    (ROOT / "schedule.ics").write_text(build_ics(schedule))

    print(f"generated {now.isoformat()}")
    print(f"  canvas connected : {schedule['canvas']['connected']}")
    print(f"  courses matched  : {len(canvas_courses)}/{len(baseline['courses'])}")
    print(f"  occurrences      : {len(occurrences)}")
    print(f"  alerts           : {len(alerts)}")
    print(f"  changes this run : {len(changes)}")
    print(f"  assignments      : {len(assignments)}")
    print(f"  severe alerts    : {len(severe)}")
    print(f"  football games   : {len(football)}")
    print(f"  campus events    : {len(campus_events)}")
    print(f"  key dates ahead  : {len(key_dates)}")
    print(f"  weather          : {weather['current']['temp']}F {weather['current']['label']}"
          if weather else "  weather          : unavailable")
    for err in canvas.errors:
        print(f"  canvas error     : {err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
