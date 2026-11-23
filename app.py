"""Word Grid Duel - Streamlit version.

Modes: same device, vs bot, and online (two devices, room code).
Online rooms live in server memory (shared across sessions), and every player's
page polls once a second, so no database or extra service is needed.
"""
import copy
import random
import re
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Word Grid Duel", page_icon="🔤", layout="wide")

LIMIT = 30
DIRS = [(0, 1), (1, 0), (1, 1), (1, -1)]  # right, down, down-right, down-left
HERE = Path(__file__).parent
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LETTERS = list("ETAOINSRHLDCUMFPGWYBVKXJQZ")
ss = st.session_state


# ---------------------------------------------------------------- dictionary
def parse_words(text):
    return {w.upper() for w in re.findall(r"[A-Za-z]+", text) if len(w) >= 3}


@st.cache_resource
def base_dictionary():
    words = set()
    for name in ("words_builtin.txt", "Simple_Dictioinary.txt"):
        p = HERE / name
        if p.exists():
            words |= parse_words(p.read_text(encoding="utf-8", errors="ignore"))
    return frozenset(words)


@st.cache_resource
def shared():
    """One dict shared by every browser session on this server."""
    return {"lock": threading.RLock(), "rooms": {}}


# ---------------------------------------------------------------- game logic
def new_game(rows, cols, min_word):
    return {
        "rows": rows, "cols": cols, "min_word": min_word,
        "grid": [["" for _ in range(cols)] for _ in range(rows)],
        "turn": "A", "scores": {"A": 0, "B": 0}, "claimed": set(), "last": [],
        "history": [], "started": False, "over": False,
        "turn_start": time.time(), "extra": set(),
    }


def log(g, p, text, detail):
    g["history"].insert(0, (p, text, detail))
    del g["history"][10:]


def advance(g):
    if all(all(row) for row in g["grid"]):
        g["over"] = True
    else:
        g["turn"] = "B" if g["turn"] == "A" else "A"
    g["turn_start"] = time.time()


def scan(g, words):
    """Every unclaimed dictionary word on the board (forward in 4 directions)."""
    rows, cols, mw, grid = g["rows"], g["cols"], g["min_word"], g["grid"]
    found, seen = [], set()
    for r in range(rows):
        for c in range(cols):
            for dr, dc in DIRS:
                text, cells, rr, cc = "", [], r, c
                while 0 <= rr < rows and 0 <= cc < cols and grid[rr][cc]:
                    text += grid[rr][cc]
                    cells.append((rr, cc))
                    if len(text) >= mw and text in words and text not in g["claimed"] and text not in seen:
                        seen.add(text)
                        found.append((text, list(cells)))
                    rr += dr
                    cc += dc
    return found


def words_through(g, words, r, c):
    """Unclaimed words that include cell (r, c). Fast version used by the bot."""
    rows, cols, mw, grid = g["rows"], g["cols"], g["min_word"], g["grid"]
    out = set()
    for dr, dc in DIRS:
        sr, sc = r, c
        while 0 <= sr - dr < rows and 0 <= sc - dc < cols and grid[sr - dr][sc - dc]:
            sr -= dr
            sc -= dc
        letters, k, rr, cc = [], -1, sr, sc
        while 0 <= rr < rows and 0 <= cc < cols and grid[rr][cc]:
            if (rr, cc) == (r, c):
                k = len(letters)
            letters.append(grid[rr][cc])
            rr += dr
            cc += dc
        text = "".join(letters)
        for i in range(k + 1):
            for j in range(max(k, i + mw - 1), len(text)):
                w = text[i:j + 1]
                if w in words and w not in g["claimed"]:
                    out.add(w)
    return out


def place(g, words, r, c, ch):
    p = g["turn"]
    g["grid"][r][c] = ch
    found = scan(g, words)
    g["last"] = found
    if found:
        g["scores"][p] += len(found)
        g["claimed"].update(w for w, _ in found)
    detail = (f"Scored {len(found)}: " + ", ".join(w for w, _ in found)) if found \
        else "No score — no new dictionary word found"
    log(g, p, f'placed "{ch}" at ({r + 1}, {c + 1})', detail)
    advance(g)


