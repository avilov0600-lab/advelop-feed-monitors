"""Read-only Cian/Avito requests. No bid-setting, tariff-changing or call recordings."""
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class SourceError(Exception):
    pass


class API:
    def __init__(self, base, token=None, interval=.25):
        self.base, self.token, self.interval = base, token, interval
        self.last = 0
        self.requests = 0

    def call(self, path, *, query=None, data=None, form=False, attempts=3, timeout=18):
        url = self.base + path
        if query:
            url += '?' + urllib.parse.urlencode(query, doseq=True)
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        body = None
        if data is not None:
            headers['Content-Type'] = ('application/x-www-form-urlencoded' if form else 'application/json')
            body = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
        for attempt in range(attempts):
            time.sleep(max(0, self.last + self.interval - time.monotonic()))
            self.last = time.monotonic()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers=headers, data=body)
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise SourceError('ответ API слишком большой')
                return json.loads(raw)
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                    raise SourceError('API HTTP ' + str(error.code)) from None
                time.sleep(min(10, 2 ** (attempt + 1)))
            except (OSError, ValueError):
                if attempt == attempts - 1:
                    raise SourceError('API временно недоступен') from None
                time.sleep(2)


def failure(error):
    return {'status': 'unavailable', 'reason': str(error) if isinstance(error, SourceError)
            else 'неполный или изменившийся ответ API'}


def cian_inventory(api):
    ids = set()
    for page in range(1, 51):
        result = api.call('/v2/get-my-offers', query={
            'source': 'upload', 'statuses': 'published', 'page': page, 'pageSize': 100})['result']
        total = int(result['totalCount'])
        previous = len(ids)
        ids.update(int(i['id']) for i in result['announcements'])
        if len(ids) == total:
            return sorted(ids)
        if len(ids) <= previous or len(ids) > total:
            break
    raise SourceError('не удалось получить полный список объявлений')


def cian_auction(api, ids):
    items = {}
    deadline = time.monotonic() + 120

    def fetch(batch):
        if time.monotonic() >= deadline:
            return
        try:
            rows = api.call('/v1/get-auction', query={'offerIds': batch}, attempts=1,
                            timeout=min(8, max(1, deadline - time.monotonic())))['result']['items']
        except SourceError as error:
            if str(error).endswith(('401', '403', '429')):
                raise
            if len(batch) > 1:
                middle = len(batch) // 2
                fetch(batch[:middle])
                fetch(batch[middle:])
            return
        for row in rows:
            ident = int(row['offerId'])
            if ident not in batch or ident in items:
                raise SourceError('неоднозначный список аукциона')
            items[ident] = {k: row.get(k) for k in ('offerId', 'currentBet', 'leaderBet', 'page', 'position')}
    for offset in range(0, len(ids), 20):
        fetch(ids[offset:offset + 20])
    return {'status': 'ok' if len(items) == len(ids) else 'partial', 'total': len(ids),
            'covered': len(items), 'items': list(items.values())}


