import threading
import http.server
import os

def start_dashboard():
    """Serve dashboard.html on port 8080"""
    port = int(os.environ.get("PORT", 8080))
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("0.0.0.0", port), handler)
    print(f"Dashboard running at port {port}")
    server.serve_forever()

def run_dashboard_thread():
    t = threading.Thread(target=start_dashboard, daemon=True)
    t.start()
