"""Model bakeoff: every candidate brain runs the golden door plus open cases.

中文摘要：同一套金题与开放问题，逐模型真打上游；金题按门禁判分（车道/核验/
接受词），开放问题只记录原文供人工对比。输出 JSON 明细 + 供汇报的对比样本。
不是门禁：不拦截部署，只产生证据。key 只从环境变量读取，绝不落盘。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import llm_router
from ride_brain import _FORBIDDEN
from tools.eval_door import build_app, judge_case, load_corpus, validate_corpus

DEFAULT_CORPUS = ROOT / "corpus" / "eval_door_v1.json"
DEFAULT_OPEN_CORPUS = ROOT / "corpus" / "bakeoff_open_v1.json"

# Provider specs: "name" or "name:model". Keys come from <NAME>_API_KEY env;
# endpoints/models default from llm_router.REGISTRY. Unconfigured providers
# are skipped with an honest note — never silently dropped from the report.
def parse_specs(text):
    specs = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, model = item.partition(":")
        name = name.strip().lower()
        if name not in llm_router.REGISTRY or name == "backup":
            raise ValueError(f"unknown provider in --providers: {name}")
        specs.append((name, model.strip() or None))
    return specs


def _scoped_env(name, model):
    """Env where only this provider is in the pool, models pinned if given."""
    prefix = name.upper()
    env = {}
    key = os.environ.get(prefix + "_API_KEY")
    if key:
        env[prefix + "_API_KEY"] = key
    if model:
        env[prefix + "_MODEL_FAST"] = model
        env[prefix + "_MODEL_SMART"] = model
    env["LLM_FAST_ORDER"] = name
    env["LLM_SMART_ORDER"] = name
    return env


def run_model(name, model, door_cases, open_cases, *, delay_s=0.3):
    """Run one candidate across door + open cases with the real upstream."""
    label = f"{name}:{model}" if model else name
    original = {}
    scoped = _scoped_env(name, model)
    for key, value in scoped.items():
        original[key] = os.environ.get(key)
        os.environ[key] = value
    rows = {"door": [], "open": []}
    try:
        with tempfile.TemporaryDirectory(prefix="bakeoff-") as tmp_root:
            app = build_app(door_cases, tmp_root, real_transport=True)
            for case in door_cases:
                started = time.monotonic()
                result = app._chat_ride(_payload(case))
                elapsed = time.monotonic() - started
                row = judge_case(case, result)
                row["seconds"] = round(elapsed, 2)
                rows["door"].append(row)
                time.sleep(delay_s)
            for case in open_cases:
                started = time.monotonic()
                result = app._chat_ride(_payload(case))
                elapsed = time.monotonic() - started
                speech = result.get("speech") or ""
                row = {"id": case["id"], "lane": result.get("lane"),
                       "status": result["speech_status"], "speech": speech,
                       "seconds": round(elapsed, 2)}
                hits = sorted(set(_FORBIDDEN.findall(speech)))
                custom = [part for part in case.get("reject_any", []) if part in speech]
                row["redline_hits"] = hits + custom
                rows["open"].append(row)
                time.sleep(delay_s)
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return label, rows

# Open cases live in a versioned corpus file so every bakeoff run is
# reproducible and diffs are reviewable. Each carries red-line probes
# (reject_any scanned against _FORBIDDEN + custom); speech text is the evidence.
OPEN_REQUIRED = {"id", "question", "note"}
OPEN_OPTIONAL = {"reject_any", "live_id", "weather", "nav"}


def load_open_corpus(path):
    """Load and validate the open-question corpus; return the original list."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        corpus = json.loads(text)
        if type(corpus) is not list or not corpus:
            raise ValueError("open corpus must be a non-empty list")
        seen = set()
        for index, case in enumerate(corpus):
            prefix = f"open[{index}]"
            if type(case) is not dict:
                raise ValueError(f"{prefix}: case must be an object")
            if not OPEN_REQUIRED <= case.keys():
                raise ValueError(f"{prefix}: missing required fields")
            if case.keys() - OPEN_REQUIRED - OPEN_OPTIONAL:
                raise ValueError(f"{prefix}: unknown case fields")
            if (type(case["id"]) is not str
                    or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", case["id"])):
                raise ValueError(f"{prefix}: id must be a kebab-case string")
            if case["id"] in seen:
                raise ValueError(f"{prefix}: duplicate id {case['id']}")
            seen.add(case["id"])
            if (type(case["question"]) is not str
                    or not 1 <= len(case["question"]) <= 200
                    or any(ord(c) < 32 or ord(c) == 127 for c in case["question"])):
                raise ValueError(f"{prefix}: question must be 1..200 printable characters")
            if type(case["note"]) is not str or not case["note"].strip():
                raise ValueError(f"{prefix}: note must be a non-empty string")
            if "reject_any" in case:
                values = case["reject_any"]
                if (type(values) is not list
                        or any(type(v) is not str or not v for v in values)):
                    raise ValueError(f"{prefix}: reject_any must be a list of non-empty strings")
        return corpus
    except (OSError, ValueError, TypeError) as error:
        print(f"Invalid open corpus: {error}", file=sys.stderr)
        raise SystemExit(2) from error


def _payload(case):
    payload = {"question": case["question"]}
    for key in ("live_id", "weather", "nav"):
        if key in case:
            payload[key] = case[key]
    return payload


def summarize(model, rows):
    door = rows["door"]
    passed = sum(row["passed"] for row in door)
    latencies = [row["seconds"] for row in door]
    redline = sum(1 for row in rows["open"] if row["redline_hits"])
    return {"model": model, "door_pass": passed, "door_total": len(door),
            "door_pass_rate": round(passed / len(door), 3) if door else 0,
            "avg_seconds": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "open_redline_rows": redline}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--open-corpus", default=str(DEFAULT_OPEN_CORPUS))
    parser.add_argument("--providers", default="dashscope",
                        help="comma list of provider specs, e.g. dashscope:qwen-max,deepseek")
    parser.add_argument("--out", default=None, help="write JSON report here")
    args = parser.parse_args(argv)
    door_cases = load_corpus(args.corpus)
    validate_corpus(door_cases)
    open_cases = load_open_corpus(args.open_corpus)
    report = {"kind": "model-bakeoff/2", "models": {}, "skipped": [], "summary": []}
    for name, model in parse_specs(args.providers):
        if not os.environ.get(name.upper() + "_API_KEY"):
            note = f"{name}: key 未配置（{name.upper()}_API_KEY），跳过"
            report["skipped"].append(note)
            print(note, flush=True)
            continue
        print(f"=== {name}{':' + model if model else ''} ===", flush=True)
        try:
            label, rows = run_model(name, model, door_cases, open_cases)
        except Exception as error:  # evidence, not a gate: record and continue
            report["models"][name] = {"error": repr(error)}
            print(f"{name}: ERROR {error!r}", flush=True)
            continue
        report["models"][label] = rows
        summary = summarize(label, rows)
        report["summary"].append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
