# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env                # fill in GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

python watcher.py --probe           # pin the Gemini API shape; run FIRST after any API/model change
python watcher.py --test-telegram   # confirm token + chat id
python watcher.py --dry-run         # parse + decide; sends nothing, writes no state
python watcher.py --only <id>       # single question (repeatable flag)
python watcher.py                   # real run: notifies and writes state/
```

There is no test suite, linter, or build. Verification is manual — the numbered procedure in
`README.md` ("Verification") is the contract; the only end-to-end proof of the notify path is the
temporary `sky-test` question, because the real question is almost always `no`.

## Architecture

One cron script, deliberately: `watcher.py` (~400 lines, stdlib + `pyyaml` only) driven by
`questions.yml`, with `state/` as the only persistence. Adding a question is a YAML entry, never a
code change. Do not split this into modules or add dependencies.

Per-question flow in `run()`:

1. `call_gemini()` — `generateContent` with `SEARCH_TOOL` grounding, `temperature: 0`, retries on
   429/5xx only (other 4xx will not fix itself).
2. `extract_text()` + `parse_answer()` — the reply is *text*, JSON-parsed after stripping a possible
   markdown fence. There is no `responseSchema`: structured output plus grounding in one call is
   Gemini-3-preview-only.
3. `normalise()` — merges model-declared `sources` with `extract_grounding_urls()` (the API's own
   citations), then applies the guard: **`answer: yes` with zero sources is forced to `no`**.
4. `decide(prev, now)` → `new` / `updated` / `cleared` / `None`, comparing against `state/<id>.json`.
5. Notify on change, then always `save_state()` (unless `--dry-run`).

Two invariants worth preserving:

- **Change is fingerprinted on `sorted(set(details))`, not `summary`.** The model rewords `summary`
  every run; including it would fire a spurious daily "Actualizado".
- **Fail quiet on ambiguity, loud on breakage.** An unparseable reply is logged and skipped with no
  notification; a failed question does not abort the others but makes the process exit 1; a non-2xx
  from Telegram raises. A silent notifier is the failure nobody notices.

### The two API facts everything rests on

`SEARCH_TOOL = {"google_search": {}}` and the citation path
`candidates[0].groundingMetadata.groundingChunks[].web.uri` are both empirically pinned, not
documented guarantees. `--probe` re-verifies them (tries three tool spellings, writes the first 200
to the gitignored `probe.json`) and is the first thing to run when grounding behaviour looks off.
Exit 0 means both facts still hold. Watch for `sources=0` in run logs as the early warning.

Grounding requires a **paid-tier** Gemini key; a free-tier key 429s every grounded call. A `--probe`
that shows 200 without tools and 429 with tools means "grounding not enabled on this key", not "rate
limited".

### Question prompts

Prompts in `questions.yml` pass through `str.format(today=..., weekday=...)` with Europe/Madrid dates
(not UTC), so **literal JSON braces in a prompt must be doubled**: `{{"answer":...}}`. Every prompt
must ask for the same object shape (`answer`/`summary`/`details`/`evidence_date`/`sources`) and
instruct "no source URL => no", since `normalise()` enforces that anyway.

### CI

`.github/workflows/watch.yml` runs daily at 05:00 UTC and commits `state/` back to the repo with
`contents: write`. That commit is load-bearing — it is the only memory of the previous answer, so a
run whose state commit fails will re-alert next time. Cron is UTC and ignores DST, so the local fire
time drifts an hour in winter. `GEMINI_MODEL` comes from a repo **variable**, not a secret.

`load_env_file()` never overwrites an already-set variable, so `.env` is inert in CI.

## Local environment gotcha

Local runs on this machine need the corporate proxy to reach the Gemini API, but that same proxy
captcha-blocks `api.telegram.org`. Practical consequence: verify Gemini-side behaviour locally with
`--dry-run`/`--probe`, and verify the Telegram path via `workflow_dispatch` in Actions. Do not add
proxy-bypass code to `watcher.py`.
