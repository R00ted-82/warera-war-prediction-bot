# warera-war-alert

Discord bot that watches War Era for countries gearing up for war against Ireland (and ones that are visibly standing down). Every three hours it samples each watchlisted country's most active high-level players for skill rebuilds and shifts in combat-vs-economy skill allocation, and posts a digest when something looks worth knowing, plus dedicated alerts for high-severity events. Once a day it posts a posture overview of how the whole watchlist splits between war and economy footing.

Sibling of [`warera-tools-ireland`](https://github.com/to-ie/warera-tools-ireland): same proxy, same Discord style, separate repo for focus.

## What you'll see

Every run posts at most one **daily digest** embed listing flagged countries. The digest leads with a short explainer (sample = a country's ~50 most active high-level players, "combat focus" = share of skill points on combat) so first-time readers aren't guessing.

Once a day, on the last scheduled run, the bot also posts a **posture overview** embed, a standing picture of the watchlist regardless of whether anything is flagged. See [Posture overview](#posture-overview).

In addition, certain events get their own dedicated alert message so they don't get lost in the digest.

### Severity icons

| Icon | Meaning |
|---|---|
| 🔴 | Active mobilisation or major combat shift, direct threat indicator |
| 🟠 | Significant mobilisation or shift, caution |
| 🟡 | Minor drift toward combat, keep an eye on it |
| 🟢 | Country is standing down or rebuilding toward economy, reassuring |
| 🕊️ | Dedicated stand-down alert, strong de-escalation signal |
| ✅ | Country no longer flagged since last run |
| 📊 | Daily posture overview, informational, not an alert |

Red is always danger to Ireland. Green is always reassurance. Yellow and orange sit on the mobilisation spectrum between them.

## What it detects

The bot watches several independent signals. A single country can trip multiple in one run, and each is reported.

**Reset bursts.** A cluster of players wiping and rebuilding their skills in a single check. Rebuilds cost gold, so a burst is rarely casual. Fires at the absolute floor (8+ rebuilds) even with no baseline, and once a country has history, at 2σ above its own outlier-filtered baseline. Bursts are then classified by direction: if the people who rebuilt went to combat, it's a war-prep signal (red or orange); if they clearly went to economy, it's a green stand-down signal instead, not a warning; mixed bursts stay amber as worth-watching.

**Combat-intent rebuilds.** Even a couple of rebuilds count if the players who rebuilt clearly went combat (70%+ of their skill points on combat). Fires at two or more such rebuilds, or a single one corroborated by an upward 7-day combat-focus shift of at least 5 points. Catches early-stage mobilisation in normally-quiet countries.

**Ratio creep / major combat shift.** The typical player's combat focus has risen significantly over the past day (30+ points) or week (20+ points). Tiered by magnitude: yellow at 20-40, orange at 40-60, red at 60+ (with a dedicated alert).

**Ratio collapse / standing down.** Mirror of the above on the green side. The typical player's combat focus has dropped significantly. Dedicated 🕊️ alert at 50+ point drops.

**Eco-intent rebuilds.** Mirror of combat-intent. Two or more rebuilds into economy (30% or less on combat), or a single one corroborated by a downward weekly shift, flags as demobilising.

### Flagged, holding, or stood down

When a country that was flagged last run produces no fresh signal this run, the bot doesn't just drop it. It decides between two outcomes:

- **Standing down (✅).** Combat focus has fallen meaningfully from the peak recorded while the country was flagged, and now sits below the combat-posture ceiling. Reported as de-escalating.
- **Holding at high readiness (🟠).** No new activity, but combat posture is still elevated, or Ireland is actively at war with the country. These stay on watch and are reported as a reminder, not a fresh warning. This keeps a mobilised-but-quiet country (ratio plateaued, war ongoing) from being wrongly declared a stand-down.

Low-severity items (yellow creep, single-rebuild intents that cleared the corroboration gate) roll up into one "Minor activity" line in the digest rather than each taking a full field.

## Posture overview

Once a day, the 📊 overview shows where the whole watchlist sits on the war-vs-economy spectrum, independent of any alerts:

- **Headline split.** How many countries are war-posture vs economy-posture, a red/green bar, and the average combat focus across the watchlist.
- **Four tiers.** Heavy combat (70%+), combat-leaning (50-70%), mixed (30-50%), economy-focused (under 30%), each listing its member countries with their combat percentages. Countries Ireland is at war with carry a ⚔️ marker.
- **Biggest movers.** The largest 7-day shifts toward war and toward economy. Countries with under a week of history don't appear here.

It's gated to one post per day on the first run at or after 21:00 UTC, tracked by date so a manual trigger can't double-post.

## Example alerts

What the messages look like in Discord. Wording leads with the plain event, and every percentage is labelled as combat focus so nothing floats free.

**Reset burst, combat direction** (high severity, its own message):

> **⚠️ War Preparation Detected: France**
> **18 of the top 50 players** (36%) wiped and rebuilt their skills since the last check, and they rebuilt for combat. Rebuilding costs gold, so a cluster this size is a concrete sign of war prep. This country normally sees about 1 per day.
> *Who this tracks:* the top 50 most active high-level players (level 20+, online in the last 14 days).
> *Where they stand now:* the typical one has put 72% of their skill points into combat (the other 28% on economy).
> *What the rebuilders chose:* 14 of them rebuilt into combat builds, the typical rebuild is heavily combat-focused (88% combat).

**Reset burst, economy direction** (green, good news, appears in the digest):

> 🟢 **Morocco** — 3 of the top 50 players (6%) wiped and rebuilt their skills since the last check, and they moved toward economy, not war. The ones who rebuilt now spend only 0% of their points on combat. Good news for Ireland: this points to winding down, not gearing up.

**Standing down** (big collapse, its own message):

> **🕊️ Standing Down: Belgium**
> The top players in Belgium are clearly easing off war. Their typical combat focus dropped from **58% to 0%** over the past 7 days, moving skill points back into economy. This usually means a campaign is wrapping up. **Likely no longer an immediate threat.**

**Combat-intent rebuilds** (appears in the digest):

> 🟠 **Iceland** — 2 of this country's top players have just rebuilt into combat builds, each now putting about 90% of their skill points on combat. Several people moving the same way is an early sign of mobilisation.

**The bundled daily digest** (posts on any run where something fired):

> **🛡️ War Watch · Daily Digest**
> **1** urgent, **2** preparing, **1** minor, **1** holding, **2** standing down (7 total).
> 🔴 **France** — 18 of the top 50 players (36%) wiped and rebuilt. 14 of them rebuilt into combat builds, putting 88% of their points into combat. Concrete sign of war prep.
> 🟠 **Lithuania** — Significant combat shift (mobilising): the typical top player's combat focus climbed from 40% to 81% over the past 7 days.
> 🟡 **Minor activity** — *soft signals, below threshold:* Germany (drifting toward combat)
> 🟠 **Holding at high readiness** — *previously mobilised, quiet now, still elevated:* Sweden, 84% combat focus (peaked 89% on May 26)
> 🟢 **Morocco** — 3 of the top 50 players (6%) wiped and rebuilt, and they moved toward economy, not war. Good news for Ireland.
> ✅ **No longer flagged** — Belgium, combat focus dropped 58% to 0% (de-escalating)

## Who it watches

The bot doesn't monitor every country in the game, only those that pose a credible threat. The watchlist rebuilds every run from two sources:

**Border controllers.** Countries controlling a region within `BORDER_HOPS` (default 3) hops of Ireland's territory. The walk is anchored on Ireland's fixed home region IDs (`IRELAND_REGION_IDS`) rather than whatever Ireland currently owns, so it keeps working while Ireland is **occupied**, when its home regions are held by an invader and would otherwise match nothing. Regions Ireland currently owns are folded into the anchor too, so territorial **expansions** through conquest are picked up automatically. The adjacency graph is walked live from `region.getRegionsObject` each run. The run log prints a `[debug]` line showing regions matched and an `OCCUPIED` note when Ireland holds no territory, so a silently-shrunk watchlist is visible.

**Diplomatic enemies.** Countries listed in Ireland's `warsWith` field, pulled fresh each run from `country.getCountryById`.

Typical watchlist size: 5-15 countries. Runs complete in 3-5 minutes.

## How it samples

For each watchlisted country, fetch lite profiles for the country's cached player IDs from prior runs. If the cache is stale (>7 days) or too many players have dropped below the activity threshold, paginate `user.getUsersByCountry` to rediscover the cohort. Newest-first, up to 15 pages.

A qualifying player is level 20+ and connected within the last 14 days. The top 50 by level make the sample (fewer for small countries, with `MIN_SAMPLE` as the floor below which a country is skipped). Note this is the highest-level **active players**, not players built for combat, so an economy-heavy country's sample is mostly workers, which is why the wording says "players" throughout.

Per sample, the bot tracks:

- **new_resets** — count of players whose `lastSkillsResetAt` advanced since the previous run. True event count, not windowed.
- **combat_ratio** — median combat-skill allocation across the sample.
- **resetter_combat_ratio** — median combat allocation of just those who rebuilt since last run.
- **combat_resets / eco_resets** — how many of those rebuilds ended up strongly combat- or economy-focused.

Combat skills: `attack`, `precision`, `dodge`, `armor`, `lootChance`, `criticalChance`, `criticalDamages`, `health`. Economy: `companies`, `entrepreneurship`, `production`, `management`. Energy and hunger are excluded as ambiguous.

## Detection thresholds

| Detector | Fires at |
|---|---|
| Reset burst (absolute floor) | 8+ rebuilds in one run, always |
| Reset burst (σ-based) | 2σ above outlier-filtered rolling baseline (5+ runs of history) |
| Burst direction | combat (rebuilders 70%+ combat) = war-prep; economy (0 combat rebuilds, 30% or less) = green; otherwise mixed/amber |
| Combat-intent rebuild | 2+ rebuilds with rebuilder combat 70%+, or 1 plus a +5pt 7-day shift |
| Ratio creep (yellow) | combat focus gained 20-40 points in 7d, or 30+ in 1d |
| Ratio creep (orange) | combat focus gained 40-60 points in 7d, or equivalent 1d |
| Ratio creep (red) | combat focus gained 60+ points in 7d, or equivalent 1d → dedicated alert |
| Ratio collapse (green) | mirror of creep on the falling side, 50+ points → dedicated alert |
| Eco-intent rebuild | 2+ rebuilds with rebuilder combat 30% or less, or 1 plus a -5pt 7-day shift |

Combat bursts at 1.5× their threshold OR 20+ absolute rebuilds trigger a dedicated "War Preparation Detected" alert. Red-tier ratio creep triggers a dedicated "Major Combat Shift" alert. 50+ point drops trigger "Standing Down" alerts.

All dedicated alerts have a per-country 3-day cooldown that's bypassed if severity escalates by 50%+.

### Outlier-filtered baselines

A country's own past mobilisations are excluded from its rolling baseline (any check where `new_resets` hits the absolute floor of 8 doesn't count toward the mean). Without this, a country that mobilised once becomes harder to detect next time. Falls back to the unfiltered series if filtering would leave too little baseline data.

### History length

The rolling history keeps `HISTORY_LEN = 56` entries per country. At the 3-hour cron cadence, that's ~7 days of context. To restore the previous ~14 days, set `HISTORY_LEN = 112`. If you change the cron cadence again, scale it accordingly (`days_of_context × runs_per_day`). The 7-day comparisons used by creep, collapse, and the posture movers need at least a week of history to fire, so don't drop `HISTORY_LEN` below one week's worth of runs.

## Setup

1. **Repo files.** `war_alert.py`, `requirements.txt` (just `requests`), `.github/workflows/war_alert.yml`, and this README.
2. **Discord webhook.** New channel for war alerts. Channel settings → Integrations → Webhooks → copy URL.
3. **GitHub secret.** Settings → Secrets and variables → Actions. Name `WAR_DISCORD_WEBHOOK_URL`, value = webhook URL.
4. **First run.** Actions → "War preparation alert" → Run workflow. Takes 3-5 minutes, then commits `war_state.json`.
5. **First day is calmer than later.** With the always-on absolute floor, you'll see real mobilisations from run 1, but the σ-based baseline detector still needs ~5 runs (~15 hours at 3h cadence) before it engages. False positives drop further after that.

## Tuning

Constants at the top of `war_alert.py`. The ones you'll most likely touch:

| Constant | Default | What it does |
|---|---|---|
| `IRELAND_REGION_IDS` | 4 IDs | Ireland's fixed home regions, the anchor for border detection. Update only if the game remaps Ireland |
| `BORDER_HOPS` | 3 | How far out from Ireland's territory to watch. 1 = direct borders only |
| `SAMPLE_TOP_N` | 50 | How many top players per country to sample |
| `NO_BASELINE_RESET_FLOOR` | 8 | Always-on absolute floor for burst alerts |
| `RESET_FLOOR` | 6 | σ-path floor once baseline exists |
| `HIGH_SEVERITY_FLOOR` | 20 | Absolute rebuilds for a dedicated burst alert |
| `RATIO_CREEP_MIN` | 20.0 | Minimum 7-day combat-focus gain for yellow creep |
| `RATIO_CREEP_RED` | 60.0 | 7-day gain that promotes to red + dedicated alert |
| `RATIO_DROP_MIN` | 20.0 | Minimum 7-day combat-focus drop for green standdown |
| `HIGH_DEMOB_FOR_ALERT` | 50.0 | Drop that triggers dedicated standdown alert |
| `COMBAT_INTENT` | 70.0 | Rebuilder combat % counted as "rebuilt for combat" |
| `DEMOB_RESET_INTENT` | 30.0 | Rebuilder combat % counted as "rebuilt for economy" |
| `STAND_DOWN_RATIO_CEILING` | 50.0 | Combat focus above this counts as still in posture, not stood down |
| `STAND_DOWN_DROP_MIN` | 15.0 | Drop from flagged peak needed to call a country stood down |
| `POSTURE_WAR` / `POSTURE_LEAN` | 70 / 50 | War-footing and combat-leaning thresholds in the posture overview |
| `POSTURE_MOVER_MIN` | 5.0 | Minimum 7-day shift to list a country as a mover |
| `MIN_LEVEL` | 20 | Minimum player level to include in sampling |
| `URGENT_COOLDOWN_DAYS` | 3 | Per-country cooldown between repeat urgent alerts |
| `HISTORY_LEN` | 56 | Rolling history entries per country (~7 days at 3h cadence) |

If the larger 50-player sample plus the higher reset floors make alerts feel too quiet, dialling `RESET_FLOOR` and `NO_BASELINE_RESET_FLOOR` down a couple of points is the main lever.

## State

`war_state.json` lives in the repo and is auto-committed every run. Holds per-country aggregate snapshots with rolling history, cached player IDs for fast next-run sampling, cooldown timestamps for each alert type, the peak combat ratio recorded while a country was flagged (for the stand-down vs holding decision), and the date of the last posture overview. Stays well under 1MB.

The state file is migrated automatically on first run after a schema change. Current version is v6, which added flagged-peak tracking. Migrations are idempotent.

Delete `war_state.json` to reset baselines. The first ~5 runs after that will rely on the absolute floor only. Note that when a country re-enters the watchlist after being absent (for example after an occupation ends, or after the watchlist was broken), its first run re-counts every rebuild since it was last sampled as "new," which can briefly look noisy until the next run refreshes its baseline.

## Schedule

Runs every 3 hours via GitHub Actions cron (`0 */3 * * *`), at 00:00, 03:00, … 21:00 UTC. At this cadence the worst-case lag between an event and an alert is ~3 hours and the average is ~1.5 hours. The per-country cooldowns prevent spam regardless of cadence. The posture overview is gated to the first run at or after 21:00 so it lands once a day.

The workflow has a concurrency group, so two runs can't race on the state file.

## Known gaps

**State-owned ammo production** isn't detected. Countries that stockpile through their own companies without player retraining will fly under the radar. Would need a separate watcher on `transaction.getPaginatedTransactions`.

**Impulsive wars without a prep window** aren't caught. This bot detects mobilisation, not declarations from peacetime. To catch attacks-in-progress, watch battle creation events directly.

**Pre-mobilisation diplomacy** isn't observable. By the time the first player pays gold to rebuild, the political decision was made days or weeks earlier. The earliest signal in the game data is the first rebuild wave.

**Tiny countries** with fewer than 10 active level-20+ players still show as "skipped" in logs. They can't mobilise meaningfully, so ignoring them is fine.

## Credits

Data via the [warera-proxy](https://warera-proxy.toie.workers.dev/) gateway and [warerastats.io](https://warerastats.io/). Made by toie.