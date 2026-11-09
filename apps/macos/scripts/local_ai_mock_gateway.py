"""Synthetic loopback gateway for LocalAITest; never calls a model provider."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != "/v1/analyze" or self.headers.get("Authorization") != "Bearer synthetic-e2e-gateway-token":
            self.send_error(403)
            return
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 400000:
            self.send_error(400)
            return
        request = json.loads(self.rfile.read(size))
        evidence = request["evidence"]
        assert evidence and "合成 AI 主流程测试" in evidence[0]["text"]
        citations = [evidence[0]["id"]]
        suggestion = None
        if request["kind"] == "organize":
            domain = next(item for item in request["domains"] if item["name"] == "合成目标领域")
            project = next(item for item in request["projects"] if item["name"] == "合成目标项目")
            suggestion = {"domain_id": domain["id"], "project_ids": [project["id"]], "reason": "合成整理建议", "citations": citations}
        response = {"provider": "synthetic", "model": "mock-no-fees", "prompt_version": "synthetic-v1",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                    "result": {"summary": "合成 AI 结果（非真实模型）", "summary_citations": citations,
                               "insights": [{"text": "合成要点", "citations": citations}],
                               "organizing_suggestion": suggestion, "warnings": []}}
        if request.get("contract_version", 1) >= 2:
            response["result"].update({
                "contract_version": request["contract_version"],
                "overview": {"text": "合成知识库本地保存流程", "citations": citations},
                "limitations": [{"text": "仅用于合成测试", "citations": citations}],
                "tag_suggestions": [
                    {"tag_id": None, "name": "本地知识管理", "dimension": "topic", "reason": "合成主题", "citations": citations},
                    {"tag_id": None, "name": "合成实体", "dimension": "entity", "reason": "合成实体依据", "citations": citations}]
            })
        if request.get("phase") == "segment":
            response["result"]["tag_suggestions"] = []
            response["result"]["organizing_suggestion"] = None
        for tag in response["result"].get("tag_suggestions", []):
            existing = next((item for item in request.get("tags", []) if item["name"] == tag["name"] and item["dimension"] == tag["dimension"]), None)
            if existing:
                tag["tag_id"] = existing["id"]
        rejected = {(item["dimension"], item["name"].casefold()) for item in request.get("rejected_tags", [])}
        response["result"]["tag_suggestions"] = [tag for tag in response["result"].get("tag_suggestions", [])
            if (tag["dimension"], tag["name"].casefold()) not in rejected]
        calls = ready.with_name("gateway-calls.json")
        previous = json.loads(calls.read_text()) if calls.exists() else []
        calls.write_text(json.dumps(previous + [request["request_id"]]))
        data = json.dumps(response, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = HTTPServer(("127.0.0.1", 0), Gateway)
ready = Path(sys.argv[1])
ready.write_text(json.dumps({"port": server.server_port}))
os.chmod(ready, 0o600)
server.serve_forever()
