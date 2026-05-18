"""
Detects countries gearing up for war by sampling active citizens for two
signals: bursts of skill resets (a strong leading indicator that people are
repurposing themselves) and gradual climbs in the combat/economy skill-
allocation ratio.

Sampling is restricted to a watchlist built from two sources:
  1. Countries currently controlling a region bordering Ireland (see
     BORDER_REGION_NAMES — static list of region names, dynamic country
     resolution at runtime)
  2. Countries listed as Ireland's sworn enemy or active-war opponent
     in the diplomatic data on the Ireland country object
A country joins the watchlist if either criterion is met. Both fields
update dynamically each run — no code change needed when controllers
shift or wars are declared/ended.

Per run, for every watchlisted country:
  - Fetch lite profiles for the country's known veterans (cached from prior
    runs) OR paginate user.getUsersByCountry to discover them, refreshing
    the cohort every DISCOVERY_INTERVAL_DAYS
  - Keep top 25 qualifying (level >= 20, active in last 14 days) by level
  - Aggregate: count of resets in last 5 days, median combat-skill ratio

Diff against state.json's per-country history (14-day rolling window):
  - Reset burst: 5-day reset count >= 2σ above the country's rolling
    baseline (or RESET_FLOOR floor, whichever is greater)
  - Ratio creep: combat ratio >= 20 percentage points above where it was
    ~7 days ago

Output:
  - One digest embed per run listing every flagged country with severity
    icons (red/orange/yellow)
  - Dedicated alerts for high-severity bursts only (>= 1.5× threshold or
    10+ absolute resets), with sparkline trends for resets and ratio

First ~5 runs collect baseline silently. Sibling of alert.py.
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
STATE_VERSION = 3
IRELAND_COUNTRY_ID = "6813b6d446e731854c7ac7fe"

# Regions that physically border Ireland (land or short sea routes). We sample
# only countries that currently control one of these regions, since they're
# the only ones in practical attack range. Update this list if Ireland
# expands its territory — the API doesn't expose region adjacency, so this
# can't be derived automatically. Region names must match exactly as
# returned by region.getRegionsObject.
BORDER_REGION_NAMES = {
    # British Isles
    "Northern Ireland",
    "Scotland",
    "Wales",
    "Central England",
    "Southeastern England",
    # Channel and North Sea
    "Brittany",
    "Northern France",
    # Iceland and North Atlantic islands
    "Western Iceland",
    "Northeastern Iceland",
    "Eastern Iceland",
    "Southern Iceland",
    "Faroe Islands",
    # Mid-Atlantic
    "Azores",
    # Transatlantic sea routes
    "American Atlantic Coast",
    "Atlantic Canada",
    "Southern Greenland",
}

# Sampling per country
ENUM_LIMIT = 100               # citizens per page via user.getUsersByCountry
MAX_PAGES = 15                 # paginate up to this many pages of older citizens
SAMPLE_TOP_N = 25              # of those that pass filters, keep top-N by level
MIN_LEVEL = 20                 # ignore citizens below this level
MIN_SAMPLE = 10                # skip countries with fewer eligible citizens
ACTIVITY_WINDOW_DAYS = 14      # citizen counts as "active" if connected within
DISCOVERY_INTERVAL_DAYS = 7    # full re-pagination cadence per country

# Concurrency
MAX_WORKERS = 5
HTTP_TIMEOUT = 30
RETRY_ATTEMPTS = 3

# Detection
RESET_WINDOW_DAYS = 5
HISTORY_LEN = 14               # rolling history kept per country
MIN_HISTORY_FOR_BASELINE = 5
BASELINE_SIGMA = 2.0
RESET_FLOOR = 5                # absolute minimum reset count to consider alerting
RATIO_CREEP_PP = 20.0          # combat-ratio gain (percentage points) that triggers
RATIO_LOOKBACK_DAYS = 7

# Severity (bursts above these get their own alert; everything else goes in the digest)
HIGH_SEVERITY_FACTOR = 1.5     # burst >= this × threshold counts as urgent
HIGH_SEVERITY_FLOOR = 10       # OR >= this many absolute resets

# Embed colours
COLOUR_RESET_BURST = 0xED4245  # red: concrete preparation signal
COLOUR_RATIO_CREEP = 0xFEE75C  # yellow: softer, longer-running shift
COLOUR_DIGEST = 0x5865F2       # blurple: neutral roundup colour

# Sparkline characters (low to high)
SPARKLINE_BARS = "▁▂▃▄▅▆▇█"

# Skill buckets: each skill's `level` field is the number of points spent on it.
# Energy and hunger are excluded as ambiguous (both feed work cycles AND combat).
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
                "post ", "api2.warera.io",  # upstream batch errors
            )
            if any(s in msg.lower() for s in transient):
                raise requests.exceptions.RequestException(f"transient: {msg}")
            raise RuntimeError(f"{endpoint} → {msg[:120]}")
        return data.get("result", {}).get("data")
    except (requests.exceptions.RequestException, json.JSONDecodeError):
        if attempt < RETRY_ATTEMPTS:
            # Jitter prevents N parallel retries from bundling at the proxy again
            time.sleep(0.4 * attempt + random.uniform(0, 0.5))
            return trpc(endpoint, payload, attempt + 1)
        raise


def fetch_countries():
    data = trpc("country.getAllCountries", {})
    if isinstance(data, dict) and "items" in data:
        return data["items"]
    return data or []


def fetch_regions():
    """Returns dict of region_id -> region object."""
    data = trpc("region.getRegionsObject", {}) or {}
    return data if isinstance(data, dict) else {}


def fetch_ireland():
    """Fetches Ireland's country object for diplomatic fields."""
    try:
        return trpc("country.getCountryById", {"countryId": IRELAND_COUNTRY_ID})
    except Exception as e:
        print(f"  warn: could not fetch Ireland country object: {e}",
              file=sys.stderr)
        return None


