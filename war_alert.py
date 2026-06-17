"""
War preparation alert bot.

This version:

  * Watchlist anchors on Ireland's fixed home regions (IRELAND_REGION_IDS)
    so it survives occupation; expansions are picked up via regions Ireland
    currently owns. find_border_countries prints region/border counts.
  * Sample is the top SAMPLE_TOP_N (50) most active high-level players per
    country, by level. Reset floors scaled to the larger sample.
  * Reset bursts classified by direction: combat/mixed = mobilisation
    (red/orange); economy-directed = green stand-down signal.
  * Single-player reset intents are dropped entirely (too noisy, they bounce
    in and out of the watchlist run-to-run). Only count >= MIN fires.
  * "Holding at high readiness" appears only in the daily posture report,
    not the per-run update. The per-run update posts only on fresh flags
    or stand-downs, and collapse/creep lines are deduped so a lingering 7d
    comparison point doesn't re-announce the same shift every run.
  * Only med/high flags persist to flagged_last_run, so low-severity signals
    can't generate a "no longer flagged" line when they vanish.
  * Posture report: three-colour split bar (war/mixed/economy) and a separate
    at-war callout at the top so at-war countries never read as
    economy-focused (they may fight through gear/rank, which shows as low
    skill-point combat).
  * Heartbeat: the daily posture report carries an "all quiet" line when
    nothing fired, so a healthy run is visible once a day. A staleness
    watchdog fires a health alert when the previous run is older than
    STALE_RUN_HOURS (catches missed/hung runs once they resume).
  * Player-facing copy says "players"; every percentage labelled as combat
    focus. _find_history_point ages from `now`. Posture report gated to the
    first run at/after 21:00 UTC.

State fields: last_flagged_peak_ratio, last_flagged_at, last_posture_date,
last_digest_creep_delta, last_digest_demob_delta.
STATE_VERSION 7 with idempotent migration.
"""

import json
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
API_BASE = "https://warera-proxy.toie.workers.dev/trpc"
STATE_FILE = Path("war_state.json")
STATE_VERSION = 9
IRELAND_COUNTRY_ID = "6813b6d446e731854c7ac7fe"

# Ireland's home region IDs. These map territories are fixed; ownership
# changes when Ireland is invaded but the region IDs do not. We anchor the
# border walk on these so the watchlist survives occupation instead of
# collapsing to just active enemies. Update only if the game remaps Ireland.
IRELAND_REGION_IDS = [
    "6813b7069403bc4170a5d825",
    "6813b7069403bc4170a5d828",
    "6813b7069403bc4170a5d82b",
    "6813b7069403bc4170a5d82e",
]

# Watchlist scope
BORDER_HOPS = 3

# Sampling per country
ENUM_LIMIT = 100
MAX_PAGES = 15
SAMPLE_TOP_N = 50          # top players by level per country
MIN_LEVEL = 20
MIN_SAMPLE = 10
ACTIVITY_WINDOW_DAYS = 14
DISCOVERY_INTERVAL_DAYS = 7

# Concurrency
MAX_WORKERS = 5
HTTP_TIMEOUT = 30
RETRY_ATTEMPTS = 3

# Detection
HISTORY_LEN = 56
MIN_HISTORY_FOR_BASELINE = 5
BASELINE_SIGMA = 2.0
# Reset-count floors scaled to a 50-player sample (~rate-preserving vs the
# old 25-player values of 3 / 4 / 10). Dial down if alerts feel too quiet.
RESET_FLOOR = 6
NO_BASELINE_RESET_FLOOR = 8
RATIO_CREEP_MIN = 20.0
RATIO_CREEP_ORANGE = 40.0
RATIO_CREEP_RED = 60.0
RATIO_JUMP_1D_MIN = 30.0
RATIO_LOOKBACK_DAYS = 7

RATIO_DROP_MIN = 20.0
RATIO_DROP_ORANGE = 40.0
RATIO_DROP_RED = 60.0
RATIO_DROP_1D_MIN = 30.0
DEMOB_RESET_INTENT = 30.0

# ---- Military activity signal (from country.getCountryById rankings) ----
# Weekly damage dealt (a rolling 7-day total) is the accurate, activity-based
# signal that skill focus can't see: an economy-built country can top the
# damage tables by fighting through gear/rank. We model each country as being
# in one of two phases — "attacking" or "quiet" — with hysteresis between two
# thresholds so it doesn't flap, and report the transitions:
#   quiet -> attacking          = "started attacking"
#   attacking, N+ days running  = "sustained offensive"
#   attacking -> quiet          = "went quiet"
DAMAGE_ACTIVE = 3_000_000    # weekly damage at/above this = "attacking"
DAMAGE_QUIET = 1_000_000     # weekly damage below this = "quiet" (hysteresis gap)
SUSTAINED_DAYS = 4           # days attacking before a "sustained offensive" flag
WEEKLY_DAMAGE_HIGH_RANK = 25 # top-N weekly-damage rank counts as a heavyweight

COMBAT_INTENT = 70.0

# Minimum resetter counts for intent signals. Single-resetter signals are
# dropped entirely, so these effectively gate at "2 or more moving the same
# way in one check".
COMBAT_INTENT_MIN_RESETTERS = 2
ECO_INTENT_MIN_RESETTERS = 2

# A reset cluster in a country that's ALREADY saturated in that direction is
# routine reinforcement, not fresh mobilisation (e.g. an 88%-combat country
# where 2 more players rebuild for combat). Those intents are downgraded to the
# compact "minor activity" roll-up instead of each rendering a full paragraph.
COMBAT_INTENT_SATURATED = 75.0   # already this combat-built => combat rebuilds are churn
ECO_INTENT_SATURATED = 25.0      # already this economy-built => eco rebuilds are churn

# Stand-down gating
STAND_DOWN_RATIO_CEILING = 50.0
STAND_DOWN_DROP_MIN = 15.0

# Posture overview: plain-word build tiers for the daily roster.
POSTURE_WAR = 70.0      # >= "combat" build
POSTURE_LEAN = 50.0     # >= "combat-leaning"
POSTURE_MIXED = 30.0    # >= "mixed"; below this is an "economy" build
POSTURE_MOVER_MIN = 5.0 # minimum 7d build shift to tag a country as a mover

# Pipeline health
HEALTH_SNAPSHOT_RATE = 0.5
STALE_RUN_HOURS = 9        # alert if the previous run completed longer ago
                            # than this. 3h cadence, so 9h ~ 3 missed runs.
                            # Lower toward 6-7 to catch a single missed run.

# Embed colours
COLOUR_RESET_BURST = 0xED4245      # red, used for the per-run update when escalating
COLOUR_DEMOB = 0x57F287            # green, per-run update when only de-escalating
COLOUR_DIGEST = 0x5865F2           # indigo, daily posture report
COLOUR_HEALTH_WARN = 0xFEE75C
COLOUR_HEALTH_CRIT = 0xED4245

COMBAT_SKILLS = {
    "attack", "precision", "dodge", "armor", "lootChance",
    "criticalChance", "criticalDamages", "health",
}
ECO_SKILLS = {
    "companies", "entrepreneurship", "production", "management",
}

