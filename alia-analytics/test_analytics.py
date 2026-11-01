import datetime as dt
import sqlite3
import unittest
from unittest.mock import Mock, patch

from dashboard import HEADERS, parse_month, parse_actions, public_range, number
from providers import avito_metrics, cian_auction, SourceError
from render import render, delta_auction
from run import send_once


class DashboardTests(unittest.TestCase):
    def test_csv_preserves_first_data_row_and_localized_money(self):
        response = Mock()
        response.headers = {'Content-Type': 'text/csv; charset=utf-8'}
        response.read.return_value = '"Вчера","01.11.2026"\n'.encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch('dashboard.urllib.request.urlopen', return_value=context):
            self.assertEqual(public_range(123, 'D6:E6'), [['Вчера', '01.11.2026']])
        self.assertEqual(number('12\u00a0494\u00a0508,01 ₽'), 12494508.01)
        self.assertIsNone(number('—'))

    def fixture(self, shift=0):
        header = ['Источник', 'Разбивка', 'Место', 'Формат'] + [''] * shift + list(HEADERS.values())
        rows = [['Вчера', 46327], header, ['Базы недвижимости', '', '', ''] + [''] * shift + [75, 2, 0, 1200, 40, 0, 0],
                ['Cian', '', 'Premium', ''] + [''] * shift + [9, 0, 0, 900, 30, 0, 0],
                ['Социальные сети', '', '', ''] + [''] * shift + [999] * 7]
        rows[0][1] = (dt.date(2026, 11, 1) - dt.date(1899, 12, 30)).days
        return rows

    def test_header_mapping_excludes_other_channels_even_when_columns_move(self):
        for shift in (0, 30, 31):
            report = parse_month(self.fixture(shift), month='Ноябрь', yesterday=dt.date(2026, 11, 1), journal_rows=[])
            self.assertEqual(report['total']['calls_plan'], 75)
            self.assertEqual([x['name'] for x in report['platforms']], ['Cian'])
            self.assertFalse(report['calls_verified'])

    def test_stale_formula_date_rejected(self):
        with self.assertRaises(ValueError):
            parse_month(self.fixture(), month='Ноябрь', yesterday=dt.date(2026, 11, 2), journal_rows=[])

    def test_missing_and_zero_remain_different(self):
        rows = self.fixture()
        rows[2][6] = ''
        r = parse_month(rows, month='Ноябрь', yesterday=dt.date(2026, 11, 1), journal_rows=[])
        self.assertIsNone(r['total']['calls'])
        self.assertEqual(r['total']['spend'], 0)

    def test_proposal_status_preserved_and_future_actions_excluded(self):
        rows = [[], [], [], [], ['01.11.2026', 'Циан', 'Ставка', 10, 15, 'Предложено'],
                ['03.11.2026', 'Циан', 'Тариф', '', '', 'Выполнено']]
        result = parse_actions(rows, dt.date(2026, 11, 1))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['status'], 'Предложено')


class ApiTests(unittest.TestCase):
    def response(self):
        return {'result': {'dataTotalCount': 1, 'groupings': [
            {'id': 1793494800, 'type': 'dates', 'metrics': [
                {'slug': 'impressions', 'value': 289}, {'slug': 'views', 'value': 71},
                {'slug': 'contacts', 'value': 0}]}]}}

    def test_avito_zero_contacts_valid_and_wrong_date_rejected(self):
        payload = self.response()
        # Construct the known day boundary rather than relying on the fixture timestamp.
        from zoneinfo import ZoneInfo
        payload['result']['groupings'][0]['id'] = int(dt.datetime(2026, 11, 1, tzinfo=ZoneInfo('Europe/Moscow')).timestamp())
        self.assertEqual(avito_metrics(payload, dt.date(2026, 11, 1))['contacts'], 0)
        with self.assertRaises(SourceError):
            avito_metrics(payload, dt.date(2026, 11, 2))

    def test_partial_avito_total_is_never_reported_as_complete(self):
        payload = self.response()
        payload['result']['dataTotalCount'] = 2
        with self.assertRaises(SourceError):
            avito_metrics(payload, dt.date(2026, 11, 1))

    def test_auction_changes_compare_same_ids(self):
        a = {'status': 'ok', 'items': [{'offerId': 1, 'currentBet': 11, 'page': 1, 'position': 2}]}
        b = {'status': 'ok', 'items': [{'offerId': 2, 'currentBet': 20, 'page': 1, 'position': 2}]}
        self.assertIsNone(delta_auction(a, b))

    def test_bad_lot_does_not_discard_remaining_auction_rows(self):
        api = Mock()
        def call(path, query, **kwargs):
            ids = query['offerIds']
            if 2 in ids:
                raise SourceError('API HTTP 500')
            return {'result': {'items': [{'offerId': i, 'page': 1, 'position': i} for i in ids]}}
        api.call.side_effect = call
        result = cian_auction(api, [1, 2, 3])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['covered'], 2)
        self.assertEqual({x['offerId'] for x in result['items']}, {1, 3})


class DeliveryTests(unittest.TestCase):
    def database(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE deliveries(run_key TEXT,chat TEXT,status TEXT,body_hash TEXT,message_id INTEGER,PRIMARY KEY(run_key,chat))')
        return db

    def test_same_day_sent_once(self):
        db = self.database()
        bot = Mock()
        bot.call.return_value = {'message_id': 100}
        for _ in range(2):
            self.assertEqual(send_once(db, key='2026-11-01', chat='42', text='test', telegram=bot), 'sent')
        self.assertEqual(bot.call.call_count, 1)

    def test_ambiguous_delivery_is_not_resent(self):
        db = self.database()
        bot = Mock()
        bot.call.side_effect = TimeoutError()
        for _ in range(2):
            self.assertEqual(send_once(db, key='2026-11-01', chat='42', text='test', telegram=bot), 'unknown')
        self.assertEqual(bot.call.call_count, 1)


if __name__ == '__main__':
    unittest.main()
