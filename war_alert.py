"""
War preparation alert bot.

Patch summary (this version):

  * FIX: _find_history_point now ages the matched point relative to
    `now`, not relative to `target`. The old version measured age from
    target (which is already now - 7d), so points near the 7-day mark
    came out with age ~0 and were rejected by the min_age guard. That
    silently killed every ratio creep / collapse / corroboration signal.
    Signature is now _find_history_point(history, target, now,
    min_age_days); all three call sites updated.

  * NEW: send_posture_digest — an informational overview sent each run
    showing how the watchlist splits between war-posture and
    economy-posture countries, average combat focus, per-bucket
    membership, and the biggest 7-day movers in each direction.

Earlier changes (unchanged here):

  1. ECO_INTENT requires eco_resets >= 2, OR eco_resets >= 1 with a
     corroborating ratio drop.
  2. COMBAT_INTENT tightened symmetrically.
  3. Stand-down detection compares against a recorded flagged peak;
     Norway-style plateaus report as "holding at high readiness".
  4. Low-severity items roll up into one "Minor activity" line.

State fields: last_flagged_peak_ratio, last_flagged_at. STATE_VERSION 6
with idempotent migration.
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
STATE_VERSION = 6
IRELAND_COUNTRY_ID = "6813b6d446e731854c7ac7fe"

# Watchlist scope
BORDER_HOPS = 3

# Sampling per country
ENUM_LIMIT = 100
MAX_PAGES = 15
SAMPLE_TOP_N = 25
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
RESET_FLOOR = 3
NO_BASELINE_RESET_FLOOR = 4
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

COMBAT_INTENT = 70.0

# Minimum resetter counts for intent signals
COMBAT_INTENT_MIN_RESETTERS = 2
ECO_INTENT_MIN_RESETTERS = 2
INTENT_CORROBORATION_RATIO = 5.0   # minimum 7d ratio shift in the same
                                    # direction to corroborate a single
                                    # resetter

HIGH_SEVERITY_FACTOR = 1.5
HIGH_SEVERITY_FLOOR = 10

URGENT_COOLDOWN_DAYS = 3
URGENT_ESCALATION_FACTOR = 1.5

# Stand-down gating
STAND_DOWN_RATIO_CEILING = 50.0
STAND_DOWN_DROP_MIN = 15.0

# Posture overview buckets (current combat focus)
POSTURE_WAR = 70.0      # heavily combat
POSTURE_LEAN = 50.0     # combat-leaning
POSTURE_BALANCED = 30.0 # mixed; below this is economy-focused
POSTURE_MOVER_MIN = 5.0 # minimum 7d shift to list as a mover

# Pipeline health
HEALTH_SNAPSHOT_RATE = 0.5

# Embed colours
COLOUR_RESET_BURST = 0xED4245
COLOUR_RATIO_CREEP = 0xFEE75C
COLOUR_DEMOB = 0x57F287
COLOUR_HOLDING = 0xFAA61A          # orange — informational, not green
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
            raise RuntimeError(f"{endpoint} → {msg[:120]}")
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


def find_border_countries(regions_obj, country_id, max_hops=None):
    if max_hops is None:
        max_hops = BORDER_HOPS

    own_ids = {
        r["_id"] for r in regions_obj.values()
        if isinstance(r, dict)
        and r.get("country") == country_id
        and r.get("_id")
    }
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
        regions_obj, IRELAND_COUNTRY_ID
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


def _find_history_point(history, target, now, min_age_days):
    """Return the history point closest to `target`, but only if it is at
    least `min_age_days` old relative to `now`.

    NB: age is measured from `now`, not from `target`. `target` is itself
    a past time (e.g. now - 7d), so ageing from target would make a point
    sitting right on the lookback mark look brand new and get rejected —
    which is exactly the bug that previously silenced all ratio signals.
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
    None. Used as corroboration for single-resetter intent and for the
    posture overview's mover lists.
    """
    pt = _find_history_point(
        history, now - timedelta(days=RATIO_LOOKBACK_DAYS), now,
        RATIO_LOOKBACK_DAYS - 1,
    )
    if pt is None:
        return None
    return current_ratio - pt["combat_ratio"]


def detect_combat_intent_resets(snap, history, now):
    """Returns dict if resets this run point at war prep.

    Fires when EITHER combat_resets >= COMBAT_INTENT_MIN_RESETTERS, OR
    combat_resets >= 1 with a corroborating upward 7d ratio shift.
    """
    combat_resets = snap.get("combat_resets", 0)
    rcr = snap.get("resetter_combat_ratio")
    if combat_resets < 1 or rcr is None or rcr < COMBAT_INTENT:
        return None

    if combat_resets >= COMBAT_INTENT_MIN_RESETTERS:
        return {
            "combat_resets": combat_resets,
            "resetter_combat_ratio": rcr,
            "new_resets": snap.get("new_resets", 0),
            "corroborated_by": "count",
        }

    shift = _ratio_shift_7d(history, snap["combat_ratio"], now)
    if shift is not None and shift >= INTENT_CORROBORATION_RATIO:
        return {
            "combat_resets": combat_resets,
            "resetter_combat_ratio": rcr,
            "new_resets": snap.get("new_resets", 0),
            "corroborated_by": "ratio_shift",
            "ratio_shift_7d": round(shift, 1),
        }

    return None


def detect_eco_intent_resets(snap, history, now):
    """Mirror of detect_combat_intent_resets for demobilisation."""
    eco_resets = snap.get("eco_resets", 0)
    rcr = snap.get("resetter_combat_ratio")
    if eco_resets < 1 or rcr is None or rcr > DEMOB_RESET_INTENT:
        return None

    if eco_resets >= ECO_INTENT_MIN_RESETTERS:
        return {
            "eco_resets": eco_resets,
            "resetter_combat_ratio": rcr,
            "new_resets": snap.get("new_resets", 0),
            "corroborated_by": "count",
        }

    shift = _ratio_shift_7d(history, snap["combat_ratio"], now)
    if shift is not None and shift <= -INTENT_CORROBORATION_RATIO:
        return {
            "eco_resets": eco_resets,
            "resetter_combat_ratio": rcr,
            "new_resets": snap.get("new_resets", 0),
            "corroborated_by": "ratio_shift",
            "ratio_shift_7d": round(shift, 1),
        }

    return None


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
        print("Migrated state v3 → v4.")

    if version < 5:
        for country in state.get("countries", {}).values():
            country.setdefault("combat_resets", 0)
            country.setdefault("eco_resets", 0)
            country.setdefault("last_demob_alert", None)
            country.setdefault("last_demob_delta", None)
            country.setdefault("last_creep_alert", None)
            country.setdefault("last_creep_delta", None)
        state["version"] = 5
        print("Migrated state v4 → v5.")

    if version < 6:
        for country in state.get("countries", {}).values():
            country.setdefault("last_flagged_at", None)
            country.setdefault("last_flagged_peak_ratio", None)
        state["version"] = 6
        print("Migrated state v5 → v6 (added flagged-peak tracking).")

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
        f"`{sparkline(reset_series)}` skill switches per day "
        f"({reset_series[0]} → {reset_series[-1]})\n"
        f"`{sparkline(ratio_series)}` typical citizen's combat focus "
        f"({ratio_series[0]:.0f}% → {ratio_series[-1]:.0f}%)"
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
    n = snap.get("new_resets", 0)

    parts = []
    if n >= 1:
        if combat_resets >= 1:
            parts.append(f"**{combat_resets} of {n}** rebuilt as a combat fighter")
        elif eco_resets >= 1:
            parts.append(f"**{eco_resets} of {n}** rebuilt as a worker")

    if rcr >= COMBAT_INTENT:
        descriptor = "heavily combat-focused"
    elif rcr >= 50:
        descriptor = "combat-leaning"
    elif rcr > DEMOB_RESET_INTENT:
        descriptor = "balanced, slightly economy-leaning"
    else:
        descriptor = "heavily economy-focused"

    parts.append(f"typical rebuild is {descriptor} ({rcr:.0f}% combat skills)")
    return " · ".join(parts)


def send_high_severity_burst(country_name, snap, burst, history):
    n = burst["current"]
    sample_size = snap["sample_size"]
    pct = (n / sample_size) * 100 if sample_size else 0

    if burst.get("baseline_mean") is not None:
        normal_phrase = _humanise_baseline(burst["baseline_mean"])
        baseline_clause = f"This country normally sees {normal_phrase}."
    elif burst.get("reason") == "no_baseline_floor":
        baseline_clause = (
            f"Not enough history yet to know what's normal — but any single "
            f"day with {NO_BASELINE_RESET_FLOOR}+ skill switches in a small "
            f"sample is unusual."
        )
    else:
        baseline_clause = ""

    description = (
        f"**{n} of {sample_size}** top citizens ({pct:.0f}%) wiped and rebuilt "
        f"their skills since yesterday's check. Skill switches cost gold, "
        f"so this kind of activity usually means people are repurposing "
        f"themselves — typically for combat. {baseline_clause}"
    ).strip()

    fields = [
        {
            "name": "Who we're watching",
            "value": (
                f"The top **{sample_size}** active fighters in this country "
                f"(level ≥ {MIN_LEVEL}, online in last {ACTIVITY_WINDOW_DAYS} days)."
            ),
            "inline": False,
        },
        {
            "name": "Current combat focus",
            "value": (
                f"The typical sampled citizen has spent **{snap['combat_ratio']:.0f}%** "
                f"of their skill points on combat skills "
                f"(vs. **{100 - snap['combat_ratio']:.0f}%** on economy)."
            ),
            "inline": False,
        },
    ]

    resetter_line = _resetter_summary(snap)
    if resetter_line:
        fields.append({
            "name": "What the people who switched are doing",
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
    drop = abs(collapse["delta"])
    window = collapse["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"

    description = (
        f"Top fighters in **{country_name}** are visibly de-escalating. Their "
        f"combat focus dropped from **{collapse['old_ratio']:.0f}% → "
        f"{snap['combat_ratio']:.0f}%** over {window_text} — they're shifting "
        f"skill points back to economy. This typically means a campaign is "
        f"ending and they're returning to peacetime activity. **Likely no "
        f"longer an immediate threat.**"
    )

    fields = [
        {
            "name": "Who we're watching",
            "value": (
                f"The top **{snap['sample_size']}** active fighters in "
                f"{country_name} (level ≥ {MIN_LEVEL}, online in last "
                f"{ACTIVITY_WINDOW_DAYS} days)."
            ),
            "inline": False,
        },
        {
            "name": "Current combat focus",
            "value": (
                f"The typical sampled citizen now has **{snap['combat_ratio']:.0f}%** "
                f"of their skill points on combat skills "
                f"(vs. **{100 - snap['combat_ratio']:.0f}%** on economy)."
            ),
            "inline": False,
        },
    ]

    resetter_line = _resetter_summary(snap)
    if resetter_line:
        fields.append({
            "name": "What the people who switched are doing",
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
        f"**{country_name}**'s top fighters have made a major shift toward "
        f"combat skills. The typical sampled citizen went from "
        f"**{creep['old_ratio']:.0f}% combat-focused → {snap['combat_ratio']:.0f}% "
        f"combat-focused** over {window_text} (a {gain:.0f}-point shift). "
        f"This is a completed rebuild, not necessarily today's activity — "
        f"so it may indicate they're now war-ready rather than preparing."
    )

    fields = [
        {
            "name": "Who we're watching",
            "value": (
                f"The top **{snap['sample_size']}** active fighters in "
                f"{country_name} (level ≥ {MIN_LEVEL}, online in last "
                f"{ACTIVITY_WINDOW_DAYS} days)."
            ),
            "inline": False,
        },
        {
            "name": "Recent activity",
            "value": (
                f"**{snap['new_resets']}** skill switches in the latest check. "
                f"A shift this large without much fresh reset activity usually "
                f"means the rebuild happened over the preceding days."
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

    main = f"**{n} of {sample}** top citizens ({pct:.0f}%) switched their builds"
    if combat_resets >= 1 and rcr is not None and rcr >= COMBAT_INTENT:
        main += (
            f" — **{combat_resets} of {n}** rebuilt as combat fighters "
            f"({rcr:.0f}% combat skills). Concrete preparation signal."
        )
    elif rcr is not None:
        main += (
            f" (typical resetter is now {rcr:.0f}% combat). "
            f"Costly activity — usually means repurposing for combat."
        )
    else:
        main += ". Costly activity — usually means repurposing for combat."
    return icon, name, main


def _digest_line_combat_intent(name, snap, intent):
    icon = "🟠"
    n = intent["combat_resets"]
    total = intent["new_resets"]
    rcr = intent["resetter_combat_ratio"]
    sample = snap["sample_size"]
    citizen = "citizen" if n == 1 else "citizens"
    main = (
        f"**{n} of {total}** {citizen} (out of {sample} top citizens) "
        f"rebuilt as combat fighters ({rcr:.0f}% combat skills)"
    )
    if intent.get("corroborated_by") == "ratio_shift":
        main += (
            f" — confirmed by 7d ratio shift of +{intent['ratio_shift_7d']:.0f}pp. "
            f"Small numbers but directionally consistent with mobilisation."
        )
    else:
        main += ". Multiple resetters in the same direction — early mobilisation signal."
    return icon, name, main


def _digest_line_creep(name, snap, creep):
    tier = creep.get("tier", "yellow")
    icon = {"red": "🔴", "orange": "🟠", "yellow": "🟡"}[tier]
    label = {
        "red": "Major combat shift (likely war-ready)",
        "orange": "Significant combat shift (mobilising)",
        "yellow": "Drifting toward combat (worth watching)",
    }[tier]
    window = creep["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"
    main = (
        f"{label}: typical citizen went from **{creep['old_ratio']:.0f}% → "
        f"{snap['combat_ratio']:.0f}% combat** over {window_text}"
    )
    return icon, name, main


def _digest_line_collapse(name, snap, collapse):
    icon = "🟢"
    tier = collapse.get("tier", "green_light")
    label = {
        "green_strong": "Strong stand-down (campaign likely ending)",
        "green_med": "Visible stand-down (de-escalating)",
        "green_light": "Drifting away from combat (early stand-down signal)",
    }[tier]
    window = collapse["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"
    main = (
        f"{label}: typical citizen went from **{collapse['old_ratio']:.0f}% → "
        f"{snap['combat_ratio']:.0f}% combat** over {window_text}"
    )
    return icon, name, main


def _digest_line_eco_intent(name, snap, intent):
    icon = "🟢"
    n = intent["eco_resets"]
    total = intent["new_resets"]
    rcr = intent["resetter_combat_ratio"]
    sample = snap["sample_size"]
    citizen = "citizen" if n == 1 else "citizens"
    main = (
        f"**{n} of {total}** {citizen} (out of {sample} top citizens) "
        f"rebuilt as workers ({rcr:.0f}% combat skills)"
    )
    if intent.get("corroborated_by") == "ratio_shift":
        main += (
            f" — confirmed by 7d ratio shift of {intent['ratio_shift_7d']:.0f}pp. "
            f"Small numbers but directionally consistent with demobilisation."
        )
    else:
        main += ". Multiple resetters returning to economy — early stand-down signal."
    return icon, name, main


# ---------- Digest assembly with rollup and holding state ----------

def _is_low_severity(flag):
    if flag["kind"] == "creep" and flag["detection"].get("tier") == "yellow":
        return True
    if flag["kind"] == "combat_intent":
        return flag["detection"]["combat_resets"] < COMBAT_INTENT_MIN_RESETTERS
    if flag["kind"] == "eco_intent":
        return flag["detection"]["eco_resets"] < ECO_INTENT_MIN_RESETTERS
    return False


def _minor_activity_label(flag):
    name = flag["name"]
    if flag["kind"] == "creep":
        return f"{name} (drifting toward combat)"
    if flag["kind"] == "combat_intent":
        return f"{name} (1 resetter, combat-leaning)"
    if flag["kind"] == "eco_intent":
        return f"{name} (1 resetter, eco-leaning)"
    return name


def send_digest(flagged, stood_down, holding, now):
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
    }

    fields = []
    for f in full_lines[:25]:
        renderer = line_renderers.get(f["kind"])
        if not renderer:
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
                "_Soft signals, individually below threshold — listed "
                "for awareness, not action._\n"
                + ", ".join(labels) + more
            ),
            "inline": False,
        })

    if holding:
        holding_lines = []
        for h in sorted(holding, key=lambda x: -x["current_ratio"])[:15]:
            if h.get("reason") == "active_war":
                line = (
                    f"**{h['name']}** — {h['current_ratio']:.0f}% combat "
                    f"(actively at war with Ireland)"
                )
            elif h.get("peak_date") and h["peak_date"] != "unknown":
                line = (
                    f"**{h['name']}** — {h['current_ratio']:.0f}% combat "
                    f"(peaked {h['peak_ratio']:.0f}% on {h['peak_date']})"
                )
            else:
                line = (
                    f"**{h['name']}** — {h['current_ratio']:.0f}% combat "
                    f"(still in combat posture)"
                )
            holding_lines.append(line)
        fields.append({
            "name": "🟠 Holding at high readiness",
            "value": (
                "_Previously mobilised, no fresh activity this run, "
                "but combat posture remains. Not a new warning — a "
                "reminder these countries are still elevated._\n"
                + "\n".join(holding_lines)
            ),
            "inline": False,
        })

    if stood_down:
        stood_down_lines = []
        by_reason = {"ratio_dropped": [], "soft_flag": [], "retired_no_data": []}
        for s in stood_down:
            by_reason.setdefault(s["reason"], []).append(s)

        for s in by_reason.get("ratio_dropped", []):
            if s.get("peak_ratio") is not None:
                stood_down_lines.append(
                    f"**{s['name']}** — combat focus dropped "
                    f"{s['peak_ratio']:.0f}% → {s['current_ratio']:.0f}% "
                    f"(de-escalating)"
                )
            else:
                stood_down_lines.append(
                    f"**{s['name']}** — de-escalating "
                    f"(now {s['current_ratio']:.0f}% combat)"
                )

        soft = by_reason.get("soft_flag", [])
        if soft:
            names = ", ".join(s["name"] for s in soft)
            stood_down_lines.append(
                f"{names} — never highly mobilised; activity quieted"
            )

        retired = by_reason.get("retired_no_data", [])
        if retired:
            names = ", ".join(s["name"] for s in retired)
            stood_down_lines.append(
                f"{names} — previous flag retired "
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
        if f["kind"] in ("collapse", "eco_intent"):
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
    if holding:
        parts.append(f"**{len(holding)}** holding")
    if counts_demob:
        parts.append(f"**{counts_demob}** standing down")
    summary = ", ".join(parts) if parts else None

    intro = (
        "Each line below tracks the top ~25 active fighters in a country "
        "(level ≥ 20, recently online) — the people who actually show up "
        "to battles. \"Combat focus\" means the share of a citizen's "
        "skill points spent on combat skills.\n\n"
    )

    total_items = len(full_lines) + len(minor_dedup) + len(holding) + len(stood_down)
    if total_items:
        body = f"{summary} ({total_items} total)." if summary else f"{total_items} items."
        description = intro + body
    else:
        description = intro + "All quiet."

    embed = {
        "title": "🛡️ War Watch · Daily Digest",
        "color": COLOUR_DIGEST,
        "description": description,
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    return safe_post(embed, "digest")


def _severity_rank(f):
    if f["kind"] in ("collapse", "eco_intent"):
        return (10, f["name"])
    rank = {"high": 0, "med": 1, "low": 2}.get(f["severity"], 9)
    return (rank, f["name"])


# ---------- Posture overview ----------

def _posture_bucket(ratio):
    if ratio >= POSTURE_WAR:
        return "war"
    if ratio >= POSTURE_LEAN:
        return "leaning"
    if ratio >= POSTURE_BALANCED:
        return "balanced"
    return "eco"


def send_posture_digest(snapshots, state, active_war_ids, country_name, now):
    """Informational overview of how the watchlist splits between war and
    economy posture this run, plus the largest 7d shifts in either
    direction. Sent every run, independent of whether anything is flagged.
    """
    rows = []
    for cid, snap in snapshots.items():
        ratio = snap["combat_ratio"]
        history = state.get("countries", {}).get(cid, {}).get("history", [])
        shift = _ratio_shift_7d(history, ratio, now)
        rows.append({
            "cid": cid,
            "name": snap["name"],
            "ratio": ratio,
            "bucket": _posture_bucket(ratio),
            "shift_7d": shift,
            "at_war": cid in active_war_ids,
        })

    if not rows:
        return False

    buckets = {"war": [], "leaning": [], "balanced": [], "eco": []}
    for r in rows:
        buckets[r["bucket"]].append(r)

    total = len(rows)
    war_side = len(buckets["war"]) + len(buckets["leaning"])
    eco_side = len(buckets["balanced"]) + len(buckets["eco"])

    bar_len = 20
    war_blocks = round((war_side / total) * bar_len) if total else 0
    eco_blocks = bar_len - war_blocks
    split_bar = "🟥" * war_blocks + "🟩" * eco_blocks

    avg_ratio = statistics.mean(r["ratio"] for r in rows)

    description = (
        f"Across **{total}** monitored countries, the watchlist leans "
        f"**{war_side} war-posture vs {eco_side} economy-posture**. "
        f"Average combat focus is **{avg_ratio:.0f}%**.\n\n"
        f"{split_bar}\n"
        f"_war ← → eco_"
    )

    def _fmt_country(r):
        tag = " ⚔️" if r["at_war"] else ""
        return f"{r['name']} ({r['ratio']:.0f}%){tag}"

    fields = []

    bucket_meta = [
        ("war", "🔴 War footing (≥70% combat)"),
        ("leaning", "🟠 Combat-leaning (50-70%)"),
        ("balanced", "🟡 Mixed (30-50%)"),
        ("eco", "🟢 Economy-focused (<30%)"),
    ]
    for key, label in bucket_meta:
        members = sorted(buckets[key], key=lambda r: -r["ratio"])
        if not members:
            continue
        names = ", ".join(_fmt_country(r) for r in members[:15])
        more = f" (+{len(members) - 15} more)" if len(members) > 15 else ""
        fields.append({
            "name": f"{label} — {len(members)}",
            "value": names + more,
            "inline": False,
        })

    with_shift = [r for r in rows if r["shift_7d"] is not None]
    risers = sorted(
        (r for r in with_shift if r["shift_7d"] >= POSTURE_MOVER_MIN),
        key=lambda r: -r["shift_7d"],
    )[:5]
    fallers = sorted(
        (r for r in with_shift if r["shift_7d"] <= -POSTURE_MOVER_MIN),
        key=lambda r: r["shift_7d"],
    )[:5]

    if risers:
        fields.append({
            "name": "📈 Shifting toward war (7d)",
            "value": "\n".join(
                f"**{r['name']}** +{r['shift_7d']:.0f}pp → {r['ratio']:.0f}% combat"
                for r in risers
            ),
            "inline": False,
        })
    if fallers:
        fields.append({
            "name": "📉 Shifting toward economy (7d)",
            "value": "\n".join(
                f"**{r['name']}** {r['shift_7d']:.0f}pp → {r['ratio']:.0f}% combat"
                for r in fallers
            ),
            "inline": False,
        })

    embed = {
        "title": "📊 War Watch · Posture Overview",
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
    """Decide whether a previously-flagged country actually stood down or
    is just holding at high readiness. See reason codes in the digest
    renderer.
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

    return ("stood_down", "soft_flag")