# Flag taxonomy. Every flag is one short event line in the per-run update. A
# MOBILISATION flag puts a country on the watchlist (flagged_last_run); a DEMOB
# flag never does, since standing down shouldn't add a country to the watch.
# Severity (high/med/low) is derived per detection, never hardcoded per call
# site, and drives the line icon (🔴 / 🟠 / 🟢).
MOBILISATION_KINDS = {
    "war_declared", "started_attacking", "sustained_offensive",
    "arming_up", "rebuild_war",
}
DEMOB_KINDS = {"war_ended", "went_quiet", "easing_off", "rebuild_eco"}
INTENT_SEVERITY = "med"


# ---------- API ----------

def trpc(endpoint, payload=None, attempt=1):
    params = {"input": json.dumps(payload or {})}
    try:
        r = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            msg = str(data["error"].get("message") or "unknown")
            transient = (
                "503", "504", "no available server", "timed out", "fetch failed",
                "post ", "api2.warera.io",
            )
            if any(s in msg.lower() for s in transient):
                raise requests.exceptions.RequestException(f"transient: {msg}")
            raise RuntimeError(f"{endpoint} -> {msg[:120]}")
        return data.get("result", {}).get("data")
    except (requests.exceptions.RequestException, json.JSONDecodeError):
        if attempt < RETRY_ATTEMPTS:
            time.sleep(0.4 * attempt + random.uniform(0, 0.5))
            return trpc(endpoint, payload, attempt + 1)
        raise


def fetch_countries():
    data = trpc("country.getAllCountries", {})
    if isinstance(data, dict) and "items" in data:
        return data["items"]
    return data or []


def fetch_regions():
    data = trpc("region.getRegionsObject", {}) or {}
    return data if isinstance(data, dict) else {}


def fetch_ireland():
    try:
        return trpc("country.getCountryById", {"countryId": IRELAND_COUNTRY_ID})
    except Exception as e:
        print(f"  warn: could not fetch Ireland country object: {e}",
              file=sys.stderr)
        return None


def fetch_country_detail(country_id):
    """Pull a watchlisted country's own ranking + diplomacy block.

    One cheap call per country (no per-player fan-out). Gives the accurate,
    activity-based military signal that skill sampling can't: weekly damage
    dealt, total-damage standing, active population, and the live war /
    non-aggression-pact state. Returns None on failure so callers can fall
    back to skill data alone.
    """
    try:
        d = trpc("country.getCountryById", {"countryId": country_id})
    except Exception as e:
        print(f"  warn: country detail {country_id}: {e}", file=sys.stderr)
        return None
    if not isinstance(d, dict):
        return None
    rk = d.get("rankings") or {}

    def _rank_val(key):
        v = rk.get(key)
        if not isinstance(v, dict):
            return None, None
        return v.get("value"), v.get("rank")

    weekly_damage, weekly_rank = _rank_val("weeklyCountryDamages")
    _, total_rank = _rank_val("countryDamages")
    active_pop, _ = _rank_val("countryActivePopulation")

    return {
        "weekly_damage": weekly_damage,
        "weekly_damage_rank": weekly_rank,
        "total_damage_rank": total_rank,
        "active_pop": active_pop,
        "wars_with": sorted(
            c for c in (d.get("warsWith") or []) if isinstance(c, str)
        ),
        "naps": {
            k: v for k, v in (d.get("nonAggressionUntil") or {}).items()
            if isinstance(v, str)
        },
    }


def parallel_fetch_details(country_ids):
    if not country_ids:
        return {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_country_detail, cid): cid for cid in country_ids}
        out = {}
        for f in as_completed(futures):
            cid = futures[f]
            try:
                detail = f.result()
            except Exception:
                detail = None
            if detail is not None:
                out[cid] = detail
        return out


def find_border_countries(regions_obj, country_id, home_region_ids, max_hops=None):
    if max_hops is None:
        max_hops = BORDER_HOPS

    # Anchor on Ireland's fixed home regions (ownership-independent, so the
    # walk works even while Ireland is occupied) plus any regions Ireland
    # currently owns (so territorial expansions are covered when it isn't).
    owned_now = {
        r["_id"] for r in regions_obj.values()
        if isinstance(r, dict)
        and r.get("country") == country_id
        and r.get("_id")
    }
    home_present = [rid for rid in home_region_ids if rid in regions_obj]
    own_ids = set(home_present) | owned_now

    occupied = not owned_now and bool(home_present)
    occ_note = " (OCCUPIED, anchoring on home regions)" if occupied else ""
    print(f"  [debug] regions fetched: {len(regions_obj)}, Ireland owns now: "
          f"{len(owned_now)}, home regions anchored: {len(home_present)}{occ_note}",
          file=sys.stderr)

    visited = set(own_ids)
    frontier = set(own_ids)
    borders = {}

    for _ in range(max_hops):
        next_frontier = set()
        for rid in frontier:
            region = regions_obj.get(rid)
            if not isinstance(region, dict):
                continue
            for nid in region.get("neighbors") or []:
                if nid in visited:
                    continue
                visited.add(nid)
                next_frontier.add(nid)
                neighbor = regions_obj.get(nid)
                if not isinstance(neighbor, dict):
                    continue
                ncountry = neighbor.get("country")
                if not ncountry or ncountry == country_id:
                    continue
                borders.setdefault(ncountry, []).append(
                    neighbor.get("name") or nid
                )
        if not next_frontier:
            break
        frontier = next_frontier

    print(f"  [debug] border countries within {max_hops} hops: {len(borders)}",
          file=sys.stderr)
    return borders


def _extract_diplomatic_enemies(ireland):
    enemies = {}
    if not ireland:
        return enemies
    for cid in ireland.get("warsWith") or []:
        if isinstance(cid, str):
            enemies.setdefault(cid, []).append("at war")
    return enemies


def build_watchlist(regions_obj, ireland):
    entries = {}
    for ncid, region_names in find_border_countries(
        regions_obj, IRELAND_COUNTRY_ID, IRELAND_REGION_IDS
    ).items():
        entries[ncid] = {
            "border_regions": sorted(set(region_names)),
            "diplomatic": [],
        }
    for cid, reasons in _extract_diplomatic_enemies(ireland).items():
        entries.setdefault(
            cid, {"border_regions": [], "diplomatic": []}
        )["diplomatic"].extend(reasons)
    return entries


def get_active_war_ids(ireland):
    """Returns set of country IDs Ireland is actively at war with."""
    if not ireland:
        return set()
    return {cid for cid in (ireland.get("warsWith") or []) if isinstance(cid, str)}


def fetch_citizens_page(country_id, cursor=None):
    payload = {"countryId": country_id, "limit": ENUM_LIMIT}
    if cursor:
        payload["cursor"] = cursor
    data = trpc("user.getUsersByCountry", payload) or {}
    ids = [u["_id"] for u in data.get("items", []) if u.get("_id")]
    return ids, data.get("nextCursor")


def fetch_user_lite(user_id):
    time.sleep(random.uniform(0, 0.15))
    try:
        return trpc("user.getUserLite", {"userId": user_id})
    except Exception as e:
        print(f"  warn: user {user_id}: {e}", file=sys.stderr)
        return None


def parallel_fetch_lite(user_ids):
    if not user_ids:
        return []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        return [
            r for r in (
                f.result() for f in as_completed(
                    {ex.submit(fetch_user_lite, uid) for uid in user_ids}
                )
            ) if r is not None
        ]


