"""Minimal stand-in for an Ollama server, for testing model resolution.

MODELS env: comma list of installed models; CAPS=0 simulates an old server
without /api/show capabilities.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = [m for m in os.environ.get("MODELS", "qwen2.5vl:7b,llama3.1:8b,nomic-embed-text:latest").split(",") if m]
CAPS = os.environ.get("CAPS", "1") == "1"


def caps(name):
    if "embed" in name:
        return ["embedding"]
    if "vl" in name or "vision" in name or "llava" in name:
        return ["completion", "vision"]
    return ["completion", "tools"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": m, "model": m, "size": 4_700_000_000} for m in MODELS]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        model = body.get("model") or body.get("name")
        self.server.seen.append({"path": self.path, **body})
        if model not in MODELS:
            self._send(404, {"error": f"model '{model}' not found"})
            return
        if self.path == "/api/show":
            d = {"details": {"family": model.split(":")[0]}}
            if CAPS:
                d["capabilities"] = caps(model)
            self._send(200, d)
        elif self.path == "/api/embed":
            # V4.25: deterministic vectors (hashed character trigrams) for the brain's memory
            import zlib
            vecs = []
            for t in body.get("input") or []:
                v = [0.0] * 32
                s_ = f" {str(t).lower()} "
                for i in range(len(s_) - 2):
                    v[zlib.crc32(s_[i:i + 3].encode()) % 32] += 1.0
                vecs.append(v)
            self.server.calls.append({"embed": len(vecs)})
            self._send(200, {"model": model, "embeddings": vecs})
        elif self.path == "/api/generate" and "tagging brain of a PDF accessibility tool" in body.get("prompt", ""):
            # V4.25: the brain's question — capitals are the client's heads (H2), the rest keeps its tag
            pr = body["prompt"].split("Blocks:", 1)[-1]
            out = []
            for r in pr.splitlines():
                m = re.match(r"^(\d+)\. \[proposed=(\w+)[^\]]*\] (.*)$", r)
                if not m:
                    continue
                if m.group(3).strip().isupper():
                    out.append({"i": int(m.group(1)), "role": "H2", "confidence": 0.9,
                                "why": "capitals like the finished heads"})
                else:
                    out.append({"i": int(m.group(1)), "role": m.group(2), "confidence": 0.8, "why": "running text"})
            self.server.calls.append({"brain": True, "n": len(out)})
            self._send(200, {"model": model, "response": json.dumps({"tags": out})})
        elif self.path == "/api/generate" and "learn from people's corrections" in body.get("prompt", ""):
            # V4.26: the AI proposes a rule's conditions (as data)
            self.server.calls.append({"rule": True})
            self._send(200, {"model": model, "response": json.dumps({
                "conditions": [{"f": "caps", "op": ">=", "v": 0.8}, {"f": "words", "op": "<=", "v": 6},
                               {"f": "__import__", "op": "==", "v": "os"}], "why": "capital heads"})})
        elif self.path == "/api/generate" and "Study how it is tagged" in body.get("prompt", ""):
            self.server.calls.append({"study": True})
            self._send(200, {"model": model, "response": json.dumps({
                "doc_type": "journal_article", "summary": "A short research article with capital section heads.",
                "rules": [{"rule": "Section heads set in capitals at the body size are H2", "tag": "H2"},
                          {"rule": "The journal line at the foot of every page is an artifact", "tag": "Artifact"}]})})
        elif self.path == "/api/generate":
            if "CONTINUATION of the table" in body.get("prompt", ""):
                pr = body["prompt"]
                same = "titles: different" not in pr
                self.server.calls.append({"vision": bool(body.get("images")), "same": same})
                self._send(200, {"model": model, "response": json.dumps(
                    {"same_table": same, "why": "titles differ" if not same else "header repeats"})})
            elif "really one" in body.get("prompt", ""):
                pr = body["prompt"]
                self._send(200, {"model": model, "response": json.dumps(
                    {"same": True, "repeat_header": "Skill | Task | Score" in pr.split("START of")[-1],
                     "why": "same columns continue"})})
            elif body.get("images") and "Would a sighted reader" in body.get("prompt", ""):
                k = sum(1 for r in body["prompt"].splitlines() if re.match(r"^\s+\d+: ", r))
                order = [2, 1] + list(range(3, k + 1)) if k >= 2 else [1]
                self._send(200, {"model": model, "response": json.dumps(
                    {"ok": False, "order": order, "why": "the heading comes before its text"})})
            elif body.get("images") and "Say what kind of content it is" in body.get("prompt", ""):
                self._send(200, {"model": model, "response": json.dumps(
                    {"role": "heading", "level": 2, "why": "large bold title over a section"})})
            elif "the way one client" in body.get("prompt", ""):
                # the client's heads are in capitals: those blocks become H2
                pr = body["prompt"].split("Blocks:", 1)[-1]
                out = []
                for r in pr.splitlines():
                    m = re.match(r"^(\d+)\. \[proposed=(\w+)[^\]]*\] (.*)$", r)
                    if m and m.group(3).isupper() and m.group(2) != "H2":
                        out.append({"i": int(m.group(1)), "role": "H2", "why": "capitals like the client's heads"})
                self.server.calls.append({"samples": True, "n": len(out)})
                self._send(200, {"model": model, "response": json.dumps(out)})
            elif "lines of a reference list" in body.get("prompt", ""):
                # entries start where a line opens with "Surname, X."
                rows = [r.split(": ", 1)[-1] for r in body["prompt"].splitlines() if r[:1].isdigit()]
                starts = [i + 1 for i, r in enumerate(rows) if re.match(r"[A-Z][\w'’]+, [A-Z]\.", r)]
                self._send(200, {"model": model, "response": json.dumps({"starts": starts})})
            elif "printed lines of a list" in body.get("prompt", ""):
                rows = [r.split(": ", 1)[-1] for r in body["prompt"].splitlines() if r[:1].isdigit()]
                starts = [i + 1 for i, r in enumerate(rows) if r[:1].isupper()]
                self._send(200, {"model": model, "response": json.dumps({"starts": starts})})
            elif body.get("images") and "Read its structure" in body.get("prompt", ""):
                self._send(200, {"model": model, "response": json.dumps(
                    {"kind": "table", "rows": 5, "columns": 3, "header_rows": 1,
                     "header_column": False, "ordered": False, "why": "ruled grid"})})
            elif body.get("images") and "if it is mathematics" in body.get("prompt", ""):
                self._send(200, {"model": model, "response": json.dumps(
                    {"is_math": True, "decorative": False,
                     "alt": "a squared plus b squared equals c squared",
                     "latex": "a^{2}+b^{2}=c^{2}"})})
            elif "correct level of each heading" in body.get("prompt", ""):
                self._send(200, {"model": model, "response":
                                 '[{"i": 2, "level": 2, "why": "sub-point of the heading above"}]'})
            elif "needs a description that makes sense" in body.get("prompt", ""):
                self._send(200, {"model": model, "response": "Download the scoring guide (PDF)"})
            elif "no tooltip (/TU)" in body.get("prompt", ""):
                self._send(200, {"model": model, "response": "Student name"})
            elif "Summarise what a data table holds" in body.get("prompt", ""):
                self._send(200, {"model": model, "response":
                                 "Skill sets 1 to 13 with the task and the score for each form"})
            elif "cross-reference to another part" in body.get("prompt", ""):
                self._send(200, {"model": model, "response":
                                 '{"is_reference": true, "target": 1, "why": "names a section"}'})
            elif body.get("images") and "INSIDE the table" in body.get("prompt", ""):
                inside = "Number" not in body["prompt"]
                self._send(200, {"model": model, "response": json.dumps({"inside": inside})})
            elif body.get("images"):
                self._send(200, {"model": model, "response": "Bar chart of quarterly sales; Q4 is highest at 140"})
            elif body.get("format") == "json" and "header" in body.get("prompt", ""):
                self._send(200, {"model": model, "response": '{"header": true}'})
            else:
                self._send(200, {"model": model, "response": '[{"i": 3, "role": "P", "why": "author line, not heading"}]'})
        else:
            self._send(404, {"error": "not found"})


class Server(HTTPServer):
    calls: list = []
    seen: list = []          # every request body, in order (path included)


if __name__ == "__main__":
    Server(("127.0.0.1", int(os.environ.get("PORT", "11499"))), H).serve_forever()
