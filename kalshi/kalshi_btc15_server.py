#!/usr/bin/env python3
import base64
import csv
import html
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    pending_key: str | None = None
    pending_value: list[str] = []
    for raw_line in path.read_text().splitlines():
        if pending_key:
            pending_value.append(raw_line)
            if "END " in raw_line and "PRIVATE KEY" in raw_line:
                if pending_key not in os.environ:
                    os.environ[pending_key] = "\n".join(pending_value)
                pending_key = None
                pending_value = []
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if "BEGIN " in value and "PRIVATE KEY" in value and "END " not in value:
            pending_key = key
            pending_value = [value]
            continue
        if key and key not in os.environ:
            os.environ[key] = value.replace("\\n", "\n")


load_dotenv()

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8090"))
BASE_URL = os.getenv("KALSHI_API_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
SERIES_TICKER = os.getenv("KALSHI_SERIES_TICKER", "KXBTC15M")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
ORDERBOOK_DEPTH = int(os.getenv("ORDERBOOK_DEPTH", "10"))
DATA_DIR = Path(os.getenv("KALSHI_DATA_DIR", "kalshi_btc15m_data"))


STATE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "series_ticker": SERIES_TICKER,
    "active_ticker": None,
    "active_market": None,
    "orderbook": None,
    "snapshot": None,
    "history": [],
    "last_fetch_at": None,
    "next_fetch_at": None,
    "error": None,
    "consecutive_errors": 0,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seconds_from_now(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def private_key_pem() -> str | None:
    for key in ("KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PEM"):
        value = os.getenv(key)
        if value:
            possible_path = Path(value).expanduser()
            if possible_path.exists():
                return possible_path.read_text()
            return value
    path_value = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if path_value:
        path = Path(path_value).expanduser()
        if path.exists():
            return path.read_text()
    return None


def auth_headers(method: str, path: str) -> dict[str, str]:
    key_id = (
        os.getenv("KALSHI_API_ID")
        or os.getenv("KALSHI_KEY_ID")
        or os.getenv("KALSHI_API_KEY_ID")
        or os.getenv("KALSHI_ACCESS_KEY")
    )
    pem = private_key_pem()
    if not key_id or not pem:
        return {}
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception:
        return {}

    timestamp = str(int(time.time() * 1000))
    parsed = urlparse(BASE_URL)
    api_path = parsed.path.rstrip("/") + path
    signing_payload = f"{timestamp}{method.upper()}{api_path}".encode()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    signature = key.sign(
        signing_payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def request_json(path: str, params: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
    query = f"?{urlencode(params, doseq=True)}" if params else ""
    url = f"{BASE_URL}{path}{query}"
    headers = {"Accept": "application/json", "User-Agent": "kalshi-btc15m-monitor/1.0"}
    if auth:
        headers.update(auth_headers("GET", path))
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def kalshi_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return request_json(path, params=params, auth=False)
    except HTTPError as exc:
        if exc.code != 401:
            raise
        return request_json(path, params=params, auth=True)


def parse_ts(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def normalize_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100.0
    return max(0.0, min(1.0, number))


def best_level(levels: Any, reverse: bool = True) -> tuple[float | None, float | None]:
    if not isinstance(levels, list) or not levels:
        return None, None
    parsed: list[tuple[float, float | None]] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or not level:
            continue
        price = normalize_price(level[0])
        quantity = None
        if len(level) > 1:
            try:
                quantity = float(level[1])
            except (TypeError, ValueError):
                quantity = None
        if price is not None:
            parsed.append((price, quantity))
    if not parsed:
        return None, None
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return parsed[0]


def market_price(market: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = normalize_price(market.get(key))
        if value is not None:
            return value
    return None


def discover_active_market() -> dict[str, Any] | None:
    data = kalshi_get(
        "/markets",
        {
            "series_ticker": SERIES_TICKER,
            "status": "open",
            "limit": 200,
        },
    )
    markets = data.get("markets", [])
    if not markets:
        data = kalshi_get("/markets", {"event_ticker": SERIES_TICKER, "status": "open", "limit": 200})
        markets = data.get("markets", [])
    if not markets:
        data = kalshi_get("/markets", {"status": "open", "limit": 1000})
        markets = [
            market
            for market in data.get("markets", [])
            if str(market.get("ticker", "")).startswith(SERIES_TICKER)
            or str(market.get("event_ticker", "")).startswith(SERIES_TICKER)
        ]
    if not markets:
        return None
    markets.sort(
        key=lambda market: (
            parse_ts(market.get("close_time") or market.get("close_ts") or market.get("expiration_time")),
            str(market.get("ticker", "")),
        )
    )
    return markets[0]


def orderbook_levels(orderbook: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    book = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {}
    yes = book.get("yes_dollars") or book.get("yes") or []
    no = book.get("no_dollars") or book.get("no") or []
    return yes, no


def make_snapshot(market: dict[str, Any], orderbook: dict[str, Any]) -> dict[str, Any]:
    yes_levels, no_levels = orderbook_levels(orderbook)
    best_yes_bid, best_yes_bid_qty = best_level(yes_levels)
    best_no_bid, best_no_bid_qty = best_level(no_levels)
    yes_bid = market_price(market, "yes_bid_dollars", "yes_bid", "bid", "last_price") or best_yes_bid
    yes_ask = market_price(market, "yes_ask_dollars", "yes_ask", "ask")
    if yes_ask is None and best_no_bid is not None:
        yes_ask = 1.0 - best_no_bid
    no_bid = market_price(market, "no_bid_dollars", "no_bid") or best_no_bid
    no_ask = market_price(market, "no_ask_dollars", "no_ask")
    if no_ask is None and best_yes_bid is not None:
        no_ask = 1.0 - best_yes_bid
    midpoint = None
    if yes_bid is not None and yes_ask is not None:
        midpoint = (yes_bid + yes_ask) / 2.0
    elif yes_bid is not None:
        midpoint = yes_bid
    elif yes_ask is not None:
        midpoint = yes_ask
    return {
        "timestamp_utc": iso_utc(),
        "ticker": market.get("ticker"),
        "title": market.get("title") or market.get("subtitle") or "",
        "event_ticker": market.get("event_ticker") or "",
        "close_time": market.get("close_time") or market.get("close_ts") or market.get("expiration_time") or "",
        "status": market.get("status") or "",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": midpoint,
        "last_price": market_price(market, "last_price_dollars", "last_price"),
        "volume": market.get("volume") or market.get("volume_fp") or market.get("volume_24h") or market.get("volume_24h_fp") or "",
        "open_interest": market.get("open_interest") or market.get("open_interest_fp") or "",
        "best_yes_bid_qty": best_yes_bid_qty,
        "best_no_bid_qty": best_no_bid_qty,
        "yes_levels": yes_levels,
        "no_levels": no_levels,
    }


def append_snapshot_csv(snapshot: dict[str, Any]) -> None:
    ticker = snapshot.get("ticker") or "unknown"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{ticker}.csv"
    exists = path.exists()
    fields = [
        "timestamp_utc",
        "ticker",
        "title",
        "event_ticker",
        "close_time",
        "status",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "yes_mid",
        "last_price",
        "volume",
        "open_interest",
        "best_yes_bid_qty",
        "best_no_bid_qty",
        "yes_levels_json",
        "no_levels_json",
    ]
    row = {key: snapshot.get(key, "") for key in fields}
    row["yes_levels_json"] = json.dumps(snapshot.get("yes_levels", []), separators=(",", ":"))
    row["no_levels_json"] = json.dumps(snapshot.get("no_levels", []), separators=(",", ":"))
    with path.open("a", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def poll_once() -> None:
    market = discover_active_market()
    if not market:
        raise RuntimeError(f"No open market found for {SERIES_TICKER}")
    ticker = market["ticker"]
    orderbook = kalshi_get(f"/markets/{ticker}/orderbook", {"depth": ORDERBOOK_DEPTH})
    snapshot = make_snapshot(market, orderbook)
    append_snapshot_csv(snapshot)
    with STATE_LOCK:
        if STATE.get("active_ticker") != ticker:
            STATE["history"] = []
        STATE.update(
            {
                "active_ticker": ticker,
                "active_market": market,
                "orderbook": orderbook,
                "snapshot": snapshot,
                "last_fetch_at": snapshot["timestamp_utc"],
                "next_fetch_at": seconds_from_now(POLL_SECONDS),
                "error": None,
                "consecutive_errors": 0,
            }
        )
        STATE["history"].append(
            {
                "timestamp_utc": snapshot["timestamp_utc"],
                "ticker": ticker,
                "yes_mid": snapshot["yes_mid"],
                "yes_bid": snapshot["yes_bid"],
                "yes_ask": snapshot["yes_ask"],
            }
        )
        STATE["history"] = STATE["history"][-720:]


def polling_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            poll_once()
        except Exception as exc:
            with STATE_LOCK:
                STATE["error"] = f"{type(exc).__name__}: {exc}"
                STATE["consecutive_errors"] = int(STATE.get("consecutive_errors", 0)) + 1
                STATE["last_traceback"] = traceback.format_exc(limit=3)
        elapsed = time.monotonic() - started
        sleep_for = max(1.0, POLL_SECONDS - elapsed)
        with STATE_LOCK:
            STATE["next_fetch_at"] = seconds_from_now(sleep_for)
        time.sleep(sleep_for)


def state_payload() -> dict[str, Any]:
    with STATE_LOCK:
        payload = json.loads(json.dumps(STATE, default=str))
    payload["poll_seconds"] = POLL_SECONDS
    payload["orderbook_depth"] = ORDERBOOK_DEPTH
    payload["data_dir"] = str(DATA_DIR)
    return payload


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kalshi BTC 15m Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101418;
      --panel: #181f26;
      --panel-2: #202933;
      --text: #e9eef4;
      --muted: #9cacbb;
      --line: #34414e;
      --green: #31c48d;
      --red: #f98080;
      --blue: #7db7ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 20px 24px 12px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 0; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 5px; }
    .status { text-align: right; font-size: 13px; color: var(--muted); }
    main { padding: 18px 24px 24px; display: grid; gap: 18px; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(150px, 1fr));
      gap: 12px;
    }
    .metric, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .metric { padding: 14px; min-height: 86px; }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .value { font-size: 28px; margin-top: 8px; font-weight: 750; }
    .small-value { font-size: 17px; overflow-wrap: anywhere; }
    .grid { display: grid; grid-template-columns: 1.4fr .9fr; gap: 18px; }
    .panel { padding: 16px; min-width: 0; }
    .panel h2 { margin: 0 0 12px; font-size: 15px; font-weight: 700; }
    .chart-wrap { height: 360px; }
    canvas { width: 100%; height: 100%; display: block; }
    table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
    th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: right; font-size: 13px; }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    .books { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .error { color: var(--red); white-space: pre-wrap; }
    @media (max-width: 1000px) {
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid, .books { grid-template-columns: 1fr; }
      header { align-items: start; flex-direction: column; }
      .status { text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Kalshi BTC 15m Monitor</h1>
      <div class="sub" id="title">Waiting for first market snapshot</div>
    </div>
    <div class="status">
      <div id="clock">Now: --</div>
      <div id="ticker">Ticker: --</div>
      <div id="timing">Last fetch: --</div>
    </div>
  </header>
  <main>
    <section class="metrics">
      <div class="metric"><div class="label">YES midpoint</div><div class="value" id="yesMid">--</div></div>
      <div class="metric"><div class="label">YES bid / ask</div><div class="value small-value" id="yesBidAsk">--</div></div>
      <div class="metric"><div class="label">NO bid / ask</div><div class="value small-value" id="noBidAsk">--</div></div>
      <div class="metric"><div class="label">Volume</div><div class="value small-value" id="volume">--</div></div>
      <div class="metric"><div class="label">Close time</div><div class="value small-value" id="closeTime">--</div></div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Odds Over Time</h2>
        <div class="chart-wrap"><canvas id="oddsChart"></canvas></div>
      </div>
      <div class="panel">
        <h2>Current Snapshot</h2>
        <table>
          <tbody id="snapshotTable"></tbody>
        </table>
        <p class="error" id="error"></p>
      </div>
    </section>
    <section class="panel">
      <h2>Order Book</h2>
      <div class="books">
        <div>
          <h2>YES bids</h2>
          <table><thead><tr><th>Price</th><th>Contracts</th></tr></thead><tbody id="yesBook"></tbody></table>
        </div>
        <div>
          <h2>NO bids</h2>
          <table><thead><tr><th>Price</th><th>Contracts</th></tr></thead><tbody id="noBook"></tbody></table>
        </div>
      </div>
    </section>
  </main>
  <script>
    const fmtPct = value => value === null || value === undefined ? "--" : `${(value * 100).toFixed(1)}¢`;
    const fmtNum = value => value === null || value === undefined || value === "" ? "--" : Number(value).toLocaleString();
    let latestHistory = [];

    function row(label, value) {
      return `<tr><td>${label}</td><td>${value}</td></tr>`;
    }
    function bookRows(levels) {
      if (!Array.isArray(levels) || levels.length === 0) return `<tr><td colspan="2">No levels</td></tr>`;
      return levels.slice(0, 10).map(level => {
        const price = Number(level[0]);
        const qty = Number(level[1]);
        return `<tr><td>${Number.isFinite(price) ? (price * 100).toFixed(1) + "¢" : "--"}</td><td>${Number.isFinite(qty) ? qty.toLocaleString() : "--"}</td></tr>`;
      }).join("");
    }
    function localTime(value) {
      if (!value) return "--";
      const date = new Date(value);
      return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
    }
    function drawChart() {
      const canvas = document.getElementById("oddsChart");
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      const width = rect.width;
      const height = rect.height;
      const pad = { left: 48, right: 16, top: 18, bottom: 54 };
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#181f26";
      ctx.fillRect(0, 0, width, height);
      ctx.font = "12px Inter, system-ui, sans-serif";
      ctx.textBaseline = "middle";
      for (let y = 0; y <= 100; y += 20) {
        const py = pad.top + (100 - y) / 100 * (height - pad.top - pad.bottom);
        ctx.strokeStyle = "#26313c";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, py);
        ctx.lineTo(width - pad.right, py);
        ctx.stroke();
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "right";
        ctx.fillText(`${y}¢`, pad.left - 8, py);
      }
      const points = latestHistory.length;
      const xFor = index => points <= 1 ? pad.left : pad.left + index / (points - 1) * (width - pad.left - pad.right);
      const yFor = value => pad.top + (100 - value * 100) / 100 * (height - pad.top - pad.bottom);
      ctx.strokeStyle = "#7db7ff";
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      let started = false;
      latestHistory.forEach((item, index) => {
        const value = item.yes_mid;
        if (value === null || value === undefined) return;
        const x = xFor(index);
        const y = yFor(value);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      if (started) ctx.stroke();
      const tickCount = Math.min(6, Math.max(2, points));
      if (points > 0) {
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        for (let i = 0; i < tickCount; i++) {
          const index = tickCount === 1 ? 0 : Math.round(i * (points - 1) / (tickCount - 1));
          const x = xFor(index);
          const time = new Date(latestHistory[index].timestamp_utc).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
          });
          ctx.strokeStyle = "#34414e";
          ctx.beginPath();
          ctx.moveTo(x, height - pad.bottom);
          ctx.lineTo(x, height - pad.bottom + 5);
          ctx.stroke();
          ctx.fillText(time, x, height - pad.bottom + 10);
        }
      }
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#7db7ff";
      ctx.fillRect(pad.left, height - 14, 14, 3);
      ctx.fillStyle = "#e9eef4";
      ctx.fillText("YES mid", pad.left + 20, height - 14);
      if (!latestHistory.length) {
        ctx.fillStyle = "#9cacbb";
        ctx.textAlign = "center";
        ctx.fillText("Waiting for odds history", width / 2, height / 2);
      }
    }
    function updateClock() {
      document.getElementById("clock").textContent = `Now: ${new Date().toLocaleString()}`;
    }
    async function refresh() {
      const response = await fetch("/api/state", { cache: "no-store" });
      const state = await response.json();
      const snap = state.snapshot || {};
      document.getElementById("title").textContent = snap.title || "Waiting for first market snapshot";
      document.getElementById("ticker").textContent = `Ticker: ${state.active_ticker || "--"}`;
      document.getElementById("timing").textContent = `Last fetch: ${localTime(state.last_fetch_at)} | every ${state.poll_seconds}s`;
      document.getElementById("yesMid").textContent = fmtPct(snap.yes_mid);
      document.getElementById("yesBidAsk").textContent = `${fmtPct(snap.yes_bid)} / ${fmtPct(snap.yes_ask)}`;
      document.getElementById("noBidAsk").textContent = `${fmtPct(snap.no_bid)} / ${fmtPct(snap.no_ask)}`;
      document.getElementById("volume").textContent = fmtNum(snap.volume);
      document.getElementById("closeTime").textContent = localTime(snap.close_time);
      document.getElementById("snapshotTable").innerHTML = [
        row("Event", snap.event_ticker || "--"),
        row("Open interest", fmtNum(snap.open_interest)),
        row("Last price", fmtPct(snap.last_price)),
        row("CSV directory", state.data_dir),
        row("Depth", state.orderbook_depth)
      ].join("");
      document.getElementById("error").textContent = state.error ? `Fetch issue: ${state.error}` : "";
      const yesLevels = Array.isArray(snap.yes_levels) ? [...snap.yes_levels].sort((a, b) => Number(b[0]) - Number(a[0])) : [];
      document.getElementById("yesBook").innerHTML = bookRows(yesLevels);
      document.getElementById("noBook").innerHTML = bookRows(snap.no_levels);
      latestHistory = state.history || [];
      drawChart();
    }
    updateClock();
    refresh().catch(console.error);
    setInterval(updateClock, 1000);
    setInterval(() => refresh().catch(console.error), 2000);
    window.addEventListener("resize", drawChart);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
            return
        if parsed.path == "/api/state":
            body = json.dumps(state_payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{iso_utc()} {self.address_string()} {fmt % args}")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(target=polling_loop, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Kalshi BTC 15m monitor listening on http://{HOST}:{PORT}")
    print(f"Polling {SERIES_TICKER} every {POLL_SECONDS:g}s; writing CSV files to {DATA_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