# ---------- Parsing helpers ----------

def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def user_level(user):
    return ((user or {}).get("leveling") or {}).get("level", 0)


def is_active(user, now):
    last = parse_iso((user.get("dates") or {}).get("lastConnectionAt"))
    return last is not None and (now - last).days <= ACTIVITY_WINDOW_DAYS


def combat_ratio(user):
    skills = user.get("skills") or {}
    combat = sum((skills.get(s) or {}).get("level", 0) for s in COMBAT_SKILLS)
    eco = sum((skills.get(s) or {}).get("level", 0) for s in ECO_SKILLS)
    total = combat + eco
    if total == 0:
        return None
    return (combat / total) * 100.0


# ---------- Country sampling ----------

def process_reset_events(sample, prev_user_resets):
    new_user_resets = {}
    new_resets = 0
    resetter_ratios = []
    combat_resets = 0
    eco_resets = 0

    for user in sample:
        uid = user.get("_id")
        if not uid:
            continue

        last_reset_iso = (user.get("dates") or {}).get("lastSkillsResetAt")
        last_reset = parse_iso(last_reset_iso)
        created = parse_iso(user.get("createdAt"))
        if last_reset and created and abs((last_reset - created).total_seconds()) < 60:
            last_reset = None
            last_reset_iso = None

        prev_iso = prev_user_resets.get(uid)

        if last_reset_iso and prev_iso:
            prev_reset = parse_iso(prev_iso)
            if prev_reset and last_reset > prev_reset:
                new_resets += 1
                cr = combat_ratio(user)
                if cr is not None:
                    resetter_ratios.append(cr)
                    if cr >= COMBAT_INTENT:
                        combat_resets += 1
                    elif cr <= DEMOB_RESET_INTENT:
                        eco_resets += 1

        if last_reset_iso:
            new_user_resets[uid] = last_reset_iso
        elif prev_iso:
            new_user_resets[uid] = prev_iso

    return new_resets, resetter_ratios, combat_resets, eco_resets, new_user_resets


def discover_qualifying(country_id, now):
    qualifying = []
    seen_ids = set()
    cursor = None

    for _ in range(MAX_PAGES):
        try:
            page_ids, cursor = fetch_citizens_page(country_id, cursor)
        except Exception:
            break
        new_ids = [uid for uid in page_ids if uid not in seen_ids]
        if not new_ids:
            break
        seen_ids.update(new_ids)

        users = parallel_fetch_lite(new_ids)
        qualifying.extend(
            u for u in users
            if user_level(u) >= MIN_LEVEL and is_active(u, now)
        )

        if len(qualifying) >= SAMPLE_TOP_N or not cursor:
            break

    return qualifying


def sample_country(country_id, country_name, now, country_state):
    known_ids = country_state.get("known_veterans", [])
    last_discovery_iso = country_state.get("last_discovery")
    last_discovery = parse_iso(last_discovery_iso)
    cache_stale = (
        last_discovery is None
        or (now - last_discovery).days >= DISCOVERY_INTERVAL_DAYS
    )

    qualifying = []
    used_discovery = False

    if known_ids and not cache_stale:
        users = parallel_fetch_lite(known_ids)
        qualifying = [
            u for u in users
            if user_level(u) >= MIN_LEVEL and is_active(u, now)
        ]
        if len(qualifying) < MIN_SAMPLE:
            qualifying = []

    if not qualifying:
        qualifying = discover_qualifying(country_id, now)
        used_discovery = True

    qualifying.sort(key=user_level, reverse=True)
    sample = qualifying[:SAMPLE_TOP_N]
    if len(sample) < MIN_SAMPLE:
        return None

    prev_user_resets = country_state.get("user_resets") or {}
    new_resets, resetter_ratios, combat_resets, eco_resets, new_user_resets = \
        process_reset_events(sample, prev_user_resets)

    ratios = [r for r in (combat_ratio(u) for u in sample) if r is not None]
    if not ratios:
        return None

    return {
        "name": country_name,
        "sample_size": len(sample),
        "new_resets": new_resets,
        "combat_resets": combat_resets,
        "eco_resets": eco_resets,
        "combat_ratio": round(statistics.median(ratios), 2),
        "resetter_combat_ratio": (
            round(statistics.median(resetter_ratios), 2)
            if resetter_ratios else None
        ),
        "known_veterans": [u.get("_id") for u in sample if u.get("_id")],
        "user_resets": new_user_resets,
        "last_discovery": now.isoformat() if used_discovery else last_discovery_iso,
        "used_discovery": used_discovery,
    }


# ---------- Detection ----------

def detect_reset_burst(history, current):
    if current >= NO_BASELINE_RESET_FLOOR:
        prior_clean = _baseline_history(history)
        if len(prior_clean) >= MIN_HISTORY_FOR_BASELINE:
            mean = statistics.mean(prior_clean)
            return {
                "baseline_mean": round(mean, 1),
                "threshold": NO_BASELINE_RESET_FLOOR,
                "current": current,
                "reason": "absolute_floor",
            }
        return {
            "baseline_mean": None,
            "threshold": NO_BASELINE_RESET_FLOOR,
            "current": current,
            "reason": "no_baseline_floor",
        }

    prior_clean = _baseline_history(history)
    if len(prior_clean) < MIN_HISTORY_FOR_BASELINE:
        return None
    mean = statistics.mean(prior_clean)
    stdev = statistics.stdev(prior_clean) if len(prior_clean) > 1 else 0.0
    threshold = max(mean + BASELINE_SIGMA * stdev, RESET_FLOOR)
    if current >= threshold:
        return {
            "baseline_mean": round(mean, 1),
            "threshold": round(threshold, 1),
            "current": current,
            "reason": "baseline_breach",
        }
    return None


def _baseline_history(history):
    values = [h.get("new_resets", 0) for h in history]
    cleaned = [v for v in values if v < NO_BASELINE_RESET_FLOOR]
    if len(cleaned) < MIN_HISTORY_FOR_BASELINE:
        return values
    return cleaned


def _burst_direction(snap):
    """Which way a reset burst points: 'combat', 'eco', or 'mixed'.

    Based on how many resetters went each way and their median combat
    focus. A clearly economy-directed burst (nobody rebuilt for combat,
    median well below the demob line) is a stand-down signal, not war prep.
    """
    combat_resets = snap.get("combat_resets", 0)
    eco_resets = snap.get("eco_resets", 0)
    rcr = snap.get("resetter_combat_ratio")
    if combat_resets >= 1 and rcr is not None and rcr >= COMBAT_INTENT:
        return "combat"
    if combat_resets == 0 and eco_resets >= 1 and rcr is not None and rcr <= DEMOB_RESET_INTENT:
        return "eco"
    return "mixed"


def _find_history_point(history, target, now, min_age_days):
    """Return the history point closest to `target`, but only if it is at
    least `min_age_days` old relative to `now`.

    Age is measured from `now`, not from `target`. `target` is itself a
    past time (e.g. now - 7d), so ageing from target would make a point
    sitting right on the lookback mark look brand new and get rejected,
    which is the bug that previously silenced all ratio signals.
    """
    if not history:
        return None
    closest = min(history, key=lambda h: abs(parse_iso(h["ts"]) - target))
    age_days = (now - parse_iso(closest["ts"])).total_seconds() / 86400
    if age_days < min_age_days - 0.5:
        return None
    return closest


