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
STATE_VERSION = 8
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
HIGH_DEMOB_FOR_ALERT = 50.0
DEMOB_RESET_INTENT = 30.0

# ---- Military activity signal (from country.getCountryById rankings) ----
# Co-equal with skill focus. Skill focus is a *leading* indicator (who is
# rebuilding for combat before fighting starts); weekly damage is the
# *concurrent* indicator (who is actually fighting hard right now). The two
# disagree by design: an economy-built country can top the damage tables by
# fighting through gear/rank, which is exactly why skill focus alone mislabels
# the real military powers (e.g. Netherlands/Germany) as "economy-focused".
WEEKLY_DAMAGE_JUMP_MIN = 0.60   # >= +60% wk-over-wk vs the 7d-ago point = surge
WEEKLY_DAMAGE_DROP_MIN = 0.50   # <= -50% = damage falling off (de-escalation)
WEEKLY_DAMAGE_FLOOR = 500_000   # ignore shifts below this absolute weekly damage
                                # (small countries swing wildly in % terms)
WEEKLY_DAMAGE_HIGH_RANK = 25    # top-N weekly-damage rank counts as a heavyweight

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

HIGH_SEVERITY_FACTOR = 1.5
HIGH_SEVERITY_FLOOR = 20

URGENT_COOLDOWN_DAYS = 3
URGENT_ESCALATION_FACTOR = 1.5

# Per-run digest dedup: a collapse/creep already announced is only re-rendered
# if its magnitude has grown by at least this factor (further escalation).
DIGEST_REFRESH_FACTOR = 1.5

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
COLOUR_RESET_BURST = 0xED4245
COLOUR_RATIO_CREEP = 0xFEE75C
COLOUR_DEMOB = 0x57F287
COLOUR_HOLDING = 0xFAA61A          # orange, informational, not green
COLOUR_DIGEST = 0x5865F2
COLOUR_HEALTH_WARN = 0xFEE75C
COLOUR_HEALTH_CRIT = 0xED4245

SPARKLINE_BARS = "▁▂▃▄▅▆▇█"

COMBAT_SKILLS = {
    "attack", "precision", "dodge", "armor", "lootChance",
    "criticalChance", "criticalDamages", "health",
}
ECO_SKILLS = {
    "companies", "entrepreneurship", "production", "management",
}

# Flag taxonomy. A MOBILISATION flag puts a country on the watchlist
# (flagged_last_run); a DEMOB flag never does, since standing down shouldn't
# add a country to the mobilisation watch. Severity (high/med/low) is derived
# per detection, never hardcoded per call site. A 2+ player reset cluster
# (combat_intent / eco_intent) is "med" in either direction; the two are
# symmetric.
MOBILISATION_KINDS = {"burst", "combat_intent", "creep", "war_declared", "damage_surge"}
DEMOB_KINDS = {"collapse", "eco_burst", "eco_intent", "war_ended", "damage_drop"}
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


def sparkline(values):
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if lo == hi:
        return SPARKLINE_BARS[0] * len(values)
    rng = hi - lo
    n = len(SPARKLINE_BARS) - 1
    return "".join(
        SPARKLINE_BARS[min(int((v - lo) / rng * n), n)] for v in values
    )


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


def detect_damage_shift(history, current_weekly, now):
    """Week-over-week change in weekly damage dealt, against the 7d-ago point.

    weeklyCountryDamages is a rolling 7-day total, so comparing it to the value
    7 days ago is a clean week-over-week delta. Gated by an absolute floor so
    small countries (which swing wildly in percentage terms) don't trip it.
    """
    if current_weekly is None:
        return None
    pt = _find_history_point(
        history, now - timedelta(days=RATIO_LOOKBACK_DAYS), now,
        RATIO_LOOKBACK_DAYS - 1,
    )
    if pt is None:
        return None
    old = pt.get("weekly_damage")
    if not old or old <= 0:
        return None
    if max(old, current_weekly) < WEEKLY_DAMAGE_FLOOR:
        return None
    pct = (current_weekly - old) / old
    if pct >= WEEKLY_DAMAGE_JUMP_MIN:
        return {"direction": "surge", "old": old, "current": current_weekly,
                "pct": round(pct * 100)}
    if pct <= -WEEKLY_DAMAGE_DROP_MIN:
        return {"direction": "drop", "old": old, "current": current_weekly,
                "pct": round(pct * 100)}
    return None


def is_high_severity_burst(burst):
    threshold = burst.get("threshold") or RESET_FLOOR
    return burst["current"] >= max(threshold * HIGH_SEVERITY_FACTOR, HIGH_SEVERITY_FLOOR)


def is_high_severity_demob(collapse):
    return abs(collapse["delta"]) >= HIGH_DEMOB_FOR_ALERT


