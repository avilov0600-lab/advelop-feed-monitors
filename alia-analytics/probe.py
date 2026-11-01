"""Read-only ALIA API capability check. Credentials and personal data never leave VPS."""
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


def environment():
    result = {}
    for line in Path('/etc/asterus-feed-monitor.env').read_text().splitlines():
        key, sep, val = line.strip().removeprefix('export ').partition('=')
        if sep:
            parts = shlex.split(val, comments=True)
            if len(parts) == 1:
                result[key] = parts[0]
    return result


def request(url, token=None, data=None, form=False):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
    req = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return {'http': response.status, 'data': json.load(response)}
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(16384))
        except ValueError:
            body = {}
        return {'http': error.code, 'data': body}
    except Exception as error:
        return {'error': type(error).__name__}


def shape(value, depth=0):
    if depth > 6:
        return type(value).__name__
    if isinstance(value, dict):
        return {k: '[private]' if any(s in k.lower() for s in
                ('token', 'secret', 'phone', 'address', 'record', 'url', 'name', 'email'))
                else shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return {'count': len(value), 'sample': [shape(v, depth + 1) for v in value[:1]]}
    if isinstance(value, str):
        return value[:300] if not any(c in value for c in ['@', '://']) else '[string]'
    return value


def main():
    env = environment()
    now = dt.datetime.now(ZoneInfo('Europe/Moscow'))
    day = (now.date() - dt.timedelta(days=1)).isoformat()
    results = {}
    cian = env.get('CIAN_API_KEY_CIAN_1')
    base = 'https://public-api.cian.ru'
    offers = request(base + '/v2/get-my-offers?source=upload&statuses=published&page=1&pageSize=100', cian)
    results['cian_offers'] = shape(offers)
    rows = (offers.get('data', {}).get('result') or {}).get('announcements') or []
    if rows:
        ident = rows[0]['id']
        results['cian_auction'] = shape(request(base + '/v1/get-auction?' + urllib.parse.urlencode({'offerIds':ident}), cian))
        results['cian_services'] = shape(request(base + '/v1/get-offer-active-services?' + urllib.parse.urlencode({'offerIds':ident}), cian))
    auth = request('https://api.avito.ru/token', data={
        'grant_type':'client_credentials', 'client_id':env.get('AVITO_CLIENT_ID_AVITO_1'),
        'client_secret':env.get('AVITO_CLIENT_SECRET_AVITO_1')}, form=True)
    results['avito_auth'] = {'http':auth.get('http')}
    token = auth.get('data', {}).get('access_token')
    if token:
        me = request('https://api.avito.ru/core/v1/accounts/self', token)
        user = me.get('data', {}).get('id')
        results['avito_self'] = {'http':me.get('http'), 'account_present':bool(user)}
        items = request('https://api.avito.ru/core/v1/items?status=active&page=1&per_page=5', token)
        results['avito_items'] = shape(items)
        ids = [i['id'] for i in items.get('data', {}).get('resources', [])]
        if user:
            results['avito_analytics'] = shape(request(f'https://api.avito.ru/stats/v2/accounts/{user}/items', token,
                {'dateFrom':day,'dateTo':day,'grouping':'day','metrics':['impressions','views','contacts'],'limit':1,'offset':0}))
            if ids:
                time.sleep(1.2)
                results['avito_stats_v1'] = shape(request(f'https://api.avito.ru/stats/v1/accounts/{user}/items', token,
                    {'dateFrom':day,'dateTo':day,'itemIds':ids}))
    destination = Path('/tmp/alia-api-capabilities.json')
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    os.chmod(destination, 0o600)
    print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    main()
