#!/usr/bin/env python3
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from garminconnect import Garmin

TOKEN_DIR = Path.home() / ".garminconnect"


def fetch_day(client, d):
    try:
        stats = client.get_stats(d)
    except Exception:
        stats = None
    try:
        sleep = client.get_sleep_data(d)
    except Exception:
        sleep = None
    try:
        hrv = client.get_hrv_data(d)
    except Exception:
        hrv = None
    return {"date": d, "stats": stats, "sleep": sleep, "hrv": hrv}


def has_signal(day):
    stats = day.get("stats") or {}
    return bool(stats.get("restingHeartRate"))


def main():
    client = Garmin()
    client.login(str(TOKEN_DIR))

    today = date.today()
    result = fetch_day(client, today.isoformat())
    if not has_signal(result):
        result = fetch_day(client, (today - timedelta(days=1)).isoformat())

    try:
        result["racePredictions"] = client.get_race_predictions()
    except Exception:
        result["racePredictions"] = None

    print(json.dumps(result, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
