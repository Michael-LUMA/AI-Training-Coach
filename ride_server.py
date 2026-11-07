"""Isolated loopback ride review service with explicitly authorized ICS downloads.

POST /api/rides/{create,list,get,edit,confirm,authorize,calendar,finish,
feedback,delete,export}. Create content is strict base64 for GPX/TCX or a manual
summary dict. No multipart, cloud, background capture or automatic confirmation.
"""
from __future__ import annotations
import argparse
import base64
import binascii
import hashlib
import json
import math
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from action_cards import ActionError, require, fields, due_time, calendar_file
from ride_gpx import parse_gpx, GPXError, MAX_FILE_BYTES
from ride_tcx import parse_tcx, TCXError
from ride_review import build_review, ReviewError, SCHEMA_VERSION, MAX_TEXT_CHARS

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / '.runtime/ride-review/rides.sqlite3'
MAX_BODY_BYTES = 7 * 1024 * 1024
MAX_RIDES = 100
COMMON = ('schema_version', 'id', 'expected_revision', 'consent')
KEYS = {'list': ('schema_version',), 'get': ('schema_version', 'id'),
        'create': ('schema_version', 'kind', 'content', 'dataset_kind', 'consent'),
        'edit': COMMON + ('values',), 'confirm': COMMON, 'authorize': COMMON + ('scope',),
        'calendar': COMMON, 'finish': COMMON + ('state',), 'feedback': COMMON + ('value',),
        'delete': COMMON, 'export': COMMON}


class RideError(ActionError):
    def __init__(self, code, message, status=400, current_revision=None):
        super().__init__(code, message, status)
        self.current_revision = current_revision


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('nonfinite number')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        # Also detects numeric overflow such as 1e999 and invalid Unicode.
        dumps(value).encode('utf-8')
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise RideError('invalid_json', '需要严格UTF-8 JSON；拒绝重复键及非有限数') from None


def checked_text(value):
    require(type(value) is str and len(value) <= MAX_TEXT_CHARS, '文字格式或长度不符')
    require(not any(0xD800 <= ord(c) <= 0xDFFF or (ord(c) < 32 and c not in '\n\r\t') for c in value), '文字编码不支持')
    return value


def ride_calendar_file(card):
    """Adapt the existing action generator without changing shared action behavior."""
    suggestion = card['training_suggestion']
    start = suggestion['scheduled_start']
    if isinstance(start, str) and start.endswith('Z'):
        start = start[:-1] + '+00:00'
    dt = due_time(start)
    duration = suggestion['duration_s']
    require(dt is not None and type(duration) is int and 0 < duration <= 86400,
            '需要已确认的开始时间和训练时长', 'invalid_time')
    # Stable, ride-namespaced UID; no original route or private source in the ICS.
    content = calendar_file({'id': 'ride-' + card['id'], 'revision': card['revision'] + 1,
                             'due_at': dt.isoformat(), 'action': suggestion['text'],
                             'recipient': '本人', 'source': '骑行复盘规则建议；用户已确认，仅供自行安排，不保证训练效果。'})
    # The shared action generator defaults to five minutes. Only replace its
    # short UTC DTEND property so confirmed ride duration is respected; all
    # escaping, UTF-8 folding and remaining serialization stay with the generator.
    lines = content.split('\r\n')
    require(sum(line.startswith('DTEND:') for line in lines) == 1,
            '日历模板不兼容；未生成文件', 'calendar_template_error', 500)
    end = 'DTEND:' + (dt + timedelta(seconds=duration)).strftime('%Y%m%dT%H%M%SZ')
    return '\r\n'.join(end if line.startswith('DTEND:') else line for line in lines)

class PreviewPage:
    def __init__(self, body, csp):
        self.body, self.csp = body, csp