def maybe_timeout(g):
    if g["started"] and not g["over"] and time.time() - g["turn_start"] >= LIMIT:
        log(g, g["turn"], "time ran out", "Turn skipped.")
        advance(g)


def bot_pick(g, words, level):
    grid = g["grid"]
    empties = [(r, c) for r in range(g["rows"]) for c in range(g["cols"]) if not grid[r][c]]
    if not empties:
        return None

    def rand_move():
        r, c = random.choice(empties)
        return r, c, random.choice(LETTERS)

    if level == "Easy" and random.random() < 0.6:
        return rand_move()

    def gain(r, c, ch):
        grid[r][c] = ch
        n = len(words_through(g, words, r, c))
        grid[r][c] = ""
        return n

    best, best_gain = [], 0
    for r, c in empties:
        for ch in LETTERS:
            n = gain(r, c, ch)
            if n > best_gain:
                best_gain, best = n, [(r, c, ch)]
            elif n == best_gain and n > 0:
                best.append((r, c, ch))
    if best_gain > 0:
        return random.choice(best)
    if level != "Hard" or len(empties) < 2:
        return rand_move()

    # Hard: nothing to score, so leave the opponent the fewest scoring replies.
    pick, pick_threat, t0 = rand_move(), 10 ** 9, time.perf_counter()
    while time.perf_counter() - t0 < 0.3:
        r, c, ch = rand_move()
        grid[r][c] = ch
        threat = 0
        for r2, c2 in empties:
            if (r2, c2) == (r, c):
                continue
            for ch2 in LETTERS:
                threat = max(threat, gain(r2, c2, ch2))
            if threat >= pick_threat:
                break
        grid[r][c] = ""
        if threat < pick_threat:
            pick_threat, pick = threat, (r, c, ch)
            if not threat:
                break
    return pick


def bot_step(g, words, level):
    if g["started"] and not g["over"] and g["turn"] == "B" and time.time() - g["turn_start"] >= 0.8:
        m = bot_pick(g, words, level)
        if m:
            place(g, words, *m)


# ---------------------------------------------------------------- helpers
def name_for(p):
    if ss.mode == "local":
        return f"Player {p}"
    if ss.mode == "bot":
        return "You" if p == "A" else "Bot"
    return "You" if p == ss.seat else "Opponent"


def label_for(p):
    return f"Player {p}" if ss.mode == "local" else f"{name_for(p)} ({p})"


def can_act(g, has_guest):
    if not g or not g["started"] or g["over"]:
        return False
    if ss.mode == "local":
        return True
    if ss.mode == "bot":
        return g["turn"] == "A"
    return g["turn"] == ss.seat and has_guest


def current():
    """(game, lock, room) for the active mode. game is None if the room is gone."""
    S = shared()
    if ss.mode == "online":
        room = S["rooms"].get(ss.room)
        return (room["game"] if room else None), S["lock"], room
    return ss.game, nullcontext(), None


def make_game():
    g = new_game(ss.rows_w, ss.cols_w, ss.minw_w)
    g["extra"] = set(ss.extra)
    return g


# ---------------------------------------------------------------- callbacks
def cb_start_local():
    ss.mode, ss.game, ss.sel, ss.view = "local", make_game(), None, "game"


def cb_start_bot():
    ss.mode, ss.game, ss.sel, ss.view = "bot", make_game(), None, "game"
    ss.bot_level = ss.level_w


