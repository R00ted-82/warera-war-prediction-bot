---
name: skill-ratio-vs-weekly-damage
description: Why the skill-combat-focus metric mislabels real military powers, and the accurate signal that replaces it
metadata:
  type: project
---

In warera-war-prediction-bot, the original "combat focus" metric (median combat-vs-economy skill-point share among the top-50-by-level active players) is **anti-correlated with actual military activity** for the heavyweight countries. Measured 2026-06-15 from live data: Netherlands read 0% combat focus while ranked **#5 in the game for weekly damage** (#2 all-time, at war with 11); Germany 27% while #9 weekly / **#1 all-time**; Belgium 8% while #4 all-time. Meanwhile France/Lithuania/Spain read 81-88% "war-ready" but ranked only #16-#35 weekly. Reason: the real powers' elite players invest skills into companies/production to *fund* the war machine and fight through gear/rank, so skill focus is a leading indicator that lags (and inverts vs) actual fighting.

The accurate, cheap signal lives in `country.getCountryById` → `rankings.weeklyCountryDamages` (rolling 7-day combat damage, with a game-wide rank), plus `warsWith`, `nonAggressionUntil`, `countryActivePopulation`. One call per watchlist country (~13-20), no per-player fan-out — far cheaper than the skill-sampling pipeline.

**How to apply:** treat weekly-damage rank + war-list diffs as the primary risk/de-escalation signal; keep skill rebuilds only as an early-warning supplement. Never present skill-combat % as a country's military strength without the damage rank beside it (the daily report shows build as a plain word — combat/mixed/economy — next to the rank, never a bald "0%"). Pulling the whole population (~6,634 citizens across the watchlist) would make accuracy *worse* (population median collapses toward 0%) and ~7x the API load — don't.

Notifications are deliberately terse (bunker-bot style): the per-run **Update** is one compact embed, one line per country (`icon · country · what happened`), escalating first then de-escalating. The daily **posture report** is one unified roster (each watched country once, sorted by weekly-damage rank), gated to fire at **8pm Ireland local time** (gate on Europe/Dublin local hour ≥ 20; workflow runs at both 19:00 and 20:00 UTC to cover IST/GMT).

**Damage metric gotcha (important):** the game's `weeklyCountryDamages` is a weekly ACCUMULATOR (grows from ~0 each week to hundreds of millions, then resets) — NOT a rolling window. Its raw value tells you how far into the week it is, not whether a country is fighting now. So "is it attacking" is detected from the **rate** = increase in cumulative `countryDamages` (never resets) over a ~24h window, as damage/day. Calibrated live (Jun 2026): heavy fighters 20-340 M/day, quiet <8 M/day → DAMAGE_ACTIVE_RATE=20M, DAMAGE_QUIET_RATE=8M. Revisit if game numbers scale.

Signals: started_attacking / sustained_offensive / went_quiet (two-phase rate machine, hysteresis, SUSTAINED_DAYS=4); arming_up/easing_off (build shift) and rebuild_war/rebuild_eco — all SUPPRESSED for countries at war with Ireland (expected noise during war); war_declared/war_ended (warsWith diff); **at_war** = once-a-day reminder for ongoing wars with Ireland (war/attack transitions predate tracking, so a standing reminder is the only way an inherited war stays visible). Old paragraph alerts removed. State schema v11. See [[warera-bot-overview]].