def render_capture():
    """Local sensor UI; exact inline CSP, same-origin token/API, no external assets."""
    static = ROOT / 'static'
    css = (static / 'ride_capture.css').read_text(encoding='utf-8-sig')
    gpx = (static / 'ride_capture_gpx.js').read_text(encoding='utf-8-sig')
    script = (static / 'ride_capture.js').read_text(encoding='utf-8-sig')
    def digest(text):
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii') + "'"
    csp = ("default-src 'none'; script-src " + digest(gpx) + ' ' + digest(script)
           + '; style-src ' + digest(css)
           + "; connect-src 'self'; img-src 'none'; font-src 'none'; frame-ancestors 'none'; "
             "base-uri 'none'; form-action 'none'; object-src 'none'")
    body = (static / 'ride_capture.html').read_text(encoding='utf-8-sig')
    body = body.replace('/*__STYLE__*/', css).replace('/*__GPX__*/', gpx).replace('/*__SCRIPT__*/', script)
    return PreviewPage(body.encode('utf-8'), csp)

def render_console():
    """Self-contained local UI; session token is fetched same-origin, never embedded."""
    static = ROOT / 'static'
    css = (static / 'ride_console.css').read_text(encoding='utf-8-sig')
    script = (static / 'ride_console.js').read_text(encoding='utf-8-sig')
    def digest(text):
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii') + "'"
    csp = ("default-src 'none'; script-src " + digest(script) + '; style-src ' + digest(css)
           + "; connect-src 'self'; img-src 'none'; font-src 'none'; frame-ancestors 'none'; "
             "base-uri 'none'; form-action 'none'; object-src 'none'")
    body = (static / 'ride_console.html').read_text(encoding='utf-8-sig')
    body = body.replace('/*__STYLE__*/', css).replace('/*__SCRIPT__*/', script)
    return PreviewPage(body.encode('utf-8'), csp)