def cb_create():
    S = shared()
    with S["lock"]:
        for k in [k for k, v in S["rooms"].items() if time.time() - v["created"] > 6 * 3600]:
            del S["rooms"][k]
        code = "".join(random.choice(CODE_CHARS) for _ in range(5))
        while code in S["rooms"]:
            code = "".join(random.choice(CODE_CHARS) for _ in range(5))
        S["rooms"][code] = {"game": make_game(), "A": ss.token, "B": None, "created": time.time()}
    ss.mode, ss.room, ss.seat, ss.sel, ss.view = "online", code, "A", None, "game"
    ss.net_msg = ""
    st.query_params.update({"room": code, "t": ss.token})


def cb_join():
    code = (ss.get("join_w") or "").strip().upper()
    S = shared()
    with S["lock"]:
        room = S["rooms"].get(code)
        if len(code) != 5 or not room:
            ss.net_msg = "Room not found. Check the code and try again."
            return
        if room["A"] == ss.token:
            ss.net_msg = "You created this room. Share the code with your friend."
            return
        if room["B"] not in (None, ss.token):
            ss.net_msg = "That room already has two players."
            return
        room["B"] = ss.token
    ss.mode, ss.room, ss.seat, ss.sel, ss.view = "online", code, "B", None, "game"
    ss.net_msg = ""
    st.query_params.update({"room": code, "t": ss.token})


def cb_select(r, c):
    g, lock, room = current()
    with lock:
        if g and can_act(g, bool(room and room["B"]) or ss.mode != "online") and not g["grid"][r][c]:
            ss.sel = (r, c)


def cb_letter(ch):
    g, lock, room = current()
    with lock:
        if g and ss.sel and can_act(g, bool(room and room["B"]) or ss.mode != "online"):
            r, c = ss.sel
            if not g["grid"][r][c]:
                place(g, base_dictionary() | g["extra"], r, c, ch)
    ss.sel = None


def is_host():
    return ss.mode != "online" or ss.seat == "A"


def cb_start():
    g, lock, room = current()
    with lock:
        if g and is_host() and not g["started"] and not g["over"] and (ss.mode != "online" or room["B"]):
            g["started"], g["turn_start"] = True, time.time()


def cb_reset():
    g, lock, room = current()
    with lock:
        if g and is_host():
            fresh = new_game(g["rows"], g["cols"], g["min_word"])
            fresh["extra"] = g["extra"]
            g.clear()
            g.update(fresh)
    ss.sel = None


def cb_leave():
    g, lock, room = current()
    if ss.mode == "online" and room is not None:
        with lock:
            if ss.seat == "A":
                shared()["rooms"].pop(ss.room, None)
            else:
                room["B"] = None
                fresh = new_game(g["rows"], g["cols"], g["min_word"])
                fresh["extra"] = g["extra"]
                g.clear()
                g.update(fresh)
    st.query_params.clear()
    ss.view, ss.mode, ss.room, ss.seat, ss.sel, ss.game = "menu", "local", None, None, None, None


def cb_add_words():
    ss.extra |= parse_words(ss.custom_w)
    ss.custom_w = ""


