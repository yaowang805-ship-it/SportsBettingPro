#!/usr/bin/env python3
"""Bootstrap tennis player name mappings by direct BB→Pinnacle matching.

Strategy: For each tennis league (now that league mappings are fixed),
fetch Pinnacle matchups, match by time proximity, then extract Chinese→English
player name pairs using multiple verification signals.

This bypasses the chicken-and-egg problem of Phase 1 needing names it doesn't have.
"""

import json, re, sys
from pathlib import Path
from difflib import SequenceMatcher as SM
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.pinnacle_api import api_get


def load_data():
    with open(ROOT / 'data/storage/bb_odds_extracted.json') as f:
        bb_data = json.load(f)
    with open(ROOT / 'data/storage/league_keywords.json') as f:
        kw = json.load(f)
    with open(ROOT / 'data/storage/team_name_map.json') as f:
        name_map = json.load(f)
    return bb_data, kw, name_map


def strip_country(name):
    """Remove country suffix like (美国) from player names."""
    return re.sub(r'\s*[（(][^)）]*[)）]', '', name).strip()


def to_pinyin(cjk_text):
    """Convert CJK text to pinyin alphabet string."""
    from pypinyin import lazy_pinyin
    py = "".join(lazy_pinyin(cjk_text)).lower()
    return re.sub(r'[^a-z]', '', py)


def split_tennis_players(name):
    """Split tennis player names (handles singles and doubles).
    Returns list of individual player names.
    """
    # Remove country suffix first
    name = strip_country(name)
    # Split by / for doubles
    parts = re.split(r'\s*/\s*', name)
    return [p.strip() for p in parts if p.strip()]


