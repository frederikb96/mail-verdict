#!/usr/bin/env python3
"""
Manual evaluation of the orders pipeline's two model calls (decide and
write) against known-answer cases. Never a CI job: it calls real models.

A case is a frozen JSON file holding the inputs of one call; the system
prompts and the prompt assembly are the product's own (orders/prompts.py,
orders/documents.py), read afresh on every run, so a run measures exactly the
prompt files as they stand. Expectations live beside the cases
(tests/eval/orders/expectations.yaml) and are scored by
tests/eval/orders/scorer.py.

Commands (run from the repository root with `uv run --extra dev -- python
scripts/eval_orders.py ...`):

  build    make cases from a mailbox export (a directory of `orders/`,
           `msgs/` and `att/`) and a manifest; the result stays outside the
           repository because it is real mail
  run      one case set against one model; one JSON record per case and run
  rescore  score the records of a label again against current expectations
  table    one row per label: passes, first-answer validity, latency, tokens,
           cost
  tally    token and cost totals of one or more labels

Models are `anthropic:<model>` or `infomaniak:<model>` (the latter needs
`--base-url`, the OpenAI-compatible endpoint of the account). The API keys
are read through the `secret` CLI into memory and never printed.

Case format (JSON): `name`, `task` ("write", "decide" or "file"), then
- write: `language`, `current_text` (null or merchant/subject/status/summary),
  `mails` (received_at, from_addr, subject, body, documents), `files`
  (documents plus `added`)
- decide / file: `mail` (received_at, from_addr, to_addrs, subject, body,
  documents) or `file` (added_at, documents), and `candidates`
A document holds filename, content_type, page_count, method, the whole
extracted text with its `[code bN ...]` markers, and `barcodes`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The harness imports the product, whose config loader insists on a database
# address; nothing here connects to one.
os.environ.setdefault("MAIL_VERDICT_DATABASE_URL", "postgresql+asyncpg://eval@localhost/eval")
os.environ.setdefault("MAIL_VERDICT_SECURITY_ENCRYPTION_KEY", "0" * 64)

from mail_verdict.core.retry import RetryConfig  # noqa: E402
from mail_verdict.core.structured_llm import (  # noqa: E402
    call_anthropic_structured,
    call_chat_completions_structured,
)
from mail_verdict.orders import documents, prompts  # noqa: E402
from mail_verdict.orders.candidates import Candidate, CandidateMail  # noqa: E402
from mail_verdict.orders.extract.model import Barcode, ExtractedDocument  # noqa: E402
from mail_verdict.orders.extract.truncate import is_terms_like, truncate_text  # noqa: E402
from mail_verdict.orders.worker import _WRITE_ALL_MAILS_CEILING  # noqa: E402
from mail_verdict.settings.defaults import SETTING_DEFAULTS  # noqa: E402
from tests.eval.orders import scorer  # noqa: E402

DEFAULT_CASES = REPO / "tests" / "eval" / "orders" / "cases"
DEFAULT_EXPECT = REPO / "tests" / "eval" / "orders" / "expectations.yaml"
_NAMESPACE = uuid.UUID("5f0c1e6e-0000-4000-8000-000000000001")
_ACCOUNT = uuid.uuid5(_NAMESPACE, "account")

# Per million tokens: (input, output, cache read), from the providers' price
# pages. Anthropic in USD, Infomaniak in CHF.
PRICES: dict[str, tuple[float, float, float]] = {
    "anthropic:claude-opus-5-5": (4.0, 20.0, 0.4),
    "anthropic:claude-sonnet-5-5": (2.0, 10.0, 0.2),
    "anthropic:claude-haiku-5-5": (0.1, 0.5, 0.01),
    "infomaniak:Qwen/Qwen3.8-27B-FP8": (0.40, 3.00, 0.40),
    "infomaniak:mistralai/Mistral-Small-4-119B-2603": (0.20, 0.75, 0.20),
    "infomaniak:moonshotai/Kimi-K2.6": (0.60, 3.00, 0.60),
    "infomaniak:Qwen/Qwen3.5-397B-A17B-FP8": (0.80, 3.60, 0.80),
    "infomaniak:google/gemma-4-31B-it": (0.20, 0.40, 0.20),
}
CURRENCY = {"anthropic": "USD", "infomaniak": "CHF"}


# --- Cases -> prompts ---------------------------------------------------------


def _doc_barcodes(doc: dict[str, Any]) -> tuple[Barcode, ...]:
    return tuple(
        Barcode(
            index=b["index"], format=b["format"],
            payload=base64.b64decode(b.get("payload_b64", "")),
            text=b.get("text"), page=None, bbox=None, text_offset=None,
        )
        for b in doc.get("barcodes", [])
    )


def _extracted(doc: dict[str, Any]) -> ExtractedDocument:
    return ExtractedDocument(
        filename=doc["filename"], content_type=doc["content_type"], size_bytes=0, sha256="",
        method=doc["method"], page_count=doc.get("page_count"), text=doc["text"],
        text_chars_total=len(doc["text"]), barcodes=_doc_barcodes(doc),
    )


def _stored_text(doc: dict[str, Any]) -> str:
    """The text a stored document holds (documents._row_values)."""
    cut = truncate_text(doc["text"], terms_like=is_terms_like(doc["filename"], doc["text"]))
    return cut.text[:20_000].replace("\x00", "")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _candidate(index: int, raw: dict[str, Any]) -> Candidate:
    return Candidate(
        order_id=uuid.uuid5(_NAMESPACE, f"candidate-{index}"), merchant=raw["merchant"],
        subject=raw["subject"], status=raw["status"], is_open=raw["is_open"],
        summary=raw["summary"], mail_count=raw["mail_count"],
        first_mail_at=_dt(raw["first_mail_at"]) if raw.get("first_mail_at") else None,
        last_mail_at=_dt(raw["last_mail_at"]) if raw.get("last_mail_at") else None,
        numbers=tuple((k, v) for k, v in raw["numbers"]), reasons=tuple(raw["reasons"]),
        latest_mails=tuple(
            CandidateMail(_dt(m["received_at"]), m["from_addr"], m["subject"])
            for m in raw.get("latest_mails", [])
        ),
    )


def _context(built: dict[str, Any]) -> dict[str, Any]:
    """What the scorer needs besides the answer and the expectations."""
    return {k: built[k] for k in ("task", "input_text", "markers")}


def case_prompts(case: dict[str, Any]) -> dict[str, Any]:
    """
    The call a case stands for, built through the product's own functions.

    Returns:
        system, user, schema, validate, schema_name, max_tokens (the task's
        default), task, and the scoring context (`input_text`, `markers`)
    """
    orders = SETTING_DEFAULTS["orders"]
    if case["task"] == "write":
        return _write_prompts(case, orders)
    return _decide_prompts(case, orders)


def _write_prompts(case: dict[str, Any], orders: dict[str, Any]) -> dict[str, Any]:
    all_mails = case["mails"]
    if len(all_mails) > _WRITE_ALL_MAILS_CEILING:
        shown = all_mails[:3] + all_mails[-9:]
    else:
        shown = all_mails
    keys = {id(m): f"m{i}" for i, m in enumerate(all_mails)}
    mails: list[dict[str, Any]] = []
    stored: list[documents.StoredDocument] = []
    for mail in shown:
        key = keys[id(mail)]
        mails.append({
            "account_id": _ACCOUNT, "msg_key": key, "received_at": mail["received_at"],
            "from_addr": mail["from_addr"], "subject": mail["subject"], "body": mail["body"],
        })
    for mail in all_mails:
        key = keys[id(mail)]
        if mail not in shown:
            continue
        for j, doc in enumerate(mail["documents"]):
            stored.append(documents.StoredDocument(
                id=uuid.uuid5(_NAMESPACE, f"{key}/{j}"), source="mail", account_id=_ACCOUNT,
                msg_key=key, file_id=None, filename=doc["filename"],
                content_type=doc["content_type"], page_count=doc.get("page_count"),
                method=doc["method"], text=_stored_text(doc), added_at=None,
            ))
    for j, doc in enumerate(case.get("files", [])):
        file_id = uuid.uuid5(_NAMESPACE, f"file/{j}")
        stored.append(documents.StoredDocument(
            id=uuid.uuid5(_NAMESPACE, f"filedoc/{j}"), source="file", account_id=None,
            msg_key=None, file_id=file_id, filename=doc["filename"],
            content_type=doc["content_type"], page_count=doc.get("page_count"),
            method=doc["method"], text=_stored_text(doc), added_at=_dt(doc["added"]),
        ))
    stored.sort(key=lambda d: (d.filename, str(d.id)))
    write_input = documents.assemble_write_input(mails, stored)
    user = prompts.build_write_user_prompt(
        current_text=case.get("current_text"), mails=write_input.mails, files=write_input.files,
    )
    return {
        "task": "write", "schema_name": "order_text", "schema": prompts.WRITE_SCHEMA,
        "system": prompts.build_write_system_prompt(
            language=case.get("language", orders["language"]),
        ),
        "user": user, "validate": prompts.validate_write_response,
        "max_tokens": orders["write_max_tokens"],
        "input_text": user, "markers": sorted(write_input.code_map),
    }


def _decide_prompts(case: dict[str, Any], orders: dict[str, Any]) -> dict[str, Any]:
    candidates = [_candidate(i, c) for i, c in enumerate(case["candidates"])]
    handles = [f"C{i + 1}" for i in range(len(candidates))]
    allow_none = case["task"] == "decide"
    if allow_none:
        mail = case["mail"]
        reads = [documents.ReadDocument(None, None, _extracted(d)) for d in mail["documents"]]
        user = prompts.build_decide_user_prompt(
            subject=mail["subject"], from_addr=mail["from_addr"], to_addrs=mail.get("to_addrs", ""),
            received_at=mail["received_at"],
            attachments=[(d["filename"], d["content_type"]) for d in mail["documents"]],
            body=mail["body"], candidates=candidates, documents=documents.decide_documents(reads),
        )
    else:
        item = case["file"]
        reads = [documents.ReadDocument(None, None, _extracted(d)) for d in item["documents"]]
        user = prompts.build_decide_file_prompt(
            added_at=item["added_at"], documents=documents.decide_documents(reads),
            candidates=candidates,
        )
    return {
        "task": case["task"], "schema_name": "order_decision",
        "schema": prompts.build_decide_schema(handles, allow_none=allow_none),
        "system": prompts.load_decide_system_prompt(), "user": user,
        "validate": lambda data: prompts.validate_decide_response(
            data, handles=handles, allow_none=allow_none,
        ),
        "max_tokens": orders["decide_max_tokens"], "input_text": user, "markers": [],
    }


def load_cases(directory: Path, names: list[str] | None) -> list[dict[str, Any]]:
    cases = [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]
    if names:
        missing = set(names) - {c["name"] for c in cases}
        if missing:
            raise SystemExit(f"no such case: {sorted(missing)}")
        cases = [c for c in cases if c["name"] in names]
    return cases


def load_expectations(path: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    return data["cases"]


# --- Models -------------------------------------------------------------------


def _secret(name: str) -> str:
    """A secret's value, read into memory; the name alone is on the command line."""
    done = subprocess.run(["secret", "get", name], capture_output=True, text=True, check=True)
    return done.stdout.strip()


