"""
Kenya Sports Odds Aggregator
Sources: SportPesa, Betika, Odibets
"""

import requests
import json
import time
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────
#  UTILS
# ─────────────────────────────────────────────────────────────
def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean_team(name):
    """Normalize team name for fuzzy matching."""
    name = name.lower().strip()
    # Remove common suffixes/prefixes
    noise = [
        r'\bfc\b', r'\bsc\b', r'\bac\b', r'\baf\b', r'\bafc\b',
        r'\bclub\b', r'\bunited\b', r'\bcity\b', r'\btown\b',
        r'\bsporting\b', r'\batlético\b', r'\batlético\b',
        r'\breal\b', r'\bborussia\b',
    ]
    for n in noise:
        name = re.sub(n, '', name)
    # Remove punctuation and extra spaces
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def normalize_key(home, away):
    """Sorted cleaned team name pair as match key."""
    return tuple(sorted([clean_team(home), clean_team(away)]))


def safe_get(url, headers, params=None, cookies=None, retries=3, delay=1):
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, cookies=cookies, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                raise e


def safe_post(url, headers, payload, retries=3, delay=1):
    for i in range(retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
            else:
                raise e


# ─────────────────────────────────────────────────────────────
#  SPORTPESA
# ─────────────────────────────────────────────────────────────
SP_BASE = "https://www.ke.sportpesa.com/api"
SP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.ke.sportpesa.com/en/sports-betting/football-1/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "X-App-Timezone": "Africa/Nairobi",
    "X-Requested-With": "XMLHttpRequest",
}
SP_COOKIES = {
    "visited": "1",
    "locale": "en",
    "settings": '{"markets_layout":"multiple","betslip":{"acceptOdds":false,"amount":null,"direct":false,"betSpinnerSkipAnimation":false,"globalBetSpinnerEnabled":true},"single-wallet-first-phase":"1"}',
    "spkssid": "6143e8199a40dd054ab68e86246c6045",
}
# SportPesa highlights sport IDs (highlights/N endpoint)
# 1=Football, 2=Basketball, 3=Tennis, 4=Cricket, 5=Rugby
SP_SPORTS = [
    {"id": 1,  "name": "Football"},
    {"id": 2,  "name": "Basketball"},
    {"id": 3,  "name": "Tennis"},
    {"id": 4,  "name": "Cricket"},
    {"id": 5,  "name": "Rugby"},
    {"id": 6,  "name": "American Football"},
    {"id": 7,  "name": "Ice Hockey"},
    {"id": 8,  "name": "Baseball"},
    {"id": 9,  "name": "Volleyball"},
    {"id": 10, "name": "Table Tennis"},
]
SP_MARKETS = "10,46,52-2.5,43"


def sp_fetch_sport(sport):
    sport_id = sport["id"]
    sport_name = sport["name"]
    highlights = safe_get(f"{SP_BASE}/highlights/{sport_id}", SP_HEADERS, cookies=SP_COOKIES)
    if not highlights or not isinstance(highlights, list):
        return []

    game_ids = [g["id"] for g in highlights]
    if not game_ids:
        return []

    odds_data = safe_get(
        f"{SP_BASE}/games/markets",
        SP_HEADERS,
        params={"games": ",".join(str(g) for g in game_ids[:50]), "markets": SP_MARKETS},
        cookies=SP_COOKIES,
    )

    # API returns a dict keyed by game_id (string), not a list
    match_details = {str(m["id"]): m for m in highlights}

    results = []
    if not isinstance(odds_data, dict):
        return results

    for game_id, raw_markets in odds_data.items():
        details = match_details.get(str(game_id), {})
        competitors = details.get("competitors", [])

        markets = {}
        for m in raw_markets:
            name = m.get("name", f"market_{m.get('id')}")
            sels = {}
            for s in m.get("selections", []):
                odds_val = to_float(s.get("odds")) if s.get("odds") else None
                sels[s.get("shortName", s.get("name", ""))] = odds_val
            markets[name] = sels

        results.append({
            "source": "sportpesa",
            "id": str(game_id),
            "home": competitors[0].get("name", "") if len(competitors) > 0 else "",
            "away": competitors[1].get("name", "") if len(competitors) > 1 else "",
            "competition": details.get("competition", {}).get("name", ""),
            "country": details.get("country", {}).get("name", ""),
            "kickoff": details.get("date", ""),
            "sport": sport_name,
            "markets": markets,
        })
    return results


