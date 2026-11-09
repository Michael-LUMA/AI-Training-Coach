"""LLM card factory: draft knowledge cards in bulk, validate locally.

草稿进入 staging 文件，逐张过本地校验（schema/长度/标签/禁词/检索可达），
并集与现有库去重后才打印合并建议。绝不直接改卡片库——合并是显式人工动作。
用法：DASHSCOPE_API_KEY=... python tools/gen_cards.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ride_knowledge as know
from llm_review import _http_post, _json
from ride_brain import _FORBIDDEN

MODEL = 'qwen-plus'
STAGING = ROOT / '.runtime' / 'cards_staging.json'

SYSTEM = """你是运动知识卡片撰写员。按用户给出的主题写一张中文知识卡片，只输出 JSON：
{"id":"kebab-case-英文","title":"问题式标题，不超过二十字","tags":["三到六个纯中文标签"],"body":"正文","source":"通用运动常识"}
硬性要求：
- body 六十到三百字，口语化，给做法不给处方；禁止医疗诊断，涉及伤病写「停止运动，找医生看看」。
- body 禁止出现这些措辞：建议、推荐、参考、变强、训练效果、减脂效果、燃脂效果、效率高、强度大、强度低、诊断、治疗。
- body 里尽量不用数字；非用不可时用中文数字（如「九十分钟」），且数字必须是公开常识而非编造。
- tags 每个一到二十字，只用中文，三到六个。
- 不输出任何额外文字或字段。"""

TOPICS = [
    ('骑行', '换挡时机怎么把握'), ('骑行', '跟车距离与风阻'), ('骑行', '骑行裤怎么选'),
    ('骑行', '长距离骑行补给频率'), ('骑行', '自行车尺码怎么量'), ('骑行', '骑行手势大全'),
    ('骑行', '头盔多久换一次'), ('骑行', '公路车与平把车区别'),
    ('跑步', '前掌落地还是后掌落地'), ('跑步', '马拉松配速策略入门'), ('跑步', '跑步时手机怎么带'),
    ('跑步', '越野跑入门注意什么'), ('跑步', '跑步脚踝疼怎么办'), ('跑步', '雨天跑步注意什么'),
    ('徒步', '登山包怎么打包'), ('徒步', '溯溪注意什么'), ('徒步', '一日露营基础准备'),
    ('徒步', '野外失温怎么预防'), ('徒步', '徒步鞋底怎么选'),
    ('游泳', '游泳入门先学什么'), ('游泳', '自由泳换气怎么练'), ('游泳', '公开水域游泳安全'),
    ('游泳', '泳镜起雾怎么办'), ('游泳', '游泳后耳朵进水怎么办'),
    ('健身', '力量训练入门原则'), ('健身', '深蹲动作要点'), ('健身', '俯卧撑怎么做标准'),
    ('健身', '核心训练入门'), ('健身', '引体向上练不上去怎么办'),
    ('营养', '运动后蛋白质怎么补'), ('营养', '运动饮料和白水怎么选'), ('营养', '咖啡因和运动表现'),
    ('营养', '比赛前一天吃什么'), ('营养', '素食者运动营养注意'),
    ('心理', '运动坚持不下去怎么办'), ('心理', '平台期怎么看待'), ('心理', '比赛紧张怎么调节'),
    ('安全', '运动中扭伤怎么处理'), ('安全', '户外运动告知原则'), ('安全', '雾霾天能户外运动吗'),
    ('装备', '运动内衣怎么选'), ('装备', '压缩袜有用吗'), ('装备', '臂包和腰包怎么选'),
    ('装备', '运动手表心率准吗'),
]


def draft_card(domain, topic, key):
    payload = {'model': MODEL, 'max_tokens': 800,
               'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system', 'content': SYSTEM},
                            {'role': 'user', 'content': json.dumps(
                                {'领域': domain, '主题': topic}, ensure_ascii=False)}]}
    request = Request(know.ENDPOINT,
                      data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                      headers={'Authorization': 'Bearer ' + key,
                               'Content-Type': 'application/json'}, method='POST')
    status, _, body = _http_post(request, timeout=30)
    if status != 200:
        return None
    content = _json(body)['choices'][0]['message']['content']
    card = _json(content)
    if type(card) is not dict:
        return None
    return card


def main():
    key = os.environ.get('DASHSCOPE_API_KEY', '').strip()
    if not key:
        raise SystemExit('DASHSCOPE_API_KEY required')
    existing = know.load_cards(ROOT / 'knowledge' / 'cards_zh_v1.json')
    staged = json.loads(STAGING.read_text(encoding='utf-8')) if STAGING.exists() else []
    taken = {card['id'] for card in existing} | {card['id'] for card in staged}
    accepted = rejected = 0
    for domain, topic in TOPICS:
        slug_hint = topic
        if any(card['title'] == topic for card in existing + staged):
            print('SKIP dup-title:', topic)
            continue
        try:
            card = draft_card(domain, topic, key)
        except Exception as error:
            print('FAIL upstream:', topic, type(error).__name__)
            rejected += 1
            continue
        if card is None:
            print('REJ upstream:', topic)
            rejected += 1
            continue
        problems = []
        try:
            know._validate_cards([card])
        except ValueError as error:
            problems.append('schema')
        if card.get('id') in taken:
            problems.append('dup-id')
        if type(card.get('body')) is str and _FORBIDDEN.search(card['body']):
            problems.append('forbidden-word')
        if not problems:
            # 自检索可达性：用自己的标题提问，top-1 必须是自己。
            hits = know.retrieve(card['title'], existing + staged + [card])
            if not hits or hits[0]['id'] != card['id']:
                problems.append('not-retrievable:' + (hits[0]['id'] if hits else 'none'))
        if problems:
            print('REJ', ','.join(problems), ':', topic)
            rejected += 1
            continue
        staged.append(card)
        taken.add(card['id'])
        accepted += 1
        print('OK  ', card['id'], '|', card['title'])
        STAGING.write_text(json.dumps(staged, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'staged={len(staged)} accepted_now={accepted} rejected_now={rejected}')
    print('staging file:', STAGING)


if __name__ == '__main__':
    main()
