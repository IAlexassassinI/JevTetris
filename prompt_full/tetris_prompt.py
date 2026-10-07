"""
Turns the Tetris state (GET /state from tetris.py) into a Jev request.

Step-by-step control: after every action Jev receives the current state and picks
one action. The board is given as is (no hole/height analysis), in a format that
is easy for the model to read: space-separated cells, column and row numbers.

Extra context given to the model:
  * the goal (score as many points as possible by clearing rows) and the scoring table;
  * the losing condition (how high the stack may get before the game is over);
  * drop predictions: where the piece would be after soft_drop (one row down) and
    where it would land after hard_drop, plus how many rows that landing would complete.
    The landing spot is also drawn on the board as 'o' (show_ghost);
  * action previews (action_previews): for EVERY option, where the piece will be right
    after that action, and where it would land if dropped straight to the bottom right
    after it (with the rows that landing would complete);
  * next-state boards (next_state_boards): every option also carries the full board
    as it would look right after that move (for hard_drop: after locking, clearing
    rows and spawning the next piece).

Two questions per request (stop_question): a yes/no "stop" question decides whether
to drop the piece now; the "action" choice (without hard_drop) picks the move otherwise.

Drop actions are offered, but the instructions and action descriptions tell the
model to rotate first, then move, and only then drop (steps 1-2-3).
"""

from tetris import ROT, SHAPES   # same rotation tables as the game, so previews match reality

QUESTION_ID = "action"   # choice: which move to make
STOP_ID = "stop"         # noul: is the piece already in a good final position (drop it now)?
KICKS = [(0, 0), (-1, 0), (1, 0), (-2, 0), (2, 0), (0, -1)]   # same wall kicks as tetris.py

LINE_POINTS = {1: 40, 2: 100, 3: 300, 4: 1200}   # same table as tetris.py, x (level + 1)


def instructions(s):
    w, h = s["width"], s["height"]
    c0 = (w - 4) // 2
    return (
        "You are playing Tetris. GOAL: score as many points as possible. Points come "
        "from completing full horizontal rows of blocks, which then disappear: "
        f"1 row = {LINE_POINTS[1]}, 2 rows at once = {LINE_POINTS[2]}, "
        f"3 rows = {LINE_POINTS[3]}, 4 rows = {LINE_POINTS[4]} points, "
        "multiplied by (level + 1). "
        f"LOSING: new pieces appear in rows 0-1, columns {c0}-{c0 + 3}. If locked blocks "
        f"reach row 1 there (a stack {h - 1} rows high), the next piece cannot appear and the "
        f"game is over, so keep the stack at most {h - 2} rows high and as low as possible. "
        "Do not leave empty gaps under pieces: a row with a gap cannot be completed. "
        "Handle every falling piece '@' like this: "
        "1) SEARCH EARLY: as soon as the piece appears, while it is still high and there "
        "is time, move it left and right (and try its rotations) and compare the predicted "
        "landings of the options to find the best place for it, before committing; "
        "2) STEER: rotate it into the orientation that fits that place and move it directly "
        "above it; "
        "3) only then DROP it. Do not drop a piece that has just appeared without having "
        "looked at other columns first. Rotating more than needed wastes time: after four "
        "rotations the piece is back where it started. "
        "Use the drop predictions to see where the piece would land."
    )


ACTION_TEXT = {
    "rotate_cw": "Search/steer: rotate the falling piece 90 degrees clockwise, to try this "
                 "orientation or because its current orientation does not fit the place "
                 "where it should land.",
    "rotate_ccw": "Search/steer: rotate the falling piece 90 degrees counter-clockwise, to try "
                  "this orientation or because its current orientation does not fit the "
                  "place where it should land.",
    "left": "Search/steer: move the falling piece one column to the left. Good right after "
            "the piece appears, to look for a better landing place early, or to bring it "
            "above the chosen place.",
    "right": "Search/steer: move the falling piece one column to the right. Good right after "
             "the piece appears, to look for a better landing place early, or to bring it "
             "above the chosen place.",
    "hard_drop": "Drop: the piece is rotated correctly and directly above the place "
                 "where it fits: drop it straight down and lock it now.",
    "soft_drop": "Drop: the piece is rotated correctly and directly above the place "
                 "where it fits: move it one row down faster.",
    "wait": "Do nothing this moment and let the piece keep falling by itself. Useful only "
            "when the piece is already above its chosen place; while it is still high, waiting "
            "wastes the time for searching.",
}

