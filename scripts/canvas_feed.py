#!/usr/bin/env python3
"""
Fold Canvas's personal iCal feed into the schedule that sync_canvas.py built.

UCF blocks students from generating Canvas API tokens, but every Canvas user
can still make a private calendar feed (Canvas -> Calendar -> Calendar Feed).
That feed has no grades, no announcements and no submission status, but it does
carry assignment due dates and instructor-created calendar events - and those
events are where one-off room changes actually show up.

This runs AFTER sync_canvas.py and rewrites its output, so sync_canvas.py needs
no knowledge of it. If CANVAS_FEED_URL is unset, it is a no-op.

Env:
  CANVAS_FEED_URL  the private .ics URL. Secret: anyone holding it can read
                   your Canvas calendar, so it lives in a GitHub secret.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_canvas import (  # noqa: E402  - sibling module, path set above
    build_ics, norm_code, overlaps, strip_html,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FEED_URL = os.environ.get("CANVAS_FEED_URL", "").strip()


# ------------------------------------------------------------------ ICS parse

def unfold(text: str) -> str:
    """RFC 5545 folds long lines with CRLF + one space or tab."""
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def parse_events(ics: str) -> list[dict]:
    """Return one dict of {PROPNAME: (params, value)} per VEVENT."""
    out = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)\nEND:VEVENT", unfold(ics), re.S):
        props: dict[str, tuple[str, str]] = {}
        for line in block.split("\n"):
            if ":" not in line:
                continue
            head, _, value = line.partition(":")
            name, _, params = head.partition(";")
            props[name.upper()] = (params, value)
        if props:
            out.append(props)
    return out


def ics_unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\,", ",")
                 .replace("\\;", ";").replace("\\\\", "\\"))


def parse_dt(entry: tuple[str, str] | None, tz: ZoneInfo) -> datetime | None:
    """Handle the three shapes Canvas emits: UTC Z, TZID=..., and all-day DATE."""
    if not entry:
        return None
    params, value = entry
    value = value.strip()

    if "VALUE=DATE" in params.upper() and "DATE-TIME" not in params.upper():
        try:
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=tz)
        except ValueError:
            return None

    fmt = "%Y%m%dT%H%M%SZ" if value.endswith("Z") else "%Y%m%dT%H%M%S"
    try:
        dt = datetime.strptime(value, fmt)
    except ValueError:
        return None

    if value.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc).astimezone(tz)

    m = re.search(r"TZID=([^;:]+)", params)
    try:
        return dt.replace(tzinfo=ZoneInfo(m.group(1)) if m else tz).astimezone(tz)
    except Exception:  # noqa: BLE001 - unknown TZID, treat as campus time
        return dt.replace(tzinfo=tz)


def course_of(summary: str, known: set[str]) -> str | None:
    """Canvas titles read 'Homework 3 [PHY 2048-26Fall 0001]'."""
    for chunk in re.findall(r"\[([^\]]+)\]", summary):
        code = norm_code(chunk)
        if code in known:
            return code
    code = norm_code(summary)
    return code if code in known else None


def clean_title(summary: str) -> str:
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", summary).strip()


# ----------------------------------------------------------------------- main

def main() -> int:
    schedule_path = DATA / "schedule.json"
    if not schedule_path.exists():
        print("error: run sync_canvas.py first", file=sys.stderr)
        return 1

    schedule = json.loads(schedule_path.read_text())

    if not FEED_URL:
        print("note: CANVAS_FEED_URL not set - leaving schedule untouched",
              file=sys.stderr)
        return 0

    try:
        req = urllib.request.Request(
            FEED_URL, headers={"User-Agent": "ucf-schedule/1.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            ics = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - never break the schedule
        print(f"warn: Canvas feed unavailable ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 0

    tz = ZoneInfo(schedule["term"]["timezone"])
    now = datetime.now(tz)
    titles = {c["code"]: c.get("title") or c["code"] for c in schedule["courses"]}
    known = set(titles)

    assignments: list[dict] = []
    room_changes = 0
    extras: list[dict] = []
    seen_courses: set[str] = set()

    for ev in parse_events(ics):
        summary = ics_unescape(ev.get("SUMMARY", ("", ""))[1])
        uid = ev.get("UID", ("", ""))[1]
        start = parse_dt(ev.get("DTSTART"), tz)
        if not summary or not start:
            continue

        code = course_of(summary, known)
        if not code:
            continue
        seen_courses.add(code)

        url = ev.get("URL", ("", ""))[1] or None
        title = clean_title(summary)

        # Assignments carry an 'assignment' UID and a single due instant.
        if "assignment" in uid.lower():
            assignments.append({
                "course": code,
                "courseTitle": titles[code],
                "title": title,
                "dueAt": start.isoformat(),
                "points": None,
                "overdue": start < now,
                "url": url,
            })
            continue

        # Everything else is a calendar event: exam rooms, review sessions,
        # and the one-off "we're meeting in X today" entries.
        end = parse_dt(ev.get("DTEND"), tz) or (start + timedelta(hours=1))
        location = ics_unescape(ev.get("LOCATION", ("", ""))[1]).strip() or None
        day = start.date().isoformat()
        s, e = start.strftime("%H:%M"), end.strftime("%H:%M")

        match = next(
            (o for o in schedule["occurrences"]
             if o["date"] == day and o["course"] == code
             and overlaps(s, e, o["start"], o["end"])),
            None,
        )

        if match:
            changes = dict(match.get("changes") or {})
            if location and location != match["location"]:
                changes["location"] = {"from": match["location"], "to": location}
                match["location"] = location
                room_changes += 1
            if (s, e) != (match["start"], match["end"]):
                changes["time"] = {"from": f"{match['start']}-{match['end']}",
                                   "to": f"{s}-{e}"}
                match["start"], match["end"] = s, e
            if changes:
                match["source"] = "canvas"
                match["changes"] = changes
                match["url"] = url
        else:
            extras.append({
                "date": day, "meetingId": None, "course": code,
                "courseTitle": titles[code], "kind": "Canvas event",
                "note": strip_html(ics_unescape(
                    ev.get("DESCRIPTION", ("", ""))[1]))[:180] or None,
                "start": s, "end": e,
                "location": location or "See Canvas",
                "source": "canvas", "canceled": False, "changes": {},
                "title": title, "url": url,
            })

    schedule["occurrences"] = sorted(
        schedule["occurrences"] + extras,
        key=lambda o: (o["date"], o["start"], o["course"]),
    )

    assignments.sort(key=lambda a: a["dueAt"])
    schedule["assignments"] = assignments[:40]
    schedule["canvas"] = {
        **schedule.get("canvas", {}),
        "connected": True,
        "mode": "feed",
        "coursesMatched": len(seen_courses),
        # The feed cannot tell submitted from unsubmitted, so say so in the data.
        "note": "Calendar feed: due dates and events only, no grades or submission status.",
    }

    schedule_path.write_text(json.dumps(schedule, indent=2) + "\n")
    (ROOT / "schedule.ics").write_text(build_ics(schedule))

    print(f"canvas feed: {len(assignments)} assignments, {len(extras)} extra events, "
          f"{room_changes} room changes, {len(seen_courses)} courses matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
