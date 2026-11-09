import asyncio
import csv
import datetime
import io
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
import websockets
import zhconv

session = requests.Session()

# ord('⋯') = 8943
# ord('…') = 8230
punctuation_pattern = re.compile(r"([，⋯…？?、!！「」～~☆【】《》。.·—{}'\"“”♪↗↘\s]|\\n)")


def parse_gamekee_data(data: str):
    ret = defaultdict(set)
    person = None
    data = re.sub(R'\n\n\s+', '', data)
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('100') or 'question' in line:
            continue
        if line.startswith('120'):
            a = punctuation_pattern.sub('', line[5:])
            a = a.replace('AccountDataNickName', '')
            if not a:
                continue
            ret[person].add(a)
        else:
            person = line[:-1]
    print('Gamekee pastebin:')
    keys = list(ret.keys())
    for i in range(0, len(keys), 5):
        print(', '.join(keys[i:i+5]))
    return {k: list(v) for k, v in ret.items()}


async def get_from_gamekee_baidu_pan():
    docid = '521691390812354'
    surl = f'12uSFe5M1o5PMM4p0kwJnQ-{docid}'
    appid = '250528'
    pwd = 'x2fh'
    author_uk = '2351864206'    # can actually get uk from some api
    timestamp = int(time.time())
    # verify
    params = {
        'clienttype': 0,
        'app_id': appid,
        'web': 1
    }
    data = dict(pwd=pwd, surl=surl)
    resp = session.post(
        'https://pan.baidu.com/api/docol/verify', params=params, data=data,
        timeout=30)
    resp.raise_for_status()
    assert resp.json()['errno'] == 0
    # get signature
    params = {
        'clienttype': 0,
        'bdstoken': '',
        'docid': docid,
        'doc_uri': f'/doc?appid={appid}&docid={docid}&doc_timestamp={timestamp}&mode=readonly',
        'surl': surl
    }
    resp = session.get(
        'https://pan.baidu.com/api/docol/signature', params=params, timeout=30)
    resp.raise_for_status()
    assert resp.json()['errno'] == 0
    sig = resp.json()['data']['sign']
    # fetch data via websocket
    uri = f'wss://docpan.baidu.com/doc?appid={appid}&docid={docid}&doc_timestamp={timestamp}&mode=readonly&sig={sig}&uk={author_uk}'
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({'a': 's', 'c': appid, 'd': docid}))
        ans = await ws.recv()
        ans = await ws.recv()
        data = json.loads(ans)['data']['data']['ops'][0]['insert']
    return parse_gamekee_data(data)


def get_from_gamekee_netcut():
    '''deprecated'''
    note_id = '38b3472ea9d95306'
    resp = session.post(
        'https://netcut.txtbin.cn/api/note2/info/',
        data={'note_id': note_id}
    ).json()
    if 'error' in resp:
        print(f'Netcut error: {resp["error"]}')
        return {}
    note_content = resp['data']['note_content']
    note_content = note_content.replace('\\n', '\n').replace('\\t', '\t')
    return parse_gamekee_data(note_content)


def get_from_gamekee_wiki(skip_names: set[str]):
    # gamekee误写
    skip_names.update(['D:杀手妻子'])
    ret = dict()
    game_header = {'game-alias': 'nikke'}
    entry_url = 'https://www.gamekee.com/v1/wiki/entry'
    entry_response = session.get(entry_url, headers=game_header, timeout=30)
    entry_response.raise_for_status()
    entry_json = entry_response.json()
    characters = None
    entry_id = None
    for d in entry_json['data']['entry_list']:
        if d.get('name', None) == '妮姬图鉴':
            for l in d['child']:
                if l.get('name', None) == '角色图鉴':
                    characters = l['child']
                    entry_id = l['id']
                    break
            break
    if characters is None:
        raise ValueError('获取gamekee角色图鉴失败')
    filter_response = session.get(
        'https://www.gamekee.com/v1/entryFilter/getEntryFilter',
        headers=game_header,
        params={'entry_id': entry_id},
        timeout=30
    )
    filter_response.raise_for_status()
    entry_filter = filter_response.json()
    invalid_pair = set()
    for f in entry_filter['data']['entry_filter']:
        if f['name'] == '企业':
            for c in f['children']:
                if c['name'] == '反常':
                    invalid_pair.add((f['id'], c['id']))
        elif f['name'] == '稀有度':
            for c in f['children']:
                if c['name'] == 'R':
                    invalid_pair.add((f['id'], c['id']))

    def is_valid(nikke_entry):
        for attr in entry_filter['data']['entry_filter_attr'].get(str(nikke_entry['id']), []):
            if len(attr['value']) != 1:
                print(nikke_entry)
                print(entry_filter['data']['entry_filter_attr'].get(
                    str(nikke_entry['id'])))
                raise ValueError('gamekee wiki parsing failed')
            if attr['value'][0] == '':
                continue
            if (attr['input_id'], int(attr['value'][0])) in invalid_pair:
                return False
        return True

    def get_single(base_data):
        for d in base_data:
            if d[0]['value'] != '120好感度':
                continue
            answer = d[1]['value']
            answer = punctuation_pattern.sub('', answer)
            answer = answer.replace('AccountDataNickName', '')
            if answer:
                yield answer

    for nikke in characters:
        if nikke["name"] in skip_names:
            continue
        # 编辑中词条
        if nikke["content_id"] == 0:
            continue
        if not is_valid(nikke):
            ret[nikke["name"]] = []
            continue
        detail_response = session.get(
            f"https://www.gamekee.com/v1/content/detail/{nikke['content_id']}",
            headers=game_header,
            timeout=30
        )
        detail_response.raise_for_status()
        content = detail_response.json()["data"]["content_json"]
        if not content:
            raise ValueError(f"{nikke['name']}: Gamekee Wiki content format changed")
        content_json = json.loads(content)
        base_data = content_json.get("baseData", [])
        name = next((x for x in base_data if x[0]["value"] == "角色名称"), None)
        name = name[1]["value"] if name else nikke["name"]
        ret[name] = set(get_single(base_data))
        time.sleep(0.5)
    print('Gamekee Wiki:')
    keys = list(ret.keys())
    for i in range(0, len(keys), 5):
        print(', '.join(keys[i:i+5]))
    return ret


