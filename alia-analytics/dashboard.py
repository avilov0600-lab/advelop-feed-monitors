"""Read only the ALIA real-estate-bases block and the explicit action journal."""
import datetime as dt
import csv
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

SHEET_ID = '1Cy7npgBg0giH4X_U96hCD8l6DhuV1j_i3DxeEz6Vozs'
JOURNAL = 'Базы — журнал действий'
MONTHS = 'январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь'.split()
HEADERS = {
    'calls_plan': 'Звонки План на месяц', 'calls_plan_date': 'Звонки План на вчера',
    'calls': 'Звонки Факт на вчера', 'budget_plan': 'Бюджет План на месяц',
    'budget_plan_date': 'Бюджет План на вчера', 'spend': 'Бюджет Факт на вчера',
    'gross': 'ВАЛ',
}
# Explicit, reviewed source bounds. A new month must not silently read another tab.
PUBLIC_MONTHS = {
    '2026-11': {'gid': 1133548286, 'title': 'ДБ - Ноябрь 2026 ',
                'header': 'A10:BL10', 'body': 'A15:BL23', 'date': 'D6:E6'},
}


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def date_value(value):
    if isinstance(value, (int, float)):
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=value)).date()
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%Y %H:%M'):
        try:
            return dt.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if value >= 0 else None
    if isinstance(value, str):
        cleaned = re.sub(r'[\s\u00a0\u202f₽]', '', value).replace(',', '.')
        if re.fullmatch(r'\d+(?:\.\d+)?', cleaned):
            return float(cleaned)
    return None  # A dash, formula error, empty cell and text are not zero.


def at(row, col):
    return row[col] if col < len(row) else None


def parse_month(rows, *, month, yesterday, journal_rows):
    """Bind by full metric headings, never by hardcoded October/November offsets."""
    headings = next((r for r in rows if norm('Звонки План на месяц') in map(norm, r)), None)
    if not headings:
        raise ValueError('Не найдены заголовки показателей ДБ')
    columns = {}
    for key, label in HEADERS.items():
        indexes = [i for i, v in enumerate(headings) if norm(v) == norm(label)]
        if len(indexes) != 1:
            raise ValueError('Изменилась структура показателей ДБ')
        columns[key] = indexes[0]
    source_columns = [i for i, v in enumerate(headings) if norm(v) == 'источник']
    if len(source_columns) != 1:
        raise ValueError('Не определена колонка источника ДБ')
    source_col = source_columns[0]
    starts = [i for i, r in enumerate(rows) if norm(at(r, source_col)) == 'базы недвижимости']
    if len(starts) != 1:
        raise ValueError('Не определены границы блока Базы недвижимости')
    start = starts[0]
    total = {k: number(at(rows[start], c)) for k, c in columns.items()}
    # Group rows have no placement/tariff, unlike the individual platform rows.
    details = []
    allowed = ('cian', 'циан', 'avito', 'авито', 'яндекс', 'домклик', 'domclick', 'm2', 'м2')
    for row in rows[start + 1:]:
        name = str(at(row, source_col) or '').strip()
        if not name:
            break
        if not norm(name).startswith(allowed):
            break
        details.append({'name': name, 'tariff_plan': at(row, source_col + 2),
                        **{k: number(at(row, c)) for k, c in columns.items()}})
    if not details:
        raise ValueError('В блоке нет строк площадок')
    as_of = None
    for row in rows[:10]:
        for i, value in enumerate(row[:-1]):
            if norm(value) == 'вчера':
                as_of = date_value(row[i + 1])
    if as_of != yesterday:
        raise ValueError('Дата расчёта ДБ не соответствует вчерашнему дню')
    meta = journal_rows[1] if len(journal_rows) > 1 else []
    calls_checked = date_value(at(meta, 1))
    spend_checked = date_value(at(meta, 4))
    plan_approved = date_value(at(meta, 7))
    plan_month = date_value(at(meta, 9))
    return {
        'status': 'ok', 'month': month, 'as_of': as_of.isoformat(), 'total': total,
        'platforms': details, 'calls_verified': bool(calls_checked and calls_checked >= as_of),
        'spend_verified': bool(spend_checked and spend_checked >= as_of),
        'plan_approved': bool(plan_approved and plan_approved <= yesterday + dt.timedelta(days=1)
                              and plan_month and (plan_month.year, plan_month.month) ==
                              (as_of.year, as_of.month)),
        'actions': parse_actions(journal_rows, yesterday),
    }


