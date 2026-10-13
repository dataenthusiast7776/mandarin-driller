import hashlib, hmac, html, json, os, random, re, secrets, time
from collections import Counter
from datetime import date, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import create_engine, text

st.set_page_config(page_title="中文 Character Driller", page_icon="🌱", layout="wide")
S = st.session_state
TILES = ["#2a3040", "#b91c1c", "#c2410c", "#ca8a04", "#65a30d", "#16a34a", "#22c55e"]
st.markdown("""<style>.t{width:38px;height:38px;border-radius:6px;display:inline-flex;align-items:center;
justify-content:center;font-size:18px;color:#fff;margin:2px}</style>""", unsafe_allow_html=True)


# ------------------------------------------------------------ database (Postgres in prod, SQLite locally)
@st.cache_resource
def db():
    try:
        url = st.secrets["DATABASE_URL"]
    except Exception:
        url = "sqlite:///driller.db"
    url = re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)   # always use psycopg 3
    eng = create_engine(url, pool_pre_ping=True)
    with eng.begin() as c:
        for q in ("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, pw TEXT NOT NULL, goal INTEGER DEFAULT 30, sprint_best INTEGER DEFAULT 0)",
                  "CREATE TABLE IF NOT EXISTS chars (username TEXT, ch TEXT, data TEXT, PRIMARY KEY (username, ch))",
                  "CREATE TABLE IF NOT EXISTS days (username TEXT, day TEXT, n INTEGER, PRIMARY KEY (username, day))"):
            c.execute(text(q))
    return eng


def run(q, **p):
    with db().begin() as c:
        c.execute(text(q), p)


def rows(q, **p):
    with db().connect() as c:
        return c.execute(text(q), p).fetchall()


@st.cache_resource
def dictionary():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictionary.json")
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}


# ------------------------------------------------------------ accounts
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def login(u, pw):
    r = rows("SELECT pw FROM users WHERE username=:u", u=u)
    if not r or not hmac.compare_digest(hash_pw(pw, r[0][0].split("$")[0]), r[0][0]):
        return False
    load_user(u); return True


def signup(u, pw):
    if rows("SELECT 1 FROM users WHERE username=:u", u=u):
        return False
    run("INSERT INTO users (username, pw) VALUES (:u,:p)", u=u, p=hash_pw(pw))
    load_user(u); return True


def load_user(u):
    chars = {r[0]: json.loads(r[1]) for r in rows("SELECT ch, data FROM chars WHERE username=:u", u=u)}
    S.chars = dict(sorted(chars.items(), key=lambda kv: kv[1].get("added", 0)))
    S.days = {r[0]: r[1] for r in rows("SELECT day, n FROM days WHERE username=:u", u=u)}
    g, b = rows("SELECT goal, sprint_best FROM users WHERE username=:u", u=u)[0]
    S.user, S.goal, S.best, S.page, S.drill = u, g or 30, b or 0, "home", None


def save_char(ch):
    run("INSERT INTO chars (username,ch,data) VALUES (:u,:c,:d) ON CONFLICT (username,ch) DO UPDATE SET data=excluded.data",
        u=S.user, c=ch, d=json.dumps(S.chars[ch], ensure_ascii=False))


def save_meta():
    run("UPDATE users SET goal=:g, sprint_best=:b WHERE username=:u", g=S.goal, b=S.best, u=S.user)


# ------------------------------------------------------------ pinyin
VOW = {"a": "āáǎà", "e": "ēéěè", "i": "īíǐì", "o": "ōóǒò", "u": "ūúǔù", "v": "ǖǘǚǜ"}
TONE_MARKS = {m: (b, i + 1) for b, ms in VOW.items() for i, m in enumerate(ms)}


def normalize_pinyin(t):
    t = t.strip().lower().replace("ü", "v").replace(" ", "")
    m = re.search(r"([0-5])$", t)
    if m:
        n = int(m.group(1)); return t[:-1], (0 if n == 5 else n)
    for mark, (base, n) in TONE_MARKS.items():
        if mark in t:
            return t.replace(mark, base), n
    return t, None


def grade_pinyin(user, e):
    base, tone = normalize_pinyin(user)
    if base not in [e["pinyin"].lower()] + [a.lower() for a in e.get("alt", [])]:
        return "no"
    want = e.get("tone")
    if want in (0, None):
        return "ok" if tone in (None, 0) else "tone"
    return "ok" if tone == want else "tone"