def _extract_diplomatic_enemies(ireland):
    """Returns dict country_id -> list of reasons ('sworn enemy', 'at war')."""
    enemies = {}
    if not ireland:
        return enemies
    sworn = ireland.get("swornEnemy") or ireland.get("enemy")
    if sworn and isinstance(sworn, str):
        enemies.setdefault(sworn, []).append("sworn enemy")
    # War list field name varies in similar APIs; entries may be raw IDs or
    # wrapped war objects with an opponent reference inside.
    wars = (ireland.get("activeWars") or ireland.get("wars")
            or ireland.get("warOpponents") or [])
    for w in wars:
        opponent = None
        if isinstance(w, str):
            opponent = w
        elif isinstance(w, dict):
            for key in ("opponent", "enemy", "country", "opponentCountry"):
                if w.get(key) and isinstance(w[key], str):
                    opponent = w[key]
                    break
        if opponent:
            enemies.setdefault(opponent, []).append("at war")
    return enemies


def build_watchlist(regions_obj, ireland):
    """Map of country_id -> {border_regions, diplomatic} describing why
    each country is being monitored.

    A country can earn a watchlist spot by controlling a border region,
    by being listed as Ireland's sworn enemy or active-war opponent, or
    both. Logs a warning for any BORDER_REGION_NAMES entry that doesn't
    appear in the API response (typo or rename).
    """
    entries = {}
    found_names = set()
    for region in regions_obj.values():
        if not isinstance(region, dict):
            continue
        name = region.get("name")
        country_id = region.get("country")
        if name in BORDER_REGION_NAMES:
            found_names.add(name)
            if country_id:
                entries.setdefault(
                    country_id, {"border_regions": [], "diplomatic": []}
                )["border_regions"].append(name)
    missing = BORDER_REGION_NAMES - found_names
    if missing:
        print(f"  warn: border regions not found in API: {sorted(missing)}",
              file=sys.stderr)

    for cid, reasons in _extract_diplomatic_enemies(ireland).items():
        entries.setdefault(
            cid, {"border_regions": [], "diplomatic": []}
        )["diplomatic"].extend(reasons)

    return entries


def fetch_citizens_page(country_id, cursor=None):
    """One page of citizen IDs plus the next cursor (or None if exhausted)."""
    payload = {"countryId": country_id, "limit": ENUM_LIMIT}
    if cursor:
        payload["cursor"] = cursor
    data = trpc("user.getUsersByCountry", payload) or {}
    ids = [u["_id"] for u in data.get("items", []) if u.get("_id")]
    return ids, data.get("nextCursor")


