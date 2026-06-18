#!/usr/bin/env python3
"""
2026-06-17-research/dashboard_server.py

Multi-strategy DRY-TESTING backtest dashboard.  http://0.0.0.0:8099

>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
>>  DRY TESTING / RESEARCH ONLY. This server NEVER places orders, never connects  >>
>>  to any exchange, never touches live capital. It only re-runs offline           >>
>>  backtests over the locally-collected CSVs and renders the results.             >>
>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

Self-contained data collection (no separate collector script needed):
  * A background COLLECTOR thread runs the Polymarket 5m BTC collector in-process
    (reusing polymarket/polymarket_data_BTC_5m.py), writing per-contract CSVs to the same
    polymarket/data_BTC_5m/ directory the backtest reads from.
  * When a contract closes, the collector fetches its official settlement outcome from the
    Gamma API (reusing fetch_all_outcomes.py) and appends it to the official-outcomes CSV,
    then SIGNALS the backtest worker to recompute. So the dashboard "updates after every
    contract" rather than on a blind fixed timer.

Design for "keeps on running":
  * A background worker reloads the contracts from disk and re-runs every strategy whenever a
    contract completes (event-driven), with a fixed-interval fallback heartbeat (default 600s).
  * Each strategy is computed inside its own try/except — one failing strategy can't take
    down the others or the server.
  * The whole worker loop is wrapped so any exception is logged and the loop continues.
  * The collector thread is likewise wrapped + auto-restarting; a collection failure can never
    take down the HTTP server or the backtest worker.
  * The HTTP handler only ever reads the last-good cached results under a lock; a request
    can never trigger a recompute or crash the worker.
  * ThreadingHTTPServer.serve_forever() runs in the main thread; the page auto-refreshes.

Each tab = one Strategy (different model config / entry time / post-model filters), scored
by the same expanding-window CV + lottery-robust gate used across the study (backtest_lib).

Run (kept alive by pm2/nohup in practice):
    kalshi/.venv-cli-trader/bin/python 2026-06-17-research/dashboard_server.py
    kalshi/.venv-cli-trader/bin/python 2026-06-17-research/dashboard_server.py --port 8099 --interval 600 --seeds 5
    kalshi/.venv-cli-trader/bin/python 2026-06-17-research/dashboard_server.py --no-collect  # read disk only
"""
from __future__ import annotations
import argparse
import asyncio
import copy
import csv
import html
import json
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

import huber_common as hc
import backtest_lib as bl

# Make the sibling collector module and the repo-root outcome fetcher importable so the
# dashboard can collect data itself instead of relying on a separate script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "polymarket")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # collector deps (httpx/websockets) live in the project venv
    import polymarket_data_BTC_5m as collector
    import fetch_all_outcomes as outcomes_mod
    _COLLECTOR_IMPORT_ERR = None
except Exception as _exc:  # don't let an import problem take down the read-only dashboard
    collector = None
    outcomes_mod = None
    _COLLECTOR_IMPORT_ERR = f"{type(_exc).__name__}: {_exc}"

# Where recorded PnL lives on disk (survives restarts; accrues a row per pass per strategy).
RESULTS_DIR = Path(__file__).parent / "dashboard_pnl"
RESULTS_DIR.mkdir(exist_ok=True)
HISTORY_CSV = RESULTS_DIR / "pnl_history.csv"
HISTORY_COLS = ["updated", "run_count", "strategy", "t1", "trades", "n_days",
                "ev_avail", "win_capped", "trim10", "true_ev_ci_lo", "ci_lo_worst",
                "seeds_pass", "n_seeds", "gate", "total_pnl_R", "total_pnl_usd", "stake_usd"]

# ── shared state (written by worker, read by handler) ────────────────────────────
_LOCK = threading.Lock()
_STATE = {
    "results": [],          # list of per-strategy result dicts (or {"name","error"})
    "updated": None,        # ISO timestamp of last successful pass
    "n_contracts": 0,
    "run_count": 0,
    "last_error": None,
    "computing": True,
    "elapsed": None,
    "collector": "off",     # short collector status string for the header
}
_CFG = {"interval": 600, "seeds": 5, "boot": 2000, "stake": 1.0, "strategies": None,
        "collect": True}

