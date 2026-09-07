#!/usr/bin/env python3
"""
Local web UI for frenenemy. Stdlib only, no extra installs.

  python3 ui.py            # opens http://127.0.0.1:8765 in your browser
  python3 ui.py --port 9000 --no-open

Type or paste the question in a textarea, pick model + effort per side, press Run.
Model lists are account-aware: the page reads which Claude / ChatGPT account is
logged in, then probes every model and effort level with a tiny real call and
caches what works per account. Log in with a different account and it re-detects.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import debate as D  # noqa: E402  (reuses the exact same CLI calls the debate uses)

DEBATE = os.path.join(HERE, "debate.py")
OUT_DIR = os.path.join(HERE, "debates")
CONFIG_FILE = os.path.join(HERE, "debate.config.json")
CODEX_MODELS_CACHE = os.path.expanduser("~/.codex/models_cache.json")
CODEX_AUTH = os.path.expanduser("~/.codex/auth.json")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
AVAIL_FILE = os.path.join(os.path.expanduser("~/.frenenemy"), "availability.json")

JOBS = {}          # id -> dict(proc, lines, done, rc, transcript, question)
DETECT = {"running": False, "lines": [], "done": True, "error": None, "started": None}
LOCK = threading.Lock()

EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

CLAUDE_MODELS = [
    {"slug": "claude-fable-5-1", "display_name": "Fable 5.1", "description": "Most intelligent, Mythos-class"},
    {"slug": "claude-opus-5", "display_name": "Opus 5", "description": "Deep reasoning, most capable Opus"},
    {"slug": "claude-sonnet-5", "display_name": "Sonnet 5", "description": "Balanced speed and intelligence"},
    {"slug": "claude-haiku-4-5-20251001", "display_name": "Haiku 4.5", "description": "Fastest and lightest"},
]
CLAUDE_EFFORTS = [
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth"},
    {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
]
GPT_MODELS_FALLBACK = [
    {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "description": "Flagship tier"},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6 Terra", "description": "Balanced tier"},
    {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6 Luna", "description": "Fast, cheap tier"},
    {"slug": "gpt-5.5", "display_name": "GPT-5.5", "description": "Previous generation"},
]
GPT_EFFORTS_FALLBACK = [{"effort": e, "description": ""} for e in ("low", "medium", "high", "xhigh")]


# ----------------------------------------------------------------------------
# accounts
# ----------------------------------------------------------------------------

def _jwt_claims(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:  # noqa
        return {}


def codex_account():
    try:
        with open(CODEX_AUTH) as f:
            a = json.load(f)
    except (OSError, ValueError):
        return {"logged_in": False, "provider": "gpt", "label": "ChatGPT", "warning": "Codex not logged in. Run `codex login`."}
    tokens = a.get("tokens") or {}
    claims = _jwt_claims(tokens.get("id_token", ""))
    auth = claims.get("https://api.openai.com/auth", {})
    plan = auth.get("chatgpt_plan_type")
    acc_id = auth.get("chatgpt_account_id") or tokens.get("account_id")
    info = {
        "provider": "gpt", "label": "ChatGPT",
        "logged_in": bool(tokens.get("access_token")),
        "email": claims.get("email"),
        "plan": (plan or "").capitalize() or None,
        "id": acc_id,
        "mode": a.get("auth_mode"),
    }
    if not info["logged_in"] and a.get("OPENAI_API_KEY"):
        info["warning"] = "Codex is using an API KEY, which costs credits. Run `codex logout` then `codex login` with ChatGPT."
    elif not info["logged_in"]:
        info["warning"] = "Codex not logged in. Run `codex login`."
    return info


def claude_account():
    try:
        with open(CLAUDE_JSON) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    acc = d.get("oauthAccount") or {}
    st = claude_auth_status()
    org = acc.get("organizationType") or ""
    plans = {"claude_pro": "Pro", "claude_max": "Max", "claude_team": "Team", "claude_enterprise": "Enterprise"}
    plan = plans.get(org) or (st.get("subscriptionType") or "").capitalize() or None
    info = {
        "provider": "claude", "label": "Claude",
        "logged_in": bool(st.get("loggedIn")),
        "email": st.get("email") or acc.get("emailAddress"),
        "plan": plan,
        "id": acc.get("accountUuid") or st.get("email"),
    }
    if not info["logged_in"]:
        info["warning"] = "Claude Code not logged in. Run /login in Claude Code."
    return info


def account_key(info):
    if not info.get("id"):
        return None
    return info["provider"] + ":" + hashlib.sha256(str(info["id"]).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------
# catalogue + availability
# ----------------------------------------------------------------------------

def load_defaults():
    d = {"rounds": 2, "judge": "claude", "claude_effort": "medium", "gpt_effort": "medium",
         "claude_model": "claude-sonnet-5", "gpt_model": "gpt-5.6-sol", "max_words": 400}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            d.update(json.load(f))
    return d


def sort_efforts(efforts):
    return sorted(efforts, key=lambda e: EFFORT_ORDER.index(e["effort"]) if e["effort"] in EFFORT_ORDER else 99)


def gpt_catalogue():
    """Model list Codex cached for this login (a global catalogue, NOT plan-filtered)."""
    try:
        with open(CODEX_MODELS_CACHE) as f:
            data = json.load(f)
        raw = data.get("models", data) if isinstance(data, dict) else data
        models = []
        for m in raw:
            if not isinstance(m, dict) or m.get("visibility", "list") != "list":
                continue
            models.append({
                "slug": m["slug"],
                "display_name": re.sub(r"-(?=[A-Za-z])", " ", m.get("display_name") or m["slug"]),
                "description": m.get("description", ""),
                "efforts": sort_efforts(m.get("supported_reasoning_levels") or GPT_EFFORTS_FALLBACK),
                "default_effort": m.get("default_reasoning_level", "medium"),
                "priority": m.get("priority", 999),
            })
        models.sort(key=lambda m: m["priority"])
        if models:
            return models, True
    except (OSError, ValueError, KeyError):
        pass
    return [dict(m, efforts=GPT_EFFORTS_FALLBACK, default_effort="medium") for m in GPT_MODELS_FALLBACK], False


def claude_catalogue():
    return [dict(m, efforts=list(CLAUDE_EFFORTS), default_effort="medium") for m in CLAUDE_MODELS]


def load_avail():
    try:
        with open(AVAIL_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_avail(data):
    os.makedirs(os.path.dirname(AVAIL_FILE), exist_ok=True)
    with open(AVAIL_FILE, "w") as f:
        json.dump(data, f, indent=2)


def apply_availability(models, record):
    """Annotate catalogue entries with what the probe found for this account."""
    out = []
    for m in models:
        m = dict(m)
        found = (record or {}).get("models", {}).get(m["slug"])
        if found is None:
            m["available"] = None
            m["reason"] = ""
        else:
            m["available"] = bool(found.get("ok"))
            m["reason"] = found.get("reason", "")
            if m["available"]:
                allowed = set(found.get("efforts") or [])
                m["all_efforts"] = m["efforts"]
                m["efforts"] = [e for e in m["efforts"] if e["effort"] in allowed] or m["efforts"]
        out.append(m)
    return out


def models_payload():
    gm, live = gpt_catalogue()
    cm = claude_catalogue()
    avail = load_avail()
    accounts = {"claude": claude_account(), "gpt": codex_account()}
    out = {"defaults": load_defaults(), "accounts": accounts, "detect": detect_state()}
    for prov, models in (("claude", cm), ("gpt", gm)):
        key = account_key(accounts[prov])
        rec = avail.get(key) if key else None
        accounts[prov]["detected_at"] = rec.get("detected_at") if rec else None
        accounts[prov]["needs_detect"] = bool(accounts[prov]["logged_in"]) and rec is None
        out[prov] = {"models": apply_availability(models, rec), "live": live if prov == "gpt" else True}
    return out


# ----------------------------------------------------------------------------
# probing (what can THIS account actually use?)
# ----------------------------------------------------------------------------

def probe_cfg():
    cfg = dict(D.DEFAULTS)
    cfg.update(load_defaults())
    cfg["workdir"] = HERE
    cfg["timeout"] = 180
    cfg["web"] = False
    try:
        cfg["claude_bin"] = D.find_claude(cfg.get("claude_bin", ""))
    except D.SideError:
        cfg["claude_bin"] = ""
    return cfg


def probe_one(cfg, provider, model, effort):
    c = dict(cfg)
    c.update({"claude_model": model, "claude_effort": effort, "gpt_model": model, "gpt_effort": effort})
    fn = D.ask_claude if provider == "claude" else D.ask_gpt
    try:
        fn(c, "You are a connectivity check. Reply with exactly: OK", "Reply with exactly: OK", role="detect")
        return True, ""
    except D.SideError as e:
        msg = str(e).strip().splitlines()
        return False, (msg[-1] if msg else str(e))[-240:]


def detect_state():
    return {"running": DETECT["running"], "done": DETECT["done"], "error": DETECT["error"],
            "lines": len(DETECT["lines"])}


def log(line):
    DETECT["lines"].append(line)


def probe_provider(cfg, provider, models, account, results):
    key = account_key(account)
    if not key:
        log("[%s] not logged in, skipped" % provider)
        return
    if provider == "claude" and not cfg.get("claude_bin"):
        log("[claude] claude binary not found, skipped")
        return
    found = {}
    lock = threading.Lock()

    def work(m):
        efforts = [e["effort"] for e in m["efforts"]]
        lowest = efforts[0]
        ok, reason = probe_one(cfg, provider, m["slug"], lowest)
        log("[%s] %s @ %s ... %s%s" % (provider, m["slug"], lowest, "ok" if ok else "NOT available", "" if ok else " (" + reason + ")"))
        if not ok:
            with lock:
                found[m["slug"]] = {"ok": False, "reason": reason, "efforts": []}
            return
        allowed = [lowest]
        # try efforts from the top down; the first that works caps the list
        for i in range(len(efforts) - 1, 0, -1):
            e = efforts[i]
            ok2, reason2 = probe_one(cfg, provider, m["slug"], e)
            log("[%s] %s @ %s ... %s%s" % (provider, m["slug"], e, "ok" if ok2 else "no", "" if ok2 else " (" + reason2 + ")"))
            if ok2:
                allowed = efforts[: i + 1]
                break
        with lock:
            found[m["slug"]] = {"ok": True, "reason": "", "efforts": allowed}

    threads = []
    sem = threading.Semaphore(3)

    def guarded(m):
        with sem:
            work(m)

    for m in models:
        t = threading.Thread(target=guarded, args=(m,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    results[key] = {
        "provider": provider, "email": account.get("email"), "plan": account.get("plan"),
        "detected_at": datetime.datetime.now().isoformat(timespec="seconds"), "models": found,
    }
    n_ok = sum(1 for v in found.values() if v["ok"])
    log("[%s] done: %d of %d models available for %s" % (provider, n_ok, len(found), account.get("email") or "account"))


def start_detect(providers):
    with LOCK:
        if DETECT["running"]:
            return False
        DETECT.update({"running": True, "done": False, "error": None, "lines": [], "started": time.time()})

    def run():
        try:
            cfg = probe_cfg()
            accounts = {"claude": claude_account(), "gpt": codex_account()}
            cats = {"claude": claude_catalogue(), "gpt": gpt_catalogue()[0]}
            results = {}
            ts = []
            for prov in providers:
                log("[%s] detecting for %s (%s)..." % (prov, accounts[prov].get("email") or "?", accounts[prov].get("plan") or "?"))
                t = threading.Thread(target=probe_provider, args=(cfg, prov, cats[prov], accounts[prov], results), daemon=True)
                t.start()
                ts.append(t)
            for t in ts:
                t.join()
            avail = load_avail()
            avail.update(results)
            save_avail(avail)
            log("Saved to %s" % AVAIL_FILE)
        except Exception as e:  # noqa
            DETECT["error"] = str(e)
            log("ERROR: %s" % e)
        finally:
            DETECT["running"] = False
            DETECT["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return True


# ----------------------------------------------------------------------------
# login / logout
# ----------------------------------------------------------------------------

LOGIN = {
    "claude": {"state": "idle", "auth_url": None, "needs_code": False, "message": "", "log": []},
    "gpt": {"state": "idle", "auth_url": None, "needs_code": False, "message": "", "log": []},
}
LOGIN_PROC = {"claude": None}      # the running `claude auth login` process
CODEX_SESSION = {"proc": None, "login_id": None, "msgs": None}


def claude_auth_status():
    """`claude auth status` is authoritative about the Claude login."""
    try:
        binp = D.find_claude("")
    except D.SideError:
        return {"loggedIn": False, "error": "claude binary not found"}
    try:
        r = subprocess.run([binp, "auth", "status"], capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"loggedIn": False}


def set_login(provider, **kw):
    LOGIN[provider].update(kw)


def start_claude_login():
    try:
        binp = D.find_claude("")
    except D.SideError as e:
        set_login("claude", state="error", message=str(e))
        return
    old = LOGIN_PROC.get("claude")
    if old and old.poll() is None:
        old.terminate()
    set_login("claude", state="starting", auth_url=None, needs_code=False, message="Starting sign-in…", log=[])
    proc = subprocess.Popen([binp, "auth", "login", "--claudeai"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=dict(os.environ, NO_COLOR="1"))
    LOGIN_PROC["claude"] = proc

    def reader():
        buf = ""
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            buf += ch
            if "visit: " in buf and not LOGIN["claude"]["auth_url"]:
                tail = buf.split("visit: ", 1)[1]
                if "\n" in tail or len(tail) > 400:
                    url = tail.split("\n")[0].strip()
                    if url.startswith("http"):
                        set_login("claude", auth_url=url, state="awaiting_browser",
                                  message="Sign in in the browser, then paste the code shown.")
            if "Paste code" in buf and not LOGIN["claude"]["needs_code"]:
                set_login("claude", needs_code=True, state="awaiting_code",
                          message="Paste the code from the browser page below.")
        rc = proc.wait()
        st = claude_auth_status()
        if st.get("loggedIn"):
            set_login("claude", state="done", needs_code=False,
                      message="Signed in as %s" % (st.get("email") or "your account"))
        else:
            set_login("claude", state="error", needs_code=False,
                      message="Sign-in did not complete (exit %s)." % rc)

    threading.Thread(target=reader, daemon=True).start()


def submit_claude_code(code):
    proc = LOGIN_PROC.get("claude")
    if not proc or proc.poll() is not None:
        set_login("claude", state="error", message="Sign-in is no longer running. Start it again.")
        return False
    try:
        proc.stdin.write(code.strip() + "\n")
        proc.stdin.flush()
        set_login("claude", state="verifying", needs_code=False, message="Checking the code…")
        return True
    except (BrokenPipeError, OSError):
        set_login("claude", state="error", message="Could not send the code.")
        return False


def codex_session_start():
    """A long-lived app-server process, needed because it hosts the OAuth callback."""
    codex_session_close()
    proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    msgs = []
    CODEX_SESSION.update({"proc": proc, "msgs": msgs, "login_id": None})

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msgs.append(d)
            if d.get("method") == "account/login/completed":
                ok = (d.get("params") or {}).get("success", True)
                st = D.codex_quota() or {}
                set_login("gpt", state="done" if ok else "error", needs_code=False,
                          message=("Signed in as %s" % st.get("email")) if ok else "Sign-in failed.")

    threading.Thread(target=reader, daemon=True).start()

    def send(i, method, params):
        proc.stdin.write(json.dumps({"id": i, "method": method, "params": params}) + "\n")
        proc.stdin.flush()

    send(0, "initialize", {"clientInfo": {"name": "frenenemy", "version": "1.0"}})
    time.sleep(0.8)
    return proc, msgs, send


def codex_session_close():
    proc = CODEX_SESSION.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    CODEX_SESSION.update({"proc": None, "login_id": None, "msgs": None})


def start_codex_login():
    set_login("gpt", state="starting", auth_url=None, needs_code=False, message="Starting sign-in…")
    try:
        proc, msgs, send = codex_session_start()
    except FileNotFoundError:
        set_login("gpt", state="error", message="`codex` not found. Install it with: npm install -g @openai/codex")
        return
    send(1, "account/login/start", {"type": "chatgpt"})
    deadline = time.time() + 15
    while time.time() < deadline:
        for d in list(msgs):
            if d.get("id") == 1 and "result" in d:
                r = d["result"]
                CODEX_SESSION["login_id"] = r.get("loginId")
                set_login("gpt", state="awaiting_browser", auth_url=r.get("authUrl"),
                          message="Finish signing in to ChatGPT in the browser tab.")
                return
            if d.get("id") == 1 and "error" in d:
                set_login("gpt", state="error", message=str(d["error"])[:300])
                return
        time.sleep(0.3)
    set_login("gpt", state="error", message="Codex did not return a sign-in link.")


def cancel_login(provider):
    if provider == "claude":
        proc = LOGIN_PROC.get("claude")
        if proc and proc.poll() is None:
            proc.terminate()
    else:
        proc = CODEX_SESSION.get("proc")
        lid = CODEX_SESSION.get("login_id")
        if proc and proc.poll() is None and lid:
            try:
                proc.stdin.write(json.dumps({"id": 99, "method": "account/login/cancel",
                                             "params": {"loginId": lid}}) + "\n")
                proc.stdin.flush()
                time.sleep(0.5)
            except (BrokenPipeError, OSError):
                pass
        codex_session_close()
    set_login(provider, state="idle", auth_url=None, needs_code=False, message="Sign-in cancelled.")


def do_logout(provider):
    if provider == "claude":
        try:
            binp = D.find_claude("")
        except D.SideError:
            return False
        cmd = [binp, "auth", "logout"]
    else:
        codex_session_close()
        cmd = ["codex", "logout"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        set_login(provider, state="idle", auth_url=None, needs_code=False, message="Signed out.")
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def login_payload():
    out = {}
    for p in ("claude", "gpt"):
        st = dict(LOGIN[p])
        st.pop("log", None)
        out[p] = st
    out["accounts"] = {"claude": claude_account(), "gpt": codex_account()}
    return out


# ----------------------------------------------------------------------------
# quota / usage
# ----------------------------------------------------------------------------

def codex_quota():
    return D.codex_quota()


def stored_quota(provider):
    try:
        with open(D.QUOTA_FILE) as f:
            return json.load(f).get(provider)
    except (OSError, ValueError):
        return None


def usage_totals():
    """Aggregate the usage log into today / this week / all time, per side."""
    empty = lambda: {"calls": 0, "in": 0, "out": 0, "think": 0, "cost": 0.0}
    buckets = {p: {"today": empty(), "week": empty(), "all": empty()} for p in ("claude", "gpt")}
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    try:
        with open(D.USAGE_LOG) as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        prov = rec.get("provider")
        if prov not in buckets:
            continue
        try:
            day = datetime.datetime.fromisoformat(rec["at"]).date()
        except (KeyError, ValueError):
            day = today
        u = rec.get("usage") or {}
        scopes = ["all"] + (["week"] if day >= week_start else []) + (["today"] if day == today else [])
        for sc in scopes:
            b = buckets[prov][sc]
            b["calls"] += 1
            b["in"] += u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            b["out"] += u.get("output_tokens", 0)
            b["think"] += u.get("thinking_tokens", 0)
            b["cost"] += rec.get("cost_usd") or 0.0
    return buckets


def quota_payload():
    cq = stored_quota("claude")
    if cq:
        acct = claude_account()
        cq = dict(cq, plan=acct.get("plan"), email=acct.get("email"))
    return {
        "claude": cq,
        "gpt": codex_quota(),
        "usage": usage_totals(),
        "claude_note": "Claude reports its limits only while answering, so this is from the most recent call.",
    }


def refresh_claude_quota():
    """One tiny Haiku call, purely to make Claude report fresh limit numbers."""
    cfg = probe_cfg()
    if not cfg.get("claude_bin"):
        return False
    cfg.update({"claude_model": "claude-haiku-4-5-20251001", "claude_effort": "low"})
    try:
        D.ask_claude(cfg, "Reply with exactly: OK", "Reply with exactly: OK", role="quota-check")
        return True
    except D.SideError:
        return False


# ----------------------------------------------------------------------------
# debate jobs
# ----------------------------------------------------------------------------

def start_job(params):
    job_id = uuid.uuid4().hex[:8]
    cmd = [sys.executable, "-u", DEBATE, "-",
           "--rounds", str(params.get("rounds", 2)),
           "--judge", params.get("judge", "claude"),
           "--claude-effort", params.get("claude_effort", "medium"),
           "--gpt-effort", params.get("gpt_effort", "medium"),
           "--claude-model", params.get("claude_model", "claude-sonnet-5"),
           "--gpt-model", params.get("gpt_model", "gpt-5.6-sol"),
           "--max-words", str(params.get("max_words", 400)),
           "--out-dir", OUT_DIR]
    if params.get("web"):
        cmd.append("--web")
    if params.get("solo") in ("claude", "gpt"):
        cmd += ["--solo", params["solo"]]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=HERE, bufsize=1)
    job = {"proc": proc, "lines": [], "done": False, "rc": None, "transcript": None,
           "question": params["question"], "started": time.time()}
    with LOCK:
        JOBS[job_id] = job

    def feed():
        try:
            proc.stdin.write(params["question"])
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def pump():
        for line in proc.stdout:
            if line.startswith("Transcript: "):
                job["transcript"] = line[len("Transcript: "):].strip()
            job["lines"].append(line)
        job["rc"] = proc.wait()
        job["done"] = True

    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=pump, daemon=True).start()
    return job_id


def list_history():
    items = []
    if not os.path.isdir(OUT_DIR):
        return items
    for name in sorted(os.listdir(OUT_DIR), reverse=True):
        path = os.path.join(OUT_DIR, name, "transcript.md")
        if not os.path.exists(path):
            continue
        question = ""
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("**Question:**"):
                        question = line[len("**Question:**"):].strip()
                        break
        except OSError:
            pass
        items.append({"name": name, "path": path, "question": question, "mtime": os.path.getmtime(path)})
    return items


# ----------------------------------------------------------------------------
# page
# ----------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>frenenemy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f7f9;--fg:#1a1c20;--muted:#6b7280;--card:#fff;--line:#e3e6ea;--accent:#2563eb;--accent-fg:#fff;--pre:#f1f3f6;--warn:#b45309;--warnbg:#fef3c7;--ok:#15803d}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6e8eb;--muted:#9aa3ad;--card:#171a20;--line:#2a2f37;--accent:#4f8cff;--accent-fg:#fff;--pre:#0b0d11;--warn:#fbbf24;--warnbg:#3a2a05;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.app{display:grid;grid-template-columns:280px 1fr;height:100vh}
aside{border-right:1px solid var(--line);background:var(--card);overflow:auto;padding:12px}
main{display:grid;grid-template-rows:auto minmax(0,1fr);overflow:hidden;min-height:0}
.top{padding:12px 18px;border-bottom:1px solid var(--line);background:var(--card);max-height:62vh;overflow:auto}
h1{font-size:16px;margin:0}
.titlebar{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.titlebar .spacer{flex:1}
.chips{display:flex;gap:6px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.chip{border:1px solid var(--line);border-radius:99px;padding:2px 9px;white-space:nowrap}
.chip b{color:var(--fg);font-weight:600}
.collapsed .detail{display:none}
.side .detail{margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}
.side .detail .acct{margin-bottom:6px}
textarea{width:100%;min-height:76px;resize:vertical;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px}
label{color:var(--muted);font-size:12px;display:flex;gap:4px;align-items:center}
select,input[type=number]{padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
button{padding:7px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;cursor:pointer}
button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
button.small{padding:3px 8px;font-size:12px}
button:disabled{opacity:.5;cursor:default}
.out{overflow:auto;padding:18px;min-height:0}
.status{color:var(--muted);font-size:12px;margin-left:auto}
.md{max-width:900px}.md h2{margin-top:28px;padding-top:12px;border-top:1px solid var(--line);font-size:15px}
.md pre,pre.log{background:var(--pre);padding:10px;border-radius:8px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.md table{border-collapse:collapse}.md td,.md th{border:1px solid var(--line);padding:4px 8px}
.hist{display:block;width:100%;text-align:left;padding:8px;border:1px solid transparent;border-radius:8px;background:none;color:var(--fg);margin-bottom:4px;white-space:normal}
.hist:hover{border-color:var(--line)}.hist small{display:block;color:var(--muted)}
.hist.active{border-color:var(--accent)}
.tools{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.hint{color:var(--muted);font-size:12px}
.sides{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.side{border:1px solid var(--line);border-radius:10px;padding:10px;background:var(--bg)}
.side-title{font-weight:600;margin-bottom:2px;display:flex;justify-content:space-between;align-items:center}
.acct{font-weight:400;font-size:12px;color:var(--muted);margin-bottom:8px}
.acct b{color:var(--fg);font-weight:600}
.side label{display:grid;grid-template-columns:52px 1fr;align-items:center;margin-bottom:6px;font-size:12px}
.side select{width:100%}
.auth{margin:0 0 8px}
.auth .authbox{border:1px dashed var(--line);border-radius:8px;padding:8px;margin-top:6px;font-size:12px}
.auth .authbox a{color:var(--accent);word-break:break-all}
.auth input[type=text]{width:100%;padding:5px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit;margin:6px 0}
.auth .arow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.usage{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.ucard{border:1px solid var(--line);border-radius:10px;padding:10px;background:var(--bg)}
.uhead{display:flex;justify-content:space-between;align-items:center;font-weight:600;margin-bottom:6px}
.urow{display:grid;grid-template-columns:64px 1fr auto;gap:8px;align-items:center;font-size:12px;color:var(--muted);margin-bottom:4px}
.bar{height:8px;border-radius:99px;background:var(--line);overflow:hidden}
.bar i{display:block;height:100%;background:var(--ok);transition:width .3s}
.bar.warn i{background:var(--warn)}.bar.hot i{background:#dc2626}
.unum{font-variant-numeric:tabular-nums;color:var(--fg)}
.ufoot{font-size:12px;color:var(--muted);margin-top:6px;border-top:1px dashed var(--line);padding-top:6px}
@media(max-width:900px){.usage{grid-template-columns:1fr}}
.banner{margin-top:10px;padding:8px 12px;border-radius:8px;background:var(--warnbg);color:var(--warn);font-size:13px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.banner.ok{background:transparent;color:var(--muted);border:1px dashed var(--line)}
pre.detect{max-height:160px;margin:6px 0 0;font-size:12px;width:100%}
@media(max-width:900px){.sides{grid-template-columns:1fr}.app{grid-template-columns:1fr}aside{display:none}}
</style></head><body>
<div class="app">
<aside>
  <div style="font-weight:600;margin-bottom:8px">Past debates</div>
  <div id="history"></div>
</aside>
<main>
  <div class="top">
    <div class="titlebar">
      <h1>frenenemy · Claude vs ChatGPT</h1>
      <div class="chips" id="chips"></div>
      <span class="spacer"></span>
      <button class="small" id="toggle">Show usage &amp; limits</button>
    </div>
    <textarea id="q" placeholder="Type or paste your question here. Multi-line is fine."></textarea>
    <div class="sides">
      <div class="side">
        <div class="side-title"><span>Claude</span></div>
        <label>Model <select id="cm"></select></label>
        <label>Effort <select id="ce"></select></label>
        <div class="hint" id="cm-desc"></div>
        <div class="detail">
          <div class="acct" id="acct-claude">…</div>
          <div class="auth" id="auth-claude"></div>
          <div class="arow"><button class="small" data-detect="claude">Re-detect models</button></div>
        </div>
      </div>
      <div class="side">
        <div class="side-title"><span>ChatGPT</span></div>
        <label>Model <select id="gm"></select></label>
        <label>Effort <select id="ge"></select></label>
        <div class="hint" id="gm-desc"></div>
        <div class="detail">
          <div class="acct" id="acct-gpt">…</div>
          <div class="auth" id="auth-gpt"></div>
          <div class="arow"><button class="small" data-detect="gpt">Re-detect models</button></div>
        </div>
      </div>
    </div>
    <div class="usage detail" id="usage"></div>
    <div class="banner" id="banner" hidden></div>
    <div class="row">
      <label>Rounds <input id="rounds" type="number" min="0" max="6" value="2" style="width:56px"></label>
      <label>Judge <select id="judge"><option value="claude">Claude</option><option value="gpt">ChatGPT</option><option value="none">none</option></select></label>
      <label>Mode <select id="solo"><option value="">debate</option><option value="claude">ask Claude only</option><option value="gpt">ask ChatGPT only</option></select></label>
      <label><input id="web" type="checkbox"> web search</label>
      <label><input id="showall" type="checkbox"> show unavailable models</label>
    </div>
    <div class="row">
      <button class="primary" id="run">Run debate</button>
      <button id="stop" disabled>Stop</button>
      <button id="clear">Clear question</button>
      <span class="hint">⌘/Ctrl+Enter runs</span>
      <span class="status" id="status">idle</span>
    </div>
  </div>
  <div class="out">
    <div class="tools" id="tools" hidden>
      <button id="copyMd">Copy transcript (Markdown)</button>
      <button id="copyVerdict">Copy verdict only</button>
      <button id="reuse">Edit this question again</button>
      <span class="hint" id="path"></span>
    </div>
    <pre class="log" id="log" hidden></pre>
    <div class="md" id="md"></div>
  </div>
</main>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pref=(k,v)=>{ try{ if(v===undefined) return localStorage.getItem('frenenemy.'+k); localStorage.setItem('frenenemy.'+k,v);}catch(e){} };
let CAT=null, LOGINSTATE=null, QUOTA=null, job=null, poll=null, detectPoll=null, currentMd="", currentQ="";
try{ $('q').value = localStorage.getItem('frenenemy.q')||''; }catch(e){}
$('q').addEventListener('input',()=>{ try{localStorage.setItem('frenenemy.q',$('q').value)}catch(e){} });
$('showall').checked = pref('showall')==='1';
$('showall').onchange=()=>{ pref('showall',$('showall').checked?'1':'0'); renderPickers(); };

function acctLine(a){
  if(!a.logged_in) return '<span style="color:var(--warn)">'+esc(a.warning||'not logged in')+'</span>';
  let s='<b>'+esc(a.email||'?')+'</b>'+(a.plan?' · '+esc(a.plan):'');
  s+= a.detected_at ? ' · detected '+esc(a.detected_at.replace('T',' ')) : ' · <span style="color:var(--warn)">not detected yet</span>';
  if(a.warning) s+=' · <span style="color:var(--warn)">'+esc(a.warning)+'</span>';
  return s;
}
function fillModels(side, modelSel, effortSel, descEl, defModel, defEffort){
  const all=CAT[side].models, showAll=$('showall').checked;
  const list = all.filter(m=>m.available!==false || showAll);
  modelSel.innerHTML='';
  for(const m of list){
    const o=document.createElement('option'); o.value=m.slug;
    o.textContent=m.display_name + (m.available===false?'  (not available on this account)':'') + (m.available===null?'  (untested)':'');
    if(m.available===false) o.disabled=true;
    modelSel.appendChild(o);
  }
  const savedModel=pref(side+'.model')||defModel;
  const pick = list.find(m=>m.slug===savedModel && m.available!==false) || list.find(m=>m.available===true) || list[0];
  if(pick) modelSel.value=pick.slug;
  const fillEffort=(keep)=>{
    const m=all.find(x=>x.slug===modelSel.value)||list[0]; if(!m){effortSel.innerHTML='';return;}
    effortSel.innerHTML='';
    for(const e of m.efforts){ const o=document.createElement('option'); o.value=e.effort; o.textContent=e.effort+(e.description?' — '+e.description:''); effortSel.appendChild(o); }
    const want=keep||pref(side+'.effort')||defEffort||m.default_effort;
    effortSel.value = m.efforts.some(e=>e.effort===want)? want : (m.efforts.some(e=>e.effort==='medium')?'medium':(m.efforts[m.efforts.length-1]||{}).effort);
    let d=m.description||'';
    if(m.available===true && m.all_efforts && m.all_efforts.length!==m.efforts.length) d+=' · effort capped at '+m.efforts[m.efforts.length-1].effort+' for this account';
    if(m.available===false) d='Not available: '+(m.reason||'the call failed');
    descEl.textContent=d;
  };
  fillEffort();
  modelSel.onchange=()=>{ pref(side+'.model',modelSel.value); fillEffort(); pref(side+'.effort',effortSel.value); renderChips(); };
  effortSel.onchange=()=>{ pref(side+'.effort',effortSel.value); renderChips(); };
}
let loginPoll=null;
function renderAuth(side){
  const el=$('auth-'+side); if(!el) return;
  const a=CAT.accounts[side], L=(LOGINSTATE&&LOGINSTATE[side])||{};
  const name = side==='claude'?'Claude':'ChatGPT';
  let h='<div class="arow">';
  if(a.logged_in) h+='<button class="small" data-logout="'+side+'">Log out of '+name+'</button>';
  else h+='<button class="small primary" data-login="'+side+'">Log in to '+name+'</button>';
  if(L.state && !['idle','done'].includes(L.state)) h+='<button class="small" data-cancel="'+side+'">Cancel</button>';
  if(L.message) h+='<span class="hint">'+esc(L.message)+'</span>';
  h+='</div>';
  if(L.auth_url && !['idle','done'].includes(L.state)){
    h+='<div class="authbox">Browser tab not open? <a href="'+esc(L.auth_url)+'" target="_blank" rel="noopener">Open the sign-in page</a>';
    if(L.needs_code){
      h+='<input type="text" id="code-'+side+'" placeholder="Paste the code from the browser here" autocomplete="off">'+
         '<div class="arow"><button class="small primary" data-code="'+side+'">Submit code</button></div>';
    }
    h+='</div>';
  }
  el.innerHTML=h;
}
function wireAuth(){
  document.querySelectorAll('[data-login]').forEach(b=>b.onclick=async()=>{
    const side=b.dataset.login; b.disabled=true;
    const r=await (await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:side})})).json();
    LOGINSTATE=r; renderAuth(side);
    if(r[side] && r[side].auth_url) window.open(r[side].auth_url,'_blank','noopener');
    watchLogin();
  });
  document.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{
    LOGINSTATE=await (await fetch('/login/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:b.dataset.cancel})})).json();
    renderAuth(b.dataset.cancel); wireAuth();
  });
  document.querySelectorAll('[data-code]').forEach(b=>b.onclick=async()=>{
    const side=b.dataset.code, inp=$('code-'+side); if(!inp||!inp.value.trim()) return;
    b.disabled=true;
    const r=await (await fetch('/login/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:side,code:inp.value})})).json();
    LOGINSTATE=r.login; renderAuth(side); wireAuth(); watchLogin();
  });
  document.querySelectorAll('[data-logout]').forEach(b=>b.onclick=async()=>{
    const side=b.dataset.logout;
    if(!confirm('Log out of '+(side==='claude'?'Claude':'ChatGPT')+'? You will need to sign in again to run debates.')) return;
    b.disabled=true;
    const r=await (await fetch('/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:side})})).json();
    LOGINSTATE=r.login; await refreshAll();
  });
}
function watchLogin(){
  clearInterval(loginPoll);
  loginPoll=setInterval(async()=>{
    LOGINSTATE=await (await fetch('/login/status')).json();
    let busy=false;
    for(const side of ['claude','gpt']){
      const st=(LOGINSTATE[side]||{}).state;
      if(st && !['idle','done'].includes(st)) busy=true;
      renderAuth(side);
    }
    wireAuth();
    if(!busy){ clearInterval(loginPoll); await refreshAll(); }
  },1500);
}
async function refreshAll(){
  CAT=await (await fetch('/models')).json();
  LOGINSTATE=await (await fetch('/login/status')).json();
  renderPickers(); loadQuota();
  const need=['claude','gpt'].filter(p=>CAT.accounts[p].needs_detect);
  if(need.length){ banner('New account detected. Checking which models it can use…'); startDetect(need); }
}
function pct(side){
  const q=QUOTA&&QUOTA[side]; if(!q||!(q.windows||[]).length) return '';
  const w=q.windows[0]; return w.used_percent==null?'':' · '+w.used_percent+'% of 5h used';
}
function renderChips(){
  const el=$('chips'); if(!el) return;
  if(!QUOTA){ el.innerHTML=''; return; }
  let h='';
  for(const [side,label] of [['claude','Claude'],['gpt','ChatGPT']]){
    const q=QUOTA[side]; if(!q||!(q.windows||[]).length) continue;
    const w=q.windows[0];
    if(w.used_percent==null) continue;
    h+='<span class="chip">'+label+' <b>'+w.used_percent+'%</b> of 5h used</span>';
  }
  el.innerHTML=h;
}
function setCollapsed(v){
  document.body.classList.toggle('collapsed', !!v);
  $('toggle').textContent = v ? 'Show usage & limits' : 'Hide usage & limits';
  try{ localStorage.setItem('frenenemy.collapsed', v?'1':'0'); }catch(e){}
}
$('toggle').onclick=()=>setCollapsed(!document.body.classList.contains('collapsed'));
try{ setCollapsed(localStorage.getItem('frenenemy.collapsed')!=='0'); }catch(e){ setCollapsed(true); }

function renderPickers(){
  const d=CAT.defaults;
  fillModels('claude',$('cm'),$('ce'),$('cm-desc'),d.claude_model,d.claude_effort);
  fillModels('gpt',$('gm'),$('ge'),$('gm-desc'),d.gpt_model,d.gpt_effort);
  $('acct-claude').innerHTML=acctLine(CAT.accounts.claude);
  $('acct-gpt').innerHTML=acctLine(CAT.accounts.gpt);
  renderAuth('claude'); renderAuth('gpt'); wireAuth(); renderChips();
}
async function loadModels(){
  CAT=await (await fetch('/models')).json();
  LOGINSTATE=await (await fetch('/login/status')).json();
  renderPickers();
  for(const k of ['rounds','judge','solo']){ const v=pref(k); if(v!==null&&v!==undefined&&v!=='') $(k).value=v; $(k).onchange=()=>pref(k,$(k).value); }
  const need=['claude','gpt'].filter(p=>CAT.accounts[p].needs_detect);
  if(CAT.detect.running){ watchDetect(); }
  else if(need.length){ banner('New account detected for '+need.join(' and ')+'. Checking which models and effort levels this account can use (about 20 tiny calls)…'); startDetect(need); }
}
function banner(msg, ok){ const b=$('banner'); if(!msg){ b.hidden=true; b.innerHTML=''; return; } b.hidden=false; b.className='banner'+(ok?' ok':''); b.innerHTML='<span>'+esc(msg)+'</span><pre class="detect" id="dlog" hidden></pre>'; }
async function startDetect(providers){
  const r=await (await fetch('/detect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({providers})})).json();
  if(!r.started){ banner('Detection is already running…'); }
  watchDetect();
}
function watchDetect(){
  document.querySelectorAll('[data-detect]').forEach(b=>b.disabled=true);
  if(!$('dlog')) banner('Detecting available models for this account…');
  const dlog=$('dlog'); dlog.hidden=false; let off=0;
  clearInterval(detectPoll);
  detectPoll=setInterval(async()=>{
    const s=await (await fetch('/detect/status?offset='+off)).json();
    if(s.text){ dlog.textContent+=s.text; off=s.offset; dlog.scrollTop=dlog.scrollHeight; }
    if(s.done){
      clearInterval(detectPoll); document.querySelectorAll('[data-detect]').forEach(b=>b.disabled=false);
      CAT=await (await fetch('/models')).json(); renderPickers();
      banner(s.error?('Detection failed: '+s.error):'Model lists updated for your accounts.', !s.error);
      setTimeout(()=>{ if(!s.error) $('banner').hidden=true; }, 6000);
    }
  },1000);
}
document.querySelectorAll('[data-detect]').forEach(b=>b.onclick=()=>{ banner('Re-detecting '+b.dataset.detect+' models…'); startDetect([b.dataset.detect]); });

function fmtNum(n){ n=+n||0; return n>=1e6?(n/1e6).toFixed(1)+'M':(n>=1e3?(n/1e3).toFixed(1)+'k':String(n)); }
function fmtReset(ts){
  if(!ts) return '';
  const d=new Date(ts*1000), mins=Math.round((d-Date.now())/60000);
  if(mins<=0) return 'resets now';
  const h=Math.floor(mins/60), m=mins%60;
  const when = d.toLocaleString([], {weekday:'short', hour:'2-digit', minute:'2-digit'});
  return 'resets in '+(h?h+'h ':'')+m+'m ('+when+')';
}
function renderQuota(q){
  QUOTA=q;
  const el=$('usage'); el.innerHTML='';
  for(const [side,label] of [['claude','Claude'],['gpt','ChatGPT']]){
    const s=q[side], u=(q.usage&&q.usage[side])||{};
    let h='<div class="ucard"><div class="uhead"><span>'+label+' limits'+(s&&s.plan?' · '+esc(s.plan):'')+'</span>'+
          '<button class="small" data-quota="'+side+'">Refresh'+(side==='claude'?' (1 tiny call)':'')+'</button></div>';
    if(!s || !(s.windows||[]).length){
      h+='<div class="hint">'+(side==='claude'
        ? 'Claude only reports limits while answering. Run something, or press Refresh.'
        : 'No limit data. Is Codex logged in?')+'</div>';
    } else {
      for(const w of s.windows){
        const used=w.used_percent, cls = used==null?'':(used>=85?' hot':(used>=50?' warn':''));
        h+='<div class="urow"><span>'+esc(w.label)+'</span>'+
           '<div class="bar'+cls+'"><i style="width:'+Math.min(100,used||0)+'%"></i></div>'+
           '<span class="unum">'+(used==null?'?':used+'% used')+'</span></div>'+
           '<div class="urow"><span></span><span class="hint" style="grid-column:2/4">'+esc(fmtReset(w.resets_at))+'</span></div>';
      }
      if(s.reset_credits) h+='<div class="hint">'+s.reset_credits+' free limit reset(s) available in Codex</div>';
      if(s.using_overage) h+='<div class="hint" style="color:var(--warn)">Currently using extra usage beyond the plan</div>';
      if(s.limit_reached) h+='<div class="hint" style="color:var(--warn)">Limit reached: '+esc(s.limit_reached)+'</div>';
    }
    const t=u.today||{};
    if(t.calls){
      h+='<div class="ufoot">Today via frenenemy: '+t.calls+' calls · '+fmtNum(t.in)+' in · '+fmtNum(t.out)+' out'+
         (side==='claude'&&t.cost?' · $'+t.cost.toFixed(3)+' list-price equivalent':'')+'</div>';
    }
    if(s&&s.at) h+='<div class="hint">checked '+esc(String(s.at).replace('T',' '))+'</div>';
    el.insertAdjacentHTML('beforeend', h+'</div>');
  }
  renderChips();
  document.querySelectorAll('[data-quota]').forEach(b=>b.onclick=async()=>{
    b.disabled=true; b.textContent='checking…';
    const r=await (await fetch('/quota/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:b.dataset.quota})})).json();
    renderQuota(r.quota);
  });
}
async function loadQuota(){ try{ renderQuota(await (await fetch('/quota')).json()); }catch(e){} }

function render(md){ currentMd=md; $('md').innerHTML = window.marked ? marked.parse(md) : '<pre>'+esc(md)+'</pre>'; $('tools').hidden=false; }
async function loadHistory(){
  const r=await fetch('/history'); const items=await r.json();
  $('history').innerHTML = items.length? '' : '<div class="hint">Nothing yet.</div>';
  for(const it of items){
    const b=document.createElement('button'); b.className='hist'; b.dataset.path=it.path;
    b.innerHTML = esc((it.question||it.name).slice(0,120)) + '<small>'+new Date(it.mtime*1000).toLocaleString()+'</small>';
    b.onclick=()=>openTranscript(it.path, it.question, b);
    $('history').appendChild(b);
  }
}
async function openTranscript(path, question, btn){
  const r=await fetch('/transcript?path='+encodeURIComponent(path)); const md=await r.text();
  document.querySelectorAll('.hist').forEach(x=>x.classList.remove('active')); if(btn) btn.classList.add('active');
  $('log').hidden=true; $('path').textContent=path; currentQ=question||''; render(md);
}
async function run(){
  const question=$('q').value.trim(); if(!question) return;
  const params={question, rounds:+$('rounds').value, judge:$('judge').value, claude_model:$('cm').value, claude_effort:$('ce').value, gpt_model:$('gm').value, gpt_effort:$('ge').value, web:$('web').checked, solo:$('solo').value};
  const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)});
  job=(await r.json()).id; currentQ=question;
  $('run').disabled=true; $('stop').disabled=false;
  $('status').textContent='running… '+$('cm').selectedOptions[0].text+'/'+$('ce').value+' vs '+$('gm').selectedOptions[0].text+'/'+$('ge').value;
  $('md').innerHTML=''; $('tools').hidden=true; $('log').hidden=false; $('log').textContent='';
  setCollapsed(true);
  let offset=0;
  poll=setInterval(async()=>{
    const s=await (await fetch('/status/'+job+'?offset='+offset)).json();
    if(s.text){ $('log').textContent+=s.text; offset=s.offset; $('log').scrollTop=$('log').scrollHeight; }
    if(s.done){
      clearInterval(poll); $('run').disabled=false; $('stop').disabled=true;
      $('status').textContent = s.rc===0 ? 'done' : 'failed (exit '+s.rc+') – see log';
      if(s.transcript){ const md=await (await fetch('/transcript?path='+encodeURIComponent(s.transcript))).text(); $('path').textContent=s.transcript; render(md); if(s.rc===0) $('log').hidden=true; }
      loadHistory(); loadQuota();
    }
  },1000);
}
$('run').onclick=run;
$('q').addEventListener('keydown',e=>{ if((e.metaKey||e.ctrlKey)&&e.key==='Enter') run(); });
$('stop').onclick=async()=>{ if(job) await fetch('/stop/'+job,{method:'POST'}); };
$('clear').onclick=()=>{ $('q').value=''; try{localStorage.removeItem('frenenemy.q')}catch(e){} $('q').focus(); };
$('copyMd').onclick=()=>navigator.clipboard.writeText(currentMd).then(()=>$('status').textContent='copied transcript');
$('copyVerdict').onclick=()=>{ const i=currentMd.indexOf('## Verdict'); navigator.clipboard.writeText(i>=0?currentMd.slice(i):currentMd).then(()=>$('status').textContent='copied verdict'); };
$('reuse').onclick=()=>{ if(currentQ){ $('q').value=currentQ; $('q').focus(); $('q').dispatchEvent(new Event('input')); } };
loadModels(); loadHistory(); loadQuota();
</script>
</body></html>
"""


