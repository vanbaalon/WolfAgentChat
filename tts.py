"""Server-side speech for the coord web view.

The page never uses the browser's speech API: in Chrome that API has one queue
shared by every tab of a site, so any tab's cancel() silences all the others,
and voices load late and start unpredictably. Instead this module renders the
text with the operating system's own engine (macOS `say`, else espeak-ng or
espeak) into WAV files, cached on disk, and the page plays them with an
ordinary <audio> element. Each tab plays its own files; nothing is shared.

Voices are assigned per agent here, once, and stored next to the board
database, so every tab and every browser uses the same voice for an agent.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading
from concurrent.futures import ThreadPoolExecutor

ENGINE = next((e for e in ("say", "espeak-ng", "espeak") if shutil.which(e)), None)
CACHE = os.path.join(os.path.expanduser("~/Library/Caches" if sys.platform == "darwin" else "~/.cache"), "agent-coord-tts")
CACHE_KEEP = 400            # newest rendered files kept
RATE_WPM = 175              # words per minute at speech rate 1.0

# Novelty and robotic voices that macOS lists among the English ones.
NOVELTY = re.compile(r"\b(albert|bad news|bahh|bells|boing|bubbles|cellos|deranged|good news|hysterical|jester|junior|"
                     r"kathy|organ|princess|ralph|superstar|trinoids|whisper|wobble|zarvox|fred|eddy|flo|grandma|"
                     r"grandpa|reed|rocko|sandy|shelley)\b", re.I)
FEM = re.compile(r"\b(samantha|karen|moira|tessa|serena|fiona|victoria|allison|ava|susan|kate|stephanie|tara|veena|"
                 r"catherine|zoe|nicky|female)\b|\+f\d", re.I)
MAL = re.compile(r"\b(daniel|alex|oliver|rishi|tom|aaron|arthur|gordon|lee|aman|jamie|evan|nathan|male)\b", re.I)
ESPEAK_VOICES = [("en-us", "en-US"), ("en-us+f3", "en-US"), ("en-gb", "en-GB"), ("en-gb+f3", "en-GB"),
                 ("en-gb-scotland", "en-GB"), ("en-gb-x-rp+f2", "en-GB")]

_voices = None
_voices_lock = threading.Lock()
_started, _start_lock = False, threading.Lock()
_key_locks, _key_locks_guard = {}, threading.Lock()
_render_slots = threading.BoundedSemaphore(2)   # at most two `say` processes at a time


def gender(name):
    return "f" if FEM.search(name) else "m" if MAL.search(name) else "?"


def base(name):
    """Speaker identity: 'Daniel (Enhanced)' and 'Daniel' are the same person."""
    return re.sub(r"\s*\(.*\)\s*$", "", name)


def quality(name):
    return 0 if re.search(r"premium", name, re.I) else 1 if re.search(r"enhanced", name, re.I) else 2


def _render(voice, wpm, text, out):
    """Render text to a WAV file at out (atomically)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)                                  # a file, so text starting with '-' is never an option
        txt = f.name
    part = out + ".part.wav"
    try:
        if ENGINE == "say":
            cmd = ["say", "-v", voice, "-r", str(wpm), "--file-format=WAVE", "--data-format=LEI16@22050", "-o", part, "-f", txt]
        else:
            cmd = [ENGINE, "-v", voice, "-s", str(wpm), "-w", part, "-f", txt]
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) < 1000:
            raise RuntimeError((r.stderr or b"").decode(errors="replace").strip() or "no audio produced")
        os.replace(part, out)
    finally:
        for p in (txt, part):
            try:
                os.unlink(p)
            except OSError:
                pass


def voices(block=True):
    """English, non-novelty voices that actually render (a listed voice may not be downloaded). Computed once.

    Checking takes several seconds; with block=False this returns None until it is done (and starts it),
    so a web request never waits for it."""
    global _voices, _started
    if _voices is not None:
        return _voices
    if not block:
        with _start_lock:
            if not _started:
                _started = True
                threading.Thread(target=voices, daemon=True).start()
        return None
    with _voices_lock:
        if _voices is not None:
            return _voices
        found = []
        if ENGINE == "say":
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=60).stdout
            for line in out.splitlines():
                m = re.match(r"^(.+?)\s+([a-z]{2,3}_[A-Za-z0-9]{2,4})\s+#", line)
                if m and m.group(2).startswith("en_") and not NOVELTY.search(m.group(1)):
                    found.append({"name": m.group(1).strip(), "lang": m.group(2).replace("_", "-")})
        elif ENGINE:
            found = [{"name": n, "lang": l} for n, l in ESPEAK_VOICES]

        def works(v):
            out = os.path.join(tempfile.gettempdir(), "coord-voice-%d-%s.wav" % (os.getpid(), hashlib.sha1(v["name"].encode()).hexdigest()[:10]))
            try:
                _render(v["name"], RATE_WPM, "Test.", out)
                return True
            except Exception:
                return False
            finally:
                try:
                    os.unlink(out)
                except OSError:
                    pass

        with ThreadPoolExecutor(4) as ex:
            ok = list(ex.map(works, found))
        _voices = sorted((dict(v, gender=gender(v["name"]), quality=quality(v["name"])) for v, k in zip(found, ok) if k),
                         key=lambda v: (v["lang"], v["name"]))
        return _voices


