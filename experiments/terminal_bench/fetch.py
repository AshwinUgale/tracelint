#!/usr/bin/env python3
"""Download Terminal-Bench 2.0 leaderboard trials (public Hugging Face dataset) for the runner.

The leaderboard dataset ``harborframework/terminal-bench-2-leaderboard`` (Apache-2.0) holds every
submission's Harbor job: ``submissions/terminal-bench/2.0/<Agent__Model>/<job>/<trial>/`` with the
agent's ATIF trajectory (``agent/trajectory.json``) and the grader's ``result.json``. Only those two
files per trial are fetched, into ``<out>/<submission>/<job>/<trial>/`` (the trajectory gzipped).
Nothing is committed. Re-runnable: trials already present are skipped.

    python experiments/terminal_bench/fetch.py --list                 # which submissions are ATIF
    python experiments/terminal_bench/fetch.py --submission <name> --out <data_dir>
    python experiments/terminal_bench/fetch.py --all-atif --out <data_dir>

Set ``HF_TOKEN`` to use your own Hugging Face token (higher rate limits); it is never required.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tracelint.adapters.atif import is_atif

REPO = "harborframework/terminal-bench-2-leaderboard"
ROOT = "submissions/terminal-bench/2.0"
API = f"https://huggingface.co/api/datasets/{REPO}/tree/main/"
RAW = f"https://huggingface.co/datasets/{REPO}/resolve/main/"


def _request(url: str, *, tries: int = 6) -> tuple[bytes | None, str]:
    """``(body, Link header)`` with retry/backoff on rate limits and transient errors; the body is
    ``None`` on a 404."""
    headers = {"User-Agent": "tracelint-experiment"}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read(), resp.headers.get("Link") or ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, ""
            retry_after = e.headers.get("Retry-After") if e.code == 429 else None
            time.sleep(min(float(retry_after or 2**attempt), 120))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(2**attempt)
    raise RuntimeError(f"failed after {tries} attempts: {url}")


def list_dirs(path: str) -> list[str]:
    """Sub-directory paths directly under ``path`` (follows the API's cursor pagination)."""
    out: list[str] = []
    url = API + urllib.parse.quote(path)
    while url:
        body, link = _request(url)
        out += [e["path"] for e in json.loads(body or b"[]") if e.get("type") == "directory"]
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = nxt.group(1) if nxt else ""
    return out


def submissions() -> list[str]:
    return sorted(p.rsplit("/", 1)[-1] for p in list_dirs(ROOT))


def trials(submission: str) -> list[str]:
    return [t for job in list_dirs(f"{ROOT}/{submission}") for t in list_dirs(job)]


def is_atif_submission(submission: str) -> bool:
    """Probe one trial: does this submission publish ATIF trajectories?"""
    for job in list_dirs(f"{ROOT}/{submission}"):
        for trial in list_dirs(job)[:1]:
            raw, _ = _request(RAW + trial + "/agent/trajectory.json")
            try:
                return raw is not None and is_atif(json.loads(raw))
            except ValueError:
                return False
    return False


def _fetch_trial(trial: str, out: Path) -> str:
    dest = out / trial[len(ROOT) + 1 :]
    traj, result = dest / "trajectory.json.gz", dest / "result.json"
    if traj.exists() and result.exists():
        return "skip"
    dest.mkdir(parents=True, exist_ok=True)
    raw_result, _ = _request(RAW + trial + "/result.json")
    if raw_result is None:
        return "no-result"
    raw_traj, _ = _request(RAW + trial + "/agent/trajectory.json")
    if raw_traj is None:
        result.write_bytes(raw_result)
        return "no-trajectory"
    traj.write_bytes(gzip.compress(raw_traj))
    result.write_bytes(raw_result)  # last: its presence marks the trial complete
    return "ok"


def fetch(submission: str, out: Path, workers: int) -> dict[str, int]:
    paths = trials(submission)
    counts: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_trial, t, out) for t in paths]
        for fut in concurrent.futures.as_completed(futures):
            try:
                key = fut.result()
            except Exception:  # noqa: BLE001 - one flaky trial must not abort the download
                key = "error"
            counts[key] = counts.get(key, 0) + 1
    print(f"{submission}: {len(paths)} trials -> {counts}", flush=True)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="list submissions; mark the ATIF ones")
    ap.add_argument("--submission", action="append", default=[], help="repeatable")
    ap.add_argument("--all-atif", action="store_true", help="every submission publishing ATIF")
    ap.add_argument("--out", help="data directory (keep it outside the repo)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.list:
        for name in submissions():
            print(f"{'ATIF' if is_atif_submission(name) else '----'}  {name}", flush=True)
        return 0
    if not args.out:
        ap.error("--out is required to download")
    chosen = list(args.submission)
    if args.all_atif:
        chosen += [s for s in submissions() if s not in chosen and is_atif_submission(s)]
    if not chosen:
        ap.error("pass --submission, --all-atif, or --list")
    print(f"fetching {len(chosen)} submission(s): {', '.join(chosen)}", flush=True)
    out = Path(args.out)
    errors = sum(fetch(s, out, args.workers).get("error", 0) for s in chosen)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