def parse_actions(rows, yesterday):
    actions = []
    for row in rows[4:]:
        day = date_value(at(row, 0))
        if not day or not yesterday - dt.timedelta(days=6) <= day <= yesterday:
            continue
        actions.append({'date': day.isoformat(), 'platform': str(at(row, 1) or ''),
                        'action': str(at(row, 2) or ''), 'before': str(at(row, 3) or ''),
                        'after': str(at(row, 4) or ''), 'status': str(at(row, 5) or ''),
                        'comment': str(at(row, 6) or '')})
    return sorted(actions, key=lambda a: a['date'], reverse=True)


def public_range(gid, cell_range):
    query = urllib.parse.urlencode({'gid': gid, 'range': cell_range, 'headers': 0, 'tqx': 'out:csv'})
    request = urllib.request.Request(f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?{query}')
    with urllib.request.urlopen(request, timeout=20) as response:
        if 'csv' not in response.headers.get('Content-Type', ''):
            raise ValueError('Google не вернул CSV выбранного диапазона')
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Ответ Google превышает размер выбранного диапазона')
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    # Visualization CSV always has a column-label row, even with headers=0.
    if not rows or any(rows[0]):
        raise ValueError('Изменилась структура CSV Google')
    return rows[1:]


def collect_public(yesterday):
    cfg = PUBLIC_MONTHS.get(yesterday.strftime('%Y-%m'))
    if not cfg:
        return {'status': 'unavailable', 'reason': 'нужны диапазоны Базы недвижимости для нового месяца ДБ'}
    try:
        date_rows = public_range(cfg['gid'], cfg['date'])
        headings = public_range(cfg['gid'], cfg['header'])
        body = public_range(cfg['gid'], cfg['body'])
        meta = public_range(20261102, 'A2:J2')
        actions = public_range(20261102, 'A5:J205')
        journal = [[], meta[0] if meta else [], [], []] + actions
        if len(date_rows) != 1 or len(headings) != 1 or len(body) != 9:
            raise ValueError('Изменилась структура CSV Google')
        rows = date_rows + headings + body
        report = parse_month(rows, month=cfg['title'], yesterday=yesterday, journal_rows=journal)
        report['access'] = 'existing_link_scoped_csv'
        return report
    except Exception as error:
        return {'status': 'unavailable', 'reason':
                str(error) if isinstance(error, ValueError) else 'не удалось прочитать выбранные диапазоны ДБ'}


def collect(credentials_path, yesterday):
    if not credentials_path or not Path(credentials_path).is_file():
        return collect_public(yesterday)
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
        credentials.refresh(Request())
        base = 'https://sheets.googleapis.com/v4/spreadsheets/' + SHEET_ID

        def get(path):
            req = urllib.request.Request(base + path,
                                         headers={'Authorization': 'Bearer ' + credentials.token})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)

        sheets = get('?fields=sheets.properties')['sheets']
        title = f'ДБ - {MONTHS[yesterday.month - 1]} {yesterday.year}'
        matches = [s['properties']['title'] for s in sheets
                   if norm(s['properties']['title']) == norm(title)]
        if len(matches) != 1:
            raise ValueError('Не найдена вкладка отчётного месяца')
        prop = next(s['properties'] for s in sheets if s['properties']['title'] == matches[0])
        count = min(100, prop['gridProperties']['columnCount'])
        col = ''
        while count:
            count, digit = divmod(count - 1, 26)
            col = chr(65 + digit) + col
        end = min(70, prop['gridProperties']['rowCount'])
        ranges = [f"'{matches[0]}'!A1:{col}{end}", f"'{JOURNAL}'!A1:J205"]
        data = get('/values:batchGet?' + urllib.parse.urlencode({
            'ranges': ranges, 'valueRenderOption': 'UNFORMATTED_VALUE'}, doseq=True))
        values = [v.get('values', []) for v in data['valueRanges']]
        return parse_month(values[0], month=matches[0], yesterday=yesterday, journal_rows=values[1])
    except Exception as error:
        # No response bodies/URLs/credential data in logs or Telegram.
        reason = str(error) if isinstance(error, ValueError) and str(error).startswith(
            ('Не ', 'Изменилась', 'Дата ', 'В блоке')) else 'ошибка доступа или структуры ДБ'
        return {'status': 'unavailable', 'reason': reason}
