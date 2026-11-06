#!/usr/bin/env python3
"""coord: a small inter-agent coordination board for this project.

A shared message board for AI agents working in one project: posts with
summaries, requests and acknowledgements, questions to everyone, claims and
rooms joined by invitation. `export` writes the whole board as Markdown.

Design
- One SQLite database, outside Dropbox (live SQLite files and Dropbox sync
  do not mix): ~/Library/Application Support/agent-coord/<project>.sqlite,
  overridable with COORD_DB. All agents run on this machine.
- Every write is a single transaction, so concurrent agents cannot clobber
  each other.
- Identity: --me NAME or the COORD_AGENT environment variable.

Commands (run `coord.py <cmd> -h` for options)
  post     post a message (kinds: info, request, claim-note, result, review, answer)
  inbox    unread messages for me (addressed to me or to all); marks them read
  wait     block until I have unread messages (use as a background job)
  show     one message by id;   thread  a message with all its replies
  log      recent messages, filterable by sender / kind / text
  claim    claim a work item (+ files); refuses on conflict with an active claim
  release  release a claim;  claims  list active claims
  ack      acknowledge a request;  pending  requests addressed to me not yet acked
  export   write the whole board as Markdown (human-readable record)
"""
import argparse, json, os, re, shlex, sqlite3, sys, time, textwrap
from datetime import datetime, timezone

# Where am I? The chat lives in a folder inside a project (e.g. <project>/agent-chat). The project folder is
# that folder's parent unless agent-chat.json next to this file says otherwise ({"root": "..", "project": "name"}),
# or COORD_ROOT / COORD_PROJECT are set. The project name picks the board database, so each project has its own.
HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(HERE, "agent-chat.json"), encoding="utf-8") as _f:
        _CFG = json.load(_f)
except (OSError, ValueError):
    _CFG = {}
ROOT = os.path.realpath(os.environ.get("COORD_ROOT") or os.path.join(HERE, _CFG.get("root", "..")))
PROJECT = os.environ.get("COORD_PROJECT") or _CFG.get("project") or re.sub(r"[^a-z0-9]+", "-", os.path.basename(ROOT).lower()).strip("-") or "project"
DATA_DIR = os.environ.get("COORD_DATA_DIR") or (os.path.expanduser("~/Library/Application Support/agent-coord") if sys.platform == "darwin" else
    os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "agent-coord"))
DEFAULT_DB = os.path.join(DATA_DIR, PROJECT + ".sqlite")
COORD_CMD = "python3 " + shlex.quote(os.path.relpath(os.path.join(HERE, "coord.py"), ROOT))   # how to run me from the project folder
KINDS = ["info", "request", "claim-note", "result", "review", "answer"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, sender TEXT NOT NULL,
  recipients TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
  body TEXT NOT NULL, reply_to INTEGER, refs TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS reads(agent TEXT NOT NULL, msg INTEGER NOT NULL, ts TEXT, PRIMARY KEY(agent,msg));
CREATE TABLE IF NOT EXISTS acks(agent TEXT NOT NULL, msg INTEGER NOT NULL, ts TEXT NOT NULL, note TEXT, PRIMARY KEY(agent,msg));
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, agent TEXT NOT NULL,
  item TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
  released TEXT);
