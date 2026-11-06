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
import argparse, atexit, glob, hmac, json, mimetypes, os, re, secrets, socket, sqlite3, statistics, subprocess, sys, threading, time
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
PORT, HOST = 8780, "127.0.0.1"         # set in main


# ---------------------------------------------------------------------------
# Super admin: one password for every chat on this machine. A sign-in gives a
# session cookie (HttpOnly, SameSite=Strict) that every chat server here
# accepts, because cookies do not depend on the port.
# ---------------------------------------------------------------------------
_sess_lock = threading.Lock()
_fails = {"n": 0, "t": 0.0}


def _sessions():
    try:
        with open(coord.ADMIN_SESSIONS, encoding="utf-8") as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if v > time.time()} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sessions(d):
    os.makedirs(coord.DATA_DIR, exist_ok=True)
    tmp = coord.ADMIN_SESSIONS + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, coord.ADMIN_SESSIONS)


def new_session():
    sid = secrets.token_urlsafe(24)
    with _sess_lock:
        d = _sessions()
        d[sid] = time.time() + 30 * 86400
        _save_sessions(d)
    return sid


def session_ok(sid):
    return bool(sid) and coord.password_is_set() and sid in _sessions()


def drop_session(sid):
    with _sess_lock:
        d = _sessions()
        d.pop(sid, None)
        _save_sessions(d)


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _listening(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def register():
    """Tell the super-admin page where this chat's server runs."""
    info = {"project": coord.PROJECT, "root": ROOT, "here": HERE, "port": PORT, "host": HOST, "pid": os.getpid(), "started": coord.now()}
    path = os.path.join(coord.DATA_DIR, coord.PROJECT + ".server.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f)

    def bye():                              # keep the folder on record (so the admin page can start it again), drop the process
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
            if cur.get("pid") == os.getpid():
                cur.update(pid=None, stopped=coord.now())
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cur, f)
        except (OSError, ValueError):
            pass
    atexit.register(bye)
    return bye


def overview():
    """Every chat board on this machine with its server, rooms and activity."""
    chats = []
    for dbp in sorted(glob.glob(os.path.join(coord.DATA_DIR, "*.sqlite"))):
        name = os.path.basename(dbp)[:-7]
        reg = {}
        try:
            with open(os.path.join(coord.DATA_DIR, name + ".server.json"), encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, ValueError):
            pass
        running = bool(reg) and _alive(reg.get("pid")) and _listening(reg.get("host", "127.0.0.1"), reg.get("port", 0))
        tok = ""
        try:
            with open(os.path.join(coord.DATA_DIR, name + ".webtoken"), encoding="utf-8") as f:
                tok = f.read().strip()
        except OSError:
            pass
        c = {"project": name, "root": reg.get("root"), "here": reg.get("here"), "running": running,
             "port": reg.get("port"), "host": reg.get("host", "127.0.0.1"), "token": tok, "rooms": [], "messages": 0}
        try:
            con = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True, timeout=3)
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT count(*), max(ts) FROM messages").fetchone()
            c["messages"], c["last"] = r[0], r[1]
            hour = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
            c["active"] = [x[0] for x in con.execute("SELECT DISTINCT sender FROM messages WHERE ts>=? AND sender<>'Admin'", (hour,))]
            for room in con.execute("SELECT name, created FROM rooms ORDER BY name"):
                members = [x[0] for x in con.execute("SELECT agent FROM members WHERE room=? ORDER BY joined", (room["name"],))]
                try:
                    pend = con.execute("SELECT count(*) FROM invites WHERE room=? AND used IS NULL AND revoked IS NULL", (room["name"],)).fetchone()[0]
                except sqlite3.OperationalError:
                    pend = 0
                c["rooms"].append({"name": room["name"], "created": room["created"], "members": members, "pending": pend})
            con.close()
        except sqlite3.Error as e:
            c["error"] = str(e)
        c["size_kb"] = round(os.path.getsize(dbp) / 1024)
        chats.append(c)
    chats.sort(key=lambda c: (not c["running"], c.get("last") or ""), reverse=False)
    return {"chats": chats, "data_dir": coord.DATA_DIR, "me": coord.PROJECT, "set_password": coord.COORD_CMD + " set-password"}


CHILDREN = {}                          # chat servers started from this admin page: reaped on stop (no zombies)


def _free_port(start=8780):
    for p in range(start, start + 100):
        with socket.socket() as s_:
            try:
                s_.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port")


