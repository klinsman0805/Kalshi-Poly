#!/usr/bin/env python3
"""
scripts/label_candidates.py — attach settled outcomes to recorded candidates.

Reads candidate_data/candidates-*.jsonl, resolves each distinct market against
the venue's own settlement source, and writes candidate_data/labels.jsonl.

Idempotent and resumable by design. It is meant to run on a cron, repeatedly,
over a growing capture: markets that have not settled yet simply stay
unlabelled and are retried on the next pass. Nothing is ever labelled by
guessing, because a wrong label is undetectable downstream.

One resolution per (venue, station-or-slug, date, bucket) is shared by every
snapshot of that market, which is what keeps a batch over thousands of rows to
a few dozen API calls.

Run:  python scripts/label_candidates.py [--limit N] [--verbose]
"""

import argparse
import glob
import gzip
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(override=False)

from modules.settlement_truth import SettlementTruth             # noqa: E402

CANDIDATE_GLOB = os.getenv("WEATHER_CANDIDATE_GLOB",
                           "candidate_data/candidates-*.jsonl*")
LABEL_PATH = Path(os.getenv("WEATHER_LABEL_LOG", "candidate_data/labels.jsonl"))


def _market_id(rec):
    """What uniquely identifies a settleable market, across snapshots."""
    return "|".join(str(rec.get(k)) for k in
                    ("venue", "station", "slug", "date", "kind", "lo", "hi"))


def load_candidates(pattern=CANDIDATE_GLOB):
    for path in sorted(glob.glob(pattern)):
        op = gzip.open if path.endswith(".gz") else open
        with op(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def load_existing(path=LABEL_PATH):
    """Already-resolved markets, so a rerun costs nothing for them."""
    done = {}
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("won") is not None:
                done[r["market_id"]] = r
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after resolving this many new markets")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # Collapse snapshots to one row per market. Any snapshot carries the
    # identity fields, so the first is as good as the last.
    markets = {}
    n_snapshots = 0
    for rec in load_candidates():
        n_snapshots += 1
        markets.setdefault(_market_id(rec), rec)

    done = load_existing()
    pending = [(mid, rec) for mid, rec in markets.items() if mid not in done]
    print(f"snapshots {n_snapshots}  distinct markets {len(markets)}  "
          f"already labelled {len(done)}  to resolve {len(pending)}")

    truth = SettlementTruth()
    stats = Counter()
    LABEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    resolved = 0

    with LABEL_PATH.open("a", encoding="utf-8") as out:
        for mid, rec in pending:
            res = truth.label(rec)
            stats[res["reason"]] += 1
            if res["won"] is None:
                if args.verbose:
                    print(f"  pending  {rec.get('key')} — {res['reason']}")
                continue
            out.write(json.dumps({
                "market_id": mid,
                "ts": datetime.now(timezone.utc).isoformat(),
                "venue": rec.get("venue"), "key": rec.get("key"),
                "date": rec.get("date"), "kind": rec.get("kind"),
                "label": rec.get("label"), "lo": rec.get("lo"), "hi": rec.get("hi"),
                "won": res["won"], "actual_extreme": res["actual_extreme"],
                "source": res["source"],
            }) + "\n")
            resolved += 1
            stats["labelled"] += 1
            if args.verbose:
                print(f"  {'WIN ' if res['won'] else 'LOSS'} {rec.get('key')} "
                      f"{rec.get('label')} actual={res['actual_extreme']}")
            if args.limit and resolved >= args.limit:
                break
        out.flush()

    print(f"\nnewly labelled: {resolved}")
    for k, v in stats.most_common():
        print(f"  {k}: {v}")
    print(f"api: {dict(truth.stats)}")
    print(f"labels -> {LABEL_PATH}")


if __name__ == "__main__":
    main()