def fetch_user_lite(user_id):
    # Small jitter decorrelates parallel requests so the proxy doesn't bundle
    # them into a single upstream call (which the game API rejects when large).
    time.sleep(random.uniform(0, 0.15))
    try:
        return trpc("user.getUserLite", {"userId": user_id})
    except Exception as e:
        print(f"  warn: user {user_id}: {e}", file=sys.stderr)
        return None


def parallel_fetch_lite(user_ids):
    """Fetch lite profiles for a batch of IDs concurrently."""
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
    """API uses ISO with 'Z' suffix. Portable across Python 3.10 and 3.11+."""
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


def reset_within(user, now, days):
    """True if the user genuinely reset (not just account creation) within window."""
    last_reset = parse_iso((user.get("dates") or {}).get("lastSkillsResetAt"))
    if last_reset is None:
        return False
    # The game sometimes seeds lastSkillsResetAt to createdAt for fresh accounts.
    # Treat anything within a minute of account creation as "never reset".
    created = parse_iso(user.get("createdAt"))
    if created and abs((last_reset - created).total_seconds()) < 60:
        return False
    return (now - last_reset).days <= days


def combat_ratio(user):
    """Returns combat % of points spent on bucketed skills, or None if nothing spent."""
    skills = user.get("skills") or {}
    combat = sum((skills.get(s) or {}).get("level", 0) for s in COMBAT_SKILLS)
    eco = sum((skills.get(s) or {}).get("level", 0) for s in ECO_SKILLS)
    total = combat + eco
    if total == 0:
        return None
    return (combat / total) * 100.0


def sparkline(values):
    """Unicode block-character sparkline. Empty string if no values."""
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

def discover_qualifying(country_id, now):
    """Paginate user.getUsersByCountry until enough qualifying citizens found."""
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
    """Aggregate snapshot for a country, or None if eligible sample < MIN_SAMPLE.

    Uses cached citizen IDs from prior runs when available. Re-discovers via
    pagination every DISCOVERY_INTERVAL_DAYS, when the cache is empty, or
    when too few cached citizens still qualify.
    """
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
        # Too many dropouts since last discovery: force a refresh
        if len(qualifying) < MIN_SAMPLE:
            qualifying = []

    if not qualifying:
        qualifying = discover_qualifying(country_id, now)
        used_discovery = True

    qualifying.sort(key=user_level, reverse=True)
    sample = qualifying[:SAMPLE_TOP_N]
    if len(sample) < MIN_SAMPLE:
        return None

    resets = sum(1 for u in sample if reset_within(u, now, RESET_WINDOW_DAYS))
    ratios = [r for r in (combat_ratio(u) for u in sample) if r is not None]
    if not ratios:
        return None

    return {
        "name": country_name,
        "sample_size": len(sample),
        "resets_5d": resets,
        "combat_ratio": round(statistics.median(ratios), 2),
        "known_veterans": [u.get("_id") for u in sample if u.get("_id")],
        "last_discovery": now.isoformat() if used_discovery else last_discovery_iso,
        "used_discovery": used_discovery,
    }


# ---------- Detection ----------

def detect_reset_burst(history, current):
    """Returns dict describing the burst, or None."""
    prior = [h["resets_5d"] for h in history]
    if len(prior) < MIN_HISTORY_FOR_BASELINE:
        # No baseline yet, collect data silently. Reset rates vary widely
        # across countries; firing on an absolute floor here would alert
        # on routine background activity rather than mobilisation.
        return None
    mean = statistics.mean(prior)
    stdev = statistics.stdev(prior) if len(prior) > 1 else 0.0
    threshold = max(mean + BASELINE_SIGMA * stdev, RESET_FLOOR)
    if current >= threshold:
        return {
            "baseline_mean": round(mean, 1),
            "threshold": round(threshold, 1),
            "current": current,
        }
    return None


