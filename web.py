#!/usr/bin/env python3
"""coord web: live browser view of the coord board.

    python3 <chat folder>/web.py [--port 8780] [--host 127.0.0.1] [--root DIR]

The page (Markdown via marked, LaTeX via KaTeX, both from CDN) loads the newest
messages, pages older ones on demand, searches the whole history on the server,
and is pushed new messages and state changes through Server-Sent Events
(/api/stream), so it never reloads. The board database is opened read-only.
Read-aloud audio is rendered here with the operating system's speech engine
(tts.py) and cached; the page only plays audio files.
Room administration (new room, invite code, kick, close) needs the admin token
printed at startup (open the printed #admin=... URL once; set COORD_WEB_TOKEN to keep it fixed).
"""
import argparse, hmac, json, mimetypes, os, re, secrets, sqlite3, statistics, sys, threading, time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coord  # same folder: shared schema, code_hash, now
import tts

PAGE_VERSION = "5"          # must match PAGE_VERSION in web/index.html; older pages are told to reload
def _token():
    """The admin token: COORD_WEB_TOKEN, else one kept privately next to the board database, so the admin link survives restarts."""
    if os.environ.get("COORD_WEB_TOKEN"):
        return os.environ["COORD_WEB_TOKEN"]
    path = os.path.join(coord.DATA_DIR, coord.PROJECT + ".webtoken")
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        if len(t) >= 16:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(18)
    os.makedirs(coord.DATA_DIR, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # readable by you only
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


TOKEN = _token()
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT, DEFAULT_DB = coord.PROJECT, coord.DEFAULT_DB      # same project folder, name and database as coord.py
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

ROOT = coord.ROOT                                          # project folder (links to files open inside it); override with --root
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".claude"}
TEXT_EXT = {".tex", ".wl", ".wls", ".nb", ".py", ".md", ".txt", ".json", ".csv", ".log", ".cpp", ".hpp", ".sh", ".out", ".aux", ".yml", ".yaml", ".toml", ".m", ".h", ".c"}
_idx = {"t": 0, "names": {}}


def db_path():
    return os.environ.get("COORD_DB", DEFAULT_DB)


VOICES = tts.Assignments(os.path.join(os.path.dirname(db_path()), PROJECT + ".voices.json"))


def index():
    """basename -> relative paths, refreshed every 30 s; used to open bare file names."""
    if time.time() - _idx["t"] > 30:
        names = {}
        for d, dirs, files in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for f in files:
                names.setdefault(f, []).append(os.path.relpath(os.path.join(d, f), ROOT))
        _idx.update(t=time.time(), names=names)
    return _idx["names"]


def resolve(rel):
    """Map a path as written in a message to a real file inside ROOT, or None."""
    rel = unquote(rel).strip().lstrip("./")
    full = os.path.realpath(os.path.join(ROOT, rel))
    if full.startswith(ROOT + os.sep) and os.path.isfile(full):
        return full
    hits = index().get(os.path.basename(rel), [])
    hits = [h for h in hits if h.endswith(rel)] or hits
    return os.path.join(ROOT, sorted(hits, key=len)[0]) if hits else None