# Set by the collector whenever a contract finishes (and its outcome is recorded); the
# backtest worker waits on it so the dashboard recomputes "after every contract".
_RECOMPUTE_EVENT = threading.Event()

# Slugs whose official outcome is already on disk (skip), and slugs collected but not yet
# resolved by the Gamma API (retry each contract until they settle). Guarded by _PENDING_LOCK.
_PENDING_LOCK = threading.Lock()
_RESOLVED: set[str] = set()
_PENDING: set[str] = set()
OUTCOMES_CSV = _REPO_ROOT / "polymarket" / f"polymarket_{hc.COIN.lower()}_5m_official_outcomes.csv"


def _log(msg: str):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _record_pnl(res: dict, updated: str, run_count: int):
    """Persist a strategy's PnL to disk: overwrite its OOS ledger CSV (with running cum_pnl)
    and append one summary row to the shared pnl_history.csv. Returns total PnL in $ at stake."""
    stake = _CFG["stake"]
    ledger = res.pop("ledger", None)            # keep it out of the cached in-memory state
    if ledger is not None:
        led_df = pd.DataFrame(ledger)
        if not led_df.empty:
            led_df["pnl_usd"] = (led_df["pnl"] * stake).round(4)
            led_df["cum_pnl_usd"] = (led_df["cum_pnl"] * stake).round(4)
        led_df.to_csv(RESULTS_DIR / f"{res['name']}_oos_ledger.csv", index=False)
    total_usd = round(res.get("total_pnl", 0.0) * stake, 4)
    res["total_pnl_usd"] = total_usd
    row = {
        "updated": updated, "run_count": run_count, "strategy": res["name"], "t1": res["t1"],
        "trades": res["trades"], "n_days": res["n_days"], "ev_avail": round(res["ev_avail"], 6),
        "win_capped": round(res["win_capped"], 6), "trim10": round(res["trim10"], 6),
        "true_ev_ci_lo": round(res["true_ev_ci_lo"], 6), "ci_lo_worst": round(res["ci_lo_worst"], 6),
        "seeds_pass": res["seeds_pass"], "n_seeds": res["n_seeds"],
        "gate": res["seeds_pass"] == res["n_seeds"],
        "total_pnl_R": round(res.get("total_pnl", 0.0), 4), "total_pnl_usd": total_usd,
        "stake_usd": stake,
    }
    new = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS)
        if new:
            w.writeheader()
        w.writerow(row)
    return total_usd


# ── self-contained data collection ────────────────────────────────────────────────
def _load_resolved_slugs() -> set[str]:
    """Slugs already recorded with a Up/Down outcome on disk (so we never re-fetch them)."""
    if not OUTCOMES_CSV.exists():
        return set()
    try:
        df = pd.read_csv(OUTCOMES_CSV, usecols=["market_slug", "winning_outcome"])
    except Exception:
        return set()
    ok = df["winning_outcome"].astype(str).isin(["Up", "Down"])
    return set(df.loc[ok, "market_slug"].astype(str))


def _seed_pending_from_data_dir(cap: int = 200) -> None:
    """Find the most-recent collected contracts whose outcome isn't on disk yet and queue
    them for resolution, so a freshly-started dashboard self-heals recent gaps (bounded)."""
    data_dir = _REPO_ROOT / "polymarket" / f"data_{hc.COIN}_5m"
    slugs = []
    for path in data_dir.glob(f"polymarket_data_{hc.COIN}_5m_*.csv"):
        slug = path.stem
        if "_5m_" in slug:
            slug = slug.split("_5m_", 1)[1]
        slugs.append(slug)
    # newest first by trailing unix timestamp
    slugs.sort(key=lambda s: int(s.rsplit("-", 1)[-1]) if s.rsplit("-", 1)[-1].isdigit() else 0,
               reverse=True)
    with _PENDING_LOCK:
        for slug in slugs:
            if len(_PENDING) >= cap:
                break
            if slug not in _RESOLVED:
                _PENDING.add(slug)


def _append_outcome_row(row: dict) -> None:
    new = not OUTCOMES_CSV.exists()
    with OUTCOMES_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=outcomes_mod.POLYMARKET_CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in outcomes_mod.POLYMARKET_CSV_FIELDS})


