"""Synthetic loopback RAG gateway; no external network or model provider calls."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

ready = Path(sys.argv[1])
count_file = ready.with_suffix(".count")
count_file.write_text("0")
os.chmod(count_file, 0o600)


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != "/v1/answer" or self.headers.get("Authorization") != "Bearer synthetic-rag-gateway-token":
            self.send_error(403)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 100000:
            self.send_error(400)
            return
        request = json.loads(self.rfile.read(size))
        evidence = request["evidence"]
        assert evidence and "RAG合成检索" in evidence[0]["text"]
        count_file.write_text(str(int(count_file.read_text()) + 1))
        response = {
            "request_id": request["request_id"], "provider": "synthetic", "model": "mock-no-fees",
            "prompt_version": "synthetic-rag-v1", "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            "result": {"answer": f"合成回答：知识问答原文留在本机。[{evidence[0]['id']}]", "citations": [evidence[0]["id"]],
                       "insufficient_evidence": False, "warnings": []},
        }
        data = json.dumps(response, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = HTTPServer(("127.0.0.1", 0), Gateway)
ready.write_text(json.dumps({"port": server.server_port}))
os.chmod(ready, 0o600)
server.serve_forever()