STEER = ("left", "right", "rotate_cw", "rotate_ccw")


# ------------------------------------------------------------- predictions
def _free(board, cells):
    h, w = len(board), len(board[0])
    return all(0 <= c < w and r < h and (r < 0 or board[r][c] == ".") for r, c in cells)


def _land(board, cells):
    """Landing cells after a hard drop from `cells`, and how many rows it completes."""
    land = [tuple(x) for x in cells]
    while _free(board, [(r + 1, c) for r, c in land]):
        land = [(r + 1, c) for r, c in land]
    tmp = [list(row) for row in board]
    for r, c in land:
        if r >= 0:
            tmp[r][c] = "#"
    return land, sum(all(ch != "." for ch in row) for row in tmp)


def predict(s):
    """Where the piece would be after soft_drop and after hard_drop."""
    board = s["board"]
    cells = [tuple(x) for x in s["current_piece"]["cells"]]
    down1 = [(r + 1, c) for r, c in cells]
    soft = down1 if _free(board, down1) else None
    land, full = _land(board, cells)
    return {"soft": soft, "land": land, "lines": full}


def after_action(s, move):
    """Piece cells after left/right/rotate (with the game's wall kicks), or None."""
    p, board = s["current_piece"], s["board"]
    if move in ("left", "right"):
        dx = -1 if move == "left" else 1
        cells = [(r, c + dx) for r, c in p["cells"]]
        return cells if _free(board, cells) else None
    drot = {"rotate_cw": 1, "rotate_ccw": -1}.get(move)
    if drot is None:
        return None
    rot = (p["rotation"] + drot) % 4
    for kx, ky in KICKS:
        cells = [(p["y"] + ky + r, p["x"] + kx + c) for r, c in ROT[p["type"]][rot]]
        if _free(board, cells):
            return cells
    return None


def _lock(board, cells):
    """Lock cells into the board and clear full rows -> (new_board, rows_cleared)."""
    w = len(board[0])
    tmp = [list(row) for row in board]
    for r, c in cells:
        if r >= 0:
            tmp[r][c] = "#"
    keep = ["".join(row) for row in tmp if not all(ch != "." for ch in row)]
    cleared = len(board) - len(keep)
    return ["." * w] * cleared + keep, cleared


def _spawn(board, ptype):
    n, w = SHAPES[ptype][0], len(board[0])
    x = (w - n) // 2
    return [(r, x + c) for r, c in ROT[ptype][0]]


def next_state(s, move):
    """Board right after `move`: dict(board, piece, ghost, cleared, locked, game_over, piece_type)."""
    board = s["board"]
    cur = [tuple(x) for x in s["current_piece"]["cells"]]
    res = {"board": board, "piece": cur, "cleared": 0, "locked": False,
           "game_over": False, "piece_type": s["current_piece"]["type"]}
    if move in STEER:
        cells = after_action(s, move)
        res["piece"] = cells or cur
    elif move in ("soft_drop", "wait"):
        down = [(r + 1, c) for r, c in cur]
        if _free(board, down):
            res["piece"] = down
        elif move == "wait":          # resting on the stack: gravity will lock it
            move = "hard_drop"
    if move == "hard_drop":
        land, _ = _land(board, cur)
        new_board, cleared = _lock(board, land)
        nxt = s["next_pieces"][0]
        spawn = _spawn(new_board, nxt)
        res.update(board=new_board, cleared=cleared, locked=True, piece_type=nxt,
                   piece=spawn, game_over=not _free(new_board, spawn))
    if not res["game_over"]:
        res["ghost"] = _land(res["board"], res["piece"])[0]
    return res