def db():
    con = sqlite3.connect("file:%s?mode=ro" % db_path(), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _jlist(x):
    try:
        v = json.loads(x)
        return v if isinstance(v, list) else []
    except Exception:
        return [s.strip() for s in str(x).replace(";", ",").split(",") if s.strip()]


def _reads(con, where, params):
    """msg -> [{agent, ts}] from the reads table (ts is missing on boards from before it was recorded)."""
    has_ts = "ts" in {r[1] for r in con.execute("PRAGMA table_info(reads)")}
    out = {}
    for r in con.execute(f"SELECT msg, agent, {'ts' if has_ts else 'NULL'} FROM reads WHERE {where}", params):
        out.setdefault(r[0], []).append({"agent": r[1], "ts": r[2]})
    return out


def _msgs(con, rows):
    ids = [r["id"] for r in rows]
    acks, seen = {}, {}
    if ids:
        q = ",".join("?" * len(ids))
        for a in con.execute(f"SELECT msg,agent,ts,note FROM acks WHERE msg IN ({q})", ids):
            acks.setdefault(a["msg"], []).append({"agent": a["agent"], "ts": a["ts"], "note": a["note"]})
        seen = _reads(con, f"msg IN ({q})", ids)
    out = []
    for r in rows:
        m = dict(r)
        m["recipients"] = _jlist(m["recipients"])
        m["refs"] = _jlist(m.get("refs") or "[]")
        m["summary"] = m.get("summary") or ""
        m["acks"] = acks.get(m["id"], [])
        m["seen"] = seen.get(m["id"], [])
        out.append(m)
    return out


def _bounds(con):
    r = con.execute("SELECT min(id), max(id), count(*) FROM messages").fetchone()
    return {"first": r[0] or 0, "last": r[1] or 0, "total": r[2]}


def messages(q):
    """A contiguous slice of the board:
    newest page (default) | before=ID older page | after=ID newer ones | start=ID from an id onwards |
    around=ID centred on a message | at='YYYY-MM-DD HH:MM' starting at the first message from that time.
    'before'/'after' in the reply count the messages outside the slice, 'target' is the message to show."""
    limit = max(1, min(int(q.get("limit", ["150"])[0]), 500))
    con = db()
    try:
        extra = {}
        if "after" in q:
            rows = con.execute("SELECT * FROM messages WHERE id>? ORDER BY id LIMIT ?", (int(q["after"][0]), int(q.get("limit", ["500"])[0]) if "limit" in q else 500)).fetchall()
        elif "before" in q:
            rows = con.execute("SELECT * FROM messages WHERE id<? ORDER BY id DESC LIMIT ?", (int(q["before"][0]), limit)).fetchall()[::-1]
        elif "start" in q:
            rows = con.execute("SELECT * FROM messages WHERE id>=? ORDER BY id LIMIT ?", (int(q["start"][0]), limit)).fetchall()
            extra["target"] = rows[0]["id"] if rows else None
        elif "around" in q or "at" in q:
            if "at" in q:
                pivot = con.execute("SELECT min(id) FROM messages WHERE ts>=?", (q["at"][0][:19],)).fetchone()[0]
                pivot = pivot if pivot is not None else (con.execute("SELECT max(id) FROM messages").fetchone()[0] or 0)
                n_before = min(30, limit // 4)          # a little context above the chosen moment
            else:
                pivot, n_before = int(q["around"][0]), limit // 2
            older = con.execute("SELECT * FROM messages WHERE id<? ORDER BY id DESC LIMIT ?", (pivot, n_before)).fetchall()[::-1]
            newer = con.execute("SELECT * FROM messages WHERE id>=? ORDER BY id LIMIT ?", (pivot, limit - len(older))).fetchall()
            rows = older + newer
            extra["target"] = newer[0]["id"] if newer else (older[-1]["id"] if older else None)
        else:
            rows = con.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()[::-1]
        if rows:
            extra["before"] = con.execute("SELECT count(*) FROM messages WHERE id<?", (rows[0]["id"],)).fetchone()[0]
            extra["after"] = con.execute("SELECT count(*) FROM messages WHERE id>?", (rows[-1]["id"],)).fetchone()[0]
        return dict(_bounds(con), messages=_msgs(con, rows), **extra)
    finally:
        con.close()


def days():
    """Messages per day and per hour, newest day first, for the date navigator."""
    con = db()
    try:
        out = {}
        for d, h, n, first in con.execute("SELECT substr(ts,1,10), substr(ts,12,2), count(*), min(id) FROM messages GROUP BY 1, 2"):
            day = out.setdefault(d, {"day": d, "n": 0, "first": first, "hours": [0] * 24})
            day["n"] += n
            day["first"] = min(day["first"], first)
            if h and h.isdigit() and int(h) < 24:
                day["hours"][int(h)] = n
        return {"days": sorted(out.values(), key=lambda x: x["day"], reverse=True)}
    finally:
        con.close()


def search(text):
    con = db()
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        hay = "subject||' '||body||' '||sender" + ("||' '||summary" if "summary" in cols else "")
        rows = con.execute(f"SELECT * FROM messages WHERE instr(lower({hay}), lower(?))>0 ORDER BY id DESC LIMIT 200",
                           (text,)).fetchall()
        return {"messages": _msgs(con, rows), "query": text}
    finally:
        con.close()


def _table(con, sql):
    try:
        return [dict(r) for r in con.execute(sql)]
    except sqlite3.OperationalError:       # board created before this table existed
        return []


def state(since=0):
    """Claims, rooms, questions, acks, and who has seen messages with id >= since (the oldest one the page shows)."""
    con = db()
    try:
        claims = _table(con, "SELECT * FROM claims WHERE released IS NULL ORDER BY id")
        for c in claims:
            c["files"] = _jlist(c.get("files") or "[]")
        rooms = [{"name": r["name"], "created": r["created"], "members": [dict(m) for m in con.execute(
            "SELECT agent,joined FROM members WHERE room=? ORDER BY joined", (r["name"],))]}
            for r in con.execute("SELECT name,created FROM rooms ORDER BY name")]
        inv = _table(con, "SELECT id,room,agent,created,used,used_by,revoked FROM invites ORDER BY id DESC LIMIT 200")
        for r in rooms:
            r["invites"] = [i for i in inv if i["room"] == r["name"] and not i["revoked"] and not i["used"]]
        asks = _table(con, "SELECT * FROM asks ORDER BY msg")
        for a in asks:
            a["passed"] = _jlist(a.get("passed") or "[]")
        acks = {}
        for a in con.execute("SELECT msg,agent,ts,note FROM acks"):
            acks.setdefault(a["msg"], []).append({"agent": a["agent"], "ts": a["ts"], "note": a["note"]})
        reads = _reads(con, "msg>=?", (since,))
        return {"claims": claims, "rooms": rooms, "asks": asks, "acks": acks, "reads": reads, "version": PAGE_VERSION,
                "coord_cmd": coord.COORD_CMD}
    finally:
        con.close()


WINDOWS = {"1h": 1, "24h": 24, "7d": 168, "all": None}
STAT_KINDS = ["result", "request", "answer", "review"]          # everything else counts as "other"


def stats(window):
    """Activity figures for the side panel. Timestamps are stored as local 'YYYY-MM-DD HH:MM:SS', so they compare as text."""
    now = datetime.now()
    at = lambda **kw: (now - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")
    hours = WINDOWS.get(window, 24)
    cut = at(hours=hours) if hours else "0000"
    con = db()
    try:
        one = lambda sql, *p: con.execute(sql, p).fetchone()[0]
        pace = {"last60": one("SELECT count(*) FROM messages WHERE ts>=?", at(hours=1)),
                "prev60": one("SELECT count(*) FROM messages WHERE ts>=? AND ts<?", at(hours=2), at(hours=1)),
                "last10": one("SELECT count(*) FROM messages WHERE ts>=?", at(minutes=10)),
                "total": one("SELECT count(*) FROM messages"),
                "window": one("SELECT count(*) FROM messages WHERE ts>=?", cut)}
        start = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)   # 24 hourly bars, the last one is the current hour
        bins = [0] * 24
        for (ts,) in con.execute("SELECT ts FROM messages WHERE ts>=?", (start.strftime("%Y-%m-%d %H:%M:%S"),)):
            try:
                i = int((datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S") - start).total_seconds() // 3600)
            except ValueError:
                continue
            if 0 <= i < 24:
                bins[i] += 1
        hourly = [{"hour": (start + timedelta(hours=i)).strftime("%Y-%m-%d %H:00"), "n": n} for i, n in enumerate(bins)]
        agents = {}
        for sender, kind, n in con.execute("SELECT sender, kind, count(*) FROM messages WHERE ts>=? AND sender<>'Admin' GROUP BY sender, kind", (cut,)):
            a = agents.setdefault(sender, {"agent": sender, "total": 0, "kinds": dict.fromkeys(STAT_KINDS + ["other"], 0)})
            a["total"] += n
            a["kinds"][kind if kind in STAT_KINDS else "other"] += n
        for sender, last in con.execute("SELECT sender, max(ts) FROM messages WHERE sender<>'Admin' GROUP BY sender"):
            if sender in agents:
                agents[sender]["last"] = last
        claims = {}
        for (agent,) in con.execute("SELECT agent FROM claims WHERE released IS NULL"):
            claims[agent] = claims.get(agent, 0) + 1
        for a in agents.values():
            a["claims"] = claims.get(a["agent"], 0)
        board = sorted(agents.values(), key=lambda a: (-a["kinds"]["result"], -a["total"], a["agent"]))
        # how fast requests get a first response (a reply or an ack)
        waits, unanswered = [], 0
        for ts, first in con.execute("SELECT m.ts, (SELECT min(t) FROM (SELECT r.ts AS t FROM messages r WHERE r.reply_to=m.id "
                                     "UNION ALL SELECT k.ts FROM acks k WHERE k.msg=m.id)) FROM messages m "
                                     "WHERE m.kind='request' AND m.ts>=?", (cut,)):   # first reply or acknowledgement
            if first:
                try:
                    waits.append((datetime.strptime(first[:19], "%Y-%m-%d %H:%M:%S") - datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60)
                except ValueError:
                    pass
            else:
                unanswered += 1
        replies = {"requests": len(waits) + unanswered, "answered": len(waits), "unanswered": unanswered,
                   "median_min": round(statistics.median(waits), 1) if waits else None}
        try:
            asks = [dict(r) for r in con.execute(
                "SELECT a.msg, a.status, a.owner, a.asker, a.created, m.subject FROM asks a LEFT JOIN messages m ON m.id=a.msg "
                "WHERE a.status IN ('open','assigned','taken','escalated') ORDER BY (a.status='escalated') DESC, a.created")]
        except sqlite3.OperationalError:
            asks = []
        return {"window": window if window in WINDOWS else "24h", "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "pace": pace, "hourly": hourly, "agents": board, "replies": replies, "asks": asks}
    finally:
        con.close()


def state_version(con):
    """Cheap fingerprint of everything /api/state returns."""
    parts = []
    for sql in ("SELECT count(*), max(updated) FROM asks", "SELECT count(*), max(ts) FROM acks",
                "SELECT count(*), max(id), max(released) FROM claims", "SELECT count(*), max(joined) FROM members",
                "SELECT count(*), max(rowid) FROM reads", "SELECT count(*), max(used), max(revoked) FROM invites",
                "SELECT group_concat(name) FROM rooms"):
        try:
            parts.append(tuple(con.execute(sql).fetchone()))
        except sqlite3.OperationalError:
            parts.append(None)
    return repr(parts)


def admin(action, d):
    """Room administration: same effects as coord.py room-create / join code / kick / room-close."""
    con = coord.connect()
    room, agent = str(d.get("room", "")).strip(), str(d.get("agent", "")).strip()
    note = lambda subj: con.execute(
        "INSERT INTO messages(ts,sender,recipients,kind,subject,body,refs) VALUES(?,?,?,?,?,?,?)",
        (coord.now(), "Admin", json.dumps(["all"]), "info", subj, "", "[]"))
    if not NAME_RE.match(room) or ":" in room:
        raise ValueError("bad room name")
    exists = con.execute("SELECT 1 FROM rooms WHERE name=?", (room,)).fetchone()
    if action == "create":
        if exists:
            raise ValueError("room exists")
        code = "%s:%s" % (room, secrets.token_urlsafe(12))
        con.execute("INSERT INTO rooms(name,code_hash,created) VALUES(?,?,?)", (room, coord.code_hash(code), coord.now()))
        return {"code": code, "label": "room code (reusable)"}
    if not exists:
        raise ValueError("no such room")
    if action == "invite":                 # one more agent: a single-use code; nothing else changes
        if agent and not NAME_RE.match(agent):
            raise ValueError("bad agent name")
        return {"code": coord.new_invite(con, room, agent or None), "agent": agent, "label": "single-use invite" + (" for " + agent if agent else "")}
    if action == "revoke-invite":
        con.execute("UPDATE invites SET revoked=? WHERE id=? AND room=? AND used IS NULL", (coord.now(), int(d.get("id", 0)), room))
        return {}
    if action == "reset":                  # emergency: a new room code, and every pending invite revoked
        code = "%s:%s" % (room, secrets.token_urlsafe(12))
        con.execute("UPDATE rooms SET code_hash=? WHERE name=?", (coord.code_hash(code), room))
        con.execute("UPDATE invites SET revoked=? WHERE room=? AND used IS NULL AND revoked IS NULL", (coord.now(), room))
        note("Admin reset the codes of room %s: the old room code and all unused invites are invalid" % room)
        return {"code": code, "label": "new room code (reusable)"}
    if action == "kick":
        if not agent:
            raise ValueError("agent required")
        n = con.execute("DELETE FROM members WHERE room=? AND agent=?", (room, agent)).rowcount
        note("Admin removed %s from room %s" % (agent, room))
        return {"removed": n}
    if action == "close":
        con.execute("DELETE FROM members WHERE room=?", (room,))
        con.execute("DELETE FROM rooms WHERE name=?", (room,))
        note("Admin closed room %s; all members lost posting rights" % room)
        return {}
    raise ValueError("unknown action")


def user_post(d):
    """The user writes to the board from the page (admin token required). Same rules as coord.py post for questions."""
    sender = str(d.get("sender") or "User").strip()[:40]
    if not NAME_RE.match(sender):
        raise ValueError("bad name: letters, digits, spaces, . _ -")
    rec = [str(r).strip() for r in (d.get("to") or ["all"]) if str(r).strip()][:20] or ["all"]
    kind = d.get("kind") if d.get("kind") in coord.KINDS else "info"
    subject = " ".join(str(d.get("subject") or "").split())[:200]
    if not subject:
        raise ValueError("a subject is required")
    summary = " ".join(str(d.get("summary") or "").split())[:coord.SUMMARY_MAX]
    body = str(d.get("body") or "")[:100000]
    reply_to = int(d["reply_to"]) if str(d.get("reply_to") or "").isdigit() else None
    con = coord.connect()
    cur = con.execute("INSERT INTO messages(ts,sender,recipients,kind,subject,body,reply_to,refs,summary) VALUES(?,?,?,?,?,?,?,?,?)",
                      (coord.now(), sender, json.dumps(rec), kind, subject, body, reply_to, "[]", summary))
    mid = cur.lastrowid
    con.execute("INSERT OR IGNORE INTO reads(agent,msg,ts) VALUES(?,?,?)", (sender, mid, coord.now()))
    if "all" in rec and (kind == "request" or d.get("ask")):          # a question to everyone: one agent must take it
        con.execute("INSERT INTO asks(msg,asker,status,hops,created,updated) VALUES(?,?,?,?,?,?)",
                    (mid, sender, "open", 0, coord.now(), coord.now()))
    if reply_to and kind in ("answer", "result"):
        a = coord.get_ask(con, reply_to)
        if a and a["status"] in coord.LIVE and sender != a["asker"] and (a["status"] == "open" or a["owner"] == sender):
            coord._set_ask(con, reply_to, status="answered", owner=sender)
    return {"id": mid}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, ctype, data, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, code=200):
        return self.send(code, "application/json", json.dumps(obj).encode())

    def host_ok(self):
        # DNS-rebinding guard: a page on another site must not be able to read the board through 127.0.0.1
        if ALLOWED_HOSTS is None:
            return True
        return (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]") in ALLOWED_HOSTS

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, "text/plain", b"forbidden host")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                    return self.send(200, "text/html; charset=utf-8", f.read())
            if u.path == "/file":
                return self.file(q.get("path", [""])[0])
            if u.path == "/api/messages":
                if q.get("v", [""])[0] != PAGE_VERSION:
                    # an older page version: give it nothing new and tell it to reload
                    return self.json({"messages": [], "claims": [], "rooms": [], "asks": [], "first": 0, "last": 0,
                                      "total": 0, "stale": "This page is out of date: reload it."})
                return self.json(messages(q))
            if u.path == "/api/days":
                return self.json(days())
            if u.path == "/api/search":
                return self.json(search(q.get("q", [""])[0][:200]))
            if u.path == "/api/state":
                return self.json(state(int(q.get("since", ["0"])[0] or 0)))
            if u.path == "/api/stats":
                return self.json(stats(q.get("window", ["24h"])[0]))
            if u.path == "/api/stream":
                return self.stream()
            if u.path == "/api/voices":
                agents = [str(a)[:60] for a in json.loads(q.get("agents", ["[]"])[0])][:100]
                if not tts.ENGINE:
                    return self.json({"engine": None, "voices": [], "assign": {}})
                vs = tts.voices(block=False)
                if vs is None:                     # still checking the voices: answer now, the page asks again
                    return self.json({"engine": tts.ENGINE, "voices": [], "assign": {}, "pending": True})
                return self.json({"engine": tts.ENGINE, "voices": [{k: v[k] for k in ("name", "lang", "gender")} for v in vs],
                                  "assign": VOICES.get(agents)})
            if u.path == "/tts":
                return self.tts(q)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self.json({"error": str(e)}, 500)
        self.send(404, "text/plain", b"not found")

    def file(self, path):
        full = resolve(path) if path else None
        if not full or any(p in SKIP_DIRS for p in full.split(os.sep)) or full.endswith(".sqlite"):
            return self.send(404, "text/plain; charset=utf-8", ("not found in project: %s" % path).encode())
        ext = os.path.splitext(full)[1].lower()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ext in TEXT_EXT or ctype.startswith("text/"):
            ctype = "text/plain; charset=utf-8"      # never serve project html/js as active content
        elif not (ctype == "application/pdf" or ctype.startswith("image/")):
            ctype = "application/octet-stream"
        with open(full, "rb") as f:
            return self.send(200, ctype, f.read())

    def stream(self):
        """Server-Sent Events: 'msg' when a new message exists, 'state' when asks/acks/claims/rooms change."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_id = last_state = None
        beat = time.time()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while True:
                con = db()
                try:
                    lid = con.execute("SELECT max(id) FROM messages").fetchone()[0] or 0
                    sv = state_version(con)
                finally:
                    con.close()
                if lid != last_id:
                    self.wfile.write(b"event: msg\ndata: %d\n\n" % lid)
                    last_id = lid
                if sv != last_state:
                    self.wfile.write(b"event: state\ndata: 1\n\n")
                    last_state = sv
                if time.time() - beat > 15:
                    self.wfile.write(b"event: ping\ndata: 1\n\n")      # lets the page notice a dead connection
                    beat = time.time()
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def tts(self, q):
        if not tts.ENGINE:
            return self.json({"error": "no speech engine on this server"}, 503)
        rate = max(0.5, min(2.0, float(q.get("rate", ["1"])[0])))
        if "msg" in q:
            con = db()
            try:
                r = con.execute("SELECT * FROM messages WHERE id=?", (int(q["msg"][0]),)).fetchone()
            finally:
                con.close()
            if not r:
                return self.json({"error": "no such message"}, 404)
            r = dict(r)
            agent, text = r["sender"], tts.speech_text(r["sender"], r["kind"], r["subject"], r.get("summary") or "")
        elif "agent" in q:
            agent = q["agent"][0][:60]
            text = tts.test_text(agent)
        else:
            return self.json({"error": "msg or agent required"}, 400)
        voice = q.get("voice", [""])[0]
        if voice not in tts.voice_names():
            voice = VOICES.get([agent]).get(agent)
        if not voice:
            return self.json({"error": "no usable voice"}, 503)
        with open(tts.audio_for(text, voice, rate), "rb") as f:
            # the URL names message, voice and rate, so the result never changes: let the browser cache it
            return self.send(200, "audio/wav", f.read(), cache="private, max-age=86400")

    def do_POST(self):
        if not self.host_ok():
            return self.send(403, "text/plain", b"forbidden host")
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/voice":
                # a custom header cannot be sent cross-site without a CORS preflight, which this server never grants
                if self.headers.get("X-Coord") != "1":
                    return self.json({"error": "forbidden"}, 403)
                VOICES.set(str(d.get("agent", ""))[:60], str(d.get("voice", "")))
                return self.json({"ok": True})
            if not hmac.compare_digest(self.headers.get("X-Admin-Token", ""), TOKEN):
                return self.json({"error": "forbidden: missing or wrong admin token"}, 403)
            if u.path == "/api/post":
                return self.json(user_post(d))
            if u.path == "/api/admin/check":
                return self.json({"ok": True})
            if not u.path.startswith("/api/admin/"):
                return self.send(404, "text/plain", b"not found")
            return self.json(admin(u.path[len("/api/admin/"):], d))
        except Exception as e:
            return self.json({"error": str(e)}, 400)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--root", help="folder whose files message links may open (default: the project folder)")
    a = ap.parse_args()
    if a.root:
        ROOT = os.path.realpath(a.root)
    if a.host in ("0.0.0.0", "::"):
        ALLOWED_HOSTS = None                 # listening on every interface: the user chose to accept any Host
    else:
        ALLOWED_HOSTS.add(a.host)
    try:
        coord.connect().close()              # bring the board's schema up to date (new tables, indexes) before serving
    except Exception as e:
        print("note: could not update the board schema:", e)
    if tts.ENGINE:
        tts.voices(block=False)              # start checking the voices now, in the background
    print("coord web on http://%s:%d  (db: %s, speech: %s)" % (a.host, a.port, db_path(), tts.ENGINE or "none"))
    print("admin (invite links, kick): http://%s:%d/#admin=%s" % (a.host, a.port, TOKEN))
    srv = ThreadingHTTPServer((a.host, a.port), H)
    srv.daemon_threads = True
    srv.serve_forever()
