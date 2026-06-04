#!/usr/bin/env python3
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "kalshi_trader.log"
MAX_BYTES = 250_000
MAX_LINES = 300
PORT = 8099

app = Flask(__name__)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kalshi Trader Log</title>
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
  <header>
    <h1>kalshi_trader.log</h1>
    <div id="status">loading</div>
  </header>
  <pre id="log"></pre>
  <script>
    const logEl = document.getElementById("log");
    const statusEl = document.getElementById("status");
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
