# warera-war-alert

Discord bot that watches War Era for countries gearing up for war. Each day, samples every country's active citizens for skill resets and shifts in combat-vs-economy skill allocation. Posts an embed when something looks off.

Sibling of [`warera-tools-ireland`](https://github.com/to-ie/warera-tools-ireland) — same proxy, same Discord style, separate repo for focus. Will eventually fold into [tools.we-ie.com](https://tools.we-ie.com/) as a "War Watch" tab.

## What it detects

**Reset bursts (red).** Skill resets cost gold, so a burst of them in one country within 5 days is rarely casual — citizens are almost always repurposing. High urgency.

**Ratio creep (yellow).** Slower-burn signal: citizens gradually allocating new skill points to combat rather than economy. When a country's average combat ratio climbs 20+ percentage points above where it was a week ago, that's the softer signal.

A single run can fire both alerts for the same country.

## How it samples

For each country, paginate the citizen list (newest-first) until 25 qualifying citizens are found or the list is exhausted — max 5 pages. Qualifying = level ≥ 20 and connected within the last 14 days. Big countries terminate on page 1; small ones walk deeper to find their veterans.

Per sample, count resets in the last 5 days and compute the mean combat ratio. Combat skills: `attack`, `precision`, `dodge`, `armor`, `lootChance`, `criticalChance`, `criticalDamages`, `health`. Economy: `companies`, `entrepreneurship`, `production`, `management`. Energy and hunger are excluded as ambiguous.

Roughly 7,000-8,000 API calls per daily run, 10-30 minutes wall time.

## Detection thresholds

Reset burst fires when the current 5-day reset count is 2σ above the country's own 14-day rolling baseline, with an absolute floor of 5. Countries with fewer than 5 runs of history use the floor only.

Ratio creep fires when the current combat ratio is at least 20 percentage points above the value recorded ~7 days ago in the rolling history.

## Setup

1. **Repo files.** `war_alert.py`, `requirements.txt` (just `requests`), `.github/workflows/war_alert.yml`, and this README.
2. **Discord webhook.** New channel for war alerts (separate from migration alerts). Channel settings → Integrations → Webhooks → copy URL.
3. **GitHub secret.** Settings → Secrets and variables → Actions. Name `WAR_DISCORD_WEBHOOK_URL`, value = webhook URL.
4. **First run.** Actions → "War preparation alert" → Run workflow. Takes 10-30 minutes, then commits `war_state.json`.
5. **First week is loud.** No baselines = alerts fire on the absolute floor. After ~5 runs the per-country baselines kick in and false positives drop sharply. Don't tune in week one.

## Tuning

Constants at the top of `war_alert.py`. The ones you'll most likely touch:

| Constant | Default | What it does |
|---|---|---|
| `RESET_FLOOR` | 5 | Minimum resets to ever alert |
| `RATIO_CREEP_PP` | 20 | Combat-ratio gain (pp) for yellow alert |
| `MIN_LEVEL` | 20 | Minimum citizen level to include |
| `MAX_PAGES` | 5 | Pagination depth (raise for more small-country coverage, at higher cost) |

## State

`war_state.json` lives in the repo and is auto-committed every run. Holds per-country aggregate snapshots with a 14-entry rolling history each — stays under ~200KB. Delete it to reset baselines (next run will be loud for ~5 cycles).

## Known gaps

State-owned ammo production isn't detected — countries that stockpile through their own companies without citizen retraining will fly under the radar. Would need a separate watcher on `transaction.getPaginatedTransactions`.

Tiny countries with fewer than 10 active level-20+ citizens still show as "skipped" in logs. They can't mobilize meaningfully anyway, so ignoring them is fine.

Impulsive wars without a prep window aren't caught. This bot detects planned mobilizations, not declarations from peacetime.

## Credits

Data via the [warera-proxy](https://warera-proxy.toie.workers.dev/) gateway and [warerastats.io](https://warerastats.io/). Made by toie.