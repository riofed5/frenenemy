# frenenemy

Make **Claude** and **ChatGPT** debate a question and converge on the best answer,
using only the subscriptions you already pay for. No API keys, no extra credits.

| Side | How it's called | Default model | Effort |
|---|---|---|---|
| Claude | `claude -p` (Claude Code) | `claude-sonnet-5` | medium |
| ChatGPT | `codex exec` (Codex CLI) | `gpt-5.6-sol` | medium |

Both calls bill against your subscriptions' usage limits, exactly like typing in Claude Code or
Codex yourself.

---

## One-time setup

1. **Codex CLI.** Install it and sign in with your ChatGPT account:
   ```
   npm install -g @openai/codex
   codex login
   ```
   Pick the **ChatGPT** sign-in in the browser, not "API key". Verify with `codex login status`.
   You can also do this from the web page, see [Signing in](#signing-in-from-the-page).
2. **Claude Code** must be logged in. If you use it in VS Code you already are. The script finds the
   `claude` binary bundled with the editor extension automatically; override with
   `CLAUDE_BIN=/path/to/claude` or `"claude_bin"` in `debate.config.json`.
3. **Python 3.9+.** The macOS system one is fine. No packages to install.

---

## Use it: the local web page (easiest)

```
python3 ui.py
```

Opens http://127.0.0.1:8765. Type or paste your question, press **Run debate** or Cmd/Ctrl+Enter.
Output streams live, the finished transcript renders as formatted text, and every past debate is
listed on the left with **Copy transcript**, **Copy verdict only**, and **Edit this question again**.
Your draft question is remembered between visits.

### Layout

Only the **model** and **effort** pickers are shown by default, one pair per side. The button
**Show usage & limits** expands everything else: account email and plan, sign-in controls,
**Re-detect models**, and the limit bars. Two chips beside the title keep the 5-hour usage visible
while it stays collapsed. The panel collapses itself when a debate starts so results get the whole
window, and your preference is remembered.

### Signing in from the page

You never need the terminal to authenticate. Each side card has a **Log in** button.

* **Claude** uses a paste-code flow. The page opens the Anthropic sign-in tab, you approve, the
  browser shows a code, and you paste that code into the box that appears on the card.
* **ChatGPT** uses a browser callback. The page opens the OpenAI sign-in tab and finishes on its own,
  with nothing to paste.
* **Cancel** aborts a sign-in in progress and leaves your existing login untouched.
* **Log out** sits on the same card and asks for confirmation first.

After a successful sign-in the page re-reads the account and, if it has not seen that account before,
runs model detection automatically.

### Account-aware model lists

The page shows which Claude account and which ChatGPT account are logged in, with their plans. The
first time it sees an account it runs a detection pass: every model, and every effort level from the
top down, gets one tiny real call. Anything that fails is hidden from the pickers (tick **show
unavailable models** to see them greyed out with the reason), and effort lists are capped at the
highest level that worked. Results are cached per account in `~/.frenenemy/availability.json`, so
logging in with a different account re-detects automatically. **Re-detect models** refreshes it.

Why probe at all: the Codex model cache is a global catalogue that is not filtered by plan, and
neither login exposes model gating. Asking the server is the only reliable answer.

Caveat: a server either accepts a call or rejects it. It never says "this effort was silently
downgraded". Detection proves a model and effort are *usable* by your account, not that the reasoning
depth matches what a website's picker would show.

### Cost and remaining quota

Both sides report what a run consumed and how much of your subscription is left.

* **In the page**, a limits card per side shows the rolling **5-hour** and **7-day** windows as bars
  with percent used and reset times, the plan name, any free limit resets Codex has granted, and what
  frenenemy itself has spent today.
* **In every transcript**, a *Usage for this run* section lists calls and input / output / thinking
  tokens per side, plus the limits remaining at the end.
* **Where the numbers come from.** Claude reports its limits only while answering, inside the streamed
  response, so its card refreshes whenever a call runs; its **Refresh** button makes one tiny Haiku
  call just to get fresh numbers. ChatGPT's come from asking the Codex app-server, which costs no
  tokens, so its **Refresh** is free.
* **About the dollar figure.** Claude reports a list-price equivalent for the tokens used. On a
  subscription you are **not** charged it; it only indicates how large a run was. Codex reports no
  price, so ChatGPT shows tokens only.

Every call is logged to `~/.frenenemy/usage.jsonl`, and the newest limit snapshot to
`~/.frenenemy/quota.json`.

### A note on Fable and Claude Pro

Fable 5.1 is **not included in the Pro plan**. Pro serves it from prepaid **usage credits**, so every
Fable call spends real money you bought in advance, while Sonnet, Opus and Haiku come out of the
subscription. You can confirm this yourself: a Fable call reports its limit type as `overage` with no
subscription windows, whereas Sonnet and Opus report the normal 5-hour and 7-day windows.

On **Max**, Fable is included at no extra cost up to 50% of the weekly limit. That 50% is a ceiling
inside the same weekly allowance, not extra capacity, and Fable drains it faster than other models.

---

## Stopping the UI server

The server holds port 8765 until it is stopped. Pick whichever applies.

**Running in a terminal you can see.** Press `Ctrl+C` in that window.

**Running in the background, or you closed the terminal.** Kill whatever holds the port:

```
lsof -ti tcp:8765 | xargs kill
```

Or kill it by name:

```
pkill -f ui.py
```

If it ignores both, force it:

```
lsof -ti tcp:8765 | xargs kill -9
```

**You just want to restart it with new code.** Do not kill it by hand, use the flag. It stops whatever
owns the port and takes over:

```
python3 ui.py --restart
```

**Check what is running:**

```
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

An empty result means the port is free.

Two things worth knowing. `pkill -f "ui.py --port 8765"` only matches instances started with an
explicit port flag, so it misses a plain `python3 ui.py`; match on `ui.py` alone or go by port. And
starting a second copy never silently fails: it reports the port is busy and points you at `--restart`.

---

## Use it: terminal

```
python3 debate.py "Should a 3-person startup use Postgres or MongoDB?"
python3 debate.py                     # type the question interactively
python3 debate.py --rounds 3 "..."    # more back-and-forth
python3 debate.py --judge gpt "..."   # let ChatGPT write the verdict
python3 debate.py --web "..."         # allow both models to search the web
python3 debate.py --solo gpt "..."    # just ask ChatGPT once, no debate
```

## Use it: inside Claude Code

In this folder, type:

```
/debate Should a 3-person startup use Postgres or MongoDB?
```

Claude runs the script and summarises the outcome for you.

---

## What happens in a debate

1. **Opening** — both models answer independently, in parallel.
2. **Rounds** (default 2) — each model reads the full transcript and replies with
   agree / disagree / update / current answer. The last round asks for a final answer.
3. **Verdict** — a neutral chair (Claude by default) writes the best answer, the agreed points,
   rulings on unresolved disagreements, and a confidence note.
4. **Usage** — a table of calls and tokens per side, plus remaining limits.

Everything streams to the terminal and is saved to `debates/<timestamp>-<slug>/transcript.md`.

## Configuration

Defaults live in `debate.config.json`; every key can be overridden by a CLI flag
(`--claude-model`, `--claude-effort`, `--gpt-model`, `--gpt-effort`, `--rounds`, `--judge`,
`--max-words`, `--timeout`, `--out-dir`, `--claude-bin`, `--codex-bin`).

Effort values: Claude `low|medium|high|xhigh|max`; Codex `minimal|low|medium|high|xhigh|max|ultra`.
Which ones actually work depends on the model, and the web page only offers what your account passed
detection.

`ui.py` takes `--port`, `--no-open`, and `--restart`.

## Troubleshooting

- `Codex is not logged in` -> `codex login`, or use the page's **Log in to ChatGPT** button.
- `Not logged in · Please run /login` from Claude -> open Claude Code and run `/login`, or use the
  page's **Log in to Claude** button.
- Model unavailable on your plan -> the page hides it after detection. From the terminal, pass
  `--gpt-model gpt-5.5` or `--claude-model sonnet`.
- `Port 8765 is busy` -> another copy is already running. Open http://127.0.0.1:8765, or replace it
  with `python3 ui.py --restart`. See [Stopping the UI server](#stopping-the-ui-server).
- The page stops responding -> the server is not running. Check with
  `lsof -nP -iTCP:8765 -sTCP:LISTEN` and start it again with `python3 ui.py --restart`.
- Slow or hanging -> raise `--timeout` (seconds per call, default 900), or use
  `--claude-effort low --gpt-effort low` for quick runs.

## For future development

`CLAUDE.md` in this folder records the architecture, the exact CLI invocations, the Codex app-server
protocol, and the findings that were expensive to discover. Read it before extending the project.
