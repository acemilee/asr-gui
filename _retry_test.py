"""验证真实 yt-dlp 重试链路：前两次传输被中途掐断，看能否带退避续传成功。"""
import http.server
import re
import socketserver
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, r"E:\AI_dev\video-text\asr_gui")
import sources  # noqa: E402

ROOT = Path(r"E:\AI_dev\video-text\_local\downloads\retrytest")
ROOT.mkdir(parents=True, exist_ok=True)
MEDIA = Path(r"E:\AI_dev\video-text\_local\downloads\speech-speech.wav")
PAYLOAD = MEDIA.read_bytes()
state = {"hits": 0, "broken": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _ranges(self):
        match = re.search(r"bytes=(\d+)-", self.headers.get("Range", ""))
        return int(match.group(1)) if match else 0

    def do_GET(self):
        state["hits"] += 1
        start = self._ranges()
        chunk = PAYLOAD[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        self.end_headers()
        if state["broken"] < 2:
            # 传一半就断（模拟 10054 / 传输中途被掐）
            state["broken"] += 1
            self.wfile.write(chunk[: len(chunk) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(chunk)

    def do_HEAD(self):
        state["hits"] += 1
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


srv = Server(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

url = f"http://127.0.0.1:{port}/retry-test.wav"
print("url", url)
start_time = time.time()
result = sources.download_audio(url, ROOT, log=lambda text: print("  log:", text))
elapsed = time.time() - start_time
print("ok =", result.ok, "| error =", (result.error[:120] if result.error else ""))
print("size =", result.path.stat().st_size if result.path else 0, "expected", len(PAYLOAD))
print("hits =", state["hits"], "broken =", state["broken"], "elapsed = %.2fs" % elapsed)
srv.shutdown()

ok = (
    result.ok
    and result.path is not None
    and result.path.stat().st_size == len(PAYLOAD)
    and state["broken"] == 2
    and elapsed >= 1.0
)
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
