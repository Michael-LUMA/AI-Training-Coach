"""M0-only L3 bakeoff: offline unittest discovery; explicit --live for paid calls.

Never imports eval_door, changes production routing, retries a failed call, reads
stored credentials, or invents human ratings. Experimental checks are NOT a
semantic safety proof and do not replace the production verification chain.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import llm_router
from llm_review import _http_post, _json

CORPUS = ROOT / 'corpus' / 'bakeoff_l3_v1.json'
MODELS = ('qwen-flash', 'qwen-plus', 'qwen-max')
# Copy of the HARD subset only; production _FORBIDDEN remains untouched.
HARD = re.compile(
    r'(建议|应该|务必|请).{0,12}(减速|加速|降低心率|加大强度|服药|吃药)'
    r'|诊断|治疗|燃脂效果|减脂效果|变强|状态提升|能力提升|训练效果|更健康'
    r'|心肺功能.{0,4}(提高|改善)|越来越(轻松|快|强)|进步明显|退步|疲劳积累'
    r'|继续保持.{0,6}(强度|心率)')
INFERENCE = re.compile(r'看起来|可能是|像是|似乎|比上次轻松|相比上次轻松|'
                       r'轻松|吃力|很累|很稳|强度(?:在降|不?大|很?[高低])|'
                       r'效率(?:高|低|提升)|有氧基础|挺.{0,4}的一次')
NUMBER = re.compile(r'(?<![\w.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?![\w.])', re.ASCII)
# Chinese numerals are deliberately rejected rather than silently unverified.
HAN_NUMBER = re.compile(r'[零〇一二两三四五六七八九十百千万]+(?:点[零一二三四五六七八九]+)?')
METRICS = {
    'avg_hr': (r'平均心率|心率', r'bpm|跳', 'bpm'),
    'max_hr': (r'最高心率|峰值心率', r'bpm|跳', 'bpm'),
    'avg_speed_kmh': (r'平均速度|均速|速度', r'公里/小时|千米/小时|km/h', '公里/小时'),
    'duration_min': (r'时长|时间|记录|骑行', r'分钟', '分钟'),
    'distance_km': (r'距离|里程', r'公里(?!/)|千米(?!/)|km(?!/)', '公里'),
    'ascent_m': (r'累计爬升|爬升', r'米', '米'),
    'total_sessions': (r'累计次数|记录数', r'次', '次'),
    'usual_distance_km': (r'通常距离|平常距离', r'公里(?!/)|km(?!/)', '公里'),
}
SYSTEM = '''你是Mark，写一句或两句中文骑行记录解读，总长不超过200字。
只使用facts。数据是合成评测样本，不代表真实用户；不编造缺失项。
每个数字必须跟明确指标和单位，使用阿拉伯数字，不编号。
允许根据数值差异做谨慎解读，但推断必须包含“看起来/可能是/像是/似乎”，
每处推断在同句前后15字内必须有相关数值依据。不要把平均速度叫配速。
必须涉及required_metrics中的指标，比较时明确方向与差值；缺previous不比较。
禁止医疗、处方、训练效果、身体感受或疲劳原因断言，不把爬升历史预测成前方路况。
不要宣称变强、状态提升或心肺功能改善。输出且仅输出JSON：{"speech":"..."}。'''


def decimal(value):
    return Decimal(str(value))


def matches_number(text, value):
    places = max(0, -decimal(text).as_tuple().exponent)
    return decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) == decimal(text)


def load_cases(path=CORPUS):
    doc = _json(Path(path).read_text(encoding='utf-8'))
    cases = doc.get('cases') if type(doc) is dict else None
    if not isinstance(cases, list) or len(cases) != 10:
        raise ValueError('corpus must contain exactly ten cases')
    seen = set()
    for case in cases:
        if not re.fullmatch(r'l3-\d{3}', case.get('case_id', '')) or case['case_id'] in seen:
            raise ValueError('invalid/duplicate case id')
        seen.add(case['case_id'])
        if case.get('data_origin') != 'synthetic_fixture_not_user_measurement':
            raise ValueError('fixture origin must be explicit')
        data = case.get('data')
        if not isinstance(data, dict) or set(data) != {'current', 'previous', 'profile'}:
            raise ValueError('invalid case data')
        for scope, values in data.items():
            if values is None and scope != 'current':
                continue
            if not isinstance(values, dict) or not values or set(values) - METRICS.keys():
                raise ValueError('invalid metric map')
            for value in values.values():
                if value is not None and (type(value) not in (int, float)
                        or not decimal(value).is_finite() or value < 0):
                    raise ValueError('invalid numeric metric')
        required = case.get('required_metrics')
        if not isinstance(required, list) or not required or any(m not in METRICS for m in required):
            raise ValueError('invalid required metrics')
        if not isinstance(case.get('expected_inference'), str) or not case['expected_inference']:
            raise ValueError('missing reference speech')
    return cases


def make_facts(case):
    data = case['data']
    differences = {}
    for metric, current in data['current'].items():
        previous = (data['previous'] or {}).get(metric)
        if current is not None and previous is not None:
            differences[metric] = str(decimal(current) - decimal(previous))
    return {**data, 'differences': differences, 'required_metrics': case['required_metrics']}


def numeric_claim(text, match, facts):
    """Fail closed on unsupported units, wrong dimensions or comparison direction."""
    prefix = text[max(0, match.start()-30):match.start()]
    # Avoid crossing a clause to attach a number to an unrelated metric.
    prefix = re.split(r'[，,；;。！？\n]', prefix)[-1]
    suffix = text[match.end():match.end()+10].lstrip()
    mentions = []
    for metric, (label, unit, _) in METRICS.items():
        for mention in re.finditer(label, prefix):
            mentions.append((mention.start(), len(mention.group()), metric, unit))
    if not mentions:
        return None
    _, _, metric, unit = max(mentions, key=lambda item: (item[0]+item[1], item[1]))
    # An explicit but wrong unit cannot be rescued by a nearby metric label.
    unit_like = re.match(r'(公里/小时|千米/小时|km/h|bpm|分钟|小时|秒|公里|千米|米|跳|次|[a-zA-Z]+)', suffix)
    if unit_like and not re.fullmatch(unit, unit_like.group(), re.I):
        return None
    label = max(re.finditer(METRICS[metric][0], prefix), key=lambda m: m.end())
    between = prefix[label.end():]
    scope = 'previous' if re.search(r'(上次|上回)(?:的)?$', prefix[:label.start()]) else 'current'
    if metric in ('total_sessions', 'usual_distance_km'):
        scope = 'profile'
    value = (facts[scope] or {}).get(metric)
    delta = facts['differences'].get(metric)
    decreasing = bool(re.search(r'低(?:了)?|慢(?:了)?|少(?:了)?|下降|减少', between))
    increasing = bool(re.search(r'高(?:了)?|快(?:了)?|多(?:了)?|上升|增加', between))
    if decreasing or increasing:
        if delta is None or increasing and decreasing:
            return None
        signed = decimal(delta)
        if decreasing and signed >= 0 or increasing and signed <= 0:
            return None
        value = abs(signed)
    elif re.search(r'差(?:了)?|相差', between):
        value = abs(decimal(delta)) if delta is not None else None
    return metric if value is not None and matches_number(match.group(), value) else None


def judge(case, speech):
    reasons = []
    if type(speech) is not str or not speech.strip() or len(speech) > 200 or any(ord(c)<32 and c!='\n' for c in speech):
        return {'passed': False, 'reject_reasons': ['invalid_speech'], 'verified_metrics': []}
    if HARD.search(speech):
        reasons.append('hard_forbidden')
    # Strip nonnumeric fixed expressions before the conservative Han-number gate.
    han_text = speech
    for idiom in ('上一次', '这一次', '的一次', '一起', '一样', '一致', '一回', '这回'):
        han_text = han_text.replace(idiom, '')
    if HAN_NUMBER.search(han_text):
        reasons.append('unsupported_han_number')
    facts = make_facts(case)
    if facts['previous'] is None and re.search(r'(比|相比)上次|比上回', speech):
        reasons.append('missing_previous')
    verified = set()
    for sentence in re.split(r'[。！？；;\n]', speech):
        numbers = list(NUMBER.finditer(sentence))
        good = []
        for number in numbers:
            metric = numeric_claim(sentence, number, facts)
            if metric is None:
                reasons.append('unverified_number')
            else:
                good.append(number); verified.add(metric)
        for inference in INFERENCE.finditer(sentence):
            if not any(n.end() >= inference.start()-15 and n.start() <= inference.end()+15 for n in good):
                reasons.append('unsupported_inference')
    if not re.search(r'看起来|可能是|像是|似乎', speech):
        reasons.append('missing_uncertainty_marker')
    if set(case['required_metrics']) - verified:
        reasons.append('missing_required_metrics')
    return {'passed': not reasons, 'reject_reasons': sorted(set(reasons)), 'verified_metrics': sorted(verified)}


def generate(case, *, endpoint, model, api_key, post=None):
    """One bounded upstream request. Credentials and error bodies never enter reports."""
    if api_key is None:
        return {'llm_status': 'not_configured', 'speech': None}
    payload = {'model': model, 'temperature': 0, 'max_tokens': 400,
               'messages': [{'role': 'system', 'content': SYSTEM},
                            {'role': 'user', 'content': json.dumps(make_facts(case), ensure_ascii=False)}]}
    request = Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                      headers={'Authorization': 'Bearer '+api_key, 'Content-Type': 'application/json'}, method='POST')
    try:
        status, _, body = (post or _http_post)(request, timeout=20)
        if status != 200:
            return {'llm_status': 'upstream_error', 'speech': None}
        envelope = _json(body)
        if type(envelope) is not dict or not isinstance(envelope.get('choices'), list) or not envelope['choices']:
            raise ValueError('invalid response')
        draft = _json(envelope['choices'][0]['message']['content'])
        if type(draft) is not dict or set(draft) != {'speech'} or type(draft['speech']) is not str:
            raise ValueError('invalid draft')
        return {'llm_status': 'ok', 'speech': draft['speech']}
    except Exception:
        # Provider errors may contain credentials; never stringify them.
        return {'llm_status': 'upstream_error', 'speech': None}


def make_report(cases, *, live=False, api_key=None, post=None):
    if live and not llm_router._check_key(api_key):
        status = 'blocked_missing_api_key'
    else:
        status = 'generated_pending_human_review' if live else 'offline_preflight_only'
    actual = live and status != 'blocked_missing_api_key'
    models = []
    for model in MODELS:
        rows = []
        for case in cases:
            row = {'case_id': case['case_id'], 'quality_score': None}
            if actual:
                # Pin both lanes and the sole provider via explicit env mapping. No env mutation/failover.
                env = {'DASHSCOPE_API_KEY': api_key, 'DASHSCOPE_MODEL_FAST': model,
                       'DASHSCOPE_MODEL_SMART': model, 'LLM_FAST_ORDER': 'dashscope', 'LLM_SMART_ORDER': 'dashscope'}
                start = time.monotonic()
                result = llm_router.run('review', lambda **kw: generate(case, post=post, **kw), environ=env)
                row.update(result, seconds=round(time.monotonic()-start, 3))
                row.update(judge(case, result['speech']) if result['llm_status']=='ok' else
                           {'passed': False, 'reject_reasons': ['upstream_error'], 'verified_metrics': []})
            else:
                row.update(llm_status='not_run', speech=None, passed=None, reject_reasons=[])
            rows.append(row)
        completed = sum(r['llm_status']=='ok' for r in rows)
        models.append({'model': model, 'attempted': len(rows) if actual else 0, 'completed': completed,
                       'pass_rate': sum(r['passed'] is True for r in rows)/10 if actual else None,
                       'reject_reasons': dict(Counter(reason for r in rows for reason in r['reject_reasons'])),
                       'quality_scores': [None]*10, 'quality_mean': None, 'rows': rows})
    if actual and any(model['completed'] != 10 for model in models):
        status = 'generated_incomplete'
    return {'schema_version': 'mark-l3-bakeoff-report-v1', 'status': status,
            'corpus_sha256': hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'data_origin': 'synthetic_fixture_not_user_measurement', 'real_model_calls': 30 if actual else 0,
            'production_changed': False, 'human_review_complete': False, 'm0_accepted': False,
            'verifier_limitation': 'Experimental numeric/local-window checks are not semantic proof; human review required.',
            'models': models}


def ratings_template(report):
    """Blank scores only; bind each entry to its real response for human review."""
    if not report['real_model_calls'] or any(m['completed'] != 10 for m in report['models']):
        raise ValueError('thirty real responses required before human review')
    return [{'model': m['model'], 'case_id': r['case_id'], 'score': None,
             'speech_sha256': hashlib.sha256(r['speech'].encode()).hexdigest()}
            for m in report['models'] for r in m['rows']]


def apply_ratings(report, ratings):
    """Ratings are bound to exact generated speech hashes, never offline examples."""
    required = {(model['model'], row['case_id']): row for model in report['models'] for row in model['rows']}
    if not report['real_model_calls'] or any(r['llm_status'] != 'ok' for r in required.values()):
        raise ValueError('thirty real responses required before human review')
    if type(ratings) is not list or len(ratings) != 30:
        raise ValueError('thirty human ratings required')
    seen = set()
    for rating in ratings:
        key = (rating.get('model'), rating.get('case_id'))
        score = rating.get('score')
        if key not in required or key in seen or type(score) is not int or not 1 <= score <= 5:
            raise ValueError('invalid or duplicate rating')
        row = required[key]
        if rating.get('speech_sha256') != hashlib.sha256(row['speech'].encode()).hexdigest():
            raise ValueError('rating does not match generated speech')
        seen.add(key)
    for rating in ratings:
        required[(rating['model'], rating['case_id'])]['quality_score'] = rating['score']
    for model in report['models']:
        model['quality_scores'] = [r['quality_score'] for r in model['rows']]
        model['quality_mean'] = sum(model['quality_scores'])/10
    plus = next(m for m in report['models'] if m['model']=='qwen-plus')
    report['human_review_complete'] = True
    report['m0_accepted'] = plus['pass_rate'] >= .8 and plus['quality_mean'] >= 3.5
    report['status'] = 'awaiting_boss_model_decision' if report['m0_accepted'] else 'NEEDS DECISION'
    return report


class BakeoffL3Tests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases()
        self.case = self.cases[0]

    def test_reference_examples_pass(self):
        for case in self.cases:
            with self.subTest(case=case['case_id']):
                self.assertEqual(judge(case, case['expected_inference'])['reject_reasons'], [])

    def test_hard_rules(self):
        for phrase in ('你变强了', '状态提升', '能力提升', '训练效果', '更健康', '心肺功能改善',
                       '请加速', '建议降低心率', '继续保持心率', '燃脂效果', '减脂效果', '诊断', '治疗',
                       '越来越快', '进步明显', '退步', '疲劳积累'):
            with self.subTest(phrase=phrase):
                self.assertIn('hard_forbidden', judge(self.case, self.case['expected_inference']+phrase)['reject_reasons'])

    def test_unsupported_inference(self):
        for suffix in ('看起来状态很好', '可能是累了', '似乎很轻松', '像是很吃力'):
            self.assertIn('unsupported_inference', judge(self.case, self.case['expected_inference']+'。'+suffix)['reject_reasons'])

    def test_numeric_and_direction_failures(self):
        for old, new in (('低了4', '低了5'), ('低了4', '高了4'), ('慢了1.2', '快了1.2'),
                         ('4 bpm', '4公里'), ('1.2公里/小时', '1.2 bpm')):
            with self.subTest(new=new):
                self.assertIn('unverified_number', judge(self.case, self.case['expected_inference'].replace(old,new))['reject_reasons'])

    def test_wrong_dimension_same_value(self):
        case = json.loads(json.dumps(self.case))
        case['data']['current']['duration_min'] = 999
        self.assertIn('unverified_number', judge(case, '平均心率999 bpm，均速22.5公里/小时，看起来数值接近。')['reject_reasons'])

    def test_current_and_previous_cannot_be_swapped(self):
        valid = '上次平均心率135 bpm，本次平均心率131 bpm，均速22.5公里/小时，看起来数值有差异。'
        self.assertTrue(judge(self.case, valid)['passed'])
        for speech in (valid.replace('上次平均心率135', '上次平均心率131'),
                       valid.replace('本次平均心率131', '本次平均心率135')):
            self.assertIn('unverified_number', judge(self.case, speech)['reject_reasons'])

    def test_no_comparison_without_previous(self):
        self.assertIn('missing_previous', judge(self.cases[-1], '比上次心率低了4 bpm，看起来强度在降。')['reject_reasons'])

    def test_unknown_and_han_numbers_rejected(self):
        self.assertIn('unverified_number', judge(self.case, self.case['expected_inference']+'。123。')['reject_reasons'])
        self.assertIn('unsupported_han_number', judge(self.case, self.case['expected_inference'].replace('4 bpm','四 bpm'))['reject_reasons'])

    def test_required_metrics_and_marker(self):
        self.assertIn('missing_required_metrics', judge(self.case, '心率低了4 bpm，看起来数值在降。')['reject_reasons'])
        self.assertIn('missing_uncertainty_marker', judge(self.case, '心率低了4 bpm，均速慢了1.2公里/小时。')['reject_reasons'])

    def test_rounding_half_up(self):
        self.assertTrue(matches_number('1.3', '1.25'))
        self.assertFalse(matches_number('1.2', '1.25'))
        self.assertFalse(matches_number('4', '4.6'))

    def test_corpus_rejects_duplicate_or_bad_numbers(self):
        import tempfile
        for mutation in ('duplicate', 'nan', 'bool', 'missing-origin'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                cases = json.loads(json.dumps(self.cases))
                if mutation=='duplicate': cases[-1]['case_id']=cases[0]['case_id']
                if mutation=='nan': cases[0]['data']['current']['avg_hr']=float('nan')
                if mutation=='bool': cases[0]['data']['current']['avg_hr']=True
                if mutation=='missing-origin': cases[0].pop('data_origin')
                path=Path(tmp)/'cases.json';path.write_text(json.dumps({'cases':cases}),encoding='utf-8')
                with self.assertRaises(ValueError): load_cases(path)

    def test_offline_and_missing_key_never_call_network(self):
        for live in (False, True):
            with patch(__name__+'._http_post', side_effect=AssertionError('network')) as post:
                report=make_report(self.cases,live=live)
                self.assertEqual(report['real_model_calls'],0)
                self.assertFalse(report['m0_accepted'])
                self.assertIsNone(report['models'][1]['pass_rate'])
                post.assert_not_called()

    def fake_post(self, request, timeout):
        payload=json.loads(request.data)
        facts=json.loads(payload['messages'][1]['content'])
        case=next(c for c in self.cases if make_facts(c)==facts)
        body={'choices':[{'message':{'content':json.dumps({'speech':case['expected_inference']},ensure_ascii=False)}}]}
        return 200,{},json.dumps(body,ensure_ascii=False).encode()

    def test_thirty_pinned_calls_without_env_mutation(self):
        before=dict(os.environ)
        requests=[]
        def post(request,timeout):
            requests.append(json.loads(request.data)['model'])
            return self.fake_post(request,timeout)
        report=make_report(self.cases,live=True,api_key='synthetic-key',post=post)
        self.assertEqual(Counter(requests),dict.fromkeys(MODELS,10))
        self.assertEqual(dict(os.environ),before)
        self.assertEqual([m['pass_rate'] for m in report['models']],[1,1,1])
        self.assertFalse(report['m0_accepted'])
        self.assertNotIn('synthetic-key',json.dumps(report))

    def test_error_not_retried_and_sensitive_text_suppressed(self):
        calls=[]
        def fail(*args,**kwargs):
            calls.append(1);raise RuntimeError('SECRET-provider-error')
        report=make_report(self.cases,live=True,api_key='SECRET-key',post=fail)
        self.assertEqual(len(calls),30)
        self.assertNotIn('SECRET',json.dumps(report))
        self.assertEqual(report['models'][1]['pass_rate'],0)

    def test_non_200_and_bad_json_fail_closed(self):
        for status,body in ((429,b'SECRET'),(200,b'not-json'),(200,b'{"choices":[]}')):
            result=generate(self.case,endpoint='https://example.invalid/',model='qwen-plus',api_key='test',post=lambda *a,**k:(status,{},body))
            self.assertEqual(result['llm_status'],'upstream_error')
            self.assertNotIn('SECRET',json.dumps(result))

    def test_missing_key_does_not_build_request(self):
        self.assertEqual(generate(self.case,endpoint=None,model=None,api_key=None)['llm_status'],'not_configured')

    def test_ratings_gate_requires_exact_thirty_scores(self):
        report=make_report(self.cases,live=True,api_key='test',post=self.fake_post)
        ratings=[{'model':m['model'],'case_id':r['case_id'],'score':4,
                  'speech_sha256':hashlib.sha256(r['speech'].encode()).hexdigest()}
                 for m in report['models'] for r in m['rows']]
        for bad in ([],ratings[:-1],ratings[:-1]+[ratings[0]], [{**r,'speech_sha256':'wrong'} for r in ratings]):
            with self.assertRaises(ValueError): apply_ratings(report,bad)
        apply_ratings(report,ratings)
        self.assertTrue(report['m0_accepted'])
        self.assertEqual(report['models'][1]['quality_mean'],4)
        self.assertEqual(report['status'],'awaiting_boss_model_decision')

    def test_ratings_template_has_no_invented_scores(self):
        with self.assertRaises(ValueError): ratings_template(make_report(self.cases))
        report = make_report(self.cases, live=True, api_key='test', post=self.fake_post)
        template = ratings_template(report)
        self.assertEqual(len(template), 30)
        self.assertTrue(all(row['score'] is None and len(row['speech_sha256']) == 64 for row in template))
        with self.assertRaises(ValueError): apply_ratings(report, template)

    def test_cannot_score_offline_examples(self):
        with self.assertRaises(ValueError): apply_ratings(make_report(self.cases),[])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Authorize exactly 30 paid upstream attempts; no retries.')
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--score-report',type=Path,help='Read an existing real report; no model calls.')
    parser.add_argument('--ratings',type=Path)
    args=parser.parse_args()
    if args.live and args.score_report:
        parser.error('--live and --score-report cannot be combined')
    if bool(args.score_report)!=bool(args.ratings):
        parser.error('--score-report and --ratings must be supplied together')
    if args.score_report:
        report=apply_ratings(_json(args.score_report.read_text(encoding='utf-8')),
                             _json(args.ratings.read_text(encoding='utf-8')))
    else:
        cases=load_cases()
        report=make_report(cases,live=args.live,api_key=os.environ.get('DASHSCOPE_API_KEY') if args.live else None)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.live and report['status'] == 'generated_pending_human_review':
        template_path = args.report.with_name(args.report.stem+'.ratings-template.json')
        template_path.write_text(json.dumps(ratings_template(report),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'real_model_calls':report['real_model_calls'],'m0_accepted':report['m0_accepted']},ensure_ascii=False))
    return 2 if report['status']=='blocked_missing_api_key' else 0


if __name__=='__main__':
    raise SystemExit(main())