def start_chat(project):
    c = next((c for c in overview()["chats"] if c["project"] == project), None)
    if not c:
        raise ValueError("no such chat")
    if c["running"]:
        return {"port": c["port"]}
    if not c.get("here") or not os.path.exists(os.path.join(c["here"], "web.py")):
        raise ValueError("this chat's folder is unknown: start web.py in it once")
    port = _free_port()
    log = open(os.path.join(coord.DATA_DIR, project + ".server.log"), "a")
    env = dict(os.environ)
    env.pop("COORD_DB", None)                            # the started server must open its own board, not ours
    env["COORD_PROJECT"] = project
    CHILDREN[project] = subprocess.Popen([sys.executable, os.path.join(c["here"], "web.py"), "--port", str(port)], cwd=c["here"],
                                         stdout=log, stderr=log, start_new_session=True, env=env)
    for _ in range(40):
        if _listening("127.0.0.1", port):
            return {"port": port}
        time.sleep(0.1)
    return {"port": port, "note": "started, still coming up"}


def stop_chat(project):
    c = next((c for c in overview()["chats"] if c["project"] == project), None)
    if not c or not c["running"]:
        return {}
    with open(os.path.join(coord.DATA_DIR, project + ".server.json"), encoding="utf-8") as f:
        pid = json.load(f)["pid"]
    if pid == os.getpid():
        raise ValueError("this is the server you are using; stop it from its terminal")
    cmdline = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    if "web.py" not in cmdline:                           # never signal a process that is not a chat server
        raise ValueError("the registered process is not a chat server")
    os.kill(pid, 15)
    child = CHILDREN.pop(project, None)
    if child and child.pid == pid:
        try:
            child.wait(timeout=5)                         # our own child: reap it
        except subprocess.TimeoutExpired:
            pass
    for _ in range(50):                                   # report back only once it has really gone
        if not _alive(pid) or not _listening(c["host"], c["port"]):
            break
        time.sleep(0.1)
    return {"stopped": not _listening(c["host"], c["port"])}
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


