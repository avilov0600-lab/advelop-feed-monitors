"""Pure presentation of saved monitoring results; never performs requests or changes state."""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import html
import re
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
UNAVAILABLE = {'SOURCE_UNAVAILABLE', 'source_unavailable', 'unavailable', 'not_configured'}
FIELD_NAMES = {'area': 'Площадь', 'price': 'Цена', 'deal': 'Цена сделки',
               'rooms': 'Количество комнат', 'floor': 'Этаж квартиры',
               'apartment': 'Номер квартиры', 'floors_total': 'Этажность здания'}


def checked_time(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp.astimezone(MOSCOW) if stamp.tzinfo else None
    except (ValueError, TypeError):
        return None


def is_morning(report):
    """The 09:00 Moscow report, including normal runner delays before noon."""
    stamp = checked_time(report.get('checked_at'))
    return stamp is not None and 9 <= stamp.hour < 12


def brief_reason(reason, code=None):
    text = str(reason or '').strip()
    low = text.lower()
    if code in (401, 403) or re.search(r'\b(?:401|403)\b', text):
        return 'доступ ограничен'
    if code == 404 or '404' in low:
        return 'страница не найдена'
    if code == 429 or '429' in low:
        return 'слишком много запросов'
    if code is not None and code >= 500:
        return 'ошибка сервера'
    if 'таймаут' in low or 'timeout' in low or 'время ожидания' in low:
        return 'превышено время ожидания'
    if 'dns' in low:
        return 'не найден адрес сайта'
    if any(word in low for word in ('сертификат', 'ssl', 'https')):
        return 'ошибка сертификата'
    if 'соединени' in low:
        return 'нет соединения'
    if 'перенаправлен' in low:
        return 'ошибка перенаправления'
    if 'счётчик' in low or 'счетчик' in low:
        return 'количество объявлений не определено'
    if not text:
        return 'нет подтверждённых данных'
    # Existing reasons are public diagnostic text, never request URLs or secrets.
    return text.split(' — ', 1)[0].rstrip('.;')


def website_header(report, projects):
    observations = report.get('site_availability', {})
    lines = []
    healthy = []
    for key, observation in observations.items():
        name = projects.get(key, key)
        if observation.get('status') == 'available':
            healthy.append(name)
        elif observation.get('status') == 'unavailable':
            prefix = '❌ Сайт недоступен'
            if len(observations) > 1:
                prefix += ' (' + name + ')'
            lines.append(prefix + ' — ' + brief_reason(observation.get('reason'), observation.get('status_code')))
        else:
            suffix = ' (' + name + ')' if len(observations) > 1 else ''
            lines.append('⚪ Доступность сайта не проверена' + suffix)
    if healthy:
        if len(observations) == 1:
            lines.append('✅ Сайт доступен')
        elif len(healthy) == len(observations):
            lines.append('✅ Сайты всех проектов доступны')
        else:
            lines.append('✅ Сайт доступен: ' + ', '.join(healthy))
    if not observations:
        applicable = [p for category in report.get('categories', {}).values()
                      for p in category.get('projects', {}).values()
                      if p.get('website', {}).get('status') != 'not_applicable']
        lines.append(('⚪ Доступность сайта не проверена' if len(applicable) == 1
                      else '⚪ Доступность сайтов не проверена') if applicable
                     else 'ℹ️ Сайтов у проектов нет; проверка сайта не применяется')
    return lines


def _count(value):
    return 'нет данных' if value is None else str(value)


def _source_names(sources, labels):
    return ', '.join(dict.fromkeys(labels.get(source, source) for source in sources))


def inventory_lines(project, labels):
    master = project.get('master', {})
    site = project.get('website', {})
    expected = master.get('lot_count') if master.get('status') not in UNAVAILABLE else None
    website = site.get('lot_count') if site.get('status') not in UNAVAILABLE else None
    counts = defaultdict(list)
    unavailable = []
    feeds = project.get('feeds', {})
    for source, feed in feeds.items():
        value = feed.get('lot_count') if feed.get('status') not in UNAVAILABLE else None
        if value is None:
            unavailable.append(source)
        else:
            counts[value].append(source)
    same = len(counts) == 1 and not unavailable and bool(feeds)
    feed_value = next(iter(counts)) if same else None
    if expected is not None and website == expected and feed_value == expected:
        lines = [f'Источник, сайт и наши {len(feeds)} фида: по {expected} квартир.']
    else:
        parts = ['Квартиры: источник — ' + _count(expected)]
        if website is not None:
            parts.append('сайт — ' + str(website))
        if same:
            parts.append(f'наши фиды ({len(feeds)}) — по {feed_value}')
        lines = ['; '.join(parts) + '.']
        if not same and counts:
            lines.append('Фиды: ' + '; '.join(_source_names(sources, labels) + ' — ' + str(value)
                                            for value, sources in counts.items()) + ' квартир.')
    if master.get('status') in UNAVAILABLE or expected is None:
        lines.append('🔴 Источник клиента недоступен — ' + brief_reason(master.get('reason')) + '; сверка не выполнена.')
    if unavailable:
        lines.append('🔴 Фиды недоступны: ' + _source_names(unavailable, labels) +
                     f'; доступны {len(feeds)-len(unavailable)} из {len(feeds)}, сверка не выполнена.')
    if site.get('status') == 'not_applicable':
        lines.append('ℹ️ Сайта у проекта нет; проверка сайта не применяется.')
    elif website is None:
        lines.append('⚪ Количество квартир на сайте не определено.')
    else:
        ids = site.get('id_comparison')
        if ids and (ids.get('missing_from_website') or ids.get('extra_on_website')):
            pieces = []
            if ids.get('missing_from_website'):
                pieces.append('отсутствуют на сайте ' + str(ids['missing_from_website']))
            if ids.get('extra_on_website'):
                pieces.append('сверх источника ' + str(ids['extra_on_website']))
            lines.append('ℹ️ Квартиры сайта: ' + ', '.join(pieces) + '.')
        elif not ids and expected is not None and website != expected:
            lines.append(f'ℹ️ На сайте на {abs(website-expected)} квартир ' +
                         ('больше' if website > expected else 'меньше') + ', чем в источнике; состав не подтверждён.')
    reserved = master.get('excluded_reserved')
    if reserved:
        lines.append(f'Брони: {reserved}; в рекламе не учитываются.')
    if master.get('duplicate_ids') or master.get('missing_id_count'):
        lines.append(f'🔴 Источник: повторяющихся ID — {len(master.get("duplicate_ids", []))}; без ID — {master.get("missing_id_count", 0)}.')
    missing_height = master.get('missing_building_height_count')
    if missing_height:
        lines.append(f'⚪ В источнике не указана этажность для {missing_height} квартир; сверка этажности неполная.')
    return lines


def comparison_lines(project, labels, field_labels=None, feed_modes=None):
    comparisons = project.get('master_vs_feeds', {})
    labels_for_fields = {**(field_labels or {}), **FIELD_NAMES}
    missing_groups = defaultdict(list)
    field_groups = defaultdict(lambda: defaultdict(list))
    heights = defaultdict(list)
    missing_heights = defaultdict(list)
    lines = []
    has_issues = False
    for source, comparison in comparisons.items():
        if comparison.get('status') in UNAVAILABLE:
            has_issues = True
            # Source/feed failure is already explained with the inventory.
            continue
        missing = tuple(comparison.get('missing_from_feed', []))
        extra = tuple(comparison.get('extra_in_feed', []))
        if missing or extra:
            missing_groups[(missing, extra, (feed_modes or {}).get(source) == 'subset')].append(source)
            has_issues = True
        for field, count in comparison.get('field_mismatch_counts', {}).items():
            if count and field != 'floors_total':
                field_groups[field][count].append(source)
                has_issues = True
        pairs = tuple(sorted({(x['master'], x['feed']) for x in comparison.get('floors_total_groups', [])}))
        if pairs:
            heights[pairs].append(source)
            has_issues = True
        elif comparison.get('field_mismatch_counts', {}).get('floors_total'):
            lines.append('🔴 Этажность здания не совпадает с источником: ' + labels.get(source, source) + '.')
            has_issues = True
        unknown = comparison.get('floors_total_coverage', {}).get('missing_in_feed', 0)
        if unknown:
            missing_heights[unknown].append(source)
            has_issues = True
        if comparison.get('duplicate_ids') or comparison.get('missing_id_count'):
            lines.append(f'🔴 {labels.get(source, source)}: дубли ID — {len(comparison.get("duplicate_ids", []))}; без ID — {comparison.get("missing_id_count", 0)}.')
            has_issues = True
    for (missing, extra, subset), sources in missing_groups.items():
        label = 'Во всех наших фидах' if len(sources) == len(comparisons) else 'Фиды ' + _source_names(sources, labels)
        pieces = []
        if missing:
            pieces.append('не хватает квартир: ' + str(len(missing)))
        if extra:
            pieces.append('лишних квартир: ' + str(len(extra)))
        lines.append('🔴 ' + label + ' — ' + '; '.join(pieces) + (' (выборочная выгрузка).' if subset else '.'))
    cross = project.get('cross_feed', {})
    cross_fields = {k:v for k,v in cross.get('field_mismatch_counts', {}).items() if v and k != 'floors_total'}
    for field in dict.fromkeys([*field_groups, *cross_fields]):
        pieces = ['с источником — ' + str(n) + ' (' + _source_names(sources, labels) + ')'
                  for n, sources in field_groups[field].items()]
        if field in cross_fields:
            pieces.append('между фидами — ' + str(cross_fields[field]))
        lines.append('🔴 ' + labels_for_fields.get(field, field) + ': ' + '; '.join(pieces) + ' расхождений.')
    for pairs, sources in heights.items():
        detail = '; '.join(f'в источнике {a}, в фиде {b}' for a,b in pairs)
        lines.append('🔴 Этажность здания: ' + _source_names(sources, labels) + ' — ' + detail + ' этажей.')
    for count, sources in missing_heights.items():
        lines.append('⚪ Этажность не указана: ' + _source_names(sources, labels) + f' — {count} квартир.')
    # Cross-feed ID differences are distinct evidence; do not hide equal-count ID swaps.
    cross_ids = [(s,v) for s,v in cross.get('id_differences', {}).items() if v.get('missing') or v.get('extra')]
    if cross_ids and not missing_groups:
        lines.append('🔴 Состав квартир различается между фидами: ' + _source_names([s for s,_ in cross_ids], labels) + '.')
    if project.get('master', {}).get('status') in UNAVAILABLE:
        for group in cross.get('floors_total_groups', []):
            detail = ', '.join(labels.get(s,s) + ': ' + str(v) for s,v in group['values'].items())
            lines.append('⚪ Этажность между фидами: ' + detail + '; без источника клиента правильное значение не подтверждено.')
    for source, feed in project.get('feeds', {}).items():
        if feed.get('invalid_floor_above_building'):
            lines.append(f'🔴 {labels.get(source, source)}: этаж квартиры выше этажности здания — {feed["invalid_floor_above_building"]} объявлений.')
            has_issues = True
    if comparisons and not has_issues and not cross_fields and not cross_ids:
        lines.append('✅ Состав и параметры фидов совпадают с источником.')
    return lines


def marketing_lines(project, labels):
    issues = defaultdict(list)
    unchecked = []
    for source, feed in project.get('feeds', {}).items():
        if feed.get('status') in UNAVAILABLE or 'marketing_issues' not in feed:
            unchecked.append(source)
        for issue in feed.get('marketing_issues', []):
            issues[str(issue)].append(source)
    lines = ['🔴 ' + _source_names(sources, labels) + ' · текст акции: ' + issue
             for issue, sources in issues.items()]
    if unchecked:
        lines.append('⚪ Акции не проверены: ' + _source_names(unchecked, labels) + '.')
    return lines


def publication_lines(cards, labels):
    healthy = defaultdict(list)
    unknown = defaultdict(list)
    lines = []
    for source, card in cards.items():
        if source in {'cian', 'avito'}:
            continue
        actual, expected = card.get('offer_count'), card.get('feed_count')
        label = labels.get(source, source)
        if card.get('check_method') in {'feed_proxy', 'feed_assumption'}:
            reference = card.get('reference_count')
            if reference is None:
                others = set(card.get('other_feed_counts', {}).values())
                reference = next(iter(others)) if len(others) == 1 else None
            if expected is not None and reference is not None and reference != expected:
                lines.append(f'🔴 {label}: в фиде {expected}, в остальных фидах {reference}; размещение не подтверждено.')
            else:
                lines.append(f'⚪ {label}: фид — {_count(expected)}; размещение не подтверждено.')
        elif actual is None:
            unknown[brief_reason(card.get('reason'))].append(source)
        elif expected is None:
            lines.append(f'⚪ {label}: опубликовано {actual}; фид недоступен, сравнение не выполнено.')
        elif card.get('status') == 'ok' and actual == expected:
            healthy[actual].append(source)
        elif actual == expected:
            lines.append(f'⚪ {label}: опубликовано {actual}, количество совпадает; проверка не подтверждена.')
        else:
            lines.append(f'🔴 {label}: опубликовано {actual}, в фиде {expected}; ' +
                         ('лишних' if actual > expected else 'не хватает') + f' объявлений: {abs(actual-expected)}.')
    lines[:0] = ['✅ ' + _source_names(sources, labels) + f': опубликовано {count} — совпадает с фидами.'
                 for count, sources in healthy.items()]
    lines += ['⚪ ' + _source_names(sources, labels) + ': размещение не подтверждено — ' + reason + '.'
              for reason, sources in unknown.items()]
    return lines


def api_lines(accounts, labels, projects):
    grouped = defaultdict(list)
    lines = []
    for account, api in accounts.items():
        key = (tuple(api.get('active_projects', [])), api.get('actual_platform_count'),
               api.get('expected_feed_count'), api.get('comparison_status'),
               api.get('import_error_count', 0), api.get('reason') if api.get('actual_platform_count') is None else None)
        grouped[key].append(account)
    for (scope, actual, expected, status, imports, reason), names in grouped.items():
        label = _source_names(names, labels)
        scope_label = ', '.join(projects.get(p,p) for p in scope)
        prefix = label + (' (' + scope_label + ')' if scope_label else ' (кабинет)')
        if actual is None:
            lines.append('⚪ ' + prefix + ': API не проверено — ' + brief_reason(reason) + '.')
            continue
        if expected is None:
            lines.append(f'⚪ {prefix}: опубликовано {actual}; фиды неполные, сравнение не выполнено.')
        elif status == 'ok' and actual == expected:
            lines.append(f'✅ {prefix}: опубликовано {actual} — совпадает с фидами.')
        elif actual != expected:
            lines.append(f'🔴 {prefix}: опубликовано {actual}, в фидах {expected}; ' +
                         ('лишних' if actual > expected else 'не хватает') + f' объявлений: {abs(actual-expected)}.')
        elif imports:
            lines.append(f'🔴 {prefix}: количество {actual} совпадает, но ошибок импорта: {imports}.')
        else:
            lines.append(f'⚪ {prefix}: количество {actual} совпадает, но проверка API не пройдена.')
        if imports and actual != expected:
            lines.append(f'🔴 {label}: ошибок импорта — {imports}.')
    return lines


def statistics_lines(accounts, labels, projects):
    lines = []
    for account, api in accounts.items():
        stats = api.get('daily_statistics')
        if not stats:
            continue
        names = [projects.get(p,p) for p in api.get('active_projects', [])]
        label = labels.get(account, account) + (' (' + ', '.join(names) + ')' if names else '')
        if stats.get('status') == 'ok' and stats.get('shows') is not None and stats.get('views') is not None:
            lines.append(f'{label}: {stats["shows"]} показов, {stats["views"]} просмотров · {stats.get("date", "—")}')
        else:
            lines.append(f'⚪ {label}: статистика за {stats.get("date", "—")} неполная; итог не рассчитан.')
    return lines


def render_report(report, client_name, projects, labels, *, field_labels=None, data=None,
                  html_mode=True, include_statistics=None):
    """Render one developer digest, all projects included, with no network calls."""
    labels = {'yandex_realty': 'Яндекс Недвижимость', **labels}
    escape = (lambda text: html.escape(str(text), quote=False)) if html_mode else str
    bold = (lambda text: '<b>' + escape(text) + '</b>') if html_mode else str
    lines = [escape(line) for line in website_header(report, projects)]
    mark = '🟢' if report.get('overall_status') == 'ok' else '🔴'
    lines += ['', bold(mark + ' ' + client_name)]
    notes_seen = set()
    for category in report.get('categories', {}).values():
        for project_key, project in category.get('projects', {}).items():
            lines += ['', bold(projects.get(project_key, project_key))]
            details = inventory_lines(project, labels)
            details += comparison_lines(project, labels, field_labels,
                                        (data or {}).get('projects', {}).get(project_key, {}).get('feed_modes'))
            details += marketing_lines(project, labels)
            details += publication_lines(category.get('platform_cards', {}).get(project_key, {}), labels)
            for note in (data or {}).get('projects', {}).get(project_key, {}).get('onboarding_notes', []):
                if note not in notes_seen:
                    details.append('⚪ ' + str(note))
                    notes_seen.add(note)
            lines.extend(escape(line) for line in details)
        apis = api_lines(category.get('api_checks', {}), labels, projects)
        if apis:
            lines += ['', bold('Площадки — по API')]
            lines.extend(escape(line) for line in apis)
        show_statistics = include_statistics if include_statistics is not None else is_morning(report)
        if show_statistics:
            stats = statistics_lines(category.get('api_checks', {}), labels, projects)
            if stats:
                lines += ['', bold('Циан — статистика за вчера')]
                lines.extend(escape(line) for line in stats)
    stamp = checked_time(report.get('checked_at'))
    checked = stamp.strftime('%d.%m.%Y, %H:%M МСК') if stamp else str(report.get('checked_at', '—'))
    lines += ['', escape('Проверено: ' + checked)]
    message = '\n'.join(lines)
    visible = html.unescape(re.sub(r'</?(?:b|i)>', '', message)) if html_mode else message
    if len(visible.encode('utf-16-le')) // 2 > 4096:
        raise ValueError('Concise report exceeds Telegram limit; never truncate findings')
    return message


def render_masters(report, m):
    """Normalize the MASTERS saved schema into presentation only; keep account scope."""
    comparison = report.get('master_vs_feeds', {})
    master = {**report.get('master', {}), 'lot_count': comparison.get('master_count', report.get('master', {}).get('lot_count'))}
    site = report.get('site_vs_master', {})
    website = {'status': site.get('status'), 'lot_count': site.get('site_count')}
    if site.get('status') not in UNAVAILABLE:
        website['id_comparison'] = {'missing_from_website': len(site.get('master_only', [])),
                                   'extra_on_website': len(site.get('site_only', []))}
    feeds = {key:dict(value) for key,value in report.get('feeds', {}).items()}
    for feed in feeds.values():
        stale = feed.get('stale_content', {}).get('july_discount', 0)
        feed['marketing_issues'] = [f'устаревшая скидка за июль — {stale} объявлений'] if stale else []
    project = {'master': master, 'website': website, 'feeds': feeds,
               'master_vs_feeds': comparison.get('sources', {}), 'cross_feed': report.get('cross_feed', {})}
    cards = {}
    accounts = {}
    for source, value in report.get('platform_cards', {}).items():
        if source in {'cian', 'avito'}:
            accounts[source] = {'active_projects': ['masters'], 'comparison_status': value.get('status'),
                                'actual_platform_count': value.get('offer_count'),
                                'expected_feed_count': value.get('feed_count'),
                                'reason': value.get('reason'), 'import_error_count': value.get('import_error_count', 0)}
        else:
            cards[source] = value
    if report.get('daily_statistics'):
        accounts.setdefault('cian', {'active_projects': ['masters']})['daily_statistics'] = report['daily_statistics']
    problems = bool(comparison.get('total_id_difference_count') or comparison.get('field_mismatch_counts')
                    or report.get('cross_feed', {}).get('field_mismatch_counts')
                    or any(feed.get('status') in UNAVAILABLE or feed.get('marketing_issues') for feed in feeds.values())
                    or any(card.get('status') != 'ok' for card in report.get('platform_cards', {}).values()))
    normalized = {'overall_status': 'error' if problems else 'ok', 'checked_at': report.get('checked_at'),
                  'site_availability': report.get('site_availability', {}),
                  'categories': {'residential': {'projects': {'masters': project},
                                                'platform_cards': {'masters': cards}, 'api_checks': accounts}}}
    return render_report(normalized, 'МАСТЕРС', {'masters': 'МАСТЕРС'}, m.FEED_LABELS,
                         field_labels=getattr(m, 'FIELD_LABELS', {}), html_mode=False)
