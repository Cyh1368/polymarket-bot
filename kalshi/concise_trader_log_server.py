#!/usr/bin/env python3
import csv
import math
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "concise_trader_log.txt"
BALANCE_CSV_PATH = APP_DIR / "kalshi_btc15m_data" / "cli_trader_v2_balances.csv"
MAX_BYTES = 250_000
MAX_LINES = 200
MAX_PORTFOLIO_POINTS = 2_000
MAX_REASONABLE_PORTFOLIO_VALUE = 1_000_000.0

app = Flask(__name__)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trader Log</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: #101214;
      color: #e8ecef;
    }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      background: #101214;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 10px 14px;
      border-bottom: 1px solid #2c3136;
      background: #171a1d;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    h1 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
      letter-spacing: 0;
    }
    #status {
      color: #9ba7b0;
      font-size: 13px;
      white-space: nowrap;
    }
    pre {
      flex: 1;
      margin: 0;
      padding: 12px 14px 28px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.45;
      font-size: 13px;
      color: #eef2f5;
    }
    .line {
      display: block;
      min-height: 1.45em;
    }
    .entry-skip {
      color: #8b949e;
    }
    .model-line {
      color: #67e8f9;
      font-weight: 700;
    }
    .entry-filled {
      color: #4ade80;
      font-weight: 700;
    }
    .balance-line {
      color: #facc15;
    }
    .contract-start {
      font-weight: 700;
    }
    .trade-executed {
      color: #4ade80;
    }
    .position-exited {
      color: #f87171;
    }
    .position-closed {
      color: #facc15;
    }
    .budget-total {
      color: #facc15;
      font-weight: 700;
    }
    #portfolioButton {
      position: fixed;
      right: 18px;
      bottom: 18px;
      width: 44px;
      height: 44px;
      border: 1px solid #3b4650;
      border-radius: 8px;
      background: #20262b;
      color: #e8ecef;
      display: grid;
      place-items: center;
      cursor: pointer;
      box-shadow: 0 8px 22px rgba(0, 0, 0, 0.35);
    }
    #portfolioButton:hover {
      background: #2a3239;
      border-color: #566470;
    }
    #portfolioButton svg {
      width: 24px;
      height: 24px;
      stroke: currentColor;
    }
    .chart-overlay {
      position: fixed;
      inset: 0;
      display: grid;
      place-items: center;
      background: rgba(0, 0, 0, 0.56);
      padding: 20px;
    }
    .chart-overlay[hidden] {
      display: none;
    }
    .chart-dialog {
      width: min(900px, calc(100vw - 40px));
      border: 1px solid #3b4650;
      border-radius: 8px;
      background: #171a1d;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.48);
    }
    .chart-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid #2c3136;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      font-weight: 650;
    }
    .chart-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      color: #9ba7b0;
      font-size: 12px;
      font-weight: 500;
    }
    .chart-actions button {
      border: 1px solid #3b4650;
      border-radius: 6px;
      background: #20262b;
      color: #e8ecef;
      padding: 4px 8px;
      cursor: pointer;
      font: inherit;
    }
    .chart-body {
      padding: 12px;
    }
    #portfolioChart {
      width: 100%;
      height: min(460px, 62vh);
      display: block;
      background: #101214;
      border: 1px solid #2c3136;
      border-radius: 6px;
    }
    #chartMessage {
      min-height: 18px;
      margin-top: 8px;
      color: #9ba7b0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
    }
  </style>
