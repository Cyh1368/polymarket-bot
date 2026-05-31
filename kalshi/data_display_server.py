#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, render_template_string, send_file


APP_DIR = Path(__file__).resolve().parent
COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB")
MAX_PREVIEW_ROWS = 12
CSV_EXCLUDE = {"cli_trader_v2_balances.csv"}

app = Flask(__name__)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Crypto Contract Data</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #101214;
      color: #e8ecef;
    }
    body {
      margin: 0;
      background: #101214;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 2;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid #2c3136;
      background: #171a1d;
    }
    h1 {
      margin: 0;
      font-size: 16px;
      letter-spacing: 0;
    }
    main {
      padding: 18px;
      display: grid;
      gap: 18px;
    }
    .coin {
      border: 1px solid #30363d;
      border-radius: 8px;
      background: #15191d;
      overflow: hidden;
    }
    .coin-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 12px 14px;
      border-bottom: 1px solid #30363d;
      background: #1b2025;
    }
    .coin-title {
      display: flex;
      align-items: baseline;
      gap: 12px;
      min-width: 0;
    }
    .coin-name {
      font-weight: 750;
      color: #67e8f9;
    }
    .muted {
      color: #9ba7b0;
      font-size: 13px;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    a.button {
      color: #e8ecef;
      text-decoration: none;
      border: 1px solid #3a424b;
      border-radius: 6px;
      padding: 6px 9px;
      font-size: 13px;
      background: #222830;
    }
    a.button:hover {
      background: #2a313a;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid #30363d;
    }
    .metric {
      min-width: 0;
    }
    .label {
      color: #9ba7b0;
      font-size: 12px;
    }
    .value {
      margin-top: 2px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .table-wrap {
      overflow: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    th, td {
      padding: 7px 8px;
      border-bottom: 1px solid #242a30;
      text-align: left;
      white-space: nowrap;
    }
    th {
      color: #b7c0c8;
      background: #15191d;
      position: sticky;
      top: 49px;
    }
    .empty {
      padding: 12px 14px;
      color: #fca5a5;
    }
  </style>
</head>
<body>
  <header>
    <h1>Crypto 15m Data Availability</h1>
    <div class="muted">Latest refresh: {{ generated_at }}</div>
  </header>
  <main>
    {% for coin in coins %}
    <section class="coin">
      <div class="coin-header">
        <div class="coin-title">
          <span class="coin-name">{{ coin.symbol }}</span>
          <span class="muted">{{ coin.folder }}</span>
        </div>
        <div class="actions">
          {% if coin.latest_name %}
          <a class="button" href="/download/{{ coin.symbol }}">Download latest</a>
          <a class="button" href="/files/{{ coin.symbol }}">All CSVs</a>
          {% endif %}
        </div>
      </div>
      {% if coin.latest_name %}
      <div class="summary">
        <div class="metric"><div class="label">Latest CSV</div><div class="value">{{ coin.latest_name }}</div></div>
        <div class="metric"><div class="label">Modified</div><div class="value">{{ coin.modified }}</div></div>
        <div class="metric"><div class="label">Rows</div><div class="value">{{ coin.row_count }}</div></div>
        <div class="metric"><div class="label">Kalshi Ticker</div><div class="value">{{ coin.last.kalshi_ticker }}</div></div>
        <div class="metric"><div class="label">Polymarket</div><div class="value">{{ coin.last.polymarket_ticker }}</div></div>
        <div class="metric"><div class="label">Kalshi Price</div><div class="value">{{ coin.last.kalshi_btc_price }}</div></div>
        <div class="metric"><div class="label">Polymarket Price</div><div class="value">{{ coin.last.polymarket_btc_price }}</div></div>
        <div class="metric"><div class="label">K+NP / NK+P</div><div class="value">{{ coin.last.k_plus_np }} / {{ coin.last.nk_plus_p }}</div></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              {% for col in coin.preview_columns %}
              <th>{{ col }}</th>
              {% endfor %}
            </tr>
          </thead>
          <tbody>
            {% for row in coin.preview_rows %}
            <tr>
              {% for col in coin.preview_columns %}
              <td>{{ row.get(col, "") }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty">No CSV found yet for {{ coin.symbol }}.</div>
      {% endif %}
    </section>
    {% endfor %}
  </main>
</body>
</html>
"""


FILES_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ symbol }} CSVs</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #101214; color: #e8ecef; }
    body { margin: 0; padding: 18px; background: #101214; }
    a { color: #67e8f9; }
    table { width: 100%; border-collapse: collapse; margin-top: 14px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }
    th, td { padding: 8px; border-bottom: 1px solid #30363d; text-align: left; }
  </style>
</head>
<body>
  <a href="/">Back</a>
  <h1>{{ symbol }} CSVs</h1>
  <table>
    <thead><tr><th>File</th><th>Modified</th><th>Size</th><th>Download</th></tr></thead>
    <tbody>
      {% for file in files %}
      <tr>
        <td>{{ file.name }}</td>
        <td>{{ file.modified }}</td>
        <td>{{ file.size }}</td>
        <td><a href="/download/{{ symbol }}/{{ file.name }}">Download</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</body>
</html>
"""


PREVIEW_COLUMNS = [
    "timestamp_utc",
    "kalshi_ticker",
    "polymarket_ticker",
    "kalshi_yes_bid",
    "kalshi_yes_ask",
    "polymarket_yes_bid",
    "polymarket_yes_ask",
    "kalshi_btc_price",
    "kalshi_btc_target",
    "polymarket_btc_price",
    "polymarket_btc_target",
    "k_plus_np",
    "nk_plus_p",
    "polymarket_error",
]


def coin_dirs(symbol: str) -> list[Path]:
    dirs = [APP_DIR / f"data_{symbol}"]
    if symbol == "BTC":
        dirs.append(APP_DIR / "kalshi_btc15m_data")
    return dirs


def csv_files(symbol: str) -> list[Path]:
    files: list[Path] = []
    for folder in coin_dirs(symbol):
        if not folder.exists():
            continue
        files.extend(
            path
            for path in folder.glob("*.csv")
            if path.is_file() and path.name not in CSV_EXCLUDE
        )
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def latest_csv(symbol: str) -> Path | None:
    files = csv_files(symbol)
    return files[0] if files else None


def safe_csv_path(symbol: str, filename: str | None = None) -> Path:
    symbol = symbol.upper()
    if symbol not in COINS:
        abort(404)
    if not filename:
        path = latest_csv(symbol)
        if path is None:
            abort(404)
        return path
    name = Path(filename).name
    for path in csv_files(symbol):
        if path.name == name:
            return path
    abort(404)


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv_preview(path: Path) -> tuple[int, list[str], list[dict[str, Any]], dict[str, Any]]:
    rows: deque[dict[str, Any]] = deque(maxlen=MAX_PREVIEW_ROWS)
    row_count = 0
    with path.open(newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        fieldnames = reader.fieldnames or []
        for row in reader:
            row_count += 1
            rows.append({key: str(value) if value is not None else "" for key, value in row.items()})
    last = rows[-1] if rows else {}
    columns = [col for col in PREVIEW_COLUMNS if col in fieldnames]
    if not columns:
        columns = fieldnames[:14]
    return row_count, columns, list(rows), last


def coin_status(symbol: str) -> dict[str, Any]:
    path = latest_csv(symbol)
    if path is None:
        folder = coin_dirs(symbol)[0]
        return {
            "symbol": symbol,
            "folder": str(folder.relative_to(APP_DIR) if folder.is_relative_to(APP_DIR) else folder),
            "latest_name": "",
            "modified": "",
            "row_count": 0,
            "preview_columns": [],
            "preview_rows": [],
            "last": {},
        }
    row_count, columns, rows, last = read_csv_preview(path)
    folder = path.parent
    return {
        "symbol": symbol,
        "folder": str(folder.relative_to(APP_DIR) if folder.is_relative_to(APP_DIR) else folder),
        "latest_name": path.name,
        "modified": iso_mtime(path),
        "row_count": row_count,
        "preview_columns": columns,
        "preview_rows": rows,
        "last": last,
    }


@app.get("/")
def index() -> str:
    coins = [coin_status(symbol) for symbol in COINS]
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return render_template_string(PAGE, coins=coins, generated_at=generated_at)


@app.get("/api/latest")
def api_latest() -> Response:
    return jsonify([coin_status(symbol) for symbol in COINS])


@app.get("/files/<symbol>")
def files_page(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol not in COINS:
        abort(404)
    files = [
        {
            "name": path.name,
            "modified": iso_mtime(path),
            "size": f"{path.stat().st_size:,} bytes",
        }
        for path in csv_files(symbol)
    ]
    return render_template_string(FILES_PAGE, symbol=symbol, files=files)


@app.get("/download/<symbol>")
@app.get("/download/<symbol>/<path:filename>")
def download(symbol: str, filename: str | None = None) -> Response:
    path = safe_csv_path(symbol, filename)
    return send_file(path, as_attachment=True, download_name=path.name, mimetype="text/csv")


def main() -> None:
    port = int(os.getenv("PORT", "8010"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