def cian_stats(api, ids, day, state_dir, shared_cache):
    """Reuse completed morning statistics; own partial cache never mutates monitor state."""
    own = Path(state_dir) / ('cian-statistics-' + day.isoformat() + '.json')
    for path in (Path(shared_cache), own):
        try:
            cache = json.loads(path.read_text())
            if cache.get('date') == day.isoformat() and cache.get('status') == 'ok':
                return {k: cache.get(k) for k in ('date', 'status', 'shows', 'views', 'covered', 'total')}
        except (OSError, ValueError):
            pass
    cache = {'date': day.isoformat(), 'items': {}, 'total': len(ids)}
    try:
        prior = json.loads(own.read_text())
        if prior.get('date') == day.isoformat():
            cache['items'] = {k: v for k, v in prior.get('items', {}).items() if int(k) in ids}
    except (OSError, ValueError):
        pass
    deadline = time.monotonic() + 100
    for ident in ids:
        if str(ident) in cache['items']:
            continue
        if time.monotonic() > deadline:
            break
        try:
            result = api.call('/v1/get-views-statistics-by-days', query={
                'offerId': ident, 'dateFrom': day.isoformat(),
                'dateTo': (day + dt.timedelta(days=1)).isoformat()})['result']
            if int(result['offerId']) != ident:
                raise SourceError('не совпадает ID статистики')
            values = {}
            for series, metric in (('showsByDays', 'shows'), ('viewsByDays', 'views')):
                matching = [r for r in result[series] if r['date'] == day.isoformat()]
                if len(matching) != 1:  # Missing daily point is not a confirmed zero.
                    raise SourceError('нет точки статистики за отчётный день')
                value = int(matching[0][metric])
                if value < 0:
                    raise SourceError('некорректная статистика')
                values[metric] = value
            cache['items'][str(ident)] = values
        except (SourceError, KeyError, ValueError):
            continue
    complete = len(cache['items']) == len(ids)
    cache.update(status='ok' if complete else 'partial', covered=len(cache['items']),
                 shows=sum(r['shows'] for r in cache['items'].values()) if complete else None,
                 views=sum(r['views'] for r in cache['items'].values()) if complete else None)
    own.write_text(json.dumps(cache))
    return {k: cache.get(k) for k in ('date', 'status', 'shows', 'views', 'covered', 'total')}


def collect_cian(env, day, state_dir, shared_cache):
    api = API('https://public-api.cian.ru', env.get('CIAN_API_KEY_CIAN_1'))
    if not api.token:
        return failure(SourceError('ключ Циан не настроен'))
    output = {'status': 'ok'}
    try:
        ids = cian_inventory(api)
        output['inventory'] = len(ids)
    except Exception as error:
        return failure(error)
    for name, fn in (
        ('auction', lambda: cian_auction(api, ids)),
        ('statistics', lambda: cian_stats(api, ids, day, state_dir, shared_cache)),
    ):
        try:
            output[name] = fn()
        except Exception as error:
            output[name] = failure(error)
    output['requests'] = api.requests
    return output


def avito_metrics(result, day):
    """stats/v2 day grouping, validated against ALIA's live response on 2026-11-02."""
    result = result['result']
    groups = result['groupings']
    if int(result['dataTotalCount']) != len(groups):
        raise SourceError('аналитика Авито получена не полностью')
    if len(groups) != 1 or groups[0]['type'] != 'dates':
        raise SourceError('нет подтверждённой дневной статистики Авито')
    from zoneinfo import ZoneInfo
    returned = dt.datetime.fromtimestamp(int(groups[0]['id']), ZoneInfo('Europe/Moscow')).date()
    if returned != day:
        raise SourceError('дата статистики Авито не совпадает')
    metrics = {row['slug']: row['value'] for row in groups[0]['metrics']}
    if not all(isinstance(metrics.get(k), (int, float)) and metrics[k] >= 0
               for k in ('impressions', 'views', 'contacts')):
        raise SourceError('Авито не вернул все запрошенные показатели')
    return {'status': 'ok', 'date': day.isoformat(), **metrics}


def collect_avito(env, day):
    api = API('https://api.avito.ru', interval=1.2)
    if not env.get('AVITO_CLIENT_ID_AVITO_1') or not env.get('AVITO_CLIENT_SECRET_AVITO_1'):
        return failure(SourceError('ключ Авито не настроен'))
    try:
        api.token = api.call('/token', data={
            'grant_type': 'client_credentials', 'client_id': env['AVITO_CLIENT_ID_AVITO_1'],
            'client_secret': env['AVITO_CLIENT_SECRET_AVITO_1']}, form=True)['access_token']
        user = api.call('/core/v1/accounts/self')['id']
        result = api.call(f'/stats/v2/accounts/{user}/items', data={
            'dateFrom': day.isoformat(), 'dateTo': day.isoformat(), 'grouping': 'day',
            'metrics': ['impressions', 'views', 'contacts'], 'limit': 1000, 'offset': 0})
        return {'status': 'ok', 'statistics': avito_metrics(result, day), 'requests': api.requests,
                'scope': 'account', 'positions': {'status': 'unavailable',
                                                'reason': 'метод аналитики не возвращает позиции'}}
    except Exception as error:
        return failure(error)
