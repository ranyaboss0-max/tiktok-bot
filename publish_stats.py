#!/usr/bin/env python3
"""
Writes stats-public.json (only counters, no names) for the desktop dashboard.
Runs at the end of every bot session on GitHub. Keeps the last 60 days.
"""
import json
import os
from datetime import datetime

STATS = "stats.json"
OUT = "stats-public.json"


def num(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except ValueError:
        return default


try:
    with open(STATS, encoding="utf-8") as f:
        cur = json.load(f)
except (OSError, ValueError):
    raise SystemExit(0)  # nothing to publish

try:
    with open(OUT, encoding="utf-8") as f:
        pub = json.load(f)
    if not isinstance(pub, dict):
        pub = {}
except (OSError, ValueError):
    pub = {}

days = pub.get("days")
if not isinstance(days, dict):
    days = {}

date = cur.get("date")
if date:
    days[date] = {
        "profiles": int(cur.get("profiles", 0)),
        "likes": int(cur.get("likes", 0)),
        "comments": int(cur.get("comments", 0)),
    }

keep = sorted(days)[-60:]
pub["days"] = {k: days[k] for k in keep}
pub["caps"] = {
    "profiles": num("PROFILE_CAP", 400),
    "likes": num("LIKE_CAP", 50),
    "comments": num("COMMENT_CAP", 5),
}
pub["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(pub, f, indent=1)
print("stats published:", pub["days"].get(date))
