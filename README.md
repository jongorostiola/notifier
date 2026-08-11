# Notifier

Asks Gemini a list of yes/no questions on a daily cron and sends a Telegram message **only when the
answer changes**. Config-driven: adding a question is one entry in `questions.yml`, no code change.

```
.github/workflows/watch.yml   cron + manual trigger
watcher.py                    the whole thing
questions.yml                 the questions
state/                        last answer per question, committed back by the workflow
```

## Setup

1. **Use a private repo.** Public repos auto-disable scheduled workflows after 60 days of inactivity.

2. Set three repo secrets (Settings → Secrets and variables → Actions):

   | Secret | How to get it |
   |---|---|
   | `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> |
   | `TELEGRAM_BOT_TOKEN` | message `@BotFather` → `/newbot` |
   | `TELEGRAM_CHAT_ID` | message your bot, then `GET https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id` |

3. *Optional:* to run a non-default model in Actions, set a repo **variable** (Settings → Secrets and
   variables → Actions → **Variables**) named `GEMINI_MODEL`. It is not a secret, so it does not belong
   in the Secrets tab. Leave it unset to use the default baked into `watcher.py`. Locally, `.env` does
   the same job.

4. Allow Actions to push: Settings → Actions → General → Workflow permissions → **Read and write**.
   (The workflow already requests `contents: write`, but the repo-level setting must permit it.)

## Local use

```bash
pip install -r requirements.txt
cp .env.example .env     # then fill in the three values

python watcher.py --probe           # pin the API shape — run this FIRST, see below
python watcher.py --test-telegram   # confirm token + chat id
python watcher.py --dry-run         # parse + decide, send nothing, write no state
python watcher.py --only bilbao-street-closures
python watcher.py                   # the real thing
```

`watcher.py` reads `.env` from the repo root at startup (`KEY=value` per line, `#` comments and quotes
around values are fine). It is gitignored — never commit it; `.env.example` is the tracked template.
Already-set environment variables take precedence, so the file is inert in CI where the values come from
repo secrets. No `python-dotenv` dependency; it's ten lines of stdlib.

### `--probe` first

Everything downstream depends on two unverified-by-default facts: which spelling of the search tool
the API accepts, and where citations live in the response. `--probe` tries
`{"google_search":{}}`, then `{"googleSearch":{}}`, then `{"type":"google_search"}`, prints the status
of each, writes the first 200 to `probe.json`, and reports how many
`candidates[0].groundingMetadata.groundingChunks[].web.uri` entries it found.

- Exit 0 → the spelling in `watcher.py` (`SEARCH_TOOL`) is right and citations parse. Nothing to do.
- A different spelling returned 200 → update `SEARCH_TOOL` in `watcher.py` to match.
- 200 but 0 URIs → the citation path changed; fix `extract_grounding_urls`.
- Nothing returned 200 → stop; the design does not work as written.

## Adding a question

Append to `questions.yml`. `{today}` and `{weekday}` are substituted (Europe/Madrid, not UTC);
**literal JSON braces must be doubled** — `{{"answer":...}}` — because the prompt goes through
`str.format`. Ask for the same JSON object shape; `answer` must be `"yes"` or `"no"`.

## When it notifies

| Previous | Now | Message |
|---|---|---|
| absent or `no` | `yes` | 🚧 Nuevo |
| `yes` | `yes`, different `details` | 🚧 Actualizado |
| `yes` | `no` | ✅ Resuelto |
| `no` | `no` | *(silence)* |
| `yes` | `yes`, same `details` | *(silence)* |

Change is fingerprinted on `sorted(set(details))`, not on `summary` — the model rewords the summary
every run, which would fire a spurious daily "updated".

Guards:
- `answer: yes` with zero sources is forced to `no` and logged. Every Yes carries a URL.
- An unparseable reply is logged and skipped — no notification. Fail quiet on ambiguity.
- A failed question does not abort the others, but makes the run exit non-zero (red).
- A non-2xx from Telegram exits non-zero. A silent notifier is the failure you'd never notice.

State is always written when not in `--dry-run`, notification or not.

## Verification

1. `python watcher.py --probe` → exit 0.
2. `python watcher.py --test-telegram` → message arrives.
3. `python watcher.py --dry-run` → prints parsed JSON and the would-notify decision.
4. **Forced positive** — the only end-to-end proof of the notify path, since Bilbao is usually "no".
   Temporarily append to `questions.yml`:

   ```yaml
   - id: sky-test
     prompt: |
       Today is {today} ({weekday}). Is the sky blue on a clear day?
       Reply with ONLY a JSON object, no markdown fence:
       {{"answer":"yes"|"no","summary":"...","details":["..."],
         "evidence_date":"YYYY-MM-DD or unknown","sources":["url"]}}
   ```

   `python watcher.py --only sky-test` → Telegram message. Then remove the entry and
   `state/sky-test.json`.
5. **Change detection** — edit `state/bilbao-street-closures.json` to `"answer": "no"` with
   `"details": []`, re-run → expect a "Nuevo" alert *if* today's answer is yes. To test deterministically,
   use `sky-test`: run it, then blank its state and re-run (expect an alert), then re-run again
   immediately (expect silence).
6. `workflow_dispatch` in GitHub → green run, `state/` commit appears, quota visible in AI Studio.
7. Let one real cron fire; confirm it ran near 07:00 Madrid.

## Notes

- Model: `gemini-3.6-flash` (override with `GEMINI_MODEL`). Grounding is free for the first 5,000
  prompts/month across Gemini 3 models, then $14/1,000. `gemini-3.5-flash-lite` is the cheaper
  fallback. Grounding requires a **paid-tier** key — a free-tier key 429s every grounded call.
- No `responseSchema`: structured output + grounding in one call is Gemini-3-only (preview), so the
  JSON is parsed out of the text.
- Cron is UTC and does not follow DST, so the local run time drifts an hour in winter.
- Actions cron is best-effort — a run can be delayed or dropped under load.
- Treat an alert as "go look at this link", not ground truth. `evidence_date` is there so a wrong
  answer is diagnosable from the message itself.
