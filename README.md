# warera-war-alert

Discord bot that watches War Era for countries gearing up for war against Ireland. Each day, samples active citizens of a small watchlist (Ireland's neighbours and declared enemies) for skill resets and shifts in combat-vs-economy skill allocation. Posts an embed when something looks off.

Sibling of [`warera-tools-ireland`](https://github.com/to-ie/warera-tools-ireland). Same proxy, same Discord style, separate repo for focus. Will eventually fold into [tools.we-ie.com](https://tools.we-ie.com/) as a "War Watch" tab.

## What it detects

**Reset bursts (red).** Skill resets cost gold, so a burst of them in one country within 5 days is rarely casual. Citizens are almost always repurposing themselves from economy to combat. High urgency.

**Ratio creep (yellow).** Slower-burn signal: citizens gradually allocating new skill points to combat rather than economy. When a country's median combat ratio climbs 20+ percentage points above where it was a week ago, that's the softer signal.

A single run can fire both alerts for the same country.

## Who it watches

The bot doesn't monitor every country in the game, only those that pose a credible threat. The watchlist rebuilds every run from two dynamic sources:

**Border controllers.** Countries currently controlling a region that physically borders Ireland or sits within sea-attack range. The list of border *regions* is hardcoded in `BORDER_REGION_NAMES` at the top of `war_alert.py`; the *countries* controlling those regions are resolved live each run from `region.getRegionsObject`. If UK loses Wales to France tomorrow, France gets added automatically.

**Diplomatic enemies.** Countries listed on Ireland's country object as `swornEnemy` or in `activeWars`. Pulled fresh each run from `country.getCountryById`.

A country can appear for either reason or both. If Ireland expands its territory, just add the new neighbour region names to `BORDER_REGION_NAMES`. No other code changes needed.

Typical watchlist size: 5-10 countries (versus 180 globally), so runs complete in 3-5 minutes instead of 30.

## How it samples

For each watchlisted country, paginate the citizen list (newest-first) until 25 qualifying citizens are found or the list is exhausted, up to 15 pages. Qualifying = level ≥ 20 and connected within the last 14 days.

Per sample, count resets in the last 5 days and compute the median combat ratio. Combat skills: `attack`, `precision`, `dodge`, `armor`, `lootChance`, `criticalChance`, `criticalDamages`, `health`. Economy: `companies`, `entrepreneurship`, `production`, `management`. Energy and hunger are excluded as ambiguous.

Citizen IDs from each successful sample are cached in `war_state.json`. The next 6 daily runs reuse them directly (one quick lookup per country); discovery (full pagination) re-runs once a week to refresh the cohort. Steady-state API cost is roughly 500-1,000 calls per run.

## Detection thresholds

Reset burst fires when the current 5-day reset count is 2σ above the country's own 14-day rolling baseline, with an absolute floor of 5. Countries with fewer than 5 runs of history don't alert at all; the bot collects baseline silently during that period.

Ratio creep fires when the current combat ratio is at least 20 percentage points above the value recorded ~7 days ago in the rolling history. Needs at least a week of history before it can compare.

Bursts at 1.5× the threshold or 10+ absolute resets trigger their own dedicated Discord message. Everything else gets summarised in a daily digest embed with severity icons (🔴 urgent burst, 🟠 standard burst, 🟡 ratio creep).

## Setup

1. **Repo files.** `war_alert.py`, `requirements.txt` (just `requests`), `.github/workflows/war_alert.yml`, and this README.
2. **Discord webhook.** New channel for war alerts. Channel settings → Integrations → Webhooks → copy URL.
3. **GitHub secret.** Settings → Secrets and variables → Actions. Name `WAR_DISCORD_WEBHOOK_URL`, value = webhook URL.
4. **First run.** Actions → "War preparation alert" → Run workflow. Takes 3-5 minutes, then commits `war_state.json`.
5. **First week is silent.** No baselines = no alerts. After ~5 daily runs the per-country baselines kick in and detection starts working properly. Don't change thresholds during this period.

## Tuning

Constants at the top of `war_alert.py`. The ones you'll most likely touch:

| Constant | Default | What it does |
|---|---|---|
| `BORDER_REGION_NAMES` | 16 regions | Regions that physically border Ireland. Update if Ireland expands. |
| `RESET_FLOOR` | 5 | Sanity-check minimum resets to ever alert |
| `RATIO_CREEP_PP` | 20 | Combat-ratio gain (pp) that triggers a yellow creep alert |
| `MIN_LEVEL` | 20 | Minimum citizen level included in sampling |
| `HIGH_SEVERITY_FLOOR` | 10 | Absolute resets that promote a burst to its own message |

## State

`war_state.json` lives in the repo and is auto-committed every run. Per-country aggregate snapshots with a 14-entry rolling history each, plus cached citizen IDs for fast next-run sampling. Stays well under 200KB.

Countries that drop out of the watchlist (e.g., lose their border region) keep their state. If they later rejoin the watchlist, their history is still there.

Delete `war_state.json` to reset baselines. The next ~5 runs will be silent while it rebuilds.

## Known gaps

State-owned ammo production isn't detected. Countries that stockpile through their own companies without citizen retraining will fly under the radar.

Countries genuinely too small to have 10 active level-20+ citizens still show as "skipped" in logs, even if watchlisted.

Impulsive wars without a prep window aren't caught. This bot detects planned mobilisations, not declarations from peacetime.

Diplomatic field names (`swornEnemy`, `activeWars`) are best guesses. If a watchlist log is missing declared enemies you'd expect from in-game diplomacy, the field names may be different in the actual response and need correcting.

## Credits

Data via the [warera-proxy](https://warera-proxy.toie.workers.dev/) gateway and [warerastats.io](https://warerastats.io/). Made by toie.