def should_send_urgent(prev_country, current_value, key_alert, key_count):
    last_iso = prev_country.get(key_alert)
    if not last_iso:
        return True
    last = parse_iso(last_iso)
    if last is None:
        return True
    now = datetime.now(timezone.utc)
    if (now - last).days >= URGENT_COOLDOWN_DAYS:
        return True
    last_count = prev_country.get(key_count) or 0
    return current_value >= last_count * URGENT_ESCALATION_FACTOR


def _digest_already_announced(prev_country, key_delta, current_delta):
    """True if this collapse/creep was already announced in the per-run update
    and hasn't materially grown since.

    Stops a lingering 7d comparison point from re-announcing the same shift
    every run. A new signal (no prior delta) or one that has escalated by at
    least DIGEST_REFRESH_FACTOR counts as fresh.
    """
    last = prev_country.get(key_delta)
    if last is None:
        return False
    return abs(current_delta) < abs(last) * DIGEST_REFRESH_FACTOR


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


def trend_field(history, snap):
    if len(history) < 2:
        return "Not enough history yet."
    reset_series = [h.get("new_resets", 0) for h in history] + [snap["new_resets"]]
    ratio_series = [h["combat_ratio"] for h in history] + [snap["combat_ratio"]]
    return (
        f"`{sparkline(reset_series)}` skill rebuilds per check "
        f"({reset_series[0]} -> {reset_series[-1]})\n"
        f"`{sparkline(ratio_series)}` typical player's combat focus "
        f"({ratio_series[0]:.0f}% -> {ratio_series[-1]:.0f}%)"
    )


def _humanise_baseline(mean_per_run):
    if mean_per_run < 0.15:
        return "almost never"
    if mean_per_run < 0.55:
        return f"about 1 every {round(1 / max(mean_per_run, 0.01))} days"
    if mean_per_run < 1.3:
        return "about 1 per day"
    if mean_per_run < 2.0:
        return "1-2 per day"
    if mean_per_run < 3.5:
        return f"{int(mean_per_run)}-{int(mean_per_run) + 1} per day"
    return f"around {round(mean_per_run)} per day"


def _resetter_summary(snap):
    rcr = snap.get("resetter_combat_ratio")
    if rcr is None:
        return None
    combat_resets = snap.get("combat_resets", 0)
    eco_resets = snap.get("eco_resets", 0)

    parts = []
    if combat_resets >= 1:
        people = "1 of them" if combat_resets == 1 else f"{combat_resets} of them"
        parts.append(f"{people} rebuilt into a combat build")
    elif eco_resets >= 1:
        people = "1 of them" if eco_resets == 1 else f"{eco_resets} of them"
        parts.append(f"{people} rebuilt into an economy build")

    if rcr >= COMBAT_INTENT:
        descriptor = "heavily combat-focused"
    elif rcr >= 50:
        descriptor = "combat-leaning"
    elif rcr > DEMOB_RESET_INTENT:
        descriptor = "balanced, slightly economy-leaning"
    else:
        descriptor = "heavily economy-focused"

    parts.append(f"the typical rebuild is {descriptor} ({rcr:.0f}% combat)")
    return ", ".join(parts) + "."


