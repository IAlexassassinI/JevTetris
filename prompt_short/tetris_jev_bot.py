#!/usr/bin/env python3
"""
Jev plays tetris.py in real time, without pausing the game.

Loop:  GET /state -> Jev picks ONE action from the allowed ones -> POST /move -> ...
The game does not wait for the model: the piece keeps falling while Jev thinks.
A decision is executed whenever the model is done. If the piece has locked in
the meantime, the action is discarded (tetris.py returns stale_piece) and is never
applied to the next piece.

Every action is chosen by Jev only. If Jev does not answer, the bot does nothing
(the piece just falls) and tries again.

Run (3 terminals):
  1) python -m jev.server --checkpoint ... --port 8791 --batch-size 7
  2) python tetris.py --start-frames-per-row 240 --lock-delay 120   # slow start
  3) python tetris_jev_bot.py --config config.json
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

from jev_client import JevClient, JevError
from tetris_prompt import QUESTION_ID, STOP_ID, build_request

DEFAULTS = {
    "tetris_url": "http://127.0.0.1:8765",
    "jev_url": "http://127.0.0.1:8791/v1/systemone",
    "jev_timeout": 30,
    "include_history": True,
    "drop_actions": ["hard_drop"],
    "shuffle_actions": True,
    "show_ghost": True,
    "action_previews": True,
    "next_state_boards": True,
    "stop_question": True,
    "include_previous": True,
    "include_rotations": True,
    "stop_threshold": 0.5,
    "log_prompts": True,
    "print_prompt": False,
    "max_consecutive_errors": 5,
    "games": 1,
    "reset_at_start": True,
    "seed": 42,
    "max_pieces": 0,
    "trace": "runs/trace.jsonl",
}


class Tetris:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _req(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="GET" if payload is None else "POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def state(self):
        return self._req("/state")

    def move(self, m, piece_id):
        return self._req("/move", {"move": m, "piece_id": piece_id})

    def wait_for_fall(self, s, max_s):
        """'wait' action: send nothing, wait until the piece moves down a row
        or a new piece spawns (at most max_s seconds)."""
        y0, pid0 = s["current_piece"]["y"], s["piece_id"]
        t_end = time.perf_counter() + max_s
        while time.perf_counter() < t_end:
            time.sleep(0.03)
            n = self.state()
            if n["game_over"] or n["piece_id"] != pid0 or n["current_piece"]["y"] != y0:
                break
        return {"ok": True}

    def reset(self, seed=None):
        return self._req("/reset", {} if seed is None else {"seed": seed})


def format_prompt(state_text, questions, probs=None, p_stop=None):
    """Human-readable view of exactly what Jev receives."""
    out = ["[STATE]", state_text]
    for qid, q in questions.items():
        out += ["", f"[QUESTION '{qid}' ({q['type']})]", q["instructions"]]
        if q["type"] == "noul" and p_stop is not None:
            out.append(f"  -> p(yes) = {p_stop:.3f}")
        for k, v in (q.get("criteria") or {}).items():
            p = f"  p={probs[k]:.3f}" if probs and k in probs else ""
            out.append(f"- {k}{p}: {v}")
    return "\n".join(out)


def play_game(game, cfg, jev, trace_f, game_idx, rng):
    piece_id, history, steps, errors = None, [], 0, 0
    prev = None        # where the piece was at the previous decision (for context)
    stats = {"applied": 0, "waited": 0, "stale": 0, "rejected": 0, "jev_errors": 0}
    while True:
        s = game.state()
        if s["game_over"]:
            break
        if cfg["max_pieces"] and s["pieces_placed"] >= cfg["max_pieces"]:
            break
        if s["paused"]:                      # paused from the keyboard (p): just wait
            time.sleep(0.1)
            continue
        if s["piece_id"] != piece_id:
            piece_id, history = s["piece_id"], []

        state_text, questions = build_request(
            s, history if cfg["include_history"] else None, cfg["drop_actions"],
            rng.shuffle if cfg["shuffle_actions"] else None, cfg["show_ghost"],
            cfg["action_previews"], cfg["next_state_boards"], cfg["stop_question"],
            prev, cfg["include_previous"], cfg["include_rotations"])
        if cfg["print_prompt"]:
            print("\n" + "=" * 70 + "\n" + format_prompt(state_text, questions), flush=True)
        t_seen = time.perf_counter()
        try:
            data, latency = jev.ask(state_text, questions)   # both questions, one call
            answers = data["answers"]
            choice, probs = jev.parse_choice(answers.get(QUESTION_ID),
                                             questions[QUESTION_ID]["criteria"])
            p_stop = jev.parse_noul(answers.get(STOP_ID)) if STOP_ID in questions else None
            errors = 0
        except JevError as e:
            errors += 1
            stats["jev_errors"] += 1
            print(f"  Jev error ({errors}/{cfg['max_consecutive_errors']}): {e}", file=sys.stderr)
            if errors >= cfg["max_consecutive_errors"]:
                raise
            time.sleep(0.5)
            continue

        # Jev's "stop" answer decides whether to drop now; otherwise its chosen move.
        stop = p_stop is not None and p_stop >= cfg["stop_threshold"]
        action = "hard_drop" if stop else choice
        age_ms = (time.perf_counter() - t_seen) * 1000   # how old the state was when acting
        if action == "wait":
            # wait at most one row of fall time + lock delay
            sp = s["speed"]
            max_s = (sp["frames_per_row"] + sp.get("lock_delay_frames", 30)) / sp["fps"]
            res = game.wait_for_fall(s, max_s)
            res["score"] = s["score"]
            history.append(action)
            status = "waited"
        else:
            res = game.move(action, s["piece_id"])
        if action == "wait":
            pass
        elif res.get("ok"):
            status = "applied"
            history.append(action)
        elif res.get("error") == "stale_piece":
            status = "stale"          # the piece locked while Jev was thinking
        else:
            status = "rejected"       # e.g. move blocked because the piece has moved down
        stats[status] += 1
        prev = {"piece_id": s["piece_id"], "cells": s["current_piece"]["cells"],
                "action": action, "status": status}
        steps += 1

        rec = {"game": game_idx, "step": steps, "piece_id": s["piece_id"],
               "piece": s["current_piece"]["type"], "frame_seen": s["frame"],
               "frames_per_row": s["speed"]["frames_per_row"], "action": action,
               "status": status, "latency_ms": round(latency, 1), "state_age_ms": round(age_ms, 1),
               "p_stop": None if p_stop is None else round(p_stop, 4), "stop": stop,
               "choice": choice,
               "probs": {k: round(v, 4) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])},
               "score": res.get("score")}
        if cfg["log_prompts"]:   # exactly what was sent to Jev (view with show_step.py)
            rec["request"] = {"state": state_text, "questions": questions}
        trace_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        trace_f.flush()
        ps = f" stop={p_stop:.2f}" if p_stop is not None else ""
        print(f"#{s['piece_id']:>4} {s['current_piece']['type']} {action:<10} {status:<8}{ps} "
              f"{latency:5.0f}ms  lvl={s['level']} {s['speed']['rows_per_second']}r/s "
              f"score={res.get('score')}", flush=True)

    final = game.state()
    return {"game": game_idx, "score": final["score"], "lines": final["lines"],
            "level": final["level"], "pieces": final["pieces_placed"],
            "seconds": final["elapsed_seconds"], "decisions": steps, **stats}


def main():
    ap = argparse.ArgumentParser(description="Jev plays Tetris in real time")
    ap.add_argument("--config", help="JSON settings file (see config.json)")
    for k, v in DEFAULTS.items():
        flag = "--" + k.replace("_", "-")
        if isinstance(v, bool):
            ap.add_argument(flag, type=lambda x: x.lower() in ("1", "true", "yes"), default=None)
        elif isinstance(v, list):   # e.g. --drop-actions hard_drop,soft_drop
            ap.add_argument(flag, type=lambda x: [i for i in x.split(",") if i], default=None)
        else:
            ap.add_argument(flag, type=type(v), default=None)
    ap.add_argument("--dry-run", action="store_true", help="only print the request that would be sent to Jev")
    a = ap.parse_args()

    cfg = dict(DEFAULTS)
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    cfg.update({k: v for k, v in vars(a).items() if k in DEFAULTS and v is not None})

    game = Tetris(cfg["tetris_url"])
    jev = JevClient(cfg["jev_url"], timeout=cfg["jev_timeout"])
    rng = random.Random(cfg["seed"])

    try:
        s = game.state()
    except urllib.error.URLError as e:
        sys.exit(f"Tetris is not reachable at {cfg['tetris_url']}: {e.reason}. Start tetris.py first.")

    if a.dry_run:
        state_text, questions = build_request(
            s, None, cfg["drop_actions"], show_ghost=cfg["show_ghost"],
            action_previews=cfg["action_previews"], next_state_boards=cfg["next_state_boards"],
            stop_question=cfg["stop_question"])
        print(json.dumps({"state": state_text, "questions": questions}, indent=2, ensure_ascii=False))
        print("\n--- readable ---\n" + format_prompt(state_text, questions))
        return

    os.makedirs(os.path.dirname(cfg["trace"]) or ".", exist_ok=True)
    results = []
    with open(cfg["trace"], "a", encoding="utf-8") as tf:
        for g in range(cfg["games"]):
            if g > 0 or s["game_over"] or cfg["reset_at_start"]:
                game.reset(None if cfg["seed"] is None else cfg["seed"] + g)
            try:
                results.append(play_game(game, cfg, jev, tf, g, rng))
            except JevError as e:
                print(f"\nJev is unavailable, stopping: {e}", file=sys.stderr)
                break
            except KeyboardInterrupt:
                break
            print("Game over:", results[-1], flush=True)
    if results:
        avg = sum(r["score"] for r in results) / len(results)
        print(json.dumps({"games": results, "avg_score": avg}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