def voice_names():
    return {v["name"] for v in voices()}


class Assignments:
    """agent -> voice, stored as JSON; new agents get a distinct speaker, balancing men/women and mixing accents."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def _pick(self, d, agent):
        vs = voices()
        if not vs:
            return None
        by_name = {v["name"]: v for v in vs}
        used = [by_name[x] for x in d.values() if x in by_name]
        used_bases = {base(v["name"]) for v in used}
        best = {}
        for v in vs:                                   # one entry per speaker: its best-quality variant
            b = base(v["name"])
            if b not in best or v["quality"] < best[b]["quality"]:
                best[b] = v
        pool = [v for b, v in best.items() if b not in used_bases] or list(best.values())
        f = sum(v["gender"] == "f" for v in used)
        m = sum(v["gender"] == "m" for v in used)
        want = "f" if f < m else "m" if m < f else ("f" if int(hashlib.sha1(agent.encode()).hexdigest(), 16) % 2 else "m")
        langs = {v["lang"] for v in used}
        pool.sort(key=lambda v: (v["gender"] != want, v["lang"] in langs, v["quality"], v["name"]))
        return pool[0]["name"]

    def get(self, agents):
        names = voice_names()
        with self.lock:
            d, changed = self._load(), False
            for a in agents:
                if d.get(a) not in names:
                    v = self._pick(d, a)
                    if v:
                        d[a], changed = v, True
            if changed:
                self._save(d)
            return {a: d[a] for a in agents if d.get(a) in names}

    def set(self, agent, voice):
        if voice not in voice_names():
            raise ValueError("unknown voice")
        with self.lock:
            d = self._load()
            d[agent] = voice
            self._save(d)


# ---------------------------------------------------------------------------
# What is read aloud: the agent's name, the message type, the headline and the
# short summary, never the body, with formulas, code, paths, file names and links removed.
# ---------------------------------------------------------------------------
_FILE_EXT = "tex|nb|wl|wls|py|md|json|txt|pdf|csv|sqlite|cpp|hpp|png|npz|out|log|sh|html"


def scrub(x):
    x = x or ""
    x = re.sub(r"```[\s\S]*?```", " ", x)
    x = re.sub(r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\(.*?\\\)|\$[^$\n]+\$", " ", x)
    x = re.sub(r"`([^`]*)`", lambda m: m.group(1) if len(m.group(1)) <= 12 and not re.search(r"[/\\._=(){}\[\]]", m.group(1)) else " ", x)
    x = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", x)
    x = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", " ", x, flags=re.I)
    x = re.sub(r"\S*[/\\]\S*", " ", x)
    x = re.sub(r"\b[\w-]+(?:\.[\w-]+)*\.(?:%s)\b" % _FILE_EXT, " ", x, flags=re.I)
    x = re.sub(r"\b\w*_\w*\b", " ", x)
    x = re.sub(r"\S{26,}", " ", x)
    x = re.sub(r"\S*[=^{}<>±∑∫√≈≠≤≥→]\S*", " ", x)
    x = re.sub(r"[#*_>|~-]+", " ", x)
    return re.sub(r"\s+", " ", x).strip()


SPOKEN_KIND = {"info": "note", "request": "request", "claim-note": "claim", "result": "result",
               "review": "review", "answer": "answer"}


def speech_text(sender, kind, subject, summary):
    """'Alice, result: <headline>. <summary>' -- the body is never read."""
    head, s = scrub(subject), scrub(summary)
    t = head + ("" if not head or head[-1] in ".!?" else ".") + (" " + s if s else "")
    return ("%s, %s: %s" % (sender, SPOKEN_KIND.get(kind, scrub(kind) or "message"), t[:400])).strip()


def test_text(agent):
    return "%s. This is my voice." % agent


def audio_for(text, voice, rate=1.0):
    """Path of a cached WAV of text in voice; rendered on first use."""
    if not ENGINE:
        raise LookupError("no speech engine on this server (macOS say, espeak-ng or espeak)")
    wpm = max(90, min(360, int(round(RATE_WPM * rate))))
    key = hashlib.sha1(("%s|%s|%d|%s" % (ENGINE, voice, wpm, text)).encode()).hexdigest()
    os.makedirs(CACHE, exist_ok=True)
    out = os.path.join(CACHE, key + ".wav")
    if os.path.exists(out):
        return out
    with _key_locks_guard:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:                                          # two tabs asking at once render it once
        if not os.path.exists(out):
            with _render_slots:
                _render(voice, wpm, text, out)
            _prune()
    return out


def _prune():
    try:
        files = sorted((os.path.join(CACHE, f) for f in os.listdir(CACHE) if f.endswith(".wav")), key=os.path.getmtime)
        for p in files[:-CACHE_KEEP]:
            os.unlink(p)
    except OSError:
        pass
