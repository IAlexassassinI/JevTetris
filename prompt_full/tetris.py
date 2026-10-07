#!/usr/bin/env python3
"""
ASCII Tetris with a fixed-rate game loop and a JSON HTTP API.

Run (curses UI + API):      python tetris.py
Run headless (API only):    python tetris.py --headless
Options:                    --fps 60 --port 8765 --start-level 0 --seed 42
Slow start for AI agents:   --start-frames-per-row 240 --speedup 0.12 --lock-delay 120

Gravity follows the NES curve (frames per row at 60 FPS), so the game speeds up
as lines are cleared (level = start_level + lines // 10).
Score comes only from cleared rows: 1/2/3/4 rows = 40/100/300/1200 x (level + 1);
soft/hard drops give no points.
With --start-frames-per-row N the game starts slower: frames per row =
max(NES(level), N * (1 - speedup) ** level), i.e. it gradually speeds up and
eventually joins the NES curve.

API (all JSON, default http://127.0.0.1:8765):
  GET  /state    full state (board, piece, next, score, level, speed, allowed moves, placements)
  GET  /moves    just allowed moves + placements
  GET  /ascii    plain-text rendering
  POST /move     {"move": "left|right|rotate_cw|rotate_ccw|soft_drop|hard_drop", "piece_id": N}
                 (piece_id is optional; if given and the piece has changed -> stale_piece)
                 {"move": "place", "rotation": 0-3, "col": 0-9, "piece_id": N}   (teleport + hard drop)
  POST /pause, /resume, /reset   (reset accepts optional {"seed": 1, "start_level": 0})
"""
import argparse
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

W, H = 10, 20
LOCK_DELAY = 30        # frames a grounded piece waits before locking
MAX_LOCK_RESETS = 15   # how many moves/rotations may reset the lock delay (per piece, never restored)
LINE_POINTS = [0, 40, 100, 300, 1200]   # the ONLY source of score: cleared rows x (level + 1)

# Cells as (row, col) inside an n x n box, rotation 0.
SHAPES = {
    "I": (4, [(1, 0), (1, 1), (1, 2), (1, 3)]),
    "O": (2, [(0, 0), (0, 1), (1, 0), (1, 1)]),
    "T": (3, [(0, 1), (1, 0), (1, 1), (1, 2)]),
    "S": (3, [(0, 1), (0, 2), (1, 0), (1, 1)]),
    "Z": (3, [(0, 0), (0, 1), (1, 1), (1, 2)]),
    "J": (3, [(0, 0), (1, 0), (1, 1), (1, 2)]),
    "L": (3, [(0, 2), (1, 0), (1, 1), (1, 2)]),
}


def _build_rotations():
    out = {}
    for name, (n, cells) in SHAPES.items():
        states = [list(cells)]
        for _ in range(3):
            states.append([(c, n - 1 - r) for r, c in states[-1]])  # clockwise
        out[name] = states
    return out


ROT = _build_rotations()


def gravity_frames(level):
    """Frames per one-row drop at 60 FPS (NES NTSC table)."""
    table = {0: 48, 1: 43, 2: 38, 3: 33, 4: 28, 5: 23, 6: 18, 7: 13, 8: 8, 9: 6}
    if level in table:
        return table[level]
    if level <= 12:
        return 5
    if level <= 15:
        return 4
    if level <= 18:
        return 3
    if level <= 28:
        return 2
    return 1


def board_features(board):
    heights, holes = [0] * W, 0
    for c in range(W):
        seen = False
        for r in range(H):
            if board[r][c]:
                if not seen:
                    heights[c] = H - r
                    seen = True
            elif seen:
                holes += 1
    bump = sum(abs(heights[i] - heights[i + 1]) for i in range(W - 1))
    return {"agg_height": sum(heights), "max_height": max(heights),
            "holes": holes, "bumpiness": bump}