def _resolve_pending() -> int:
    """Try the Gamma API for every queued slug; append any that have settled to the outcomes
    CSV. Blocking (urllib) — call from an executor, not the event loop. Returns # newly resolved."""
    with _PENDING_LOCK:
        pend = sorted(_PENDING)
    resolved_now: list[str] = []
    for slug in pend:
        try:
            row = outcomes_mod._row_for_slug(slug)
        except Exception as e:
            _log(f"  outcome fetch error {slug}: {type(e).__name__}: {e}")
            continue
        if str(row.get("winning_outcome")) in ("Up", "Down"):
            try:
                _append_outcome_row(row)
            except Exception:
                _log(f"  outcome write error {slug}:\n" + traceback.format_exc())
                continue
            resolved_now.append(slug)
            _log(f"  outcome resolved: {slug} -> {row['winning_outcome']}")
    if resolved_now:
        with _PENDING_LOCK:
            for s in resolved_now:
                _PENDING.discard(s)
                _RESOLVED.add(s)
    return len(resolved_now)


def _on_contract_done(slug: str) -> None:
    """Runs (in an executor) after a contract closes: queue + resolve outcomes, then wake the
    backtest worker so the dashboard updates after every contract."""
    with _PENDING_LOCK:
        if slug not in _RESOLVED:
            _PENDING.add(slug)
        n_pending = len(_PENDING)
    n_new = _resolve_pending()
    with _LOCK:
        _STATE["collector"] = (f"last contract {slug} · "
                               f"+{n_new} outcomes · {len(_PENDING)} pending")
    _log(f"contract complete: {slug} (newly-resolved outcomes={n_new}, pending={n_pending})")
    _RECOMPUTE_EVENT.set()  # trigger a backtest recompute


def _collector_args(coin: str):
    cc = collector.COIN_CONFIGS[coin]
    return argparse.Namespace(
        coin=coin, coin_config=cc,
        data_dir=_REPO_ROOT / "polymarket" / cc.data_dir_name, slug=None,
        sample_seconds=5.0, market_refresh_seconds=2.0, after_close_seconds=10.0,
        http_timeout=10.0, ws_open_timeout=10.0,
        clob_ws_url=collector.CLOB_MARKET_WS_URL, rtds_ws_url=collector.RTDS_WS_URL,
        spot_topic=collector.DEFAULT_SPOT_TOPIC, spot_symbol=cc.spot_symbol,
        target_max_distance_seconds=300.0, initial_spot_wait_seconds=5.0,
        max_spread=0.20, max_book_age_seconds=15.0, max_book_skew_seconds=8.0,
        max_spot_age_seconds=15.0, max_complement_deviation=0.20, once=False,
    )


async def _collector_main(args) -> None:
    """One contract at a time: discover the live market, collect it to CSV until it closes,
    then hand the slug off for outcome resolution + a dashboard recompute."""
    stop = asyncio.Event()
    state = collector.CollectorState(args.spot_symbol, args.spot_topic)
    spot_task = asyncio.create_task(collector.rtds_ws_loop(state, stop, args))
    loop = asyncio.get_running_loop()
    import httpx
    try:
        async with httpx.AsyncClient(timeout=args.http_timeout) as client:
            last_slug = ""
            while not stop.is_set():
                try:
                    market = await collector.discover_current_market(client, args.coin_config, None)
                except Exception as e:
                    _log(f"collector: discover failed: {type(e).__name__}: {e}")
                    await asyncio.sleep(args.market_refresh_seconds)
                    continue
                if market.slug == last_slug:
                    await asyncio.sleep(args.market_refresh_seconds)
                    continue
                last_slug = market.slug
                with _LOCK:
                    _STATE["collector"] = f"collecting {market.slug}"
                _log(f"collector: tracking {market.slug}")
                await collector.run_market(state, client, market, stop, args)
                # contract closed → resolve outcome + recompute (off the event loop)
                await loop.run_in_executor(None, _on_contract_done, market.slug)
                await asyncio.sleep(args.market_refresh_seconds)
    finally:
        stop.set()
        spot_task.cancel()
        await asyncio.gather(spot_task, return_exceptions=True)