def detect_ratio_creep(history, current_ratio, now):
    """Returns dict describing the creep, or None."""
    if not history:
        return None
    target = now - timedelta(days=RATIO_LOOKBACK_DAYS)
    closest = min(history, key=lambda h: abs(parse_iso(h["ts"]) - target))
    age = (now - parse_iso(closest["ts"])).days
    if age < RATIO_LOOKBACK_DAYS - 1:
        return None
    delta = current_ratio - closest["combat_ratio"]
    if delta >= RATIO_CREEP_PP:
        return {"old_ratio": closest["combat_ratio"], "delta_pp": round(delta, 1)}
    return None


def is_high_severity(burst):
    """True if a reset burst warrants its own dedicated Discord message."""
    threshold = burst.get("threshold") or RESET_FLOOR
    return burst["current"] >= max(threshold * HIGH_SEVERITY_FACTOR, HIGH_SEVERITY_FLOOR)


# ---------- State ----------

def load_state():
    if not STATE_FILE.exists():
        return {"version": STATE_VERSION, "countries": {}}
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------- Alerts ----------

def post_embed(payload):
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()


def trend_field(history, snap):
    """Sparkline lines showing reset count and combat ratio trends."""
    if len(history) < 2:
        return "Not enough history yet."
    reset_series = [h["resets_5d"] for h in history] + [snap["resets_5d"]]
    ratio_series = [h["combat_ratio"] for h in history] + [snap["combat_ratio"]]
    return (
        f"`{sparkline(reset_series)}` resets "
        f"({reset_series[0]} → {reset_series[-1]})\n"
        f"`{sparkline(ratio_series)}` combat ratio "
        f"({ratio_series[0]:.0f}% → {ratio_series[-1]:.0f}%)"
    )


