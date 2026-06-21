#!/usr/bin/env python3
"""
XRP Maker Trader — Live Display Server

Shows real-time performance in a browser. Auto-refreshes every 3 seconds.
Reads from:
  - polymarket/xrp_maker_live.json   (session stats written by trader)
  - polymarket/xrp_maker_trades.csv  (full trade ledger)
  - polymarket/xrp_maker_trader.log  (last N log lines)

Usage:
  python polymarket_xrp_maker_log_server.py [--port 8093]
  Then open http://localhost:8093
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

APP_DIR    = Path(__file__).resolve().parent
LIVE_JSON  = APP_DIR / "xrp_maker_live.json"
TRADES_CSV = APP_DIR / "xrp_maker_trades.csv"
LOG_PATH   = APP_DIR / "xrp_maker_trader.log"

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_session() -> dict:
    if LIVE_JSON.exists():
        try:
            return json.loads(LIVE_JSON.read_text())
        except Exception:
            pass
    return {}


def load_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    with open(TRADES_CSV) as f:
        return list(csv.DictReader(f))


def load_log_tail(n: int = 60) -> list[str]:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text().splitlines()
    return lines[-n:]


def compute_stats(trades: list[dict]) -> dict:
    entries   = [t for t in trades if t.get("event") == "order_placed"]
    settled   = [t for t in trades if t.get("event") == "settled" and t.get("filled_shares")]
    pnl_vals  = [float(t["net_pnl"]) for t in settled if t.get("net_pnl")]

    filled_settled = [t for t in settled if float(t.get("filled_shares") or 0) > 0]
    per_contract_pnls = []
    for t in filled_settled:
        try:
            pnl   = float(t["net_pnl"]) if t.get("net_pnl") else None
            fills = float(t.get("filled_shares") or 1)
            if pnl is not None and fills > 0:
                per_contract_pnls.append(pnl / fills)
        except Exception:
            pass

    cumulative_pnl = pnl_vals[-1] if pnl_vals else 0.0

    return {
        "n_entries": len(entries),
        "n_settled": len(settled),
        "n_filled": len(filled_settled),
        "fill_rate": len(filled_settled) / max(len(settled), 1),
        "cumulative_pnl": cumulative_pnl,
        "mean_pnl_per_contract": (sum(per_contract_pnls) / len(per_contract_pnls))
                                  if per_contract_pnls else None,
        "per_contract_history": per_contract_pnls,
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def pnl_color(val: float | None) -> str:
    if val is None:
        return "#aaa"
    return "#4caf50" if val > 0 else "#f44336" if val < 0 else "#aaa"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v*100:+.1f}%"


def fmt_dollar(v: float | None, decimals: int = 4) -> str:
    if v is None:
        return "—"
    return f"{'+'if v>0 else ''}{v:.{decimals}f}"


def build_html(session: dict, trades: list[dict], log_lines: list[str]) -> str:
    stats = compute_stats(trades)
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mini sparkline for per-contract PnL history
    hist = stats["per_contract_history"]
    sparkline = ""
    if hist:
        mn, mx = min(hist), max(hist)
        rng = max(mx - mn, 0.01)
        pts = " ".join(
            f"{int(10 + i * 180 / max(len(hist)-1,1))},{int(60 - (v - mn)/rng * 55)}"
            for i, v in enumerate(hist)
        )
        sparkline = f'<svg width="200" height="65" style="display:block;margin:8px auto">' \
                    f'<polyline points="{pts}" fill="none" stroke="#4caf50" stroke-width="2"/>' \
                    f'<line x1="10" y1="{int(60-(0-mn)/rng*55)}" x2="190" y2="{int(60-(0-mn)/rng*55)}" ' \
                    f'stroke="#555" stroke-dasharray="3"/></svg>'

    open_pos = session.get("open_position")
    open_html = ""
    if open_pos:
        open_html = f"""
        <div class="card" style="border:1px solid #f0a000">
          <div class="label">Open Position</div>
          <div style="font-size:1.3em;font-weight:bold;color:#f0a000">{open_pos.get('side','?')} @ {open_pos.get('limit_price','?')}</div>
          <div style="font-size:0.85em;color:#aaa">{open_pos.get('slug','')} · {open_pos.get('size','')} shares</div>
          <div style="font-size:0.75em;color:#777">order {str(open_pos.get('order_id',''))[:20]}…</div>
        </div>"""

    # Recent trades table (last 20 settled)
    settled = [t for t in trades if t.get("event") == "settled"][-20:]
    rows = ""
    for t in reversed(settled):
        outcome = t.get("official_outcome")
        outcome_str = "Up ↑" if outcome == "1" else "Down ↓" if outcome == "0" else "—"
        filled = float(t.get("filled_shares") or 0)
        pnl_val = float(t.get("net_pnl") or 0)
        pnl_str = fmt_dollar(pnl_val, 4) if filled > 0 else "unfilled"
        col = "#4caf50" if pnl_val > 0 else "#f44336" if pnl_val < 0 else "#aaa"
        rows += f"""<tr>
          <td style="color:#aaa">{t.get('timestamp_utc','')[:19]}</td>
          <td>{t.get('condition_id','')[-12:] if t.get('condition_id') else '—'}</td>
          <td>{t.get('order_status','—')}</td>
          <td>{filled:.1f}</td>
          <td>{outcome_str}</td>
          <td style="color:{col};font-weight:bold">{pnl_str}</td>
        </tr>"""

    # Log tail
    log_html = "\n".join(
        f'<div class="logline">{l}</div>'
        for l in reversed(log_lines[-30:])
    )

    mean_pnl = stats["mean_pnl_per_contract"]
    cum_pnl  = stats["cumulative_pnl"]

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="3">
  <title>XRP Maker Trader</title>
  <style>
    body {{ background:#121212; color:#e0e0e0; font-family:monospace; margin:0; padding:16px; }}
    h1 {{ color:#fff; margin:0 0 4px; font-size:1.4em; }}
    .subtitle {{ color:#888; font-size:0.85em; margin-bottom:16px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:16px; }}
    .card {{ background:#1e1e1e; border-radius:8px; padding:14px; }}
    .label {{ font-size:0.75em; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:4px; }}
    .big {{ font-size:2em; font-weight:bold; }}
    table {{ width:100%; border-collapse:collapse; font-size:0.82em; }}
    th {{ color:#888; text-align:left; padding:4px 8px; border-bottom:1px solid #333; }}
    td {{ padding:4px 8px; border-bottom:1px solid #2a2a2a; }}
    .logbox {{ background:#111; border-radius:6px; padding:10px; max-height:280px; overflow-y:auto; font-size:0.75em; }}
    .logline {{ color:#7cb; padding:1px 0; }}
    .section {{ margin-bottom:16px; }}
    h2 {{ color:#aaa; font-size:0.9em; text-transform:uppercase; letter-spacing:1px; margin:0 0 8px; }}
  </style>
</head>
<body>
  <h1>XRP Maker Trader</h1>
  <div class="subtitle">Updated: {now} · refreshes every 3s · <a href="/" style="color:#555">refresh</a></div>

  <div class="grid">
    <div class="card">
      <div class="label">Cumulative PnL</div>
      <div class="big" style="color:{pnl_color(cum_pnl)}">{fmt_dollar(cum_pnl, 4)}</div>
      <div style="font-size:0.75em;color:#666">USDC</div>
    </div>
    <div class="card">
      <div class="label">PnL / Contract</div>
      <div class="big" style="color:{pnl_color(mean_pnl)}">{fmt_dollar(mean_pnl, 4) if mean_pnl is not None else '—'}</div>
      <div style="font-size:0.75em;color:#666">avg over {stats['n_filled']} fills</div>
    </div>
    <div class="card">
      <div class="label">Fill Rate</div>
      <div class="big">{fmt_pct(stats['fill_rate'])}</div>
      <div style="font-size:0.75em;color:#666">{stats['n_filled']} / {stats['n_settled']} settled</div>
    </div>
    <div class="card">
      <div class="label">Trades Placed</div>
      <div class="big">{stats['n_entries']}</div>
      <div style="font-size:0.75em;color:#666">{stats['n_settled']} settled · {session.get('trades_skipped',0)} skipped</div>
    </div>
    <div class="card">
      <div class="label">Uptime</div>
      <div class="big">{int(session.get('uptime_s',0))//3600}h {(int(session.get('uptime_s',0))%3600)//60}m</div>
      <div style="font-size:0.75em;color:#666">{session.get('start_time_utc','—')[:16]}</div>
    </div>
  </div>

  {open_html}

  {'<div class="section"><h2>PnL History</h2>' + sparkline + '</div>' if sparkline else ''}

  <div class="section">
    <h2>Recent Settled Trades</h2>
    <table>
      <tr><th>Time</th><th>Contract</th><th>Status</th><th>Filled</th><th>Outcome</th><th>PnL (USDC)</th></tr>
      {rows if rows else '<tr><td colspan="6" style="color:#555;padding:12px">No settled trades yet</td></tr>'}
    </table>
  </div>

  <div class="section">
    <h2>Log (latest first)</h2>
    <div class="logbox">{log_html if log_html else '<div class="logline" style="color:#555">No log yet</div>'}</div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress access log

    def do_GET(self):
        session    = load_session()
        trades     = load_trades()
        log_lines  = load_log_tail(60)
        html       = build_html(session, trades, log_lines)
        body       = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8093)
    args = parser.parse_args()
    addr = ("0.0.0.0", args.port)
    server = HTTPServer(addr, Handler)
    print(f"XRP Maker display server: http://localhost:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
