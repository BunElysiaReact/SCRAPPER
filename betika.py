import requests
import json
import time

BASE_URL = "https://api.betika.com/v1"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.betika.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

FOOTBALL_SPORT_ID = 14
SUB_TYPE_IDS = "1,186,340"


def get_matches_page(page=1, limit=50, sport_id=FOOTBALL_SPORT_ID, sub_type_id=SUB_TYPE_IDS, sort_id=1):
    params = {
        "page": page,
        "limit": limit,
        "tab": "",
        "sub_type_id": sub_type_id,
        "sport_id": sport_id,
        "tag_id": "",
        "sort_id": sort_id,
        "period_id": -1,
        "esports": "false",
    }
    r = requests.get(f"{BASE_URL}/uo/matches", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def get_all_matches(sport_id=FOOTBALL_SPORT_ID, limit=50, max_pages=None, delay=0.3):
    print(f"[1] Fetching page 1...")
    first = get_matches_page(page=1, limit=limit, sport_id=sport_id)

    total = int(first.get("meta", {}).get("total", 0))
    per_page = int(first.get("meta", {}).get("per_page", limit))
    total_pages = (total + per_page - 1) // per_page if total else 1

    if max_pages:
        total_pages = min(total_pages, max_pages)

    print(f"[1] Total matches: {total} | Pages to fetch: {total_pages}")

    all_matches = first.get("data", [])

    for page in range(2, total_pages + 1):
        print(f"[{page}] Fetching page {page}/{total_pages}...")
        data = get_matches_page(page=page, limit=limit, sport_id=sport_id)
        all_matches.extend(data.get("data", []))
        time.sleep(delay)

    return all_matches


def parse_match(match):
    # odds is a list of market objects, each with sub_type_id, name, odds[]
    markets = {}
    for market in match.get("odds", []):
        sub_type_id = market.get("sub_type_id")
        markets[sub_type_id] = {
            "name": market.get("name", ""),
            "selections": [
                {
                    "display": s.get("display", ""),
                    "name": s.get("odd_key", ""),
                    "odds": s.get("odd_value", ""),
                    "outcome_id": s.get("outcome_id", ""),
                    "special_bet_value": s.get("special_bet_value", ""),
                }
                for s in market.get("odds", [])
            ]
        }

    return {
        "id": match.get("parent_match_id", match.get("match_id")),
        "home": match.get("home_team", ""),
        "away": match.get("away_team", ""),
        "competition": match.get("competition_name", ""),
        "country": match.get("category", ""),
        "kickoff": match.get("start_time", ""),
        "home_odd": match.get("home_odd", ""),
        "draw_odd": match.get("neutral_odd", ""),
        "away_odd": match.get("away_odd", ""),
        "side_bets": match.get("side_bets", 0),
        "markets": markets,
    }


def fetch_odds_pipeline(sport_id=FOOTBALL_SPORT_ID, max_pages=None):
    raw_matches = get_all_matches(sport_id=sport_id, limit=50, max_pages=max_pages)
    print(f"\n[OK] Fetched {len(raw_matches)} raw matches. Parsing...")
    results = [parse_match(m) for m in raw_matches]
    return results


if __name__ == "__main__":
    results = fetch_odds_pipeline(max_pages=None)

    print(f"\n{'='*60}")
    print(f"Total matches: {len(results)}")
    print(f"{'='*60}\n")

    for g in results[:3]:
        print(f"{g['home']} vs {g['away']}")
        print(f"  Competition : {g['competition']} ({g['country']})")
        print(f"  Kickoff     : {g['kickoff']}")
        print(f"  1X2         : {g['home_odd']} / {g['draw_odd']} / {g['away_odd']}")
        print(f"  Markets     : {json.dumps(g['markets'], indent=4)}")
        print()

    with open("betika_odds_output.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved all to betika_odds_output.json")