def display_pinyin(e):
    p, t = e["pinyin"], e.get("tone")
    if not t:
        return p.replace("v", "ü")
    i = -1
    for c in "ae":
        if c in p:
            i = p.index(c); break
    else:
        if "ou" in p:
            i = p.index("o")
        else:
            for k in range(len(p) - 1, -1, -1):
                if p[k] in "iouv":
                    i = k; break
    return p if i < 0 else p[:i] + VOW[p[i]][t - 1] + p[i + 1:]


def soundalikes(ch):
    p = S.chars[ch]["pinyin"]
    return [f"{c} {display_pinyin(e)}" for c, e in S.chars.items() if c != ch and e["pinyin"] == p]


# ------------------------------------------------------------ learning model
def tier(e):
    if not (e.get("correct", 0) + e.get("incorrect", 0)):
        return "New"
    s = e.get("strength", 0)
    return "Seedling" if s <= 3 else "Growing" if s <= 7 else "Mastered"


def tile_color(e):
    return TILES[0] if tier(e) == "New" else TILES[1 + min(5, e.get("strength", 0) // 2)]


def weight(e):
    if not (e.get("correct", 0) + e.get("incorrect", 0)):
        return 12
    w = max(1, 12 - e.get("strength", 0)) + e.get("incorrect", 0) * 1.5
    return max(1, w - min(e.get("streak", 0) * 0.2, 3))


def choose_character(last=None):
    chars = list(S.chars)
    w = [weight(S.chars[c]) for c in chars]
    if len(chars) > 1 and last in chars:
        w[chars.index(last)] *= 0.05
    return random.choices(chars, weights=w, k=1)[0]


def update_character(ch, res, typed=""):
    e = S.chars[ch]
    if res == "ok":
        e["correct"] = e.get("correct", 0) + 1; e["streak"] = e.get("streak", 0) + 1
        e["strength"] = min(10, e.get("strength", 0) + 1)
    else:
        e["incorrect"] = e.get("incorrect", 0) + 1; e["streak"] = 0
        e["strength"] = max(0, e.get("strength", 0) - (1 if res == "tone" else 3))
        if typed:
            w = e.setdefault("wrong", {}); w[typed[:24]] = w.get(typed[:24], 0) + 1
    day = str(date.today()); S.days[day] = S.days.get(day, 0) + 1
    save_char(ch)
    run("INSERT INTO days (username,day,n) VALUES (:u,:d,:n) ON CONFLICT (username,day) DO UPDATE SET n=excluded.n",
        u=S.user, d=day, n=S.days[day])


def day_streak():
    d = date.today()
    if not S.days.get(str(d)):
        d -= timedelta(days=1)
    n = 0
    while S.days.get(str(d)):
        n += 1; d -= timedelta(days=1)
    return n


def plant(ch, entry=None):
    e = entry or dictionary()[ch]
    new = {"pinyin": e["pinyin"], "tone": e["tone"], "meaning": e["meaning"], "strength": 0,
           "correct": 0, "incorrect": 0, "streak": 0, "added": time.time()}
    if e.get("alt"):
        new["alt"] = e["alt"]
    S.chars[ch] = new; save_char(ch)


def go(page):
    S.page = page


# ------------------------------------------------------------ drill engine
def start(mode, pool=None):
    if len(S.chars) < (4 if mode == "meaning" else 1):
        S.msg = "Plant at least 4 characters for the Meaning Quiz (1 for the others)."; return
    S.drill = dict(mode=mode, q=0, ok=0, streak=0, best=0, missed=[], last=None, fb=None, answered=False,
                   queue=random.sample(pool, len(pool)) if pool else None,
                   end=time.time() + 60 if mode == "sprint" else None)
    advance(); S.page = "drill"


def advance():
    d = S.drill
    if d["queue"] is not None:
        if not d["queue"]:
            d["done"] = True; return
        ch = d["queue"].pop()
    else:
        ch = choose_character(d["last"])
    d["cur"] = d["last"] = ch; d["answered"] = False
    if d["mode"] == "meaning":
        want = S.chars[ch]["meaning"]
        others = list({v["meaning"] for v in S.chars.values()} - {want})
        d["opts"] = random.sample(others, min(3, len(others))) + [want]; random.shuffle(d["opts"])


def next_q():
    advance(); S.drill["fb"] = None


def grade(res, typed):
    d = S.drill; ch = d["cur"]; e = S.chars[ch]; d["q"] += 1
    update_character(ch, res, typed)
    if res == "ok":
        d["ok"] += 1; d["streak"] += 1; d["best"] = max(d["best"], d["streak"])
        msg, color = f"✓ Correct   {display_pinyin(e)}", "#4ade80"
    else:
        d["streak"] = 0
        if ch not in d["missed"]:
            d["missed"].append(ch)
        msg, color = ((f"◐ Right sound, wrong tone → {display_pinyin(e)}", "#facc15") if res == "tone"
                      else (f"✗ {display_pinyin(e)}", "#f87171"))
    twins = soundalikes(ch)
    d["fb"] = (f"{ch}  {msg}", color, e["meaning"] + (("  ·  Sound-alikes: " + "   ".join(twins[:5])) if twins else ""))
    if d["mode"] == "sprint":
        advance()            # sprint never waits; last result stays visible above the next character
    else:
        d["answered"] = True


def pick(opt):
    d = S.drill
    if not d["answered"]:
        grade("ok" if opt == S.chars[d["cur"]]["meaning"] else "no", opt)


def finish():
    d = S.drill
    if d["mode"] == "sprint" and d["ok"] > S.best:
        S.best = d["ok"]; save_meta(); d["record"] = True
    S.page = "summary"; st.rerun()


@st.fragment(run_every=1)
def countdown():
    left = S.drill["end"] - time.time()
    if left <= 0:
        st.rerun()
    st.metric("Time", f"⏱ {left:.0f}s")


# ------------------------------------------------------------ pages
def auth_page():
    st.title("中文 Character Driller")
    st.caption("Grow your characters, don't just flip cards")
    a, b = st.tabs(["Log in", "Sign up"])
    with a, st.form("login"):
        u, p = st.text_input("Username"), st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary"):
            if login(u.strip().lower(), p):
                st.rerun()
            st.error("Wrong username or password.")
    with b, st.form("signup"):
        u, p = st.text_input("Choose a username"), st.text_input("Choose a password (8+ chars)", type="password")
        if st.form_submit_button("Create account", type="primary"):
            u = u.strip().lower()
            if not re.fullmatch(r"[a-z0-9_]{3,24}", u) or len(p) < 8:
                st.error("Username: 3-24 letters/numbers/underscore. Password: 8+ characters.")
            elif signup(u, p):
                st.rerun()
            else:
                st.error("That username is taken.")


def home_page():
    st.title("中文 Character Driller")
    if S.get("msg"):
        st.warning(S.pop("msg"))
    today, goal = S.days.get(str(date.today()), 0), max(1, S.goal)
    c1, c2 = st.columns([3, 1])
    c1.progress(min(1.0, today / goal), text=f"Today {today}/{goal}")
    c2.markdown(f"**🔥 {day_streak()}-day streak**")
    st.subheader(f"Your garden · {len(S.chars)} characters")
    if not S.chars:
        st.info("Your garden is empty. Open **Find & Plant** to search the dictionary and add what you've learned.")
    tiles = "".join(
        f"<div class='t' style='background:{tile_color(e)}' title=\"{html.escape(c + ' ' + display_pinyin(e) + ' · ' + e['meaning'] + ' · ' + tier(e))}\">{c}</div>"
        for c, e in S.chars.items())
    st.markdown(tiles, unsafe_allow_html=True)
    st.caption("Hover a tile for details · grey = new, red = weak, green = strong")
    b1, b2, b3 = st.columns(3)
    b1.button("▶  Pinyin Drill", on_click=start, args=("pinyin",), type="primary", use_container_width=True)
    b2.button("◉  Meaning Quiz", on_click=start, args=("meaning",), type="primary", use_container_width=True)
    b3.button("⚡  60s Sprint", on_click=start, args=("sprint",), type="primary", use_container_width=True)


def drill_page():
    d = S.drill
    if not d:
        S.page = "home"; st.rerun()
    if d.get("done") or (d["end"] and time.time() >= d["end"]):
        finish()
    m1, m2, m3 = st.columns(3)
    m1.metric("Correct", f"{d['ok']}/{d['q']}")
    m2.metric("Streak", f"🔥 {d['streak']}")
    with m3:
        countdown() if d["end"] else st.button("Finish session", on_click=go, args=("summary",))
    e = S.chars[d["cur"]]
    st.markdown(f"<div style='text-align:center;font-size:120px;line-height:1.3'>{d['cur']}</div>"
                f"<div style='text-align:center;color:#8b95a9;font-size:12px'>{tier(e).upper()}</div>", unsafe_allow_html=True)
    if d["fb"]:
        msg, color, detail = d["fb"]
        st.markdown(f"<div style='text-align:center;font-size:22px;font-weight:700;color:{color}'>{msg}</div>"
                    f"<div style='text-align:center;color:#8b95a9'>{html.escape(detail)}</div>", unsafe_allow_html=True)
    if d["answered"]:
        st.button("Next  →", on_click=next_q, type="primary", use_container_width=True)
    elif d["mode"] == "meaning":
        cols = st.columns(2)
        for i, o in enumerate(d["opts"]):
            cols[i % 2].button(o[:60], key=f"o{d['q']}_{i}", on_click=pick, args=(o,), use_container_width=True)
    else:
        with st.form("ans", clear_on_submit=True, border=False):
            t = st.text_input("pinyin", label_visibility="collapsed", placeholder="xué  /  xue2  /  de5  (neutral: just 'de')")
            sub = st.form_submit_button("Check", type="primary", use_container_width=True)
        if sub and t.strip():
            grade(grade_pinyin(t, e), t.strip()); st.rerun()
        components.html("<script>window.parent.document.querySelector('input[type=text]')?.focus()</script>", height=0)


def summary_page():
    d = S.drill
    if not d:
        S.page = "home"; st.rerun()
    st.title("Session complete" + (" 🏆 New sprint record!" if d.get("record") else ""))
    acc = 100 * d["ok"] / d["q"] if d["q"] else 0
    a, b, c, e = st.columns(4)
    a.metric("Questions", d["q"]); b.metric("Correct", d["ok"]); c.metric("Accuracy", f"{acc:.0f}%"); e.metric("Best streak", f"🔥 {d['best']}")
    if d["mode"] == "sprint":
        st.caption(f"Sprint record: {S.best}")
    if d["missed"]:
        st.markdown("**Missed:** " + "  ".join(f"{c} {display_pinyin(S.chars[c])}" for c in d["missed"] if c in S.chars))
        m = "pinyin" if d["mode"] == "sprint" else d["mode"]
        st.button(f"Retry {len(d['missed'])} missed", on_click=start, args=(m, [c for c in d["missed"] if c in S.chars]), type="primary")
    st.button("Drill again", on_click=start, args=(d["mode"],))
    st.button("← Home", on_click=go, args=("home",))


def search(q, limit=30):
    D = dictionary(); base, tone = normalize_pinyin(q)
    hits = [c for c in dict.fromkeys(q) if c in D]
    hits += [c for c, e in D.items() if e["pinyin"] == base and tone in (None, e["tone"])]
    if q.isascii():
        hits += [c for c, e in D.items() if q.lower() in e["meaning"].lower()]
    return list(dict.fromkeys(hits))[:limit]


def find_page():
    st.title("🔎 Find & Plant")
    D = dictionary()
    if not D:
        st.warning("dictionary.json not found. Run build_dictionary.py and commit the file.")
    t1, t2, t3, t4 = st.tabs(["Search dictionary", "Paste characters", "Custom entry", "Import / Export"])
    with t1:
        q = st.text_input("Search by character, pinyin (xue / xue2 / xué) or English").strip()
        for c in (search(q) if q else []):
            e = D[c]; a, b, m, f = st.columns([1, 2, 6, 2])
            a.markdown(f"## {c}"); b.markdown(f"**{display_pinyin(e)}**"); m.write(e["meaning"])
            if c in S.chars:
                f.caption("✓ in garden")
            else:
                f.button("🌱 Plant", key=f"p_{c}", on_click=plant, args=(c,))
    with t2:
        txt = st.text_area("Paste anything you've learned (a word list, a sentence, whatever)")
        if st.button("Plant them all", type="primary"):
            uniq = [c for c in dict.fromkeys(txt) if "\u4e00" <= c <= "\u9fff"]
            new = [c for c in uniq if c in D and c not in S.chars]
            for c in new:
                plant(c)
            miss = [c for c in uniq if c not in D]
            st.success(f"Planted {len(new)} new characters.")
            if miss:
                st.warning("Not in dictionary (use Custom entry): " + " ".join(miss))
    with t3, st.form("custom", clear_on_submit=True):
        c, p, m, alt = (st.text_input("Character"), st.text_input("Pinyin with tone (xue2, xué, de0 = neutral)"),
                        st.text_input("Meaning"), st.text_input("Other accepted spellings (optional, comma-separated, e.g. shir)"))
        if st.form_submit_button("Add", type="primary"):
            base, tone = normalize_pinyin(p)
            if not (c.strip() and m.strip()) or tone is None:
                st.error("Fill every field and include a tone.")
            elif c.strip() in S.chars:
                st.error("Already in your garden.")
            else:
                plant(c.strip(), {"pinyin": base, "tone": tone, "meaning": m.strip(),
                                  "alt": [a.strip() for a in alt.split(",") if a.strip()]})
                st.success(f"Planted {c.strip()}")
    with t4:
        up = st.file_uploader("Import your characters.json (keeps your progress)", type="json")
        over = st.checkbox("Overwrite characters already in my garden")
        if up and st.button("Import"):
            data, n = json.load(up), 0
            for i, (c, e) in enumerate(data.items()):
                if c in S.chars and not over:
                    continue
                e.setdefault("added", time.time() + i * 1e-3); S.chars[c] = e; save_char(c); n += 1
            st.success(f"Imported {n} characters.")
        st.download_button("⬇ Export my garden (characters.json)", json.dumps(S.chars, ensure_ascii=False, indent=2),
                           "characters.json", "application/json")


def bank_page():
    st.title("☷ Character Bank")
    q = st.text_input("Filter").lower()
    df = pd.DataFrame([{"Char": c, "Pinyin": display_pinyin(e), "Meaning": e["meaning"], "Stage": tier(e),
                        "✓": e.get("correct", 0), "✗": e.get("incorrect", 0)} for c, e in S.chars.items()])
    if q and not df.empty:
        df = df[df.apply(lambda r: q in " ".join(map(str, r)).lower(), axis=1)]
    sel = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="multi-row")
    picked = [df.iloc[i]["Char"] for i in sel.selection.rows]
    if picked and st.checkbox(f"Yes, delete {len(picked)} selected (their progress is lost)") and st.button("🗑 Delete"):
        for c in picked:
            run("DELETE FROM chars WHERE username=:u AND ch=:c", u=S.user, c=c); S.chars.pop(c, None)
        st.rerun()


def insights_page():
    st.title("▣ Insights")
    cs = list(S.chars.values())
    ok, bad = sum(e.get("correct", 0) for e in cs), sum(e.get("incorrect", 0) for e in cs)
    t = Counter(tier(e) for e in cs)
    a, b, c, d = st.columns(4)
    a.metric("Characters", len(cs)); b.metric("Answers", ok + bad)
    c.metric("Accuracy", f"{100 * ok / (ok + bad):.0f}%" if ok + bad else "–"); d.metric("Sprint record", S.best)
    st.write(f"🌳 Mastered **{t['Mastered']}** · 🌿 Growing **{t['Growing']}** · 🌱 Seedlings **{t['Seedling']}** · ⚪ New **{t['New']}**")
    days = [date.today() - timedelta(days=13 - i) for i in range(14)]
    st.caption("LAST 14 DAYS")
    st.bar_chart(pd.DataFrame({"answers": [S.days.get(str(x), 0) for x in days]}, index=[x.strftime("%d %b") for x in days]))
    st.subheader("Trouble spots")
    for c, e in sorted(S.chars.items(), key=lambda kv: -kv[1].get("incorrect", 0))[:5]:
        if e.get("incorrect"):
            w = e.get("wrong", {})
            often = f", you often said “{max(w, key=w.get)}”" if w else ""
            st.write(f"**{c}** {display_pinyin(e)}: missed {e['incorrect']}×{often}")


# ------------------------------------------------------------ router
if "user" not in S:
    auth_page(); st.stop()

with st.sidebar:
    st.markdown(f"### 🌱 {S.user}")
    for label, page in [("🏠 Home", "home"), ("🔎 Find & Plant", "find"), ("☷ Bank", "bank"), ("▣ Insights", "insights")]:
        st.button(label, on_click=go, args=(page,), use_container_width=True)
    g = st.number_input("Daily goal", 5, 500, S.goal, 5)
    if g != S.goal:
        S.goal = g; save_meta()
    if st.button("Log out"):
        S.clear(); st.rerun()

{"home": home_page, "drill": drill_page, "summary": summary_page, "find": find_page,
 "bank": bank_page, "insights": insights_page}[S.page]()
