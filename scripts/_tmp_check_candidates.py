from feeds.poly_rewards import scan
from modules.poly_rewards_exec import PolyRewardsExec

results = scan(tag_slug="weather", min_rate=5.0, question_filter=lambda q: "temperature in" in q.lower())
ex = PolyRewardsExec()
candidates = ex.check(results)
for c in candidates:
    print("%-14s %-6s est=$%.2f/day  capital=$%.0f  two_sided=%s  age=%.0fmin" % (
        c["city"], c["kind"], c["est_daily_usd"], c["capital_usd"], c["two_sided_required"], c["age_min"]))