class _Tap:
    """Wraps one `create` method, keeping every response (or error) and its
    wall time for the record."""

    def __init__(self, create: Any, log: list[dict[str, Any]]) -> None:
        self._create = create
        self._log = log

    async def create(self, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            response = await self._create(**kwargs)
        except Exception as exc:
            self._log.append({"error": exc, "seconds": time.monotonic() - started})
            raise
        self._log.append({"response": response, "seconds": time.monotonic() - started})
        return response


def make_client(spec: str, base_url: str | None) -> tuple[Any, list[dict[str, Any]], str]:
    """A recording client for `provider:model`; returns (client, log, provider)."""
    provider, _, _ = spec.partition(":")
    log: list[dict[str, Any]] = []
    if provider == "anthropic":
        from anthropic import AsyncAnthropic

        real = AsyncAnthropic(api_key=_secret("ANTHROPIC_API_KEY_MANUAL"), max_retries=0)
        return SimpleNamespace(messages=_Tap(real.messages.create, log)), log, provider
    if provider == "infomaniak":
        from openai import AsyncOpenAI

        if not base_url:
            raise SystemExit("infomaniak needs --base-url")
        real_oa = AsyncOpenAI(
            api_key=_secret("INFOMANIAK_AI_TOKEN"), base_url=base_url, max_retries=0,
        )
        completions = _Tap(real_oa.chat.completions.create, log)
        return SimpleNamespace(chat=SimpleNamespace(completions=completions)), log, provider
    raise SystemExit(f"unknown provider {provider!r}")


def _usage(entry: dict[str, Any], provider: str) -> dict[str, Any]:
    """Tokens, finish reason and error shape of one logged call. An error
    keeps its class, status and type only: a body may echo the request."""
    out: dict[str, Any] = {"seconds": round(entry["seconds"], 2)}
    if "error" in entry:
        exc = entry["error"]
        body = getattr(exc, "body", None)
        detail = body.get("error", body) if isinstance(body, dict) else {}
        out["error"] = {
            "class": type(exc).__name__, "status": getattr(exc, "status_code", None),
            "type": detail.get("type") if isinstance(detail, dict) else None,
            "code": detail.get("code") if isinstance(detail, dict) else None,
        }
        return out
    response = entry["response"]
    usage = response.usage
    if provider == "anthropic":
        out.update({
            "in": usage.input_tokens, "out": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "finish": response.stop_reason,
        })
    else:
        details = getattr(usage, "completion_tokens_details", None)
        out.update({
            "in": usage.prompt_tokens, "out": usage.completion_tokens,
            "reasoning": getattr(details, "reasoning_tokens", None) if details else None,
            "finish": response.choices[0].finish_reason,
        })
    return out


def _raw(entry: dict[str, Any], provider: str) -> str:
    response = entry.get("response")
    if response is None:
        return ""
    if provider == "anthropic":
        return "".join(b.text for b in response.content if b.type == "text")
    return response.choices[0].message.content or ""


async def run_one(
    case: dict[str, Any], expect: dict[str, Any], *, spec: str, effort: str | None,
    max_tokens: int | None, timeout: float, attempts: int, base_url: str | None,
    synthetic: bool,
) -> dict[str, Any]:
    """One case on one model: up to `attempts` calls without feeding errors
    back (the product's retry does the same), the first call's validity kept."""
    built = case_prompts(case)
    if spec.startswith("anthropic:") and built["task"] != "write" and not synthetic:
        # Ordinary mail is read by the decide call, which stays on Infomaniak.
        raise SystemExit(
            f"{case['name']}: decide cases of real mail never go to an external provider",
        )
    client, log, provider = make_client(spec, base_url)
    model = spec.partition(":")[2]
    budget = max_tokens or built["max_tokens"]
    no_retry = RetryConfig(max_retries=0, base_delay=0.0, max_delay=0.0, exp_base=2.0)
    answer: dict[str, Any] | None = None
    error = None
    started = time.monotonic()
    first_valid = False
    for attempt in range(attempts):
        try:
            if provider == "anthropic":
                coro = call_anthropic_structured(
                    client, model, effort, budget, built["system"], built["user"],
                    built["schema"], no_retry, built["validate"], timeout,
                )
            else:
                coro = call_chat_completions_structured(
                    client, model, effort, budget, built["schema_name"], built["system"],
                    built["user"], built["schema"], no_retry, built["validate"], timeout,
                )
            answer = await asyncio.wait_for(coro, timeout + 15)
            first_valid = attempt == 0
            break
        except Exception as exc:  # recorded, never printed with a body
            error = type(exc).__name__
            if not isinstance(exc, RuntimeError):
                break
    wall = time.monotonic() - started
    context = _context(built)
    result = scorer.score(answer, expect, context)
    last_raw = _raw(log[-1], provider) if answer is None and log else ""
    return {
        "case": case["name"], "task": built["task"], "spec": spec, "effort": effort,
        "max_tokens": budget, "first_valid": first_valid,
        "error": error if answer is None else None,
        "calls": [_usage(e, provider) for e in log], "wall_s": round(wall, 2),
        "answer": answer, "last_raw": last_raw[:4000],
        "hard": result.hard, "soft": result.soft, "ok": result.ok,
    }


def _record_line(label: str, rec: dict[str, Any], run: int) -> str:
    calls = rec["calls"]
    tin = sum(c.get("in", 0) for c in calls)
    tout = sum(c.get("out", 0) for c in calls)
    reasoning = sum(c.get("reasoning") or 0 for c in calls)
    status = "PASS" if rec["ok"] else "FAIL"
    head = (
        f"{status} {rec['case']}.r{run} {label} calls={len(calls)} "
        f"first_valid={rec['first_valid']} wall={rec['wall_s']}s in={tin} out={tout} "
        f"reasoning={reasoning}"
    )
    lines = [head]
    if rec["error"]:
        lines.append(f"    error: {rec['error']}")
    lines += [f"    {msg}" for msg in rec["hard"]]
    return "\n".join(lines)


async def cmd_run(args: argparse.Namespace) -> None:
    cases = load_cases(Path(args.cases_dir), args.cases.split(",") if args.cases else None)
    if args.tasks:
        cases = [c for c in cases if c["task"] in args.tasks.split(",")]
    expectations = load_expectations(Path(args.expect))
    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(min(args.jobs, 3))

    async def one(case: dict[str, Any], run: int) -> None:
        path = out / f"{case['name']}.r{run}.json"
        if path.exists():
            return
        async with gate:
            rec = await run_one(
                case, expectations[case["name"]], spec=args.model, effort=args.effort or None,
                max_tokens=args.max_tokens, timeout=args.timeout, attempts=args.attempts,
                base_url=args.base_url, synthetic=Path(args.cases_dir) == DEFAULT_CASES,
            )
        path.write_text(json.dumps(rec, indent=1, ensure_ascii=False))
        print(_record_line(args.label, rec, run), flush=True)

    await asyncio.gather(*(one(c, r) for c in cases for r in range(1, args.repeat + 1)))


def cmd_rescore(args: argparse.Namespace) -> None:
    cases = {c["name"]: c for c in load_cases(Path(args.cases_dir), None)}
    expectations = load_expectations(Path(args.expect))
    for path in sorted((Path(args.out) / args.label).glob("*.json")):
        rec = json.loads(path.read_text())
        built = case_prompts(cases[rec["case"]])
        result = scorer.score(rec["answer"], expectations[rec["case"]], _context(built))
        rec.update(hard=result.hard, soft=result.soft, ok=result.ok)
        path.write_text(json.dumps(rec, indent=1, ensure_ascii=False))
        print(_record_line(args.label, rec, int(path.stem.rsplit(".r", 1)[1])))


# --- Judge ----------------------------------------------------------------------

_JUDGE_SYSTEM = """You compare two answers to the same task. You get the task's instructions, \
the input the answers were written from, and answers A and B. Check every claim of each answer \
against the input.

For each answer list its concrete errors, one short line each: a statement the input does not \
support, a fact of the input that matters and is missing, a rule of the instructions it breaks. \
Do not list style preferences. Then score the answer from 1 (unusable) to 5 (nothing to correct). \
Identical content is not a reason to prefer either."""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        side: {
            "type": "object",
            "properties": {
                "errors": {"type": "array", "items": {"type": "string"}},
                "score": {"type": "integer"},
            },
            "required": ["errors", "score"],
            "additionalProperties": False,
        }
        for side in ("a", "b")
    },
    "required": ["a", "b"],
    "additionalProperties": False,
}


