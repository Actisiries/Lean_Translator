from pathlib import Path
import sys
import threading
import urllib.request
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from http.server import ThreadingHTTPServer
from paper2lean.webapp import Handler


def test_independent_health_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/health") as response:
            data = json.loads(response.read().decode())
        assert data["application"] == "paper2lean"
    finally:
        server.shutdown()
        server.server_close()