# ---------------------------------------------------------------- keyboard bridge
# Streamlit cannot read key presses, so this small script (a) listens for key presses on
# desktop and (b) opens the phone keyboard when you tap a cell. Either way it just "taps"
# the matching A-Z button, so all the game logic stays in Python.
KEY_JS = """
<script>
(function(){
  const P=window.parent, D=P.document;
  if(P.__wgdCleanup){try{P.__wgdCleanup()}catch(e){}}
  let box=D.getElementById('wgd-key');
  if(!box){
    box=D.createElement('input');
    box.id='wgd-key'; box.type='text';
    box.setAttribute('autocapitalize','characters'); box.setAttribute('autocomplete','off');
    box.setAttribute('autocorrect','off'); box.setAttribute('spellcheck','false');
    box.setAttribute('aria-label','Type a letter');
    box.style.cssText='position:fixed;bottom:0;left:0;width:1px;height:1px;opacity:0;border:0;padding:0;font-size:16px;z-index:-1';
    D.body.appendChild(box);
  }
  function press(letter,tries){
    const L=letter.toUpperCase();
    const btn=[...D.querySelectorAll('.st-key-pad button')].find(b=>b.textContent.trim()===L);
    if(btn&&!btn.disabled){btn.click();return}
    if(tries>0)setTimeout(()=>press(letter,tries-1),100);
  }
  function onInput(){const v=box.value;box.value='';const m=v.match(/[a-zA-Z]/g);if(m)press(m[m.length-1],25)}
  function onClick(e){if(e.target.closest&&e.target.closest('.st-key-board button')){box.focus({preventScroll:true})}}
  function onKey(e){
    const t=e.target;
    if(t===box||/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)||t.isContentEditable)return;
    if(e.ctrlKey||e.metaKey||e.altKey)return;
    if(/^[a-zA-Z]$/.test(e.key)){e.preventDefault();press(e.key,25)}
  }
  box.addEventListener('input',onInput);
  D.addEventListener('click',onClick,true);
  D.addEventListener('keydown',onKey,true);
  P.__wgdCleanup=function(){box.removeEventListener('input',onInput);D.removeEventListener('click',onClick,true);D.removeEventListener('keydown',onKey,true)};
})();
</script>
"""


# ---------------------------------------------------------------- views
CSS = """
<style>
.st-key-board [data-testid="stHorizontalBlock"],.st-key-pad [data-testid="stHorizontalBlock"],
.st-key-controls [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:.3rem!important}
.st-key-board [data-testid="stColumn"],.st-key-pad [data-testid="stColumn"],
.st-key-controls [data-testid="stColumn"]{min-width:0!important;flex:1 1 0!important;width:auto!important}
.st-key-board button{aspect-ratio:1/1;width:100%;min-height:0;padding:0;font-size:clamp(1.1rem,4.5vw,2rem);font-weight:800}
.st-key-board button:disabled{opacity:1;cursor:default}
.st-key-pad button{padding:.3rem 0;min-height:0;font-weight:700}
.scorebar{display:grid;grid-template-columns:1fr auto 1fr;gap:.5rem;margin:.25rem 0 .5rem}
.sc{border-radius:16px;padding:.6rem .8rem;display:flex;justify-content:space-between;align-items:center;border:2px solid transparent}
.sc b{font-size:1.8rem}.sc.a{background:#eef2ff;color:#4338ca}.sc.b{background:#ecfeff;color:#0891b2}.sc.on{border-color:currentColor}
.tm{background:#020617;color:#fff;border-radius:16px;padding:.5rem .9rem;text-align:center;line-height:1.2}
.tm b{font-size:1.6rem;display:block}.tm.danger{background:#dc2626}
.st-key-board button{background:#fff;color:#0f172a;border:2px solid #a5b4fc;border-radius:12px}
.st-key-board button p{color:inherit}
.st-key-board button:not(:disabled):hover{background:#e0e7ff;border-color:#4f46e5}
.st-key-board button:disabled{background:#eef2ff;border-color:#c7d2fe;color:#0f172a!important;opacity:1}
.st-key-pad button{background:#fff;color:#94a3b8;border:2px solid #e2e8f0;border-radius:10px;font-size:1rem}
.st-key-pad button:not(:disabled){color:#4338ca;border-color:#4f46e5}
.st-key-pad button:not(:disabled):hover{background:#e0e7ff}
.hint{background:#e0e7ff;color:#312e81;border-radius:12px;padding:.5rem .8rem;font-weight:600;margin:.4rem 0}
.chip{display:inline-block;background:#dcfce7;color:#166534;border-radius:999px;padding:.15rem .6rem;margin:.1rem;font-weight:700}
</style>
"""


