"""
Detects countries gearing up for war by sampling active citizens for two
signals: bursts of skill resets (a strong leading indicator that people are
repurposing themselves) and gradual climbs in the combat/economy skill-
allocation ratio.

Per run, for every country:
  - Paginate user.getUsersByCountry, fetching lite profiles per page, until
    we have 25 qualifying citizens (level >= 20, active in last 14 days) or
    the country's citizen list is exhausted (max 5 pages)
  - Aggregate: count of resets in the last 5 days, mean combat-skill ratio

Diff against state.json's per-country history (14-day rolling window):
  - Reset burst — current 5-day reset count exceeds 2σ above the country's
    own rolling baseline (or RESET_FLOOR, whichever is greater)
  - Ratio creep — current combat ratio is ≥20 percentage points above
    where it was ~7 days ago

First ~5 runs collect baseline silently. Posts Discord embeds when
triggered. Sibling of alert.py — same Discord embed style, same repo
conventions, separate state file.
"""

import json
import os
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
TOOLS_URL = "https://tools.we-ie.com/"

# Sampling per country
ENUM_LIMIT = 100               # citizens per page via user.getUsersByCountry
MAX_PAGES = 5                  # paginate up to this many pages of older citizens
SAMPLE_TOP_N = 25              # of those that pass filters, keep top-N by level
MIN_LEVEL = 20                 # ignore citizens below this level
MIN_SAMPLE = 10                # skip countries with fewer eligible citizens
ACTIVITY_WINDOW_DAYS = 14      # citizen counts as "active" if connected within

# Concurrency
MAX_WORKERS = 10
HTTP_TIMEOUT = 30
RETRY_ATTEMPTS = 3

# Detection
RESET_WINDOW_DAYS = 5
HISTORY_LEN = 14               # rolling history kept per country
MIN_HISTORY_FOR_BASELINE = 5
BASELINE_SIGMA = 2.0
RESET_FLOOR = 5                # absolute minimum reset count to even consider alerting
RATIO_CREEP_PP = 20.0          # combat-ratio gain (percentage points) that triggers
RATIO_LOOKBACK_DAYS = 7

# Embed colors
COLOR_RESET_BURST = 0xED4245   # red — concrete preparation signal
COLOR_RATIO_CREEP = 0xFEE75C   # yellow — softer, longer-running shift

# Skill buckets — each skill's `level` field is the number of points spent on it.
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
            transient = ("503", "504", "no available server", "timed out", "fetch failed")
            if any(s in msg.lower() for s in transient):
                raise requests.exceptions.RequestException(f"transient: {msg}")
            raise RuntimeError(f"{endpoint} → {msg[:120]}")
        return data.get("result", {}).get("data")
    except (requests.exceptions.RequestException, json.JSONDecodeError):
        if attempt < RETRY_ATTEMPTS:
            time.sleep(0.4 * attempt)
            return trpc(endpoint, payload, attempt + 1)
        raise


def fetch_countries():
    data = trpc("country.getAllCountries", {})
    if isinstance(data, dict) and "items" in data:
        return data["items"]
    return data or []


def fetch_citizens_page(country_id, cursor=None):
    """One page of citizen IDs + the next cursor (or None if exhausted)."""
    payload = {"countryId": country_id, "limit": ENUM_LIMIT}
    if cursor:
        payload["cursor"] = cursor
    data = trpc("user.getUsersByCountry", payload) or {}
    ids = [u["_id"] for u in data.get("items", []) if u.get("_id")]
    return ids, data.get("nextCursor")


def fetch_user_lite(user_id):
    try:
        return trpc("user.getUserLite", {"userId": user_id})
    except Exception as e:
        print(f"  warn: user {user_id}: {e}", file=sys.stderr)
        return None


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


# ---------- Country sampling ----------

def sample_country(country_id, country_name, now):
    """Aggregate snapshot for a country, or None if eligible sample < MIN_SAMPLE.

    Paginates older citizens lazily: fetches a page, pulls lite profiles for
    those IDs, accumulates qualifying citizens, then either stops (enough
    qualifying / no more pages / page cap hit) or fetches another page. Big
    countries terminate on page 1; small ones may walk several pages back
    through their citizen list before finding their established players.
    """
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

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            users = [
                r for r in (
                    f.result() for f in as_completed(
                        {ex.submit(fetch_user_lite, uid) for uid in new_ids}
                    )
                ) if r is not None
            ]

        qualifying.extend(
            u for u in users
            if user_level(u) >= MIN_LEVEL and is_active(u, now)
        )

        if len(qualifying) >= SAMPLE_TOP_N or not cursor:
            break

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
        "combat_ratio": round(statistics.mean(ratios), 2),
    }


