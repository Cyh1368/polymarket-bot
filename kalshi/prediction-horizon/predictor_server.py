#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "predictor_log.txt"
PREDICTIONS_PATH = APP_DIR / "predictions.csv"
TRUTH_DIR = APP_DIR / "truth_tables"
MAX_BYTES = 300_000
MAX_LINES = 400

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Prediction Horizon Log</title>
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
    nav {
      display: flex;
      gap: 12px;
      font-size: 13px;
    }
    a {
      color: #67e8f9;
      text-decoration: none;
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
    .model { color: #67e8f9; font-weight: 700; }
    .predict { color: #facc15; font-weight: 700; }
    .truth { color: #4ade80; font-weight: 700; }
    .fatal { color: #f87171; font-weight: 700; }
  </style>
</head>
<body>
  <header>
    <h1>prediction-horizon / predictor_log.txt</h1>
    <nav>
      <a href="/predictions">predictions.csv</a>
      <a href="/truth">truth tables</a>
    </nav>
    <div id="status">loading</div>
  </header>
  <pre id="log"></pre>
  <script>
    const logEl = document.getElementById("log");
    const statusEl = document.getElementById("status");
    let lastText = "";
    let firstLoad = true;

    function spanFor(line) {
      const span = document.createElement("span");
      span.textContent = line + "\\n";
      if (line.includes("MODEL_FEATURES")) span.className = "model";
      if (line.includes("PREDICT ")) span.className = "predict";
      if (line.includes("TRUTH_TABLE") || line.includes("OUTCOME") || line.includes("EVAL ")) span.className = "truth";
      if (line.includes("FATAL") || line.includes("ERROR")) span.className = "fatal";
      return span;
    }

    async function refresh() {
      try {
        const response = await fetch("/log", { cache: "no-store" });
        const text = await response.text();
        if (text !== lastText) {
          const shouldStick = firstLoad || (logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 16);
          logEl.replaceChildren();
          const frag = document.createDocumentFragment();
          for (const line of text.split("\\n")) frag.appendChild(spanFor(line));
          logEl.appendChild(frag);
          if (shouldStick) logEl.scrollTop = logEl.scrollHeight;
          lastText = text;
          firstLoad = false;
        }
        statusEl.textContent = new Date().toLocaleTimeString();
      } catch (err) {
        statusEl.textContent = String(err);
      }
    }
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


def read_tail(path: Path) -> str:
    if not path.exists():
        return "(log file does not exist yet)"
    size = path.stat().st_size
    with path.open("rb") as file_obj:
        if size > MAX_BYTES:
            file_obj.seek(size - MAX_BYTES)
        data = file_obj.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-MAX_LINES:])


@app.get("/")
def index() -> str:
    return render_template_string(PAGE)


@app.get("/log")
def log() -> Response:
    return Response(read_tail(LOG_PATH), mimetype="text/plain; charset=utf-8")


@app.get("/predictions")
def predictions() -> Response:
    return Response(read_tail(PREDICTIONS_PATH), mimetype="text/plain; charset=utf-8")


@app.get("/truth")
def truth() -> Response:
    rows = []
    if TRUTH_DIR.exists():
        for path in sorted(TRUTH_DIR.glob("*")):
            if path.is_file():
                rows.append({"name": path.name, "size": path.stat().st_size})
    return jsonify(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve prediction-horizon predictor log.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8083)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
