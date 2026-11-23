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
        "history": [], "words": {"A": [], "B": []}, "started": False, "over": False,
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
        g["words"][p].extend(w for w, _ in found)
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
def begin(mode):
    g = make_game()
    g["started"], g["turn_start"] = True, time.time()
    ss.mode, ss.game, ss.sel, ss.view = mode, g, None, "game"


def cb_start_local():
    begin("local")


def cb_start_bot():
    ss.bot_level = ss.level_w
    begin("bot")


def cb_pick(mode):
    ss.pick, ss.view, ss.net_msg = mode, "setup", ""


def cb_back():
    ss.view, ss.pick, ss.net_msg = "menu", None, ""


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


def cb_play_again():
    g, lock, room = current()
    with lock:
        if g and is_host() and (ss.mode != "online" or (room and room["B"])):
            fresh = new_game(g["rows"], g["cols"], g["min_word"])
            fresh["extra"] = g["extra"]
            g.clear()
            g.update(fresh)
            g["started"], g["turn_start"] = True, time.time()
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
    ss.view, ss.mode, ss.room, ss.seat, ss.sel, ss.game, ss.pick = "menu", "local", None, None, None, None, None


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
.block-container{padding:.8rem 1rem 1rem!important;max-width:1200px!important}
.st-key-board,.st-key-pad,.st-key-controls,.st-key-board [data-testid="stVerticalBlock"],.st-key-pad [data-testid="stVerticalBlock"]{gap:.3rem!important}
.st-key-board,.st-key-pad{width:min(100%,max(300px,calc(100vh - 330px)));width:min(100%,max(300px,calc(100dvh - 330px)));margin-left:auto;margin-right:auto}
.st-key-board button{font-size:clamp(.9rem,2.4vw,1.5rem)}
.st-key-pad button{height:2.1rem;min-height:0;padding:0;font-size:.95rem}
.st-key-controls button{min-height:2.4rem}
.scorebar{margin:.35rem 0 .3rem;gap:.4rem}
.sc{padding:.3rem .7rem;border-radius:12px}.sc b{font-size:1.4rem}
.tm{padding:.25rem .7rem;border-radius:12px}.tm span{font-weight:700}.tm small{display:block}
.roomtag{padding:.45rem .2rem;font-size:.9rem;text-align:center}
.hint{padding:.35rem .7rem;font-size:.92rem;margin:.3rem 0 .5rem}
[data-testid="stProgress"]{margin-top:-.4rem}
.banner{background:linear-gradient(90deg,#4f46e5,#06b6d4);color:#fff;border-radius:18px;padding:1rem;text-align:center;font-size:1.7rem;font-weight:800;margin:.4rem 0}
.res{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin:.6rem 0}
.rc{border-radius:16px;padding:.8rem;text-align:center;border:3px solid transparent}
.rc span{font-weight:700}.rc b{display:block;font-size:2.6rem;line-height:1.1}.rc.win{border-color:currentColor}
.rc.a{background:#eef2ff;color:#4338ca}.rc.b{background:#ecfeff;color:#0891b2}
.rw{margin-top:.4rem}
.final{display:grid;gap:3px;max-width:320px;margin:.8rem auto .2rem}
.fc{aspect-ratio:1;display:flex;align-items:center;justify-content:center;background:#e0e7ff;border-radius:6px;font-weight:800;color:#0f172a;font-size:.9rem}
.fc.hl{background:#bbf7d0;color:#14532d}
.chip{display:inline-block;background:#dcfce7;color:#166534;border-radius:999px;padding:.15rem .6rem;margin:.1rem;font-weight:700}
</style>
"""


def menu_view():
    st.title("🔤 Word Grid Duel")
    st.caption("Step 1 of 2 · Choose how you want to play")
    cards = [("local", "👥", "Same device", "Two players share one screen."),
             ("bot", "🤖", "Play vs bot", "Challenge the computer."),
             ("online", "🌐", "Online", "Two devices, one room code.")]
    for col, (mode, icon, title, desc) in zip(st.columns(3), cards):
        with col.container(border=True):
            st.markdown(f"### {icon} {title}")
            st.write(desc)
            st.button("Choose", key=f"pick_{mode}", on_click=cb_pick, args=(mode,), type="primary",
                      use_container_width=True)

    with st.expander(f"Dictionary ({len(base_dictionary()) + len(ss.extra):,} words)"):
        up = st.file_uploader("Import a word list (.txt)", type=["txt"], key="dict_w")
        if up is not None:
            ss.extra |= parse_words(up.getvalue().decode("utf-8", errors="ignore"))
        st.text_area("Add custom words", placeholder="REACT, CODE, INDIA, GRID", key="custom_w")
        st.button("Add words", on_click=cb_add_words, key="m_add")
        st.caption("Imported and custom words apply to games you start or host.")


def board_settings():
    c1, c2, c3 = st.columns(3)
    c1.slider("Rows", 3, 9, 5, key="rows_w")
    c2.slider("Columns", 3, 9, 5, key="cols_w")
    c3.slider("Minimum word length", 3, 8, 3, key="minw_w")


def setup_view():
    mode = ss.pick
    if mode is None:
        cb_back()
        st.rerun()
    title = {"local": "👥 Same device", "bot": "🤖 Play vs bot", "online": "🌐 Online"}[mode]
    st.button("← Back", on_click=cb_back, key="back")
    st.title(title)
    st.caption("Step 2 of 2 · Game settings")
    if mode == "local":
        board_settings()
        st.button("Start game", on_click=cb_start_local, key="s_local", type="primary")
    elif mode == "bot":
        board_settings()
        st.selectbox("Bot level", ["Easy", "Normal", "Hard"], index=1, key="level_w")
        st.button("Start game", on_click=cb_start_bot, key="s_bot", type="primary")
    else:
        with st.container(border=True):
            st.markdown("#### Create a room")
            board_settings()
            st.button("Create room", on_click=cb_create, key="s_create", type="primary")
        with st.container(border=True):
            st.markdown("#### Join a friend's room")
            st.text_input("Room code", max_chars=5, key="join_w")
            st.button("Join room", on_click=cb_join, key="s_join")
        if ss.net_msg:
            st.error(ss.net_msg)


def result_view(snap, has_guest):
    a, b = snap["scores"]["A"], snap["scores"]["B"]
    win = None if a == b else ("A" if a > b else "B")
    if win is None:
        title = "🤝 It's a draw!"
    else:
        who = name_for(win)
        title = "🏆 You win!" if who == "You" else f"🏆 {who} wins!"
    st.markdown(f'<div class="banner">{title}</div>', unsafe_allow_html=True)

    def card(p):
        words = snap["words"][p]
        chips = "".join(f'<span class="chip">{w}</span>' for w in words) or "<small>No words</small>"
        return (f'<div class="rc {p.lower()}{" win" if win == p else ""}"><span>{label_for(p)}</span>'
                f'<b>{snap["scores"][p]}</b><small>{"point" if snap["scores"][p] == 1 else "points"}</small>'
                f'<div class="rw">{chips}</div></div>')

    st.markdown(f'<div class="res">{card("A")}{card("B")}</div>', unsafe_allow_html=True)

    hi = {cell for _, cells in snap["last"] for cell in cells}
    tiles = "".join(f'<div class="fc{" hl" if (r, c) in hi else ""}">{ch}</div>'
                    for r, row in enumerate(snap["grid"]) for c, ch in enumerate(row))
    st.markdown(f'<div class="final" style="grid-template-columns:repeat({snap["cols"]},1fr)">{tiles}</div>',
                unsafe_allow_html=True)
    st.caption(f"{snap['rows']} × {snap['cols']} board · minimum word length {snap['min_word']}")

    c1, c2 = st.columns(2)
    host = is_host()
    c1.button("▶ Play again", on_click=cb_play_again, key="again", type="primary", use_container_width=True,
              disabled=not host or (ss.mode == "online" and not has_guest))
    c2.button("Main menu", on_click=cb_leave, key="to_menu", use_container_width=True)
    if not host:
        st.caption("Waiting for the host to start another game.")


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

    if over:
        result_view(snap, has_guest)
        return

    # ---- top toolbar: Start / Reset / Leave
    with st.container(key="controls"):
        b1, b2, b3 = st.columns(3)
        if not started and not over:
            b1.button("▶ Start game", on_click=cb_start, key="start", type="primary", use_container_width=True,
                      disabled=not is_host() or not has_guest)
        elif ss.mode == "online":
            b1.markdown(f'<div class="roomtag">Room <b>{ss.room}</b> · Player {ss.seat}</div>', unsafe_allow_html=True)
        b2.button("↻ Reset", on_click=cb_reset, key="reset", use_container_width=True, disabled=not is_host())
        b3.button("Leave", on_click=cb_leave, key="leave", use_container_width=True)

    # ---- scores, timer, status
    remaining = max(0, int(LIMIT - (time.time() - snap["turn_start"]) + 0.999)) if started and not over else LIMIT
    danger = " danger" if started and not over and remaining <= 5 else ""
    on = lambda p: " on" if started and not over and snap["turn"] == p else ""
    st.markdown(
        f'<div class="scorebar"><div class="sc a{on("A")}"><span>{label_for("A")}</span><b>{snap["scores"]["A"]}</b></div>'
        f'<div class="tm{danger}"><span>Turn {snap["turn"]}</span><small>{remaining}s</small></div>'
        f'<div class="sc b{on("B")}"><span>{label_for("B")}</span><b>{snap["scores"]["B"]}</b></div></div>',
        unsafe_allow_html=True)
    st.progress(remaining / LIMIT)

    if ss.mode == "online" and not has_guest:
        status = f"Waiting for an opponent. Share room code {ss.room}."
    elif not started and not over:
        status = "Waiting for the host to start the game." if not is_host() else "Press Start game to begin."
    elif over:
        a, b = snap["scores"]["A"], snap["scores"]["B"]
        if a == b:
            status = f"Game over: it's a draw ({a}–{b})."
        else:
            who = name_for("A" if a > b else "B")
            status = f"Game over: {who} {'win' if who == 'You' else 'wins'}! ({a}–{b})"
    elif ss.mode == "bot" and snap["turn"] == "B":
        status = "Bot is thinking…"
    elif ss.mode == "online" and snap["turn"] != ss.seat:
        status = "Opponent's turn…"
    elif ss.sel:
        status = f"Row {ss.sel[0] + 1}, column {ss.sel[1] + 1} selected. Type a letter or tap one below."
    else:
        who = f"Player {snap['turn']}: tap" if ss.mode == "local" else "Your turn: tap"
        status = f"{who} an empty square, then type a letter or tap one below."
    st.markdown(f'<div class="hint">{status}</div>', unsafe_allow_html=True)

    left, right = st.columns([3, 2])
    with left:
        rules = [f".st-key-cell_{r}_{c} button{{background:#bbf7d0!important;border-color:#16a34a!important;color:#14532d!important}}"
                 for _, cells in snap["last"] for r, c in cells]
        if ss.sel:
            rules.append(f".st-key-cell_{ss.sel[0]}_{ss.sel[1]} button{{background:#4f46e5!important;"
                         "border-color:#312e81!important;color:#fff!important}")
        st.markdown("<style>" + "".join(rules) + "</style>", unsafe_allow_html=True)
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
    with right:
        st.markdown("**Completed words**")
        st.markdown("".join(f'<span class="chip">+1 {w}</span>' for w, _ in snap["last"])
                    or "<small>No new word on the latest move.</small>", unsafe_allow_html=True)
        st.markdown("**Move history**")
        if not snap["history"]:
            st.caption("Select a square, then pick a letter.")
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
    ss.sel, ss.extra, ss.net_msg, ss.bot_level, ss.pick = None, set(), "", "Normal", None
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
elif ss.view == "setup":
    setup_view()
else:
    game_view()
    components.html(KEY_JS, height=0)
