#!/usr/bin/env python3
"""
frenenemy - make Claude and ChatGPT debate a question and converge on the best answer.

Uses only your existing subscriptions:
  * Claude  -> `claude -p`   (Claude Code CLI, your Claude subscription)
  * ChatGPT -> `codex exec`  (Codex CLI signed in with your ChatGPT Plus account)
No API keys, no extra credits.

Usage:
  python3 debate.py "Which is better for a small team: monorepo or polyrepo?"
  python3 debate.py                      # prompts you for the question
  python3 debate.py --rounds 3 --judge gpt "..."
  python3 debate.py --solo gpt "..."     # just ask ChatGPT once
  python3 debate.py --solo claude "..."  # just ask Claude once
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "debate.config.json")

DEFAULTS = {
    "claude_model": "claude-sonnet-5",
    "claude_effort": "medium",
    "gpt_model": "gpt-5.6-sol",
    "gpt_effort": "medium",
    "rounds": 2,
    "judge": "claude",        # claude | gpt | none
    "max_words": 400,
    "timeout": 900,           # seconds per model call
    "web": False,             # allow web search for both sides
    "out_dir": os.path.join(HERE, "debates"),
    "claude_bin": "",         # auto-detected if empty
    "codex_bin": "codex",
}

CLAUDE = "Claude"
GPT = "ChatGPT"

STATE_DIR = os.path.expanduser("~/.frenenemy")
USAGE_LOG = os.path.join(STATE_DIR, "usage.jsonl")
QUOTA_FILE = os.path.join(STATE_DIR, "quota.json")

CALLS = []          # per-process record of every model call made
CALLS_LOCK = threading.Lock()


def record_call(provider, model, effort, role, usage, cost_usd=None):
    """Append one model call to the usage log and keep it in memory."""
    rec = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "provider": provider, "model": model, "effort": effort, "role": role,
        "usage": usage, "cost_usd": cost_usd,
    }
    with CALLS_LOCK:
        CALLS.append(rec)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


def save_quota(provider, snapshot):
    """Store the newest quota snapshot a call reported, so the UI can show it."""
    if not snapshot:
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        data = {}
        if os.path.exists(QUOTA_FILE):
            with open(QUOTA_FILE) as f:
                data = json.load(f)
        data[provider] = dict(snapshot, at=datetime.datetime.now().isoformat(timespec="seconds"))
        tmp = QUOTA_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, QUOTA_FILE)
    except (OSError, ValueError):
        pass


def claude_quota_from_event(info):
    """Normalise Claude's rate_limit_event into the shape the UI renders."""
    if not info:
        return None
    windows = info.get("unifiedWindows") or {}
    out = {"provider": "claude", "windows": []}
    for key, label, mins in (("five_hour", "5 hours", 300), ("seven_day", "7 days", 10080)):
        w = windows.get(key)
        if not w:
            continue
        out["windows"].append({
            "key": key, "label": label,
            "used_percent": round(float(w.get("utilization", 0)) * 100, 1),
            "resets_at": w.get("resetsAt"), "window_mins": mins,
        })
    if not out["windows"] and info.get("rateLimitType"):
        out["windows"].append({"key": info["rateLimitType"], "label": info["rateLimitType"],
                               "used_percent": None, "resets_at": info.get("resetsAt"), "window_mins": None})
    out["status"] = info.get("status")
    out["using_overage"] = info.get("isUsingOverage")
    return out


class SideError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def load_config() -> Dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    return cfg


def find_claude(explicit: str) -> str:
    if explicit:
        return explicit
    env = os.environ.get("CLAUDE_BIN")
    if env:
        return env
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = os.path.expanduser("~")
    patterns = [
        home + "/.claude/local/claude",
        home + "/.antigravity-ide/extensions/anthropic.claude-code-*/resources/native-binary/claude",
        home + "/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
        home + "/.vscode-insiders/extensions/anthropic.claude-code-*/resources/native-binary/claude",
        home + "/.cursor/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    ]
    found = []
    for pat in patterns:
        found.extend(glob.glob(pat))
    found = [f for f in found if os.access(f, os.X_OK)]
    if not found:
        raise SideError(
            "Could not find the `claude` binary. Install Claude Code, or set CLAUDE_BIN=/path/to/claude, "
            "or put \"claude_bin\" in debate.config.json."
        )

    def version(p: str):
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    return sorted(found, key=version)[-1]


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:n].rstrip("-") or "debate"