def collector_thread():
    """Run the in-process collector forever; restart on any failure. Never exits."""
    if collector is None or outcomes_mod is None:
        with _LOCK:
            _STATE["collector"] = f"disabled (import error: {_COLLECTOR_IMPORT_ERR})"
        _log(f"COLLECTOR DISABLED — import failed: {_COLLECTOR_IMPORT_ERR}")
        return
    with _PENDING_LOCK:
        _RESOLVED.update(_load_resolved_slugs())
    _seed_pending_from_data_dir()
    _log(f"collector: {len(_RESOLVED)} resolved outcomes on disk, "
         f"{len(_PENDING)} recent contracts queued for resolution")
    args = _collector_args(hc.COIN)
    while True:
        try:
            asyncio.run(_collector_main(args))
        except Exception:
            _log("COLLECTOR ERROR (restarting in 5s):\n" + traceback.format_exc())
            with _LOCK:
                _STATE["collector"] = "error — restarting"
        time.sleep(5)


def worker():
    """Reload data + recompute every strategy forever. Never exits on error."""
    strategies = bl.default_strategies()
    if _CFG["strategies"]:
        want = [n.strip() for n in _CFG["strategies"]]
        by_name = {s.name: s for s in strategies}
        missing = [n for n in want if n not in by_name]
        if missing:
            _log(f"WARNING: unknown strategy name(s) ignored: {missing}. "
                 f"available: {list(by_name)}")
        strategies = [by_name[n] for n in want if n in by_name] or strategies
    _log(f"strategies: {[s.name for s in strategies]}")
    while True:
        t0 = time.time()
        with _LOCK:
            _STATE["computing"] = True
        try:
            _log("reloading contracts…")
            contracts = hc.load_data()
            updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
            run_count = _STATE["run_count"] + 1
            results = []
            for s in strategies:
                try:
                    res = bl.run_strategy(contracts, s, n_seeds=_CFG["seeds"], boot=_CFG["boot"])
                except Exception as e:  # one strategy failing must not kill the rest
                    res = {"name": s.name, "error": f"{type(e).__name__}: {e}"}
                    _log(f"  !! {s.name}: {res['error']}")
                else:
                    if "error" in res:
                        _log(f"  !! {s.name}: {res['error']}")
                    else:
                        try:
                            total_usd = _record_pnl(res, updated, run_count)
                        except Exception:
                            _log(f"  !! {s.name}: PnL record failed\n" + traceback.format_exc())
                            total_usd = float("nan")
                        _log(f"  ok {s.name}: trades={res['trades']} "
                             f"EV/avail={res['ev_avail']:+.4f} PnL={res.get('total_pnl',0):+.2f}R "
                             f"(${total_usd:+.2f}) pass={res['seeds_pass']}/{res['n_seeds']}")
                results.append(res)
            with _LOCK:
                _STATE.update(results=results, n_contracts=len(contracts),
                              updated=updated, run_count=run_count, last_error=None,
                              elapsed=round(time.time() - t0, 1))
            _log(f"pass #{_STATE['run_count']} done in {time.time()-t0:.1f}s")
        except Exception:
            err = traceback.format_exc()
            _log("WORKER ERROR (continuing):\n" + err)
            with _LOCK:
                _STATE["last_error"] = err
        finally:
            with _LOCK:
                _STATE["computing"] = False
        # Wake on the next completed contract (event-driven), with the interval as a fallback
        # heartbeat so the dashboard still refreshes if no contract has closed yet.
        _RECOMPUTE_EVENT.wait(timeout=_CFG["interval"])
        _RECOMPUTE_EVENT.clear()