def render_next_state(ns, level, show_ghost=True):
    head = "Next state"
    if ns["locked"]:
        head += (f" (piece locked, {_lines_text(ns['cleared'], level).replace('would complete', 'completed')};"
                 f" new {ns['piece_type']} piece appears at the top)")
    if ns["game_over"]:
        return head + ": GAME OVER, the new piece has no room to appear."
    grid = render_grid(ns["board"], ns["piece"], ns.get("ghost") if show_ghost else None)
    return head + ":\n" + grid


def _span(cells):
    rows = sorted({r for r, _ in cells})
    cols = sorted({c for _, c in cells})
    r = f"row {rows[0]}" if rows[0] == rows[-1] else f"rows {rows[0]}-{rows[-1]}"
    c = f"column {cols[0]}" if cols[0] == cols[-1] else f"columns {cols[0]}-{cols[-1]}"
    return f"{r}, {c}"


def _lines_text(n, level):
    if not n:
        return "would complete no rows"
    return f"would complete {n} row(s) = {LINE_POINTS[n] * (level + 1)} points"


# ----------------------------------------------------------------- rendering
def render_grid(board, piece=None, ghost=None):
    """'#' locked block, '@' falling piece, 'o' where hard_drop would land it, '.' empty."""
    piece = {tuple(x) for x in piece or ()}     # JSON gives cells as lists
    ghost = {tuple(x) for x in ghost or ()}
    rows = ["    " + " ".join(str(c % 10) for c in range(len(board[0])))]
    for r, row in enumerate(board):
        cells = []
        for c, ch in enumerate(row):
            if (r, c) in piece:
                cells.append("@")
            elif ch != ".":
                cells.append("#")
            else:
                cells.append("o" if (r, c) in ghost else ".")
        rows.append(f"{r:>2}  " + " ".join(cells))
    return "\n".join(rows)


def render_board(s, ghost=None):
    return render_grid(s["board"], s["current_piece"]["cells"], ghost)


def _rotate_piece(board, piece, drot):
    """Rotate a piece dict {type, rotation, x, y} like tetris.py (with kicks) -> new dict or None."""
    rot = (piece["rotation"] + drot) % 4
    for kx, ky in KICKS:
        x, y = piece["x"] + kx, piece["y"] + ky
        cells = [(y + r, x + c) for r, c in ROT[piece["type"]][rot]]
        if _free(board, cells):
            return {"type": piece["type"], "rotation": rot, "x": x, "y": y, "cells": cells}
    return None


def _shape_lines(ptype, rot):
    cells = ROT[ptype][rot]
    r0, c0 = min(r for r, _ in cells), min(c for _, c in cells)
    h = max(r for r, _ in cells) - r0 + 1
    w = max(c for _, c in cells) - c0 + 1
    norm = {(r - r0, c - c0) for r, c in cells}
    return [" ".join("@" if (r, c) in norm else "." for c in range(w)) for r in range(h)], frozenset(norm)