class Game:
    def __init__(self, fps=60, seed=None, start_level=0, start_frames=0, speedup=0.12,
                 lock_delay=LOCK_DELAY, max_lock_resets=MAX_LOCK_RESETS):
        self.fps = fps
        self.start_frames = start_frames   # 0 = pure NES curve
        self.speedup = speedup
        self.lock_delay = lock_delay
        self.max_lock_resets = max_lock_resets
        self.lock = threading.RLock()
        self.reset(seed, start_level)

    # ------------------------------------------------------------------ setup
    def reset(self, seed=None, start_level=0):
        with self.lock:
            self.seed = seed
            self.rng = random.Random(seed)
            self.start_level = start_level
            self.board = [[""] * W for _ in range(H)]
            self.bag, self.queue = [], []
            self.score = self.lines = self.frame = self.pieces = 0
            self.level = start_level
            self.over = False
            self.paused = False
            self.piece = None
            self.piece_id = 0
            for _ in range(4):
                self.queue.append(self._draw())
            self._spawn()

    def _draw(self):
        if not self.bag:
            self.bag = list(SHAPES)
            self.rng.shuffle(self.bag)
        return self.bag.pop()

    def _spawn(self):
        t = self.queue.pop(0)
        self.queue.append(self._draw())
        n = SHAPES[t][0]
        self.piece = {"type": t, "rot": 0, "x": (W - n) // 2, "y": 0}
        self.piece_id += 1
        self.grav_counter = self.lock_counter = self.lock_resets = 0
        self.touched = False                 # has this piece touched the stack/floor yet?
        self.lowest_y = self.piece["y"]      # lowest row (largest y) the piece has reached
        if not self._valid(self._cells(self.piece)):
            self.over = True

    def frames_per_row(self, level=None):
        level = self.level if level is None else level
        nes = gravity_frames(level)
        if not self.start_frames:
            return nes
        slow = round(self.start_frames * (1 - self.speedup) ** level)
        return max(nes, slow)

    # --------------------------------------------------------------- geometry
    @staticmethod
    def _cells(p, rot=None, x=None, y=None):
        rot = p["rot"] if rot is None else rot
        x = p["x"] if x is None else x
        y = p["y"] if y is None else y
        return [(y + r, x + c) for r, c in ROT[p["type"]][rot % 4]]

    def _valid(self, cells):
        for r, c in cells:
            if c < 0 or c >= W or r >= H:
                return False
            if r >= 0 and self.board[r][c]:
                return False
        return True

    def _grounded(self):
        p = self.piece
        return not self._valid(self._cells(p, y=p["y"] + 1))

    # ---------------------------------------------------------------- actions
    def _try(self, dx=0, drot=0):
        p = self.piece
        rot = (p["rot"] + drot) % 4
        kicks = [(0, 0)] if drot == 0 else [(0, 0), (-1, 0), (1, 0), (-2, 0), (2, 0), (0, -1)]
        for kx, ky in kicks:
            nx, ny = p["x"] + dx + kx, p["y"] + ky
            if self._valid(self._cells(p, rot, nx, ny)):
                # After first contact a move/rotation may restart the lock timer, but only
                # max_lock_resets times per piece; the count is never restored, even if a
                # kick lifts the piece back into the air.
                if self.touched and self.lock_resets < self.max_lock_resets:
                    self.lock_counter = 0
                    self.lock_resets += 1
                p["rot"], p["x"], p["y"] = rot, nx, ny
                return True
        return False

    def _soft_drop(self):
        p = self.piece
        if self._valid(self._cells(p, y=p["y"] + 1)):
            p["y"] += 1
            self.grav_counter = 0
            return True
        return False

    def _hard_drop(self):
        while self._valid(self._cells(self.piece, y=self.piece["y"] + 1)):
            self.piece["y"] += 1
        self._lock()

    def _lock(self):
        for r, c in self._cells(self.piece):
            if r < 0:
                self.over = True
                return
            self.board[r][c] = self.piece["type"]
        self.pieces += 1
        keep = [row for row in self.board if not all(row)]
        cleared = H - len(keep)
        if cleared:
            self.board = [[""] * W for _ in range(cleared)] + keep
            self.score += LINE_POINTS[cleared] * (self.level + 1)
            self.lines += cleared
            self.level = self.start_level + self.lines // 10
        self._spawn()

    # ------------------------------------------------------------- game clock
    def tick(self):
        """Advance the game by exactly one frame."""
        with self.lock:
            if self.over or self.paused:
                return
            self.frame += 1
            p = self.piece
            grounded = self._grounded()
            if p["y"] > self.lowest_y:           # fell to a new lowest row: fresh timer
                self.lowest_y = p["y"]
                self.lock_counter = 0
            if grounded:
                self.touched = True
            if self.touched:
                # Once the piece has touched down, the lock timer keeps running even if a
                # rotation kick lifts it into the air, so it cannot be kept alive forever.
                self.lock_counter += 1
                if self.lock_counter >= self.lock_delay:
                    while self._valid(self._cells(p, y=p["y"] + 1)):
                        p["y"] += 1
                    self._lock()
                    return
                if grounded:
                    return
            self.grav_counter += 1
            if self.grav_counter >= self.frames_per_row():
                self.grav_counter = 0
                self.piece["y"] += 1

    # ------------------------------------------------------------ public API
    def apply(self, move, **kw):
        with self.lock:
            if self.over:
                return {"ok": False, "error": "game_over"}
            if self.paused and move not in ("pause", "resume"):
                return {"ok": False, "error": "paused"}
            if move == "pause":
                self.paused = True
                return {"ok": True}
            if move == "resume":
                self.paused = False
                return {"ok": True}
            pid = kw.get("piece_id")
            if pid is not None and move not in ("pause", "resume") and pid != self.piece_id:
                return {"ok": False, "error": "stale_piece", "piece_id": self.piece_id}
            if move == "place":
                return self._place(kw.get("rotation"), kw.get("col"), kw.get("piece_id"))
            if move == "hard_drop":
                self._hard_drop()
                return {"ok": True}
            actions = {
                "left": lambda: self._try(dx=-1),
                "right": lambda: self._try(dx=1),
                "rotate_cw": lambda: self._try(drot=1),
                "rotate_ccw": lambda: self._try(drot=-1),
                "soft_drop": self._soft_drop,
            }
            if move not in actions:
                return {"ok": False, "error": f"unknown_move:{move}"}
            return {"ok": bool(actions[move]())}

    def _place_x(self, rot, col):
        minc = min(c for _, c in ROT[self.piece["type"]][rot])
        return col - minc

    def _place(self, rotation, col, piece_id):
        if piece_id is not None and piece_id != self.piece_id:
            return {"ok": False, "error": "stale_piece", "piece_id": self.piece_id}
        if not isinstance(rotation, int) or not isinstance(col, int):
            return {"ok": False, "error": "need integer rotation and col"}
        rot = rotation % 4
        x = self._place_x(rot, col)
        if not self._valid(self._cells(self.piece, rot, x, 0)):
            return {"ok": False, "error": "invalid_placement"}
        self.piece.update(rot=rot, x=x, y=0)
        self._hard_drop()
        return {"ok": True}

    def placements(self):
        """All distinct final landing spots for the current piece."""
        res, seen = [], set()
        p, t = self.piece, self.piece["type"]
        for rot in range(4):
            cells0 = ROT[t][rot]
            mr = min(r for r, _ in cells0)
            mc = min(c for _, c in cells0)
            shape = frozenset((r - mr, c - mc) for r, c in cells0)
            if shape in seen:
                continue
            seen.add(shape)
            width = max(c for _, c in cells0) - mc + 1
            for col in range(0, W - width + 1):
                x = col - mc
                if not self._valid(self._cells(p, rot, x, 0)):
                    continue
                y = 0
                while self._valid(self._cells(p, rot, x, y + 1)):
                    y += 1
                cells = self._cells(p, rot, x, y)
                tmp = [row[:] for row in self.board]
                for r, c in cells:
                    if r >= 0:
                        tmp[r][c] = t
                full = [row for row in tmp if all(row)]
                tmp = [[""] * W for _ in full] + [row for row in tmp if not all(row)]
                res.append({"rotation": rot, "col": col, "cells": cells,
                            "lines_cleared": len(full), **board_features(tmp)})
        return res

    def allowed_moves(self):
        if self.over:
            return []
        p, moves = self.piece, []
        if self._valid(self._cells(p, x=p["x"] - 1)):
            moves.append("left")
        if self._valid(self._cells(p, x=p["x"] + 1)):
            moves.append("right")
        for name, d in (("rotate_cw", 1), ("rotate_ccw", -1)):
            rot = (p["rot"] + d) % 4
            if any(self._valid(self._cells(p, rot, p["x"] + kx, p["y"] + ky))
                   for kx, ky in [(0, 0), (-1, 0), (1, 0), (-2, 0), (2, 0), (0, -1)]):
                moves.append(name)
        if self._valid(self._cells(p, y=p["y"] + 1)):
            moves.append("soft_drop")
        moves += ["hard_drop", "place"]
        return moves

    def state(self):
        with self.lock:
            cells = [] if self.over else self._cells(self.piece)
            locked = ["".join(c or "." for c in row) for row in self.board]
            shown = [list(r) for r in locked]
            for r, c in cells:
                if 0 <= r < H:
                    shown[r][c] = self.piece["type"].lower()
            gf = self.frames_per_row()
            return {
                "width": W, "height": H,
                "board": locked,                       # locked cells only; '.' = empty
                "board_with_piece": ["".join(r) for r in shown],  # active piece in lowercase
                "piece_id": self.piece_id,
                "current_piece": {"type": self.piece["type"], "rotation": self.piece["rot"],
                                  "x": self.piece["x"], "y": self.piece["y"], "cells": cells},
                "next_pieces": self.queue[:3],
                "score": self.score, "level": self.level, "lines": self.lines,
                "pieces_placed": self.pieces,
                "speed": {"frames_per_row": gf, "rows_per_second": round(self.fps / gf, 3),
                          "fps": self.fps, "lock_delay_frames": self.lock_delay},
                "frame": self.frame, "elapsed_seconds": round(self.frame / self.fps, 2),
                "paused": self.paused, "game_over": self.over,
                "allowed_moves": self.allowed_moves(),
                "placements": [] if self.over else self.placements(),
                "features": board_features(self.board),
            }


# ------------------------------------------------------------------ rendering
def render_ascii(s):
    rows = s["board_with_piece"]
    out = ["+" + "--" * W + "+"]
    for row in rows:
        out.append("|" + "".join("[]" if ch != "." else " ." for ch in row) + "|")
    out.append("+" + "--" * W + "+")
    out.append(f"Score {s['score']}  Level {s['level']}  Lines {s['lines']}  "
               f"Speed {s['speed']['rows_per_second']} rows/s")
    out.append("Next: " + " ".join(s["next_pieces"]) + ("   GAME OVER" if s["game_over"] else ""))
    return "\n".join(out)


def run_curses(stdscr, game, fps, stop):
    import curses
    curses.curs_set(0)
    stdscr.nodelay(True)
    keymap = {curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right", curses.KEY_DOWN: "soft_drop",
              curses.KEY_UP: "rotate_cw", ord("x"): "rotate_cw", ord("z"): "rotate_ccw",
              ord(" "): "hard_drop"}
    dt, nxt = 1.0 / fps, time.perf_counter()
    while not stop.is_set():
        while (k := stdscr.getch()) != -1:
            if k in (ord("q"), 27):
                return
            if k == ord("r"):
                game.reset(game.seed, game.start_level)
            elif k == ord("p"):
                game.paused = not game.paused
            elif k in keymap:
                game.apply(keymap[k])
        game.tick()
        s = game.state()
        stdscr.erase()
        try:
            for i, line in enumerate(render_ascii(s).split("\n")[: H + 2]):
                stdscr.addstr(i, 0, line)
            px = 2 * W + 5
            info = [f"SCORE  {s['score']}", f"LEVEL  {s['level']}", f"LINES  {s['lines']}",
                    f"SPEED  {s['speed']['rows_per_second']} rows/s",
                    f"TIME   {s['elapsed_seconds']}s", f"PIECES {s['pieces_placed']}", "",
                    "NEXT: " + " ".join(s["next_pieces"]), "",
                    "arrows move/soft drop", "up/x rotate cw, z ccw", "space hard drop",
                    "p pause  r restart  q quit", ""]
            if s["paused"]:
                info.append("** PAUSED **")
            if s["game_over"]:
                info.append("** GAME OVER ** (r to restart)")
            for i, line in enumerate(info):
                stdscr.addstr(i, px, line)
        except Exception:
            pass
        stdscr.refresh()
        nxt += dt
        delay = nxt - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            nxt = time.perf_counter()


def _info_lines(s):
    info = [f"SCORE  {s['score']}", f"LEVEL  {s['level']}", f"LINES  {s['lines']}",
            f"SPEED  {s['speed']['rows_per_second']} rows/s",
            f"TIME   {s['elapsed_seconds']}s", f"PIECES {s['pieces_placed']}", "",
            "NEXT: " + " ".join(s["next_pieces"]), "",
            "arrows move/soft drop", "up/x rotate cw, z ccw", "space hard drop",
            "p pause  r restart  q quit", ""]
    if s["paused"]:
        info.append("** PAUSED **")
    if s["game_over"]:
        info.append("** GAME OVER ** (r to restart)")
    return info


def run_console(game, fps, stop):
    """Windows console UI without curses: msvcrt for keys, ANSI escapes for drawing."""
    import msvcrt
    import os
    os.system("")                                   # enable ANSI (VT) mode in Windows console
    sys.stdout.write("\x1b[2J\x1b[?25l")             # clear screen, hide cursor
    arrows = {"K": "left", "M": "right", "P": "soft_drop", "H": "rotate_cw"}
    keys = {"x": "rotate_cw", "z": "rotate_ccw", " ": "hard_drop"}
    dt, nxt, redraw_every = 1.0 / fps, time.perf_counter(), max(1, fps // 30)
    n = 0
    try:
        while not stop.is_set():
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):          # arrow / special key prefix
                    move = arrows.get(msvcrt.getwch())
                    if move:
                        game.apply(move)
                    continue
                ch = ch.lower()
                if ch in ("q", "\x1b"):
                    return
                if ch == "r":
                    game.reset(game.seed, game.start_level)
                elif ch == "p":
                    game.paused = not game.paused
                elif ch in keys:
                    game.apply(keys[ch])
            game.tick()
            n += 1
            if n % redraw_every == 0:
                s = game.state()
                board = render_ascii(s).split("\n")[: H + 2]
                info = _info_lines(s)
                out = ["\x1b[H"]
                for i in range(max(len(board), len(info))):
                    left = board[i] if i < len(board) else " " * (2 * W + 2)
                    right = info[i] if i < len(info) else ""
                    out.append(f"{left}   {right:<34}\n")
                sys.stdout.write("".join(out))
                sys.stdout.flush()
            nxt += dt
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()
    finally:
        sys.stdout.write("\x1b[?25h\n")            # show cursor again
        sys.stdout.flush()


def run_headless(game, fps, stop, exit_on_over):
    dt, nxt, last = 1.0 / fps, time.perf_counter(), 0
    while not stop.is_set():
        game.tick()
        if game.over and exit_on_over:
            return
        if game.frame - last >= fps * 5:
            last = game.frame
            s = game.state()
            print(f"[t={s['elapsed_seconds']}s] score={s['score']} level={s['level']} "
                  f"lines={s['lines']}", flush=True)
        nxt += dt
        delay = nxt - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            nxt = time.perf_counter()


# ------------------------------------------------------------------ HTTP API
def make_handler(game):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, text=False):
            body = (obj if text else json.dumps(obj)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain" if text else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/state":
                self._send(200, game.state())
            elif self.path == "/moves":
                s = game.state()
                self._send(200, {"allowed_moves": s["allowed_moves"], "placements": s["placements"],
                                 "piece_id": s["piece_id"]})
            elif self.path == "/ascii":
                self._send(200, render_ascii(game.state()), text=True)
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "error": "bad_json"})
            if self.path == "/move":
                move = data.pop("move", None)
                res = game.apply(move, **{k: v for k, v in data.items()
                                          if k in ("rotation", "col", "piece_id")})
                self._send(200, {**res, "score": game.score, "game_over": game.over})
            elif self.path in ("/pause", "/resume"):
                self._send(200, game.apply(self.path[1:]) if not game.over else {"ok": False})
            elif self.path == "/reset":
                game.reset(data.get("seed", game.seed), data.get("start_level", game.start_level))
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not_found"})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=60, help="fixed refresh/tick rate")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--start-level", type=int, default=0)
    ap.add_argument("--start-frames-per-row", type=int, default=0,
                    help="slow start: frames per row at level 0 (0 = NES curve, 48)")
    ap.add_argument("--speedup", type=float, default=0.12,
                    help="slow start: per-level speedup fraction")
    ap.add_argument("--lock-delay", type=int, default=LOCK_DELAY,
                    help="frames a grounded piece waits before locking")
    ap.add_argument("--max-lock-resets", type=int, default=MAX_LOCK_RESETS,
                    help="moves/rotations that may restart the lock timer once a piece has landed")
    ap.add_argument("--headless", action="store_true", help="no curses UI, API only")
    ap.add_argument("--exit-on-over", action="store_true", help="headless: exit at game over")
    a = ap.parse_args()

    game = Game(a.fps, a.seed, a.start_level, a.start_frames_per_row, a.speedup, a.lock_delay,
                a.max_lock_resets)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(game))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()
    try:
        if a.headless:
            print(f"API on http://{a.host}:{a.port}  (Ctrl+C to stop)", flush=True)
            run_headless(game, a.fps, stop, a.exit_on_over)
        else:
            try:
                import curses
                curses.wrapper(run_curses, game, a.fps, stop)
            except ImportError:
                try:
                    import msvcrt  # noqa: F401  (Windows)
                except ImportError:
                    print("No curses/msvcrt available -> running headless", flush=True)
                    run_headless(game, a.fps, stop, a.exit_on_over)
                else:
                    run_console(game, a.fps, stop)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        s = game.state()
        print(json.dumps({"final_score": s["score"], "level": s["level"], "lines": s["lines"],
                          "pieces": s["pieces_placed"], "seconds": s["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