def send_high_severity_burst(country_name, snap, burst, history):
    n = burst["current"]
    sample_size = snap["sample_size"]
    pct = (n / sample_size) * 100 if sample_size else 0

    if burst.get("baseline_mean") is not None:
        normal_phrase = _humanise_baseline(burst["baseline_mean"])
        baseline_clause = f"This country normally sees {normal_phrase}."
    elif burst.get("reason") == "no_baseline_floor":
        baseline_clause = (
            f"Not enough history yet to know what's normal, but any single "
            f"check with {NO_BASELINE_RESET_FLOOR}+ rebuilds in the sample is "
            f"unusual."
        )
    else:
        baseline_clause = ""

    description = (
        f"**{n} of the top {sample_size} players** ({pct:.0f}%) wiped and "
        f"rebuilt their skills since the last check, and they rebuilt for "
        f"combat. Rebuilding costs gold, so a cluster this size is a "
        f"concrete sign of war prep. {baseline_clause}"
    ).strip()

    fields = [
        {
            "name": "Who this tracks",
            "value": (
                f"The top **{sample_size}** most active high-level players in "
                f"this country (level {MIN_LEVEL}+, online in the last "
                f"{ACTIVITY_WINDOW_DAYS} days)."
            ),
            "inline": False,
        },
        {
            "name": "Where they stand now",
            "value": (
                f"The typical one of these players has put **{snap['combat_ratio']:.0f}%** "
                f"of their skill points into combat "
                f"(the other **{100 - snap['combat_ratio']:.0f}%** on economy)."
            ),
            "inline": False,
        },
    ]

    resetter_line = _resetter_summary(snap)
    if resetter_line:
        fields.append({
            "name": "What the rebuilders chose",
            "value": resetter_line,
            "inline": False,
        })

    fields.append({
        "name": "Trend",
        "value": trend_field(history, snap),
        "inline": False,
    })

    embed = {
        "title": f"⚠️ War Preparation Detected: {country_name}",
        "color": COLOUR_RESET_BURST,
        "description": description,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return safe_post(embed, f"burst alert for {country_name}")


def send_high_severity_demob(country_name, snap, collapse, history):
    window = collapse["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"

    description = (
        f"The top players in **{country_name}** are clearly easing off war. "
        f"Their typical combat focus dropped from **{collapse['old_ratio']:.0f}% "
        f"to {snap['combat_ratio']:.0f}%** over {window_text}, moving skill "
        f"points back into economy. This usually means a campaign is wrapping "
        f"up. **Likely no longer an immediate threat.**"
    )

    fields = [
        {
            "name": "Who this tracks",
            "value": (
                f"The top **{snap['sample_size']}** most active high-level players "
                f"in {country_name} (level {MIN_LEVEL}+, online in the last "
                f"{ACTIVITY_WINDOW_DAYS} days)."
            ),
            "inline": False,
        },
        {
            "name": "Where they stand now",
            "value": (
                f"The typical one of these players now has **{snap['combat_ratio']:.0f}%** "
                f"of their skill points in combat "
                f"(the other **{100 - snap['combat_ratio']:.0f}%** on economy)."
            ),
            "inline": False,
        },
    ]

    resetter_line = _resetter_summary(snap)
    if resetter_line:
        fields.append({
            "name": "What the rebuilders chose",
            "value": resetter_line,
            "inline": False,
        })

    fields.append({
        "name": "Trend",
        "value": trend_field(history, snap),
        "inline": False,
    })

    embed = {
        "title": f"🕊️ Standing Down: {country_name}",
        "color": COLOUR_DEMOB,
        "description": description,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return safe_post(embed, f"demob alert for {country_name}")


def send_high_severity_creep(country_name, snap, creep, history):
    window = creep["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"
    gain = creep["delta"]

    description = (
        f"The top players in **{country_name}** have made a big move toward "
        f"combat. Their typical combat focus went from **{creep['old_ratio']:.0f}% "
        f"to {snap['combat_ratio']:.0f}%** over {window_text} (a {gain:.0f}-point "
        f"jump). This looks like a finished rebuild rather than today's activity, "
        f"so they may already be war-ready rather than still preparing."
    )

    fields = [
        {
            "name": "Who this tracks",
            "value": (
                f"The top **{snap['sample_size']}** most active high-level players "
                f"in {country_name} (level {MIN_LEVEL}+, online in the last "
                f"{ACTIVITY_WINDOW_DAYS} days)."
            ),
            "inline": False,
        },
        {
            "name": "Fresh rebuilds this check",
            "value": (
                f"**{snap['new_resets']}** skill rebuild(s) in the latest check. "
                f"A shift this big with little fresh rebuild activity means most "
                f"of it happened over the preceding days."
            ),
            "inline": False,
        },
        {
            "name": "Trend",
            "value": trend_field(history, snap),
            "inline": False,
        },
    ]

    embed = {
        "title": f"⚠️ Major Combat Shift: {country_name}",
        "color": COLOUR_RESET_BURST,
        "description": description,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return safe_post(embed, f"red-tier creep alert for {country_name}")


# ---------- Digest line renderers ----------

def _digest_line_burst(name, snap, burst, severity):
    icon = "🔴" if severity == "high" else "🟠"
    n = burst["current"]
    sample = snap["sample_size"]
    pct = (n / sample) * 100 if sample else 0
    rcr = snap.get("resetter_combat_ratio")
    combat_resets = snap.get("combat_resets", 0)

    main = (
        f"**{n} of the top {sample} players** ({pct:.0f}%) wiped and rebuilt "
        f"their skills since the last check"
    )
    if combat_resets >= 1 and rcr is not None and rcr >= COMBAT_INTENT:
        builds = "a combat build" if combat_resets == 1 else "combat builds"
        main += (
            f". {combat_resets} of them rebuilt into {builds}, putting "
            f"{rcr:.0f}% of their points into combat. Rebuilds cost gold, "
            f"so this is a concrete sign of war prep."
        )
    elif rcr is not None:
        main += (
            f". The ones who rebuilt put about {rcr:.0f}% of their points "
            f"into combat, so the direction is mixed. Rebuilds cost gold, "
            f"so it's worth watching either way."
        )
    else:
        main += ". Rebuilds cost gold, so a cluster like this is worth watching."
    return icon, name, main


def _digest_line_eco_burst(name, snap, burst):
    icon = "🟢"
    n = burst["current"]
    sample = snap["sample_size"]
    pct = (n / sample) * 100 if sample else 0
    rcr = snap.get("resetter_combat_ratio")

    main = (
        f"**{n} of the top {sample} players** ({pct:.0f}%) wiped and rebuilt "
        f"their skills since the last check, and they moved toward economy, "
        f"not war"
    )
    if rcr is not None:
        main += (
            f". The ones who rebuilt now spend only {rcr:.0f}% of their points "
            f"on combat. Good news for Ireland: this points to winding down, "
            f"not gearing up."
        )
    else:
        main += ". Good news for Ireland: winding down, not gearing up."
    return icon, name, main


def _digest_line_combat_intent(name, snap, intent):
    icon = "🟠"
    n = intent["combat_resets"]
    rcr = intent["resetter_combat_ratio"]
    main = (
        f"{n} of this country's top players have just rebuilt into combat "
        f"builds, each now putting about {rcr:.0f}% of their skill points "
        f"on combat. Several people moving the same way is an early sign of "
        f"mobilisation."
    )
    return icon, name, main


def _digest_line_creep(name, snap, creep):
    tier = creep.get("tier", "yellow")
    icon = {"red": "🔴", "orange": "🟠", "yellow": "🟡"}[tier]
    label = {
        "red": "Major combat shift (likely now war-ready)",
        "orange": "Significant combat shift (mobilising)",
        "yellow": "Drifting toward combat (worth watching)",
    }[tier]
    window = creep["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"
    main = (
        f"{label}: the typical top player's combat focus climbed from "
        f"**{creep['old_ratio']:.0f}% to {snap['combat_ratio']:.0f}%** "
        f"over {window_text}."
    )
    return icon, name, main


def _digest_line_collapse(name, snap, collapse):
    icon = "🟢"
    tier = collapse.get("tier", "green_light")
    label = {
        "green_strong": "Strong stand-down (campaign likely ending)",
        "green_med": "Standing down (de-escalating)",
        "green_light": "Easing off combat (early stand-down sign)",
    }[tier]
    window = collapse["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"
    main = (
        f"{label}: the typical top player's combat focus fell from "
        f"**{collapse['old_ratio']:.0f}% to {snap['combat_ratio']:.0f}%** "
        f"over {window_text}."
    )
    return icon, name, main


def _digest_line_eco_intent(name, snap, intent):
    icon = "🟢"
    n = intent["eco_resets"]
    rcr = intent["resetter_combat_ratio"]
    main = (
        f"{n} of this country's top players have just rebuilt into economy "
        f"builds, each now putting only {rcr:.0f}% of their skill points on "
        f"combat (the rest on economy). People moving back to economy is an "
        f"early sign they're standing down."
    )
    return icon, name, main


def _digest_line_war_declared(name, snap, det):
    names = det.get("new_war_names", [])
    targets = ", ".join(names)
    if det.get("against_ireland"):
        icon = "🔴"
        extra = f" (also opened war on {targets})" if len(names) > 1 else ""
        main = (
            f"**Declared war on Ireland.**{extra} This is a live war footing "
            f"against us, not a skill-build guess."
        )
    else:
        icon = "🟠"
        war_word = "a new war" if len(names) == 1 else f"{len(names)} new wars"
        main = (
            f"Opened {war_word} against {targets}. A bordering country going on "
            f"the offensive is an active mobilisation signal."
        )
    return icon, name, main


def _digest_line_war_ended(name, snap, det):
    names = det.get("ended_war_names", [])
    targets = ", ".join(names)
    war_word = "a war" if len(names) == 1 else f"{len(names)} wars"
    main = (
        f"Ended {war_word} with {targets}. One fewer active front, an early "
        f"sign of winding down."
    )
    return "🟢", name, main


def _digest_line_damage_surge(name, snap, det):
    rank = det.get("rank")
    heavyweight = rank is not None and rank <= WEEKLY_DAMAGE_HIGH_RANK
    icon = "🔴" if heavyweight else "🟠"
    rank_str = f", now **#{rank}** in the game for weekly damage" if rank else ""
    main = (
        f"Weekly combat damage jumped **+{det['pct']}%** vs the previous week"
        f"{rank_str}. They are fighting markedly harder right now, whatever "
        f"their skill builds read."
    )
    return icon, name, main


def _digest_line_damage_drop(name, snap, det):
    main = (
        f"Weekly combat damage fell **{abs(det['pct'])}%** vs the previous "
        f"week. Actual military activity is easing off."
    )
    return "🟢", name, main


# ---------- Digest assembly with rollup ----------

def _is_low_severity(flag):
    # "Minor activity" = soft signals listed for awareness, not action: a yellow
    # creep, or a reset cluster in an already-saturated country (downgraded to
    # "low" at flag time because it's reinforcement, not fresh mobilisation).
    if flag["kind"] == "creep":
        return flag["detection"].get("tier") == "yellow"
    if flag["kind"] in ("combat_intent", "eco_intent"):
        return flag["severity"] == "low"
    return False


def _minor_activity_label(flag):
    name = flag["name"]
    if flag["kind"] == "creep":
        return f"{name} (drifting toward combat)"
    if flag["kind"] == "combat_intent":
        return f"{name} (combat rebuilds, already war-ready)"
    if flag["kind"] == "eco_intent":
        return f"{name} (economy rebuilds, already economy-built)"
    return name


def send_digest(flagged, stood_down, now):
    """Per-run update roll-up. Posts whenever there's a fresh flag or a
    stand-down this run. "Holding at high readiness" is intentionally NOT
    here, it lives in the daily posture report, so a country that stays
    mobilised but quiet doesn't re-trigger this every 3 hours.

    Collapse/creep lines carry a "fresh" flag; stale ones (already announced,
    not escalated) are suppressed so the same shift isn't repeated every run
    while its 7-day comparison point lingers.
    """
    full_line_by_cid = {}
    for f in flagged:
        if not _is_low_severity(f):
            existing = full_line_by_cid.get(f["cid"])
            if existing is None or _severity_rank(f) < _severity_rank(existing):
                full_line_by_cid[f["cid"]] = f

    minor_only = [
        f for f in flagged
        if f["cid"] not in full_line_by_cid and _is_low_severity(f)
    ]
    seen_cids = set()
    minor_dedup = []
    minor_priority = {"combat_intent": 0, "eco_intent": 0, "creep": 1}
    for f in sorted(minor_only, key=lambda x: minor_priority.get(x["kind"], 9)):
        if f["cid"] in seen_cids:
            continue
        seen_cids.add(f["cid"])
        minor_dedup.append(f)

    full_lines = sorted(full_line_by_cid.values(), key=_severity_rank)

    line_renderers = {
        "burst": lambda f: _digest_line_burst(
            f["name"], f["snap"], f["detection"], f["severity"]
        ),
        "eco_burst": lambda f: _digest_line_eco_burst(
            f["name"], f["snap"], f["detection"]
        ),
        "combat_intent": lambda f: _digest_line_combat_intent(
            f["name"], f["snap"], f["detection"]
        ),
        "creep": lambda f: _digest_line_creep(
            f["name"], f["snap"], f["detection"]
        ),
        "collapse": lambda f: _digest_line_collapse(
            f["name"], f["snap"], f["detection"]
        ),
        "eco_intent": lambda f: _digest_line_eco_intent(
            f["name"], f["snap"], f["detection"]
        ),
        "war_declared": lambda f: _digest_line_war_declared(
            f["name"], f["snap"], f["detection"]
        ),
        "war_ended": lambda f: _digest_line_war_ended(
            f["name"], f["snap"], f["detection"]
        ),
        "damage_surge": lambda f: _digest_line_damage_surge(
            f["name"], f["snap"], f["detection"]
        ),
        "damage_drop": lambda f: _digest_line_damage_drop(
            f["name"], f["snap"], f["detection"]
        ),
    }

    fields = []
    for f in full_lines[:25]:
        renderer = line_renderers.get(f["kind"])
        if not renderer:
            continue
        # Suppress collapse/creep lines already announced and not materially
        # escalated, so the same de-escalation isn't repeated every run while
        # the 7-day comparison point lingers.
        if f["kind"] in ("collapse", "creep") and not f.get("fresh", True):
            continue
        icon, name, value = renderer(f)
        fields.append({
            "name": f"{icon} {name}",
            "value": value,
            "inline": False,
        })

    if minor_dedup:
        labels = [_minor_activity_label(f) for f in minor_dedup[:12]]
        more = ""
        if len(minor_dedup) > 12:
            more = f" (+{len(minor_dedup) - 12} more)"
        fields.append({
            "name": "🟡 Minor activity",
            "value": (
                "_Soft signals, individually below threshold, listed "
                "for awareness, not action._\n"
                + ", ".join(labels) + more
            ),
            "inline": False,
        })

    if stood_down:
        stood_down_lines = []
        by_reason = {"ratio_dropped": [], "retired_no_data": []}
        for s in stood_down:
            by_reason.setdefault(s["reason"], []).append(s)

        for s in by_reason.get("ratio_dropped", []):
            if s.get("peak_ratio") is not None:
                stood_down_lines.append(
                    f"**{s['name']}** · combat focus dropped "
                    f"{s['peak_ratio']:.0f}% to {s['current_ratio']:.0f}% "
                    f"(de-escalating)"
                )
            else:
                stood_down_lines.append(
                    f"**{s['name']}** · de-escalating "
                    f"(now {s['current_ratio']:.0f}% combat focus)"
                )

        retired = by_reason.get("retired_no_data", [])
        if retired:
            names = ", ".join(s["name"] for s in retired)
            stood_down_lines.append(
                f"{names} · previous flag retired "
                f"(was based on signals that no longer qualify)"
            )

        fields.append({
            "name": "✅ No longer flagged",
            "value": (
                "_Countries removed from watch this run, with reasons._\n"
                + "\n".join(stood_down_lines)
            ),
            "inline": False,
        })

    counts_mob = {"high": 0, "med": 0}
    counts_demob = 0
    for f in full_lines:
        if f["kind"] in DEMOB_KINDS:
            counts_demob += 1
        else:
            sev = f["severity"]
            if sev in counts_mob:
                counts_mob[sev] = counts_mob.get(sev, 0) + 1
    parts = []
    if counts_mob["high"]:
        parts.append(f"**{counts_mob['high']}** urgent")
    if counts_mob["med"]:
        parts.append(f"**{counts_mob['med']}** preparing")
    if minor_dedup:
        parts.append(f"**{len(minor_dedup)}** minor")
    if counts_demob:
        parts.append(f"**{counts_demob}** standing down")
    if stood_down:
        parts.append(f"**{len(stood_down)}** no longer flagged")
    summary = ", ".join(parts) if parts else None

    intro = (
        f"Risks and de-escalations since the last check. Signals come from a "
        f"country's actual war activity (wars declared/ended, weekly combat "
        f"damage) and from its top ~{SAMPLE_TOP_N} active players' skill builds "
        f"(combat vs economy — a leading indicator that lags real fighting).\n\n"
    )

    total_items = len(full_lines) + len(minor_dedup) + len(stood_down)
    if total_items:
        body = f"{summary} ({total_items} total)." if summary else f"{total_items} items."
        description = intro + body
    else:
        description = intro + "All quiet."

    embed = {
        "title": "🛡️ War Watch · Update",
        "color": COLOUR_DIGEST,
        "description": description,
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    return safe_post(embed, "update")


def _severity_rank(f):
    if f["kind"] in DEMOB_KINDS:
        return (10, f["name"])
    rank = {"high": 0, "med": 1, "low": 2}.get(f["severity"], 9)
    return (rank, f["name"])


# ---------- Posture overview ----------

def _build_word(ratio):
    """Plain-word label for a country's skill build, so a fighting-but-economy
    country reads as "economy build · #11 dmg" instead of a jarring "0%"."""
    if ratio >= POSTURE_WAR:
        return "combat"
    if ratio >= POSTURE_LEAN:
        return "combat-leaning"
    if ratio >= POSTURE_MIXED:
        return "mixed"
    return "economy"


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
    rows = []
    for cid, snap in snapshots.items():
        ratio = snap["combat_ratio"]
        history = state.get("countries", {}).get(cid, {}).get("history", [])
        shift = _ratio_shift_7d(history, ratio, now)
        det = snap.get("detail") or details.get(cid) or {}
        rows.append({
            "cid": cid,
            "name": snap["name"],
            "ratio": ratio,
            "shift_7d": shift,
            "at_war": cid in active_war_ids,
            "weekly_rank": det.get("weekly_damage_rank"),
        })

    monitored = len(watchlist) if watchlist else len(rows)
    sampled = len(rows)
    skipped = sorted(
        ((country_name.get(cid) or cid), (details.get(cid) or {}))
        for cid in (watchlist or {})
        if cid not in snapshots
    )

    if not rows:
        embed = {
            "title": "📊 War Watch · Daily Posture Report",
            "color": COLOUR_DIGEST,
            "description": (
                f"Monitoring **{monitored}** countries, but none could be "
                f"sampled this run, so there's no posture data to show. This "
                f"usually means the data proxy was down during the run."
            ),
            "timestamp": now.isoformat(),
        }
        return safe_post(embed, "posture digest")

    holding_by_cid = {h["cid"]: h for h in (holding or []) if h.get("cid")}

    def _roster_line(r, at_war=False):
        rank = f"#{r['weekly_rank']} dmg" if r.get("weekly_rank") else "dmg rank n/a"
        parts = [f"**{r['name']}**", rank, f"{_build_word(r['ratio'])} build"]
        sh = r.get("shift_7d")
        if not at_war and sh is not None and abs(sh) >= POSTURE_MOVER_MIN:
            was = r["ratio"] - sh
            arrow = "📈" if sh > 0 else "📉"
            parts.append(f"{arrow} {was:.0f}→{r['ratio']:.0f}%")
        if not at_war and r["cid"] in holding_by_cid:
            parts.append("holding")
        return " · ".join(parts)

    # At-war countries appear ONLY in their callout; everyone else appears once
    # in the roster below, sorted by recent combat output.
    at_war_rows = sorted(
        (r for r in rows if r["at_war"]),
        key=lambda r: (r["weekly_rank"] or 9999),
    )
    others = sorted(
        (r for r in rows if not r["at_war"]),
        key=lambda r: (r["weekly_rank"] is None, r["weekly_rank"] or 0, -r["ratio"]),
    )

    # Heartbeat / activity status line
    mob = sum(1 for f in flagged if f["kind"] in MOBILISATION_KINDS)
    demob = sum(1 for f in flagged if f["kind"] in DEMOB_KINDS)
    bits = []
    if mob:
        bits.append(f"**{mob}** mobilising")
    if demob:
        bits.append(f"**{demob}** standing down")
    if stood_down:
        bits.append(f"**{len(stood_down)}** no longer flagged")
    if holding:
        bits.append(f"**{len(holding)}** holding")
    if bits:
        status_line = "Today: " + ", ".join(bits) + ".\n\n"
    else:
        status_line = "✅ All quiet today — no mobilisation or stand-down signals.\n\n"

    scope = (
        f"all **{monitored}** monitored countries"
        if sampled == monitored
        else f"**{sampled} of {monitored}** monitored countries"
    )
    description = (
        status_line
        + f"Read {scope}, sorted by recent combat output. _Damage rank = how hard "
        f"they're actually fighting; build = how the top players are skilled, a "
        f"leading hint that lags real fighting._"
    )

    fields = []

    if at_war_rows:
        fields.append({
            "name": f"⚔️ At war with Ireland   ({len(at_war_rows)})",
            "value": "\n".join(_roster_line(r, at_war=True) for r in at_war_rows),
            "inline": False,
        })

    if others:
        shown = others[:25]
        lines = "\n".join(_roster_line(r) for r in shown)
        if len(others) > 25:
            lines += f"\n_…and {len(others) - 25} more_"
        fields.append({
            "name": f"📊 Watchlist by combat output   ({len(others)})",
            "value": lines,
            "inline": False,
        })

    if skipped:
        def _skip_label(item):
            nm, det = item
            rank = det.get("weekly_damage_rank")
            return f"{nm} (#{rank} dmg)" if rank else nm
        shown = ", ".join(_skip_label(s) for s in skipped[:15])
        more = f" (+{len(skipped) - 15} more)" if len(skipped) > 15 else ""
        fields.append({
            "name": f"⚪ Couldn't read · too few active players   ({len(skipped)})",
            "value": shown + more,
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
        "title": "🚨 War Watch · Critical" if critical else "⚠️ War Watch · Degraded",
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
                f"Monitoring gap: the previous run completed **{gap_h:.0f} hours "
                f"ago**. Runs are scheduled every 3 hours, so one or more were "
                f"missed. This run is proceeding normally; just flagging the gap."
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
            "Watchlist came back empty. Either Ireland's region/diplomatic data "
            "is unavailable or the API is failing. No sampling performed this run.",
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
                f"Only **{len(snapshots)}/{len(watchlist)}** watchlisted countries "
                f"could be sampled this run ({snapshot_rate:.0%}). The proxy may "
                f"be degraded or playerbases may have dropped below thresholds."
            )

    flagged = []
    high_sev_sent = 0
    demob_sent = 0
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
        damage_shift = detect_damage_shift(history, weekly_damage, now)

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
            "last_urgent_alert": prev_country.get("last_urgent_alert"),
            "last_urgent_count": prev_country.get("last_urgent_count"),
            "last_demob_alert": prev_country.get("last_demob_alert"),
            "last_demob_delta": prev_country.get("last_demob_delta"),
            "last_creep_alert": prev_country.get("last_creep_alert"),
            "last_creep_delta": prev_country.get("last_creep_delta"),
            "last_flagged_at": prev_country.get("last_flagged_at"),
            "last_flagged_peak_ratio": prev_country.get("last_flagged_peak_ratio"),
            "last_digest_creep_delta": prev_country.get("last_digest_creep_delta"),
            "last_digest_demob_delta": prev_country.get("last_digest_demob_delta"),
            "wars_with": current_wars if current_wars is not None else prev_wars,
            "weekly_damage": weekly_damage,
            "weekly_damage_rank": detail.get("weekly_damage_rank"),
            "total_damage_rank": detail.get("total_damage_rank"),
            "active_pop": detail.get("active_pop"),
            "history": new_history,
        }

        # ---- Mobilisation flagging ----
        # Combat or mixed bursts are war-prep signals. Economy-directed
        # bursts are handled on the demob side below.
        flagged_this_run = False

        if burst and burst_dir in ("combat", "mixed"):
            high = is_high_severity_burst(burst) and burst_dir == "combat"
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "burst",
                "severity": "high" if high else "med",
                "snap": snap,
                "detection": burst,
                "fresh": True,
            })
            flagged_this_run = True
            if high and should_send_urgent(
                prev_country, burst["current"],
                "last_urgent_alert", "last_urgent_count",
            ):
                if send_high_severity_burst(snap["name"], snap, burst, history):
                    high_sev_sent += 1
                    new_country["last_urgent_alert"] = now.isoformat()
                    new_country["last_urgent_count"] = burst["current"]
        elif combat_intent:
            intent_sev = (
                "low" if snap["combat_ratio"] >= COMBAT_INTENT_SATURATED
                else INTENT_SEVERITY
            )
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "combat_intent",
                "severity": intent_sev,
                "snap": snap,
                "detection": combat_intent,
                "fresh": True,
            })
            flagged_this_run = True

        if creep:
            sev = {"red": "high", "orange": "med", "yellow": "low"}[creep["tier"]]
            fresh = not _digest_already_announced(
                prev_country, "last_digest_creep_delta", creep["delta"]
            )
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "creep",
                "severity": sev,
                "snap": snap,
                "detection": creep,
                "fresh": fresh,
            })
            flagged_this_run = True
            new_country["last_digest_creep_delta"] = creep["delta"]
            if creep["tier"] == "red" and should_send_urgent(
                prev_country, creep["delta"],
                "last_creep_alert", "last_creep_delta",
            ):
                if send_high_severity_creep(snap["name"], snap, creep, history):
                    new_country["last_creep_alert"] = now.isoformat()
                    new_country["last_creep_delta"] = creep["delta"]
        else:
            # No creep this run: clear so a future re-occurrence counts fresh.
            new_country["last_digest_creep_delta"] = None

        # ---- Demobilisation flagging (suppressed for active wars) ----
        if cid not in active_war_ids:
            if collapse:
                high_demob = is_high_severity_demob(collapse)
                tier_sev = {
                    "green_strong": "med",
                    "green_med": "med",
                    "green_light": "low",
                }[collapse["tier"]]
                fresh = not _digest_already_announced(
                    prev_country, "last_digest_demob_delta", collapse["delta"]
                )
                flagged.append({
                    "cid": cid,
                    "name": snap["name"],
                    "kind": "collapse",
                    "severity": tier_sev,
                    "snap": snap,
                    "detection": collapse,
                    "fresh": fresh,
                })
                flagged_this_run = True
                new_country["last_digest_demob_delta"] = collapse["delta"]
                if high_demob and should_send_urgent(
                    prev_country, abs(collapse["delta"]),
                    "last_demob_alert", "last_demob_delta",
                ):
                    if send_high_severity_demob(snap["name"], snap, collapse, history):
                        demob_sent += 1
                        new_country["last_demob_alert"] = now.isoformat()
                        new_country["last_demob_delta"] = abs(collapse["delta"])
            elif burst and burst_dir == "eco":
                eco_burst_high = is_high_severity_burst(burst)
                flagged.append({
                    "cid": cid,
                    "name": snap["name"],
                    "kind": "eco_burst",
                    "severity": "med" if eco_burst_high else "low",
                    "snap": snap,
                    "detection": burst,
                    "fresh": True,
                })
                flagged_this_run = True
            elif eco_intent:
                intent_sev = (
                    "low" if snap["combat_ratio"] <= ECO_INTENT_SATURATED
                    else INTENT_SEVERITY
                )
                flagged.append({
                    "cid": cid,
                    "name": snap["name"],
                    "kind": "eco_intent",
                    "severity": intent_sev,
                    "snap": snap,
                    "detection": eco_intent,
                    "fresh": True,
                })
                flagged_this_run = True

            if not collapse:
                new_country["last_digest_demob_delta"] = None
        else:
            # Active war: collapse suppressed, so clear stored demob delta.
            new_country["last_digest_demob_delta"] = None

        # ---- Military-activity flagging (independent of skill signals) ----
        # War-list and weekly-damage changes are the accurate, activity-based
        # signal that skill focus can't see. A war *declared* against Ireland is
        # the single most urgent thing this bot can report.
        if new_wars:
            against_ireland = IRELAND_COUNTRY_ID in new_wars
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "war_declared",
                "severity": "high" if against_ireland else "med",
                "snap": snap,
                "detection": {
                    "new_wars": new_wars,
                    "new_war_names": [country_name.get(c, c) for c in new_wars],
                    "against_ireland": against_ireland,
                },
                "fresh": True,
            })
            flagged_this_run = True
        if ended_wars:
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "war_ended",
                "severity": "med",
                "snap": snap,
                "detection": {
                    "ended_wars": ended_wars,
                    "ended_war_names": [country_name.get(c, c) for c in ended_wars],
                },
                "fresh": True,
            })
            flagged_this_run = True

        if damage_shift and damage_shift["direction"] == "surge":
            rank = detail.get("weekly_damage_rank")
            heavyweight = rank is not None and rank <= WEEKLY_DAMAGE_HIGH_RANK
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "damage_surge",
                "severity": "high" if heavyweight else "med",
                "snap": snap,
                "detection": {**damage_shift, "rank": rank},
                "fresh": True,
            })
            flagged_this_run = True
        elif damage_shift and damage_shift["direction"] == "drop":
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "damage_drop",
                "severity": "med",
                "snap": snap,
                "detection": {**damage_shift, "rank": detail.get("weekly_damage_rank")},
                "fresh": True,
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

    # ---- Posture overview: once per UTC day, on the first run at or
    # after 21:00 (the last scheduled 3-hourly run of the day). Tracked
    # by date so a manual workflow_dispatch can't double-post, and only
    # recorded if the post succeeds so a failed webhook retries next run.
    # Carries holding + the daily "all quiet" heartbeat. ----
    today = now.strftime("%Y-%m-%d")
    if now.hour >= 21 and state.get("last_posture_date") != today:
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
        f"Flagged: {len(flagged)} ({mob_count} mobilising, {demob_count} demobilising; "
        f"{high_sev_sent} urgent burst, {demob_sent} urgent demob sent). "
        f"Holding: {len(holding)}. Stood down: {len(stood_down)}."
    )


if __name__ == "__main__":
    main()