def _judge_user(built: dict[str, Any], first: dict[str, Any], second: dict[str, Any]) -> str:
    return (
        f"<instructions>\n{built['system']}\n</instructions>\n\n<input>\n{built['user']}\n</input>\n\n"
        f"<answer_a>\n{json.dumps(first, ensure_ascii=False, indent=1)}\n</answer_a>\n\n"
        f"<answer_b>\n{json.dumps(second, ensure_ascii=False, indent=1)}\n</answer_b>"
    )


async def cmd_judge(args: argparse.Namespace) -> None:
    import random

    cases = {c["name"]: c for c in load_cases(Path(args.cases_dir), None)}
    root = Path(args.out)
    out = root / "judgments" / f"{args.label_a}__{args.label_b}"
    out.mkdir(parents=True, exist_ok=True)
    client, log, _ = make_client(args.model, None)
    gate = asyncio.Semaphore(3)

    async def one(name: str) -> None:
        path = out / f"{name}.json"
        if path.exists():
            return
        recs = [
            json.loads((root / label / f"{name}.r1.json").read_text())
            for label in (args.label_a, args.label_b)
        ]
        if any(r["answer"] is None for r in recs) or recs[0]["answer"] == recs[1]["answer"]:
            return
        built = case_prompts(cases[name])
        order = [0, 1]
        random.Random(name).shuffle(order)
        user = _judge_user(built, recs[order[0]]["answer"], recs[order[1]]["answer"])
        async with gate:
            data = await call_anthropic_structured(
                client, args.model.partition(":")[2], args.effort, 4000, _JUDGE_SYSTEM, user,
                _JUDGE_SCHEMA,
                RetryConfig(max_retries=1, base_delay=1.0, max_delay=4.0, exp_base=2.0),
                None, 180.0,
            )
        scores = {args.label_a: None, args.label_b: None}
        errors: dict[str, list[str]] = {}
        for side, idx in zip(("a", "b"), order, strict=True):
            label = (args.label_a, args.label_b)[idx]
            scores[label] = data[side]["score"]
            errors[label] = data[side]["errors"]
        path.write_text(json.dumps({"case": name, "scores": scores, "errors": errors}, indent=1,
                                   ensure_ascii=False))
        print(f"{name}: {scores}", flush=True)

    wanted = set(args.cases.split(",")) if args.cases else None
    names = sorted(
        n for n in cases
        if cases[n]["task"] == "write" and (wanted is None or n in wanted)
        and (root / args.label_a / f"{n}.r1.json").exists()
        and (root / args.label_b / f"{n}.r1.json").exists()
    )
    await asyncio.gather(*(one(n) for n in names))
    usage = [_usage(e, "anthropic") for e in log]
    tokens_in = sum(u.get("in", 0) for u in usage)
    print("judge tokens in/out:", tokens_in, sum(u.get("out", 0) for u in usage))


