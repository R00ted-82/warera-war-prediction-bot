"""
Detects countries gearing up for war by sampling active citizens for two
signals: bursts of skill resets (a strong leading indicator that people
are repurposing themselves) and gradual climbs in the combat/economy
skill-allocation ratio. Also detects the reverse: countries demobilising.

Sampling is restricted to a watchlist built from two sources:
  1. Countries controlling at least one region within BORDER_HOPS hops
     of Ireland's territory, derived dynamically by walking each
     region's `neighbors` field in region.getRegionsObject
  2. Countries listed in Ireland's `warsWith` diplomatic field
A country joins the watchlist if either criterion is met. Both update
dynamically each run.

Per run, for every watchlisted country:
  - Fetch lite profiles for the country's known veterans (cached from
    prior runs) OR paginate user.getUsersByCountry to discover them,
    refreshing the cohort every DISCOVERY_INTERVAL_DAYS
  - Keep top 25 qualifying (level >= 20, active in last 14 days) by level
  - Aggregate four numbers:
      * new_resets: citizens whose lastSkillsResetAt advanced since the
        previous run (true event count, not windowed)
      * combat_ratio: median combat-skill ratio across the full sample
      * resetter_combat_ratio: median combat ratio of just the citizens
        who reset since the previous run (null if none reset)
      * combat_resets: citizens whose reset rebuilt them strongly into
        combat (resetter combat ratio >= COMBAT_INTENT_PP). Lets us
        flag mobilisation even when the absolute reset count is small.

Diff against state.json's per-country history (14-day rolling window):
  - Reset burst: new_resets >= 2σ above the country's rolling baseline,
    OR >= RESET_FLOOR (whichever is greater). No-baseline-yet countries
    still fire if new_resets >= NO_BASELINE_RESET_FLOOR — fixes the
    silent-collection gap that let early mobilisations slip through.
  - Ratio creep (mobilising): combat ratio >= RATIO_CREEP_PP above where
    it was ~7 days ago, OR >= RATIO_JUMP_1D_PP above yesterday's value
  - Ratio collapse (demobilising): combat ratio dropped by the same
    thresholds. Treated as a "standing down" signal.
  - Combat-intent resets: any run where new_resets >= 1 AND
    resetter_combat_ratio >= COMBAT_INTENT_PP triggers a low-severity
    flag even if the absolute count is below RESET_FLOOR.

Output:
  - One digest embed per run listing every flagged country with severity
    icons (red/orange/yellow for mobilising, green for standing down),
    plus any countries that stood down since the last run
  - Dedicated alerts for high-severity bursts (>= 1.5× threshold or 10+
    new resets) and for high-severity demobs (≥50pp drop in 7d), with
    sparkline trends and resetter-allocation context. Per-country
    cooldown on these — by default 3 days, unless severity escalates by
    50%+
  - Health warnings to Discord if the watchlist comes back empty or
    fewer than half of watchlisted countries can be sampled

First ~5 runs collect baseline silently for the σ-based burst detector;
the absolute-floor and combat-intent detectors fire from run 1.

Sibling of alert.py.
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
STATE_VERSION = 5
IRELAND_COUNTRY_ID = "6813b6d446e731854c7ac7fe"

# Watchlist scope
BORDER_HOPS = 3                # how far out to walk Ireland's adjacency graph.

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
HISTORY_LEN = 14
MIN_HISTORY_FOR_BASELINE = 5
BASELINE_SIGMA = 2.0
RESET_FLOOR = 3                # per-run reset count needed once we HAVE a baseline
NO_BASELINE_RESET_FLOOR = 4    # NEW: per-run reset count that fires even without baseline.
                               # Set lower than RESET_FLOOR × HIGH_SEVERITY_FACTOR so we
                               # don't miss early mobilisations like Norway 2026-05-21.
RATIO_CREEP_MIN = 20.0         # Minimum ratio shift (in % points) to flag as creep at all.
                               # Tiered above this: 20-40 = yellow ("drifting"),
                               # 40-60 = orange ("shifting"), 60+ = red ("major swing").
RATIO_CREEP_ORANGE = 40.0
RATIO_CREEP_RED = 60.0
RATIO_JUMP_1D_MIN = 30.0       # 1-day equivalent: catches fast swings that the
                               # 7-day creep misses entirely.
RATIO_LOOKBACK_DAYS = 7

# De-spec / demobilisation detection (mirror of above)
RATIO_DROP_MIN = 20.0
RATIO_DROP_ORANGE = 40.0       # Note: "orange" in demob context = strong stand-down (good).
RATIO_DROP_RED = 60.0          # Colours stay green-shaded in the digest; thresholds
                               # mirror creep magnitudes for symmetry.
RATIO_DROP_1D_MIN = 30.0
HIGH_DEMOB_FOR_ALERT = 50.0    # Drops at or above this trigger a dedicated alert,
                               # not just a digest line.
DEMOB_RESET_INTENT = 30.0      # If a resetter's new combat allocation is at or below
                               # this, they're considered "rebuilding into economy".

# Combat-intent resets (catches mobilisation even at low absolute count)
COMBAT_INTENT = 70.0           # If a resetter's new combat allocation is at or above
                               # this, they're considered "rebuilding into combat".

# Severity for dedicated alerts (urgent burst threshold)
HIGH_SEVERITY_FACTOR = 1.5     # burst >= this × threshold counts as urgent
HIGH_SEVERITY_FLOOR = 10       # OR >= this many absolute new resets

# Urgent alert cooldown (per-country, applies to both mobilisation and demob alerts)
URGENT_COOLDOWN_DAYS = 3
URGENT_ESCALATION_FACTOR = 1.5

# Pipeline health
HEALTH_SNAPSHOT_RATE = 0.5

# Embed colours
COLOUR_RESET_BURST = 0xED4245  # red: concrete preparation signal
COLOUR_RATIO_CREEP = 0xFEE75C  # yellow: softer, longer-running shift
COLOUR_DEMOB = 0x57F287        # green: standing down
COLOUR_DIGEST = 0x5865F2       # blurple: neutral roundup colour
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
    """Hit the proxy. Retries on transient errors like the JS clients do."""
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
    """Returns {country_id: [region_names]} for foreign countries
    controlling at least one region within max_hops of the given
    country's territory.
    """
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
    """Compare each sampled user's reset timestamp to last run's value.

    Returns (new_resets count, resetter_ratios list, combat_resets count,
    eco_resets count, updated user_resets dict).

    combat_resets and eco_resets are per-user counts of resets where the
    individual resetter ended up strongly combat- or economy-leaning;
    they let us flag mobilisation/demob direction even at low absolute
    counts.
    """
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
    """Returns dict describing the burst, or None.

    Two paths:
      1. Baseline available (>=5 runs of history): fire if current is
         >=2σ above mean, OR >= RESET_FLOOR.
      2. No baseline yet: fire if current >= NO_BASELINE_RESET_FLOOR.
         This catches obvious mobilisations during the warm-up window
         that the old code would silently swallow.
    """
    prior = [h.get("new_resets", 0) for h in history]
    if len(prior) < MIN_HISTORY_FOR_BASELINE:
        if current >= NO_BASELINE_RESET_FLOOR:
            return {
                "baseline_mean": None,
                "threshold": NO_BASELINE_RESET_FLOOR,
                "current": current,
                "reason": "no_baseline_floor",
            }
        return None
    mean = statistics.mean(prior)
    stdev = statistics.stdev(prior) if len(prior) > 1 else 0.0
    threshold = max(mean + BASELINE_SIGMA * stdev, RESET_FLOOR)
    if current >= threshold:
        return {
            "baseline_mean": round(mean, 1),
            "threshold": round(threshold, 1),
            "current": current,
            "reason": "baseline_breach",
        }
    return None


def detect_combat_intent_resets(snap):
    """Returns dict if any resets this run were strongly combat-leaning,
    even when the absolute count is too low for a burst.
    """
    combat_resets = snap.get("combat_resets", 0)
    rcr = snap.get("resetter_combat_ratio")
    if combat_resets >= 1 and rcr is not None and rcr >= COMBAT_INTENT_PP:
        return {
            "combat_resets": combat_resets,
            "resetter_combat_ratio": rcr,
            "new_resets": snap.get("new_resets", 0),
        }
    return None


def _find_history_point(history, target, min_age_days):
    """Returns the history entry closest to `target` time, but only if
    it's at least `min_age_days` old. None if no such point exists.
    """
    if not history:
        return None
    closest = min(history, key=lambda h: abs(parse_iso(h["ts"]) - target))
    age_days = (target - parse_iso(closest["ts"])).total_seconds() / 86400
    # Allow a half-day of slack: a point that's 0.6 days old still
    # functions as "yesterday" for 1d detection
    if age_days < min_age_days - 0.5:
        return None
    return closest


def detect_ratio_creep(history, current_ratio, now):
    """Returns dict if combat ratio is rising. Includes magnitude tier
    so the digest can render yellow / orange / red.

    Two timeframes:
      - 7-day creep: gradual shift, minimum 20 point gain
      - 1-day jump: fast swing, minimum 30 point gain
    Reports the more dramatic of the two.
    """
    candidates = []

    week = _find_history_point(
        history, now - timedelta(days=RATIO_LOOKBACK_DAYS), RATIO_LOOKBACK_DAYS - 1
    )
    if week is not None:
        delta = current_ratio - week["combat_ratio"]
        if delta >= RATIO_CREEP_MIN:
            candidates.append({
                "old_ratio": week["combat_ratio"],
                "delta": round(delta, 1),
                "window_days": RATIO_LOOKBACK_DAYS,
            })

    day = _find_history_point(history, now - timedelta(days=1), 1)
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
    # Tier by magnitude. The detector reports the tier, the digest decides
    # icon / colour from it.
    mag = winner["delta"]
    if mag >= RATIO_CREEP_RED:
        winner["tier"] = "red"
    elif mag >= RATIO_CREEP_ORANGE:
        winner["tier"] = "orange"
    else:
        winner["tier"] = "yellow"
    return winner


def detect_ratio_collapse(history, current_ratio, now):
    """Mirror of detect_ratio_creep for the demobilising direction.
    Returns dict with delta as a negative number (magnitude of drop).
    Tiers track magnitude on the green side of the palette.
    """
    candidates = []

    week = _find_history_point(
        history, now - timedelta(days=RATIO_LOOKBACK_DAYS), RATIO_LOOKBACK_DAYS - 1
    )
    if week is not None:
        delta = current_ratio - week["combat_ratio"]
        if delta <= -RATIO_DROP_MIN:
            candidates.append({
                "old_ratio": week["combat_ratio"],
                "delta": round(delta, 1),
                "window_days": RATIO_LOOKBACK_DAYS,
            })

    day = _find_history_point(history, now - timedelta(days=1), 1)
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


def detect_eco_intent_resets(snap):
    """Returns dict if any resets this run were strongly eco-leaning."""
    eco_resets = snap.get("eco_resets", 0)
    rcr = snap.get("resetter_combat_ratio")
    if eco_resets >= 1 and rcr is not None and rcr <= DEMOB_RESET_INTENT:
        return {
            "eco_resets": eco_resets,
            "resetter_combat_ratio": rcr,
            "new_resets": snap.get("new_resets", 0),
        }
    return None


def is_high_severity_burst(burst):
    threshold = burst.get("threshold") or RESET_FLOOR
    return burst["current"] >= max(threshold * HIGH_SEVERITY_FACTOR, HIGH_SEVERITY_FLOOR)


def is_high_severity_demob(collapse):
    return abs(collapse["delta"]) >= HIGH_DEMOB_FOR_ALERT


def should_send_urgent(prev_country, current_value, key_alert, key_count):
    """Per-country cooldown for urgent alerts. Generic over alert type:
    pass the state key prefix (e.g. 'last_urgent_alert' or
    'last_demob_alert') and the count being compared.
    """
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
    """Bring older state files up to the current schema. Idempotent and
    incremental — each version bump runs once per file.
    """
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
        print("Migrated state v3 → v4 (dropped history, added per-user reset tracking).")

    if version < 5:
        # v5 changes:
        #  - Added combat_resets / eco_resets per-country
        #  - Added last_demob_alert / last_demob_delta for demob cooldown
        #  - Added last_creep_alert / last_creep_delta for red-tier creep cooldown
        for country in state.get("countries", {}).values():
            country.setdefault("combat_resets", 0)
            country.setdefault("eco_resets", 0)
            country.setdefault("last_demob_alert", None)
            country.setdefault("last_demob_delta", None)
            country.setdefault("last_creep_alert", None)
            country.setdefault("last_creep_delta", None)
        state["version"] = 5
        print("Migrated state v4 → v5 (added combat/eco reset counts and demob tracking).")

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
    """Turn 'mean resets per run' (one run ≈ one day) into plain English.

    Examples:
      0.0  → "almost never"
      0.4  → "about 1 every 2-3 days"
      1.0  → "about 1 per day"
      2.5  → "2-3 per day"
      5.0  → "around 5 per day"
    """
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
    """Plain-English line describing what the people who reset are doing.

    Returns up to two facts: how many went strongly combat/economy, and
    the typical resetter's new combat focus. Designed to be honest about
    referents ("of those who reset" not "of the country").
    """
    rcr = snap.get("resetter_combat_ratio")
    if rcr is None:
        return None
    combat_resets = snap.get("combat_resets", 0)
    eco_resets = snap.get("eco_resets", 0)
    n = snap.get("new_resets", 0)

    parts = []
    if n >= 1:
        if combat_resets >= 1:
            parts.append(
                f"**{combat_resets} of {n}** rebuilt as a combat fighter"
            )
        elif eco_resets >= 1:
            parts.append(
                f"**{eco_resets} of {n}** rebuilt as a worker"
            )

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
    """Dedicated alert when a notable number of citizens reset at once."""
    n = burst["current"]
    sample_size = snap["sample_size"]
    pct = (n / sample_size) * 100 if sample_size else 0

    # Baseline phrasing has to handle both "no baseline yet" and the
    # normal case. Decimal means-per-run are unintuitive so we humanise.
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
                f"(level ≥ {MIN_LEVEL}, online in last {ACTIVITY_WINDOW_DAYS} days). "
                f"Not the whole population — just the people who actually show up "
                f"to battles."
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
    """Dedicated alert when a country's fighters are visibly standing down."""
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


