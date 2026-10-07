#!/usr/bin/env python3
"""
FAKE Jev server for testing the integration without a GPU. NOT a model!

Answers in the /v1/systemone format, but the probabilities are random
(--mode random) or favour the first listed option (--mode first).
  python mock_jev_server.py --port 8791
"""
import argparse
import json
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(mode, rng, delay):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if delay:
                time.sleep(delay)  # simulate model latency
            answers = {}
            for qid, q in req.get("questions", {}).items():
                if q.get("type") == "noul":
                    p = rng.random()
                    answers[qid] = {"type": "noul", "noul": p}
                    continue
                labels = list(q.get("criteria") or {})
                if mode == "first":
                    w = [2.0 ** -i for i in range(len(labels))]
                else:
                    w = [rng.random() for _ in labels]
                tot = sum(w) or 1.0
                probs = {l: x / tot for l, x in zip(labels, w)}
                answers[qid] = {"type": "choice", "choice": max(probs, key=probs.get),
                                "probabilities": probs}
            body = json.dumps({"answers": answers, "model": "MOCK"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--mode", choices=["random", "first"], default="first")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds of delay per response")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.mode, random.Random(a.seed), a.delay))
    print(f"MOCK Jev at http://127.0.0.1:{a.port}/v1/systemone (mode={a.mode})")
    srv.serve_forever()