def describe_orientations(s):
    """All orientations the falling piece can take, how to reach each one from here,
    and where it would land if rotated into it now and then dropped."""
    p = dict(s["current_piece"])
    t, cur = p["type"], p["rotation"]
    board = s["board"]
    if t == "O":
        return ("Possible orientations: this O piece looks the same in every rotation, so "
                "rotating it changes nothing; only moving it left or right matters.")
    out = [f"Possible orientations of this {t} piece (rotate_cw: +1, rotate_ccw: -1; "
           f"four rotations bring it back to the start):"]
    seen = {}
    for k in range(4):                         # k clockwise steps from the current rotation
        rot = (cur + k) % 4
        lines, shape = _shape_lines(t, rot)
        how = {0: "current orientation", 1: "reached with 1x rotate_cw",
               2: "reached with 2x rotate_cw or 2x rotate_ccw", 3: "reached with 1x rotate_ccw"}[k]
        head = f"- rotation {rot} ({how})"
        dup = shape in seen
        if dup:
            head += f", same shape as rotation {seen[shape]} but may sit in other columns"
        seen.setdefault(shape, rot)
        # simulate the rotations on the real board (shortest direction, with kicks)
        q, steps = {**p, "cells": [tuple(c) for c in p["cells"]]}, (k if k <= 2 else -1)
        for _ in range(abs(steps)):
            q = q and _rotate_piece(board, q, 1 if steps > 0 else -1)
        if q is None:
            result = "cannot be reached from here right now (the rotation is blocked)"
        else:
            land, n = _land(board, q["cells"])
            prefix = "dropped now" if k == 0 else "if rotated into it now and dropped"
            result = f"{prefix}: lands at {_span(land)}, {_lines_text(n, s['level'])}"
        out.append(f"{head}: {result}")
        if not dup:                            # draw each distinct shape once
            out += ["    " + l for l in lines]
    return "\n".join(out)


def _rows_above(cells, land):
    return max(r for r, _ in land) - max(r for r, _ in cells)


def describe_previous(s, prev):
    """Where this piece was one decision ago and where it would have landed from there.
    prev = {"piece_id", "cells", "action", "status"} recorded by the bot, or None."""
    if not prev or prev.get("piece_id") != s["piece_id"]:
        return "Previous step: none, this piece has just appeared."
    land, n = _land(s["board"], prev["cells"])      # same piece -> same locked board
    what = {"applied": f"then '{prev['action']}' was applied",
            "waited": "then you waited and it fell by itself",
            "rejected": f"then '{prev['action']}' was blocked and had no effect"}.get(
                prev.get("status"), f"then '{prev['action']}' was chosen")
    now_land, now_n = _land(s["board"], s["current_piece"]["cells"])
    cmp = ("the same landing as now" if sorted(map(tuple, land)) == sorted(map(tuple, now_land))
           else "a different landing than now")
    return (f"Previous step: the piece was at {_span(prev['cells'])}; {what}. "
            f"Dropped straight to the bottom from there it would have landed at {_span(land)}, "
            f"{_lines_text(n, s['level']).replace('would complete', 'would have completed')} ({cmp}).")


def describe_state(s, history=None, pred=None, show_ghost=True, prev=None, include_previous=True,
                   include_rotations=True):
    p = s["current_piece"]
    pred = pred or predict(s)
    legend = "'#' = locked block, '@' = falling piece, '.' = empty"
    if show_ghost:
        legend += ", 'o' = where the piece would land after hard_drop"
    lines = [
        f"Tetris board, {s['width']} columns (0-{s['width'] - 1}) x {s['height']} rows "
        f"(row 0 is the top, row {s['height'] - 1} is the bottom).",
        legend + ".",
        render_board(s, pred["land"] if show_ghost else None),
        f"Falling piece: {p['type']}, rotation {p['rotation']}, at {_span(p['cells'])}; "
        f"it is {_rows_above(p['cells'], pred['land'])} row(s) above where it would land"
        + (" (still high: there is time to search for a better place)."
           if _rows_above(p['cells'], pred['land']) >= 6 else "."),
        "Drop predictions:",
        "- after soft_drop (one row down): "
        + (f"{_span(pred['soft'])}." if pred["soft"] else "cannot move down, it is resting on the stack."),
        f"- after hard_drop (to the bottom): lands at {_span(pred['land'])}, "
        f"{_lines_text(pred['lines'], s['level'])}.",
        *([describe_orientations(s)] if include_rotations else []),
        *([describe_previous(s, prev)] if include_previous else []),
        f"Next pieces: {', '.join(s['next_pieces'])}.",
        f"Score {s['score']}, level {s['level']}, rows cleared {s['lines']}.",
    ]
    if history:
        lines.append("Actions already taken with this piece: " + ", ".join(history[-12:]) + ".")
    return "\n".join(lines)