# --- Tables -------------------------------------------------------------------


def _records(root: Path, label: str) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((root / label).glob("*.json"))]


def _cost(rec: dict[str, Any]) -> float:
    price = PRICES.get(rec["spec"])
    if price is None:
        return 0.0
    total = 0.0
    for c in rec["calls"]:
        total += (
            c.get("in", 0) * price[0] + c.get("out", 0) * price[1]
            + c.get("cache_read", 0) * price[2] + c.get("cache_write", 0) * price[0] * 1.25
        ) / 1e6
    return total


def cmd_tally(args: argparse.Namespace) -> None:
    totals: dict[str, list[float]] = {}
    for label in args.labels:
        for rec in _records(Path(args.out), label):
            cur = CURRENCY[rec["spec"].partition(":")[0]]
            t = totals.setdefault(cur, [0, 0, 0, 0.0])
            t[0] += len(rec["calls"])
            t[1] += sum(c.get("in", 0) for c in rec["calls"])
            t[2] += sum(c.get("out", 0) for c in rec["calls"])
            t[3] += _cost(rec)
    for cur, (calls, tin, tout, cost) in totals.items():
        print(f"{cur}: calls={int(calls)} in={int(tin)} out={int(tout)} cost={cost:.4f}")


def cmd_table(args: argparse.Namespace) -> None:
    print(
        "| label | task | runs | pass | soft | first valid | latency p50 / max (s) "
        "| out tokens mean | cost |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for label in args.labels:
        recs = [r for out in args.out for r in _records(Path(out), label)]
        for task in ("write", "decide"):
            rows = [r for r in recs if (r["task"] == "write") == (task == "write")]
            if not rows:
                continue
            walls = sorted(r["wall_s"] for r in rows)
            cur = CURRENCY[rows[0]["spec"].partition(":")[0]]
            out_mean = statistics.mean(sum(c.get("out", 0) for c in r["calls"]) for r in rows)
            print(
                f"| {label} | {task} | {len(rows)} | {sum(r['ok'] for r in rows)} | "
                f"{sum(bool(r['soft']) for r in rows)} | {sum(r['first_valid'] for r in rows)} | "
                f"{statistics.median(walls):.0f} / {walls[-1]:.0f} | {out_mean:.0f} | "
                f"{sum(_cost(r) for r in rows):.3f} {cur} |"
            )



# --- Building cases from a mailbox export ---------------------------------------


def _find(directory: Path, prefix: str) -> Path:
    matches = sorted(directory.glob(f"{prefix}*.json"))
    if len(matches) != 1:
        raise SystemExit(f"{prefix!r} matches {len(matches)} files in {directory}")
    return matches[0]


def _document_json(doc: ExtractedDocument) -> dict[str, Any]:
    return {
        "filename": doc.filename, "content_type": doc.content_type, "page_count": doc.page_count,
        "method": doc.method, "text": doc.text,
        "barcodes": [
            {
                "index": b.index, "format": b.format, "text": b.text,
                "payload_b64": base64.b64encode(b.payload).decode("ascii"),
            }
            for b in doc.barcodes
        ],
    }


def _read_mail(source: Path, mail_ref: dict[str, Any], limits: Any) -> dict[str, Any]:
    """One mail as the pipeline reads it: the prepared body and every
    attachment through the extractor."""
    from mail_verdict.orders.content import prepare_body
    from mail_verdict.orders.extract.reader import extract_bytes

    message = json.loads((source / "msgs" / f"{mail_ref['message_id']}.json").read_text())
    body = prepare_body(body_text=message.get("body_text"), body_html=message.get("body_html"))
    docs = []
    for att in message.get("attachments", []):
        found = sorted((source / "att").glob(f"{message['id'][:8]}_{att['id'][:8]}.*"))
        if not found:
            continue
        docs.append(_document_json(
            extract_bytes(
                found[0].read_bytes(), att["content_type"] or "", att["filename"] or "", limits,
            ),
        ))
    return {
        "received_at": mail_ref["received_at"], "from_addr": message["from_addr"] or "",
        "to_addrs": "", "subject": message["subject"] or "", "body": body.text,
        "documents": docs, "_raw": body.raw + " ".join(d["text"] for d in docs),
    }


def _candidate_json(order: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any]:
    """An order as the decide call lists it, built from the mails it held
    before the new one. Only identifiers its remaining mails name are kept,
    so the new mail cannot be the source of the number that matches it."""
    held = scorer.normalize(" ".join(m["_raw"] for m in others))
    numbers = [
        [i["kind"], i["value"]] for i in order.get("identifiers", [])
        if scorer.normalize(i["value"]) in held
    ]
    return {
        "merchant": order["merchant"], "subject": order["subject"], "status": order["status"],
        "is_open": bool(order["is_open"]), "summary": order["summary"] or "",
        "mail_count": len(others),
        "first_mail_at": others[0]["received_at"] if others else None,
        "last_mail_at": others[-1]["received_at"] if others else None,
        "numbers": numbers, "reasons": ["recent activity"],
        "latest_mails": [
            {"received_at": m["received_at"], "from_addr": m["from_addr"], "subject": m["subject"]}
            for m in others[-3:]
        ],
    }


def cmd_build(args: argparse.Namespace) -> None:
    from mail_verdict.config import get_config

    source = Path(args.source)
    limits = get_config().extraction
    manifest = yaml.safe_load(Path(args.manifest).read_text())
    out = Path(args.cases_dir)
    out.mkdir(parents=True, exist_ok=True)
    mail_cache: dict[str, dict[str, Any]] = {}

    def order_mails(order: dict[str, Any]) -> list[dict[str, Any]]:
        mails = sorted(order["mails"], key=lambda m: m["received_at"])
        result = []
        for ref in mails:
            if ref["message_id"] not in mail_cache:
                mail_cache[ref["message_id"]] = _read_mail(source, ref, limits)
            result.append(mail_cache[ref["message_id"]])
        return result

    def strip(mail: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in mail.items() if k != "_raw"}

    for entry in manifest["cases"]:
        order = json.loads(_find(source / "orders", entry["order"]).read_text())
        mails = order_mails(order)
        case: dict[str, Any] = {"name": entry["name"], "task": entry["task"]}
        if entry["task"] == "write":
            case["language"] = entry.get("language", SETTING_DEFAULTS["orders"]["language"])
            case["current_text"] = (
                {k: order[k] or "" for k in ("merchant", "subject", "status", "summary")}
                if entry.get("current_text") else None
            )
            case["mails"] = [strip(m) for m in mails]
        else:
            index = entry.get("mail_index", len(mails) - 1)
            new = mails[index]
            if entry["task"] == "file":
                case["file"] = {"added_at": new["received_at"], "documents": new["documents"]}
            else:
                case["mail"] = strip(new)
            candidates = []
            for cand in entry["candidates"]:
                c_order = order if cand["order"] == entry["order"] else json.loads(
                    _find(source / "orders", cand["order"]).read_text(),
                )
                c_mails = mails if c_order is order else order_mails(c_order)
                if c_order is order and not cand.get("all_mails"):
                    c_mails = [m for i, m in enumerate(mails) if i < index]
                candidates.append(_candidate_json(c_order, c_mails))
            case["candidates"] = candidates
        (out / f"{case['name']}.json").write_text(json.dumps(case, indent=1, ensure_ascii=False))
        print(f"built {case['name']}")


# --- Entry point --------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cases-dir", default=str(DEFAULT_CASES))
        p.add_argument("--expect", default=str(DEFAULT_EXPECT))
        p.add_argument("--out", required=True, help="directory holding one subdirectory per label")

    run = sub.add_parser("run")
    common(run)
    run.add_argument("--label", required=True)
    run.add_argument("--model", required=True, help="provider:model")
    run.add_argument("--effort", default="", help="reasoning effort; empty sends none")
    run.add_argument("--max-tokens", type=int, default=None)
    run.add_argument(
        "--timeout", type=float, default=SETTING_DEFAULTS["orders"]["call_timeout_seconds"],
    )
    run.add_argument("--attempts", type=int, default=2)
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--jobs", type=int, default=1)
    run.add_argument("--cases", default="", help="comma-separated case names; default all")
    run.add_argument("--tasks", default="", help="comma-separated: write, decide, file")
    run.add_argument("--base-url", default=None)

    rescore = sub.add_parser("rescore")
    common(rescore)
    rescore.add_argument("--label", required=True)

    judge = sub.add_parser("judge")
    common(judge)
    judge.add_argument("--label-a", required=True)
    judge.add_argument("--label-b", required=True)
    judge.add_argument("--model", default="anthropic:claude-opus-5-5")
    judge.add_argument("--effort", default="medium")
    judge.add_argument("--cases", default="", help="comma-separated case names; default all")

    table = sub.add_parser("table")
    table.add_argument("--out", action="append", required=True, help="repeatable")
    table.add_argument("labels", nargs="+")

    tally = sub.add_parser("tally")
    tally.add_argument("--out", required=True)
    tally.add_argument("labels", nargs="+")

    build = sub.add_parser("build")
    build.add_argument("--from", dest="source", required=True)
    build.add_argument("--manifest", required=True)
    build.add_argument("--cases-dir", required=True)

    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(cmd_run(args))
    elif args.command == "judge":
        asyncio.run(cmd_judge(args))
    elif args.command == "rescore":
        cmd_rescore(args)
    elif args.command == "table":
        cmd_table(args)
    elif args.command == "tally":
        cmd_tally(args)
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