CREATE TABLE IF NOT EXISTS asks(
  msg INTEGER PRIMARY KEY, asker TEXT NOT NULL, status TEXT NOT NULL, owner TEXT,
  hops INTEGER NOT NULL DEFAULT 0, passed TEXT NOT NULL DEFAULT '[]',
  created TEXT NOT NULL, updated TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS messages_reply_to ON messages(reply_to);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS reads_msg ON reads(msg);
CREATE INDEX IF NOT EXISTS acks_msg ON acks(msg);
CREATE TABLE IF NOT EXISTS invites(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL, code_hash TEXT NOT NULL, agent TEXT,
  created TEXT NOT NULL, used TEXT, used_by TEXT, revoked TEXT);
CREATE TABLE IF NOT EXISTS rooms(name TEXT PRIMARY KEY, code_hash TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(room TEXT NOT NULL, agent TEXT NOT NULL, joined TEXT NOT NULL, PRIMARY KEY(room,agent));
"""


def require_human_terminal(what):
    """Room administration is for the user only: refuse unless run interactively.

    Agents run commands without a terminal attached, so they cannot pass this.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit(f"coord: '{what}' must be run by the user in an interactive terminal.")


def now():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Authorization policy, configured manually by the user.
#
# Fail closed: without a policy file no agent can write anything (post, claim,
# release, ack). Reading is always allowed. The tool never writes the policy;
# there is deliberately no command for it. Format (JSON):
#   {"agents": {"Alice": {"may_address": ["Bob", "Carol"]},
#               "Bob":   {"may_address": ["all"]},
#               "Carol": {"may_address": ["Alice", "Bob"]}}}
# "all" in may_address permits broadcast; otherwise each recipient must be
# listed. Claims, releases and acks count as broadcast-free bookkeeping and
# need only that the agent itself is listed.
# ---------------------------------------------------------------------------
DEFAULT_POLICY = os.path.join(os.path.dirname(DEFAULT_DB), PROJECT + ".policy.json")


def policy_path():
    return os.environ.get("COORD_POLICY", DEFAULT_POLICY)


def load_policy():
    path = policy_path()
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def room_members(agent):
    """All agents sharing at least one room with `agent` (including itself), or None if in no room."""
    con = connect()
    rooms = [r["room"] for r in con.execute("SELECT room FROM members WHERE agent=?", (agent,))]
    if not rooms:
        return None
    q = ",".join("?" * len(rooms))
    return {r["agent"] for r in con.execute(f"SELECT agent FROM members WHERE room IN ({q})", rooms)}


def authorize(agent, recipients=None):
    # Route 1: membership of a room joined with a user-issued code.
    mates = room_members(agent)
    if mates is not None:
        for r in recipients or []:
            if r != "all" and r not in mates:
                sys.exit(f"coord: refused. '{r}' is not in a room with '{agent}'.")
        return True
    # Route 2: the manually written policy file.
    pol = load_policy()
    if pol is None:
        sys.exit(f"coord: refused. No policy file at {policy_path()}.\n"
                 "The user must create it manually, or give you a room invite (see README.md next to coord.py).")
    agents = pol.get("agents", {})
    if agent not in agents:
        sys.exit(f"coord: refused. Agent '{agent}' is not authorized in {policy_path()}.")
    if recipients is not None:
        allowed = set(agents[agent].get("may_address", []))
        for r in recipients:
            if r == "all" and "all" not in allowed:
                sys.exit(f"coord: refused. '{agent}' may not broadcast to all (allowed: {sorted(allowed)}).")
            if r != "all" and r not in allowed and "all" not in allowed:
                sys.exit(f"coord: refused. '{agent}' may not address '{r}' (allowed: {sorted(allowed)}).")
    return True


def connect():
    path = os.environ.get("COORD_DB", DEFAULT_DB)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    if "ts" not in {r["name"] for r in con.execute("PRAGMA table_info(reads)")}:
        try:   # older boards: reads get a time so the web view can say when an agent saw a message
            con.execute("ALTER TABLE reads ADD COLUMN ts TEXT")
        except sqlite3.OperationalError:
            pass
    if "room" not in {r["name"] for r in con.execute("PRAGMA table_info(messages)")}:
        try:   # messages belong to a room; existing ones are assigned once from their senders' memberships
            con.execute("ALTER TABLE messages ADD COLUMN room TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS messages_room ON messages(room, id)")
            backfill_rooms(con)
        except sqlite3.OperationalError:
            pass
    if "summary" not in {r["name"] for r in con.execute("PRAGMA table_info(messages)")}:
        try:   # older boards: add the column; old messages keep an empty summary
            con.execute("ALTER TABLE messages ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    return con


# ---------------------------------------------------------------------------
# Rooms. Every message belongs to one room (or none, on boards without rooms).
# "all" means everyone in that room.
# ---------------------------------------------------------------------------
def rooms_of(con, agent):
    return [r["room"] for r in con.execute("SELECT room FROM members WHERE agent=? ORDER BY joined", (agent,))]


def room_mates(con, room):
    return {r["agent"] for r in con.execute("SELECT agent FROM members WHERE room=?", (room,))}


def own_room(con, agent):
    """The agent's room if it has exactly one (for automatic notes such as claims)."""
    mine = rooms_of(con, agent)
    return mine[0] if len(mine) == 1 else None


def pick_room(con, agent, explicit, rec, reply_to):
    """The room a new message goes to: --room, else the agent's only room, else what the reply or recipients imply."""
    mine = rooms_of(con, agent)
    if explicit:
        if mine and explicit not in mine:
            sys.exit(f"coord: refused. You are not in room '{explicit}' (your rooms: {', '.join(mine)}).")
        return explicit
    if not mine:
        return None
    if len(mine) == 1:
        return mine[0]
    if reply_to:
        r = con.execute("SELECT room FROM messages WHERE id=?", (reply_to,)).fetchone()
        if r and r["room"] in mine:
            return r["room"]
    named = [x for x in rec if x != "all"]
    if named:
        fits = [rm for rm in mine if set(named) <= room_mates(con, rm)]
        if len(fits) == 1:
            return fits[0]
    sys.exit(f"coord: you are in several rooms ({', '.join(mine)}): add --room NAME to say which one this message is for.")


def backfill_rooms(con):
    """Assign a room to older messages: the sender's room at the time (none before they joined one);
    for agents in several rooms, the one the recipients or the replied-to message imply. Ambiguous ones stay without a room."""
    joins = {}
    for m in con.execute("SELECT room, agent, joined FROM members ORDER BY joined"):
        joins.setdefault(m["agent"], []).append((m["room"], m["joined"]))
    mates = {}
    for m in con.execute("SELECT room, agent FROM members"):
        mates.setdefault(m["room"], set()).add(m["agent"])
    known_rooms = {r["name"] for r in con.execute("SELECT name FROM rooms")}
    got = {}
    for r in con.execute("SELECT id, ts, sender, recipients, reply_to, subject FROM messages WHERE room IS NULL ORDER BY id").fetchall():
        m = re.search(r"\broom (\S+)", r["subject"] or "")
        if r["sender"] == "Admin" or re.search(r" joined room ", r["subject"] or ""):
            if m and m.group(1) in known_rooms:
                got[r["id"]] = m.group(1)
            continue
        js = joins.get(r["sender"]) or []
        if not js:
            continue
        cand = [rm for rm, j in js if j <= r["ts"]]       # before the sender joined any room: no room
        if len(cand) > 1:
            try:
                named = [x for x in json.loads(r["recipients"]) if x != "all"]
            except ValueError:
                named = []
            fit = [rm for rm in cand if named and set(named) <= mates.get(rm, set())]
            if len(fit) == 1:
                cand = fit
            elif r["reply_to"] in got and got[r["reply_to"]] in cand:
                cand = [got[r["reply_to"]]]
            else:                                         # still ambiguous: the room the sender joined most recently
                cand = [max(((rm, j) for rm, j in js if j <= r["ts"]), key=lambda x: x[1])[0]]
        if len(cand) == 1:
            got[r["id"]] = cand[0]
    con.executemany("UPDATE messages SET room=? WHERE id=?", [(v, k) for k, v in got.items()])
    return len(got)


def me(args):
    name = getattr(args, "me", None) or os.environ.get("COORD_AGENT")
    if not name:
        sys.exit("coord: give --me NAME or set COORD_AGENT")
    return name


def addressed(row, agent, con=None):
    rec = json.loads(row["recipients"])
    if agent in rec:
        return True
    if "all" not in rec:
        return False
    room = row["room"] if "room" in row.keys() else None
    if not room or con is None:
        return True
    return agent in room_mates(con, room)              # "all" means everyone in the message's room


def fmt(row, full=True):
    rec = ",".join(json.loads(row["recipients"]))
    head = f"#{row['id']} [{row['kind']}] {row['sender']} -> {rec}  {row['ts']}"
    if row["reply_to"]:
        head += f"  (re #{row['reply_to']})"
    out = head + "\n  " + row["subject"]
    if row["summary"]:
        out += "\n  summary: " + row["summary"]
    refs = json.loads(row["refs"])
    if refs:
        out += "\n  refs: " + ", ".join(refs)
    try:
        al = ask_line(connect(), row["id"])
    except Exception:
        al = ""
    if al:
        out += "\n  >> " + al
    if full:
        out += "\n" + textwrap.indent(row["body"].rstrip(), "    ")
    return out


def body_from(args):
    if args.body is not None:
        return args.body
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


SUMMARY_MAX = 300

# ---------------------------------------------------------------------------
# Questions to everyone ("asks"): a request addressed to `all` must be answered.
#   open      nobody has it yet: any agent may `take` it or `propose` someone
#   assigned  proposed to one agent, who must take it, answer it or pass it on
#   taken     one agent owns it and owes an answer
#   answered  the owner (or the first answerer of an open ask) posted a reply
#   escalated the owner gave up (`release`): it goes to the user
#   closed    the asker withdrew it
# Ping-pong is impossible by construction: at most MAX_HOPS handovers per ask,
# nobody who already passed it (or the asker) may be proposed, and when the
# budget is spent the holder can only answer or `release` it to the user.
# ---------------------------------------------------------------------------
MAX_HOPS = 2
LIVE = ("open", "assigned", "taken")


def get_ask(con, mid):
    return con.execute("SELECT * FROM asks WHERE msg=?", (mid,)).fetchone()


def eligible_agents(con, asker, msg=None):
    """Agents that can be asked: the question's room, else room mates of the asker, else the policy list."""
    room = msg and (con.execute("SELECT room FROM messages WHERE id=?", (msg,)).fetchone() or [None])[0]
    if room:
        return sorted(room_mates(con, room) - {asker})
    mates = room_members(asker)
    if mates is None:
        pol = load_policy() or {}
        mates = set(pol.get("agents", {}))
    return sorted(mates - {asker})


def ask_line(con, mid, viewer=None):
    a = get_ask(con, mid)
    if not a:
        return ""
    left = MAX_HOPS - a["hops"]
    st = a["status"]
    if st == "open":
        t = (f"ANSWER NEEDED, unclaimed: run `coord take {mid}` to answer it yourself, or "
             f"`coord propose {mid} --agent NAME` to hand it to a better-placed agent ({left} handover(s) left)")
    elif st == "assigned":
        t = f"proposed to {a['owner']} (handover {a['hops']}/{MAX_HOPS}); {a['owner']} must take it, answer it or pass it on"
    elif st == "taken":
        t = f"taken by {a['owner']}, who owes the answer; no action needed from anyone else"
    elif st == "answered":
        t = f"answered by {a['owner']}"
    elif st == "escalated":
        t = f"ESCALATED to the user: {a['note'] or 'nobody could answer'}"
    else:
        t = "closed by the asker"
    return f"ask #{mid} [{st}]: {t}"


def _set_ask(con, mid, **kw):
    kw["updated"] = now()
    con.execute("UPDATE asks SET " + ",".join(f"{k}=?" for k in kw) + " WHERE msg=?", (*kw.values(), mid))


def _ask_note(con, sender, to, kind, subject, summary, reply_to, body=""):
    con.execute("INSERT INTO messages(ts,sender,recipients,kind,subject,body,reply_to,refs,summary,room) "
                "VALUES(?,?,?,?,?,?,?,?,?,(SELECT room FROM messages WHERE id=?))",   # same room as the question
                (now(), sender, json.dumps(to), kind, subject, body, reply_to, "[]", summary, reply_to))


def _load_live_ask(con, mid, agent):
    a = get_ask(con, mid)
    if not a:
        sys.exit(f"coord: #{mid} is not a question to everyone (no ask is attached to it).")
    if a["status"] not in LIVE:
        sys.exit(f"coord: ask #{mid} is already {a['status']}; nothing to do.")
    return a


def cmd_take(args):
    agent = me(args)
    authorize(agent)
    con = connect()
    con.execute("BEGIN IMMEDIATE")          # first taker wins; concurrent takers are serialized
    try:
        a = _load_live_ask(con, args.id, agent)
        if agent == a["asker"]:
            sys.exit("coord: you asked this question; use `coord close` to withdraw it.")
        if a["status"] == "open" and agent not in eligible_agents(con, a["asker"], a["msg"]):
            sys.exit("coord: you are not in a room with the asker.")
        if a["status"] in ("assigned", "taken") and a["owner"] != agent:
            sys.exit(f"coord: ask #{args.id} belongs to {a['owner']}; you cannot take it.")
        if a["status"] == "taken":
            print(f"you already own ask #{args.id}; post the answer with --reply-to {args.id}")
            con.execute("COMMIT"); return
        _set_ask(con, args.id, status="taken", owner=agent)
        _ask_note(con, agent, [a["asker"]], "claim-note", f"{agent} will answer #{args.id}",
                  f"{agent} has taken your question and will reply.", args.id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK"); raise
    print(f"you own ask #{args.id}: post your answer with  coord post --reply-to {args.id} --kind answer --to {a['asker']} --subject ... --summary ...")


def cmd_propose(args):
    agent = me(args)
    target = args.agent.strip()
    authorize(agent, [target])
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    try:
        a = _load_live_ask(con, args.id, agent)
        passed = json.loads(a["passed"])
        if a["status"] in ("assigned", "taken") and a["owner"] != agent:
            sys.exit(f"coord: ask #{args.id} belongs to {a['owner']}; only the owner may hand it over.")
        if a["status"] == "open" and agent == a["asker"]:
            sys.exit("coord: you asked this question; you cannot hand it over.")
        if a["hops"] >= MAX_HOPS:
            sys.exit(f"coord: refused. Ask #{args.id} has already been handed over {a['hops']} times (the limit). "
                     f"Answer it yourself, or run `coord escalate {args.id} --reason ...` to send it to the user.")
        if target == agent:
            sys.exit("coord: you cannot propose yourself; use `coord take`.")
        if target == a["asker"]:
            sys.exit("coord: the asker cannot be asked to answer their own question.")
        if target in passed:
            sys.exit(f"coord: refused. {target} already handed this question on; handing it back would be ping-pong. "
                     "Choose someone else, or answer it yourself.")
        if target not in eligible_agents(con, a["asker"], a["msg"]):
            sys.exit(f"coord: {target} is not an agent in the asker's room. Eligible: {', '.join(eligible_agents(con, a['asker'], a['msg']))}.")
        _set_ask(con, args.id, status="assigned", owner=target, hops=a["hops"] + 1, passed=json.dumps(passed + [agent]))
        left = MAX_HOPS - a["hops"] - 1
        _ask_note(con, agent, [target], "request", f"Please answer #{args.id} (proposed by {agent})",
                  f"{agent} thinks you are best placed to answer question {args.id} from {a['asker']}."
                  + (" This is the last handover: you must answer it yourself." if left == 0 else ""),
                  args.id, body=(args.reason or "") + f"\n\nRun `coord take {args.id}` to accept it, then post your answer with --reply-to {args.id}."
                  + ("" if left == 0 else f" You may hand it on once more ({left} handover left) with `coord propose`.")
                  + f" If you cannot answer it at all: `coord escalate {args.id} --reason ...` sends it to the user.")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK"); raise
    print(f"ask #{args.id} proposed to {target} (handover {a['hops'] + 1}/{MAX_HOPS})")


def cmd_escalate(args):
    agent = me(args)
    authorize(agent)
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    try:
        a = _load_live_ask(con, args.id, agent)
        if a["owner"] != agent:
            sys.exit(f"coord: only the owner ({a['owner'] or 'nobody yet'}) can escalate ask #{args.id}.")
        _set_ask(con, args.id, status="escalated", note=f"{agent}: {args.reason}")
        _ask_note(con, agent, ["all"], "info", f"Question #{args.id} needs the user",
                  f"{agent} could not answer question {args.id} and sent it to the user: {args.reason}", args.id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK"); raise
    print(f"ask #{args.id} escalated to the user")


def cmd_close(args):
    agent = me(args)
    authorize(agent)
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    try:
        a = _load_live_ask(con, args.id, agent)
        if a["asker"] != agent:
            sys.exit("coord: only the asker can close an ask.")
        _set_ask(con, args.id, status="closed")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK"); raise
    print(f"ask #{args.id} closed")


def cmd_asks(args):
    con = connect()
    rows = con.execute("SELECT * FROM asks ORDER BY msg").fetchall()
    if not args.all:
        rows = [r for r in rows if r["status"] in LIVE + ("escalated",)]
    if not rows:
        print("no open questions")
    for r in rows:
        m = con.execute("SELECT subject FROM messages WHERE id=?", (r["msg"],)).fetchone()
        print(f"#{r['msg']} from {r['asker']} ({r['created']}): {m['subject'] if m else ''}\n  {ask_line(con, r['msg'])}")


def owed_asks(con, agent):
    """Asks the agent must still act on: assigned/taken to it, plus unclaimed ones it could take."""
    mine = con.execute("SELECT * FROM asks WHERE owner=? AND status IN ('assigned','taken')", (agent,)).fetchall()
    openq = [r for r in con.execute("SELECT * FROM asks WHERE status='open' AND asker<>?", (agent,))
             if agent in eligible_agents(con, r["asker"], r["msg"])]
    return mine, openq


def cmd_post(args):
    summary = " ".join((args.summary or "").split())
    if args.ask and "all" not in [r.strip() for r in args.to.split(",")]:
        sys.exit("coord: --ask only makes sense with --to all.")
    if not summary:
        sys.exit("coord: refused. Add --summary \"one or two plain sentences\" (at most %d characters): the "
                 "gist of this message in words that can be read aloud, with no formulas, paths or file names. "
                 "The subject is the headline; the body can stay as long as it needs." % SUMMARY_MAX)
    if len(summary) > SUMMARY_MAX:
        sys.exit("coord: refused. --summary is %d characters; shorten it to at most %d." % (len(summary), SUMMARY_MAX))
    rec = [r.strip() for r in args.to.split(",") if r.strip()] or ["all"]
    authorize(me(args), rec)
    con = connect()
    room = pick_room(con, me(args), args.room, rec, args.reply_to)
    if room:
        outside = [r for r in rec if r != "all" and r not in room_mates(con, room)]
        if outside:
            sys.exit(f"coord: refused. {', '.join(outside)} not in room '{room}'.")
    cur = con.execute(
        "INSERT INTO messages(ts,sender,recipients,kind,subject,body,reply_to,refs,summary,room) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now(), me(args), json.dumps(rec), args.kind, args.subject, body_from(args), args.reply_to,
         json.dumps(args.ref or []), summary, room))
    con.execute("INSERT OR IGNORE INTO reads(agent,msg,ts) VALUES(?,?,?)", (me(args), cur.lastrowid, now()))
    mid = cur.lastrowid
    if "all" in rec and not args.no_ask and (args.ask or args.kind == "request"):
        con.execute("INSERT INTO asks(msg,asker,status,hops,created,updated) VALUES(?,?,?,?,?,?)",
                    (mid, me(args), "open", 0, now(), now()))
        print(f"posted #{mid}: question to everyone. One agent must `coord take {mid}` (or `coord propose {mid} --agent NAME`); "
              f"at most {MAX_HOPS} handovers, then the holder answers or escalates to the user.")
    else:
        print(f"posted #{mid}" + (f" in room {room}" if room else ""))
    if args.reply_to:                                    # a reply from the owner (or first answerer) closes the ask
        a = get_ask(con, args.reply_to)
        if a and a["status"] in LIVE and me(args) != a["asker"] and args.kind in ("answer", "result"):
            if a["status"] == "open" or a["owner"] == me(args):
                _set_ask(con, args.reply_to, status="answered", owner=me(args))
                print(f"ask #{args.reply_to} marked answered by {me(args)}")


def unread(con, agent):
    rows = con.execute(
        "SELECT * FROM messages m WHERE sender<>? AND NOT EXISTS"
        " (SELECT 1 FROM reads r WHERE r.agent=? AND r.msg=m.id) ORDER BY id", (agent, agent)).fetchall()
    return [r for r in rows if addressed(r, agent, con)]


def cmd_inbox(args):
    con = connect()
    agent = me(args)
    rows = unread(con, agent)
    if not rows:
        print("inbox empty")
    for r in rows:
        print(fmt(r, full=not args.brief))
        print()
        if not args.peek:
            con.execute("INSERT OR IGNORE INTO reads(agent,msg,ts) VALUES(?,?,?)", (agent, r["id"], now()))
    pend = pending_rows(con, agent)
    if pend:
        print(f"{len(pend)} request(s) to you not yet acknowledged: " + ", ".join(f"#{r['id']}" for r in pend))
    mine, openq = owed_asks(con, agent)
    for r in mine:
        print(f"YOU OWE AN ANSWER to #{r['msg']} ({r['status']}): `coord take {r['msg']}` then post with --reply-to {r['msg']}, "
              f"or pass it on / `coord escalate` it.")
    for r in openq:
        print(f"UNCLAIMED QUESTION #{r['msg']} from {r['asker']}: nobody has taken it; `coord take {r['msg']}` or `coord propose {r['msg']} --agent NAME`.")
    print(f"(Keep the chat armed:  {arm_line(agent)}  in the background.)")


WAIT_SECONDS = 300        # agents re-arm the watch at least this often, so nobody is out of touch for long


def arm_line(agent):
    return f"{COORD_CMD} wait --me {shlex.quote(agent)} --timeout {WAIT_SECONDS}"


def stay_tuned(agent):
    """The standing instruction, repeated wherever an agent is told what to do next."""
    return (f"Keep the chat armed: run this in the background (it returns when a message for you arrives, "
            f"or after {WAIT_SECONDS // 60} minutes):\n  {arm_line(agent)}\n"
            f"Each time it returns: run  {COORD_CMD} inbox --me {shlex.quote(agent)}, act on what is for you, then run the wait again.")


def cmd_wait(args):
    con = connect()
    agent = me(args)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        rows = unread(con, agent)
        if rows:
            print(f"{len(rows)} unread for {agent}: " + "; ".join(f"#{r['id']} {r['sender']}: {r['subject']}" for r in rows))
            print(f"Now: {COORD_CMD} inbox --me {shlex.quote(agent)}  -- then re-arm:  {arm_line(agent)}")
            return
        time.sleep(args.interval)
    mine, openq = owed_asks(con, agent)
    extra = f" (you still owe answers: {', '.join('#' + str(r['msg']) for r in mine)})" if mine else ""
    print(f"no new messages for {agent} within {args.timeout}s{extra}. Re-arm:  {arm_line(agent)}")


def cmd_show(args):
    con = connect()
    r = con.execute("SELECT * FROM messages WHERE id=?", (args.id,)).fetchone()
    if not r:
        sys.exit(f"no message #{args.id}")
    print(fmt(r))
    acks = con.execute("SELECT * FROM acks WHERE msg=?", (args.id,)).fetchall()
    for a in acks:
        print(f"  ack by {a['agent']} {a['ts']}" + (f": {a['note']}" if a["note"] else ""))


def cmd_thread(args):
    con = connect()
    root = args.id
    while True:
        r = con.execute("SELECT reply_to FROM messages WHERE id=?", (root,)).fetchone()
        if not r or not r["reply_to"]:
            break
        root = r["reply_to"]
    ids, frontier = [root], [root]
    while frontier:
        q = ",".join("?" * len(frontier))
        frontier = [r["id"] for r in con.execute(f"SELECT id FROM messages WHERE reply_to IN ({q})", frontier)]
        ids += frontier
    for r in con.execute(f"SELECT * FROM messages WHERE id IN ({','.join('?'*len(ids))}) ORDER BY id", ids):
        print(fmt(r))
        print()


def cmd_log(args):
    con = connect()
    sql, par = "SELECT * FROM messages WHERE 1=1", []
    if args.sender:
        sql += " AND sender=?"; par.append(args.sender)
    if args.kind:
        sql += " AND kind=?"; par.append(args.kind)
    if args.grep:
        sql += " AND (subject LIKE ? OR body LIKE ?)"; par += [f"%{args.grep}%"] * 2
    rows = con.execute(sql + " ORDER BY id DESC LIMIT ?", par + [args.last]).fetchall()
    for r in reversed(rows):
        print(fmt(r, full=args.full))
        print()


def overlaps(a, b):
    a, b = set(a), set(b)
    return a & b


def cmd_claim(args):
    agent = me(args)
    authorize(agent)
    con = connect()
    files = args.file or []
    con.execute("BEGIN IMMEDIATE")
    try:
        active = con.execute("SELECT * FROM claims WHERE released IS NULL AND agent<>?", (agent,)).fetchall()
        clash = [c for c in active if c["item"].lower() == args.item.lower() or overlaps(json.loads(c["files"]), files)]
        if clash and not args.force:
            con.execute("ROLLBACK")
            for c in clash:
                print(f"CONFLICT with claim #{c['id']} by {c['agent']} ({c['ts']}): {c['item']} files={json.loads(c['files'])}")
            sys.exit(2)
        cur = con.execute("INSERT INTO claims(ts,agent,item,files,note) VALUES(?,?,?,?,?)",
                          (now(), agent, args.item, json.dumps(files), args.note or ""))
        con.execute("INSERT INTO messages(ts,sender,recipients,kind,subject,body,refs,room) VALUES(?,?,?,?,?,?,?,?)",
                    (now(), agent, json.dumps(["all"]), "claim-note", f"CLAIM #{cur.lastrowid}: {args.item}",
                     (args.note or "") + ("\n(forced despite conflict with " + ", ".join(f"#{c['id']}" for c in clash) + ")" if clash else ""),
                     json.dumps(files), own_room(con, agent)))
        con.execute("COMMIT")
        print(f"claim #{cur.lastrowid} recorded")
    except SystemExit:
        raise
    except Exception:
        con.execute("ROLLBACK")
        raise


def cmd_release(args):
    agent = me(args)
    authorize(agent)
    con = connect()
    c = con.execute("SELECT * FROM claims WHERE id=?", (args.id,)).fetchone()
    if not c or c["released"]:
        sys.exit(f"no active claim #{args.id}")
    if c["agent"] != agent and not args.force:
        sys.exit(f"claim #{args.id} belongs to {c['agent']} (use --force only with their agreement)")
    con.execute("UPDATE claims SET released=? WHERE id=?", (now(), args.id))
    con.execute("INSERT INTO messages(ts,sender,recipients,kind,subject,body,refs,room) VALUES(?,?,?,?,?,?,?,?)",
                (now(), agent, json.dumps(["all"]), "claim-note", f"RELEASE #{args.id}: {c['item']}", args.note or "", "[]", own_room(con, agent)))
    print(f"claim #{args.id} released")


def cmd_claims(args):
    con = connect()
    rows = con.execute("SELECT * FROM claims WHERE released IS NULL ORDER BY id").fetchall()
    if not rows:
        print("no active claims")
    for c in rows:
        print(f"#{c['id']} {c['agent']:<10} {c['ts']}  {c['item']}" + (f"  files={json.loads(c['files'])}" if json.loads(c["files"]) else ""))


def cmd_ack(args):
    authorize(me(args))
    con = connect()
    con.execute("INSERT OR REPLACE INTO acks(agent,msg,ts,note) VALUES(?,?,?,?)", (me(args), args.id, now(), args.note))
    print(f"acknowledged #{args.id}")


def pending_rows(con, agent):
    rows = con.execute("SELECT * FROM messages m WHERE kind='request' AND sender<>? AND NOT EXISTS"
                       " (SELECT 1 FROM acks a WHERE a.agent=? AND a.msg=m.id) ORDER BY id", (agent, agent)).fetchall()
    return [r for r in rows if agent in json.loads(r["recipients"])]


def cmd_pending(args):
    con = connect()
    rows = pending_rows(con, me(args))
    if not rows:
        print("no pending requests")
    for r in rows:
        print(fmt(r, full=False))


def cmd_export(args):
    con = connect()
    out = ["# Coordination board export", "", f"Generated {now()} from {os.environ.get('COORD_DB', DEFAULT_DB)}.", ""]
    claims = con.execute("SELECT * FROM claims WHERE released IS NULL ORDER BY id").fetchall()
    out += ["## Active claims", ""] + ([f"- #{c['id']} **{c['agent']}**: {c['item']}" for c in claims] or ["- none"]) + [""]
    out += ["## Messages", ""]
    for r in con.execute("SELECT * FROM messages ORDER BY id"):
        rec = ",".join(json.loads(r["recipients"]))
        out.append(f"### #{r['id']} {r['sender']} -> {rec} [{r['kind']}] {r['subject']}")
        out.append(f"_{r['ts']}" + (f", reply to #{r['reply_to']}" if r["reply_to"] else "") + "_")
        out.append("")
        if r["summary"]:
            out.append("**Summary:** " + r["summary"])
            out.append("")
        out.append(r["body"].rstrip())
        out.append("")
    with open(args.path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"exported to {args.path}")


# ---------------------------------------------------------------------------
# One admin password for every chat on this machine (the super-admin page and
# admin rights in every chat page). Set only from an interactive terminal.
# ---------------------------------------------------------------------------
ADMIN_PW = os.path.join(DATA_DIR, "admin.password")
ADMIN_SESSIONS = os.path.join(DATA_DIR, "admin.sessions.json")


def _scrypt(password, salt):
    import hashlib
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def password_is_set():
    return os.path.exists(ADMIN_PW)


def check_password(password):
    import hmac
    try:
        with open(ADMIN_PW, encoding="utf-8") as f:
            salt_hex, key_hex = f.read().strip().split(":")
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(_scrypt(password, bytes.fromhex(salt_hex)).hex(), key_hex)


def write_password(password):
    os.makedirs(DATA_DIR, exist_ok=True)
    salt = os.urandom(16)
    fd = os.open(ADMIN_PW, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(salt.hex() + ":" + _scrypt(password, salt).hex())
    try:
        os.unlink(ADMIN_SESSIONS)          # a new password signs everyone out
    except OSError:
        pass


def cmd_set_password(args):
    import getpass
    require_human_terminal("set-password")
    pw = getpass.getpass("New admin password for all agent chats on this machine: ")
    if len(pw) < 8:
        sys.exit("coord: use at least 8 characters.")
    if getpass.getpass("Repeat it: ") != pw:
        sys.exit("coord: the two entries differ; nothing changed.")
    write_password(pw)
    print(f"Admin password set ({ADMIN_PW}). Open any chat page and go to /admin to sign in; earlier sign-ins were ended.")


def code_hash(code):
    import hashlib
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def cmd_room_create(args):
    import secrets
    require_human_terminal("room-create")
    con = connect()
    if con.execute("SELECT 1 FROM rooms WHERE name=?", (args.name,)).fetchone():
        sys.exit(f"room '{args.name}' exists; use room-close first to reissue its code")
    code = f"{args.name}:{secrets.token_urlsafe(12)}"
    con.execute("INSERT INTO rooms(name,code_hash,created) VALUES(?,?,?)", (args.name, code_hash(code), now()))
    print(f"Room '{args.name}' created. Its join code is shown only once:\n\n  {code}\n")
    print("Paste this line into each agent's chat, with the agent's name filled in:\n")
    print(f"  {COORD_CMD} join '{code}' --me <AgentName>")


def cmd_room_close(args):
    require_human_terminal("room-close")
    con = connect()
    con.execute("DELETE FROM members WHERE room=?", (args.name,))
    con.execute("DELETE FROM rooms WHERE name=?", (args.name,))
    print(f"room '{args.name}' closed; all its members lost posting rights")


def cmd_kick(args):
    require_human_terminal("kick")
    con = connect()
    con.execute("DELETE FROM members WHERE room=? AND agent=?", (args.room, args.agent))
    print(f"removed {args.agent} from {args.room}")


def new_invite(con, room, agent=None):
    """A single-use invite to room, optionally only for one agent name. The room's own code stays valid."""
    import secrets
    code = f"{room}:{secrets.token_urlsafe(12)}"
    con.execute("INSERT INTO invites(room,code_hash,agent,created) VALUES(?,?,?,?)", (room, code_hash(code), agent or None, now()))
    return code


def cmd_invite(args):
    require_human_terminal("invite")
    con = connect()
    if not con.execute("SELECT 1 FROM rooms WHERE name=?", (args.room,)).fetchone():
        sys.exit(f"no room '{args.room}'")
    code = new_invite(con, args.room, args.agent)
    who = args.agent or "<AgentName>"
    print(f"Single-use invite{' for ' + args.agent if args.agent else ''} (shown once; the room code and other invites stay valid):\n")
    print(f"  {COORD_CMD} join '{code}' --me '{who}'")


def cmd_join(args):
    import hmac
    agent = me(args)
    name = args.code.split(":", 1)[0]
    con = connect()
    con.execute("BEGIN IMMEDIATE")                       # a single-use invite can be used only once, even by two agents at the same moment
    try:
        row = con.execute("SELECT code_hash FROM rooms WHERE name=?", (name,)).fetchone()
        if not row:
            sys.exit("coord: invalid join code")
        if not hmac.compare_digest(row["code_hash"], code_hash(args.code)):
            inv = con.execute("SELECT * FROM invites WHERE room=? AND code_hash=?", (name, code_hash(args.code))).fetchone()
            if not inv:
                sys.exit("coord: invalid join code")
            if inv["revoked"]:
                sys.exit("coord: this invite was revoked by the user")
            if inv["used"]:
                sys.exit(f"coord: this invite was already used (by {inv['used_by']})")
            if inv["agent"] and inv["agent"] != agent:
                sys.exit(f"coord: this invite is for '{inv['agent']}', not '{agent}'")
            con.execute("UPDATE invites SET used=?, used_by=? WHERE id=?", (now(), agent, inv["id"]))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    con.execute("INSERT OR IGNORE INTO members(room,agent,joined) VALUES(?,?,?)", (name, agent, now()))
    con.execute("INSERT INTO messages(ts,sender,recipients,kind,subject,body,refs,room) VALUES(?,?,?,?,?,?,?,?)",
                (now(), agent, json.dumps(["all"]), "info", f"{agent} joined room {name}", "", "[]", name))
    print(f"{agent} joined room '{name}'.\n")
    print("From now on:")
    print(f"1. {stay_tuned(agent)}")
    print(f"2. Post with  {COORD_CMD} post --me {shlex.quote(agent)} --to all|NAME --kind info|request|result|answer|review "
          "--subject '...' --summary 'one or two plain sentences' --body '...'  (the summary is required; reply with --reply-to ID).")
    print(f"3. Acknowledge requests to you ({COORD_CMD} pending, then ack ID), and when a question to everyone fits you, take it ({COORD_CMD} asks, then take ID).")
    print(f"4. Before editing shared files, claim them ({COORD_CMD} claim --item ... --file ...); release when done.")
    print("Full guide: the README next to coord.py, section 'For agents'.")


def cmd_rooms(args):
    con = connect()
    rows = con.execute("SELECT * FROM rooms ORDER BY name").fetchall()
    if not rows:
        print("no rooms")
    for r in rows:
        mem = [m["agent"] for m in con.execute("SELECT agent FROM members WHERE room=? ORDER BY joined", (r["name"],))]
        print(f"{r['name']} (created {r['created']}): {', '.join(mem) or 'no members'}")


def cmd_policy(args):
    pol = load_policy()
    print(f"policy file: {policy_path()}")
    if pol is None:
        print("NOT CONFIGURED: all writes are refused until the user creates this file.")
        return
    for a, spec in pol.get("agents", {}).items():
        print(f"  {a}: may address {spec.get('may_address', [])}")


def main():
    p = argparse.ArgumentParser(prog="coord", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--me", help="agent name (default: $COORD_AGENT)")
        s.set_defaults(fn=fn)
        return s

    s = add("post", cmd_post, "post a message")
    s.add_argument("--to", default="all", help="comma-separated agent names, or all")
    s.add_argument("--kind", choices=KINDS, default="info")
    s.add_argument("--subject", required=True, help="headline")
    s.add_argument("--summary", help="REQUIRED: 1-2 plain sentences (max 300 chars) saying what this message is about; it is what gets read aloud")
    s.add_argument("--body"); s.add_argument("--body-file")
    s.add_argument("--reply-to", type=int)
    s.add_argument("--room", help="which room this message is for (needed only if you are in several and it is not implied)")
    s.add_argument("--ask", action="store_true", help="with --to all: someone must answer (automatic for --kind request)")
    s.add_argument("--no-ask", action="store_true", help="a request to all that needs no single answerer")
    s.add_argument("--ref", action="append", help="file path or artifact this message refers to (repeatable)")
    s = add("inbox", cmd_inbox, "show and mark read my unread messages")
    s.add_argument("--peek", action="store_true"); s.add_argument("--brief", action="store_true")
    s = add("wait", cmd_wait, "block until I have unread messages")
    s.add_argument("--timeout", type=float, default=WAIT_SECONDS); s.add_argument("--interval", type=float, default=5)
    s = add("show", cmd_show, "show one message"); s.add_argument("id", type=int)
    s = add("thread", cmd_thread, "show a message with its replies"); s.add_argument("id", type=int)
    s = add("log", cmd_log, "recent messages")
    s.add_argument("--last", type=int, default=15); s.add_argument("--sender"); s.add_argument("--kind")
    s.add_argument("--grep"); s.add_argument("--full", action="store_true")
    s = add("claim", cmd_claim, "claim a work item")
    s.add_argument("--item", required=True); s.add_argument("--file", action="append")
    s.add_argument("--note"); s.add_argument("--force", action="store_true")
    s = add("release", cmd_release, "release a claim"); s.add_argument("id", type=int)
    s.add_argument("--note"); s.add_argument("--force", action="store_true")
    add("claims", cmd_claims, "list active claims")
    s = add("ack", cmd_ack, "acknowledge a request"); s.add_argument("id", type=int); s.add_argument("--note")
    s = add("take", cmd_take, "take a question addressed to everyone: you will answer it"); s.add_argument("id", type=int)
    s = add("propose", cmd_propose, "propose a better-placed agent for a question addressed to everyone (max %d handovers)" % MAX_HOPS)
    s.add_argument("id", type=int); s.add_argument("--agent", required=True); s.add_argument("--reason")
    s = add("escalate", cmd_escalate, "owner gives up a question: it goes to the user"); s.add_argument("id", type=int); s.add_argument("--reason", required=True)
    s = add("close", cmd_close, "asker withdraws a question"); s.add_argument("id", type=int)
    s = add("asks", cmd_asks, "list unanswered questions to everyone"); s.add_argument("--all", action="store_true")
    add("pending", cmd_pending, "requests to me not yet acknowledged")
    add("policy", cmd_policy, "show the user-configured authorization policy (read-only)")
    s = add("room-create", cmd_room_create, "USER ONLY (interactive terminal): create a room, print its join code")
    s.add_argument("name")
    s = add("room-close", cmd_room_close, "USER ONLY: close a room and revoke all its members"); s.add_argument("name")
    s = add("kick", cmd_kick, "USER ONLY: remove one agent from a room")
    s.add_argument("room"); s.add_argument("agent")
    s = add("join", cmd_join, "join a room with a code the user gave you"); s.add_argument("code")
    add("set-password", cmd_set_password, "USER ONLY (interactive terminal): set the admin password for all chats on this machine")
    s = add("invite", cmd_invite, "USER ONLY: a single-use invite to a room (optionally for one agent); other codes stay valid")
    s.add_argument("room"); s.add_argument("--agent")
    add("rooms", cmd_rooms, "list rooms and their members")
    s = add("export", cmd_export, "export the board as Markdown")
    s.add_argument("path")
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
