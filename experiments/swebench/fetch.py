#!/usr/bin/env python3
"""Download a SWE-bench Verified OpenHands submission's trajectories + ground truth (public S3).

Trajectory data is NOT committed; this fetches it for the runner into ``<out>/<submission>/``
(``trajs/<instance_id>.json`` + ``results.json``). Re-runnable (skips files already present).

    python experiments/swebench/fetch.py --submission <name> --out <data_dir>
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import urllib.parse
import urllib.request
from pathlib import Path

S3 = "https://swe-bench-submissions.s3.amazonaws.com/"
GROUND_TRUTH = (
    "https://raw.githubusercontent.com/SWE-bench/experiments/main/evaluation/verified/"
)


def list_keys(prefix: str) -> list[str]:
    keys: list[str] = []
    token = None
    while True:
        url = f"{S3}?list-type=2&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token)
        xml = urllib.request.urlopen(url, timeout=60).read().decode()
        keys += re.findall(r"<Key>([^<]+)</Key>", xml)
        match = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not match:
            return keys
        token = match.group(1)


def _fetch(url: str, dest: Path) -> str:
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"
    dest.write_bytes(urllib.request.urlopen(url, timeout=120).read())
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submission", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    out = Path(args.out) / args.submission
    (out / "trajs").mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_bytes(
        urllib.request.urlopen(
            f"{GROUND_TRUTH}{args.submission}/results/results.json", timeout=60
        ).read()
    )

    keys = [k for k in list_keys(f"verified/{args.submission}/trajs/") if k.endswith(".json")]
    tasks = [(S3 + k, out / "trajs" / k.rsplit("/", 1)[-1]) for k in keys]
    print(f"{args.submission}: {len(tasks)} trajectories")
    ok = skip = err = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in concurrent.futures.as_completed(pool.submit(_fetch, u, d) for u, d in tasks):
            try:
                result = fut.result()
                ok += result == "ok"
                skip += result == "skip"
            except Exception:  # noqa: BLE001 - a flaky fetch shouldn't abort the whole download
                err += 1
    print(f"  downloaded {ok}, skipped {skip}, errors {err} -> {out}")
    return 1 if err else 0


if __name__ == "__main__":
    raise SystemExit(main())