def sp_fetch_all():
    print("  [SportPesa] Fetching sports...")
    all_results = []
    for sport in SP_SPORTS:
        try:
            matches = sp_fetch_sport(sport)
            if matches:
                print(f"  [SportPesa] {sport['name']}: {len(matches)} matches")
            all_results.extend(matches)
        except Exception as e:
            print(f"  [SportPesa] {sport['name']} failed: {e}")
    return all_results


# ─────────────────────────────────────────────────────────────
#  BETIKA
# ─────────────────────────────────────────────────────────────
BK_BASE = "https://api.betika.com/v1"
BK_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.betika.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


def bk_get_sports():
    data = safe_get(f"{BK_BASE}/sports", BK_HEADERS)
    items = data if isinstance(data, list) else data.get("data", [])
    return [{"id": s.get("sport_id") or s.get("id"), "name": s.get("sport_name") or s.get("name", "")} for s in items if s.get("sport_id") or s.get("id")]


def bk_fetch_sport(sport_id, sport_name="", limit=50, delay=0.2):
    first = safe_get(f"{BK_BASE}/uo/matches", BK_HEADERS, params={
        "page": 1, "limit": limit, "tab": "",
        "sub_type_id": "1,186,340", "sport_id": sport_id,
        "tag_id": "", "sort_id": 1, "period_id": -1, "esports": "false",
    })
    total = int(first.get("meta", {}).get("total", 0))
    per_page = int(first.get("meta", {}).get("per_page", limit))
    total_pages = (total + per_page - 1) // per_page if total else 1

    raw = first.get("data", [])
    for page in range(2, total_pages + 1):
        try:
            data = safe_get(f"{BK_BASE}/uo/matches", BK_HEADERS, params={
                "page": page, "limit": limit, "tab": "",
                "sub_type_id": "1,186,340", "sport_id": sport_id,
                "tag_id": "", "sort_id": 1, "period_id": -1, "esports": "false",
            })
            raw.extend(data.get("data", []))
            time.sleep(delay)
        except Exception:
            break

    results = []
    for m in raw:
        markets = {}
        for mkt in m.get("odds", []):
            name = mkt.get("name", str(mkt.get("sub_type_id")))
            sels = {s["display"]: to_float(s["odd_value"]) for s in mkt.get("odds", [])}
            markets[name] = sels
        results.append({
            "source": "betika",
            "id": m.get("parent_match_id", m.get("match_id")),
            "home": m.get("home_team", ""),
            "away": m.get("away_team", ""),
            "competition": m.get("competition_name", ""),
            "country": m.get("category", ""),
            "kickoff": m.get("start_time", ""),
            "sport": sport_name,
            "markets": markets,
        })
    return results


def bk_fetch_all():
    print("  [Betika] Fetching sports...")
    try:
        sports = bk_get_sports()
    except Exception:
        sports = [{"id": 14, "name": "Soccer"}]

    all_results = []
    for sport in sports:
        try:
            matches = bk_fetch_sport(sport["id"], sport["name"])
            if matches:
                print(f"  [Betika] {sport['name']}: {len(matches)} matches")
            all_results.extend(matches)
        except Exception as e:
            print(f"  [Betika] {sport['name']} failed: {e}")
    return all_results


# ─────────────────────────────────────────────────────────────
#  ODIBETS
# ─────────────────────────────────────────────────────────────
ODI_BASE = "https://api.odi.site/odi/sportsbook"
ODI_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.odibets.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Authorization": "Bearer t",
}


def odi_get_sports():
    data = safe_get(ODI_BASE, ODI_HEADERS, params={
        "producer": "0", "day": "", "sport_id": "",
        "resource": "sportevents", "platform": "desktop", "mode": "1",
    })
    return [
        {"id": s.get("sport_id"), "name": s.get("sport_name", "")}
        for s in data.get("data", {}).get("sports", [])
        if s.get("sport_id")
    ]


def odi_fetch_sport(sport_id, sport_name="", day=""):
    data = safe_get(ODI_BASE, ODI_HEADERS, params={
        "producer": "0", "day": day, "sport_id": sport_id,
        "resource": "sportevents", "platform": "desktop", "mode": "1",
    })
    leagues = data.get("data", {}).get("leagues", [])
    results = []
    for league in leagues:
        for match in league.get("matches", []):
            markets = {}
            for mkt in match.get("markets", []):
                name = mkt.get("odd_type", str(mkt.get("sub_type_id")))
                for line in mkt.get("lines", []):
                    sels = {}
                    for o in line.get("outcomes", []):
                        sels[o.get("outcome_key", o.get("outcome_id"))] = {
                            "name": o.get("outcome_name", ""),
                            "odds": to_float(o.get("odd_value")),
                        }
                    markets[name] = sels
            results.append({
                "source": "odibets",
                "id": match.get("parent_match_id", ""),
                "home": match.get("home_team", ""),
                "away": match.get("away_team", ""),
                "competition": match.get("competition_name", league.get("competition_name", "")),
                "country": match.get("country_name", league.get("category_name", "")),
                "kickoff": match.get("start_time", ""),
                "sport": sport_name,
                "markets": markets,
            })
    return results