def _digest_line_burst(name, snap, burst, severity):
    """A clutch of citizens switched their builds.

    Fires when: new_resets ≥ baseline+2σ (or ≥ RESET_FLOOR with baseline,
    or ≥ NO_BASELINE_RESET_FLOOR without). 'high' severity adds a
    dedicated alert on top.
    """
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
            f"({rcr:.0f}% combat skills)"
        )
    elif rcr is not None:
        main += f" (typical resetter is now {rcr:.0f}% combat)"
    return icon, name, main


def _digest_line_combat_intent(name, snap, intent):
    """Small number of resets, but those who reset clearly went combat.

    Fires when: new_resets ≥ 1 AND median resetter has combat allocation
    ≥ COMBAT_INTENT (70%). Only emitted when there's no burst already
    flagging the same activity.
    """
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
    return icon, name, main


def _digest_line_creep(name, snap, creep):
    """The country's typical fighter has shifted toward combat skills
    over the past day or week — without necessarily resetting.

    Fires when: combat ratio rose by ≥ 20 points in 7d OR ≥ 30 in 1d.
    Icon tier scales with magnitude (yellow / orange / red).
    """
    tier = creep.get("tier", "yellow")
    icon = {"red": "🔴", "orange": "🟠", "yellow": "🟡"}[tier]
    label = {
        "red": "Major combat shift",
        "orange": "Significant combat shift",
        "yellow": "Drifting toward combat",
    }[tier]
    window = creep["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"
    main = (
        f"{label}: typical citizen went from **{creep['old_ratio']:.0f}% → "
        f"{snap['combat_ratio']:.0f}% combat** over {window_text}"
    )
    return icon, name, main


def _digest_line_collapse(name, snap, collapse):
    """Mirror of creep — citizens shifted away from combat.

    Fires when: combat ratio dropped by ≥ 20 points in 7d OR ≥ 30 in 1d.
    Always green-shaded; magnitude controls the qualifier.
    """
    icon = "🟢"
    tier = collapse.get("tier", "green_light")
    label = {
        "green_strong": "Strong stand-down",
        "green_med": "Visible stand-down",
        "green_light": "Drifting away from combat",
    }[tier]
    window = collapse["window_days"]
    window_text = "the past day" if window == 1 else f"the past {window} days"
    drop = abs(collapse["delta"])
    likely = ""
    if drop >= RATIO_DROP_RED:
        likely = " — appears to be standing down from active campaign"
    elif drop >= RATIO_DROP_ORANGE:
        likely = " — likely de-escalating"
    main = (
        f"{label}: typical citizen went from **{collapse['old_ratio']:.0f}% → "
        f"{snap['combat_ratio']:.0f}% combat** over {window_text}{likely}"
    )
    return icon, name, main


def _digest_line_eco_intent(name, snap, intent):
    """Small number of resets, but those who reset rebuilt as workers.

    Fires when: new_resets ≥ 1 AND median resetter has combat allocation
    ≤ DEMOB_RESET_INTENT (30%). Only emitted when there's no collapse
    already flagging the same activity.
    """
    icon = "🟢"
    n = intent["eco_resets"]
    total = intent["new_resets"]
    rcr = intent["resetter_combat_ratio"]
    sample = snap["sample_size"]
    citizen = "citizen" if n == 1 else "citizens"
    main = (
        f"**{n} of {total}** {citizen} (out of {sample} top citizens) "
        f"rebuilt as workers ({rcr:.0f}% combat skills) — demobilising"
    )
    return icon, name, main


def send_high_severity_creep(country_name, snap, creep, history):
    """Dedicated alert for a major combat-allocation swing (red tier).
    Distinct from burst alerts — this one fires on the state change
    (where allocation is now) rather than the event (resets happening).
    """
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


def send_digest(flagged, stood_down, now):
    """One summary embed per run. Lists flagged countries severity-ranked,
    appends a stood-down section if any.
    """
    # Order:
    #   1. high-severity mobilisation (red burst / red creep)
    #   2. medium mobilisation (orange burst / orange creep / combat_intent)
    #   3. low mobilisation (yellow creep)
    #   4. demobilisation (any green tier)
    def order_key(f):
        if f["kind"] in ("collapse", "eco_intent"):
            return (10, f["name"])
        sev_rank = {"high": 0, "med": 1, "low": 2}.get(f["severity"], 9)
        return (sev_rank, f["name"])
    flagged.sort(key=order_key)

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
    for f in flagged[:25]:
        renderer = line_renderers.get(f["kind"])
        if not renderer:
            continue
        icon, name, value = renderer(f)
        fields.append({
            "name": f"{icon} {name}",
            "value": value,
            "inline": False,
        })

    if stood_down:
        stood_names = sorted(name for _, name in stood_down)
        fields.append({
            "name": "✅ No longer flagged",
            "value": ", ".join(stood_names),
            "inline": False,
        })

    counts_mob = {"high": 0, "med": 0, "low": 0}
    counts_demob = 0
    for f in flagged:
        if f["kind"] in ("collapse", "eco_intent"):
            counts_demob += 1
        else:
            counts_mob[f["severity"]] = counts_mob.get(f["severity"], 0) + 1
    parts = []
    if counts_mob["high"]:
        parts.append(f"**{counts_mob['high']}** urgent")
    if counts_mob["med"]:
        parts.append(f"**{counts_mob['med']}** preparing")
    if counts_mob["low"]:
        parts.append(f"**{counts_mob['low']}** drifting")
    if counts_demob:
        parts.append(f"**{counts_demob}** standing down")
    summary = ", ".join(parts) if parts else None

    intro = (
        "Each line below tracks the top ~25 active fighters in a country "
        "(level ≥ 20, recently online) — the people who actually show up "
        "to battles. \"Combat focus\" means the share of a citizen's "
        "skill points spent on combat skills.\n\n"
    )

    if flagged:
        body = f"{summary} ({len(flagged)} total)."
        if len(flagged) > 25:
            body += f"\nShowing top 25; {len(flagged) - 25} more not displayed."
        if stood_down:
            body += f" {len(stood_down)} no longer flagged."
        description = intro + body
    else:
        description = intro + f"All quiet now — {len(stood_down)} no longer flagged."

    embed = {
        "title": "🛡️ War Watch · Daily Digest",
        "color": COLOUR_DIGEST,
        "description": description,
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    return safe_post(embed, "digest")


def send_health_alert(message, critical=False):
    embed = {
        "title": "🚨 War Watch · Critical" if critical else "⚠️ War Watch · Degraded",
        "color": COLOUR_HEALTH_CRIT if critical else COLOUR_HEALTH_WARN,
        "description": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return safe_post(embed, "health alert")


# ---------- Main ----------

def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    state = migrate(state)
    countries = fetch_countries()
    regions = fetch_regions()
    ireland = fetch_ireland()
    watchlist = build_watchlist(regions, ireland)

    country_name = {c["_id"]: c.get("name", c["_id"]) for c in countries if c.get("_id")}
    print(f"Loaded {len(countries)} countries. Watchlist: {len(watchlist)} "
          f"(within {BORDER_HOPS} hops of Ireland).")
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
        combat_intent = detect_combat_intent_resets(snap)
        eco_intent = detect_eco_intent_resets(snap)

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
            "history": new_history,
        }

        # Mobilisation signals (mutually compatible — burst beats combat_intent
        # in the digest since they'd describe the same event)
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
            if high and should_send_urgent(
                prev_country, burst["current"],
                "last_urgent_alert", "last_urgent_count",
            ):
                if send_high_severity_burst(snap["name"], snap, burst, history):
                    high_sev_sent += 1
                    new_country["last_urgent_alert"] = now.isoformat()
                    new_country["last_urgent_count"] = burst["current"]
        elif combat_intent:
            # Only flag combat_intent as its own line if there's no burst
            # already explaining the same activity
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "combat_intent",
                "severity": "med",
                "snap": snap,
                "detection": combat_intent,
            })

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
            # Red-tier creep (60+ point shift) gets its own dedicated alert.
            # Cooldown so we don't re-alert daily on a sustained high ratio.
            if creep["tier"] == "red" and should_send_urgent(
                prev_country, creep["delta"],
                "last_creep_alert", "last_creep_delta",
            ):
                if send_high_severity_creep(snap["name"], snap, creep, history):
                    new_country["last_creep_alert"] = now.isoformat()
                    new_country["last_creep_delta"] = creep["delta"]

        # Demobilisation signals
        if collapse:
            high_demob = is_high_severity_demob(collapse)
            # Tier the digest severity by magnitude so red-tier demobs
            # rank higher in the digest order
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

        new_countries_state[cid] = new_country

    # Stand-down detection: countries flagged last run that aren't anymore.
    # Demob-direction flags count as "still flagged" for this purpose —
    # they're an active state, not a stand-down event.
    prev_flagged_ids = set(state.get("flagged_last_run", []))
    current_flagged_ids = {f["cid"] for f in flagged}
    sampled_ids = set(snapshots.keys())
    stood_down_ids = (prev_flagged_ids & sampled_ids) - current_flagged_ids
    stood_down = []
    for cid in stood_down_ids:
        name = (country_name.get(cid)
                or state.get("countries", {}).get(cid, {}).get("name")
                or cid)
        stood_down.append((cid, name))

    if flagged or stood_down:
        send_digest(flagged, stood_down, now)

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
        f"Stood down: {len(stood_down)}."
    )


if __name__ == "__main__":
    main()