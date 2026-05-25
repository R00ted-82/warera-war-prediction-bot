# warera-war-alert

Discord bot that watches War Era for countries gearing up for war against Ireland (and ones that are visibly standing down). Every six hours, samples each watchlisted country's top active citizens for skill resets and shifts in combat-vs-economy skill allocation. Posts a digest embed when something looks worth knowing, plus dedicated alerts for high-severity events.

Sibling of [`warera-tools-ireland`](https://github.com/to-ie/warera-tools-ireland) — same proxy, same Discord style, separate repo for focus.

## What you'll see

Every run posts at most one **daily digest** embed listing flagged countries. The digest leads with a short explainer (sample = top ~25 active fighters, "combat focus" = share of skill points on combat) so first-time readers aren't guessing.

In addition, certain events get their own dedicated alert message so they don't get lost in the digest.

### Severity icons

| Icon | Meaning |
|---|---|
| 🔴 | Active mobilisation or major combat shift — direct threat indicator |
| 🟠 | Significant mobilisation or shift — caution |
| 🟡 | Minor drift toward combat — keep an eye on it |
| 🟢 | Country is standing down — reassuring |
| 🕊️ | Dedicated stand-down alert — strong de-escalation signal |
| ✅ | Country no longer flagged since last run |

Red is always danger to Ireland. Green is always reassurance. Yellow/orange sit on the mobilisation spectrum between them.

## What it detects

The bot watches five independent signals. A single country can trip multiple signals in a single run — each is reported.

**Reset bursts.** A clutch of citizens wiping and rebuilding their skills in a single day. Skill resets cost gold, so a burst is rarely casual. Fires at ≥4 resets even with no baseline; once a country has history, it also fires at 2σ above the country's own (outlier-filtered) baseline.

**Combat-intent resets.** Even one or two resets count, *if* the citizens who reset clearly rebuilt as combat fighters (≥70% combat allocation). Catches early-stage mobilisation in normally-quiet countries.

**Ratio creep / major combat shift.** The typical citizen's combat focus has risen significantly over the past day (≥30 points) or week (≥20 points). Tiered by magnitude: yellow at 20-40, orange at 40-60, red at 60+ (with dedicated alert).

**Ratio collapse / standing down.** Mirror of the above on the green side. The typical citizen's combat focus has dropped significantly. Dedicated 🕊️ alert at 50+ point drops.

**Eco-intent resets.** Mirror of combat-intent. Even one or two resets that rebuilt as workers (≤30% combat) flags as demobilising.

## Who it watches

The bot doesn't monitor every country in the game — only those that pose a credible threat. The watchlist rebuilds every run from two sources:

**Border controllers.** Countries currently controlling a region within `BORDER_HOPS` (default 3) hops of Ireland's territory. The adjacency graph is walked live from `region.getRegionsObject` each run, so if a country expands toward Ireland through conquest, they're added automatically.

**Diplomatic enemies.** Countries listed in Ireland's `warsWith` field, pulled fresh each run from `country.getCountryById`.

Typical watchlist size: 5-15 countries. Runs complete in 3-5 minutes.

## How it samples

For each watchlisted country, fetch lite profiles for the country's cached citizen IDs from prior runs. If the cache is stale (>7 days) or too many citizens have dropped below the activity threshold, paginate `user.getUsersByCountry` to rediscover the cohort. Newest-first, up to 15 pages.

"Top citizen" means: level ≥ 20 and connected within the last 14 days. The top 25 by level make the sample.

Per sample, the bot tracks:

- **new_resets** — count of citizens whose `lastSkillsResetAt` advanced since the previous run. True event count, not windowed.
- **combat_ratio** — median combat-skill allocation across the sample.
- **resetter_combat_ratio** — median combat allocation of just those who reset since last run.
- **combat_resets / eco_resets** — how many of those resets ended up strongly combat- or economy-focused.

Combat skills: `attack`, `precision`, `dodge`, `armor`, `lootChance`, `criticalChance`, `criticalDamages`, `health`. Economy: `companies`, `entrepreneurship`, `production`, `management`. Energy and hunger are excluded as ambiguous.

## Detection thresholds

| Detector | Fires at |
|---|---|
| Reset burst (absolute floor) | ≥4 resets in one run, always |
| Reset burst (σ-based) | ≥2σ above outlier-filtered rolling baseline (5+ runs of history) |
| Combat-intent reset | ≥1 reset with resetter combat ≥70% |
| Ratio creep (yellow) | ratio gained 20-40 points in 7d, or 30+ in 1d |
| Ratio creep (orange) | ratio gained 40-60 points in 7d, or equivalent 1d |
| Ratio creep (red) | ratio gained 60+ points in 7d, or equivalent 1d → dedicated alert |
| Ratio collapse (green) | mirror of creep on the falling side, ≥50 points → dedicated alert |
| Eco-intent reset | ≥1 reset with resetter combat ≤30% |

Bursts at 1.5× their threshold OR 10+ absolute resets trigger a dedicated "War Preparation Detected" alert. Red-tier ratio creep triggers a dedicated "Major Combat Shift" alert. 50+ point drops trigger "Standing Down" alerts.

All dedicated alerts have a per-country 3-day cooldown that's bypassed if severity escalates by 50%+.

### Outlier-filtered baselines

A country's own past mobilisations are excluded from its rolling baseline (any day where `new_resets ≥ 4` doesn't count toward the mean). Without this, a country that mobilised once becomes harder to detect next time. Falls back to the unfiltered series if filtering would leave too little baseline data.

