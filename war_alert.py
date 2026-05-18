"""
Detects countries gearing up for war by sampling active citizens for two
signals: bursts of skill resets (a strong leading indicator that people
are repurposing themselves) and gradual climbs in the combat/economy
skill-allocation ratio.

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
  - Aggregate three numbers:
      * new_resets: citizens whose lastSkillsResetAt advanced since the
        previous run (true event count, not windowed — avoids the
        smearing the old resets_5d metric had)
      * combat_ratio: median combat-skill ratio across the full sample
      * resetter_combat_ratio: median combat ratio of just the citizens
        who reset since the previous run (null if none reset)

Diff against state.json's per-country history (14-day rolling window):
  - Reset burst: new_resets >= 2σ above the country's rolling baseline,
    or >= RESET_FLOOR (whichever is greater)
  - Ratio creep: combat ratio >= 20 percentage points above where it
    was ~7 days ago

Output:
  - One digest embed per run listing every flagged country with severity
    icons (red/orange/yellow), plus any countries that stood down since
    the last run
  - Dedicated alerts for high-severity bursts only (>= 1.5× threshold or
    10+ new resets), with sparkline trends and resetter-allocation context.
    Per-country cooldown on these — by default 3 days, unless severity
    escalates by 50%+
  - Health warnings to Discord if the watchlist comes back empty or
    fewer than half of watchlisted countries can be sampled

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
STATE_VERSION = 4
IRELAND_COUNTRY_ID = "6813b6d446e731854c7ac7fe"

# Watchlist scope
BORDER_HOPS = 3                # how far out to walk Ireland's adjacency graph.
                               # 1 = direct borders only; 3 catches countries
                               # that could project force via 1-2 intermediate
                               # conquests.

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
HISTORY_LEN = 14               # rolling history kept per country
MIN_HISTORY_FOR_BASELINE = 5
BASELINE_SIGMA = 2.0
RESET_FLOOR = 3                # absolute minimum new_resets to consider alerting.
                               # Lower than the old resets_5d floor of 5 because
                               # new_resets is a per-run event count, not a 5-day
                               # rolling total.
RATIO_CREEP_PP = 20.0          # combat-ratio gain (percentage points) that triggers
RATIO_LOOKBACK_DAYS = 7

# Severity (bursts above these get their own alert; everything else goes in the digest)
HIGH_SEVERITY_FACTOR = 1.5     # burst >= this × threshold counts as urgent
HIGH_SEVERITY_FLOOR = 10       # OR >= this many absolute new resets

# Urgent alert cooldown (per-country)
URGENT_COOLDOWN_DAYS = 3        # gap between repeat urgent alerts for the same country
URGENT_ESCALATION_FACTOR = 1.5  # unless current is this much higher than last alert

# Pipeline health
HEALTH_SNAPSHOT_RATE = 0.5     # warn if fewer than this fraction of watchlist sampled

# Embed colours
COLOUR_RESET_BURST = 0xED4245  # red: concrete preparation signal
COLOUR_RATIO_CREEP = 0xFEE75C  # yellow: softer, longer-running shift
COLOUR_DIGEST = 0x5865F2       # blurple: neutral roundup colour
COLOUR_HEALTH_WARN = 0xFEE75C  # yellow: degraded
COLOUR_HEALTH_CRIT = 0xED4245  # red: critical

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


def find_border_countries(regions_obj, country_id, max_hops=None):
    """Returns {country_id: [region_names]} for foreign countries
    controlling at least one region within max_hops of the given
    country's territory.

    BFS across the game's adjacency graph (region.neighbors), starting
    from every region the country controls. At max_hops=1 this is
    direct borders; higher values catch countries that could plausibly
    project force via intermediate conquests. Own-country regions are
    excluded from the result but are still traversable as starting
    points.
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
    """Returns dict country_id -> list of reasons ('at war')."""
    enemies = {}
    if not ireland:
        return enemies
    for cid in ireland.get("warsWith") or []:
        if isinstance(cid, str):
            enemies.setdefault(cid, []).append("at war")
    return enemies


def build_watchlist(regions_obj, ireland):
    """Map of country_id -> {border_regions, diplomatic} describing why
    each country is being monitored. Nearby countries come from the
    adjacency graph (BORDER_HOPS deep); diplomatic from warsWith.
    """
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