def send_high_severity_burst(country_name, snap, burst, history):
    """Dedicated alert for high-severity reset bursts."""
    if burst.get("baseline_mean") is not None:
        baseline_text = f"normally ~**{burst['baseline_mean']}** in a 5-day window"
    else:
        baseline_text = "no baseline yet, alerting on absolute floor"

    embed = {
        "title": f"⚠️ War Preparation Detected: {country_name}",
        "color": COLOUR_RESET_BURST,
        "description": (
            f"**{burst['current']}** sampled citizens reset their skills in "
            f"the last {RESET_WINDOW_DAYS} days ({baseline_text}). Skill resets "
            f"cost gold, so a burst is rarely casual. It usually means people "
            f"are repurposing themselves for combat."
        ),
        "fields": [
            {
                "name": "Sample",
                "value": (
                    f"Top **{snap['sample_size']}** active citizens "
                    f"(level ≥ {MIN_LEVEL}, online in last {ACTIVITY_WINDOW_DAYS} days)"
                ),
                "inline": False,
            },
            {
                "name": "Current allocation",
                "value": (
                    f"**{snap['combat_ratio']:.1f}%** combat / "
                    f"**{100 - snap['combat_ratio']:.1f}%** economy"
                ),
                "inline": False,
            },
            {
                "name": "Trend",
                "value": trend_field(history, snap),
                "inline": False,
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_embed({"embeds": [embed]})


def send_digest(flagged, now):
    """One summary embed of every flagged country, severity-ranked."""
    if not flagged:
        return

    severity_order = {"high": 0, "med": 1, "low": 2}
    flagged.sort(key=lambda f: (severity_order.get(f["severity"], 9), f["name"]))

    fields = []
    for f in flagged[:25]:
        snap = f["snap"]
        if f["kind"] == "burst":
            icon = "🔴" if f["severity"] == "high" else "🟠"
            burst = f["detection"]
            baseline = (
                f"baseline ~{burst['baseline_mean']:.1f}"
                if burst.get("baseline_mean") is not None
                else "no baseline"
            )
            value = (
                f"**{burst['current']}** resets in {RESET_WINDOW_DAYS}d "
                f"({baseline}) • {snap['combat_ratio']:.0f}% combat"
            )
        else:
            icon = "🟡"
            creep = f["detection"]
            value = (
                f"{creep['old_ratio']:.0f}% → **{snap['combat_ratio']:.0f}%** "
                f"(+{creep['delta_pp']:.1f} pp, ~{RATIO_LOOKBACK_DAYS}d)"
            )
        fields.append({
            "name": f"{icon} {f['name']}",
            "value": value,
            "inline": False,
        })

    counts = {"high": 0, "med": 0, "low": 0}
    for f in flagged:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    parts = []
    if counts["high"]:
        parts.append(f"**{counts['high']}** urgent")
    if counts["med"]:
        parts.append(f"**{counts['med']}** preparing")
    if counts["low"]:
        parts.append(f"**{counts['low']}** drifting")
    summary = ", ".join(parts) if parts else "all quiet"

    description = f"{summary} ({len(flagged)} total)."
    if len(flagged) > 25:
        description += f"\nShowing top 25; {len(flagged) - 25} more not displayed."

    embed = {
        "title": "🛡️ War Watch · Daily Digest",
        "color": COLOUR_DIGEST,
        "description": description,
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    post_embed({"embeds": [embed]})


# ---------- Main ----------

def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    countries = fetch_countries()
    regions = fetch_regions()
    watchlist = build_watchlist(regions)

    country_name = {c["_id"]: c.get("name", c["_id"]) for c in countries if c.get("_id")}
    total_border_regions = sum(len(v) for v in watchlist.values())
    print(
        f"Loaded {len(countries)} countries. Watchlist: {len(watchlist)} "
        f"controlling {total_border_regions} border regions."
    )
    for cid in sorted(watchlist, key=lambda c: country_name.get(c, c)):
        print(f"  {country_name.get(cid, cid)}: {', '.join(sorted(watchlist[cid]))}")

    if not watchlist:
        print("Empty watchlist, nothing to sample. Exiting.")
        return

    # Sample only countries that currently control a region neighbouring Ireland
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
        print(f"sample={snap['sample_size']}({mode}) "
              f"resets5d={snap['resets_5d']} "
              f"combat={snap['combat_ratio']:.1f}%")

    flagged = []
    high_sev_sent = 0
    # Preserve state for countries not in this run's watchlist — if they
    # rejoin later we keep their history available.
    new_countries_state = dict(state.get("countries", {}))

    for cid, snap in snapshots.items():
        prev = state.get("countries", {}).get(cid, {})
        history = prev.get("history", [])

        burst = detect_reset_burst(history, snap["resets_5d"])
        creep = detect_ratio_creep(history, snap["combat_ratio"], now)

        if burst:
            high = is_high_severity(burst)
            flagged.append({
                "name": snap["name"],
                "kind": "burst",
                "severity": "high" if high else "med",
                "snap": snap,
                "detection": burst,
            })
            if high:
                try:
                    send_high_severity_burst(snap["name"], snap, burst, history)
                    high_sev_sent += 1
                except Exception as e:
                    print(f"  failed burst alert for {snap['name']}: {e}", file=sys.stderr)

        if creep:
            flagged.append({
                "name": snap["name"],
                "kind": "creep",
                "severity": "low",
                "snap": snap,
                "detection": creep,
            })

        new_history = (history + [{
            "ts": now.isoformat(),
            "resets_5d": snap["resets_5d"],
            "combat_ratio": snap["combat_ratio"],
        }])[-HISTORY_LEN:]

        new_countries_state[cid] = {
            "name": snap["name"],
            "sample_size": snap["sample_size"],
            "resets_5d": snap["resets_5d"],
            "combat_ratio": snap["combat_ratio"],
            "known_veterans": snap.get("known_veterans", []),
            "last_discovery": snap.get("last_discovery"),
            "history": new_history,
        }

    if flagged:
        try:
            send_digest(flagged, now)
        except Exception as e:
            print(f"  failed digest: {e}", file=sys.stderr)

    state["version"] = STATE_VERSION
    state["last_run"] = now.isoformat()
    state["countries"] = new_countries_state
    save_state(state)
    print(
        f"Done. Snapshots: {len(snapshots)}. "
        f"Flagged: {len(flagged)} ({high_sev_sent} urgent, "
        f"{sum(1 for f in flagged if f['severity'] == 'med')} preparing, "
        f"{sum(1 for f in flagged if f['severity'] == 'low')} drifting)."
    )


if __name__ == "__main__":
    main()