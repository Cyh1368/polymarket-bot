#!/usr/bin/env python3
"""
pmcluster.registry — the multiple-comparisons defenses.

* trial_registry.jsonl : one append-only line per evaluated config (incl. failures).
  Every model/horizon/feature/threshold combination we *look at* must be logged;
  the count feeds the Phase-2 deflation. An untracked evaluation is a silent bias.
* lockbox : a deterministic, content-hashed split of held-out trading days that no
  analysis phase may read until the Final Gate.
* env_manifest : package versions, git commit, seeds — for reproducibility.

Concurrency: registry append uses an fcntl advisory lock so parallel Slurm jobs can
append safely to the shared file.
"""
from __future__ import annotations
import datetime
import fcntl
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

from . import config as C


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def trial_hash(definition: dict) -> str:
    """Stable hash of a trial definition (order-independent)."""
    blob = json.dumps(definition, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def log_trial(phase: str, definition: dict, metric: dict,
              registry_path: Path = None, status: str = "evaluated") -> str:
    """Append one trial line. `definition` identifies the config; `metric` is what we saw."""
    registry_path = registry_path or C.REGISTRY
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    h = trial_hash(definition)
    rec = {
        "ts": _utcnow(), "phase": phase, "hash": h, "status": status,
        "definition": definition, "metric": metric,
        "slurm_job": os.environ.get("SLURM_JOB_ID"),
        "slurm_task": os.environ.get("SLURM_ARRAY_TASK_ID"),
    }
    line = json.dumps(rec, default=str)
    with open(registry_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return h


def count_trials(registry_path: Path = None, phases=None) -> int:
    registry_path = registry_path or C.REGISTRY
    if not registry_path.exists():
        return 0
    n = 0
    with open(registry_path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            if phases is not None:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("phase") not in phases:
                    continue
            n += 1
    return n


# ── lockbox ───────────────────────────────────────────────────────────────────
def build_lockbox(all_days_by_coin: dict, frac: float = 0.25, min_days: int = 5,
                  seed: int = 20260621, lockbox_path: Path = None) -> dict:
    """Reserve the most recent `frac` of each coin's trading days as the lockbox.

    The most-recent days are chosen (not random) so the lockbox is a true future
    holdout — the hardest test of generalization. The split is content-hashed.
    """
    lockbox_path = lockbox_path or C.LOCKBOX
    split = {"seed": seed, "frac": frac, "min_days": min_days,
             "created": _utcnow(), "method": "most_recent_K_days", "coins": {}}
    for coin, days in all_days_by_coin.items():
        days = sorted(str(d) for d in days)
        k = max(min_days, int(round(frac * len(days))))
        k = min(k, len(days))
        lockbox = days[-k:] if k > 0 else []
        train = days[:-k] if k > 0 else days
        split["coins"][coin] = {
            "n_total": len(days), "n_lockbox": len(lockbox), "n_train": len(train),
            "train_days": train, "lockbox_days": lockbox,
            "lockbox_start": lockbox[0] if lockbox else None,
        }
    split["content_hash"] = trial_hash(
        {c: v["lockbox_days"] for c, v in split["coins"].items()})
    lockbox_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lockbox_path, "w") as f:
        json.dump(split, f, indent=2)
    return split


def load_lockbox(lockbox_path: Path = None) -> dict:
    lockbox_path = lockbox_path or C.LOCKBOX
    with open(lockbox_path) as f:
        return json.load(f)


def is_lockbox_day(coin: str, day, split: dict) -> bool:
    return str(day) in set(split["coins"].get(coin, {}).get("lockbox_days", []))


def write_env_manifest(path: Path = None) -> dict:
    path = path or (C.RESULTS / "env_manifest.json")
    import numpy, pandas, scipy, lightgbm
    try:
        import statsmodels
        sm_v = statsmodels.__version__
    except Exception:
        sm_v = None
    try:
        git = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                      cwd=str(Path(__file__).resolve().parent),
                                      stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        git = None
    man = {
        "created": _utcnow(), "host": platform.node(), "python": platform.python_version(),
        "numpy": numpy.__version__, "pandas": pandas.__version__, "scipy": scipy.__version__,
        "lightgbm": lightgbm.__version__, "statsmodels": sm_v, "git_commit": git,
        "horizons": C.HORIZONS, "coins": C.COINS, "model_cfg": C.MODEL_CFG,
        "gate": {k: getattr(C.Gate, k) for k in vars(C.Gate) if not k.startswith("_")},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(man, f, indent=2, default=str)
    return man
