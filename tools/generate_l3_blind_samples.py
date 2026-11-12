"""V1: paired L2/L3 generation; explicit --live authorizes exactly 20 paid attempts.

Synthetic aggregates only. No retries, eval_door, persistence, token output or
provider fallback. Rejects never enter participant packets. Offline mock output
cannot pass the release gate. All claims still use production builder gates.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import llm_router
from llm_review import _http_post, _json
import ride_brain as brain
from memory_service import SummarySession

CORPUS = ROOT / 'corpus/l3_blind_v1.json'
BASELINE = ROOT / 'corpus/l2_review_prompt_20261112.txt'


def load_cases(path=CORPUS):
    value = _json(Path(path).read_text(encoding='utf-8'))
    cases = value['cases']
    if len(cases) != 10 or len({c['case_id'] for c in cases}) != 10:
        raise ValueError('Exactly ten distinct cases required')
    for case in cases:
        if case['data_origin'] != 'synthetic_fixture_not_user_measurement':
            raise ValueError('Only explicitly synthetic fixtures allowed')
        session_for(case)
        brain._summary(case['previous_summary'])
    return cases


def session_for(case):
    start = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
    end = start + timedelta(seconds=case['summary']['duration_s'])
    return SummarySession({'id': case['case_id'], 'started_at': start.isoformat(),
                           'ended_at': end.isoformat(), 'summary': case['summary'],
                           'source': 'simulated', 'activity': 'ride', 'coach_notes': []})


def generate(cases, *, api_key, endpoint, model='qwen-max', post=None,
             live=False, checkpoint=None):
    if model != 'qwen-max' or len(cases) != 10:
        raise ValueError('Ten cases and approved qwen-max required')
    baseline = BASELINE.read_text(encoding='utf-8')
    report = {'schema': 'mark-blind-generation/1', 'date': '2026-11-12',
              'data_origin': 'synthetic_fixture_not_user_measurement',
              'real_model_calls': 0, 'model': model, 'production_chain_passed': False,
              'blind_ready': False, 'human_evaluation': 'not_started',
              'source': 'live_model' if live else 'offline_mock',
              'baseline_prompt_sha256': hashlib.sha256(baseline.encode()).hexdigest(),
              'l3_prompt_sha256': hashlib.sha256(brain.SYSTEM_PROMPT.encode()).hexdigest(),
              'cases': []}
    for case in cases:
        row = {'case_id': case['case_id'], 'data': case, 'variants': {}}
        report['cases'].append(row)
        for variant in ('A', 'B'):
            candidate = None
            def capture(request, *, timeout):
                nonlocal candidate
                response = (_http_post if post is None else post)(request, timeout=timeout)
                try:
                    message = _json(response[2])['choices'][0]['message']['content']
                    candidate = _json(message).get('speech')
                except (ValueError, TypeError, KeyError, IndexError):
                    pass
                return response
            prompt = baseline if variant == 'A' else brain.SYSTEM_PROMPT
            start = time.monotonic()
            with patch.object(brain, 'SYSTEM_PROMPT', prompt):
                result = brain.build_ride_review(session_for(case), case['previous_summary'],
                    recent_summaries=case.get('recent_summaries'), api_key=api_key,
                    endpoint=endpoint, model=model, http_post=capture)
            if live:
                report['real_model_calls'] += 1
            speech = result['draft']['speech'] if result['llm_status'] == 'ok' else None
            style_ok = bool(speech and (not brain._FORBIDDEN.search(speech)
                        and not any(cue in speech for cue in brain.INFERENCE_CUES[:4]) if variant == 'A'
                        else any(cue in speech for cue in brain.INFERENCE_CUES[:4])))
            row['variants'][variant] = {'speech': speech, 'llm_status': result['llm_status'],
                'style_passed': style_ok, 'candidate_text': candidate,
                'latency_ms': round((time.monotonic()-start)*1000), 'usage': result['usage']}
            if checkpoint:
                checkpoint(report)
    variants = [v for row in report['cases'] for v in row['variants'].values()]
    report['production_chain_passed'] = len(variants) == 20 and all(v['llm_status'] == 'ok' for v in variants)
    report['blind_ready'] = bool(live and report['real_model_calls'] == 20
                                and report['production_chain_passed']
                                and all(v['style_passed'] for v in variants))
    if checkpoint:
        checkpoint(report)
    return report


def write_packets(report, output, *, seed=None):
    if not report['blind_ready'] or report['source'] != 'live_model':
        raise ValueError('No participant packets before complete live validation')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    key = []
    for person in range(1, 6):
        cases = list(report['cases']); rng.shuffle(cases)
        rows = []
        for index, case in enumerate(cases, 1):
            variants = ['A', 'B']; rng.shuffle(variants)
            pair = f'P{person}-{index:02d}'
            rows.append({'pair_id': pair,
                'slot1_text': case['variants'][variants[0]]['speech'],
                'slot2_text': case['variants'][variants[1]]['speech'],
                'companionship_1': '', 'companionship_2': '', 'continue_choice': '',
                'understands_1': '', 'understands_2': '', 'coach_1': '', 'coach_2': '',
                'notes': ''})
            key.append({'reviewer_id': f'P{person}', 'pair_id': pair,
                        'case_id': case['case_id'], 'slot1': variants[0], 'slot2': variants[1]})
        with (output/f'骑行者P{person}_评分表.csv').open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)
    (output/'主持人密钥_不要发给受试者.json').write_text(json.dumps(key, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--packets', type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error('Use unit tests for offline checks; --live required for sample generation')
    if args.output.exists() or (args.packets and args.packets.exists()):
        parser.error('Refusing to overwrite existing evidence')
    plans = llm_router.plan('review')
    cfg = next((item for item in plans if item['provider'] == 'dashscope' and item['model'] == 'qwen-max'), None)
    if cfg is None:
        parser.error('Approved qwen-max configuration unavailable; no calls made')
    cases = load_cases()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save(report):
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    report = generate(cases, **{k: cfg[k] for k in ('api_key', 'endpoint', 'model')}, live=True, checkpoint=save)
    if args.packets and report['blind_ready']:
        write_packets(report, args.packets)
    print(json.dumps({k: report[k] for k in ('real_model_calls', 'production_chain_passed', 'blind_ready', 'human_evaluation')}))
    return 0 if report['blind_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
