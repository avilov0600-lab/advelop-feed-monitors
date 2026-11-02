"""Compact factual commentary. Never infer a bid change or an action from a proposal."""
import html
import statistics


def esc(value):
    return html.escape(str(value))


def amount(value, money=False):
    if value is None:
        return 'нет данных'
    text = f'{value:,.0f}'.replace(',', ' ')
    return text + (' ₽' if money else '')


def pct(actual, plan):
    return f'{100 * actual / plan:.0f}%' if actual is not None and plan else 'не рассчитано'


def delta_auction(current, previous):
    if not previous or previous.get('status') != 'ok' or current.get('status') != 'ok':
        return None
    old = {str(x['offerId']): x for x in previous.get('items', [])}
    comparable = [(x, old[str(x['offerId'])]) for x in current['items'] if str(x['offerId']) in old]
    if not comparable:
        return None
    # Absent currentBet is not zero and cannot prove that our bid did not change.
    bids = [(a, b) for a, b in comparable
            if all(isinstance(x.get('currentBet'), (int, float))
                   and not isinstance(x.get('currentBet'), bool) for x in (a, b))]
    changed = sum(a['currentBet'] != b['currentBet'] for a, b in bids)
    moved = sum((a.get('page'), a.get('position')) != (b.get('page'), b.get('position'))
                for a, b in comparable)
    bid_text = (f'ставки изменились у {changed} из {len(bids)} лотов'
                if bids else 'изменение наших ставок не определено')
    return f'С прошлого среза: {bid_text}; позиции — у {moved} из {len(comparable)} сопоставленных лотов.'


