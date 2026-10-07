"""
Minimal HTTP client for Jev / Open-Jev (System One), standard library only.

Protocol (Open-Jev, endpoint /v1/systemone):
  POST {"state": "<text>", "questions": {"<id>": {"type": "choice",
        "instructions": "...", "criteria": {"<label>": "<description>", ...}}}}
  -> {"answers": {"<id>": {"type": "choice", "choice": "<label>",
        "probabilities": {"<label>": p, ...}}}}

The response parser is deliberately tolerant of field names (choice/selected,
probabilities/distribution), since different server builds may differ slightly.
"""
import json
import os
import time
import urllib.error
import urllib.request


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(self, url="http://127.0.0.1:8791/v1/systemone", timeout=30.0,
                 api_key_env="OPEN_JEV_API_KEY"):
        self.url = url
        self.timeout = timeout
        self.api_key = os.environ.get(api_key_env) if api_key_env else None

    # ------------------------------------------------------------------ raw
    def ask(self, state, questions):
        body = json.dumps({"state": state, "questions": questions}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise JevError(f"HTTP {e.code} from Jev: {detail}") from e
        except urllib.error.URLError as e:
            raise JevError(f"Could not connect to Jev ({self.url}): {e.reason}") from e
        latency_ms = (time.perf_counter() - t0) * 1000
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise JevError(f"Jev returned non-JSON: {raw[:200]!r}") from e
        if "answers" not in data:
            raise JevError(f"Response has no 'answers': {str(data)[:300]}")
        return data, latency_ms

    # ---------------------------------------------------------------- parsers
    @staticmethod
    def parse_choice(ans, criteria):
        """-> (chosen_label, {label: p})"""
        if not isinstance(ans, dict):
            raise JevError(f"Bad choice answer: {ans}")
        probs = ans.get("probabilities") or ans.get("distribution") or ans.get("probs") or {}
        if isinstance(probs, list):  # format [{"label":..,"probability":..}]
            probs = {p.get("label") or p.get("key"): p.get("probability", p.get("p"))
                     for p in probs}
        probs = {k: float(v) for k, v in probs.items() if k in criteria}
        choice = ans.get("choice") or ans.get("selected") or ans.get("answer")
        if choice not in criteria:
            if not probs:
                raise JevError(f"Answer has neither a valid choice nor probabilities: {ans}")
            choice = max(probs, key=probs.get)
        return choice, probs

    @staticmethod
    def parse_noul(ans):
        """-> probability of "yes" for a noul (yes/no) question."""
        if isinstance(ans, (int, float)):
            return float(ans)
        if not isinstance(ans, dict):
            raise JevError(f"Bad noul answer: {ans}")
        for k in ("noul", "probability", "p", "p_yes", "yes", "value"):
            v = ans.get(k)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, dict):          # e.g. {"noul": {"probability": 0.7}}
                for kk in ("probability", "p", "yes"):
                    if isinstance(v.get(kk), (int, float)):
                        return float(v[kk])
        probs = ans.get("probabilities")
        if isinstance(probs, dict):
            for k in ("yes", "Yes", "true", "True"):
                if k in probs:
                    return float(probs[k])
        raise JevError(f"Cannot read the yes-probability from noul answer: {ans}")

    # --------------------------------------------------------------- choice
    def choose(self, state, question_id, instructions, criteria):
        """One choice question. Returns (chosen_label, {label: p}, latency_ms, raw)."""
        q = {question_id: {"type": "choice", "instructions": instructions, "criteria": criteria}}
        data, latency = self.ask(state, q)
        ans = data["answers"].get(question_id)
        if not isinstance(ans, dict):
            raise JevError(f"No answer for question '{question_id}': {data['answers']}")
        probs = ans.get("probabilities") or ans.get("distribution") or ans.get("probs") or {}
        if isinstance(probs, list):  # format [{"label":..,"probability":..}]
            probs = {p.get("label") or p.get("key"): p.get("probability", p.get("p"))
                     for p in probs}
        probs = {k: float(v) for k, v in probs.items() if k in criteria}
        choice = ans.get("choice") or ans.get("selected") or ans.get("answer")
        if choice not in criteria:
            if not probs:
                raise JevError(f"Answer has neither a valid choice nor probabilities: {ans}")
            choice = max(probs, key=probs.get)
        return choice, probs, latency, data