### History length

The rolling history keeps `HISTORY_LEN = 56` entries per country. At the 6-hour cron cadence, that's ~14 days of context. If you change the cron cadence, scale `HISTORY_LEN` accordingly (`days_of_context × runs_per_day`).

## Setup

1. **Repo files.** `war_alert.py`, `requirements.txt` (just `requests`), `.github/workflows/war_alert.yml`, and this README.
2. **Discord webhook.** New channel for war alerts. Channel settings → Integrations → Webhooks → copy URL.
3. **GitHub secret.** Settings → Secrets and variables → Actions. Name `WAR_DISCORD_WEBHOOK_URL`, value = webhook URL.
4. **First run.** Actions → "War preparation alert" → Run workflow. Takes 3-5 minutes, then commits `war_state.json`.
5. **First day is calmer than before.** With the always-on absolute floor, you'll see real mobilisations from run 1, but the σ-based baseline detector still needs ~5 runs (~30 hours at 6h cadence) before it engages. False positives drop further after that.

## Tuning

Constants at the top of `war_alert.py`. The ones you'll most likely touch:

| Constant | Default | What it does |
|---|---|---|
| `BORDER_HOPS` | 3 | How far out from Ireland's territory to watch. 1 = direct borders only |
| `NO_BASELINE_RESET_FLOOR` | 4 | Always-on absolute floor for burst alerts |
| `RESET_FLOOR` | 3 | σ-path floor once baseline exists |
| `RATIO_CREEP_MIN` | 20.0 | Minimum 7-day combat-ratio gain for yellow creep |
| `RATIO_CREEP_RED` | 60.0 | 7-day gain that promotes to red + dedicated alert |
| `RATIO_DROP_MIN` | 20.0 | Minimum 7-day combat-ratio drop for green standdown |
| `HIGH_DEMOB_FOR_ALERT` | 50.0 | Drop that triggers dedicated standdown alert |
| `COMBAT_INTENT` | 70.0 | Resetter combat % counted as "rebuilt as combat fighter" |
| `DEMOB_RESET_INTENT` | 30.0 | Resetter combat % counted as "rebuilt as worker" |
| `MIN_LEVEL` | 20 | Minimum citizen level to include in sampling |
| `URGENT_COOLDOWN_DAYS` | 3 | Per-country cooldown between repeat urgent alerts |
| `HISTORY_LEN` | 56 | Rolling history entries per country (~14 days at 6h cadence) |

## State

`war_state.json` lives in the repo and is auto-committed every run. Holds per-country aggregate snapshots with rolling history, cached citizen IDs for fast next-run sampling, and cooldown timestamps for each alert type. Stays well under 1MB.

The state file is migrated automatically on first run after a schema change — current version is v5. Migrations are idempotent.

Delete `war_state.json` to reset baselines. The first ~5 runs after that will rely on the absolute floor only.

## Schedule

Runs every 6 hours via GitHub Actions cron (`0 */6 * * *`). At this cadence, the worst-case lag between an event and an alert is ~6 hours and the average is ~3 hours. Bump to `*/3` or `*/2` if you want it tighter; the per-country cooldowns prevent spam regardless of cadence.

The workflow has a concurrency group, so two runs can't race on the state file.

## Known gaps

**State-owned ammo production** isn't detected. Countries that stockpile through their own companies without citizen retraining will fly under the radar. Would need a separate watcher on `transaction.getPaginatedTransactions`.

**Impulsive wars without a prep window** aren't caught. This bot detects mobilisation, not declarations from peacetime. To catch attacks-in-progress, watch battle creation events directly.

**Pre-mobilisation diplomacy** isn't observable — by the time the first citizen pays gold to reset, the political decision was made days or weeks earlier. The earliest signal available in the game data is the first reset wave.

**Tiny countries** with fewer than 10 active level-20+ citizens still show as "skipped" in logs. They can't mobilise meaningfully, so ignoring them is fine.

## Credits

Data via the [warera-proxy](https://warera-proxy.toie.workers.dev/) gateway and [warerastats.io](https://warerastats.io/). Made by toie.