</head>
<body>
  <header>
    <h1>concise_trader_log.txt</h1>
    <div id="status">loading</div>
  </header>
  <pre id="log"></pre>
  <button id="portfolioButton" type="button" aria-label="Portfolio value chart" title="Portfolio value chart">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M3 3v18h18"></path>
      <path d="m7 14 4-4 3 3 5-7"></path>
    </svg>
  </button>
  <div id="chartOverlay" class="chart-overlay" hidden>
    <div class="chart-dialog" role="dialog" aria-modal="true" aria-labelledby="chartTitle">
      <div class="chart-header">
        <div id="chartTitle">Portfolio Value</div>
        <div class="chart-actions">
          <span id="chartStatus"></span>
          <button id="chartRefresh" type="button">Refresh</button>
          <button id="chartClose" type="button">Close</button>
        </div>
      </div>
      <div class="chart-body">
        <canvas id="portfolioChart"></canvas>
        <div id="chartMessage"></div>
      </div>
    </div>
  </div>
  <script>
    const logEl = document.getElementById("log");
    const statusEl = document.getElementById("status");
    const portfolioButton = document.getElementById("portfolioButton");
    const chartOverlay = document.getElementById("chartOverlay");
    const chartClose = document.getElementById("chartClose");
    const chartRefresh = document.getElementById("chartRefresh");
    const chartStatus = document.getElementById("chartStatus");
    const chartMessage = document.getElementById("chartMessage");
    const chartCanvas = document.getElementById("portfolioChart");
    let lastText = "";

    let firstLoad = true;

    function classifyLine(line) {
      if (line.includes("CONTRACT ")) {
        return "contract-start";
      }
      if (line.startsWith("MODEL ") || line.includes(" | MODEL ")) {
        return "model-line";
      }
      if (line.startsWith("ENTRY FILLED ") || line.includes(" | ENTRY FILLED ")) {
        return "entry-filled";
      }
      if (line.startsWith("BALANCE ") || line.includes(" | BALANCE ")) {
        return "balance-line";
      }
      if (line.includes("ENTRY SKIP") || line.includes("CHECK SKIP") || line.includes("HOLD continue")) {
        return "entry-skip";
      }
      if (line.includes("TRADED ") || line.includes("DRY RUN would place") || line.includes("DRY ENTRY ")) {
        return "trade-executed";
      }
      if (line.includes("EXITED")) {
        return "position-closed";
      }
      if (line.includes("EXIT_REVIEW") || line.includes("FATAL ")) {
        return "position-exited";
      }
      return "";
    }

    function cleanAnsi(line) {
      return line.replace(/\\x1b\\[[0-9;]*m/g, "").replace(/\\[33m/g, "").replace(/\\[0m/g, "");
    }

    function appendHighlightedLine(span, line) {
      const match = line.match(/total \\$[-0-9.,]+/);
      if (!match) {
        span.textContent = line;
        return;
      }
      span.append(document.createTextNode(line.slice(0, match.index)));
      const budget = document.createElement("span");
      budget.className = "budget-total";
      budget.textContent = match[0];
      span.append(budget);
      span.append(document.createTextNode(line.slice(match.index + match[0].length)));
    }

    function renderLog(text) {
      logEl.replaceChildren();
      if (!text) {
        logEl.textContent = "(empty log)";
        return;
      }
      const fragment = document.createDocumentFragment();
      for (const rawLine of text.split("\\n")) {
        const line = cleanAnsi(rawLine);
        const span = document.createElement("span");
        span.className = `line ${classifyLine(line)}`.trim();
        appendHighlightedLine(span, line);
        fragment.appendChild(span);
      }
      logEl.appendChild(fragment);
    }

    function formatMoney(value) {
      if (!Number.isFinite(value)) {
        return "$--";
      }
      return `$${value.toFixed(4)}`;
    }

    function drawPortfolio(points) {
      const rect = chartCanvas.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      chartCanvas.width = Math.max(320, Math.floor(rect.width * scale));
      chartCanvas.height = Math.max(220, Math.floor(rect.height * scale));
      const ctx = chartCanvas.getContext("2d");
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      const width = chartCanvas.width / scale;
      const height = chartCanvas.height / scale;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#101214";
      ctx.fillRect(0, 0, width, height);

      const pad = { left: 56, right: 18, top: 18, bottom: 40 };
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      ctx.strokeStyle = "#2c3136";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + (plotH * i) / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
      }

      if (!points.length) {
        ctx.fillStyle = "#9ba7b0";
        ctx.font = "13px system-ui, -apple-system, Segoe UI, sans-serif";
        ctx.fillText("No balance rows found.", pad.left, pad.top + 28);
        return;
      }

      const values = points.map((point) => point.total_balance);
      const times = points.map((point) => new Date(point.timestamp_utc).getTime());
      let minValue = Math.min(...values);
      let maxValue = Math.max(...values);
      if (minValue === maxValue) {
        minValue -= 1;
        maxValue += 1;
      }
      const minTime = Math.min(...times);
      const maxTime = Math.max(...times);
      const timeRange = Math.max(1, maxTime - minTime);
      const valueRange = maxValue - minValue;
      const xFor = (time) => pad.left + ((time - minTime) / timeRange) * plotW;
      const yFor = (value) => pad.top + (1 - (value - minValue) / valueRange) * plotH;

      ctx.strokeStyle = "#facc15";
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((point, index) => {
        const x = xFor(new Date(point.timestamp_utc).getTime());
        const y = yFor(point.total_balance);
        if (index === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();

      const last = points[points.length - 1];
      const lastX = xFor(new Date(last.timestamp_utc).getTime());
      const lastY = yFor(last.total_balance);
      ctx.fillStyle = "#facc15";
      ctx.beginPath();
      ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#9ba7b0";
      ctx.font = "12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 4; i += 1) {
        const value = maxValue - (valueRange * i) / 4;
        const y = pad.top + (plotH * i) / 4;
        ctx.fillText(formatMoney(value), pad.left - 8, y);
      }
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(new Date(minTime).toLocaleTimeString(), pad.left, height - pad.bottom + 12);
      ctx.textAlign = "right";
      ctx.fillText(new Date(maxTime).toLocaleTimeString(), width - pad.right, height - pad.bottom + 12);
    }

    async function refreshPortfolioChart() {
      chartStatus.textContent = "loading";
      chartMessage.textContent = "";
      try {
        const response = await fetch("/portfolio-data", { cache: "no-store" });
        const data = await response.json();
        drawPortfolio(data.points || []);
        chartStatus.textContent = `${data.points.length} points`;
        if (data.points.length) {
          const last = data.points[data.points.length - 1];
          chartMessage.textContent =
            `latest ${new Date(last.timestamp_utc).toLocaleString()} total ${formatMoney(last.total_balance)}`;
        } else {
          chartMessage.textContent = `source ${data.path}`;
        }
      } catch (error) {
        chartStatus.textContent = "error";
        chartMessage.textContent = `${error}`;
      }
    }

    function openChart() {
      chartOverlay.hidden = false;
      refreshPortfolioChart();
    }

    function closeChart() {
      chartOverlay.hidden = true;
    }

    portfolioButton.addEventListener("click", openChart);
    chartRefresh.addEventListener("click", refreshPortfolioChart);
    chartClose.addEventListener("click", closeChart);
    chartOverlay.addEventListener("click", (event) => {
      if (event.target === chartOverlay) {
        closeChart();
      }
    });
    window.addEventListener("resize", () => {
      if (!chartOverlay.hidden) {
        refreshPortfolioChart();
      }
    });

    async function refreshLog() {
      try {
        const response = await fetch("/log", { cache: "no-store" });
        const data = await response.json();
        if (data.text !== lastText) {
          const pinnedToBottom =
            firstLoad || window.innerHeight + window.scrollY >= document.body.scrollHeight - 48;
          renderLog(data.text);
          lastText = data.text;
          if (pinnedToBottom) {
            window.scrollTo(0, document.body.scrollHeight);
          }
          firstLoad = false;
        }
        statusEl.textContent = `${data.lines} lines, updated ${new Date().toLocaleTimeString()}`;
      } catch (error) {
        statusEl.textContent = `error: ${error}`;
      }
    }

    refreshLog();
    setInterval(refreshLog, 500);
  </script>
</body>
</html>
"""


def read_log_tail() -> str:
    if not LOG_PATH.exists():
        return ""
    size = LOG_PATH.stat().st_size
    with LOG_PATH.open("rb") as file_obj:
        if size > MAX_BYTES:
            file_obj.seek(size - MAX_BYTES)
            file_obj.readline()
        data = file_obj.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-MAX_LINES:])


def parse_float(value: str | None) -> float | None:
    try:
        number = float(value or "")
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def read_portfolio_points() -> list[dict[str, float | str]]:
    if not BALANCE_CSV_PATH.exists():
        return []
    points: list[dict[str, float | str]] = []
    with BALANCE_CSV_PATH.open(newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for row in reader:
            timestamp = str(row.get("timestamp_utc") or "").strip()
            total_balance = parse_float(row.get("total_balance"))
            if total_balance is None or abs(total_balance) > MAX_REASONABLE_PORTFOLIO_VALUE:
                kalshi_balance = parse_float(row.get("kalshi_balance")) or 0.0
                polymarket_balance = parse_float(row.get("polymarket_balance")) or 0.0
                total_balance = kalshi_balance + polymarket_balance
            if not timestamp or abs(total_balance) > MAX_REASONABLE_PORTFOLIO_VALUE:
                continue
            points.append(
                {
                    "timestamp_utc": timestamp,
                    "total_balance": total_balance,
                }
            )
    return points[-MAX_PORTFOLIO_POINTS:]


@app.get("/")
def index() -> str:
    return render_template_string(PAGE)


@app.get("/log")
def log() -> Response:
    text = read_log_tail()
    return jsonify(
        {
            "path": str(LOG_PATH),
            "lines": 0 if not text else text.count("\n") + (0 if text.endswith("\n") else 1),
            "text": text,
        }
    )


@app.get("/portfolio-data")
def portfolio_data() -> Response:
    points = read_portfolio_points()
    return jsonify(
        {
            "path": str(BALANCE_CSV_PATH),
            "points": points,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8086, debug=False, threaded=True)