# ── rendering ────────────────────────────────────────────────────────────────────
def _sparkline(equity, w=760, h=180):
    if not equity:
        return '<div class="muted">no trades</div>'
    n = len(equity)
    lo, hi = min(equity + [0.0]), max(equity + [0.0])
    rng = (hi - lo) or 1.0
    def x(i): return 8 + i * (w - 16) / max(n - 1, 1)
    def y(v): return h - 8 - (v - lo) * (h - 16) / rng
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(equity))
    zero = y(0.0)
    final = equity[-1]
    color = "#2e9e5b" if final >= 0 else "#d9483b"
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'preserveAspectRatio="none" style="background:#0d1117;border-radius:6px">'
            f'<line x1="8" y1="{zero:.1f}" x2="{w-8}" y2="{zero:.1f}" '
            f'stroke="#30363d" stroke-dasharray="4 4"/>'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>'
            f'<text x="{w-8}" y="16" text-anchor="end" fill="{color}" '
            f'font-size="13" font-family="monospace">cum P&amp;L {final:+.2f}R</text>'
            f'</svg>')


def _badge(ok: bool, n_pass: int, n_seeds: int):
    if ok:
        return f'<span class="badge pass">GATE PASS · {n_pass}/{n_seeds} seeds</span>'
    return f'<span class="badge fail">no pass · {n_pass}/{n_seeds} seeds</span>'


def _metric(label, val, good=None, fmt="{:+.4f}"):
    cls = ""
    if good is not None and val == val:  # not NaN
        cls = "pos" if (val > 0) == good or (good and val > 0) else ""
        cls = "pos" if val > 0 else "neg"
    sval = fmt.format(val) if val == val else "—"
    return f'<div class="m"><span class="ml">{label}</span><span class="mv {cls}">{sval}</span></div>'


def _tab_panel(r: dict, idx: int) -> str:
    if "error" in r:
        return (f'<section class="panel" id="p{idx}"><h2>{html.escape(r["name"])}</h2>'
                f'<div class="err">error: {html.escape(str(r["error"]))}</div></section>')
    cfg = r["cfg"]; flt = r["filters"]
    band = f'[{flt["band"][0]},{flt["band"][1]})' if flt["band"] else "off"
    gate_ok = r["seeds_pass"] == r["n_seeds"]
    pnl_cls = "pos" if r.get("total_pnl", 0) >= 0 else "neg"
    flt_stake = _CFG["stake"]
    rows = "".join(
        f'<tr><td>{html.escape(str(t["close_time"])[:16])}</td>'
        f'<td class="{ "yes" if t["action"]=="YES" else "no" }">{t["action"]}</td>'
        f'<td>{t["p_yes_mid"]:.2f}</td><td>{t["up_ask"]:.2f}</td><td>{t["down_ask"]:.2f}</td>'
        f'<td class="{ "pos" if t["pnl"]>=0 else "neg" }">{t["pnl"]:+.3f}</td></tr>'
        for t in reversed(r["recent"])
    ) or '<tr><td colspan="6" class="muted">no trades</td></tr>'

    return f"""
    <section class="panel" id="p{idx}">
      <div class="phead">
        <h2>{html.escape(r['name'])}</h2>
        {_badge(gate_ok, r['seeds_pass'], r['n_seeds'])}
      </div>
      <p class="desc">{html.escape(r['desc'])}</p>
      <div class="cfgline">
        T1=<b>{r['t1']}s</b> · mc={cfg['min_child_samples']} · δ={cfg['huber_alpha']} ·
        l2={cfg['lambda_l2']} · lr={cfg['learning_rate']} · rounds={cfg['n_rounds']} ·
        depth={cfg['max_depth']} &nbsp;|&nbsp; filters: skip_bonus={flt['skip_bonus']},
        yes_gate≥{flt['yes_gate_lo']}, block≥{flt['block_above']}, band={band}
      </div>
      <div class="pnl {pnl_cls}">
        <span class="pnl-l">cumulative OOS PnL (seed 0)</span>
        <span class="pnl-v">{r.get('total_pnl', 0):+.2f} R&nbsp;&nbsp;·&nbsp;&nbsp;${r.get('total_pnl_usd', 0):+.2f}</span>
        <span class="pnl-s">over {r['trades']} trades · ${flt_stake:.2f}/trade stake</span>
      </div>
      <div class="metrics">
        {_metric("EV / avail", r['ev_avail'])}
        {_metric("EV / trig", r['ev_trig'])}
        {_metric("win-capped", r['win_capped'])}
        {_metric("trim10", r['trim10'])}
        {_metric("true-EV CI lo (mean)", r['true_ev_ci_lo'])}
        {_metric("CI lo (worst seed)", r['ci_lo_worst'])}
        <div class="m"><span class="ml">win rate</span><span class="mv">{r['win_rate']:.1%}</span></div>
        <div class="m"><span class="ml">trades / pool</span><span class="mv">{r['trades']} / {r['rows']}</span></div>
        <div class="m"><span class="ml">day-blocks</span><span class="mv">{r['n_days']}</span></div>
      </div>
      <h3>seed-0 OOS equity curve — cumulative PnL (R units)</h3>
      {_sparkline(r['equity'])}
      <p class="rec">recorded → <code>dashboard_pnl/{html.escape(r['name'])}_oos_ledger.csv</code>
         · history → <code>dashboard_pnl/pnl_history.csv</code></p>
      <h3>recent OOS trades (seed 0)</h3>
      <table class="trades"><thead><tr><th>close (UTC)</th><th>side</th><th>p_mid</th>
        <th>up_ask</th><th>down_ask</th><th>pnl (R)</th></tr></thead><tbody>{rows}</tbody></table>
    </section>"""