# ------------------------------------------------------------------- request
def available_actions(s, drop_actions=()):
    allowed = set(s["allowed_moves"])
    steer = [m for m in STEER if m in allowed]
    if s["current_piece"]["type"] == "O":       # rotating an O piece changes nothing
        steer = [m for m in steer if not m.startswith("rotate")]
    acts = steer + ["wait"]
    acts += [m for m in drop_actions if m in allowed and m in ACTION_TEXT]
    return acts


def stop_instructions(s, pred, show_ghost=True):
    ns = next_state(s, "hard_drop")
    return (
        instructions(s) + " "
        "QUESTION: Is the falling piece already in a good final position, so it should be "
        "dropped right now? Look at where it would land (the hard_drop prediction"
        + (" and the 'o' marks" if show_ghost else "") + ") and at the board after the drop "
        "shown below. Answer yes if that landing completes rows, or lies flat on the "
        "surface without leaving empty gaps under it and without making the stack "
        "needlessly high. Answer no if rotating or moving the piece would clearly give a "
        "better landing, and also answer no if the piece has just appeared and is still "
        "high while other columns have not been tried yet: there is time to search first.\n"
        f"If dropped now: lands at {_span(pred['land'])}, {_lines_text(pred['lines'], s['level'])}.\n"
        + render_next_state(ns, s["level"], show_ghost)
    )


def action_result(s, move):
    """One-sentence prediction for an option: position after the move + landing after it."""
    level = s["level"]
    if move == "hard_drop":
        land, n = _land(s["board"], s["current_piece"]["cells"])
        return (f"Result: the piece drops at once and locks at {_span(land)}, "
                f"{_lines_text(n, level)}; then the next piece appears at the top.")
    ns = next_state(s, move)
    if ns["locked"]:                      # 'wait' while resting on the stack
        land, n = _land(s["board"], s["current_piece"]["cells"])
        return (f"Result: the piece is resting on the stack, so it locks where it is, at "
                f"{_span(land)}, {_lines_text(n, level)}; then the next piece appears.")
    land, n = _land(ns["board"], ns["piece"])
    return (f"Result: after this action the piece will be at {_span(ns['piece'])}. "
            f"If it is dropped straight to the bottom right after that, it lands at "
            f"{_span(land)}, {_lines_text(n, level)}.")


def build_request(s, history=None, drop_actions=(), order=None, show_ghost=True,
                  action_previews=True, next_state_boards=True, stop_question=True,
                  prev=None, include_previous=True, include_rotations=True):
    """Returns (state_text, questions) ready for POST /v1/systemone."""
    pred = predict(s)
    acts = available_actions(s, drop_actions)
    if stop_question:                # dropping is decided by the stop question
        acts = [m for m in acts if m != "hard_drop"]
    if order is not None:            # shuffle to remove option-order bias
        order(acts)
    criteria = {}
    for m in acts:
        text = ACTION_TEXT[m]
        if action_previews:
            text += " " + action_result(s, m)
        if next_state_boards:
            text += "\n" + render_next_state(next_state(s, m), s["level"], show_ghost)
        criteria[m] = text
    instr = instructions(s)
    if stop_question:
        instr += (" The piece is NOT in its final position yet: choose the move that "
                  "searches for or steers toward a better place.")
    questions = {QUESTION_ID: {"type": "choice", "instructions": instr, "criteria": criteria}}
    if stop_question and "hard_drop" in s["allowed_moves"]:
        questions = {STOP_ID: {"type": "noul", "instructions": stop_instructions(s, pred, show_ghost)},
                     **questions}
    return (describe_state(s, history, pred, show_ghost, prev, include_previous, include_rotations),
            questions)
