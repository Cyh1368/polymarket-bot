"""Local live dashboard for BTC 15-minute Up/Down order book snapshots."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_DB = Path("data/live_orderbooks/btc_updown_orderbooks.sqlite")


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BTC 15m Order Book</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1115;
      --panel: #171a21;
      --panel-2: #1f242d;
      --line: #303642;
      --text: #edf1f7;
      --muted: #9aa4b2;
      --up: #23c483;
      --down: #f45b69;
      --warn: #f5bf4f;
      --blue: #5b9dff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: #12151b;
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
    }
    .sub {
      color: var(--muted);
      font-size: 12px;
      margin-top: 3px;
    }
    .status {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--warn);
    }
    .dot.live { background: var(--up); }
    main {
      width: min(1480px, 100%);
      margin: 0 auto;
      padding: 16px;
    }
    .market-row {
      display: grid;
      grid-template-columns: 1.35fr repeat(4, minmax(120px, .5fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric, .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    .metric {
      padding: 12px;
      min-height: 76px;
    }
    .label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .value {
      margin-top: 7px;
      font-size: 22px;
      font-weight: 720;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .value.small {
      font-size: 14px;
      line-height: 1.35;
      white-space: normal;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 14px;
    }
    .section h2 {
      margin: 0;
      padding: 12px 12px 10px;
      font-size: 14px;
      border-bottom: 1px solid var(--line);
    }
    .quote {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 1px;
      background: var(--line);
      border-bottom: 1px solid var(--line);
    }
    .quote div {
      background: var(--panel-2);
      padding: 10px 12px;
      min-width: 0;
    }
    .quote strong {
      display: block;
      font-size: 18px;
      margin-top: 4px;
    }
    .up { color: var(--up); }
    .down { color: var(--down); }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }
    th, td {
      padding: 7px 10px;
      border-bottom: 1px solid #242a34;
      text-align: right;
      font-variant-numeric: tabular-nums;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      background: #151922;
    }
    th:first-child, td:first-child { text-align: left; }
    .book-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 1px;
      background: var(--line);
    }
    .book-grid > div { background: var(--panel); min-width: 0; }
    .bar-cell {
      position: relative;
      isolation: isolate;
    }
    .bar {
      position: absolute;
      top: 3px;
      bottom: 3px;
      right: 0;
      opacity: .16;
      z-index: -1;
    }
    .bar.bid { background: var(--up); }
    .bar.ask { background: var(--down); }
    .history {
      margin-top: 14px;
    }
    canvas {
      width: 100%;
      height: 170px;
      display: block;
      background: #11151c;
      border-bottom: 1px solid var(--line);
    }
    .empty {
      color: var(--muted);
      padding: 18px;
      font-size: 14px;
    }
    @media (max-width: 980px) {
      .market-row, .grid { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; }
      .quote { grid-template-columns: repeat(2, 1fr); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>BTC 15m Up/Down Order Book</h1>
      <div class="sub" id="marketTitle">Waiting for snapshots...</div>
    </div>
    <div class="status"><span id="dot" class="dot"></span><span id="status">Connecting</span></div>
  </header>

  <main>
    <section class="market-row">
      <div class="metric">
        <div class="label">Market</div>
        <div class="value small" id="slug">-</div>
      </div>
      <div class="metric">
        <div class="label">Seconds To Close</div>
        <div class="value" id="secondsToClose">-</div>
      </div>
      <div class="metric">
        <div class="label">Synthetic Arb</div>
        <div class="value" id="arb">-</div>
      </div>
      <div class="metric">
        <div class="label">Snapshots</div>
        <div class="value" id="snapshots">-</div>
      </div>
      <div class="metric">
        <div class="label">Last Update</div>
        <div class="value small" id="lastUpdate">-</div>
      </div>
    </section>

    <section class="grid">
      <div class="section">
        <h2>Up Token</h2>
        <div class="quote" id="upQuote"></div>
        <div class="book-grid">
          <div><table id="upBids"></table></div>
          <div><table id="upAsks"></table></div>
        </div>
      </div>
      <div class="section">
        <h2>Down Token</h2>
        <div class="quote" id="downQuote"></div>
        <div class="book-grid">
          <div><table id="downBids"></table></div>
          <div><table id="downAsks"></table></div>
        </div>
      </div>
    </section>

    <section class="section history">
      <h2>Mid Price History</h2>
      <canvas id="chart" width="1200" height="260"></canvas>
      <table id="recent"></table>
    </section>
  </main>

  <script>
    const fmt = (value, digits = 3) => value === null || value === undefined ? "-" : Number(value).toFixed(digits);
    const pct = (value) => value === null || value === undefined ? "-" : (Number(value) * 100).toFixed(1) + "%";

    function quoteHtml(snapshot) {
      if (!snapshot) return '<div class="empty">No snapshot yet</div>';
      return [
        ['Best Bid', fmt(snapshot.best_bid), 'up'],
        ['Best Ask', fmt(snapshot.best_ask), 'down'],
        ['Mid', fmt(snapshot.mid_price), ''],
        ['Spread', fmt(snapshot.spread), '']
      ].map(([label, value, cls]) => `<div><span class="label">${label}</span><strong class="${cls}">${value}</strong></div>`).join('');
    }

    function renderLevels(id, levels, side) {
      const maxSize = Math.max(1, ...levels.map(x => Number(x.size || 0)));
      const rows = levels.map(level => {
        const width = Math.max(3, Number(level.size || 0) / maxSize * 100);
        return `<tr>
          <td>${level.level_index + 1}</td>
          <td class="bar-cell"><span class="bar ${side}" style="width:${width}%"></span>${fmt(level.price)}</td>
          <td>${fmt(level.size, 2)}</td>
        </tr>`;
      }).join('');
      document.getElementById(id).innerHTML = `<thead><tr><th>#</th><th>Price</th><th>Size</th></tr></thead><tbody>${rows}</tbody>`;
    }

    function renderRecent(rows) {
      const body = rows.map(row => `<tr>
        <td>${row.collected_utc.slice(11, 19)}</td>
        <td>${fmt(row.up_mid)}</td>
        <td>${fmt(row.down_mid)}</td>
        <td>${fmt(row.up_spread)}</td>
        <td>${fmt(row.down_spread)}</td>
      </tr>`).join('');
      document.getElementById('recent').innerHTML = `<thead><tr>
        <th>UTC</th><th>Up Mid</th><th>Down Mid</th><th>Up Spread</th><th>Down Spread</th>
      </tr></thead><tbody>${body}</tbody>`;
    }

    function drawChart(history) {
      const canvas = document.getElementById('chart');
      const ctx = canvas.getContext('2d');
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = '#11151c';
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = '#303642';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const y = 20 + i * (h - 40) / 4;
        ctx.beginPath(); ctx.moveTo(42, y); ctx.lineTo(w - 16, y); ctx.stroke();
      }
      if (!history.length) return;
      const xs = history.map(x => x.collected_ts);
      const minX = Math.min(...xs), maxX = Math.max(...xs);
      const xPos = ts => 42 + ((ts - minX) / Math.max(1, maxX - minX)) * (w - 58);
      const yPos = p => 20 + (1 - Number(p || 0)) * (h - 40);

      function line(key, color) {
        ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
        let started = false;
        for (const row of history) {
          if (row[key] === null || row[key] === undefined) continue;
          const x = xPos(row.collected_ts), y = yPos(row[key]);
          if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
        }
        ctx.stroke();
      }
      line('up_mid', '#23c483');
      line('down_mid', '#f45b69');
      ctx.fillStyle = '#9aa4b2';
      ctx.font = '12px system-ui';
      ctx.fillText('1.00', 6, 24);
      ctx.fillText('0.50', 6, h / 2 + 4);
      ctx.fillText('0.00', 6, h - 20);
    }

    async function tick() {
      try {
        const res = await fetch('/api/state', { cache: 'no-store' });
        const data = await res.json();
        if (!data.market) throw new Error('No data yet');
        document.getElementById('dot').classList.add('live');
        document.getElementById('status').textContent = 'Live';
        document.getElementById('marketTitle').textContent = data.market.title;
        document.getElementById('slug').textContent = data.market.slug;
        document.getElementById('secondsToClose').textContent = data.market.seconds_to_close;
        document.getElementById('snapshots').textContent = data.snapshot_count;
        document.getElementById('lastUpdate').textContent = data.last_update_utc || '-';
        const arb = data.synthetic_arb;
        document.getElementById('arb').textContent = arb === null ? '-' : fmt(arb, 4);
        document.getElementById('arb').className = arb !== null && arb < 1 ? 'value up' : 'value';
        document.getElementById('upQuote').innerHTML = quoteHtml(data.latest.Up);
        document.getElementById('downQuote').innerHTML = quoteHtml(data.latest.Down);
        renderLevels('upBids', data.levels.Up.bid, 'bid');
        renderLevels('upAsks', data.levels.Up.ask, 'ask');
        renderLevels('downBids', data.levels.Down.bid, 'bid');
        renderLevels('downAsks', data.levels.Down.ask, 'ask');
        renderRecent(data.history.slice(-20).reverse());
        drawChart(data.history);
      } catch (err) {
        document.getElementById('dot').classList.remove('live');
        document.getElementById('status').textContent = err.message;
      }
    }
    tick();
    setInterval(tick, 1000);
  </script>
</body>
</html>
"""