def render_page() -> str:
    with _LOCK:
        st = copy.deepcopy(_STATE)  # snapshot under lock; render outside lock

    results = st["results"]
    if not results:
        body = ('<div class="loading">⏳ first backtest pass is running — '
                'this can take a couple of minutes. The page auto-refreshes.</div>')
        tabs = ""
    else:
        tabs = "".join(
            f'<button class="tab{" active" if i==0 else ""}" onclick="show({i})">'
            f'{html.escape(r["name"])}'
            f'{" ⚠" if "error" in r else (" ✅" if r.get("seeds_pass")==r.get("n_seeds") else "")}'
            f'</button>'
            for i, r in enumerate(results)
        )
        body = "".join(_tab_panel(r, i) for i, r in enumerate(results))

    status = "computing…" if st["computing"] and not results else \
             (f'updated {st["updated"]} UTC · pass #{st["run_count"]} · '
              f'{st["n_contracts"]} contracts · {st["elapsed"]}s/pass' if st["updated"] else "starting…")
    status = f'{status}  ‖  collector: {st.get("collector", "off")}'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>Backtest Dashboard (DRY ONLY)</title>
<style>
  * {{ box-sizing:border-box }}
  body {{ font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; background:#010409; color:#c9d1d9 }}
  .dry {{ background:#7d1d1d; color:#ffd7d7; text-align:center; padding:6px; font-weight:600;
          letter-spacing:.5px; font-size:13px }}
  header {{ padding:14px 22px; border-bottom:1px solid #21262d; display:flex; justify-content:space-between; align-items:baseline }}
  h1 {{ font-size:18px; margin:0 }}
  .status {{ color:#8b949e; font-size:12px; font-family:monospace }}
  .tabs {{ display:flex; flex-wrap:wrap; gap:6px; padding:12px 22px; border-bottom:1px solid #21262d }}
  .tab {{ background:#161b22; color:#c9d1d9; border:1px solid #30363d; border-radius:6px;
          padding:7px 12px; cursor:pointer; font-size:13px }}
  .tab.active {{ background:#1f6feb; border-color:#1f6feb; color:#fff }}
  .panel {{ display:none; padding:20px 22px; max-width:900px }}
  .panel.show {{ display:block }}
  .phead {{ display:flex; align-items:center; gap:14px }}
  h2 {{ margin:0; font-size:20px }}
  .desc {{ color:#8b949e; font-size:13px; margin:6px 0 10px }}
  .cfgline {{ font-family:monospace; font-size:12px; color:#8b949e; background:#0d1117;
              padding:8px 10px; border-radius:6px; margin-bottom:14px }}
  .badge {{ padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600 }}
  .badge.pass {{ background:#16331f; color:#56d364; border:1px solid #2ea043 }}
  .badge.fail {{ background:#33211f; color:#f0883e; border:1px solid #9e5230 }}
  .pnl {{ background:#0d1117; border:1px solid #21262d; border-radius:8px; padding:12px 16px;
          margin:6px 0 14px; display:flex; flex-direction:column; gap:2px }}
  .pnl-l {{ color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:.5px }}
  .pnl-v {{ font-family:monospace; font-size:26px; font-weight:700 }}
  .pnl-s {{ color:#6e7681; font-size:11px }}
  .pnl.pos .pnl-v {{ color:#56d364 }} .pnl.neg .pnl-v {{ color:#f85149 }}
  .rec {{ color:#6e7681; font-size:11px; margin:6px 0 0 }}
  .rec code {{ color:#8b949e }}
  .metrics {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:16px }}
  .m {{ background:#0d1117; border:1px solid #21262d; border-radius:6px; padding:8px 10px;
        display:flex; justify-content:space-between; align-items:baseline }}
  .ml {{ color:#8b949e; font-size:12px }} .mv {{ font-family:monospace; font-size:15px }}
  .mv.pos {{ color:#56d364 }} .mv.neg {{ color:#f85149 }}
  h3 {{ font-size:13px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin:18px 0 8px }}
  table.trades {{ border-collapse:collapse; width:100%; font-size:12px; font-family:monospace }}
  table.trades th,td {{ text-align:right; padding:4px 8px; border-bottom:1px solid #21262d }}
  table.trades th {{ color:#8b949e; font-weight:500 }}
  td.yes {{ color:#58a6ff }} td.no {{ color:#d2a8ff }}
  .pos {{ color:#56d364 }} .neg {{ color:#f85149 }} .muted {{ color:#6e7681 }}
  .err {{ color:#f85149; font-family:monospace }}
  .loading {{ padding:60px; text-align:center; color:#8b949e; font-size:15px }}
</style></head><body>
<div class="dry">⚠ DRY TESTING / RESEARCH ONLY — this dashboard NEVER trades live capital ⚠</div>
<header><h1>Polymarket 5m · Multi-Strategy Backtest Dashboard</h1>
  <span class="status">{html.escape(status)}</span></header>
<div class="tabs">{tabs}</div>
{body}
<script>
  function show(i){{
    document.querySelectorAll('.panel').forEach((p,j)=>p.classList.toggle('show',j===i));
    document.querySelectorAll('.tab').forEach((t,j)=>t.classList.toggle('active',j===i));
  }}
  document.addEventListener('DOMContentLoaded',()=>{{const p=document.querySelector('.panel');if(p)p.classList.add('show');}});
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                page = render_page().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif self.path == "/health":
                with _LOCK:
                    payload = json.dumps({"run_count": _STATE["run_count"],
                                          "updated": _STATE["updated"],
                                          "n_strategies": len(_STATE["results"])}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404); self.end_headers()
        except BrokenPipeError:
            pass
        except Exception:
            _log("handler error:\n" + traceback.format_exc())
            try:
                self.send_response(500); self.end_headers()
            except Exception:
                pass

    def log_message(self, *args):  # silence default request logging
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--interval", type=int, default=600, help="seconds between backtest passes")
    ap.add_argument("--seeds", type=int, default=5, help="model seeds per strategy")
    ap.add_argument("--boot", type=int, default=2000, help="day-block bootstrap resamples")
    ap.add_argument("--stake", type=float, default=1.0,
                    help="$ staked per trade for dollar PnL (R units × stake); 1.0 = R==$")
    ap.add_argument("--strategies", default=None,
                    help="comma-separated strategy names to run (default: all). "
                         "e.g. prod_t180_mc150,t245_combined")
    ap.add_argument("--no-collect", action="store_true",
                    help="do NOT collect data in-process; only re-run backtests over CSVs "
                         "already on disk (rely on a separate collector script).")
    args = ap.parse_args()
    _CFG.update(interval=args.interval, seeds=args.seeds, boot=args.boot, stake=args.stake,
                strategies=args.strategies.split(",") if args.strategies else None,
                collect=not args.no_collect)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    if _CFG["collect"]:
        c = threading.Thread(target=collector_thread, daemon=True)
        c.start()
    else:
        with _LOCK:
            _STATE["collector"] = "off (--no-collect)"

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    _log(f"DRY-ONLY dashboard at http://{args.host}:{args.port}  "
         f"(interval={args.interval}s seeds={args.seeds} collect={_CFG['collect']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