def get_from_google_sheet(apiKey=None):
    ret = defaultdict(set)
    # 定义要访问的google表格的ID和范围
    spreadsheetId = '1K_oGZWL4uvuqYM9JL2l5Kb-L0IXIuyrDI347b6rurho'
    if apiKey:
        url = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheetId}/values:batchGet'
        response = session.get(url, params={
            'key': apiKey,
            'ranges': ['角色排序查詢!A2:A', '角色排序查詢!C2:C']
        }, timeout=30)
        response.raise_for_status()
        value_ranges = response.json()['valueRanges']
        names = next(x['values'] for x in value_ranges if 'A2' in x['range'])
        answers = next(x['values'] for x in value_ranges if 'C2' in x['range'])
        if len(names) != len(answers):
            raise ValueError('Google sheet name and answer columns have different lengths')
        rows = ((name[0] if name else '', '', answer[0] if answer else '')
                for name, answer in zip(names, answers))
    else:
        url = f'https://docs.google.com/spreadsheets/d/{spreadsheetId}/gviz/tq'
        response = session.get(url, params={
            'tqx': 'out:csv',
            'sheet': '角色排序查詢',
            'range': 'A2:C'
        }, timeout=30)
        response.raise_for_status()
        if 'text/csv' not in response.headers.get('Content-Type', ''):
            raise ValueError('Google sheet did not return CSV data')
        rows = csv.reader(io.StringIO(response.text))
    excluded_pattern = re.compile(r'[<＜]指挥官名[>＞]')
    for row in rows:
        if len(row) != 3:
            raise ValueError('Google sheet row must contain three columns')
        n = zhconv.convert(row[0], 'zh-sg').strip()
        a = zhconv.convert(row[2], 'zh-sg')
        cleaned_a = excluded_pattern.sub('', punctuation_pattern.sub('', a))
        if not n or not cleaned_a:
            continue
        if n == 'Ｄ':
            n = 'D'
        elif n == '吉罗婷：寒冬杀手':
            n = '吉萝婷：寒冬杀手'
        ret[n].add(cleaned_a)
    if not ret:
        raise ValueError('Google sheet returned no consultation answers')
    return {k: list(v) for k, v in ret.items()}


def main(nikke_json_path=None):
    if nikke_json_path is None:
        nikke_json_path = Path(__file__).parent.parent / 'nikke.json'
    api_key = sys.argv[1] if len(sys.argv) > 1 else None
    google_data = get_from_google_sheet(api_key)
    if not any(google_data.values()):
        raise RuntimeError('No fresh consultation answers; cached data was not changed')
    fresh_sources = [google_data]
    try:
        baidu_data = asyncio.run(asyncio.wait_for(get_from_gamekee_baidu_pan(), 60))
        fresh_sources.append(baidu_data)
    except (OSError, ValueError, KeyError, AssertionError, requests.RequestException,
            websockets.WebSocketException) as error:
        print(f'Gamekee Baidu source unavailable; cached answers retained: {error}')
        baidu_data = {}
    try:
        fresh_sources.append(get_from_gamekee_wiki(set(baidu_data.keys())))
    except (requests.RequestException, ValueError, KeyError) as error:
        print(f'Gamekee Wiki unavailable; using Google sheet and cached answers: {error}')
    with nikke_json_path.open('r', encoding='utf-8') as f:
        old_data = json.load(f)
    updated_data = {name: set(answers) for name, answers in old_data.items()
                    if name != '$meta'}
    for source in fresh_sources:
        for name, answers in source.items():
            updated_data.setdefault(name, set()).update(answers)
    change_texts = []
    for name in sorted(updated_data):
        count = len(updated_data[name]) - len(set(old_data.get(name, [])))
        if count > 0:
            change_texts.append(f'新增【{name}】{count}条')
    if not change_texts:
        print('No new consultation answers; cached data was not changed')
        return
    print(f'Changes: {"，".join(change_texts)}')
    today_date = datetime.datetime.now().strftime('%Y-%m-%d')
    metadata = dict(old_data.get('$meta', {}))
    metadata['changelogs'] = [
        f'{today_date}\t{"，".join(change_texts)}',
        *metadata.get('changelogs', [])
    ][:5]
    output = {name: sorted(answers) for name, answers in updated_data.items()}
    output['$meta'] = metadata
    temp_path = nikke_json_path.with_suffix('.json.tmp')
    with temp_path.open('w', encoding='utf-8') as f:
        json.dump(
            dict(sorted(output.items())),
            f, ensure_ascii=False, indent=2
        )
    temp_path.replace(nikke_json_path)


if __name__ == '__main__':
    main()
