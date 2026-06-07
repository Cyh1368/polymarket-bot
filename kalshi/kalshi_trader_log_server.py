#!/usr/bin/env python3
import csv
import os
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string

import kalshi_trader_stats


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_DIR if (APP_DIR / "kalshi_trader.log").exists() else APP_DIR / "k-0606-research"
LOG_PATH = Path(os.getenv("KALSHI_TRADER_LOG_PATH", str(DEFAULT_DATA_DIR / "kalshi_trader.log"))).expanduser()
TRADES_CSV_PATH = Path(
    os.getenv("KALSHI_TRADER_TRADES_CSV", str(LOG_PATH.with_name("kalshi_trader_trades.csv")))
).expanduser()
PORTFOLIO_CSV_PATH = Path(
    os.getenv("KALSHI_TRADER_PORTFOLIO_CSV", str(LOG_PATH.with_name("kalshi_trader_portfolio.csv")))
).expanduser()
STATS_CSV_PATH = Path(
    os.getenv("KALSHI_TRADER_STATS_CSV", str(LOG_PATH.with_name("kalshi_trader_stats.csv")))
).expanduser()
MAX_BYTES = 250_000
MAX_LINES = 300
PORT = 8099

app = Flask(__name__)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kalshi Trader</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: #101214;
      color: #e8ecef;
    }
    body {
      margin: 0;
      height: 100vh;
      min-height: 100vh;
      background: #101214;
      overflow: hidden;
    }
    .dashboard {
      height: 100vh;
      display: grid;
      grid-template-rows: minmax(220px, 50vh) 6px minmax(180px, 1fr);
      background: #101214;
    }
    .top {
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(260px, 1.35fr) 6px minmax(300px, 0.65fr);
    }
    .chart-panel, .stats-panel, .log-panel {
      min-height: 0;
      min-width: 0;
      background: #101214;
    }
    .chart-panel {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .panel-header, .log-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 10px 14px;
      border-bottom: 1px solid #2c3136;
      background: #171a1d;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .panel-header {
      border-bottom: 1px solid #2c3136;
    }
    .log-header {
      border-bottom: 1px solid #2c3136;
    }
    h1 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
      letter-spacing: 0;
    }
    #status, #chartStatus {
      color: #9ba7b0;
      font-size: 13px;
      white-space: nowrap;
    }
    .range-control {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      border: 1px solid #2c3136;
      background: #101214;
    }
    .range-button {
      width: 42px;
      height: 28px;
      border: 0;
      border-right: 1px solid #2c3136;
      background: transparent;
      color: #9ba7b0;
      font: 650 12px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      cursor: pointer;
    }
    .range-button:last-child {
      border-right: 0;
    }
    .range-button.active {
      background: #26303a;
      color: #eef2f5;
    }
    .chart-wrap {
      position: relative;
      min-height: 0;
      padding: 12px 14px 14px;
    }
    .chart-value {
      position: absolute;
      top: 20px;
      right: 24px;
      z-index: 2;
      padding: 7px 10px;
      border: 1px solid rgba(127, 138, 147, 0.32);
      background: rgba(16, 18, 20, 0.72);
      color: #eef2f5;
      backdrop-filter: blur(8px);
      font: 720 18px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      line-height: 1;
      pointer-events: none;
    }
    #portfolioChart {
      width: 100%;
      height: 100%;
      display: block;
    }
    .stats-panel {
      display: flex;
      flex-direction: column;
      justify-content: center;
      padding: 22px 26px;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .counts-stack {
      width: min(100%, 520px);
    }
    .count-labels, .counts-display {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto minmax(0, 1fr);
      align-items: center;
      column-gap: 8px;
    }
    .count-labels {
      margin-bottom: 8px;
      color: #7f8a93;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .count-labels span {
      text-align: center;
    }
    .counts-display {
      font-size: clamp(38px, 5vw, 76px);
      line-height: 1;
      font-weight: 760;
      letter-spacing: 0;
      text-align: center;
    }
    .success { color: #4ade80; }
    .unsuccess { color: #f87171; }
    .skipped { color: #9ba7b0; }
    .slash { color: #3d454d; padding: 0 8px; }
    .stat-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px 24px;
      margin-top: 28px;
    }
    .stat-label {
      color: #7f8a93;
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
    }
    .stat-value {
      margin-top: 4px;
      color: #eef2f5;
      font-size: clamp(22px, 2.2vw, 34px);
      font-weight: 720;
      line-height: 1.05;
      letter-spacing: 0;
    }
    .stat-value.small {
      font-size: clamp(18px, 1.7vw, 28px);
    }
    .latest-contract {
      margin-top: 18px;
      padding: 11px 12px;
      border: 1px solid #2c3136;
      background: #15191d;
    }
    .latest-contract-title {
      color: #7f8a93;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .latest-contract-main {
      margin-top: 5px;
      color: #eef2f5;
      font-size: clamp(15px, 1.45vw, 22px);
      font-weight: 720;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }
    .latest-contract-sub {
      margin-top: 4px;
      color: #9ba7b0;
      font-size: 12px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .positive { color: #4ade80; }
    .negative { color: #f87171; }
    .log-panel {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .resizer {
      background: #171a1d;
      position: relative;
      z-index: 4;
    }
    .resizer::after {
      content: "";
      position: absolute;
      inset: 0;
      background: transparent;
      transition: background 120ms ease;
    }
    .resizer:hover::after, .resizer.active::after {
      background: rgba(103, 232, 249, 0.18);
    }
    .v-resizer {
      cursor: col-resize;
      border-left: 1px solid #2c3136;
      border-right: 1px solid #2c3136;
    }
    .h-resizer {
      cursor: row-resize;
      border-top: 1px solid #2c3136;
      border-bottom: 1px solid #2c3136;
    }
    body.resizing {
      user-select: none;
    }
    pre {
      flex: 1;
      min-height: 0;
      margin: 0;
      padding: 12px 14px 28px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.45;
      font-size: 13px;
      color: #eef2f5;
    }
    @media (max-width: 760px) {
      body {
        height: 100dvh;
        min-height: 100dvh;
      }
      .dashboard {
        height: 100dvh;
        grid-template-rows: minmax(0, 1fr);
      }
      .top {
        height: 100%;
        grid-template-columns: 1fr;
        grid-template-rows: minmax(220px, 56dvh) minmax(0, 1fr);
      }
      .chart-panel {
        border-right: 0;
      }
      .v-resizer, .h-resizer, .log-panel {
        display: none;
      }
      .panel-header {
        gap: 8px;
        padding: 8px 10px;
      }
      h1 {
        font-size: 14px;
      }
      .chart-wrap {
        padding: 8px 10px 10px;
      }
      .stats-panel {
        justify-content: flex-start;
        overflow: hidden;
        padding: 14px 14px 16px;
      }
      .counts-stack {
        width: 100%;
      }
      .count-labels {
        margin-bottom: 5px;
        font-size: 10px;
      }
      .counts-display {
        font-size: clamp(30px, 12vw, 46px);
      }
      .slash {
        padding: 0 4px;
      }
      .stat-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px 12px;
        margin-top: 14px;
      }
      .stat-label {
        font-size: 10px;
      }
      .stat-value {
        overflow-wrap: anywhere;
        font-size: clamp(18px, 6vw, 24px);
      }
      .stat-value.small {
        font-size: clamp(15px, 4.8vw, 20px);
      }
      .latest-contract {
        margin-top: 12px;
        padding: 9px 10px;
      }
      .latest-contract-main {
        font-size: clamp(14px, 4.2vw, 18px);
      }
      .latest-contract-sub {
        font-size: 11px;
      }
      .range-button {
        width: 36px;
        height: 26px;
        font-size: 11px;
      }
      .chart-value {
        top: 14px;
        right: 16px;
        padding: 6px 8px;
        font-size: 15px;
      }
    }
    .line {
      display: block;
      min-height: 1.45em;
    }
    .contract { font-weight: 700; color: #e8ecef; }
    .balance { color: #facc15; }
    .status-line { color: #67e8f9; font-weight: 700; }
    .order-filled, .order-dry { color: #4ade80; font-weight: 700; }
    .order-skip, .retry { color: #9ba7b0; }
    .outcome { color: #c4b5fd; font-weight: 700; }
    .stop-loss, .error { color: #f87171; font-weight: 700; }
    .count {
      color: #facc15;
      font-weight: 700;
    }
  </style>
</head>
<body>
  <main class="dashboard">
    <section class="top">
      <section class="chart-panel">
        <div class="panel-header">
          <h1>Portfolio</h1>
          <div class="range-control" role="group" aria-label="Portfolio time range">
            <button class="range-button active" data-range="4h">4h</button>
            <button class="range-button" data-range="1d">1D</button>
            <button class="range-button" data-range="3d">3D</button>
            <button class="range-button" data-range="1w">1W</button>
            <button class="range-button" data-range="all">All</button>
          </div>
        </div>
        <div class="chart-wrap">
          <div class="chart-value" id="portfolioValue">--</div>
          <canvas id="portfolioChart"></canvas>
        </div>
      </section>
      <div class="resizer v-resizer" id="verticalResizer" role="separator" aria-orientation="vertical"></div>
      <section class="stats-panel">
        <div class="counts-stack">
          <div class="count-labels">
            <span>success</span><span></span><span>failed</span><span></span><span>skipped</span>
          </div>
          <div class="counts-display">
            <span class="success" id="countS">0</span><span class="slash">/</span><span class="unsuccess" id="countU">0</span><span class="slash">/</span><span class="skipped" id="countK">0</span>
          </div>
        </div>
        <div class="stat-grid">
          <div>
            <div class="stat-label">Portfolio</div>
            <div class="stat-value" id="portfolioStat">--</div>
          </div>
          <div>
            <div class="stat-label">Avg Price</div>
            <div class="stat-value" id="avgPrice">--</div>
          </div>
          <div>
            <div class="stat-label">Success</div>
            <div class="stat-value" id="successPct">--</div>
          </div>
          <div>
            <div class="stat-label">p-value</div>
            <div class="stat-value small" id="pValue">--</div>
          </div>
          <div>
            <div class="stat-label">PnL Today</div>
            <div class="stat-value small" id="pnlToday">--</div>
          </div>
        </div>
        <div class="latest-contract">
          <div class="latest-contract-title">Latest Contract</div>
          <div class="latest-contract-main" id="latestContractMain">--</div>
          <div class="latest-contract-sub" id="latestContractSub">waiting for trade data</div>
        </div>
      </section>
    </section>
    <div class="resizer h-resizer" id="horizontalResizer" role="separator" aria-orientation="horizontal"></div>
    <section class="log-panel">
      <div class="log-header">
        <h1>kalshi_trader.log</h1>
        <div id="status">loading</div>
      </div>
      <pre id="log"></pre>
    </section>
  </main>
  <script>
    const logEl = document.getElementById("log");
    const statusEl = document.getElementById("status");
    const dashboardEl = document.querySelector(".dashboard");
    const topEl = document.querySelector(".top");
    const chartCanvas = document.getElementById("portfolioChart");
    const portfolioValueEl = document.getElementById("portfolioValue");
    const rangeButtons = Array.from(document.querySelectorAll(".range-button"));
    const verticalResizer = document.getElementById("verticalResizer");
    const horizontalResizer = document.getElementById("horizontalResizer");
    let statsRows = [];
    let selectedRange = "4h";
    let lastText = "";
    let firstLoad = true;

    function cleanAnsi(line) {
      return line.replace(/\\x1b\\[[0-9;]*m/g, "");
    }

    function classifyLine(line) {
      if (line.includes("STOP_LOSS")) return "stop-loss";
      if (line.includes("ERROR") || line.includes("FAILED") || line.includes("FATAL")) return "error";
      if (line.includes("CONTRACT ")) return "contract";
      if (line.includes("BALANCE")) return "balance";
      if (line.includes("STATUS T=")) return "status-line";
      if (line.includes("ORDER FILLED")) return "order-filled";
      if (line.includes("ORDER DRY_RUN")) return "order-dry";
      if (line.includes("ORDER SKIP")) return "order-skip";
      if (line.includes("ORDER RETRY")) return "retry";
      if (line.includes("OUTCOME ")) return "outcome";
      return "";
    }

    function appendHighlightedLine(span, line) {
      const match = line.match(/counts S=\\d+ U=\\d+ K=\\d+/);
      if (!match) {
        span.textContent = line;
        return;
      }
      span.append(document.createTextNode(line.slice(0, match.index)));
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = match[0];
      span.append(count);
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

    async function refreshLog() {
      try {
        const response = await fetch("/log", { cache: "no-store" });
        const data = await response.json();
        if (data.text !== lastText) {
          const pinnedToBottom = firstLoad || logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 48;
          renderLog(data.text);
          lastText = data.text;
          if (pinnedToBottom) {
            logEl.scrollTop = logEl.scrollHeight;
          }
          firstLoad = false;
        }
        statusEl.textContent = `${data.lines} lines, updated ${new Date().toLocaleTimeString()}`;
      } catch (error) {
        statusEl.textContent = `error: ${error}`;
      }
    }

    function asNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function fmtPct(value, digits = 1) {
      const number = asNumber(value);
      return number === null ? "--" : `${(number * 100).toFixed(digits)}%`;
    }

    function fmtCents(value) {
      const number = asNumber(value);
      return number === null ? "--" : `${(number * 100).toFixed(1)}c`;
    }

    function fmtMoney(value) {
      const number = asNumber(value);
      if (number === null) return "--";
      const sign = number < 0 ? "-" : "";
      return `${sign}$${Math.abs(number).toFixed(2)}`;
    }

    function fmtPValue(value) {
      const number = asNumber(value);
      if (number === null) return "--";
      if (number < 0.0001) return number.toExponential(2);
      return number.toFixed(4);
    }

    function fmtAxisDate(timestamp) {
      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime())) return "";
      return date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    }

    function fmtShortTime(value) {
      if (!value) return "--";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }

    function fmtShortDateTime(value) {
      if (!value) return "--";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    }

    function latestRow(rows) {
      for (let i = rows.length - 1; i >= 0; i -= 1) {
        if (rows[i]) return rows[i];
      }
      return null;
    }

    function renderStats(latest) {
      if (!latest) return;
      document.getElementById("countS").textContent = latest.successful_count || "0";
      document.getElementById("countU").textContent = latest.unsuccessful_count || "0";
      document.getElementById("countK").textContent = latest.skipped_count || "0";
      document.getElementById("portfolioStat").textContent = fmtMoney(latest.portfolio_value);
      document.getElementById("avgPrice").textContent = fmtCents(latest.avg_contract_price);
      document.getElementById("successPct").textContent = fmtPct(latest.success_percentage, 1);
      document.getElementById("pValue").textContent = fmtPValue(latest.p_value);
      portfolioValueEl.textContent = fmtMoney(latest.portfolio_value);
      const pnlDollars = asNumber(latest.portfolio_pnl_today_dollars);
      const pnlPct = asNumber(latest.portfolio_pnl_today_percent);
      const pnlEl = document.getElementById("pnlToday");
      pnlEl.textContent = pnlDollars === null ? "--" : `${fmtMoney(pnlDollars)} / ${fmtPct(pnlPct, 1)}`;
      pnlEl.className = `stat-value small ${pnlDollars > 0 ? "positive" : pnlDollars < 0 ? "negative" : ""}`.trim();
    }

    function renderLatestContract(contract) {
      const mainEl = document.getElementById("latestContractMain");
      const subEl = document.getElementById("latestContractSub");
      if (!contract || !contract.contract_id) {
        mainEl.textContent = "--";
        subEl.textContent = "waiting for trade data";
        return;
      }
      const closeTime = fmtShortTime(contract.close_time);
      const actual = contract.actual_side || "pending";
      const prediction = contract.prediction_side || "none";
      const resultText = contract.correct === "1" ? "correct" : contract.correct === "0" ? "wrong" : "open";
      mainEl.textContent = `${closeTime} · actual ${actual} · pred ${prediction}`;
      const probability = contract.prediction_probability ? ` @ ${fmtPct(contract.prediction_probability, 1)}` : "";
      const status = contract.order_status || "unknown";
      const updated = contract.outcome_timestamp_utc || contract.prediction_timestamp_utc || contract.timestamp_utc || "";
      subEl.textContent = `${contract.contract_id} · ${status}${probability} · ${resultText} · updated ${fmtShortDateTime(updated)}`;
    }

    function visiblePoints() {
      const points = statsRows
        .map((row) => ({ t: Date.parse(row.timestamp_utc), y: asNumber(row.portfolio_value) }))
        .filter((point) => Number.isFinite(point.t) && point.y !== null);
      if (!points.length || selectedRange === "all") return points;
      const latest = points[points.length - 1].t;
      const ranges = { "4h": 4 * 3600e3, "1d": 24 * 3600e3, "3d": 3 * 24 * 3600e3, "1w": 7 * 24 * 3600e3 };
      const cutoff = latest - ranges[selectedRange];
      return points.filter((point) => point.t >= cutoff);
    }

    function drawChart() {
      const rect = chartCanvas.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      chartCanvas.width = Math.max(1, Math.floor(rect.width * scale));
      chartCanvas.height = Math.max(1, Math.floor(rect.height * scale));
      const ctx = chartCanvas.getContext("2d");
      ctx.scale(scale, scale);
      const width = rect.width;
      const height = rect.height;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#101214";
      ctx.fillRect(0, 0, width, height);

      const points = visiblePoints();
      const pad = { left: 52, right: 16, top: 16, bottom: 44 };
      const plotW = Math.max(1, width - pad.left - pad.right);
      const plotH = Math.max(1, height - pad.top - pad.bottom);
      ctx.strokeStyle = "#242b31";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + (plotH * i) / 4;
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
      }
      ctx.stroke();

      if (!points.length) {
        portfolioValueEl.textContent = "--";
        ctx.fillStyle = "#7f8a93";
        ctx.font = "13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
        ctx.fillText("No portfolio data", pad.left, pad.top + 22);
        return;
      }

      const minT = Math.min(...points.map((p) => p.t));
      const maxT = Math.max(...points.map((p) => p.t));
      let minY = Math.min(...points.map((p) => p.y));
      let maxY = Math.max(...points.map((p) => p.y));
      if (minY === maxY) {
        minY -= 1;
        maxY += 1;
      }
      const yPad = (maxY - minY) * 0.08;
      minY -= yPad;
      maxY += yPad;
      const xFor = (t) => pad.left + ((t - minT) / Math.max(1, maxT - minT)) * plotW;
      const yFor = (y) => pad.top + (1 - (y - minY) / Math.max(0.000001, maxY - minY)) * plotH;

      ctx.fillStyle = "#7f8a93";
      ctx.font = "12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
      ctx.textAlign = "left";
      ctx.fillText(`$${maxY.toFixed(2)}`, 4, pad.top + 4);
      ctx.fillText(`$${minY.toFixed(2)}`, 4, pad.top + plotH);

      const tickCount = width < 520 ? 3 : 4;
      ctx.strokeStyle = "#1d2329";
      ctx.fillStyle = "#7f8a93";
      ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
      for (let i = 0; i < tickCount; i += 1) {
        const ratio = tickCount === 1 ? 0 : i / (tickCount - 1);
        const t = minT + (maxT - minT) * ratio;
        const x = xFor(t);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        ctx.textAlign = i === 0 ? "left" : i === tickCount - 1 ? "right" : "center";
        ctx.fillText(fmtAxisDate(t), x, height - 12);
      }

      ctx.strokeStyle = "#67e8f9";
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((point, index) => {
        const x = xFor(point.t);
        const y = yFor(point.y);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();

      const last = points[points.length - 1];
      ctx.fillStyle = "#67e8f9";
      ctx.beginPath();
      ctx.arc(xFor(last.t), yFor(last.y), 3, 0, Math.PI * 2);
      ctx.fill();
      portfolioValueEl.textContent = fmtMoney(last.y);
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function isMobileLayout() {
      return window.matchMedia("(max-width: 760px)").matches;
    }

    function refreshLogIfVisible() {
      if (!isMobileLayout()) {
        refreshLog();
      }
    }

    function startResize(handle, onMove) {
      handle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        handle.classList.add("active");
        document.body.classList.add("resizing");
        handle.setPointerCapture(event.pointerId);
        const move = (moveEvent) => {
          onMove(moveEvent);
          requestAnimationFrame(drawChart);
        };
        const stop = () => {
          handle.classList.remove("active");
          document.body.classList.remove("resizing");
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", stop);
          handle.removeEventListener("pointercancel", stop);
          drawChart();
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", stop);
        handle.addEventListener("pointercancel", stop);
      });
    }

    startResize(horizontalResizer, (event) => {
      const rect = dashboardEl.getBoundingClientRect();
      const topHeight = clamp(event.clientY - rect.top, 220, rect.height - 180);
      dashboardEl.style.gridTemplateRows = `${topHeight}px 6px minmax(180px, 1fr)`;
    });

    startResize(verticalResizer, (event) => {
      const rect = topEl.getBoundingClientRect();
      if (isMobileLayout()) {
        const chartHeight = clamp(event.clientY - rect.top, 160, rect.height - 160);
        topEl.style.gridTemplateRows = `${chartHeight}px 6px minmax(160px, 1fr)`;
        topEl.style.gridTemplateColumns = "1fr";
        return;
      }
      const chartWidth = clamp(event.clientX - rect.left, 260, rect.width - 300);
      topEl.style.gridTemplateColumns = `${chartWidth}px 6px minmax(300px, 1fr)`;
      topEl.style.gridTemplateRows = "";
    });

    async function refreshStats() {
      try {
        const response = await fetch("/stats", { cache: "no-store" });
        const data = await response.json();
        statsRows = data.rows || [];
        renderStats(data.latest || latestRow(statsRows));
        renderLatestContract(data.latest_contract || null);
        drawChart();
      } catch (error) {
        console.error(error);
      }
    }

    rangeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        selectedRange = button.dataset.range;
        rangeButtons.forEach((item) => item.classList.toggle("active", item === button));
        drawChart();
      });
    });

    window.addEventListener("resize", () => {
      if (isMobileLayout()) {
        dashboardEl.style.gridTemplateRows = "";
        topEl.style.gridTemplateColumns = "1fr";
        topEl.style.gridTemplateRows = "";
      } else {
        if (topEl.style.gridTemplateColumns === "1fr") {
          topEl.style.gridTemplateColumns = "";
        }
        topEl.style.gridTemplateRows = "";
      }
      drawChart();
    });

    refreshLogIfVisible();
    refreshStats();
    setInterval(refreshLogIfVisible, 500);
    setInterval(refreshStats, 2000);
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


def refresh_stats() -> None:
    kalshi_trader_stats.refresh_stats_csv(PORTFOLIO_CSV_PATH, TRADES_CSV_PATH, STATS_CSV_PATH)


def read_stats_rows() -> list[dict[str, str]]:
    refresh_stats()
    if not STATS_CSV_PATH.exists():
        return []
    with STATS_CSV_PATH.open(newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


def read_trade_rows() -> list[dict[str, str]]:
    if not TRADES_CSV_PATH.exists():
        return []
    with TRADES_CSV_PATH.open(newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


def latest_contract_status() -> dict[str, str]:
    rows = [row for row in read_trade_rows() if row.get("contract_id")]
    if not rows:
        return {}

    latest_row = max(
        rows,
        key=lambda row: (
            row.get("close_time", ""),
            row.get("timestamp_utc", ""),
            row.get("contract_id", ""),
        ),
    )
    key = (latest_row.get("contract_id", ""), latest_row.get("close_time", ""))
    contract_rows = [
        row
        for row in rows
        if (row.get("contract_id", ""), row.get("close_time", "")) == key
    ]
    prediction_row = next(
        (
            row
            for row in reversed(contract_rows)
            if row.get("selected_side") or row.get("order_status")
        ),
        {},
    )
    outcome_row = next((row for row in reversed(contract_rows) if row.get("event") == "outcome"), {})
    actual_side = (
        outcome_row.get("actual_side")
        or outcome_row.get("official_result", "").upper()
        or ""
    )
    return {
        "contract_id": key[0],
        "close_time": key[1],
        "timestamp_utc": latest_row.get("timestamp_utc", ""),
        "prediction_timestamp_utc": prediction_row.get("timestamp_utc", ""),
        "outcome_timestamp_utc": outcome_row.get("timestamp_utc", ""),
        "prediction_side": prediction_row.get("selected_side", ""),
        "prediction_probability": prediction_row.get("selected_probability", ""),
        "order_status": prediction_row.get("order_status", ""),
        "actual_side": actual_side,
        "actual_label": outcome_row.get("actual_label", ""),
        "correct": outcome_row.get("correct", ""),
        "official_result": outcome_row.get("official_result", ""),
        "official_status": outcome_row.get("official_status", ""),
        "official_outcome_source": outcome_row.get("official_outcome_source", ""),
    }


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
            "port": PORT,
        }
    )


@app.get("/stats")
def stats() -> Response:
    rows = read_stats_rows()
    latest = rows[-1] if rows else {}
    latest_contract = latest_contract_status()
    return jsonify(
        {
            "log_path": str(LOG_PATH),
            "trades_path": str(TRADES_CSV_PATH),
            "portfolio_path": str(PORTFOLIO_CSV_PATH),
            "stats_path": str(STATS_CSV_PATH),
            "rows": rows,
            "latest": latest,
            "latest_contract": latest_contract,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