def menu_view():
    st.title("🔤 Word Grid Duel")
    st.caption("Take turns placing one letter. Complete a dictionary word in a row, column or diagonal to score.")
    c1, c2, c3 = st.columns(3)
    c1.slider("Rows", 3, 9, 5, key="rows_w")
    c2.slider("Columns", 3, 9, 5, key="cols_w")
    c3.slider("Minimum word length", 3, 8, 3, key="minw_w")

    t_local, t_bot, t_online = st.tabs(["Same device", "Play vs bot", "Online"])
    with t_local:
        st.write("Two players share one screen.")
        st.button("Start game", on_click=cb_start_local, key="m_local", type="primary")
    with t_bot:
        st.selectbox("Bot level", ["Easy", "Normal", "Hard"], index=1, key="level_w")
        st.button("Start game", on_click=cb_start_bot, key="m_bot", type="primary")
    with t_online:
        st.write("Create a room and share the 5-letter code, or join a friend's room.")
        st.button("Create room", on_click=cb_create, key="m_create", type="primary")
        st.divider()
        st.text_input("Room code", max_chars=5, key="join_w")
        st.button("Join room", on_click=cb_join, key="m_join")
        if ss.net_msg:
            st.error(ss.net_msg)

    with st.expander(f"Dictionary ({len(base_dictionary()) + len(ss.extra):,} words)"):
        up = st.file_uploader("Import a word list (.txt)", type=["txt"], key="dict_w")
        if up is not None:
            ss.extra |= parse_words(up.getvalue().decode("utf-8", errors="ignore"))
        st.text_area("Add custom words", placeholder="REACT, CODE, INDIA, GRID", key="custom_w")
        st.button("Add words", on_click=cb_add_words, key="m_add")
        st.caption("Imported and custom words apply to games you start or host.")