def db(room=None):
    """Read-only connection. 'msgs' is the messages of one room (room name, '_none' for messages without a room) or of all."""
    con = sqlite3.connect("file:%s?mode=ro" % db_path(), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    has_room = "room" in {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    if room and has_room and room == "_none":
        where = "WHERE room IS NULL"
    elif room and has_room and NAME_RE.match(room):
        where = "WHERE room='%s'" % room.replace("'", "''")
    else:
        where = ""
    con.execute("CREATE TEMP VIEW msgs AS SELECT * FROM messages " + where)
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
    r = con.execute("SELECT min(id), max(id), count(*) FROM msgs").fetchone()
    return {"first": r[0] or 0, "last": r[1] or 0, "total": r[2]}


def messages(q):
    """A contiguous slice of the board:
    newest page (default) | before=ID older page | after=ID newer ones | start=ID from an id onwards |
    around=ID centred on a message | at='YYYY-MM-DD HH:MM' starting at the first message from that time.
    'before'/'after' in the reply count the messages outside the slice, 'target' is the message to show."""
    limit = max(1, min(int(q.get("limit", ["150"])[0]), 500))
    con = db(q.get("room", [""])[0])
    try:
        extra = {}
        if "after" in q:
            rows = con.execute("SELECT * FROM msgs WHERE id>? ORDER BY id LIMIT ?", (int(q["after"][0]), int(q.get("limit", ["500"])[0]) if "limit" in q else 500)).fetchall()
        elif "before" in q:
            rows = con.execute("SELECT * FROM msgs WHERE id<? ORDER BY id DESC LIMIT ?", (int(q["before"][0]), limit)).fetchall()[::-1]
        elif "start" in q:
            rows = con.execute("SELECT * FROM msgs WHERE id>=? ORDER BY id LIMIT ?", (int(q["start"][0]), limit)).fetchall()
            extra["target"] = rows[0]["id"] if rows else None
        elif "around" in q or "at" in q:
            if "at" in q:
                pivot = con.execute("SELECT min(id) FROM msgs WHERE ts>=?", (q["at"][0][:19],)).fetchone()[0]
                pivot = pivot if pivot is not None else (con.execute("SELECT max(id) FROM msgs").fetchone()[0] or 0)
                n_before = min(30, limit // 4)          # a little context above the chosen moment
            else:
                pivot, n_before = int(q["around"][0]), limit // 2
            older = con.execute("SELECT * FROM msgs WHERE id<? ORDER BY id DESC LIMIT ?", (pivot, n_before)).fetchall()[::-1]
            newer = con.execute("SELECT * FROM msgs WHERE id>=? ORDER BY id LIMIT ?", (pivot, limit - len(older))).fetchall()
            rows = older + newer
            extra["target"] = newer[0]["id"] if newer else (older[-1]["id"] if older else None)
        else:
            rows = con.execute("SELECT * FROM msgs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()[::-1]
        if rows:
            extra["before"] = con.execute("SELECT count(*) FROM msgs WHERE id<?", (rows[0]["id"],)).fetchone()[0]
            extra["after"] = con.execute("SELECT count(*) FROM msgs WHERE id>?", (rows[-1]["id"],)).fetchone()[0]
        return dict(_bounds(con), messages=_msgs(con, rows), **extra)
    finally:
        con.close()


def days(room=""):
    """Messages per day and per hour, newest day first, for the date navigator."""
    con = db(room)
    try:
        out = {}
        for d, h, n, first in con.execute("SELECT substr(ts,1,10), substr(ts,12,2), count(*), min(id) FROM msgs GROUP BY 1, 2"):
            day = out.setdefault(d, {"day": d, "n": 0, "first": first, "hours": [0] * 24})
            day["n"] += n
            day["first"] = min(day["first"], first)
            if h and h.isdigit() and int(h) < 24:
                day["hours"][int(h)] = n
        return {"days": sorted(out.values(), key=lambda x: x["day"], reverse=True)}
    finally:
        con.close()


def search(text, room=""):
    con = db(room)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        hay = "subject||' '||body||' '||sender" + ("||' '||summary" if "summary" in cols else "")
        rows = con.execute(f"SELECT * FROM msgs WHERE instr(lower({hay}), lower(?))>0 ORDER BY id DESC LIMIT 200",
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


def stats(window, room=""):
    """Activity figures for the side panel. Timestamps are stored as local 'YYYY-MM-DD HH:MM:SS', so they compare as text."""
    now = datetime.now()
    at = lambda **kw: (now - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")
    hours = WINDOWS.get(window, 24)
    cut = at(hours=hours) if hours else "0000"
    con = db(room)
    try:
        one = lambda sql, *p: con.execute(sql, p).fetchone()[0]
        pace = {"last60": one("SELECT count(*) FROM msgs WHERE ts>=?", at(hours=1)),
                "prev60": one("SELECT count(*) FROM msgs WHERE ts>=? AND ts<?", at(hours=2), at(hours=1)),
                "last10": one("SELECT count(*) FROM msgs WHERE ts>=?", at(minutes=10)),
                "total": one("SELECT count(*) FROM msgs"),
                "window": one("SELECT count(*) FROM msgs WHERE ts>=?", cut)}
        start = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)   # 24 hourly bars, the last one is the current hour
        bins = [0] * 24
        for (ts,) in con.execute("SELECT ts FROM msgs WHERE ts>=?", (start.strftime("%Y-%m-%d %H:%M:%S"),)):
            try:
                i = int((datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S") - start).total_seconds() // 3600)
            except ValueError:
                continue
            if 0 <= i < 24:
                bins[i] += 1
        hourly = [{"hour": (start + timedelta(hours=i)).strftime("%Y-%m-%d %H:00"), "n": n} for i, n in enumerate(bins)]
        agents = {}
        for sender, kind, n in con.execute("SELECT sender, kind, count(*) FROM msgs WHERE ts>=? AND sender<>'Admin' GROUP BY sender, kind", (cut,)):
            a = agents.setdefault(sender, {"agent": sender, "total": 0, "kinds": dict.fromkeys(STAT_KINDS + ["other"], 0)})
            a["total"] += n
            a["kinds"][kind if kind in STAT_KINDS else "other"] += n
        for sender, last in con.execute("SELECT sender, max(ts) FROM msgs WHERE sender<>'Admin' GROUP BY sender"):
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
                                     "UNION ALL SELECT k.ts FROM acks k WHERE k.msg=m.id)) FROM msgs m "
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
                "WHERE a.status IN ('open','assigned','taken','escalated') AND a.msg IN (SELECT id FROM msgs) ORDER BY (a.status='escalated') DESC, a.created")]
        except sqlite3.OperationalError:
            asks = []
        return {"window": window if window in WINDOWS else "24h", "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "pace": pace, "hourly": hourly, "agents": board, "replies": replies, "asks": asks}
    finally:
        con.close()


def room_counts(seen):
    """seen: {room or '_none': last id the reader has seen} -> {room: messages after it}."""
    con = db()
    try:
        if "room" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
            return {}
        out = {}
        for room, last in con.execute("SELECT coalesce(room,'_none'), max(id) FROM messages GROUP BY 1"):
            out[room] = con.execute("SELECT count(*) FROM messages WHERE coalesce(room,'_none')=? AND id>?",
                                    (room, int(seen.get(room, 0) or 0))).fetchone()[0]
        return out
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
        "INSERT INTO messages(ts,sender,recipients,kind,subject,body,refs,room) VALUES(?,?,?,?,?,?,?,?)",
        (coord.now(), "Admin", json.dumps(["all"]), "info", subj, "", "[]", room or None))
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
    room = str(d.get("room") or "").strip() or None
    if room and not con.execute("SELECT 1 FROM rooms WHERE name=?", (room,)).fetchone():
        raise ValueError("no such room")
    cur = con.execute("INSERT INTO messages(ts,sender,recipients,kind,subject,body,reply_to,refs,summary,room) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (coord.now(), sender, json.dumps(rec), kind, subject, body, reply_to, "[]", summary, room))
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

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def session(self):
        return session_ok(self.cookie("coord_admin"))

    def is_admin(self):
        """The admin token, or a super-admin session plus a custom header (which other sites cannot send)."""
        if hmac.compare_digest(self.headers.get("X-Admin-Token", ""), TOKEN):
            return True
        return self.session() and self.headers.get("X-Coord") == "1"

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
            if u.path == "/admin":
                with open(os.path.join(HERE, "web", "admin.html"), "rb") as f:
                    return self.send(200, "text/html; charset=utf-8", f.read())
            if u.path == "/api/whoami":
                return self.json({"admin": self.session(), "password_set": coord.password_is_set(), "project": coord.PROJECT})
            if u.path == "/api/super":
                if not self.session():
                    return self.json({"error": "sign in first", "password_set": coord.password_is_set(),
                                      "set_password": coord.COORD_CMD + " set-password"}, 401)
                return self.json(overview())
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
            room = q.get("room", [""])[0]
            if u.path == "/api/days":
                return self.json(days(room))
            if u.path == "/api/search":
                return self.json(search(q.get("q", [""])[0][:200], room))
            if u.path == "/api/roomcounts":
                return self.json(room_counts(json.loads(q.get("seen", ["{}"])[0] or "{}")))
            if u.path == "/api/state":
                return self.json(state(int(q.get("since", ["0"])[0] or 0)))
            if u.path == "/api/stats":
                return self.json(stats(q.get("window", ["24h"])[0], room))
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
            if u.path == "/api/super/login":
                if not coord.password_is_set():
                    return self.json({"error": "no admin password yet: run  %s set-password  in a terminal" % coord.COORD_CMD}, 400)
                wait = _fails["n"] and min(30, 2 ** min(_fails["n"], 5)) - (time.time() - _fails["t"])
                if wait and wait > 0:
                    return self.json({"error": "too many wrong passwords; try again in %d s" % int(wait + 1)}, 429)
                if not coord.check_password(str(d.get("password", ""))):
                    _fails.update(n=_fails["n"] + 1, t=time.time())
                    return self.json({"error": "wrong password"}, 403)
                _fails.update(n=0)
                sid = new_session()
                self.send_response(200)
                body = b'{"ok": true}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "coord_admin=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (sid, 30 * 86400))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/super/logout":
                drop_session(self.cookie("coord_admin"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.send_header("Set-Cookie", "coord_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            if u.path in ("/api/super/start", "/api/super/stop"):
                if not (self.session() and self.headers.get("X-Coord") == "1"):
                    return self.json({"error": "sign in first"}, 403)
                fn = start_chat if u.path.endswith("start") else stop_chat
                return self.json(fn(str(d.get("project", ""))))
            if u.path == "/api/voice":
                # a custom header cannot be sent cross-site without a CORS preflight, which this server never grants
                if self.headers.get("X-Coord") != "1":
                    return self.json({"error": "forbidden"}, 403)
                VOICES.set(str(d.get("agent", ""))[:60], str(d.get("voice", "")))
                return self.json({"ok": True})
            if not self.is_admin():
                return self.json({"error": "forbidden: sign in as admin (/admin) or use the admin link"}, 403)
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
    print("all chats on this machine (password): http://%s:%d/admin" % (a.host, a.port))
    PORT, HOST = a.port, a.host
    srv = ThreadingHTTPServer((a.host, a.port), H)
    unregister = register()
    import signal

    def _term(*_):                          # stopped from the admin page: unregister and exit now, even mid voice check
        unregister()
        os._exit(0)
    signal.signal(signal.SIGTERM, _term)
    srv.daemon_threads = True
    srv.serve_forever()