def render(report, previous=None):
    day = report['date']
    lines = ['<b>ALIA · Базы недвижимости</b>', f'<i>Результаты по {day}; ставки — текущий срез {esc(report["observed_at"])}</i>']
    db = report['dashboard']
    lines += ['', '<b>План и результат</b>']
    if db.get('status') != 'ok':
        lines += ['⚪ ДБ: ' + esc(db.get('reason', 'данные не получены')) + '.',
                  'План, расход и целевые звонки пока не рассчитаны.']
    else:
        t = db['total']
        lines += [f'Целевые звонки: {amount(t["calls"])} / {amount(t["calls_plan_date"])} к дате '
                  f'({pct(t["calls"], t["calls_plan_date"])}); план месяца — {amount(t["calls_plan"])}.',
                  f'Бюджет к дате: план {amount(t["budget_plan_date"], True)}, '
                  f'факт ДБ {amount(t["spend"], True)} ({pct(t["spend"], t["budget_plan_date"])}).']
        lines.append(f'Бюджет месяца: {amount(t["budget_plan"], True)}. Валовые звонки по ДБ: {amount(t["gross"])}.')
        if not db['calls_verified'] or not db['spend_verified']:
            parts = []
            if not db['calls_verified']:
                parts.append('звонки')
            if not db['spend_verified']:
                parts.append('расход')
            lines.append('⚪ ' + ', '.join(parts).capitalize() + ': внесение данных по вчера не подтверждено; нули предварительные.')
        lines.append('Разметка целевых звонков может уточняться несколько дней.')
        if not db['plan_approved']:
            lines.append('⚪ Согласование плана месяца не отмечено в журнале.')
        if db['calls_verified'] and db['spend_verified'] and t['calls'] and t['spend'] is not None:
            lines.append('Стоимость целевого по ДБ: ' + amount(t['spend'] / t['calls'], True) + '.')
        # Only mature, verified input supports a performance warning.
        if db['calls_verified'] and db['spend_verified'] and db['plan_approved']:
            if t['calls_plan_date'] and t['calls'] is not None and t['calls'] < t['calls_plan_date']:
                lines.append('План по целевым к дате пока не выполнен; проверить разметку и результат по площадкам.')
        lines.append('<i>По площадкам: факт / план к дате</i>')
        for platform in db['platforms']:
            name = platform['name'] + (' · тариф' if str(platform.get('tariff_plan', '')).casefold() == 'тариф' else '')
            calls = (f'ЦО {amount(platform["calls"])}/{amount(platform["calls_plan_date"])}; '
                     if platform['calls_plan'] is not None else '')
            lines.append(f'{esc(name)}: {calls}'
                         f'расход {amount(platform["spend"], True)}/{amount(platform["budget_plan_date"], True)} к дате.')

    cian = report['cian']
    lines += ['', '<b>Циан · ставки и позиции объявлений</b>']
    for platform in db.get('platforms', []):
        if platform['name'].casefold() in ('cian', 'циан') and platform.get('calls_plan'):
            plan = platform.get('placement_plan')
            if plan:
                lines.append('<i>В плане ДБ: ' + esc(str(plan)[:180]) + '. Это не подтверждение текущих настроек кабинета.</i>')
    auction = cian.get('auction', {})
    if auction.get('status') in ('ok', 'partial'):
        items = auction.get('items', [])
        bids = [x['currentBet'] for x in items if isinstance(x.get('currentBet'), (int, float))]
        leaders = [x['leaderBet'] for x in items if isinstance(x.get('leaderBet'), (int, float))]
        positions = [x for x in items if isinstance(x.get('page'), int) and x['page'] > 0
                     and isinstance(x.get('position'), int) and x['position'] > 0]
        first = [x['position'] for x in positions if x['page'] == 1]
        if bids:
            lines.append(f'Ставки по {len(bids)} лотам: {amount(min(bids), True)}–{amount(max(bids), True)}; '
                         f'медиана {amount(statistics.median(bids), True)}.')
        else:
            lines.append('⚪ Наши ставки по объявлениям: поле currentBet отсутствует в ответе Циан. Это не нулевая ставка.')
        if leaders:
            lines.append(f'Ставки лидеров аукциона объявлений: {amount(min(leaders), True)}–{amount(max(leaders), True)}.')
        if positions:
            position_text = (f', места {min(first)}–{max(first)}' if first else '')
            lines.append(f'На первой странице: {len(first)} из {len(positions)} лотов с позицией{position_text}.')
        else:
            lines.append('⚪ Позиции не получены.')
        if auction['covered'] != auction['total']:
            lines.append(f'⚪ Неполный срез: {auction["covered"]}/{auction["total"]} лотов.')
        old = previous.get('cian', {}).get('auction') if previous else None
        changes = delta_auction(auction, old)
        if changes:
            lines.append(changes)
        lines.append('<i>Срез аукциона по лотам; это не ставка за целевой звонок. Выдача зависит от запроса.</i>')
    else:
        lines.append('⚪ ' + esc(auction.get('reason') or cian.get('reason', 'аукцион не получен')) + '.')

    lines += ['', '<b>Циан · платные звонки и расход</b>',
              '⚪ Ставка за звонок, платные звонки и расход CPL пока не получены. Нужны методы API новостроек; обычный API объявлений их не заменяет.',
              'Конверсия в целевые пока не рассчитана: нет подтверждённого числа платных звонков за период ДБ.']

    lines += ['', '<b>Показы и просмотры за вчера</b>']
    stats = cian.get('statistics', {})
    if stats.get('status') == 'ok':
        lines.append(f'Циан: {amount(stats["shows"])} показов, {amount(stats["views"])} просмотров '
                     f'· {stats["covered"]} опубликованных XML-объявлений.')
    else:
        lines.append('⚪ Циан: статистика недоступна или неполна; итог не рассчитан.')
    avito = report['avito']
    stats = avito.get('statistics', {})
    if stats.get('status') == 'ok':
        lines.append(f'Авито: {amount(stats["impressions"])} показов, {amount(stats["views"])} просмотров, '
                     f'{amount(stats["contacts"])} контактов · весь аккаунт ALIA.')
        lines.append('<i>Контакты Авито не равны платным или целевым звонкам. Позиции этот метод не возвращает.</i>')
    else:
        lines.append('⚪ Авито: ' + esc(avito.get('reason', 'статистика не получена')) + '.')

    actions = db.get('actions', [])
    lines += ['', '<b>Действия и контроль</b>']
    if actions:
        for action in actions[:3]:
            change = f'{action["before"]} → {action["after"]}' if action['before'] or action['after'] else ''
            text = f'{action["date"]} · {action["platform"]}: {action["action"]} {change} — {action["status"]}'
            lines.append(esc(text[:300]))
    elif db.get('status') == 'ok':
        lines.append('За последние 7 дней действий в журнале нет.')
    else:
        lines.append('Журнал создан в ДБ; автоматическое чтение ожидает подключения доступа.')
    lines.append('Ставки и тарифы автоматически не меняются.')
    lines.append('<i>API-расход не сверён с ДБ. Налоговую базу расходов и состав целевых звонков нужно подтвердить.</i>')
    return '\n'.join(lines)