def bootstrap_tennis():
    bb_data, kw, name_map = load_data()

    # Get mapped tennis leagues
    tennis_bb = [m for m in bb_data['matches'] if m.get('sport') == 'tennis']
    print(f'BB tennis matches: {len(tennis_bb)}')

    # Group by league
    by_league = defaultdict(list)
    for m in tennis_bb:
        by_league[m['league']].append(m)

    # Get pin league name for each mapped league
    mapped_leagues = {}
    unmapped_leagues = set()
    for league in by_league:
        if league in kw:
            mapped_leagues[league] = kw[league]
        else:
            unmapped_leagues.add(league)

    print(f'Mapped tennis leagues: {len(mapped_leagues)}')
    print(f'Unmapped tennis leagues: {len(unmapped_leagues)}')
    if unmapped_leagues:
        print('  Unmapped:')
        for l in sorted(unmapped_leagues):
            print(f'    {l}')

    new_pairs = 0
    already_had = 0

    # Process each mapped league
    for bb_league, pin_league_name in sorted(mapped_leagues.items()):
        bb_matches_list = by_league[bb_league]
        print(f'\n  [{bb_league}] → {pin_league_name} ({len(bb_matches_list)} BB matches)')

        # Get Pinnacle matchups
        try:
            resp = api_get(f'/0.1/leagues/matchups?leagueName={pin_league_name}')
        except:
            # Try to find league ID
            structure_path = ROOT / 'data/storage/pinnacle_league_structure.json'
            with open(structure_path) as f:
                structure = json.load(f)

            # Find league ID by name
            league_id = None
            for sport_data in structure.values():
                if isinstance(sport_data, dict):
                    for lid, info in sport_data.items():
                        if isinstance(info, dict) and info.get('name') == pin_league_name:
                            league_id = lid
                            break
                    if league_id:
                        break

            if not league_id:
                # Try fuzzy match
                for sport_data in structure.values():
                    if isinstance(sport_data, dict):
                        for lid, info in sport_data.items():
                            if isinstance(info, dict):
                                sm = SM(None, pin_league_name.lower(),
                                       info.get('name', '').lower())
                                if sm.ratio() > 0.9:
                                    league_id = lid
                                    break
                        if league_id:
                            break

            if not league_id:
                print(f'    ⚠️ Cannot find Pinnacle league ID for: {pin_league_name}')
                continue

            resp = api_get(f'/0.1/leagues/{league_id}/matchups')

        if not resp:
            print(f'    ⚠️ No response from Pinnacle')
            continue

        # Parse Pinnacle matchups
        pin_matches = []
        if isinstance(resp, dict):
            matchups = resp.get('matchups', resp.get('data', []))
        elif isinstance(resp, list):
            matchups = resp
        else:
            print(f'    ⚠️ Unexpected response type: {type(resp)}')
            continue

        for mu in matchups:
            if isinstance(mu, dict):
                home = mu.get('home', '').strip()
                away = mu.get('away', '').strip()
                start = mu.get('start_time', '')
                if home and away:
                    pin_matches.append({
                        'home': home, 'away': away, 'start_time': start
                    })

        if not pin_matches:
            print(f'    ⚠️ No Pinnacle matchups found')
            continue

        print(f'    Pinnacle matches: {len(pin_matches)}')

        # Match BB to Pinnacle by time
        for bb_m in bb_matches_list:
            bb_home = bb_m.get('home', '').strip()
            bb_away = bb_m.get('away', '').strip()
            bb_bt = bb_m.get('bt')

            if not bb_bt:
                continue

            try:
                bb_epoch = int(int(bb_bt) / 1000)
            except (ValueError, TypeError):
                continue

            # Find closest Pinnacle match by time
            best_pin = None
            best_diff = float('inf')

            for pin_m in pin_matches:
                pin_start = pin_m['start_time']
                if not pin_start or 'T' not in pin_start:
                    continue
                try:
                    from datetime import datetime, timezone
                    start_clean = pin_start.replace('Z', '+00:00')
                    dt = datetime.fromisoformat(start_clean)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    pin_epoch = int(dt.timestamp())
                except:
                    continue

                diff = abs(bb_epoch - pin_epoch)
                if diff < best_diff:
                    best_diff = diff
                    best_pin = pin_m

            # Only match if time diff is reasonable (< 2 hours for tennis)
            if best_pin and best_diff < 7200:
                # Verify: pinyin of BB player names should fuzzy-match Pinnacle names
                bb_home_clean = strip_country(bb_home)
                bb_away_clean = strip_country(bb_away)
                pin_home = best_pin['home']
                pin_away = best_pin['away']

                # Split doubles players
                bb_home_parts = split_tennis_players(bb_home_clean)
                bb_away_parts = split_tennis_players(bb_away_clean)
                pin_home_parts = split_tennis_players(pin_home)
                pin_away_parts = split_tennis_players(pin_away)

                # For each BB player, try to find matching Pinnacle player
                # Simple approach: match by position (first BB → first Pin, second BB → second Pin)
                pairs = []
                confidence = 0

                # Try home vs home
                for i, bb_name in enumerate(bb_home_parts):
                    if i < len(pin_home_parts) and bb_name not in name_map:
                        if verify_name(bb_name, pin_home_parts[i]):
                            pairs.append((bb_name, pin_home_parts[i]))
                            confidence += 1

                # Try away vs away
                for i, bb_name in enumerate(bb_away_parts):
                    if i < len(pin_away_parts) and bb_name not in name_map:
                        if verify_name(bb_name, pin_away_parts[i]):
                            pairs.append((bb_name, pin_away_parts[i]))
                            confidence += 1

                # Try cross (home vs away) for cases where BB/Pin swap sides
                if not pairs:
                    for i, bb_name in enumerate(bb_home_parts):
                        if i < len(pin_away_parts) and bb_name not in name_map:
                            if verify_name(bb_name, pin_away_parts[i]):
                                pairs.append((bb_name, pin_away_parts[i]))
                                confidence += 1
                    for i, bb_name in enumerate(bb_away_parts):
                        if i < len(pin_home_parts) and bb_name not in name_map:
                            if verify_name(bb_name, pin_home_parts[i]):
                                pairs.append((bb_name, pin_home_parts[i]))
                                confidence += 1

                # Only save if we have good confidence
                if pairs and confidence >= len(bb_home_parts) + len(bb_away_parts) - 1:
                    for bb_name, pin_name in pairs:
                        if bb_name not in name_map:
                            name_map[bb_name] = pin_name
                            new_pairs += 1
                            print(f'      ✓ {bb_name} → {pin_name}')
                        else:
                            already_had += 1

    # Save
    with open(ROOT / 'data/storage/team_name_map.json', 'w') as f:
        json.dump(name_map, f, ensure_ascii=False, indent=2)

    print(f'\n{"="*60}')
    print(f'New tennis mappings: {new_pairs}')
    print(f'Already had: {already_had}')
    print(f'Total map: {len(name_map)} entries')


def verify_name(bb_name, pin_name):
    """Verify that a BB Chinese name likely corresponds to a Pinnacle English name.
    Uses pinyin + SequenceMatcher with a generous threshold."""
    if not bb_name or not pin_name:
        return False

    bb_name = strip_country(bb_name)

    # Check if it's a CJK name (Chinese characters or Chinese transliteration)
    has_cjk = any('一' <= c <= '鿿' for c in bb_name)

    if has_cjk:
        # Convert to pinyin and compare
        py = to_pinyin(bb_name)
        if len(py) < 3:
            return False

        pin_lower = pin_name.lower().replace(' ', '').replace('-', '')
        sm = SM(None, py, pin_lower)

        # Very relaxed threshold — we also require time match + league match
        # which provides strong independent verification
        if sm.ratio() >= 0.35:
            return True

    # Non-CJK names (e.g., Western names in Chinese transliteration like "亚当.马赫扎克")
    # These use dots as separators. Convert to pinyin and match.
    py = to_pinyin(bb_name.replace('.', ' ').replace('·', ' '))
    if len(py) >= 4:
        pin_lower = pin_name.lower().replace(' ', '').replace('-', '').replace('.', '')
        sm = SM(None, py, pin_lower)
        if sm.ratio() >= 0.35:
            return True

    return False


if __name__ == '__main__':
    bootstrap_tennis()
