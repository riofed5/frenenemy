---
name: debate
description: Run a Claude-vs-ChatGPT debate on a question (Sonnet 5 via the claude CLI, GPT-5.6 Sol via the codex CLI, subscriptions only) and report the best answer. Use when the user says "debate", "ask GPT", "what does ChatGPT think", or wants two models to compare answers.
---

Run the debate script from this project with the user's question. Run it from the project root (the folder that contains `debate.py`):

```
python3 debate.py "<question>" --rounds 2
```

- Use a Bash timeout of at least 600000 ms; a two-round debate usually takes several minutes.
- Pass `--rounds N` if the user asks for more or fewer rounds, `--judge gpt` if they want ChatGPT to chair, `--web` if the question needs fresh facts.
- To just get ChatGPT's opinion without a debate: `--solo gpt "<question>"`.
- When it finishes, read the transcript path it printed and tell the user, in their language: the best answer from the verdict, what both models agreed on, and what stayed unresolved. Keep it short; link the transcript file.
- If the script says codex is not logged in, tell the user to run `codex login` in a terminal and sign in with their ChatGPT account (not an API key). Do not try to log in for them.
- If the script says the GPT model is unavailable, retry once with `--gpt-model gpt-5.5` and tell the user.
