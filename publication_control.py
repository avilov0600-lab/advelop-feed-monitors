"""Shared source-of-truth → site → agency XML → publication controls.

Public card counts never use agency XML as a substitute for publication.
Cian daily statistics are a separate, date-keyed cache, not a publication test.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
from floor_control import height_profile, height_lines, non_height_counts, cross_height_profile, cross_height_lines, invalid_floor_count

MOSCOW = ZoneInfo('Europe/Moscow')
API_SOURCES = {'cian', 'avito'}


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_count(source, document):
    """Only explicit project counters; never generic numbers in recommendations."""
    if re.search(r'<title[^>]*>[^<]*(?:captcha|доступ ограничен|проверка браузера)', document, re.I):
        return None
    title_match = re.search(r'<title[^>]*>(.*?)</title>', document, re.I | re.S)
    title = html.unescape(title_match.group(1)) if title_match else ''
    reader = PageText()
    reader.feed(document)
    plain = re.sub(r'\s+', ' ', ' '.join(reader.parts))
    title = re.sub(r'\s+', ' ', title)
    patterns = {
        'yandex': [
            (title, r'[—–]\s*(\d[\d ]*)\s+квартир(?:а|ы)?(?:\s+по\s+цене|\s*$)'),
            (plain, r'в настоящий момент продажи\s+(\d[\d ]*)\s+квартир\w*\s+открыты'),
        ],
        'domclick': [
            (title, r'(\d[\d ]*)\s+предложени[йя]\s+от\s+застройщика'),
            (plain, r'(\d[\d ]*)\s+предложени[йя]\s+от\s+застройщика'),
        ],
        'm2': [(title, r'(\d[\d ]*)\s+(?:квартир\w*|предложени[йя])'),
               (re.split(r'Вам может быть интересно|Похожие новостройки|Другие жилые комплексы',plain,flags=re.I)[0],
                r'(\d[\d ]*)\s+предложени[йя]')],
    }
    counts = set()
    for text, pattern in patterns.get(source, []):
        for match in re.finditer(pattern, text, re.I):
            counts.add(int(match.group(1).replace(' ', '')))
    return next(iter(counts)) if len(counts) == 1 else None


def _get_json(url, token):
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token,
                                                  'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=8) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError('Invalid API object')
    return value


def daily_cian(cache_dir, account, token, *, budget=65, now=None, request=_get_json):
    """Yesterday in Moscow, only upload/published inventory; resume partial cache.

    Two workers share a 0.2s request interval. A complete day is fetched once;
    incomplete snapshots never present a partial sum as the account total.
    """
    now = now or dt.datetime.now(MOSCOW)
    day = (now.astimezone(MOSCOW).date() - dt.timedelta(days=1)).isoformat()
    today = now.astimezone(MOSCOW).date().isoformat()
    path = Path(cache_dir) / ('statistics-' + account + '.json')
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get('date') != day:
        state = {'date': day, 'items': {}, 'inventory_complete': False}
    if state.get('status') == 'ok':
        return state
    if not token:
        return {'date': day, 'status': 'unavailable', 'reason': 'API не подключён'}
    deadline = time.monotonic() + budget
    base = 'https://public-api.cian.ru'
    try:
        if not state.get('inventory_complete'):
            ids = set()
            total = None
            for page in range(1, 101):
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                query = urllib.parse.urlencode({'source':'upload', 'statuses':'published',
                                                 'page':page, 'pageSize':100})
                result = request(base + '/v2/get-my-offers?' + query, token)['result']
                rows = result['announcements']
                total = int(result['totalCount'])
                before = len(ids)
                ids.update(str(row['id']) for row in rows)
                if len(ids) == total:
                    break
                if len(ids) == before or len(ids) > total:
                    raise ValueError('Inventory pagination incomplete')
                time.sleep(.2)
            if len(ids) != total:
                raise ValueError('Inventory pagination limit')
            state.update(offer_ids=sorted(ids), inventory_complete=True)
        lock = threading.Lock()
        next_request = [0.0]

        def fetch(offer):
            with lock:
                delay = max(0, next_request[0] - time.monotonic())
                if time.monotonic() + delay >= deadline:
                    return offer, None
                time.sleep(delay)
                next_request[0] = time.monotonic() + .2
            query = urllib.parse.urlencode({'offerId':offer, 'dateFrom':day, 'dateTo':today})
            try:
                result = request(base + '/v1/get-views-statistics-by-days?' + query, token)['result']
                if str(result['offerId']) != offer:
                    raise ValueError('Wrong offer')
                values = {}
                for series, metric in (('showsByDays','shows'), ('viewsByDays','views')):
                    rows = result[series]
                    if not isinstance(rows, list):
                        raise ValueError('Invalid daily series')
                    values[metric] = sum(int(row[metric]) for row in rows if row['date'] == day)
                    if values[metric] < 0:
                        raise ValueError('Invalid daily value')
                return offer, values
            except (OSError, ValueError, KeyError):
                return offer, None

        missing = [offer for offer in state['offer_ids'] if offer not in state['items']]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for offer, values in pool.map(fetch, missing):
                if values is not None:
                    state['items'][offer] = values
        complete = len(state['items']) == len(state['offer_ids'])
        state['status'] = 'ok' if complete else 'partial'
        state['covered'] = len(state['items'])
        state['total'] = len(state['offer_ids'])
        state['shows'] = sum(v['shows'] for v in state['items'].values()) if complete else None
        state['views'] = sum(v['views'] for v in state['items'].values()) if complete else None
    except (OSError, ValueError, KeyError):
        state.update(status='partial', reason='Статистика получена не полностью')
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(state, ensure_ascii=False))
    pending.replace(path)
    return state


def statistics_lines(snapshots, labels):
    lines = []
    for account, snapshot in snapshots.items():
        stats = snapshot.get('daily_statistics')
        if not stats:
            continue
        day = stats.get('date', '—')
        label = labels.get(account, account)
        if stats.get('status') == 'ok':
            lines.append(f'{label}: показы {stats["shows"]}, просмотры {stats["views"]} · {day}')
        else:
            lines.append(f'⚪ {label}: статистика за {day} неполная ({stats.get("covered",0)}/{stats.get("total","—")} объявлений); итог не рассчитан')
    return lines


def install(m, client_name):
    """Adapters for the three existing multi-project engines; preserve parsers."""
    original_download = m.download_sources
    original_analyze = m.analyze_directory
    original_compare = getattr(m,'compare_master_to_feed',None)
    original_cross = getattr(m,'compare_feeds',None)

    def compare(master,feed):
        result=original_compare(master,feed)
        result.update(height_profile(master,feed))
        return result

    def compare_cross(feeds):
        result=original_cross(feeds)
        groups=cross_height_profile(feeds)
        result['floors_total_groups']=groups
        # Without a client baseline, differing building/section granularity is
        # a review note, not proof that an XML is wrong. Preserve prior status.
        return result

    if original_compare is not None:m.compare_master_to_feed=compare
    if original_cross is not None:m.compare_feeds=compare_cross

    def cards(cache_dir, statuses, category, feeds, apis):
        results = {}
        for project, sources in m.PLATFORM_CARDS.get(category, {}).items():
            results[project] = {}
            for source, url in sources.items():
                if source in API_SOURCES:
                    continue
                feed = feeds.get(project, {}).get(source)
                expected = len(feed) if feed is not None else None
                state = statuses.get('cards', {}).get(category, {}).get(project, {}).get(source, {})
                count = None
                if state.get('status') == 'OK':
                    try:
                        count = extract_count(source, m._card_path(cache_dir,category,project,source).read_text())
                    except OSError:
                        pass
                status = 'not_verified' if count is None else ('ok' if expected == count else 'error')
                results[project][source] = {'label':m.FEED_LABELS[source], 'url':url,
                    'feed_count':expected, 'offer_count':count, 'status':status,
                    'check_method':'public_page', 'reason':state.get('reason') if count is None else None,
                    'checked_at':state.get('checked_at')}
        return results

    def download(cache_dir):
        statuses = original_download(cache_dir)
        websites=statuses.setdefault('websites',{})
        for project,url in getattr(m,'WEBSITES',{}).items():
            if project in websites:
                continue
            state=m._capture_download(url,cache_dir/('website-'+project+'.html'),attempts=1)
            # A responding homepage is not a verified catalogue count.
            websites[project]={**state,'lot_count':None,'url':url}
            if url == 'https://ddoomm.moscow/apartments' and state.get('status') == 'OK':
                from website_catalogues import download_chasovaya_catalogue
                try:
                    document=(cache_dir/('website-'+project+'.html')).read_text()
                    websites[project]=download_chasovaya_catalogue(url,document)
                except (OSError, ValueError):
                    websites[project].update(status=m.SOURCE_UNAVAILABLE,
                        reason='Каталог сайта не удалось получить целиком; число квартир не подтверждено')
        for category in m.CATEGORIES:
            for account, names in m.API_SECRETS[category].items():
                if not account.startswith('cian'):
                    continue
                if getattr(m, 'API_ACCOUNTS', {}).get(account, {}).get('enabled') is False:
                    continue
                path = m._api_path(cache_dir,category,account)
                snapshot = m._read_json(path)
                snapshot['daily_statistics'] = daily_cian(cache_dir, category+'-'+account,
                                                          os.environ.get(names[0]), budget=90)
                path.write_text(json.dumps(snapshot,ensure_ascii=False))
                statuses['apis'][category][account] = snapshot
        (cache_dir/'source-statuses.json').write_text(json.dumps(statuses,ensure_ascii=False))
        return statuses

    def analyze(cache_dir):
        report = original_analyze(cache_dir)
        for category_key,category in report['categories'].items():
            for project,value in category['projects'].items():
                project_spec = getattr(m, 'DATA', {}).get('projects', {}).get(project, {})
                if project_spec.get('missing_card_sources'):
                    value['status'] = category['status'] = 'error'
                value.setdefault('website',report.get('source_status',{}).get('websites',{}).get(project,{}))
                if 'website' in project_spec and not project_spec['website']:
                    value['website'] = {'status': 'not_applicable', 'lot_count': None,
                                        'reason': 'У проекта нет сайта по данным анкеты'}
                site=value['website']
                if site.get('status') == 'OK' and 'ids' in site:
                    lots,_=m._master_for(cache_dir,report.get('source_status',{}),category_key,project)
                    if lots is not None:
                        master_ids={lot.external_id for lot in lots if lot.external_id}
                        site_ids=set(site['ids'])
                        site['id_comparison']={'missing_from_website':len(master_ids-site_ids),
                                               'extra_on_website':len(site_ids-master_ids)}
                for source,summary in value['feeds'].items():
                    if summary.get('status')!='OK':continue
                    lots,_=m.FEED_PARSERS[source](m._feed_path(cache_dir,category_key,project,source),project=project,category=category_key)
                    invalid=invalid_floor_count(lots)
                    summary['invalid_floor_above_building']=invalid
                    if invalid:
                        value['status']='error'
                        category['status']='error'
            if any(c.get('status') != 'ok' for cards in category.get('platform_cards',{}).values() for c in cards.values()):
                category['status'] = 'error'
        report['overall_status'] = 'error' if any(c['status'] != 'ok' for c in report['categories'].values()) else 'ok'
        return report

    def render(report, compact=False):
        e = lambda value: html.escape(str(value),quote=False)
        count = lambda value: '—' if value is None else str(value)
        icon = '🟢' if report['overall_status'] == 'ok' else '🔴'
        lines = [f'{icon} <b>{e(client_name)} — контроль выгрузок и площадок</b>']
        for key, category in report['categories'].items():
            lines.extend(['',f'<b>{e(m.CATEGORIES[key].upper())}</b>'])
            for project_key, project in category['projects'].items():
                master = project['master']
                master_label=master.get('label') or 'Материнский источник'
                lines.extend(['',f'<b>{e(m.PROJECTS[project_key])}</b>', '<i>Основной источник</i>',
                    e(f'{master_label} (эталон): квартир к выгрузке — {count(master.get("lot_count"))}'),
                    e('Наши XML: '+m._feed_counts_line(project['feeds']))])
                if master.get('status') == m.SOURCE_UNAVAILABLE:
                    lines.append(e('🔴 Эталон недоступен: '+master.get('reason','нет данных')))
                for note in getattr(m, 'DATA', {}).get('projects', {}).get(project_key, {}).get('onboarding_notes', []):
                    lines.append(e('⚪ ' + note))
                if master.get('excluded_reserved'):
                    lines.append(f'Брони: {master["excluded_reserved"]} — отдельно, не ожидаются в рекламе')
                if master.get('missing_building_height_count'):
                    lines.append(e(f'⚪ В эталоне не указана этажность: {master["missing_building_height_count"]} квартир; сверка этажности не выполнена'))
                if master.get('duplicate_ids') or master.get('missing_id_count'):
                    lines.append(e(f'🔴 Эталон: повторяющихся ID {len(master.get("duplicate_ids",[]))}, записей без ID {master.get("missing_id_count",0)}'))
                comparison_groups = {}
                for source, comparison in project['master_vs_feeds'].items():
                    if comparison.get('status') == m.SOURCE_UNAVAILABLE:
                        text = '🔴 '+m.FEED_LABELS[source]+' ↔ эталон: сравнение не выполнено'
                    else:
                        missing = len(comparison.get('missing_from_feed',[]))
                        extra = len(comparison.get('extra_in_feed',[]))
                        fields = sum(non_height_counts(comparison).values())
                        core_problem=bool(missing or extra or fields or comparison.get('duplicate_ids') or comparison.get('missing_id_count'))
                        mark = '🔴' if core_problem else '✅'
                        suffix = ' (выборочный XML)' if getattr(m,'DATA',{}).get('projects',{}).get(project_key,{}).get('feed_modes',{}).get(source) == 'subset' else ''
                        text = f'{mark} {m.FEED_LABELS[source]} ↔ эталон: не выгружено {missing}, лишних {extra}, ошибок полей {fields}{suffix}'
                        details=', '.join(f'{m.FIELD_LABELS.get(k,k)} {v}' for k,v in non_height_counts(comparison).items())
                        if details: text+=' ('+details+')'
                        if comparison.get('duplicate_ids') or comparison.get('missing_id_count'):
                            text+=f'; дубли ID {len(comparison.get("duplicate_ids",[]))}, без ID {comparison.get("missing_id_count",0)}'
                    signature = (comparison.get('status') == m.SOURCE_UNAVAILABLE, tuple(comparison.get('missing_from_feed',[])),
                                 tuple(comparison.get('extra_in_feed',[])),
                                 tuple(sorted(non_height_counts(comparison).items())),
                                 tuple(comparison.get('duplicate_ids',[])),comparison.get('missing_id_count',0))
                    comparison_groups.setdefault(signature,[]).append((source,text))
                for group in comparison_groups.values():
                    source,text=group[0]
                    label=', '.join(m.FEED_LABELS[s] for s,_ in group)
                    if len(group)==len(project['master_vs_feeds']): label='Все наши XML'
                    text=text.replace(m.FEED_LABELS[source]+' ↔',label+' ↔',1)
                    lines.append(e(text))
                lines.extend(e(line) for line in height_lines(project['master_vs_feeds'],m.FEED_LABELS))
                for source,summary in project['feeds'].items():
                    invalid=summary.get('invalid_floor_above_building',0)
                    if invalid:lines.append(e(f'🔴 {m.FEED_LABELS[source]}: этаж квартиры выше этажности здания — {invalid} объявлений; ошибка XML'))
                cross = non_height_counts(project.get('cross_feed',{}))
                if cross:
                    lines.append(e('🔴 Между нашими XML: '+', '.join(f'{m.FIELD_LABELS.get(k,k)} {v}' for k,v in cross.items())))
                if master.get('status') == m.SOURCE_UNAVAILABLE:
                    lines.extend(e(line) for line in cross_height_lines(project.get('cross_feed',{}).get('floors_total_groups',[]),m.FEED_LABELS))
                lines.extend(e(line) for line in m._marketing_lines(project))
                site = project.get('website',{})
                if not compact: lines.extend(['','<i>Сайт (справочно, не эталон)</i>'])
                if site.get('lot_count') is not None:
                    value = site['lot_count']
                    lines.append(f'Сайт (справочно): {value} квартир')
                    if master.get('lot_count') is not None and not compact:
                        ids=site.get('id_comparison')
                        if ids is not None:
                            lines.append(f'ℹ️ Сайт ↔ эталон по ID: нет на сайте {ids["missing_from_website"]}, сверх эталона {ids["extra_on_website"]}')
                        else:
                            lines.append(f'ℹ️ Разница с эталоном по количеству: {value-master["lot_count"]:+d}; состав по ID здесь не проверен')
                    if site.get('data_origin') and not compact:
                        lines.append('ℹ️ Сайт и каталог клиента используют общий backend; сайт не заменяет эталон')
                else:
                    if site.get('status') == 'not_applicable':
                        lines.append('ℹ️ Сайта у проекта нет; проверка сайта не применяется')
                    elif site.get('status') == m.SOURCE_UNAVAILABLE:
                        lines.append(e('⚪ Сайт недоступен: '+site.get('reason','сетевая ошибка')+'; число квартир не проверено'))
                    else:
                        lines.append('⚪ Независимый счётчик квартир сайта не подтверждён')
                public = category.get('platform_cards',{}).get(project_key,{})
                if public and not compact:
                    lines.extend(['','<i>Площадки — факт размещения по карточкам ЖК</i>'])
                for source, card in public.items():
                    actual, expected = card.get('offer_count'),card.get('feed_count')
                    label=m.FEED_LABELS[source]
                    if actual is None:
                        reason=card.get('reason') or 'счётчик не найден'
                        lines.append(e(f'⚪ {label}: публикация не подтверждена — {reason}; XML {count(expected)}'))
                    elif actual == expected:
                        lines.append(e(f'✅ {label}: {actual} предложений = XML {expected}'))
                    else:
                        difference=f'{actual-expected:+d}' if expected is not None else '—'
                        lines.append(e(f'🔴 {label}: на площадке {actual}, XML {count(expected)}; разница {difference}'))
            lines.extend(['','<i>Площадки — факт размещения по API</i>'])
            for account, api in category.get('api_checks',{}).items():
                label=m.FEED_LABELS[account]
                names=', '.join(m.PROJECTS[p] for p in api.get('active_projects',[]))
                actual, expected=api.get('actual_platform_count'),api.get('expected_feed_count')
                mark='✅' if api.get('comparison_status') == 'ok' else '🔴'
                value=f'{actual-expected:+d}' if actual is not None and expected is not None else 'не проверено'
                lines.append(e(f'{mark} {label} API ({names}): всего в кабинете {count(actual)}, наши XML {count(expected)}; разница {value}'))
                if actual is None and api.get('reason'):
                    lines.append(e('🔴 '+label+': '+api['reason']))
                if api.get('import_error_count'):
                    lines.append(e(f'🔴 {label}: ошибок импорта {api["import_error_count"]}'))
            stat_labels={a:m.FEED_LABELS[a]+' · '+', '.join(m.PROJECTS[p] for p in v.get('active_projects',[]))
                         for a,v in category.get('api_checks',{}).items()}
            stats=statistics_lines(category.get('api_checks',{}),stat_labels)
            if stats:
                lines.extend(['','<i>Статистика за вчера · Циан</i>', 'По опубликованным XML-объявлениям; не влияет на статус выгрузки.'])
                lines.extend(e(line) for line in stats)
        lines.extend(['',f'<i>Проверено: {e(m._checked_label(report["checked_at"]))}</i>'])
        message='\n'.join(lines)
        if len(message)>3900 and not compact:
            return render(report,True)
        if len(message)>4096:
            raise ValueError('Report exceeds Telegram limit; never silently truncate checks')
        return message

    m._extract_card_count=extract_count
    m.analyze_cards=cards
    m._card_has_error=lambda results:any(c.get('status')!='ok' for cards in results.values() for c in cards.values())
    m.download_sources=download
    m.analyze_directory=analyze
    m.format_telegram=render
