#!/usr/bin/env python3
"""
pmcluster.features — load Polymarket 5m contract CSVs + official outcomes and
extract the model feature row at one or more entry horizons.

Ported verbatim (logic-for-logic) from 2026-06-17-research/huber_common.py so the
cluster results stay comparable with the local CFES work. The only change is that
a single pass over each contract emits feature rows for *every* horizon in the
grid (efficiency: we parse ~16k CSVs once, not once per horizon).
"""
from __future__ import annotations
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C


# ── small numeric helpers (identical to huber_common) ─────────────────────────
def fnum(v):
    try:
        out = float(v)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def series_stats(values, last=None):
    c = pd.to_numeric(values, errors="coerce").dropna()
    if c.empty:
        return {"z": 0.0, "vol": 0.0}
    lv = float(c.iloc[-1] if last is None else last)
    mean = float(c.mean())
    vol = float(c.std(ddof=0)) if len(c) > 1 else 0.0
    z = (lv - mean) / vol if vol > 1e-12 else 0.0
    return {"z": z, "vol": vol}


@dataclass(frozen=True)
class ContractData:
    slug: str
    close_time: pd.Timestamp
    label: int
    df: pd.DataFrame


def load_contracts(coin: str):
    """Return a time-sorted list of ContractData for a coin (official outcomes only)."""
    data_dir = C.coin_data_dir(coin)
    outcomes_path = C.coin_outcomes_path(coin)

    outcomes = {}
    for row in pd.read_csv(outcomes_path).to_dict(orient="records"):
        slug = str(row.get("market_slug") or "").strip()
        wo = str(row.get("winning_outcome") or "").strip()
        if slug and wo in {"Up", "Down"}:
            outcomes[slug] = wo

    required = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "seconds_to_close")
    contracts = []
    for path in sorted(data_dir.glob("*.csv")):
        slug = path.stem
        if "_5m_" in slug:
            slug = slug.split("_5m_", 1)[1]
        wo = outcomes.get(slug)
        if not wo:
            continue
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception:
            continue
        if df.empty or any(c not in df.columns for c in required):
            continue
        df = df.copy()
        df["_stc"] = pd.to_numeric(df["seconds_to_close"], errors="coerce")
        df["_ts"] = pd.to_datetime(df.get("timestamp_utc"), utc=True, errors="coerce")
        df = df[df["_stc"].notna()]
        if df.empty:
            continue
        m = re.search(r"(\d{10})$", slug)
        if not m:
            continue
        ct = pd.Timestamp(int(m.group(1)) + C.MARKET_SECONDS, unit="s", tz="UTC")
        contracts.append(ContractData(slug=slug, close_time=ct,
                                      label=(1 if wo == "Up" else 0), df=df))
    contracts.sort(key=lambda c: c.close_time)
    return contracts


def extract_at_horizon(cd: ContractData, t1: int):
    """Feature row for one contract at entry horizon t1 (seconds-to-close), or None."""
    df = cd.df
    t1c = df[(df["_stc"] - t1).abs() <= C.HORIZON_TOL]
    if t1c.empty:
        return None
    t1r = t1c.loc[(t1c["_stc"] - t1).abs().idxmin()]
    ya = fnum(t1r.get("up_best_ask")); yb = fnum(t1r.get("up_best_bid"))
    na = fnum(t1r.get("down_best_ask")); nb = fnum(t1r.get("down_best_bid"))
    ubs = fnum(t1r.get("up_best_bid_size")) or 0.0
    dbs = fnum(t1r.get("down_best_bid_size")) or 0.0
    if any(v is None for v in (ya, yb, na, nb)):
        return None
    if not (0 < ya < 1 and 0 < na < 1 and yb <= ya and nb <= na):
        return None
    up_mid = (yb + ya) / 2.0

    t1_ts = t1r.get("_ts")
    ts_ok = not pd.isna(t1_ts)
    if ts_ok:
        h60 = df[df["_ts"].notna() & (df["_ts"] <= t1_ts) &
                 ((t1_ts - df["_ts"]).dt.total_seconds() <= C.IND_WINDOW)]
        h20 = h60[(t1_ts - h60["_ts"]).dt.total_seconds() <= 20.0] if not h60.empty else h60
    else:
        h60 = h20 = pd.DataFrame()
    if h60.empty:
        h60 = t1r.to_frame().T
    if h20.empty:
        h20 = t1r.to_frame().T

    def _mids(h):
        return pd.to_numeric(h.get("up_mid", pd.Series(dtype=float)), errors="coerce")

    def _obis(h):
        u = pd.to_numeric(h.get("up_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0)
        d = pd.to_numeric(h.get("down_best_bid_size", pd.Series(dtype=float)), errors="coerce").fillna(0)
        return (u - d) / (u + d + 1e-9)

    obi_cur = (ubs - dbs) / (ubs + dbs + 1e-9)
    ym60 = series_stats(_mids(h60), up_mid)
    ym20 = series_stats(_mids(h20), up_mid)
    ob60 = series_stats(_obis(h60), obi_cur)
    mids_60 = _mids(h60).dropna()
    mid_change = up_mid - float(mids_60.iloc[0]) if not mids_60.empty else 0.0

    if ts_ok:
        secs = t1_ts.hour * 3600 + t1_ts.minute * 60 + t1_ts.second
        tod_sin = math.sin(2 * math.pi * secs / 86400)
        tod_cos = math.cos(2 * math.pi * secs / 86400)
        hour = int(t1_ts.hour)
    else:
        tod_sin = tod_cos = 0.0
        hour = -1

    rd = {
        "contract_id": cd.slug, "horizon": int(t1),
        "close_time": cd.close_time, "ts": t1_ts if ts_ok else cd.close_time,
        "date": cd.close_time.date(), "hour": hour,
        "y_settle": cd.label, "c_yes": ya + C.COST_ADD, "c_no": na + C.COST_ADD,
        "up_ask": ya, "down_ask": na, "up_bid": yb, "down_bid": nb,
        "up_bid_size": ubs, "down_bid_size": dbs,
        "p_yes_mid": up_mid,
        "yes_mid_z_60": ym60["z"], "yes_mid_vol_60": ym60["vol"],
        "yes_mid_z_20": ym20["z"], "yes_mid_vol_20": ym20["vol"],
        "mid_change_60": mid_change, "book_qty_log": math.log1p(ubs + dbs),
        "OBI": obi_cur, "OBI_vol_60": ob60["vol"], "OBI_z_60": ob60["z"],
        "spread_yes": ya - yb, "tod_sin": tod_sin, "tod_cos": tod_cos,
    }
    if any(not math.isfinite(float(rd.get(f, float("nan")))) for f in C.FEATURES):
        return None
    return rd


def build_coin_frame(coin: str, horizons=None) -> pd.DataFrame:
    """Tidy long frame: one row per (contract, horizon) with features + outcome."""
    horizons = horizons or C.HORIZONS
    contracts = load_contracts(coin)
    rows = []
    for cd in contracts:
        for t1 in horizons:
            r = extract_at_horizon(cd, t1)
            if r is not None:
                r["coin"] = coin
                rows.append(r)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["horizon", "ts"]).reset_index(drop=True)
        # normalize date to a plain python date for stable grouping/serialization
        df["date"] = pd.to_datetime(df["close_time"], utc=True).dt.date.astype("string")
    return df
