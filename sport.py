#!/usr/bin/env python3
"""
SportPesa Odds Fetcher - FIXED VERSION
Run this to fetch live odds with correct response parsing
"""

import requests
import json
from datetime import datetime

# Configuration
BASE_URL = "https://www.ke.sportpesa.com/api"
HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.ke.sportpesa.com/en/sports-betting/football-1/',
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
    'X-App-Timezone': 'Africa/Nairobi',
    'X-Requested-With': 'XMLHttpRequest',
}

COOKIES = {
    'visited': '1',
    'locale': 'en',
    'settings': '{"markets_layout":"multiple","betslip":{"acceptOdds":false,"amount":null,"direct":false,"betSpinnerSkipAnimation":false,"globalBetSpinnerEnabled":true},"single-wallet-first-phase":"1"}',
    'spkssid': '6143e8199a40dd054ab68e86246c6045',
}

MARKET_NAMES = {
    10: "1X2 (Match Winner)",
    46: "Double Chance",
    52: "Over/Under 2.5",
    43: "Both Teams to Score",
    17: "Correct Score",
    18: "HT/FT",
    19: "First Goalscorer",
    20: "Last Goalscorer",
    21: "Draw No Bet",
    22: "Asian Handicap",
}

def fetch_highlights(sport_id=1):
    """Fetch featured matches for a sport"""
    print(f"📡 Fetching highlights for sport {sport_id}...")
    url = f"{BASE_URL}/highlights/{sport_id}"
    response = requests.get(url, headers=HEADERS, cookies=COOKIES)
    
    if response.status_code != 200:
        print(f"❌ Error: {response.status_code}")
        return []
    
    data = response.json()
    print(f"✅ Found {len(data)} matches")
    return data

def fetch_odds(game_ids, markets="10,46,52-2.5,43"):
    """Fetch odds for specific game IDs"""
    if not game_ids:
        return {}
    
    games_param = ",".join(str(id) for id in game_ids[:50])
    print(f"📊 Fetching odds for {len(game_ids)} games...")
    
    url = f"{BASE_URL}/games/markets"
    params = {"games": games_param, "markets": markets}
    
    response = requests.get(url, headers=HEADERS, cookies=COOKIES, params=params)
    
    if response.status_code != 200:
        print(f"❌ Odds fetch failed: {response.status_code}")
        return {}
    
    return response.json()

def parse_odds_response(odds_data, highlights_data):
    """Parse odds data using game IDs as keys"""
    matches = []
    
    # Create a lookup for match details from highlights
    match_details = {str(match["id"]): match for match in highlights_data}
    
    for game_id, markets in odds_data.items():
        # Get match details from highlights
        details = match_details.get(str(game_id), {})
        competitors = details.get("competitors", [])
        
        match_info = {
            "game_id": game_id,
            "home_team": competitors[0].get("name", "Unknown") if len(competitors) > 0 else "Unknown",
            "away_team": competitors[1].get("name", "Unknown") if len(competitors) > 1 else "Unknown",
            "start_time": details.get("date", "Unknown"),
            "competition": details.get("competition", {}).get("name", "Unknown"),
            "markets": []
        }
        
        # Parse markets for this game
        for market in markets:
            market_id = market.get("id")
            market_name = MARKET_NAMES.get(market_id, market.get("name", f"Market {market_id}"))
            
            market_info = {
                "id": market_id,
                "name": market_name,
                "outcomes": []
            }
            
            for selection in market.get("selections", []):
                outcome = {
                    "name": selection.get("name"),
                    "short_name": selection.get("shortName"),
                    "odds": float(selection.get("odds", 0)) if selection.get("odds") else None,
                }
                market_info["outcomes"].append(outcome)
            
            match_info["markets"].append(market_info)
        
        matches.append(match_info)
    
    return matches

def display_matches(matches):
    """Pretty print matches and odds"""
    if not matches:
        print("❌ No matches with odds data")
        return
    
    for match in matches[:5]:  # Show first 5 matches
        print(f"\n⚽ {match['home_team']} vs {match['away_team']}")
        print(f"   🏆 {match['competition']}")
        print(f"   🕒 {match['start_time']}")
        
        for market in match['markets']:
            print(f"   📌 {market['name']}:")
            for outcome in market['outcomes']:
                if outcome['odds']:
                    print(f"      • {outcome['name']} ({outcome['short_name']}): {outcome['odds']:.2f}")
        print("-" * 50)

def save_to_file(data, filename=None):
    """Save data to JSON file"""
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"sportpesa_odds_{timestamp}.json"
    
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
    print(f"💾 Saved to {filename}")

def main():
    print("=" * 60)
    print("🏆 SPORTPESA ODDS FETCHER (FIXED VERSION)")
    print("=" * 60)
    
    # Step 1: Get highlights (match details)
    highlights = fetch_highlights(1)
    if not highlights:
        print("❌ No matches found")
        return
    
    # Step 2: Extract game IDs
    game_ids = [match["id"] for match in highlights]
    print(f"🎯 First 10 game IDs: {game_ids[:10]}")
    
    # Step 3: Fetch odds for these games
    odds_data = fetch_odds(game_ids)
    if not odds_data:
        print("❌ Failed to fetch odds")
        return
    
    # Step 4: Parse odds using both data sources
    print("\n" + "=" * 60)
    print("📊 LIVE ODDS")
    print("=" * 60)
    
    parsed_matches = parse_odds_response(odds_data, highlights)
    
    # Step 5: Display results
    display_matches(parsed_matches)
    
    # Step 6: Save to file
    output = {
        "timestamp": datetime.now().isoformat(),
        "total_matches": len(parsed_matches),
        "matches": parsed_matches,
        "raw_odds": odds_data
    }
    save_to_file(output)
    
    print(f"\n✅ Done! Total matches with odds: {len(parsed_matches)}")

if __name__ == "__main__":
    main()