def process_reset_events(sample, prev_user_resets):
    """Compare each sampled user's reset timestamp to last run's value.

    Returns (new_resets count, resetter_ratios list, updated user_resets dict).
    A reset only counts when we have a previous observation to compare against
    AND the current timestamp is strictly newer. First-time-seen users seed
    the cache without counting — keeps the metric a true event stream.
    """
    new_user_resets = {}
    new_resets = 0
    resetter_ratios = []

    for user in sample:
        uid = user.get("_id")
        if not uid:
            continue

        last_reset_iso = (user.get("dates") or {}).get("lastSkillsResetAt")
        last_reset = parse_iso(last_reset_iso)
        # Filter "seeded at account creation" pseudo-resets
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

        # Carry forward the most current reset timestamp we have for this user
        if last_reset_iso:
            new_user_resets[uid] = last_reset_iso
        elif prev_iso:
            new_user_resets[uid] = prev_iso

    return new_resets, resetter_ratios, new_user_resets


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

    prev_user_resets = country_state.get("user_resets") or {}
    new_resets, resetter_ratios, new_user_resets = process_reset_events(
        sample, prev_user_resets
    )

    ratios = [r for r in (combat_ratio(u) for u in sample) if r is not None]
    if not ratios:
        return None

    return {
        "name": country_name,
        "sample_size": len(sample),
        "new_resets": new_resets,
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
    """Returns dict describing the burst, or None."""
    prior = [h.get("new_resets", 0) for h in history]
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


def should_send_urgent(prev_country, burst, now):
    """Per-country cooldown for urgent burst alerts. Send if no recent
    alert, OR if cooldown has passed, OR if current count escalated
    enough vs the last sent alert.
    """
    last_iso = prev_country.get("last_urgent_alert")
    if not last_iso:
        return True
    last = parse_iso(last_iso)
    if last is None:
        return True
    if (now - last).days >= URGENT_COOLDOWN_DAYS:
        return True
    last_count = prev_country.get("last_urgent_count") or 0
    return burst["current"] >= last_count * URGENT_ESCALATION_FACTOR


# ---------- State ----------

def load_state():
    if not STATE_FILE.exists():
        return {"version": STATE_VERSION, "countries": {}, "flagged_last_run": []}
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def migrate(state):
    """Bring older state files up to the current schema. Idempotent and
    incremental — each version bump runs once per file. Future schema
    changes should add another `if version < N` block.
    """
    version = state.get("version", 1)

    if version < 4:
        # v4 changes:
        #  - Reset counting switched from windowed (resets_5d, 5-day count)
        #    to per-user event tracking (new_resets, count of resets since
        #    last run). Old history values aren't comparable, so we drop
        #    history; baselines rebuild over ~5 runs.
        #  - Added user_resets dict per country (timestamps for change detection).
        #  - Added resetter_combat_ratio (median combat ratio of those who reset).
        #  - Added flagged_last_run for stand-down detection.
        #  - Added last_urgent_alert / last_urgent_count for cooldowns.
        for country in state.get("countries", {}).values():
            country.pop("history", None)
            country.pop("resets_5d", None)
            country.setdefault("user_resets", {})
            country.setdefault("last_urgent_alert", None)
            country.setdefault("last_urgent_count", None)
        state.setdefault("flagged_last_run", [])
        state["version"] = 4
        print("Migrated state v3 → v4 (dropped history, added per-user reset tracking).")

    return state


# ---------- Alerts ----------

def post_embed(payload):
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()


def safe_post(embed, label):
    """Wrap post_embed with logging so a broken webhook doesn't break the run."""
    try:
        post_embed({"embeds": [embed]})
        return True
    except Exception as e:
        print(f"  failed to post {label}: {e}", file=sys.stderr)
        return False


def trend_field(history, snap):
    """Sparkline lines showing reset count and combat ratio trends."""
    if len(history) < 2:
        return "Not enough history yet."
    reset_series = [h.get("new_resets", 0) for h in history] + [snap["new_resets"]]
    ratio_series = [h["combat_ratio"] for h in history] + [snap["combat_ratio"]]
    return (
        f"`{sparkline(reset_series)}` new resets/run "
        f"({reset_series[0]} → {reset_series[-1]})\n"
        f"`{sparkline(ratio_series)}` combat ratio "
        f"({ratio_series[0]:.0f}% → {ratio_series[-1]:.0f}%)"
    )


def send_high_severity_burst(country_name, snap, burst, history):
    """Dedicated alert for high-severity reset bursts."""
    if burst.get("baseline_mean") is not None:
        baseline_text = f"normally ~**{burst['baseline_mean']}** per run"
    else:
        baseline_text = "no baseline yet, alerting on absolute floor"

    fields = [
        {
            "name": "Sample",
            "value": (
                f"Top **{snap['sample_size']}** active citizens "
                f"(level ≥ {MIN_LEVEL}, online in last {ACTIVITY_WINDOW_DAYS} days)"
            ),
            "inline": False,
        },
        {
            "name": "Overall allocation",
            "value": (
                f"**{snap['combat_ratio']:.1f}%** combat / "
                f"**{100 - snap['combat_ratio']:.1f}%** economy"
            ),
            "inline": False,
        },
    ]

    if snap.get("resetter_combat_ratio") is not None:
        rcr = snap["resetter_combat_ratio"]
        fields.append({
            "name": "What resetters are rebuilding into",
            "value": (
                f"**{rcr:.1f}%** combat — "
                + ("strongly combat-skewed" if rcr >= 70
                   else "balanced" if rcr >= 40
                   else "economy-leaning")
            ),
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
        "description": (
            f"**{burst['current']}** sampled citizens reset their skills "
            f"since the last run ({baseline_text}). Skill resets cost gold, "
            f"so a burst is rarely casual. It usually means people are "
            f"repurposing themselves for combat."
        ),
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return safe_post(embed, f"burst alert for {country_name}")


def send_digest(flagged, stood_down, now):
    """One summary embed per run. Lists flagged countries severity-ranked,
    and appends a stood-down section if any. Caller should only invoke
    when at least one of flagged or stood_down is non-empty.
    """
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
            rcr_str = ""
            if snap.get("resetter_combat_ratio") is not None:
                rcr_str = f", resetters {snap['resetter_combat_ratio']:.0f}%"
            value = (
                f"**{burst['current']}** new resets "
                f"({baseline}) • {snap['combat_ratio']:.0f}% combat{rcr_str}"
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

    if stood_down:
        stood_names = sorted(name for _, name in stood_down)
        fields.append({
            "name": "✅ Stood down",
            "value": ", ".join(stood_names),
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
    summary = ", ".join(parts) if parts else None

    if flagged:
        description = f"{summary} ({len(flagged)} total)."
        if len(flagged) > 25:
            description += f"\nShowing top 25; {len(flagged) - 25} more not displayed."
        if stood_down:
            description += f" {len(stood_down)} stood down."
    else:
        # Only stand-downs, no new flags
        description = f"All quiet now — {len(stood_down)} stood down since last run."

    embed = {
        "title": "🛡️ War Watch · Daily Digest",
        "color": COLOUR_DIGEST,
        "description": description,
        "fields": fields,
        "timestamp": now.isoformat(),
    }
    return safe_post(embed, "digest")


def send_health_alert(message, critical=False):
    """Post a Discord embed when something's wrong with the pipeline."""
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
        # Persist any migration changes even though we did nothing else
        state["last_run"] = now.isoformat()
        save_state(state)
        return

    # Sample only watchlisted countries
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
        rcr_str = f" rcr={rcr:.1f}%" if rcr is not None else ""
        print(f"sample={snap['sample_size']}({mode}) "
              f"new_resets={snap['new_resets']} "
              f"combat={snap['combat_ratio']:.1f}%{rcr_str}")

    # Health check: did we sample enough of the watchlist?
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
    # Preserve state for countries not in this run's watchlist — if they
    # rejoin later we keep their history available.
    new_countries_state = dict(state.get("countries", {}))

    for cid, snap in snapshots.items():
        prev_country = state.get("countries", {}).get(cid, {})
        history = prev_country.get("history", [])

        burst = detect_reset_burst(history, snap["new_resets"])
        creep = detect_ratio_creep(history, snap["combat_ratio"], now)

        new_history = (history + [{
            "ts": now.isoformat(),
            "new_resets": snap["new_resets"],
            "combat_ratio": snap["combat_ratio"],
            "resetter_combat_ratio": snap.get("resetter_combat_ratio"),
        }])[-HISTORY_LEN:]

        new_country = {
            "name": snap["name"],
            "sample_size": snap["sample_size"],
            "new_resets": snap["new_resets"],
            "combat_ratio": snap["combat_ratio"],
            "resetter_combat_ratio": snap.get("resetter_combat_ratio"),
            "known_veterans": snap.get("known_veterans", []),
            "user_resets": snap.get("user_resets", {}),
            "last_discovery": snap.get("last_discovery"),
            "last_urgent_alert": prev_country.get("last_urgent_alert"),
            "last_urgent_count": prev_country.get("last_urgent_count"),
            "history": new_history,
        }

        if burst:
            high = is_high_severity(burst)
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "burst",
                "severity": "high" if high else "med",
                "snap": snap,
                "detection": burst,
            })
            if high and should_send_urgent(prev_country, burst, now):
                if send_high_severity_burst(snap["name"], snap, burst, history):
                    high_sev_sent += 1
                    new_country["last_urgent_alert"] = now.isoformat()
                    new_country["last_urgent_count"] = burst["current"]

        if creep:
            flagged.append({
                "cid": cid,
                "name": snap["name"],
                "kind": "creep",
                "severity": "low",
                "snap": snap,
                "detection": creep,
            })

        new_countries_state[cid] = new_country

    # Stand-down detection: countries that were flagged last run, that we
    # sampled successfully this run, but that aren't flagged anymore.
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
    print(
        f"Done. Snapshots: {len(snapshots)}. "
        f"Flagged: {len(flagged)} ({high_sev_sent} urgent sent, "
        f"{sum(1 for f in flagged if f['severity'] == 'med')} preparing, "
        f"{sum(1 for f in flagged if f['severity'] == 'low')} drifting). "
        f"Stood down: {len(stood_down)}."
    )


if __name__ == "__main__":
    main()