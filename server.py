import threading
import http.server
import os
import json

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "SYMBOL":    "RIVERUSDT",
    "LEVERAGE":  50,
    "RISK_PCT":  0.30,
    "SL_PCT":    0.0075,
    "TP1_PCT":   0.014,
    "TP2_PCT":   0.030,
    "SIDE_MODE": "BOTH"   # BOTH | LONG_ONLY | SHORT_ONLY
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()

def save_config(data):
    merged = {**DEFAULT_CONFIG, **data}
    with open(CONFIG_FILE, "w") as f:
        json.dump(merged, f, indent=2)
    return merged

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence request logs

    def do_GET(self):
        if self.path == "/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(load_config()).encode())
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/config":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            saved  = save_config(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "config": saved}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

def start_dashboard():
    port = int(os.environ.get("PORT", 8080))
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard running at port {port}")
    server.serve_forever()

def run_dashboard_thread():
    t = threading.Thread(target=start_dashboard, daemon=True)
    t.start()