def codex_app_server(requests, wait=8.0):
    """Talk JSON-RPC to `codex app-server` over stdio. Free, no model tokens."""
    try:
        proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError:
        return {}
    replies = {}
    done = threading.Event()

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and "result" in msg:
                replies[msg["id"]] = msg["result"]
                if len(replies) >= len(requests) + 1:
                    done.set()

    threading.Thread(target=reader, daemon=True).start()
    try:
        proc.stdin.write(json.dumps({"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "frenenemy", "version": "1.0"}}}) + "\n")
        proc.stdin.flush()
        time.sleep(0.8)
        for i, (method, params) in enumerate(requests, start=1):
            proc.stdin.write(json.dumps({"id": i, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
        done.wait(wait)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
    return replies


def codex_quota():
    replies = codex_app_server([("account/rateLimits/read", {}), ("account/read", {})])
    rl = (replies.get(1) or {}).get("rateLimits") or {}
    acct = (replies.get(2) or {}).get("account") or {}
    if not rl and not acct:
        return None
    windows = []
    for key, w in (("primary", rl.get("primary")), ("secondary", rl.get("secondary"))):
        if not w:
            continue
        mins = w.get("windowDurationMins")
        label = "5 hours" if mins == 300 else ("7 days" if mins == 10080 else ("%s min" % mins if mins else key))
        windows.append({"key": key, "label": label, "used_percent": w.get("usedPercent"),
                        "resets_at": w.get("resetsAt"), "window_mins": mins})
    credits = rl.get("credits") or {}
    resets = rl.get("rateLimitResetCredits") or (replies.get(1) or {}).get("rateLimitResetCredits") or {}
    return {
        "provider": "gpt", "windows": windows,
        "plan": (rl.get("planType") or acct.get("planType") or "").capitalize() or None,
        "email": acct.get("email"),
        "credits_balance": credits.get("balance"),
        "credits_unlimited": credits.get("unlimited"),
        "reset_credits": (resets or {}).get("availableCount", 0),
        "limit_reached": rl.get("rateLimitReachedType"),
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def refresh_gpt_quota():
    """Ask Codex for the account's current limits and store them. Costs no tokens."""
    q = codex_quota()
    if q:
        save_quota("gpt", q)
    return q


def fmt_int(n):
    return "{:,}".format(int(n or 0))


def usage_summary(cfg=None) -> str:
    """Markdown table of every model call this run made."""
    with CALLS_LOCK:
        calls = list(CALLS)
    if not calls:
        return ""
    rows, totals = [], {}
    for c in calls:
        u = c["usage"]
        t = totals.setdefault(c["provider"], {"calls": 0, "in": 0, "out": 0, "think": 0, "cost": 0.0, "model": c["model"], "effort": c["effort"]})
        t["calls"] += 1
        t["in"] += (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))
        t["out"] += u.get("output_tokens", 0)
        t["think"] += u.get("thinking_tokens", 0)
        if c.get("cost_usd"):
            t["cost"] += c["cost_usd"]
    rows.append("| Side | Model / effort | Calls | Input tokens | Output tokens | Thinking |")
    rows.append("|---|---|---:|---:|---:|---:|")
    for prov, name in (("claude", CLAUDE), ("gpt", GPT)):
        t = totals.get(prov)
        if not t:
            continue
        rows.append("| %s | %s / %s | %d | %s | %s | %s |" % (
            name, t["model"], t["effort"], t["calls"], fmt_int(t["in"]), fmt_int(t["out"]), fmt_int(t["think"])))
    note = ""
    ct = totals.get("claude")
    if ct and ct["cost"]:
        note = ("\n\nClaude list-price equivalent for this run: **$%.4f**. This is what the same tokens would "
                "cost on the pay-as-you-go API. On a subscription you are not charged it; it only indicates size.\n"
                % ct["cost"])
    q = quota_lines()
    return "\n".join(rows) + note + (("\n" + q) if q else "")


def quota_lines() -> str:
    """Human-readable line per side from the newest stored quota snapshot."""
    try:
        with open(QUOTA_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    out = []
    for prov, name in (("claude", CLAUDE), ("gpt", GPT)):
        snap = data.get(prov)
        if not snap:
            continue
        parts = []
        for w in snap.get("windows", []):
            used = w.get("used_percent")
            left = "?" if used is None else "%.0f%% left" % max(0.0, 100.0 - used)
            when = ""
            if w.get("resets_at"):
                when = ", resets %s" % datetime.datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M")
            parts.append("%s: %s%s" % (w.get("label", w.get("key")), left, when))
        if parts:
            out.append("- **%s** subscription limits — %s" % (name, "; ".join(parts)))
    return ("\n".join(out) + "\n") if out else ""


def hr(title: str) -> str:
    return "\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78 + "\n"


# ----------------------------------------------------------------------------
# model calls
# ----------------------------------------------------------------------------

def ask_claude(cfg: Dict, system: str, prompt: str, role: str = "debate") -> str:
    cmd = [
        cfg["claude_bin"], "-p",
        "--model", cfg["claude_model"],
        "--effort", cfg["claude_effort"],
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--system-prompt", system,
        "--tools", "WebSearch,WebFetch" if cfg["web"] else "",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           cwd=cfg["workdir"], timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        raise SideError("Claude call timed out after %ss" % cfg["timeout"])

    result, rate_info = None, None
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "rate_limit_event":
            rate_info = ev.get("rate_limit_info")

    if rate_info:
        save_quota("claude", claude_quota_from_event(rate_info))

    if r.returncode != 0 or not result or result.get("is_error"):
        msg = str((result or {}).get("result") or r.stderr or r.stdout).strip()
        hint = ""
        if "not logged in" in msg.lower() or "please run /login" in msg.lower():
            hint = "\nHint: open Claude Code and run /login (or `%s /login` in a terminal)." % cfg["claude_bin"]
        raise SideError("claude failed (exit %d): %s%s" % (r.returncode, msg[:2000], hint))

    u = result.get("usage") or {}
    record_call("claude", cfg["claude_model"], cfg["claude_effort"], role, {
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
        "thinking_tokens": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
    }, result.get("total_cost_usd"))
    return (result.get("result") or "").strip()


def check_codex_login(cfg: Dict) -> None:
    """Fail fast with a clear message instead of letting codex retry 5 times against a 401."""
    try:
        r = subprocess.run([cfg["codex_bin"], "login", "status"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise SideError("`codex` not found. Install it with: npm install -g @openai/codex")
    except subprocess.TimeoutExpired:
        return
    out = (r.stdout + r.stderr).lower()
    if r.returncode != 0 or "not logged in" in out:
        raise SideError("Codex is not logged in. Run `codex login` and sign in with your ChatGPT account "
                        "(choose the ChatGPT sign-in, not an API key).")


def ask_gpt(cfg: Dict, system: str, prompt: str, role: str = "debate") -> str:
    fd, out_file = tempfile.mkstemp(prefix="frenenemy-", suffix=".md")
    os.close(fd)
    cmd = [
        cfg["codex_bin"], "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "-s", "read-only",
        "-C", cfg["workdir"],
        "-m", cfg["gpt_model"],
        "-c", "model_reasoning_effort=%s" % cfg["gpt_effort"],
        "-c", 'approval_policy="never"',
        "-c", "features.shell_tool=false",
        "-c", 'web_search="%s"' % ("cached" if cfg["web"] else "disabled"),
        "--json",
        "-o", out_file,
        "-",  # prompt from stdin
    ]
    full_prompt = "<role>\n%s\n</role>\n\n%s" % (system, prompt)
    try:
        r = subprocess.run(cmd, input=full_prompt, capture_output=True, text=True,
                           cwd=cfg["workdir"], timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        raise SideError("Codex call timed out after %ss" % cfg["timeout"])
    except FileNotFoundError:
        raise SideError("`codex` not found. Install it with: npm install -g @openai/codex")
    try:
        with open(out_file) as f:
            answer = f.read().strip()
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass
    usage = {}
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "turn.completed" and ev.get("usage"):
            usage = ev["usage"]

    if r.returncode != 0 or not answer:
        err = (r.stderr or "") + "\n" + (r.stdout or "")
        hint = ""
        if "login" in err.lower() or "auth" in err.lower() or "unauthorized" in err.lower():
            hint = "\nHint: run `codex login` and sign in with your ChatGPT account (not an API key)."
        elif "model" in err.lower() and ("not" in err.lower() or "unknown" in err.lower()):
            hint = ("\nHint: model %r may not be available on your ChatGPT plan yet. "
                    "Try --gpt-model gpt-5.5 (or whatever `codex` shows under /model)." % cfg["gpt_model"])
        raise SideError("codex exited %d:\n%s%s" % (r.returncode, err.strip()[-3000:], hint))

    record_call("gpt", cfg["gpt_model"], cfg["gpt_effort"], role, {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_write_input_tokens", 0),
        "thinking_tokens": usage.get("reasoning_output_tokens", 0),
    })
    return answer


ASK = {CLAUDE: ask_claude, GPT: ask_gpt}


# ----------------------------------------------------------------------------
# prompts
# ----------------------------------------------------------------------------

def system_prompt(cfg: Dict, name: str, other: str) -> str:
    return (
        "You are %s, one of two AI participants in a structured debate. The other participant is %s. "
        "The shared goal is NOT to win but to arrive at the best, most correct and most useful answer to "
        "the user's question.\n"
        "Rules:\n"
        "- Be rigorous, concrete and specific. Give reasons, examples, numbers, or references when they matter.\n"
        "- Be honest: concede points that are right, and push back firmly (with reasoning) on points that are wrong "
        "or weakly supported. Do not be sycophantic and do not agree just to be polite.\n"
        "- Flag uncertainty explicitly instead of bluffing.\n"
        "- Answer in the same language the user wrote the question in.\n"
        "- Write in Markdown. No preamble, no sign-off. Stay under about %d words unless the question truly "
        "needs more.\n"
        "- You are speaking to %s and to the human who asked the question."
    ) % (name, other, cfg["max_words"], other)


def opening_prompt(question: str) -> str:
    return (
        "## Question\n%s\n\n"
        "## Task\n"
        "Give your best answer. State your key claims, the reasoning behind each, and any assumptions you make. "
        "If the question is ambiguous, say how you are interpreting it."
    ) % question


def round_prompt(question: str, transcript: str, name: str, other: str, rnd: int, total: int) -> str:
    final = rnd == total
    task = (
        "This is round %d of %d. Respond to %s's latest position.\n"
        "1. **Agree**: which of their points are correct?\n"
        "2. **Disagree**: which points are wrong, overstated, or missing, and why?\n"
        "3. **Update**: what do you change in your own answer after reading them?\n"
        "4. **%s**: %s"
    ) % (
        rnd, total, other,
        "Final answer" if final else "Current answer",
        "this is the last round, so give your complete FINAL ANSWER to the question, then list any "
        "remaining disagreements with %s in one short bullet list." % other
        if final else
        "give your revised answer to the question so far.",
    )
    return "## Question\n%s\n\n## Debate so far\n%s\n\n## Task for %s\n%s" % (question, transcript, name, task)


def judge_prompt(question: str, transcript: str) -> str:
    return (
        "## Question\n%s\n\n## Full debate transcript\n%s\n\n## Task\n"
        "You are the neutral chair of this debate. Do not favour either participant. Produce, in the language of "
        "the question:\n"
        "1. **Best answer** - the single best answer to the question, synthesised from the strongest parts of both "
        "sides. This should be directly usable by the human.\n"
        "2. **Agreed** - points both participants ended up agreeing on.\n"
        "3. **Unresolved** - remaining disagreements. For each, give your own ruling and the reason.\n"
        "4. **Confidence** - how confident you are in the best answer and what would change it.\n"
        "Write in Markdown, no preamble."
    ) % (question, transcript)


def judge_system() -> str:
    return (
        "You are a careful, impartial expert chairing a debate between two AI models. You judge arguments on "
        "their merits only. Be concise, concrete, and decisive. Flag uncertainty honestly."
    )


# ----------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------

class Transcript:
    def __init__(self, path: str, question: str, cfg: Dict):
        self.path = path
        self.entries: List[str] = []
        header = (
            "# Debate\n\n**Question:** %s\n\n"
            "| Side | Model | Effort |\n|---|---|---|\n| %s | %s | %s |\n| %s | %s | %s |\n\n"
            "Rounds: %d | Judge: %s | %s\n"
        ) % (question, CLAUDE, cfg["claude_model"], cfg["claude_effort"],
             GPT, cfg["gpt_model"], cfg["gpt_effort"],
             cfg["rounds"], cfg["judge"], datetime.datetime.now().isoformat(timespec="seconds"))
        with open(self.path, "w") as f:
            f.write(header)

    def add(self, title: str, body: str, echo: bool = True) -> None:
        block = "\n## %s\n\n%s\n" % (title, body)
        self.entries.append(block)
        with open(self.path, "a") as f:
            f.write(block)
        if echo:
            print(hr(title))
            print(body)
            sys.stdout.flush()

    def text(self) -> str:
        return "".join(self.entries)


def run_parallel(jobs: Dict[str, "callable"]) -> Dict[str, str]:
    """jobs: name -> zero-arg function returning str. Runs all, raises first error."""
    results: Dict[str, str] = {}
    errors: Dict[str, BaseException] = {}

    def worker(name, fn):
        try:
            results[name] = fn()
        except BaseException as e:  # noqa
            errors[name] = e

    threads = [threading.Thread(target=worker, args=(n, f), daemon=True) for n, f in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        name, e = next(iter(errors.items()))
        raise SideError("[%s] %s" % (name, e))
    return results


def debate(cfg: Dict, question: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(cfg["out_dir"], "%s-%s" % (stamp, slugify(question)))
    os.makedirs(run_dir, exist_ok=True)
    cfg["workdir"] = run_dir
    tpath = os.path.join(run_dir, "transcript.md")
    t = Transcript(tpath, question, cfg)
    sides = {CLAUDE: GPT, GPT: CLAUDE}  # name -> opponent

    print("Transcript: %s" % tpath)
    print("Claude=%s/%s  ChatGPT=%s/%s  rounds=%d  judge=%s" % (
        cfg["claude_model"], cfg["claude_effort"], cfg["gpt_model"], cfg["gpt_effort"],
        cfg["rounds"], cfg["judge"]))

    # Opening statements (parallel, independent)
    print("\n[opening] asking both models...", flush=True)
    openings = run_parallel({
        name: (lambda n=name, o=other: ASK[n](cfg, system_prompt(cfg, n, o), opening_prompt(question)))
        for name, other in sides.items()
    })
    for name in (CLAUDE, GPT):
        t.add("Opening - %s" % name, openings[name])

    # Rebuttal rounds (parallel per round; both see everything from previous rounds)
    for rnd in range(1, cfg["rounds"] + 1):
        print("\n[round %d/%d] asking both models..." % (rnd, cfg["rounds"]), flush=True)
        snapshot = t.text()
        replies = run_parallel({
            name: (lambda n=name, o=other: ASK[n](
                cfg, system_prompt(cfg, n, o), round_prompt(question, snapshot, n, o, rnd, cfg["rounds"])))
            for name, other in sides.items()
        })
        for name in (CLAUDE, GPT):
            t.add("Round %d - %s" % (rnd, name), replies[name])

    # Verdict
    if cfg["judge"] != "none":
        judge_name = CLAUDE if cfg["judge"] == "claude" else GPT
        print("\n[verdict] asking %s to chair..." % judge_name, flush=True)
        verdict = ASK[judge_name](cfg, judge_system(), judge_prompt(question, t.text()))
        t.add("Verdict (chair: %s)" % judge_name, verdict)

    refresh_gpt_quota()
    summary = usage_summary(cfg)
    if summary:
        t.add("Usage for this run", summary)
    print("\nSaved: %s" % tpath)
    return tpath


def solo(cfg: Dict, question: str, who: str) -> str:
    cfg["workdir"] = HERE
    os.makedirs(cfg["out_dir"], exist_ok=True)
    name = CLAUDE if who == "claude" else GPT
    system = (
        "You are %s. Answer the user's question directly and rigorously, in the language of the question, "
        "in Markdown, with no preamble. Flag uncertainty honestly." % name
    )
    print("[%s] asking..." % name, flush=True)
    answer = ASK[name](cfg, system, question)
    print(hr(name))
    print(answer)
    # save like a debate so the web UI / history can show it
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(cfg["out_dir"], "%s-%s-%s" % (stamp, who, slugify(question)))
    os.makedirs(run_dir, exist_ok=True)
    tpath = os.path.join(run_dir, "transcript.md")
    with open(tpath, "w") as f:
        f.write("# %s only\n\n**Question:** %s\n\nModel: %s\n\n## %s\n\n%s\n" % (
            name, question, cfg["claude_model"] if who == "claude" else cfg["gpt_model"], name, answer))
    if who == "gpt":
        refresh_gpt_quota()
    summary = usage_summary(cfg)
    if summary:
        with open(tpath, "a") as f:
            f.write("\n## Usage for this run\n\n" + summary + "\n")
        print(hr("Usage for this run"))
        print(summary)
    print("Transcript: %s" % tpath)
    return answer


# ----------------------------------------------------------------------------
# cli
# ----------------------------------------------------------------------------

def read_question(arg: Optional[str]) -> str:
    if arg and arg != "-":
        return arg.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print("Type your question (finish with an empty line):")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip() and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main(argv: List[str]) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description="Make Claude and ChatGPT debate a question (subscriptions only).")
    p.add_argument("question", nargs="?", help="The question. Omit to type it in, or pass '-' to read stdin.")
    p.add_argument("--rounds", type=int, default=cfg["rounds"], help="rebuttal rounds after openings (default %d)" % cfg["rounds"])
    p.add_argument("--judge", choices=["claude", "gpt", "none"], default=cfg["judge"])
    p.add_argument("--solo", choices=["claude", "gpt"], help="skip the debate; just ask one model once")
    p.add_argument("--claude-model", default=cfg["claude_model"])
    p.add_argument("--claude-effort", default=cfg["claude_effort"], choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--gpt-model", default=cfg["gpt_model"])
    p.add_argument("--gpt-effort", default=cfg["gpt_effort"], choices=["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
    p.add_argument("--max-words", type=int, default=cfg["max_words"])
    p.add_argument("--web", action="store_true", default=cfg["web"], help="allow web search for both sides")
    p.add_argument("--timeout", type=int, default=cfg["timeout"], help="seconds per model call")
    p.add_argument("--out-dir", default=cfg["out_dir"])
    p.add_argument("--claude-bin", default=cfg["claude_bin"])
    p.add_argument("--codex-bin", default=cfg["codex_bin"])
    a = p.parse_args(argv)

    cfg.update({
        "rounds": max(0, a.rounds), "judge": a.judge,
        "claude_model": a.claude_model, "claude_effort": a.claude_effort,
        "gpt_model": a.gpt_model, "gpt_effort": a.gpt_effort,
        "max_words": a.max_words, "web": a.web, "timeout": a.timeout,
        "out_dir": a.out_dir, "codex_bin": a.codex_bin,
    })

    try:
        if a.solo != "gpt":
            cfg["claude_bin"] = find_claude(a.claude_bin)
        if a.solo != "claude":
            check_codex_login(cfg)
        question = read_question(a.question)
        if not question:
            print("No question given.", file=sys.stderr)
            return 2
        if a.solo:
            solo(cfg, question, a.solo)
        else:
            debate(cfg, question)
        return 0
    except SideError as e:
        print("\nERROR: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
