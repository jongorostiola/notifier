#!/usr/bin/env python3
"""Ask Gemini a set of yes/no questions on a schedule; Telegram me on every yes.

Stateless on purpose: nothing is remembered between runs, so an ongoing closure
alerts again the next day. Single file on purpose: this is a cron script, not an
application.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
QUESTIONS_FILE = ROOT / "questions.yml"
ENV_FILE = ROOT / ".env"
TZ = ZoneInfo("Europe/Madrid")

MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
# Verified by --probe: the v1beta REST spelling of the search tool.
SEARCH_TOOL = {"google_search": {}}

TIMEOUT = 120
RETRIES = 3


def log(msg):
    print(msg, flush=True)


# Windows consoles default to cp1252 and would crash printing the emoji headings.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def post_json(url, payload, headers, timeout=TIMEOUT):
    """POST and return (status, body_text). Raises only on transport errors."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #

def call_gemini(prompt, api_key, tool=None):
    """Call generateContent with search grounding. Retries 429/5xx."""
    payload = {
        "tools": [tool or SEARCH_TOOL],
        "generationConfig": {"temperature": 0},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    url = GEMINI_URL.format(model=MODEL)
    headers = {"x-goog-api-key": api_key}

    last = None
    for attempt in range(RETRIES):
        status, body = post_json(url, payload, headers)
        if status == 200:
            return json.loads(body)
        last = f"HTTP {status}: {body[:500]}"
        if status == 429 or status >= 500:
            wait = 2 ** attempt * 5
            log(f"  gemini {status}, retrying in {wait}s ({attempt + 1}/{RETRIES})")
            time.sleep(wait)
            continue
        break  # 4xx other than 429 will not fix itself
    raise RuntimeError(f"gemini call failed: {last}")


def extract_text(response):
    """Concatenate every text part — grounded replies are often split up."""
    candidates = response.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p["text"] for p in parts if isinstance(p.get("text"), str))


def extract_grounding_urls(response):
    candidates = response.get("candidates") or []
    if not candidates:
        return []
    meta = candidates[0].get("groundingMetadata") or {}
    urls = []
    for chunk in meta.get("groundingChunks") or []:
        uri = ((chunk.get("web") or {}).get("uri") or "").strip()
        if uri:
            urls.append(uri)
    return urls


FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_answer(text):
    """Strip any fence and json.loads. Returns None on failure."""
    stripped = FENCE.sub("", text.strip())
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def normalise(obj, grounding_urls):
    answer = str(obj.get("answer", "")).strip().lower()
    if answer not in ("yes", "no"):
        return None

    details = [str(d).strip() for d in (obj.get("details") or []) if str(d).strip()]

    sources = [str(s).strip() for s in (obj.get("sources") or []) if str(s).strip()]
    for url in grounding_urls:
        if url not in sources:
            sources.append(url)

    # Anti-hallucination backstop: a yes with nothing to point at is a no.
    if answer == "yes" and not sources:
        log("  GUARD: answer=yes with no sources -> forcing no")
        answer = "no"

    return {
        "answer": answer,
        "summary": str(obj.get("summary", "")).strip(),
        "details": details,
        "evidence_date": str(obj.get("evidence_date", "unknown")).strip() or "unknown",
        "sources": sources,
    }


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(text, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    status, body = post_json(
        url,
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        headers={},
        timeout=30,
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"telegram sendMessage failed: HTTP {status}: {body[:500]}")


def build_message(label, result):
    e = html.escape
    lines = [f"<b>🚧 {e(label)} 🚧</b>"]
    if result["summary"]:
        lines.append(e(result["summary"]))
    if result["details"]:
        lines.append("")
        lines += [f"• {e(d)}" for d in result["details"]]
    lines.append("")
    lines.append(f"<i>Evidencia: {e(result['evidence_date'])}</i>")
    if result["sources"]:
        lines.append("")
        lines += [
            f'<a href="{e(u, quote=True)}">{e(u[:80])}</a>' for u in result["sources"][:5]
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def load_env_file():
    """Read .env for local runs. Real env vars win, so CI is unaffected."""
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
    log(f"loaded {ENV_FILE.name}")


def env_or_die(name):
    value = os.environ.get(name)
    if not value:
        log(f"missing environment variable {name}")
        sys.exit(1)
    return value


def probe(api_key):
    """Pin the API shape: which tool key returns 200, and where citations live."""
    prompt = "Any streets closed in Bilbao today? cite sources"
    url = GEMINI_URL.format(model=MODEL)
    for tool in ({"google_search": {}}, {"googleSearch": {}}, {"type": "google_search"}):
        label = json.dumps(tool)
        status, body = post_json(
            url,
            {
                "tools": [tool],
                "contents": [{"parts": [{"text": prompt}]}],
            },
            {"x-goog-api-key": api_key},
        )
        log(f"tool {label}: HTTP {status}")
        if status != 200:
            log(f"  {body[:300]}")
            if status == 404:
                log(f"  model {MODEL} unavailable — set GEMINI_MODEL to a current model")
            elif status == 429:
                log("  grounding quota exhausted. Check whether ungrounded calls work:")
                log("  a 200 without tools + 429 with tools means search grounding is")
                log("  not enabled on this key, not that you hit a rate limit.")
            continue
        (ROOT / "probe.json").write_text(body, encoding="utf-8")
        response = json.loads(body)
        urls = extract_grounding_urls(response)
        log(f"  wrote probe.json; grounding URIs found: {len(urls)}")
        for u in urls[:3]:
            log(f"    {u}")
        log(f"  text ({len(extract_text(response))} chars): {extract_text(response)[:300]}")
        if not urls:
            log("  WARNING: 200 but no candidates[0].groundingMetadata."
                "groundingChunks[].web.uri — citation path changed, fix watcher.py")
        return 0 if urls else 1
    log("no tool spelling returned 200 — design is void, stop here")
    return 1


def run(args):
    questions = yaml.safe_load(QUESTIONS_FILE.read_text(encoding="utf-8")) or []
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]
        if not questions:
            log(f"no question matched {args.only}")
            return 1

    api_key = env_or_die("GEMINI_API_KEY")
    if not args.dry_run:
        token = env_or_die("TELEGRAM_BOT_TOKEN")
        chat_id = env_or_die("TELEGRAM_CHAT_ID")

    now = datetime.now(TZ)
    today, weekday = now.strftime("%Y-%m-%d"), now.strftime("%A")
    failures = 0

    for q in questions:
        qid = q["id"]
        label = q.get("title") or qid  # `title` is for humans, `id` for --only
        log(f"[{qid}]")
        try:
            prompt = q["prompt"].format(today=today, weekday=weekday)
            response = call_gemini(prompt, api_key)
            text = extract_text(response)
            parsed = parse_answer(text)
            result = normalise(parsed, extract_grounding_urls(response)) if parsed else None
            if result is None:
                # Fail quiet on ambiguity rather than crying wolf.
                log(f"  unparseable reply, skipping. raw: {text[:800]!r}")
                continue

            notify = result["answer"] == "yes"
            log(f"  answer={result['answer']} sources={len(result['sources'])} "
                f"notify={'yes' if notify else 'no'}")
            log(f"  parsed: {json.dumps(result, ensure_ascii=False)}")

            if not notify:
                continue
            if args.dry_run:
                log("  would notify:")
                log(build_message(label, result))
                continue

            send_telegram(build_message(label, result), token, chat_id)
            log("  notified")
        except Exception as e:  # one bad question must not abort the others
            failures += 1
            log(f"  FAILED: {e}")

    return 1 if failures else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="print parsed answer + notify decision; send nothing")
    p.add_argument("--test-telegram", action="store_true",
                   help="send a fixed message to confirm token/chat id")
    p.add_argument("--probe", action="store_true",
                   help="pin the Gemini tool key and citation path, write probe.json")
    p.add_argument("--only", action="append", metavar="ID",
                   help="run only this question id (repeatable)")
    args = p.parse_args()

    load_env_file()

    if args.probe:
        return probe(env_or_die("GEMINI_API_KEY"))
    if args.test_telegram:
        send_telegram(
            "🚧 <b>Notifier</b> test message — if you see this, "
            "the token and chat id are correct.",
            env_or_die("TELEGRAM_BOT_TOKEN"),
            env_or_die("TELEGRAM_CHAT_ID"),
        )
        log("sent")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