# ---------- Detection ----------

def detect_reset_burst(history, current):
    """Returns dict describing the burst, or None."""
    prior = [h["resets_5d"] for h in history]
    if len(prior) < MIN_HISTORY_FOR_BASELINE:
        # No reliable baseline yet — only fire on absolute floor
        if current >= RESET_FLOOR:
            return {"baseline_mean": None, "threshold": RESET_FLOOR, "current": current}
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
        # Not enough history yet to look back
        return None
    delta = current_ratio - closest["combat_ratio"]
    if delta >= RATIO_CREEP_PP:
        return {"old_ratio": closest["combat_ratio"], "delta_pp": round(delta, 1)}
    return None


# ---------- State ----------

def load_state():
    if not STATE_FILE.exists():
        return {"version": 2, "countries": {}}
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------- Alerts ----------

def post_embed(payload):
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()


def send_reset_burst(country_name, snap, burst):
    if burst.get("baseline_mean") is not None:
        baseline_text = f"normally ~**{burst['baseline_mean']}** in a 5-day window"
    else:
        baseline_text = "no baseline yet — alerting on absolute floor"
    embed = {
        "title": f"⚠️ War Preparation Detected — {country_name}",
        "color": COLOR_RESET_BURST,
        "description": (
            f"**{burst['current']}** sampled citizens reset their skills in "
            f"the last {RESET_WINDOW_DAYS} days ({baseline_text}). Skill resets "
            f"cost gold, so a burst is rarely casual — it usually means people "
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
                "name": "Tools",
                "value": f"[War Era · Ireland tools →]({TOOLS_URL})",
                "inline": False,
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_embed({"embeds": [embed]})


def send_ratio_creep(country_name, snap, creep):
    embed = {
        "title": f"📊 Combat Ratio Climbing — {country_name}",
        "color": COLOR_RATIO_CREEP,
        "description": (
            f"Citizens are shifting their skill allocation toward combat. "
            f"Slower-burn than a reset burst — could be early-stage prep, "
            f"could be normal drift. Worth watching."
        ),
        "fields": [
            {
                "name": "Combat ratio",
                "value": (
                    f"**{creep['old_ratio']:.1f}%** → **{snap['combat_ratio']:.1f}%** "
                    f"(+{creep['delta_pp']:.1f} pp over ~{RATIO_LOOKBACK_DAYS} days)"
                ),
                "inline": False,
            },
            {
                "name": "Sample",
                "value": (
                    f"Top **{snap['sample_size']}** active citizens "
                    f"(level ≥ {MIN_LEVEL}, online in last {ACTIVITY_WINDOW_DAYS} days)"
                ),
                "inline": False,
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_embed({"embeds": [embed]})


# ---------- Main ----------

def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    countries = fetch_countries()
    print(f"Loaded {len(countries)} countries.")

    snapshots = {}
    for i, country in enumerate(countries, 1):
        cid = country.get("_id")
        cname = country.get("name") or cid
        if not cid:
            continue
        print(f"[{i}/{len(countries)}] {cname}...", end=" ", flush=True)
        try:
            snap = sample_country(cid, cname, now)
        except Exception as e:
            print(f"failed: {e}")
            continue
        if snap is None:
            print("skipped (insufficient sample)")
            continue
        snapshots[cid] = snap
        print(f"sample={snap['sample_size']} resets5d={snap['resets_5d']} "
              f"combat={snap['combat_ratio']:.1f}%")

    alerts = 0
    new_countries_state = {}
    for cid, snap in snapshots.items():
        prev = state.get("countries", {}).get(cid, {})
        history = prev.get("history", [])

        burst = detect_reset_burst(history, snap["resets_5d"])
        if burst:
            try:
                send_reset_burst(snap["name"], snap, burst)
                alerts += 1
            except Exception as e:
                print(f"  failed reset alert for {snap['name']}: {e}", file=sys.stderr)

        creep = detect_ratio_creep(history, snap["combat_ratio"], now)
        if creep:
            try:
                send_ratio_creep(snap["name"], snap, creep)
                alerts += 1
            except Exception as e:
                print(f"  failed creep alert for {snap['name']}: {e}", file=sys.stderr)

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
            "history": new_history,
        }

    state["version"] = 2
    state["last_run"] = now.isoformat()
    state["countries"] = new_countries_state
    save_state(state)
    print(f"Done. Snapshots: {len(snapshots)}. Alerts sent: {alerts}.")


if __name__ == "__main__":
    main()