def _ratio_shift_7d(history, current_ratio, now):
    """Signed 7d ratio delta if we have a usable comparison point, else
    None. Used for posture movers.
    """
    pt = _find_history_point(
        history, now - timedelta(days=RATIO_LOOKBACK_DAYS), now,
        RATIO_LOOKBACK_DAYS - 1,
    )
    if pt is None:
        return None
    return current_ratio - pt["combat_ratio"]


def detect_combat_intent_resets(snap, history, now):
    """Fires only when combat_resets >= COMBAT_INTENT_MIN_RESETTERS.

    Single-resetter signals (count == 1) are intentionally dropped: too noisy,
    they bounce in and out of the watchlist run-to-run. No flag, no minor line.
    """
    combat_resets = snap.get("combat_resets", 0)
    rcr = snap.get("resetter_combat_ratio")
    if combat_resets < COMBAT_INTENT_MIN_RESETTERS or rcr is None or rcr < COMBAT_INTENT:
        return None

    return {
        "combat_resets": combat_resets,
        "resetter_combat_ratio": rcr,
        "new_resets": snap.get("new_resets", 0),
        "corroborated_by": "count",
    }


def detect_eco_intent_resets(snap, history, now):
    """Mirror of detect_combat_intent_resets for demobilisation.

    Single-resetter signals dropped for the same reason.
    """
    eco_resets = snap.get("eco_resets", 0)
    rcr = snap.get("resetter_combat_ratio")
    if eco_resets < ECO_INTENT_MIN_RESETTERS or rcr is None or rcr > DEMOB_RESET_INTENT:
        return None

    return {
        "eco_resets": eco_resets,
        "resetter_combat_ratio": rcr,
        "new_resets": snap.get("new_resets", 0),
        "corroborated_by": "count",
    }


def detect_ratio_creep(history, current_ratio, now):
    candidates = []

    week = _find_history_point(
        history, now - timedelta(days=RATIO_LOOKBACK_DAYS), now,
        RATIO_LOOKBACK_DAYS - 1,
    )
    if week is not None:
        delta = current_ratio - week["combat_ratio"]
        if delta >= RATIO_CREEP_MIN:
            candidates.append({
                "old_ratio": week["combat_ratio"],
                "delta": round(delta, 1),
                "window_days": RATIO_LOOKBACK_DAYS,
            })

    day = _find_history_point(history, now - timedelta(days=1), now, 1)
    if day is not None:
        delta_1d = current_ratio - day["combat_ratio"]
        if delta_1d >= RATIO_JUMP_1D_MIN:
            candidates.append({
                "old_ratio": day["combat_ratio"],
                "delta": round(delta_1d, 1),
                "window_days": 1,
            })

    if not candidates:
        return None
    winner = max(candidates, key=lambda c: c["delta"])
    mag = winner["delta"]
    if mag >= RATIO_CREEP_RED:
        winner["tier"] = "red"
    elif mag >= RATIO_CREEP_ORANGE:
        winner["tier"] = "orange"
    else:
        winner["tier"] = "yellow"
    return winner


def detect_ratio_collapse(history, current_ratio, now):
    candidates = []

    week = _find_history_point(
        history, now - timedelta(days=RATIO_LOOKBACK_DAYS), now,
        RATIO_LOOKBACK_DAYS - 1,
    )
    if week is not None:
        delta = current_ratio - week["combat_ratio"]
        if delta <= -RATIO_DROP_MIN:
            candidates.append({
                "old_ratio": week["combat_ratio"],
                "delta": round(delta, 1),
                "window_days": RATIO_LOOKBACK_DAYS,
            })

    day = _find_history_point(history, now - timedelta(days=1), now, 1)
    if day is not None:
        delta_1d = current_ratio - day["combat_ratio"]
        if delta_1d <= -RATIO_DROP_1D_MIN:
            candidates.append({
                "old_ratio": day["combat_ratio"],
                "delta": round(delta_1d, 1),
                "window_days": 1,
            })

    if not candidates:
        return None
    winner = min(candidates, key=lambda c: c["delta"])
    mag = abs(winner["delta"])
    if mag >= RATIO_DROP_RED:
        winner["tier"] = "green_strong"
    elif mag >= RATIO_DROP_ORANGE:
        winner["tier"] = "green_med"
    else:
        winner["tier"] = "green_light"
    return winner


def detect_war_changes(prev_wars, current_wars):
    """Diff a country's war list run-to-run.

    Returns (newly_declared, newly_ended) lists of country IDs, or
    (None, None) when there's no prior record to diff against (first sighting),
    so a country's whole existing war list can't read as freshly declared.
    """
    if prev_wars is None:
        return None, None
    prev = set(prev_wars)
    cur = set(current_wars or [])
    return sorted(cur - prev), sorted(prev - cur)


def classify_damage_phase(prev_country, weekly_damage, now):
    """Two-phase ("attacking"/"quiet") state machine over weekly damage dealt.

    Returns (events, fields) where `events` is a list of event dicts to flag
    this run and `fields` is the phase state to persist. Hysteresis between
    DAMAGE_ACTIVE and DAMAGE_QUIET stops a country hovering near one threshold
    from flapping. On first sighting (no prior phase) we initialise silently so
    a country already mid-offensive isn't reported as "just started".
    """
    prev_phase = prev_country.get("damage_phase")
    since = prev_country.get("damage_phase_since")
    sustained_reported = bool(prev_country.get("sustained_reported"))
    events = []

    if weekly_damage is None:
        # No data this run: carry phase forward untouched.
        return events, {
            "damage_phase": prev_phase,
            "damage_phase_since": since,
            "sustained_reported": sustained_reported,
        }

    phase = prev_phase
    if prev_phase is None:
        phase = "attacking" if weekly_damage >= DAMAGE_ACTIVE else "quiet"
        since = now.isoformat()
        sustained_reported = False
    elif prev_phase == "quiet" and weekly_damage >= DAMAGE_ACTIVE:
        phase = "attacking"
        since = now.isoformat()
        sustained_reported = False
        events.append({"kind": "started_attacking", "severity": "high",
                       "detection": {"weekly_damage": weekly_damage}})
    elif prev_phase == "attacking" and weekly_damage < DAMAGE_QUIET:
        phase = "quiet"
        since = now.isoformat()
        sustained_reported = False
        events.append({"kind": "went_quiet", "severity": "med",
                       "detection": {"weekly_damage": weekly_damage}})

    if phase == "attacking" and not sustained_reported and since:
        since_dt = parse_iso(since)
        days = (now - since_dt).days if since_dt else 0
        if days >= SUSTAINED_DAYS:
            events.append({"kind": "sustained_offensive", "severity": "high",
                           "detection": {"days": days, "weekly_damage": weekly_damage}})
            sustained_reported = True

    return events, {
        "damage_phase": phase,
        "damage_phase_since": since,
        "sustained_reported": sustained_reported,
    }


# ---------- State ----------