def row_to_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path

    def log_message(self, format: str, *args) -> None:
        return

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_html()
            return
        if path == "/api/state":
            self.send_json(load_state(self.db_path))
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def latest_market(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM markets
        ORDER BY event_start_ts DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["seconds_to_close"] = max(0, item["end_ts"] - int(datetime.now(timezone.utc).timestamp()))
    return item


def latest_snapshot(conn: sqlite3.Connection, slug: str, outcome: str) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM orderbook_snapshots
        WHERE slug = ? AND outcome = ?
        ORDER BY collected_ts DESC, id DESC
        LIMIT 1
        """,
        (slug, outcome),
    ).fetchone()
    return dict(row) if row else None


def levels(conn: sqlite3.Connection, snapshot_id: int | None, side: str) -> list[dict]:
    if snapshot_id is None:
        return []
    rows = conn.execute(
        """
        SELECT side, price, size, level_index
        FROM orderbook_levels
        WHERE snapshot_id = ? AND side = ?
        ORDER BY level_index ASC
        LIMIT 12
        """,
        (snapshot_id, side),
    ).fetchall()
    return [dict(row) for row in rows]


def history(conn: sqlite3.Connection, slug: str) -> list[dict]:
    rows = conn.execute(
        """
        WITH paired AS (
            SELECT
                collected_ts,
                collected_utc,
                MAX(CASE WHEN outcome = 'Up' THEN mid_price END) AS up_mid,
                MAX(CASE WHEN outcome = 'Down' THEN mid_price END) AS down_mid,
                MAX(CASE WHEN outcome = 'Up' THEN spread END) AS up_spread,
                MAX(CASE WHEN outcome = 'Down' THEN spread END) AS down_spread
            FROM orderbook_snapshots
            WHERE slug = ?
            GROUP BY collected_ts, collected_utc
            ORDER BY collected_ts DESC
            LIMIT 240
        )
        SELECT * FROM paired ORDER BY collected_ts ASC
        """,
        (slug,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_state(db_path: Path) -> dict:
    if not db_path.exists():
        return {"market": None, "error": "Database does not exist yet"}
    with connect(db_path) as conn:
        market = latest_market(conn)
        if not market:
            return {"market": None, "error": "No snapshots yet"}
        up = latest_snapshot(conn, market["slug"], "Up")
        down = latest_snapshot(conn, market["slug"], "Down")
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM orderbook_snapshots WHERE slug = ?",
            (market["slug"],),
        ).fetchone()[0]
        synthetic_arb = None
        if up and down and up["best_ask"] is not None and down["best_ask"] is not None:
            synthetic_arb = up["best_ask"] + down["best_ask"]
        return {
            "market": market,
            "latest": {"Up": up, "Down": down},
            "levels": {
                "Up": {
                    "bid": levels(conn, up["id"] if up else None, "bid"),
                    "ask": levels(conn, up["id"] if up else None, "ask"),
                },
                "Down": {
                    "bid": levels(conn, down["id"] if down else None, "bid"),
                    "ask": levels(conn, down["id"] if down else None, "ask"),
                },
            },
            "history": history(conn, market["slug"]),
            "snapshot_count": snapshot_count,
            "last_update_utc": max(
                [value for value in [up and up["collected_utc"], down and down["collected_utc"]] if value],
                default=None,
            ),
            "synthetic_arb": synthetic_arb,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    DashboardHandler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Serving dashboard at http://{args.host}:{args.port}")
    print(f"Reading database: {args.db}")
    server.serve_forever()


if __name__ == "__main__":
    main()
