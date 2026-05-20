#!/usr/bin/env python3
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "trader_log.txt"
MAX_BYTES = 250_000
MAX_LINES = 200

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
    .contract-start {
      font-weight: 700;
    }
    .trade-executed {
      color: #4ade80;
    }
    .position-exited {
      color: #f87171;
    }
    .budget-total {
      color: #facc15;
      font-weight: 700;
    }
  </style>
</head>
<body>
  <header>
    <h1>trader_log.txt</h1>
    <div id="status">loading</div>
  </header>
  <pre id="log"></pre>
  <script>
    const logEl = document.getElementById("log");
    const statusEl = document.getElementById("status");
    let lastText = "";

    let firstLoad = true;

    function classifyLine(line) {
      if (line.includes("CONTRACT ")) {
        return "contract-start";
      }
      if (line.includes("ENTRY SKIP") || line.includes("CHECK SKIP") || line.includes("HOLD continue")) {
        return "entry-skip";
      }
      if (line.includes("TRADED ") || line.includes("DRY RUN would place")) {
        return "trade-executed";
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
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8085, debug=False, threaded=True)