class QuietHTTPServer(ThreadingHTTPServer):
    """Threaded server that ignores the normal noise of a browser closing a poll early."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if sys.exc_info()[0] in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True   # the client went away mid-poll; nothing to do

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, HTML, "text/html")
        if u.path == "/history":
            return self._send(200, list_history())
        if u.path == "/models":
            return self._send(200, models_payload())
        if u.path == "/quota":
            return self._send(200, quota_payload())
        if u.path == "/login/status":
            return self._send(200, login_payload())
        if u.path == "/detect/status":
            offset = int(qs.get("offset", ["0"])[0])
            lines = DETECT["lines"]
            return self._send(200, {"text": "".join(l + "\n" for l in lines[offset:]), "offset": len(lines),
                                    "done": DETECT["done"], "running": DETECT["running"], "error": DETECT["error"]})
        if u.path == "/transcript":
            path = os.path.realpath(qs.get("path", [""])[0])
            if not path.startswith(os.path.realpath(OUT_DIR)) or not os.path.exists(path):
                return self._send(404, "not found", "text/plain")
            with open(path) as f:
                return self._send(200, f.read(), "text/markdown")
        if u.path.startswith("/status/"):
            job = JOBS.get(u.path.split("/")[2])
            if not job:
                return self._send(404, {"error": "no such job"})
            offset = int(qs.get("offset", ["0"])[0])
            lines = job["lines"]
            return self._send(200, {"text": "".join(lines[offset:]), "offset": len(lines), "done": job["done"],
                                    "rc": job["rc"], "transcript": job["transcript"]})
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        if u.path == "/run":
            params = json.loads(raw)
            if not params.get("question", "").strip():
                return self._send(400, {"error": "empty question"})
            d = load_defaults()
            for k in ("claude_model", "gpt_model", "max_words"):
                params.setdefault(k, d[k])
            return self._send(200, {"id": start_job(params)})
        if u.path == "/login":
            p = json.loads(raw or "{}").get("provider")
            if p == "claude":
                start_claude_login()
            elif p == "gpt":
                start_codex_login()
            else:
                return self._send(400, {"error": "unknown provider"})
            return self._send(200, login_payload())
        if u.path == "/login/code":
            body = json.loads(raw or "{}")
            ok = submit_claude_code(body.get("code", ""))
            return self._send(200, {"ok": ok, "login": login_payload()})
        if u.path == "/login/cancel":
            cancel_login(json.loads(raw or "{}").get("provider", "claude"))
            return self._send(200, login_payload())
        if u.path == "/logout":
            p = json.loads(raw or "{}").get("provider")
            ok = do_logout(p) if p in ("claude", "gpt") else False
            return self._send(200, {"ok": ok, "login": login_payload()})
        if u.path == "/quota/refresh":
            params = json.loads(raw or "{}")
            ok = True
            if params.get("provider") in (None, "claude"):
                ok = refresh_claude_quota()
            return self._send(200, {"ok": ok, "quota": quota_payload()})
        if u.path == "/detect":
            params = json.loads(raw or "{}")
            providers = [p for p in params.get("providers", ["claude", "gpt"]) if p in ("claude", "gpt")]
            return self._send(200, {"started": start_detect(providers or ["claude", "gpt"])})
        if u.path.startswith("/stop/"):
            job = JOBS.get(u.path.split("/")[2])
            if job and not job["done"]:
                job["proc"].terminate()
            return self._send(200, {"ok": True})
        return self._send(404, "not found", "text/plain")


def stop_port(port):
    """Terminate whatever is listening on the port. Returns the pids it stopped."""
    try:
        out = subprocess.run(["lsof", "-ti", "tcp:%d" % port], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = [p for p in out.split() if p.strip() and p.strip() != str(os.getpid())]
    for pid in pids:
        for sig in ("-TERM", "-KILL"):
            subprocess.run(["kill", sig, pid], capture_output=True)
            time.sleep(0.6)
            still = subprocess.run(["kill", "-0", pid], capture_output=True)
            if still.returncode != 0:
                break
    return pids


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--restart", action="store_true",
                   help="stop whatever is already serving this port, then take it over")
    a = p.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    url = "http://127.0.0.1:%d" % a.port
    if a.restart:
        killed = stop_port(a.port)
        if killed:
            print("Stopped the instance already on port %d (pid %s)." % (a.port, ", ".join(killed)))
    srv = None
    for attempt in range(12):
        try:
            srv = QuietHTTPServer(("127.0.0.1", a.port), Handler)
            break
        except OSError:
            if not a.restart:
                print("Port %d is busy. frenenemy is probably already running at %s" % (a.port, url))
                print("Run `python3 ui.py --restart` to replace that instance.")
                if not a.no_open:
                    webbrowser.open(url)
                return
            time.sleep(0.5)
    if srv is None:
        print("Could not take over port %d." % a.port)
        return
    print("frenenemy UI at %s  (Ctrl+C to stop)" % url)
    if not a.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
