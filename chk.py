import json
recs=[json.loads(l) for l in open("kalshi_weather_paper.jsonl") if l.strip()]
print("ALL records for NYC|2026-07-27|low:")
for r in recs:
    if r.get("key")=="NYC|2026-07-27|low":
        keep={k:r.get(k) for k in ("entry_c","shares","cost_usd","pnl_usd","opened","settled","exit") if r.get(k) is not None}
        print("  %-8s %-10s %s"%(r.get("type"), r.get("label",""), json.dumps(keep)))