# ---------- Main ----------

def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    state = migrate(state)
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
        snapshots[cid] = snap
        mode = "disc" if snap.get("used_discovery") else "cache"
        rcr = snap.get("resetter_combat_ratio")
        rcr_str = f" rcr={rcr:.0f}%" if rcr is not None else ""
        intent_str = ""
        if snap.get("combat_resets"):
            intent_str = f" cmb={snap['combat_resets']}"
        elif snap.get("eco_resets"):
            intent_str = f" eco={snap['eco_resets']}"
        print(f"sample={snap['sample_size']}({mode}) "
              f"new_resets={snap['new_resets']}{intent_str} "
              f"combat={snap['combat_ratio']:.0f}%{rcr_str}")

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

        burst = detect_reset_burst(history, snap["new_resets"])
        creep = detect_ratio_creep(history, snap["combat_ratio"], now)
        collapse = detect_ratio_collapse(history, snap["combat_ratio"], now)
        combat_intent = detect_combat_intent_resets(snap, history, now)
        eco_intent = detect_eco_intent_resets(snap, history, now)

        new_history = (history + [{
            "ts": now.isoformat(),
            "new_resets": snap["new_resets"],
            "combat_resets": snap.get("combat_resets", 0),
            "eco_resets": snap.get("eco_resets", 0),
            "combat_ratio": snap["combat_ratio"],
            "resetter_combat_ratio": snap.get("resetter_combat_ratio"),
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
            "history": new_history,
        }

        # ---- Mobilisation flagging ----
        flagged_this_run = False

        if burst:
            high = is_high_severity_burst(burst)
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "burst",
                "severity": "high" if high else "med",
                "snap": snap,
                "detection": burst,
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
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "combat_intent",
                "severity": "med" if combat_intent["combat_resets"] >= COMBAT_INTENT_MIN_RESETTERS else "low",
                "snap": snap,
                "detection": combat_intent,
            })
            flagged_this_run = True

        if creep:
            sev = {"red": "high", "orange": "med", "yellow": "low"}[creep["tier"]]
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "creep",
                "severity": sev,
                "snap": snap,
                "detection": creep,
            })
            flagged_this_run = True
            if creep["tier"] == "red" and should_send_urgent(
                prev_country, creep["delta"],
                "last_creep_alert", "last_creep_delta",
            ):
                if send_high_severity_creep(snap["name"], snap, creep, history):
                    new_country["last_creep_alert"] = now.isoformat()
                    new_country["last_creep_delta"] = creep["delta"]

        # ---- Demobilisation flagging (suppressed for active wars) ----
        if cid not in active_war_ids:
            if collapse:
                high_demob = is_high_severity_demob(collapse)
                tier_sev = {
                    "green_strong": "med",
                    "green_med": "med",
                    "green_light": "low",
                }[collapse["tier"]]
                flagged.append({
                    "cid": cid,
                    "name": snap["name"],
                    "kind": "collapse",
                    "severity": tier_sev,
                    "snap": snap,
                    "detection": collapse,
                })
                flagged_this_run = True
                if high_demob and should_send_urgent(
                    prev_country, abs(collapse["delta"]),
                    "last_demob_alert", "last_demob_delta",
                ):
                    if send_high_severity_demob(snap["name"], snap, collapse, history):
                        demob_sent += 1
                        new_country["last_demob_alert"] = now.isoformat()
                        new_country["last_demob_delta"] = abs(collapse["delta"])
            elif eco_intent:
                flagged.append({
                    "cid": cid,
                    "name": snap["name"],
                    "kind": "eco_intent",
                    "severity": "low",
                    "snap": snap,
                    "detection": eco_intent,
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
    current_flagged_ids = {f["cid"] for f in flagged}
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
            current_flagged_ids.add(cid)

    # ---- Posture overview (always, independent of alerts) ----
    send_posture_digest(snapshots, state, active_war_ids, country_name, now)

    if flagged or stood_down or holding:
        send_digest(flagged, stood_down, holding, now)

    state["version"] = STATE_VERSION
    state["last_run"] = now.isoformat()
    state["countries"] = new_countries_state
    state["flagged_last_run"] = sorted(current_flagged_ids)
    save_state(state)

    mob_count = sum(
        1 for f in flagged if f["kind"] in ("burst", "combat_intent", "creep")
    )
    demob_count = sum(
        1 for f in flagged if f["kind"] in ("collapse", "eco_intent")
    )
    print(
        f"Done. Snapshots: {len(snapshots)}. "
        f"Flagged: {len(flagged)} ({mob_count} mobilising, {demob_count} demobilising; "
        f"{high_sev_sent} urgent burst, {demob_sent} urgent demob sent). "
        f"Holding: {len(holding)}. Stood down: {len(stood_down)}."
    )


if __name__ == "__main__":
    main()