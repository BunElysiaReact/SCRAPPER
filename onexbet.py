#!/usr/bin/env python3
"""
1xBet Kenya Odds Fetcher
Run: python3 1xbet.py
"""

import requests
import json
from datetime import datetime

BASE_URL = "https://1xbet.co.ke"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://1xbet.co.ke/en/line/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
}

# Sport IDs on 1xBet
SPORTS = [
    {"id": 1,  "name": "Football"},
    {"id": 2,  "name": "Ice Hockey"},
    {"id": 3,  "name": "Basketball"},
    {"id": 4,  "name": "Tennis"},
    {"id": 5,  "name": "Boxing"},
    {"id": 19, "name": "Cricket"},
    {"id": 40, "name": "Table Tennis"},
    {"id": 8,  "name": "Rugby"},
    {"id": 23, "name": "Volleyball"},
]


def get_sport_matches(sport_id, count=50):
    """Fetch matches for a sport via LineFeed"""
    url = f"{BASE_URL}/LineFeed/GetSportMenu"
    params = {
        "sportId": sport_id,
        "count":   count,
        "lng":     "en",
        "tf":      420,       # time frame in minutes
        "tz":      3,         # UTC+3 (EAT)
        "gr":      42,        # group — includes 1X2
        "isGlobal": "true",
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_match_odds(match_id):
    """Fetch full odds for a specific match"""
    url = f"{BASE_URL}/LineFeed/Get1x2_VZip"
    params = {
        "gameId": match_id,
        "lng":    "en",
        "isGlobal": "true",
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_match(raw, sport_name):
    """Parse a match from the sport menu response"""
    return {
        "id":          raw.get("Id") or raw.get("id"),
        "home":        raw.get("O1") or raw.get("TeamA", ""),
        "away":        raw.get("O2") or raw.get("TeamB", ""),
        "competition": raw.get("Ligue") or raw.get("league", ""),
        "country":     raw.get("country", ""),
        "kickoff":     raw.get("S") or raw.get("startTime", ""),
        "sport":       sport_name,
        # Quick 1X2 inline (not always present in menu)
        "w1":          raw.get("E", {}).get("1_1"),
        "w2":          raw.get("E", {}).get("1_2"),
        "wx":          raw.get("E", {}).get("1_3"),
    }


def fetch_sport(sport):
    sport_id   = sport["id"]
    sport_name = sport["name"]
    print(f"  [1xBet] {sport_name}...")

    try:
        data = get_sport_matches(sport_id)
    except Exception as e:
        print(f"  [1xBet] {sport_name} failed: {e}")
        return []

    # Response can be wrapped in Value, data, or direct list
    raw_list = (
        data.get("Value") or
        data.get("data") or
        data.get("Leagues") or
        (data if isinstance(data, list) else [])
    )

    matches = []

    # Flatten leagues -> games
    if isinstance(raw_list, list):
        for item in raw_list:
            # Item could be a league with Games, or a match directly
            games = item.get("Events") or item.get("Games") or item.get("TopEvents")
            if games:
                for g in games:
                    m = parse_match(g, sport_name)
                    if m["id"] and m["home"]:
                        matches.append(m)
            elif item.get("Id") or item.get("id"):
                m = parse_match(item, sport_name)
                if m["id"] and m["home"]:
                    matches.append(m)

    return matches


def normalize_match(raw):
    """Convert to same format as other scrapers"""
    markets = {}

    w1 = raw.get("w1")
    wx = raw.get("wx")
    w2 = raw.get("w2")

    if w1 and wx and w2:
        markets["1X2"] = {
            "1": float(w1),
            "X": float(wx),
            "2": float(w2),
        }

    return {
        "source":      "1xbet",
        "id":          str(raw["id"]),
        "home":        raw["home"],
        "away":        raw["away"],
        "competition": raw["competition"],
        "country":     raw["country"],
        "kickoff":     str(raw["kickoff"]),
        "sport":       raw["sport"],
        "markets":     markets,
    }


def main():
    print("=" * 60)
    print("  1xBet Kenya Odds Fetcher")
    print("=" * 60)

    all_raw = []
    for sport in SPORTS:
        matches = fetch_sport(sport)
        all_raw.extend(matches)
        if matches:
            print(f"  [1xBet] {sport['name']}: {len(matches)} matches")

    print(f"\n  Total raw: {len(all_raw)} matches")

    # Normalize
    results = [normalize_match(m) for m in all_raw if m.get("id") and m.get("home")]

    # Show sample
    print(f"\n{'='*60}")
    for m in results[:5]:
        print(f"{m['home']} vs {m['away']}")
        print(f"  {m['sport']} | {m['competition']}")
        print(f"  1X2: {m['markets'].get('1X2', 'N/A')}")
        print()

    # Save raw to inspect structure
    with open("1xbet_raw.json", "w") as f:
        json.dump(all_raw[:20], f, indent=2)
    print("Raw sample saved to 1xbet_raw.json — check structure if odds are missing")

    with open("1xbet_odds.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"Saved {len(results)} matches to 1xbet_odds.json")


if __name__ == "__main__":
    main()