def load_state():
    if not STATE_FILE.exists():
        return {"version": STATE_VERSION, "countries": {}, "flagged_last_run": []}
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def migrate(state):
    version = state.get("version", 1)

    if version < 4:
        for country in state.get("countries", {}).values():
            country.pop("history", None)
            country.pop("resets_5d", None)
            country.setdefault("user_resets", {})
            country.setdefault("last_urgent_alert", None)
            country.setdefault("last_urgent_count", None)
        state.setdefault("flagged_last_run", [])
        state["version"] = 4
        print("Migrated state v3 -> v4.")

    if version < 5:
        for country in state.get("countries", {}).values():
            country.setdefault("combat_resets", 0)
            country.setdefault("eco_resets", 0)
            country.setdefault("last_demob_alert", None)
            country.setdefault("last_demob_delta", None)
            country.setdefault("last_creep_alert", None)
            country.setdefault("last_creep_delta", None)
        state["version"] = 5
        print("Migrated state v4 -> v5.")

    if version < 6:
        for country in state.get("countries", {}).values():
            country.setdefault("last_flagged_at", None)
            country.setdefault("last_flagged_peak_ratio", None)
        state["version"] = 6
        print("Migrated state v5 -> v6 (added flagged-peak tracking).")

    if version < 7:
        for country in state.get("countries", {}).values():
            country.setdefault("last_digest_creep_delta", None)
            country.setdefault("last_digest_demob_delta", None)
        state["version"] = 7
        print("Migrated state v6 -> v7 (added per-run update dedup fields).")

    if version < 8:
        for country in state.get("countries", {}).values():
            # wars_with stays None until the first detail fetch records it, so
            # an existing war list isn't mistaken for freshly-declared wars.
            country.setdefault("wars_with", None)
            country.setdefault("weekly_damage", None)
            country.setdefault("weekly_damage_rank", None)
            country.setdefault("total_damage_rank", None)
            country.setdefault("active_pop", None)
        state["version"] = 8
        print("Migrated state v7 -> v8 (added military-activity fields).")

    if version < 9:
        for country in state.get("countries", {}).values():
            # damage_phase stays None until the first detail fetch sets it, so a
            # country already mid-offensive isn't reported as "just started".
            country.setdefault("damage_phase", None)
            country.setdefault("damage_phase_since", None)
            country.setdefault("sustained_reported", False)
        state["version"] = 9
        print("Migrated state v8 -> v9 (added damage-phase tracking).")

    return state


# ---------- Alerts ----------

def post_embed(payload):
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()


def safe_post(embed, label):
    try:
        post_embed({"embeds": [embed]})
        return True
    except Exception as e:
        print(f"  failed to post {label}: {e}", file=sys.stderr)
        return False


# ---------- Per-run update (compact, one line per country) ----------

def _event_icon(flag):
    if flag["kind"] in DEMOB_KINDS:
        return "\U0001F7E2"  # green
    return {"high": "\U0001F534", "med": "\U0001F7E0", "low": "\U0001F7E1"}.get(
        flag["severity"], "\U0001F7E0"
    )


def _event_text(flag):
    """Short "<Country> - <what happened>" line for the per-run update."""
    name = flag["name"]
    d = flag.get("detection") or {}
    snap = flag.get("snap") or {}
    kind = flag["kind"]
    ratio = snap.get("combat_ratio", 0)

    if kind == "war_declared":
        if d.get("against_ireland"):
            return f"**{name}** declared war on Ireland"
        return f"**{name}** declared war on {', '.join(d.get('new_war_names', []))}"
    if kind == "war_ended":
        return f"**{name}** ended its war with {', '.join(d.get('ended_war_names', []))}"
    if kind == "started_attacking":
        rank = d.get("rank")
        where = f" (now #{rank} in the game for damage)" if rank else ""
        return f"**{name}** started attacking — it was quiet, now it's fighting{where}"
    if kind == "sustained_offensive":
        rank = d.get("rank")
        where = f", #{rank} in the game for damage" if rank else ""
        return (f"**{name}** has been attacking hard for {d.get('days')} days "
                f"straight{where}")
    if kind == "went_quiet":
        return f"**{name}** stopped attacking — its damage has dropped off"
    if kind == "arming_up":
        return (f"**{name}**'s players are shifting toward combat builds "
                f"({d.get('old_ratio', 0):.0f}% → {ratio:.0f}% on combat skills)")
    if kind == "easing_off":
        return (f"**{name}**'s players are shifting back toward economy "
                f"({d.get('old_ratio', 0):.0f}% → {ratio:.0f}% on combat skills)")
    if kind == "rebuild_war":
        return f"**{name}** — {d.get('n')} top players just retrained for combat"
    if kind == "rebuild_eco":
        return f"**{name}** — {d.get('n')} top players just retrained for economy"
    return f"**{name}**"


def _event_sort_key(flag):
    deesc = 1 if flag["kind"] in DEMOB_KINDS else 0
    sev = {"high": 0, "med": 1, "low": 2}.get(flag["severity"], 3)
    return (deesc, sev, flag["name"])


def send_digest(flagged, stood_down, now):
    """One compact embed, one line per country. Escalating signals first
    (red high / orange med / yellow low), then de-escalating (green). A country
    with several signals this run is collapsed to its single most important line.
    """
    best = {}
    for f in flagged:
        cid = f["cid"]
        if cid not in best or _event_sort_key(f) < _event_sort_key(best[cid]):
            best[cid] = f
    events = sorted(best.values(), key=_event_sort_key)

    lines = [f"{_event_icon(f)} {_event_text(f)}" for f in events]

    shown = set(best)
    for s in (stood_down or []):
        if s.get("cid") in shown:
            continue
        lines.append(f"\U0001F7E2 **{s['name']}** has calmed down — no longer a concern")

    if not lines:
        return False

    esc = sum(1 for f in events if f["kind"] in MOBILISATION_KINDS)
    deesc = len(lines) - esc
    bits = []
    if esc:
        bits.append(f"\U0001F53A {esc} getting more dangerous")
    if deesc:
        bits.append(f"\U0001F53B {deesc} calming down")
    header = " · ".join(bits)

    embed = {
        "title": "\U0001F6E1️ War Watch · Update",
        "color": COLOUR_RESET_BURST if esc else COLOUR_DEMOB,
        "description": (f"{header}\n\n" if header else "") + "\n".join(lines),
        "timestamp": now.isoformat(),
    }
    return safe_post(embed, "update")



# ---------- Posture overview ----------

def _build_word(ratio):
    """Plain-English label for how a country's top players are skilled, so a
    fighting-but-economy country reads as "mostly economy · #11 in damage"
    instead of a jarring "0%"."""
    if ratio >= POSTURE_WAR:
        return "mostly combat"
    if ratio >= POSTURE_LEAN:
        return "leaning combat"
    if ratio >= POSTURE_MIXED:
        return "mixed"
    return "mostly economy"