@st.fragment(run_every=1)
def game_view():
    g, lock, room = current()
    if g is None:
        st.warning("This room no longer exists.")
        st.button("Back to menu", on_click=cb_leave, key="gone")
        return
    with lock:
        maybe_timeout(g)
        if ss.mode == "bot":
            bot_step(g, base_dictionary() | g["extra"], ss.bot_level)
        snap = copy.deepcopy(g)
        has_guest = room["B"] is not None if room else True
    started, over = snap["started"], snap["over"]
    act = can_act(snap, has_guest)
    if ss.sel and (not act or snap["grid"][ss.sel[0]][ss.sel[1]]):
        ss.sel = None

    if ss.mode == "online":
        st.markdown(f"**Room `{ss.room}`** · you are Player {ss.seat}")

    remaining = max(0, int(LIMIT - (time.time() - snap["turn_start"]) + 0.999)) if started and not over else LIMIT
    danger = " danger" if started and not over and remaining <= 5 else ""
    on = lambda p: " on" if started and not over and snap["turn"] == p else ""
    st.markdown(
        f'<div class="scorebar"><div class="sc a{on("A")}"><span>{label_for("A")}</span><b>{snap["scores"]["A"]}</b></div>'
        f'<div class="tm{danger}"><span>Turn</span><b>{snap["turn"]}</b><small>{remaining}s left</small></div>'
        f'<div class="sc b{on("B")}"><span>{label_for("B")}</span><b>{snap["scores"]["B"]}</b></div></div>',
        unsafe_allow_html=True)
    st.progress(remaining / LIMIT)

    if ss.mode == "online" and not has_guest:
        status = f"Waiting for an opponent. Share room code {ss.room}."
    elif not started and not over:
        status = "Waiting for the host to start the game." if not is_host() else "Press Start game to begin."
    elif over:
        status = "Game over."
    elif ss.mode == "bot" and snap["turn"] == "B":
        status = "Bot is thinking…"
    elif ss.mode == "online" and snap["turn"] != ss.seat:
        status = "Opponent's turn…"
    elif ss.sel:
        status = f"Cell ({ss.sel[0] + 1}, {ss.sel[1] + 1}) selected. Pick a letter."
    else:
        status = f"Player {snap['turn']}: pick an empty cell." if ss.mode == "local" else "Your turn: pick an empty cell."
    st.markdown(f"**{status}**")

    left, right = st.columns([3, 2])
    with left:
        rules = [f".st-key-cell_{r}_{c} button{{background:#bbf7d0!important;border-color:#16a34a!important;color:#14532d!important}}"
                 for _, cells in snap["last"] for r, c in cells]
        if ss.sel:
            rules.append(f".st-key-cell_{ss.sel[0]}_{ss.sel[1]} button{{background:#4f46e5!important;"
                         "border-color:#312e81!important;color:#fff!important}")
        st.markdown("<style>" + "".join(rules) + "</style>", unsafe_allow_html=True)
        if act:
            hint = (f"Selected row {ss.sel[0] + 1}, column {ss.sel[1] + 1}. Type a letter on your keyboard or tap one below."
                    if ss.sel else "Tap an empty square, then type a letter or tap one below.")
            st.markdown(f'<div class="hint">{hint}</div>', unsafe_allow_html=True)
        with st.container(key="board"):
            for r in range(snap["rows"]):
                row = st.columns(snap["cols"])
                for c in range(snap["cols"]):
                    ch = snap["grid"][r][c]
                    sel = ss.sel == (r, c)
                    row[c].button(ch or ("?" if sel else "\u200b"), key=f"cell_{r}_{c}",
                                  on_click=cb_select, args=(r, c),
                                  disabled=bool(ch) or not act, use_container_width=True)
        with st.container(key="pad"):
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            for i in range(0, 26, 9):
                cols_ = st.columns(9)
                for col, ch in zip(cols_, alphabet[i:i + 9]):
                    col.button(ch, key=f"pad_{ch}", on_click=cb_letter, args=(ch,),
                               disabled=not (ss.sel and act), use_container_width=True)
        with st.container(key="controls"):
            b1, b2, b3 = st.columns(3)
            b1.button("▶ Start", on_click=cb_start, key="start", use_container_width=True,
                      disabled=started or over or not is_host() or not has_guest)
            b2.button("↻ Reset", on_click=cb_reset, key="reset", use_container_width=True, disabled=not is_host())
            b3.button("Leave", on_click=cb_leave, key="leave", use_container_width=True)
        if over:
            a, b = snap["scores"]["A"], snap["scores"]["B"]
            if a == b:
                st.success(f"It's a draw! Final score: A {a} — B {b}")
            else:
                w = "A" if a > b else "B"
                who = name_for(w)
                st.success(f"{who} {'win' if who == 'You' else 'wins'}! Final score: A {a} — B {b}")
    with right:
        st.subheader("Completed words")
        st.markdown("".join(f'<span class="chip">+1 {w}</span>' for w, _ in snap["last"])
                    or "No new word on the latest move.", unsafe_allow_html=True)
        st.subheader("Move history")
        if not snap["history"]:
            st.caption("Select a cell, then pick a letter.")
        for p, text, detail in snap["history"]:
            st.markdown(f"**{name_for(p) if p else 'System'}**: {text}  \n<small>{detail}</small>",
                        unsafe_allow_html=True)
        st.caption(f"{snap['rows']} × {snap['cols']} board · minimum word length {snap['min_word']}")


# ---------------------------------------------------------------- main
def init_state():
    if "init" in ss:
        return
    ss.init = True
    ss.view, ss.mode, ss.game, ss.room, ss.seat = "menu", "local", None, None, None
    ss.sel, ss.extra, ss.net_msg, ss.bot_level = None, set(), "", "Normal"
    ss.token = uuid.uuid4().hex
    # Resume an online game after a page refresh or reconnect.
    code, tok = st.query_params.get("room"), st.query_params.get("t")
    room = shared()["rooms"].get(code) if code else None
    if room and tok:
        for seat in ("A", "B"):
            if room[seat] == tok:
                ss.token, ss.mode, ss.room, ss.seat, ss.view = tok, "online", code, seat, "game"


st.markdown(CSS, unsafe_allow_html=True)
init_state()
if ss.view == "menu":
    menu_view()
else:
    game_view()
    components.html(KEY_JS, height=0)