def render_preview(card):
    require(card['status'] == 'confirmed' and card['confirmation'] is not None,
            'HUD预览仅供已确认记录；请先确认当前版本', 'invalid_transition', 409)
    metrics = card['facts']['metrics']
    fields_out = []
    specs = (('distance', '距离', 'distance_m', 'm', 'km', 0.001, '距离'),
             ('duration', '时长', 'continuous_duration_s', 's', 's', 1, '有效连续时长（非总经过/移动时长）'),
             ('speed', '均速', 'average_speed_mps', 'm/s', 'km/h', 3.6, '均速（仅引用既有字段）'),
             ('elevation', '爬升', 'elevation_gain_m', 'm', 'm', 1, '爬升'),
             ('heart_rate', '心率', 'average_heart_rate_bpm', 'bpm', 'bpm', 1, '平均心率'))
    for key, label, metric_key, input_unit, unit, factor, description in specs:
        metric = metrics.get(metric_key) or {}
        value = metric.get('value')
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0 and metric.get('unit') == input_unit
        value = value * factor if valid else None
        if value is not None and not math.isfinite(value):
            value = None
        fields_out.append({'key': key, 'label': label, 'description': description, 'value': value,
                           'unit': unit, 'status': 'available' if value is not None else 'missing',
                           'missing_reason': None if value is not None else '本次无可用' + label + '数据'})
    data = {'revision': card['revision'], 'fields': fields_out,
            'dataset_notice': '合成数据，仅供演示' if card['source'].get('dataset_kind') == 'synthetic' else '用户记录，未上传云端'}
    # Metric whitelist only: no token, raw file, coordinates, free text or hashes.
    payload = dumps(data).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    static = ROOT / 'static'
    engine = (static / 'hud_fields.js').read_text(encoding='utf-8-sig')
    script = (static / 'ride_preview.js').read_text(encoding='utf-8-sig')
    css = (static / 'ride_preview.css').read_text(encoding='utf-8-sig')
    def digest(text):
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii') + "'"
    csp = ("default-src 'none'; script-src " + ' '.join(digest(item) for item in (engine, payload, script))
           + '; style-src ' + digest(css) + "; connect-src 'none'; img-src 'none'; font-src 'none'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    body = (static / 'ride_preview.html').read_text(encoding='utf-8-sig')
    for marker, value in (('/*__STYLE__*/', css), ('/*__ENGINE__*/', engine), ('/*__PAGE_SCRIPT__*/', script)):
        body = body.replace(marker, value)
    body = body.replace('/*__DATA__*/', payload)
    return PreviewPage(body.encode('utf-8'), csp)

class RideStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def identity(ident):
        require(type(ident) is str and re.fullmatch(r'[0-9a-f]{32}', ident), '记录ID格式错误')

    @staticmethod
    def expected(revision):
        require(type(revision) is int and revision > 0, '版本须为正整数')

    @staticmethod
    def current(db, ident, revision=None):
        row = db.execute('SELECT document FROM rides WHERE id=?', (ident,)).fetchone()
        require(row is not None, '骑行记录不存在', 'not_found', 404)
        card = json.loads(row['document'])
        if revision is not None and card['revision'] != revision:
            raise RideError('revision_conflict', '版本已改变；请重新读取，不自动覆盖', 409, card['revision'])
        return card

    def create(self, kind, content, dataset_kind):
        require(type(kind) is str and kind in ('gpx', 'tcx', 'manual_summary'), '不支持的输入类型')
        require(type(dataset_kind) is str and dataset_kind in ('synthetic', 'user_owned'), '需要声明数据来源类型')
        raw = None
        manual_json = None
        try:
            if kind == 'manual_summary':
                require(type(content) is dict, '手工摘要须为对象')
                card = build_review(manual_summary=content)
                manual_json = dumps(content)
                raw = content['text'].encode('utf-8') if 'text' in content else None
            else:
                require(type(content) is str and len(content) <= 4 * ((MAX_FILE_BYTES + 2) // 3), '文件编码超过大小上限', 'body_too_large', 413)
                try:
                    raw = base64.b64decode(content, validate=True)
                except (ValueError, binascii.Error):
                    raise RideError('invalid_input', '需要有效base64文件字节') from None
                require(len(raw) <= MAX_FILE_BYTES, '文件超过5 MiB', 'body_too_large', 413)
                card = build_review((parse_gpx if kind == 'gpx' else parse_tcx)(raw))
        except (GPXError, TCXError, ReviewError) as exc:
            raise RideError(exc.code, str(exc), 413 if exc.code in ('file_too_large', 'input_too_large') else 400) from None
        card['source']['dataset_kind'] = dataset_kind
        card['source']['storage_consent'] = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS rides (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL, original BLOB, manual_json TEXT)')
            db.execute('BEGIN IMMEDIATE')
            require(db.execute('SELECT COUNT(*) FROM rides').fetchone()[0] < MAX_RIDES, '记录数量达到本地上限', 'storage_full', 409)
            db.execute('INSERT INTO rides VALUES (?,?,?,?,?)', (card['id'], card['revision'], dumps(card), raw, manual_json))
            db.commit()
        return card

    def get(self, ident):
        self.identity(ident)
        require(self.path.exists(), '骑行记录不存在', 'not_found', 404)
        with self.connect() as db:
            return self.current(db, ident)

    def list(self):
        if not self.path.exists():
            return []
        with self.connect() as db:
            # No track coordinates or original text in list summaries.
            return [{'id': row['id'], 'revision': row['revision']} for row in db.execute('SELECT id,revision FROM rides ORDER BY rowid DESC')]

    @staticmethod
    def receipt(card, action, outcome):
        return {'id': uuid.uuid4().hex, 'action': action, 'outcome': outcome,
                'input_revision': card['revision'], 'resulting_revision': card['revision'] + 1,
                'at': now(), 'artifact': None, 'notice': '仅为本地用户操作记录，不证明训练完成或外部日历生效。'}

    def mutate(self, ident, revision, operation):
        self.identity(ident)
        self.expected(revision)
        require(self.path.exists(), '骑行记录不存在', 'not_found', 404)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            card = self.current(db, ident, revision)
            result = operation(card)
            # Every intermediate write invalidates previous version-bound ICS consent.
            if (card['calendar_authorization'] is not None and card['calendar_authorization']['state'] == 'authorized'
                    and card['calendar_authorization']['authorized_revision'] != revision + 1):
                card['calendar_authorization']['state'] = 'revoked'
            card['revision'] += 1
            card['updated_at'] = now()
            db.execute('UPDATE rides SET revision=?, document=? WHERE id=? AND revision=?',
                       (card['revision'], dumps(card), ident, revision))
            db.commit()
        return card if result is None else result

    @staticmethod
    def editable(card):
        require(card['status'] in ('draft', 'confirmed'), '当前状态不可变更', 'invalid_transition', 409)

    def edit(self, ident, revision, values):
        require(type(values) is dict and bool(values) and not set(values) - {'review_note', 'suggestion_text', 'scheduled_start', 'duration_s'}, '编辑字段不匹配')
        def operation(card):
            self.editable(card)
            suggestion = card['training_suggestion']
            if 'review_note' in values:
                note = checked_text(values['review_note'])
                card['user_statements'].append({'id': uuid.uuid4().hex, 'category': 'review_note', 'text': note,
                    'metric': None, 'value': None, 'unit': None, 'origin': 'user', 'evidence_ref': None, 'recorded_at': now()})
            if 'suggestion_text' in values:
                value = checked_text(values['suggestion_text'])
                require(bool(value.strip()), '建议内容不能为空')
                suggestion['text'] = value
                suggestion['user_edits'].append({'origin': 'user', 'text': value, 'at': now()})
            if 'scheduled_start' in values:
                start = values['scheduled_start']
                # Python 3.10 needs an explicit UTC offset for our own Z output.
                if isinstance(start, str) and start.endswith('Z'):
                    start = start[:-1] + '+00:00'
                dt = due_time(start)
                suggestion['scheduled_start'] = dt.isoformat().replace('+00:00', 'Z') if dt else None
            if 'duration_s' in values:
                value = values['duration_s']
                require(value is None or (type(value) is int and 0 < value <= 86400), '时长须为1至86400秒')
                suggestion['duration_s'] = value
            card['status'] = 'draft'
            card['confirmation'] = None
            card['calendar_authorization'] = None
        return self.mutate(ident, revision, operation)

    def confirm(self, ident, revision):
        def operation(card):
            require(card['status'] == 'draft', '仅草稿可确认', 'invalid_transition', 409)
            card['status'] = 'confirmed'
            card['confirmation'] = {'confirmed_revision': revision + 1, 'confirmed_at': now()}
        return self.mutate(ident, revision, operation)

    def authorize(self, ident, revision, scope):
        require(scope == 'generate_ics', '授权范围须为generate_ics', 'calendar_not_authorized', 409)
        def operation(card):
            require(card['status'] == 'confirmed', '须先独立确认', 'invalid_transition', 409)
            suggestion = card['training_suggestion']
            require(suggestion['scheduled_start'] is not None and suggestion['duration_s'] is not None, '先填写并确认日程与时长', 'invalid_time')
            card['calendar_authorization'] = {'state': 'authorized', 'scope': scope,
                'authorized_revision': revision + 1, 'authorized_at': now()}
        return self.mutate(ident, revision, operation)

    def feedback(self, ident, revision, value):
        require(type(value) is str and value in ('useful', 'not_useful'), '仅接受有用/无用标记')
        def operation(card):
            self.editable(card)
            card['feedback'] = {'value': value, 'recorded_at': now(), 'review_revision': revision}
        return self.mutate(ident, revision, operation)

    def finish(self, ident, revision, state):
        require(type(state) is str and state in ('done', 'cancelled'), '结束状态错误')
        def operation(card):
            require(card['status'] == 'confirmed' if state == 'done' else card['status'] in ('draft', 'confirmed'),
                    '当前状态不可完成或取消', 'invalid_transition', 409)
            card['receipts'].append(self.receipt(card, 'mark_done' if state == 'done' else 'cancel',
                                                 'user_marked' if state == 'done' else 'cancelled'))
            card['status'] = state
        return self.mutate(ident, revision, operation)

    def snapshot(self, ident, revision):
        self.expected(revision)
        card = self.get(ident)
        if card['revision'] != revision:
            raise RideError('revision_conflict', '版本已改变', 409, card['revision'])
        return card

    def calendar(self, ident, revision):
        generated = {}
        def operation(card):
            auth = card['calendar_authorization']
            require(card['status'] == 'confirmed' and auth is not None and auth['state'] == 'authorized'
                    and auth['scope'] == 'generate_ics' and auth['authorized_revision'] == revision,
                    '缺少当前版本独立ICS授权', 'calendar_not_authorized', 409)
            content = ride_calendar_file(card)
            artifact = {'filename': 'luma-ride-' + ident + '.ics', 'media_type': 'text/calendar',
                        'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()}
            receipt = self.receipt(card, 'generate_ics', 'generated')
            receipt.update({'artifact': artifact, 'notice': '仅生成文件；不证明下载完成、日历导入、通知送达或训练完成。'})
            card['receipts'].append(receipt)
            auth['state'] = 'consumed'
            generated.update({'ics': content, 'filename': artifact['filename'], 'artifact': artifact,
                              'receipt': receipt, 'receipt_persisted': True, 'notice': receipt['notice']})
        # Generation, version check, receipt and single-use consent commit together.
        generated['review'] = self.mutate(ident, revision, operation)
        return generated

    def export(self, ident, revision):
        card = self.snapshot(ident, revision)
        content = dumps(card)
        raw = content.encode('utf-8')
        artifact = {'filename': 'luma-ride-' + ident + '.json', 'media_type': 'application/json',
                    'sha256': hashlib.sha256(raw).hexdigest()}
        receipt = self.receipt(card, 'export_json', 'exported')
        receipt.update({'resulting_revision': revision, 'artifact': artifact,
                        'notice': '导出当前快照；回执未持久化且不嵌入被哈希文件，不证明下载完成。'})
        return {'content': content, 'artifact': artifact, 'receipt': receipt, 'receipt_persisted': False}

    def delete(self, ident, revision):
        self.identity(ident)
        self.expected(revision)
        require(self.path.exists(), '骑行记录不存在', 'not_found', 404)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            card = self.current(db, ident, revision)
            db.execute('DELETE FROM rides WHERE id=?', (ident,))
            db.commit()
        return {'deleted': True, 'id': ident, 'deleted_revision': revision + 1, 'deleted_at': now(),
                'calendar_may_exist': any(r['action'] == 'generate_ics' for r in card['receipts']),
                'notice': '已删除本地记录及受管原文；不删除已导出文件或外部日历，不承诺磁盘取证级擦除。'}


class App:
    def __init__(self, store=None):
        self.store = store if store is not None else RideStore()
        self.token = secrets.token_urlsafe(32)

    def request(self, name, payload):
        require(name in KEYS, '接口不存在', 'not_found', 404)
        fields(payload, KEYS[name])
        require(payload['schema_version'] == SCHEMA_VERSION, '不支持的schema', 'unsupported_schema')
        if 'consent' in payload:
            require(payload['consent'] is True, '需要本次明确同意', 'consent_required', 403)
        if name == 'create':
            return {'review': self.store.create(payload['kind'], payload['content'], payload['dataset_kind'])}
        if name == 'list':
            return {'reviews': self.store.list()}
        if name == 'get':
            return {'review': self.store.get(payload['id'])}
        ident, revision = payload['id'], payload['expected_revision']
        if name == 'edit':
            result = self.store.edit(ident, revision, payload['values'])
        elif name == 'authorize':
            result = self.store.authorize(ident, revision, payload['scope'])
        elif name == 'finish':
            result = self.store.finish(ident, revision, payload['state'])
        elif name == 'feedback':
            result = self.store.feedback(ident, revision, payload['value'])
        else:
            result = getattr(self.store, name)(ident, revision)
        return result if name in ('delete', 'export', 'calendar') else {'review': result}

    def dispatch(self, method, path, headers, body=b'', port=8793):
        """Transport boundary callable with fake capture, without opening sockets."""
        origin = 'http://127.0.0.1:' + str(port)
        require(headers.get('Host') == origin[7:], '仅接受明确本机Host', 'host_denied', 403)
        given = headers.get('Origin')
        require((given is None and method == 'GET') or given == origin, '拒绝跨源访问', 'origin_denied', 403)
        require(headers.get('Sec-Fetch-Site') in (None, 'same-origin', 'none'), '拒绝跨站访问', 'origin_denied', 403)
        if method == 'GET':
            if path == '/api/rides/capture':
                return render_capture()
            if path == '/api/rides/console':
                return render_console()
            parsed = urlsplit(path)
            if parsed.path == '/api/rides/page':
                require(not parsed.scheme and not parsed.netloc and not parsed.fragment, '页面地址无效')
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
                except ValueError:
                    raise RideError('invalid_input', '需要唯一记录id参数') from None
                require(set(query) == {'id'} and len(query['id']) == 1, '需要唯一记录id参数')
                return render_preview(self.store.get(query['id'][0]))
            require(path == '/api/status', '接口不存在', 'not_found', 404)
            return {'schema_version': SCHEMA_VERSION, 'token': self.token, 'mode': 'local_rules',
                    'cloud_enabled': False, 'ics_enabled': True, 'encrypted_at_rest': False}
        require(method == 'POST', '只接受GET状态查询和POST接口', 'method_not_allowed', 405)
        token = headers.get('X-Luma-Token', '')
        require(type(token) is str and secrets.compare_digest(token, self.token), '会话token不符', 'token_denied', 403)
        require(path.startswith('/api/rides/') and path[len('/api/rides/'):] in KEYS, '接口不存在', 'not_found', 404)
        require(not headers.get('Transfer-Encoding') and headers.get('Content-Type') == 'application/json', '只接受定长JSON', 'invalid_body', 415)
        try:
            length = int(headers.get('Content-Length', '-1'))
        except (ValueError, TypeError):
            length = -1
        require(0 < length <= MAX_BODY_BYTES, '请求体缺失或超限', 'body_too_large', 413)
        require(len(body) == length, '请求体不完整')
        return self.request(path[len('/api/rides/'):], strict_json(body))


def create_server(port=8793, app=None):
    app = app if app is not None else App()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        def log_message(self, *args):
            pass
        def reply(self, status, value):
            page = status == 200 and isinstance(value, PreviewPage)
            download = status == 200 and isinstance(value, dict) and 'ics' in value
            raw = value.body if page else value['ics'].encode('utf-8') if download else dumps(value).encode('utf-8')
            csp = value.csp if page else "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            self.send_response(status)
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="' + value['filename'] + '"')
                self.send_header('X-Luma-Revision', str(value['review']['revision']))
                self.send_header('X-Luma-Artifact-SHA256', value['artifact']['sha256'])
                self.send_header('X-Luma-Receipt-Id', value['receipt']['id'])
            for key, value in {'Content-Type': 'text/html; charset=utf-8' if page else 'text/calendar; charset=utf-8' if download else 'application/json; charset=utf-8', 'Content-Length': str(len(raw)),
                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': csp}.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def handle_request(self):
            try:
                self.connection.settimeout(15)
                # Preflight security before reading any request body.
                if self.command == 'POST':
                    try:
                        length = int(self.headers.get('Content-Length', '-1'))
                    except ValueError:
                        length = -1
                    # dispatch rejects boundary failures without reading bytes.
                    # A valid header set reaches the incomplete-body check below.
                    try:
                        app.dispatch('POST', self.path, self.headers, b'', self.server.server_port)
                    except ActionError as exc:
                        if str(exc) != '请求体不完整':
                            raise
                    require(0 < length <= MAX_BODY_BYTES, '请求体缺失或超限', 'body_too_large', 413)
                    raw = self.rfile.read(length)
                else:
                    raw = b''
                self.reply(200, app.dispatch(self.command, self.path, self.headers, raw, self.server.server_port))
            except ActionError as exc:
                value = {'error_code': exc.code, 'message': str(exc), 'retryable': False}
                if getattr(exc, 'current_revision', None) is not None:
                    value['current_revision'] = exc.current_revision
                self.reply(exc.status, value)
            except (TimeoutError, ConnectionError):
                pass
            except (sqlite3.Error, OSError):
                self.reply(503, {'error_code': 'storage_unavailable', 'message': '本地存储不可用；未自动重试', 'retryable': False})
            except Exception:
                self.reply(500, {'error_code': 'internal_error', 'message': '操作未完成，请重新读取核对；不自动重试', 'retryable': False})
        do_GET = handle_request
        do_POST = handle_request
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8793)
    args = parser.parse_args()
    if args.port in (8765, 8791):
        parser.error('不得占用8765/8791')
    server = create_server(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

