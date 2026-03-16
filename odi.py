import requests
import json
import time
from datetime import datetime

BASE_URL = "https://api.odi.site/odi/sportsbook"

HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.odibets.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Authorization": "Bearer t",
}

# sport_id=1 is Soccer
FOOTBALL_SPORT_ID = "1"


def get_sportevents(day="", sport_id=FOOTBALL_SPORT_ID):
    r = requests.get(BASE_URL, headers=HEADERS, params={
        "producer": "0",
        "day": day,
        "sport_id": sport_id,
        "resource": "sportevents",
        "platform": "desktop",
        "mode": "1",
    })
    r.raise_for_status()
    return r.json()


def parse_match(match, league_name="", country_name=""):
    markets = {}
    for mkt in match.get("markets", []):
        name = mkt.get("odd_type", str(mkt.get("sub_type_id")))
        for line in mkt.get("lines", []):
            sels = {}
            for o in line.get("outcomes", []):
                sels[o.get("outcome_key", o.get("outcome_id"))] = {
                    "name": o.get("outcome_name", ""),
                    "odds": o.get("odd_value", ""),
                }
            markets[name] = sels

    return {
        "id": match.get("parent_match_id", ""),
        "home": match.get("home_team", ""),
        "away": match.get("away_team", ""),
        "competition": match.get("competition_name", league_name),
        "country": match.get("country_name", country_name),
        "kickoff": match.get("start_time", ""),
        "markets": markets,
    }


def fetch_odds_pipeline(sport_id=FOOTBALL_SPORT_ID, day=""):
    print(f"[1] Fetching sportevents sport_id={sport_id} day='{day}'...")
    data = get_sportevents(day=day, sport_id=sport_id)

    leagues = data.get("data", {}).get("leagues", [])
    print(f"[2] Found {len(leagues)} leagues.")

    results = []
    for league in leagues:
        league_name = league.get("competition_name", "")
        country_name = league.get("category_name", "")
        for match in league.get("matches", []):
            results.append(parse_match(match, league_name, country_name))

    print(f"[3] Total matches parsed: {len(results)}")
    return results


if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    results = fetch_odds_pipeline(day=today)

    print(f"\n{'='*60}")
    print(f"Total matches: {len(results)}")
    print(f"{'='*60}\n")

    for g in results[:3]:
        print(f"{g['home']} vs {g['away']}")
        print(f"  Competition : {g['competition']} ({g['country']})")
        print(f"  Kickoff     : {g['kickoff']}")
        print(f"  1X2         : {g['markets'].get('1X2', {})}")
        print()

    with open("odibets_odds_output.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"Saved to odibets_odds_output.json")
