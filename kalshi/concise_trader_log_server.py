#!/usr/bin/env python3
import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


APP_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = APP_DIR / "concise_trader_log.txt"
MAX_BYTES = 250_000
MAX_LINES = 300

log_path = DEFAULT_LOG_PATH


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Concise Trader Log</title>
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
    .balance {
      color: #93c5fd;
    }
    .contract-start {
      font-weight: 700;
    }
    .trade-executed {
      color: #4ade80;
      font-weight: 700;
    }
    .position {
      color: #facc15;
    }
    .position-exited {
      color: #f87171;
      font-weight: 700;
    }
  </style>
</head>
<body>
  <header>
    <h1>concise_trader_log.txt</h1>
    <div id="status">loading</div>
  </header>
  <pre id="log"></pre>
  <script>
    const logEl = document.getElementById("log");
    const statusEl = document.getElementById("status");
    let lastText = "";
    let firstLoad = true;

    function classifyLine(line) {
      if (line.includes("BALANCE ")) {
        return "balance";
      }
      if (line.includes("CONTRACT ")) {
        return "contract-start";
      }
      if (line.includes("TRADED ") || line.includes("DRY RUN would place")) {
        return "trade-executed";
      }
      if (line.includes("POSITION REVIEW") || line.includes("POSITION CLEAR")) {
        return "position";
      }
      if (line.includes("EXITED ") || line.includes("EXIT FAILED") || line.includes("EXIT_REVIEW") || line.includes("FATAL ")) {
        return "position-exited";
      }
      return "";
    }

    function renderLog(text) {
      logEl.replaceChildren();
      if (!text) {
        logEl.textContent = "(empty log)";
        return;
      }
      const fragment = document.createDocumentFragment();
      for (const line of text.split("\\n")) {
        const span = document.createElement("span");
        span.className = `line ${classifyLine(line)}`.trim();
        span.textContent = line;
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
    if not log_path.exists():
        return ""
    size = log_path.stat().st_size
    with log_path.open("rb") as file_obj:
        if size > MAX_BYTES:
            file_obj.seek(size - MAX_BYTES)
            file_obj.readline()
        data = file_obj.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-MAX_LINES:])


class ConciseLogHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def handle_request(self, send_body: bool) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_text(PAGE, "text/html; charset=utf-8", send_body=send_body)
            return
        if path == "/log":
            text = read_log_tail()
            payload = {
                "path": str(log_path),
                "lines": 0 if not text else text.count("\n") + (0 if text.endswith("\n") else 1),
                "text": text,
            }
            self.send_json(payload, send_body=send_body)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_text(self, text: str, content_type: str, send_body: bool = True) -> None:
        data = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def send_json(self, payload: dict[str, object], send_body: bool = True) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve concise_trader_log.txt as a live web log.")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8096, help="Port to listen on.")
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH, help="Log file to display.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    log_path = args.log_path.expanduser().resolve()
    server = ThreadingHTTPServer((args.host, args.port), ConciseLogHandler)
    print(f"Serving {log_path} at http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
