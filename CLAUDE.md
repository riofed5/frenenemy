# frenenemy — context for Claude

Read this before changing anything. It records the decisions and the hard-won findings
that are expensive to rediscover.

## What this is

A local tool that makes **Claude** and **ChatGPT** debate a question over several rounds and
produce a judged verdict. Two front ends over one engine: a terminal script and a local web page.

## Hard constraints (do not violate)

1. **Subscriptions only. No API keys, ever.** Claude is driven through the `claude` CLI
   (Claude Code, logged in with the user's Claude subscription). ChatGPT is driven through the
   `codex` CLI signed in with a **ChatGPT account**, never `OPENAI_API_KEY`. If you find yourself
   reaching for the Anthropic or OpenAI SDK, stop; that would bill the user separately.
2. **Standard library only.** `ui.py` deliberately uses `http.server`, no Flask, no npm. The only
   external asset the page loads is `marked` from a CDN, and the page degrades to `<pre>` without it.
3. **The user is on Claude Pro and ChatGPT Plus.** Assume limited quota, and prefer cheap models
   (`claude-haiku-4-5-20251001`, `gpt-5.4-mini`) at `low` effort when testing.

## Files

| File | Role |
|---|---|
| `debate.py` | The engine. Model calls, debate loop, prompts, usage/quota accounting, terminal CLI. |
| `ui.py` | Local web server + the whole single-page UI (HTML/CSS/JS live in the `HTML` string). Imports `debate` as `D`. |
| `debate.config.json` | Defaults for models, efforts, rounds, judge, max words. |
| `.claude/skills/debate/SKILL.md` | Lets the user type `/debate <question>` inside Claude Code. |
| `debates/<ts>-<slug>/transcript.md` | One transcript per run, including a usage section. |
| `~/.frenenemy/availability.json` | Which models/efforts each **account** can actually use (probe results). |
| `~/.frenenemy/usage.jsonl` | Append-only record of every model call this tool made. |
| `~/.frenenemy/quota.json` | Newest subscription-limit snapshot per provider. |

## How each side is called

**Claude** (`debate.ask_claude`):
```
claude -p --model <m> --effort <e> --output-format stream-json --verbose
       --no-session-persistence --system-prompt <sys> --tools ""
```
- **Must be `stream-json --verbose`.** The plain `json` format omits the rate-limit data. The stream
  emits a `rate_limit_event` whose `rate_limit_info.unifiedWindows` carries `five_hour` and
  `seven_day` utilization plus `resetsAt`. That is the only source of Claude quota numbers.
- **Never pass `--bare`.** It breaks auth in print mode.
- The `result` event carries `total_cost_usd` (list price, *not* a subscription charge) and `usage`.
- The `claude` binary is usually **not on PATH**. `find_claude()` searches
  `~/.antigravity-ide/extensions/anthropic.claude-code-*/resources/native-binary/claude`,
  the VS Code / Cursor equivalents, and `~/.claude/local/claude`. Override with `CLAUDE_BIN`.

**ChatGPT** (`debate.ask_gpt`):
```
codex exec --skip-git-repo-check --ephemeral --color never -s read-only
           -m <m> -c model_reasoning_effort=<e> -c approval_policy="never"
           -c features.shell_tool=false --json -o <file> -
```
- Prompt on stdin, final answer written to `-o <file>`, JSONL events on stdout.
- `turn.completed` carries token usage. **Codex exec does not report rate limits.**

## The Codex app-server (JSON-RPC over stdio)

Quota and login both come from `codex app-server`, not from `codex exec`. Send newline-delimited
JSON-RPC on stdin, read it on stdout. Always send `initialize` first, then wait ~0.8s.

| Method | Use |
|---|---|
| `account/read` | email + planType |
| `account/rateLimits/read` | `primary` (300 min) and `secondary` (10080 min) windows with `usedPercent`/`resetsAt`, plan, credits, free reset credits |
| `account/usage/read` | lifetime token stats |
| `account/login/start` `{"type":"chatgpt"}` | returns `{authUrl, loginId}` |
| `account/login/cancel` `{loginId}` | aborts, leaves the existing login intact |
| notification `account/login/completed` | login finished |

**The app-server process must stay alive during login** because it hosts the OAuth callback.
`debate.codex_app_server()` is the one-shot helper; `ui.CODEX_SESSION` is the long-lived one for login.
All of this costs **zero model tokens**.

## Claude login

`claude auth status` prints JSON (`loggedIn`, `email`, `subscriptionType`) and is authoritative.
`claude auth login --claudeai` prints `visit: <url>`, opens a browser, then **blocks on a
`Paste code here` stdin prompt** — there is no local callback. The UI reads the URL from stdout,
shows a text box, and writes the pasted code to the process stdin. Read stdout **one character at a
time**; the prompt has no trailing newline, so line-based reading deadlocks.

## Why model availability is probed, not read

`~/.codex/models_cache.json` is a **global catalogue, not filtered by plan**, and neither login token
exposes model gating. So `ui.probe_provider()` makes one tiny real call per model at its lowest
effort, then walks efforts from the top down to find the ceiling. Results are cached in
`availability.json` keyed by a hash of the account id, so switching accounts re-detects automatically.
Empirically, on a ChatGPT Plus account **everything was callable**, including `gpt-6-astra` and
`ultra` effort. Do not hardcode plan restrictions; trust the probe.

## Billing facts worth remembering

- **Fable is not included in Claude Pro.** `~/.claude.json` lists
  `tengu_usage_overage_included_models = ["Fable", "Fable 5", "Fable 5.1"]`, and a Fable call returns
  `rateLimitType: "overage"` with no subscription windows, while Sonnet/Opus return the normal
  five_hour/seven_day windows. Fable spends prepaid **usage credits**.
- On **Max**, Fable is included up to 50% of the weekly limit, which is a ceiling inside the same
  allowance rather than extra capacity.
- `total_cost_usd` is a **list-price equivalent**. Always label it that way in the UI; the user is not
  charged it on a subscription. Codex reports no price at all, so ChatGPT shows tokens only.

## UI internals

- The entire page is the `HTML` string in `ui.py`. Edit it directly; there is no build step.
- Layout: `main` is a grid of `auto minmax(0,1fr)` so the results pane always keeps height.
  `.top` caps at `62vh` with its own scroll.
- **Progressive disclosure:** model + effort selects are always visible. Everything else
  (account row, login/logout, Re-detect, limit cards) is inside `.detail`, hidden by
  `body.collapsed`. The toggle is `#toggle`, labelled "Show/Hide usage & limits", and the state
  persists in `localStorage` (default collapsed). A run auto-collapses to free up space.
- Long operations are **poll-based**, never websockets: `/status/<job>`, `/detect/status`,
  `/login/status` all take an `offset` or return a `done` flag.

### HTTP endpoints

`GET /` `/history` `/models` `/quota` `/login/status` `/detect/status` `/transcript?path=` `/status/<job>`
`POST /run` `/login` `/login/code` `/login/cancel` `/logout` `/quota/refresh` `/detect` `/stop/<job>`

## Gotchas

- **macOS has no `timeout` command.** Use Python or `subprocess` timeouts.
- `pkill -f "ui.py --port 8765"` misses a plain `python3 ui.py`. Match `ui.py`, or kill by port.
  `python3 ui.py --restart` stops whatever owns the port and takes over.
- Python buffers stdout when redirected, so the server's startup lines only appear immediately on a tty
  or with `python3 -u`.
- A browser closing a poll mid-response raises `BrokenPipeError` in the handler thread. `QuietHTTPServer`
  swallows that (and `ConnectionResetError`) and `_send` catches it, so a closed tab cannot take the
  server down or fill the log with tracebacks. Keep that behaviour if you touch `Handler._send`.
- `debate()` runs both sides **in parallel threads** per round. Anything they touch must be thread safe
  (`CALLS_LOCK` guards the usage list).
- Each debate gets its own `run_dir`, and that directory is the CLI working directory for both models.

## Extending it

- **New provider:** add an `ask_<x>` to `debate.py` following the `ask_claude` signature
  `(cfg, system, prompt, role) -> str`, register it in `ASK`, and add a catalogue entry in `ui.py`.
- **New debate structure:** prompts live in `system_prompt` / `opening_prompt` / `round_prompt` /
  `judge_prompt`. The loop in `debate()` is deliberately small.
- **New UI panel:** add markup to `HTML`, a render function, and an endpoint in `Handler`.
  Put anything secondary inside a `.detail` block so it collapses.
- **Test cheaply:** `python3 debate.py --solo claude --claude-model claude-haiku-4-5-20251001
  --claude-effort low "Say OK"`. A full debate at `--rounds 1` with haiku + `gpt-5.4-mini` costs cents.