def send_posture_digest(snapshots, state, active_war_ids, country_name,
                        watchlist, holding, flagged, stood_down, now,
                        details=None):
    """End-of-day report and daily heartbeat. Sorts every monitored country
    into a war side and an economy side, with a per-tier breakdown, the
    day, here, not in the per-run update).

    One unified roster, each country listed exactly once: at-war countries in
    their own callout first, then everyone else in a single list sorted by
    recent combat output (weekly-damage rank). Build is shown as a plain word,
    and movers (📈/📉) and holding are inline tags rather than separate
    sections, so the same country never appears two or three times. Carries an
    "all quiet" status line when nothing fired, so a healthy run is visible
    once a day even on a calm day.
    """
    details = details or {}
    countries_state = state.get("countries", {})
    holding_by_cid = {h["cid"]: h for h in (holding or []) if h.get("cid")}

    # Build one row per WATCHED country, not just the ones we could skill-sample.
    # A country we couldn't sample but have a damage rank for still appears in
    # the roster (rank shown, build marked unknown); only countries with no data
    # at all drop to a small footnote. This keeps the roster complete.
    ids = list(watchlist) if watchlist else list(snapshots)
    rows = []
    no_data = []
    for cid in ids:
        snap = snapshots.get(cid)
        det = (snap.get("detail") if snap else None) or details.get(cid) or {}
        rank = det.get("weekly_damage_rank")
        name = (snap["name"] if snap else None) or country_name.get(cid) or cid
        if snap is None and rank is None:
            no_data.append(name)
            continue
        ratio = snap["combat_ratio"] if snap else None
        shift = None
        if snap is not None:
            history = countries_state.get(cid, {}).get("history", [])
            shift = _ratio_shift_7d(history, ratio, now)
        rows.append({
            "cid": cid, "name": name, "ratio": ratio, "shift_7d": shift,
            "at_war": cid in active_war_ids, "weekly_rank": rank,
            "sampled": snap is not None,
        })

    monitored = len(ids)
    sampled = sum(1 for r in rows if r["sampled"])

    if not rows:
        names = ", ".join(sorted(no_data)[:20]) if no_data else ""
        embed = {
            "title": "📊 War Watch · Daily Posture Report",
            "color": COLOUR_DIGEST,
            "description": (
                f"Couldn't read any of the **{monitored}** watched countries this "
                f"run — the game's data service was probably down. Nothing to "
                f"show today." + (f"\n\nWatched: {names}" if names else "")
            ),
            "timestamp": now.isoformat(),
        }
        return safe_post(embed, "posture digest")

    def _roster_line(r, at_war=False):
        rank = f"#{r['weekly_rank']} in damage" if r.get("weekly_rank") else "no damage data"
        parts = [f"**{r['name']}**", rank]
        if r["ratio"] is None:
            parts.append("build unknown (too few players)")
        else:
            parts.append(_build_word(r["ratio"]))
        sh = r.get("shift_7d")
        if not at_war and sh is not None and abs(sh) >= POSTURE_MOVER_MIN:
            parts.append("📈 shifting to combat" if sh > 0 else "📉 easing to economy")
        if not at_war and r["cid"] in holding_by_cid:
            parts.append("still on alert")
        return " · ".join(parts)

    def _chunk_fields(name, lines):
        """Split a list of lines into fields that stay under Discord's
        ~1024-char-per-field limit, so a long roster posts in full."""
        out, buf, size = [], [], 0
        for ln in lines:
            if buf and size + len(ln) + 1 > 1000:
                out.append((name if not out else f"{name} (cont.)", "\n".join(buf)))
                buf, size = [], 0
            buf.append(ln)
            size += len(ln) + 1
        if buf:
            out.append((name if not out else f"{name} (cont.)", "\n".join(buf)))
        return out

    # At-war countries appear ONLY in their callout; everyone else appears once
    # in the roster below, sorted by recent combat output (unranked last).
    at_war_rows = sorted(
        (r for r in rows if r["at_war"]),
        key=lambda r: (r["weekly_rank"] or 9999),
    )
    others = sorted(
        (r for r in rows if not r["at_war"]),
        key=lambda r: (r["weekly_rank"] is None, r["weekly_rank"] or 0,
                       -(r["ratio"] or 0)),
    )

    # Heartbeat / activity status line
    mob = sum(1 for f in flagged if f["kind"] in MOBILISATION_KINDS)
    demob = sum(1 for f in flagged if f["kind"] in DEMOB_KINDS)
    bits = []
    if mob:
        bits.append(f"**{mob}** getting more dangerous")
    if demob:
        bits.append(f"**{demob}** calming down")
    if stood_down:
        bits.append(f"**{len(stood_down)}** no longer a concern")
    if holding:
        bits.append(f"**{len(holding)}** still on alert")
    if bits:
        status_line = "Today: " + ", ".join(bits) + ".\n\n"
    else:
        status_line = "✅ All quiet today — nothing started or stopped.\n\n"

    description = (
        status_line
        + f"All **{monitored}** watched countries, sorted by how much fighting "
        f"they've done lately (#1 = most damage in the game). _The build label "
        f"is how their top players are skilled — a hint at intent that lags the "
        f"real fighting._"
    )

    fields = []

    if at_war_rows:
        for nm, val in _chunk_fields(
            f"⚔️ At war with Ireland   ({len(at_war_rows)})",
            [_roster_line(r, at_war=True) for r in at_war_rows],
        ):
            fields.append({"name": nm, "value": val, "inline": False})

    if others:
        for nm, val in _chunk_fields(
            f"📊 Everyone else, most fighting first   ({len(others)})",
            [_roster_line(r) for r in others],
        ):
            fields.append({"name": nm, "value": val, "inline": False})

    if no_data:
        names = ", ".join(sorted(no_data)[:20])
        more = f" (+{len(no_data) - 20} more)" if len(no_data) > 20 else ""
        fields.append({
            "name": f"⚪ No data this run   ({len(no_data)})",
            "value": names + more,
            "inline": False,
        })

    embed = {
        "title": "📊 War Watch · Daily Posture Report",
        "color": COLOUR_DIGEST,
        "description": description,
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    return safe_post(embed, "posture digest")


def send_health_alert(message, critical=False):
    embed = {
        "title": "🚨 War Watch · The bot couldn't run this check"
                 if critical else "⚠️ War Watch · This check has missing data",
        "color": COLOUR_HEALTH_CRIT if critical else COLOUR_HEALTH_WARN,
        "description": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return safe_post(embed, "health alert")


# ---------- Stand-down vs holding classification ----------

def classify_post_flag_state(cid, prev_country, snap, active_war_ids):
    """Decide whether a previously-flagged country stood down or is holding.

    Soft-flag retirement is gone: only med/high flags are persisted now, so a
    country reaching here was genuinely mobilised. Outcomes: holding (active
    war or still elevated), or stood_down (ratio dropped from peak / no peak).
    """
    if cid in active_war_ids:
        return ("holding", "active_war")

    current_ratio = snap["combat_ratio"]
    peak_ratio = prev_country.get("last_flagged_peak_ratio")

    if peak_ratio is None:
        return ("stood_down", "retired_no_data")

    drop_from_peak = peak_ratio - current_ratio
    if drop_from_peak >= STAND_DOWN_DROP_MIN and current_ratio < STAND_DOWN_RATIO_CEILING:
        return ("stood_down", "ratio_dropped")

    if current_ratio >= STAND_DOWN_RATIO_CEILING:
        return ("holding", "ratio_high")

    # Was mobilised, no clear ratio drop and not still high: treat as stood
    # down via the ratio_dropped path (a peak existed).
    return ("stood_down", "ratio_dropped")


# ---------- Main ----------

def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    state = migrate(state)

    # Staleness watchdog: if the previous run completed much longer ago than
    # the 3h cadence, runs were missed. Fire once on the run that resumes.
    # Can't catch a fully-dead scheduler (no run executes to send it), but
    # flags the gap as soon as runs come back.
    prev_run = parse_iso(state.get("last_run"))
    if prev_run is not None:
        gap_h = (now - prev_run).total_seconds() / 3600
        if gap_h >= STALE_RUN_HOURS:
            send_health_alert(
                f"The bot hadn't run for **{gap_h:.0f} hours** (it normally checks "
                f"every 3 hours), so a few checks were skipped and there's a gap in "
                f"the data. It's working again now — just letting you know."
            )

    countries = fetch_countries()
    regions = fetch_regions()
    ireland = fetch_ireland()
    watchlist = build_watchlist(regions, ireland)
    active_war_ids = get_active_war_ids(ireland)

    country_name = {c["_id"]: c.get("name", c["_id"]) for c in countries if c.get("_id")}
    print(f"Loaded {len(countries)} countries. Watchlist: {len(watchlist)} "
          f"(within {BORDER_HOPS} hops of Ireland). Active wars: {len(active_war_ids)}.")
    for cid in sorted(watchlist, key=lambda c: country_name.get(c, c)):
        entry = watchlist[cid]
        parts = []
        if entry["border_regions"]:
            regs = sorted(entry["border_regions"])
            if len(regs) > 4:
                shown = f"{', '.join(regs[:3])} (+{len(regs) - 3} more)"
            else:
                shown = ", ".join(regs)
            parts.append(f"in range: {shown}")
        if entry["diplomatic"]:
            parts.append(", ".join(entry["diplomatic"]))
        print(f"  {country_name.get(cid, cid)}: {'; '.join(parts)}")

    if not watchlist:
        print("Empty watchlist, nothing to sample. Exiting.")
        send_health_alert(
            "The bot couldn't work out which countries to watch this run — "
            "Ireland's map and war data didn't load. No countries were checked. "
            "This is almost always the game's data service being down; it should "
            "recover on its own.",
            critical=True,
        )
        state["last_run"] = now.isoformat()
        save_state(state)
        return

    countries = [c for c in countries if c.get("_id") in watchlist]

    # Military-activity signal: one cheap getCountryById per watchlisted country
    # (no per-player fan-out). Fetched for every watchlist member, even those we
    # can't skill-sample, so an unreadable at-war heavyweight still surfaces.
    details = parallel_fetch_details(list(watchlist.keys()))
    print(f"Fetched country detail for {len(details)}/{len(watchlist)} countries.")

    snapshots = {}
    for i, country in enumerate(countries, 1):
        cid = country.get("_id")
        cname = country.get("name") or cid
        if not cid:
            continue
        country_state = state.get("countries", {}).get(cid, {})
        print(f"[{i}/{len(countries)}] {cname}...", end=" ", flush=True)
        try:
            snap = sample_country(cid, cname, now, country_state)
        except Exception as e:
            print(f"failed: {e}")
            continue
        if snap is None:
            print("skipped (insufficient sample)")
            continue
        snap["detail"] = details.get(cid)
        snapshots[cid] = snap
        mode = "disc" if snap.get("used_discovery") else "cache"
        rcr = snap.get("resetter_combat_ratio")
        rcr_str = f" rcr={rcr:.0f}%" if rcr is not None else ""
        intent_str = ""
        if snap.get("combat_resets"):
            intent_str = f" cmb={snap['combat_resets']}"
        elif snap.get("eco_resets"):
            intent_str = f" eco={snap['eco_resets']}"
        det = snap.get("detail") or {}
        dmg_str = ""
        if det.get("weekly_damage_rank"):
            dmg_str = f" wklyDmgRank=#{det['weekly_damage_rank']}"
        print(f"sample={snap['sample_size']}({mode}) "
              f"new_resets={snap['new_resets']}{intent_str} "
              f"combat={snap['combat_ratio']:.0f}%{rcr_str}{dmg_str}")

    if watchlist:
        snapshot_rate = len(snapshots) / len(watchlist)
        if snapshot_rate < HEALTH_SNAPSHOT_RATE:
            send_health_alert(
                f"This check could only read **{len(snapshots)} of "
                f"{len(watchlist)}** watched countries ({snapshot_rate:.0%}). The "
                f"game's data service may be slow or down, so today's numbers may "
                f"be incomplete. It should recover on its own."
            )

    flagged = []
    new_countries_state = dict(state.get("countries", {}))

    for cid, snap in snapshots.items():
        prev_country = state.get("countries", {}).get(cid, {})
        history = prev_country.get("history", [])

        detail = snap.get("detail") or {}
        weekly_damage = detail.get("weekly_damage")
        current_wars = detail.get("wars_with")

        burst = detect_reset_burst(history, snap["new_resets"])
        burst_dir = _burst_direction(snap) if burst else None
        creep = detect_ratio_creep(history, snap["combat_ratio"], now)
        collapse = detect_ratio_collapse(history, snap["combat_ratio"], now)
        combat_intent = detect_combat_intent_resets(snap, history, now)
        eco_intent = detect_eco_intent_resets(snap, history, now)

        # Military-activity detectors. wars_with is only diffed when we have a
        # prior record (prev_wars is None on first sighting), so an existing
        # war list never reads as freshly declared. Damage shift needs the
        # detail block; absent it, both come back empty.
        prev_wars = prev_country.get("wars_with")
        new_wars, ended_wars = (None, None)
        if current_wars is not None:
            new_wars, ended_wars = detect_war_changes(prev_wars, current_wars)
        damage_events, phase_fields = classify_damage_phase(
            prev_country, weekly_damage, now
        )

        new_history = (history + [{
            "ts": now.isoformat(),
            "new_resets": snap["new_resets"],
            "combat_resets": snap.get("combat_resets", 0),
            "eco_resets": snap.get("eco_resets", 0),
            "combat_ratio": snap["combat_ratio"],
            "resetter_combat_ratio": snap.get("resetter_combat_ratio"),
            "weekly_damage": weekly_damage,
        }])[-HISTORY_LEN:]

        new_country = {
            "name": snap["name"],
            "sample_size": snap["sample_size"],
            "new_resets": snap["new_resets"],
            "combat_resets": snap.get("combat_resets", 0),
            "eco_resets": snap.get("eco_resets", 0),
            "combat_ratio": snap["combat_ratio"],
            "resetter_combat_ratio": snap.get("resetter_combat_ratio"),
            "known_veterans": snap.get("known_veterans", []),
            "user_resets": snap.get("user_resets", {}),
            "last_discovery": snap.get("last_discovery"),
            "last_flagged_at": prev_country.get("last_flagged_at"),
            "last_flagged_peak_ratio": prev_country.get("last_flagged_peak_ratio"),
            "wars_with": current_wars if current_wars is not None else prev_wars,
            "weekly_damage": weekly_damage,
            "weekly_damage_rank": detail.get("weekly_damage_rank"),
            "total_damage_rank": detail.get("total_damage_rank"),
            "active_pop": detail.get("active_pop"),
            "damage_phase": phase_fields["damage_phase"],
            "damage_phase_since": phase_fields["damage_phase_since"],
            "sustained_reported": phase_fields["sustained_reported"],
            "history": new_history,
        }

        # ---- Build this run's event list (each renders as one short line) ----
        # We append one flag per signal; send_digest later collapses to a
        # single line per country.
        flagged_this_run = False

        # War declared / ended — the most important signal.
        if new_wars:
            against_ireland = IRELAND_COUNTRY_ID in new_wars
            flagged.append({
                "cid": cid, "name": snap["name"], "kind": "war_declared",
                "severity": "high" if against_ireland else "med", "snap": snap,
                "detection": {
                    "new_war_names": [country_name.get(c, c) for c in new_wars],
                    "against_ireland": against_ireland,
                },
            })
            flagged_this_run = True
        if ended_wars:
            flagged.append({
                "cid": cid, "name": snap["name"], "kind": "war_ended",
                "severity": "med", "snap": snap,
                "detection": {
                    "ended_war_names": [country_name.get(c, c) for c in ended_wars],
                },
            })
            flagged_this_run = True

        # Damage-phase transitions (started attacking / sustained / went quiet).
        for ev in damage_events:
            flagged.append({
                "cid": cid, "name": snap["name"], "kind": ev["kind"],
                "severity": ev["severity"], "snap": snap,
                "detection": {**ev["detection"],
                              "rank": detail.get("weekly_damage_rank")},
            })
            flagged_this_run = True

        # War-vs-economy build shift. Easing-off is suppressed while the country
        # is actively at war with Ireland — a build dip isn't de-escalation when
        # shells are still flying.
        if creep:
            sev = {"red": "high", "orange": "med", "yellow": "low"}[creep["tier"]]
            flagged.append({
                "cid": cid, "name": snap["name"], "kind": "arming_up",
                "severity": sev, "snap": snap, "detection": creep,
            })
            flagged_this_run = True
        if collapse and cid not in active_war_ids:
            sev = {"green_strong": "med", "green_med": "med",
                   "green_light": "low"}[collapse["tier"]]
            flagged.append({
                "cid": cid, "name": snap["name"], "kind": "easing_off",
                "severity": sev, "snap": snap, "detection": collapse,
            })
            flagged_this_run = True

        # Skill-reset clusters = "rebuilding for war/economy". Skipped when the
        # country is already saturated in that direction (routine reinforcement,
        # not a fresh move).
        combat_cluster = (burst and burst_dir == "combat") or combat_intent
        if combat_cluster and snap["combat_ratio"] < COMBAT_INTENT_SATURATED:
            n = (burst["current"] if (burst and burst_dir == "combat")
                 else combat_intent["combat_resets"])
            flagged.append({
                "cid": cid, "name": snap["name"], "kind": "rebuild_war",
                "severity": "med", "snap": snap,
                "detection": {"n": n, "rcr": snap.get("resetter_combat_ratio")},
            })
            flagged_this_run = True
        eco_cluster = (burst and burst_dir == "eco") or eco_intent
        if (eco_cluster and cid not in active_war_ids
                and snap["combat_ratio"] > ECO_INTENT_SATURATED):
            n = (burst["current"] if (burst and burst_dir == "eco")
                 else eco_intent["eco_resets"])
            flagged.append({
                "cid": cid, "name": snap["name"], "kind": "rebuild_eco",
                "severity": "med", "snap": snap,
                "detection": {"n": n, "rcr": snap.get("resetter_combat_ratio")},
            })
            flagged_this_run = True

        # ---- Track peak ratio while flagged for the stand-down detector ----
        if flagged_this_run:
            if new_country["last_flagged_at"] is None:
                new_country["last_flagged_at"] = now.isoformat()
                new_country["last_flagged_peak_ratio"] = snap["combat_ratio"]
            else:
                prev_peak = new_country.get("last_flagged_peak_ratio") or 0
                if snap["combat_ratio"] > prev_peak:
                    new_country["last_flagged_peak_ratio"] = snap["combat_ratio"]

        new_countries_state[cid] = new_country

    # ---- Classify previously-flagged countries that aren't this run ----
    prev_flagged_ids = set(state.get("flagged_last_run", []))
    # A country goes on the watchlist only for a med/high MOBILISATION flag.
    # Demob flags (collapse, eco_intent, eco_burst) never persist, so standing
    # down can't add a country to the watch; low-severity signals (yellow
    # creep) never persist either, so they can't generate a "no longer
    # flagged" line when they vanish.
    current_flagged_ids = {
        f["cid"] for f in flagged
        if f["kind"] in MOBILISATION_KINDS and f["severity"] in ("med", "high")
    }
    sampled_ids = set(snapshots.keys())
    no_longer_flagged_ids = (prev_flagged_ids & sampled_ids) - current_flagged_ids

    stood_down = []
    holding = []
    for cid in no_longer_flagged_ids:
        snap = snapshots[cid]
        prev_country = state.get("countries", {}).get(cid, {})
        classification, reason = classify_post_flag_state(
            cid, prev_country, snap, active_war_ids
        )
        name = (country_name.get(cid) or prev_country.get("name") or cid)

        if classification == "stood_down":
            stood_down.append({
                "cid": cid,
                "name": name,
                "reason": reason,
                "current_ratio": snap["combat_ratio"],
                "peak_ratio": prev_country.get("last_flagged_peak_ratio"),
            })
            new_countries_state[cid]["last_flagged_at"] = None
            new_countries_state[cid]["last_flagged_peak_ratio"] = None
        elif classification == "holding":
            peak = prev_country.get("last_flagged_peak_ratio") or snap["combat_ratio"]
            peak_date = "unknown"
            flagged_at = parse_iso(prev_country.get("last_flagged_at"))
            if flagged_at:
                peak_date = flagged_at.strftime("%b %-d")
            holding.append({
                "cid": cid,
                "name": name,
                "current_ratio": snap["combat_ratio"],
                "peak_ratio": peak,
                "peak_date": peak_date,
                "reason": reason,
            })
            # Stay in flagged_last_run so we keep watching, even though
            # holding no longer triggers the per-run update.
            current_flagged_ids.add(cid)

    # ---- Posture overview: once per UTC day, on the first run at or after
    # 20:00 (8pm UTC; the workflow has a dedicated 20:00 cron so it fires then,
    # not at the next 3-hourly slot). Tracked by date so a manual
    # workflow_dispatch can't double-post, and only recorded if the post
    # succeeds so a failed webhook retries next run. Carries the daily
    # "all quiet" heartbeat. ----
    today = now.strftime("%Y-%m-%d")
    if now.hour >= 20 and state.get("last_posture_date") != today:
        if send_posture_digest(snapshots, state, active_war_ids, country_name,
                               watchlist, holding, flagged, stood_down, now,
                               details=details):
            state["last_posture_date"] = today

    # Per-run update posts only on fresh flags or stand-downs, never on
    # holding alone.
    if flagged or stood_down:
        send_digest(flagged, stood_down, now)

    state["version"] = STATE_VERSION
    state["last_run"] = now.isoformat()
    state["countries"] = new_countries_state
    state["flagged_last_run"] = sorted(current_flagged_ids)
    save_state(state)

    mob_count = sum(
        1 for f in flagged if f["kind"] in MOBILISATION_KINDS
    )
    demob_count = sum(
        1 for f in flagged if f["kind"] in DEMOB_KINDS
    )
    print(
        f"Done. Snapshots: {len(snapshots)}. "
        f"Flagged: {len(flagged)} ({mob_count} escalating, {demob_count} de-escalating). "
        f"Holding: {len(holding)}. Stood down: {len(stood_down)}."
    )


if __name__ == "__main__":
    main()