def odi_fetch_all(day=""):
    print("  [Odibets] Fetching sports...")
    try:
        sports = odi_get_sports()
    except Exception:
        sports = [{"id": "1", "name": "Soccer"}]

    all_results = []
    for sport in sports:
        try:
            matches = odi_fetch_sport(sport["id"], sport["name"], day=day)
            if matches:
                print(f"  [Odibets] {sport['name']}: {len(matches)} matches")
            all_results.extend(matches)
        except Exception as e:
            print(f"  [Odibets] {sport['name']} failed: {e}")
    return all_results


# ─────────────────────────────────────────────────────────────
#  AGGREGATOR
# ─────────────────────────────────────────────────────────────
def aggregate(all_sources):
    index = {}

    for match in all_sources:
        key = normalize_key(match["home"], match["away"])
        source = match["source"]

        if key not in index:
            index[key] = {
                "home": match["home"],
                "away": match["away"],
                "competition": match["competition"],
                "country": match["country"],
                "kickoff": match["kickoff"],
                "sport": match["sport"],
                "sportpesa": {},
                "betika": {},
                "odibets": {},
            }

        index[key][source] = match["markets"]

        # Fill missing meta
        for field in ["competition", "country", "sport", "kickoff"]:
            if not index[key][field] and match.get(field):
                index[key][field] = match[field]

    return list(index.values())


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main(day="", parallel=True):
    today = day or datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  Kenya Odds Aggregator — {today}")
    print(f"{'='*60}")

    if parallel:
        print("\n[*] Fetching all bookies in parallel...\n")
        results = {"sportpesa": [], "betika": [], "odibets": []}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(sp_fetch_all): "sportpesa",
                executor.submit(bk_fetch_all): "betika",
                executor.submit(lambda: odi_fetch_all(day=today)): "odibets",
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    print(f"  [{name}] FAILED: {e}")
    else:
        print("\n[*] Fetching sequentially...\n")
        results = {
            "sportpesa": sp_fetch_all(),
            "betika": bk_fetch_all(),
            "odibets": odi_fetch_all(day=today),
        }

    all_matches = results["sportpesa"] + results["betika"] + results["odibets"]
    print(f"\n[*] Merging {len(all_matches)} raw matches...")
    combined = aggregate(all_matches)

    sp_count  = sum(1 for m in combined if m["sportpesa"])
    bk_count  = sum(1 for m in combined if m["betika"])
    odi_count = sum(1 for m in combined if m["odibets"])
    all_three = sum(1 for m in combined if m["sportpesa"] and m["betika"] and m["odibets"])
    two_plus  = sum(1 for m in combined if sum([bool(m["sportpesa"]), bool(m["betika"]), bool(m["odibets"])]) >= 2)

    print(f"\n{'='*60}")
    print(f"  Unique matches      : {len(combined)}")
    print(f"  SportPesa coverage  : {sp_count}")
    print(f"  Betika coverage     : {bk_count}")
    print(f"  Odibets coverage    : {odi_count}")
    print(f"  On 2+ bookies       : {two_plus}")
    print(f"  On all 3 bookies    : {all_three}")
    print(f"{'='*60}\n")

    for g in combined[:3]:
        print(f"{g['home']} vs {g['away']}")
        print(f"  {g['sport']} | {g['competition']} ({g['country']}) | {g['kickoff']}")
        sp_1x2  = g["sportpesa"].get("3 Way", g["sportpesa"].get("1X2", {}))
        bk_1x2  = g["betika"].get("1X2", {})
        odi_1x2 = g["odibets"].get("1X2", {})
        print(f"  SportPesa : {sp_1x2}")
        print(f"  Betika    : {bk_1x2}")
        print(f"  Odibets   : {odi_1x2}")
        print()

    fname = f"combined_odds_{today}.json"
    with open(fname, "w") as f:
        json.dump(combined, f, indent=2)
    print(f"[✓] Saved → {fname}")
    return combined


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kenya Sports Odds Aggregator")
    parser.add_argument("--day", default="", help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-parallel", action="store_true", help="Run sequentially")
    args = parser.parse_args()
    main(day=args.day, parallel=not args.no_parallel)
