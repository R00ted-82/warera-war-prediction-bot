# warera-war-alert

Discord bot that watches War Era for countries gearing up for war. Samples each country's active citizens daily, looks for skill resets and shifts in combat-vs-economy skill allocation, and posts a Discord embed when something looks off.

Sibling of [`warera-tools-ireland`](https://github.com/to-ie/warera-tools-ireland) — same proxy, same Discord embed style, kept in a separate repo to stay focused. Expected to eventually merge into [tools.we-ie.com](https://tools.we-ie.com/) as a "War Watch" tab.

## What it watches

Two leading indicators, neither of which require a battle to have started:

**Reset bursts.** Skill resets cost gold, so people don't reset for fun. When several citizens in one country reset within a 5-day window, they're almost always repurposing — typically from economy to combat. This is the high-urgency signal.

**Ratio creep.** Slower-burn version of the same thing: even without a formal reset, citizens can spend new skill points on combat rather than economy. When a country's average combat ratio drifts up by 20+ percentage points compared to a week ago, that's the softer signal.

Reset bursts post in red, ratio creep posts in yellow.

## How it samples

For each country, every run:

1. Pull 100 citizen IDs via `user.getUsersByCountry`
2. Fetch `user.getUserLite` for each (10 concurrent)
3. Filter to citizens at level ≥ 20 who connected within the last 14 days
4. Keep the top 25 by level — that's the sample
5. Compute: how many of them reset in the last 5 days, and their mean combat-skill ratio

Roughly 5,000 API calls per run across ~200 countries, 10-30 minutes wall time. The proxy handles it fine but it's enough load that the run is scheduled 2 hours offset from the migration alert.

Skill buckets: `attack`, `precision`, `dodge`, `armor`, `lootChance`, `criticalChance`, `criticalDamages`, and `health` count as combat. `companies`, `entrepreneurship`, `production`, `management` count as economy. `energy` and `hunger` are excluded — they feed both work cycles and combat, so they don't cleanly indicate intent.

## Detection logic

**Reset burst** fires when a country's current 5-day reset count is at least 2σ above its own 14-day rolling baseline, with an absolute floor of 5 resets. Until a country has 5 runs of history, only the floor applies.

**Ratio creep** fires when a country's current combat ratio is 20+ percentage points above the value recorded ~7 days ago in its rolling history. Requires at least a week of history before it can trigger.

Both run independently, so a single country can fire both alerts in the same run if both conditions hit.

## Setup

### 1. Create the repo

New repo, name suggestion `warera-war-alert`. Drop in four files: `war_alert.py`, `requirements.txt` (one line: `requests`), `.github/workflows/war_alert.yml`, and this README.

### 2. Discord webhook

Create a new channel for war alerts — keep it separate from migration alerts since the urgency is different. In channel settings → Integrations → Webhooks, create a webhook and copy the URL.

### 3. GitHub secret

Repo Settings → Secrets and variables → Actions → New repository secret:

- Name: `WAR_DISCORD_WEBHOOK_URL`
- Value: the webhook URL from step 2

### 4. First run

Actions tab → "War preparation alert" → "Run workflow". Check the log: should see snapshots for most countries, a handful skipped (insufficient sample), then a commit creating `war_state.json`.

### 5. Wait it out

The first run will be the loudest one this bot ever fires. With no history anywhere, the reset detector falls back to the absolute floor (5 resets in a sample of 25 over 5 days), so any moderately active country can trip it. After ~5 runs, per-country baselines kick in and false positives drop sharply. Don't tune thresholds during the first week.

## Configuration

All tuning knobs are constants at the top of `war_alert.py`.

| Constant | Default | Purpose |
|---|---|---|
| `ENUM_LIMIT` | 100 | Citizens enumerated per country |
| `SAMPLE_TOP_N` | 25 | Of those, kept after filtering |
| `MIN_LEVEL` | 20 | Lowest level included in sample |
| `MIN_SAMPLE` | 10 | Skip country if fewer eligible citizens |
| `ACTIVITY_WINDOW_DAYS` | 14 | "Active" = connected within this many days |
| `RESET_WINDOW_DAYS` | 5 | Look-back window for reset counting |
| `BASELINE_SIGMA` | 2.0 | σ above baseline that triggers reset alert |
| `RESET_FLOOR` | 5 | Absolute minimum resets to alert (overrides baseline if higher) |
| `RATIO_CREEP_PP` | 20.0 | Combat-ratio gain in percentage points that triggers |
| `RATIO_LOOKBACK_DAYS` | 7 | Reference window for ratio comparison |
| `MAX_WORKERS` | 10 | Concurrent fetches against the proxy |

## State file

`war_state.json` lives in the repo and gets committed by the workflow after every run. Holds per-country aggregate snapshots with a 14-entry rolling history each — total size stays well under 200KB even with 200 countries.

Don't hand-edit unless you want to reset baselines. If you ever need to start fresh, delete the file and the next run will reseed (and be loud for ~5 runs again).

## What's not detected

A few honest gaps worth knowing about:

Countries that stockpile via state-owned ammo production rather than citizen retraining will not show up here. Their citizens' skill allocations stay stable. If that becomes a concern, the natural addition is a weapon-stockpiling watcher using `transaction.getPaginatedTransactions` filtered by country and ammo item codes.

Small countries where fewer than 10 of the 100 newest citizens are level-20 and active will show as "skipped (insufficient sample)" in the run log. If specific countries you care about consistently get skipped, lowering `MIN_LEVEL` is the lever — but be aware that low-level players have very few skill points spent, which makes the ratio metric noisy.

Sudden declarations from peacetime without any measurable prep window. Some conflicts just don't have a leading indicator. This bot catches the planned ones, not the impulsive ones.

## Local development

```bash
pip install -r requirements.txt
DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...' python war_alert.py
```

Use a webhook to a private test channel — full runs do post real alerts.

## Workflow file

```yaml
name: War preparation alert
on:
  schedule:
    - cron: '0 8 * * *'   # 08:00 UTC daily
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python war_alert.py
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.WAR_DISCORD_WEBHOOK_URL }}
      - name: Commit state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add war_state.json
          git diff --quiet && git diff --staged --quiet || git commit -m "Update war_state.json"
          git push
```

## Credits

Data via the [warera-proxy](https://warera-proxy.toie.workers.dev/) cached gateway and [warerastats.io](https://warerastats.io/). Made by toie.