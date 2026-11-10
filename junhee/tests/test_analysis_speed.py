"""Fewer HTTP calls without changing confirmed observations or API pacing."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import requests

from junhee.server.engine import analysis_market_price as market
from junhee.server.engine import market_data
from junhee.server.engine.company_analysis import CompanyAnalysis
from junhee.server.engine.request_budget import BudgetSession


class ObservationCacheTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.inputs = {'as_of': '2026-11-11', 'country': 'US', 'country_name': '미국',
                       'hs6': '854232', 'hs_edition': 'HS2022'}

    def fetch(self, session, url, params, provider, sid, keys):
        frequency = 'A' if '/C/A/' in url else 'M'
        self.calls.append((frequency, params['period']))
        rows = [{'period': int(p), 'reporterCode': 842, 'partnerCode': 0, 'partner2Code': 0,
                 'flowCode': 'M', 'cmdCode': '854232', 'classificationCode': 'H6',
                 'customsCode': 'C00', 'motCode': 0, 'freqCode': frequency, 'primaryValue': 100}
                for p in params['period'].split(',')]
        source = {'id': sid, 'provider': provider, 'status': 'RECEIVED',
                  'retrieved_at': datetime.now(timezone.utc).isoformat(), 'sha256': 'fixture'}
        return json.dumps({'count': len(rows), 'data': rows}).encode(), source

    def collect(self, prior=None):
        with patch.object(market, '_fetch', side_effect=self.fetch), \
             patch.object(market, '_industry', return_value=([], [], [])), \
             patch.object(market, 'rebase_market', side_effect=lambda trade, *args: trade):
            return market.collect_trade(self.inputs, {'UN_COMTRADE_API_KEY': 'test'}, prior=prior)

    def test_annual_years_use_one_request_and_latest_months_are_first(self):
        result = self.collect()
        self.assertEqual([p for f, p in self.calls if f == 'A'], ['2022,2023,2024,2025'])
        monthly = [p for f, p in self.calls if f == 'M']
        self.assertTrue(monthly[0].startswith('2026'))
        self.assertEqual(len(result['monthly_imports']), 60)

    def test_confirmed_periods_are_reused_with_original_evidence(self):
        prior = self.collect()
        self.calls.clear()
        result = self.collect(prior)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['monthly_imports'], prior['monthly_imports'])
        self.assertEqual(result['annual_imports'], prior['annual_imports'])
        source_ids = {s['id'] for s in result['sources']}
        for row in result['monthly_imports']:
            for evidence in row['evidence']:
                self.assertIn(evidence['source_id'], source_ids)

    def test_failed_period_is_retried_while_other_values_stay_the_same(self):
        prior = self.collect()
        row = next(r for r in prior['monthly_imports'] if r['period'] == '2024-03')
        row.update(value=None, status='FETCH_ERROR')
        self.calls.clear()
        result = self.collect(prior)
        self.assertEqual(self.calls, [('M', '202403')])
        retried = next(r for r in result['monthly_imports'] if r['period'] == '2024-03')
        self.assertEqual((retried['value'], retried['status']), (100, 'OBSERVED'))

    def test_old_sources_are_not_extended_by_new_snapshot_writes(self):
        prior = self.collect()
        for source in prior['sources']:
            source['retrieved_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.calls.clear()
        self.collect(prior)
        self.assertEqual(len(self.calls), 6)
        self.assertFalse(CompanyAnalysis._trade_complete(prior))

    def test_cached_status_does_not_override_invalid_numeric_value(self):
        prior = self.collect()
        row = next(r for r in prior['monthly_imports'] if r['period'] == '2024-03')
        row.update(value=-10, value_decimal='-10', status='OBSERVED')
        self.calls.clear()
        result = self.collect(prior)
        self.assertEqual(self.calls, [('M', '202403')])
        self.assertEqual(next(r for r in result['monthly_imports'] if r['period'] == '2024-03')['value'], 100)

    def test_wrong_hs_is_rejected_for_the_entire_annual_batch(self):
        original = self.fetch
        def invalid(*args):
            content, source = original(*args)
            doc = json.loads(content)
            doc['data'][0]['classificationCode'] = 'H5'
            return json.dumps(doc).encode(), source
        self.fetch = invalid
        result = self.collect()
        self.assertTrue(all(r['status'] == 'INVALID_SCOPE' for r in result['annual_imports'] if r['year'] >= 2022))


class ProviderFailureTests(unittest.TestCase):
    def test_forbidden_service_is_not_repeated_and_changed_key_can_retry(self):
        response = requests.Response()
        response.status_code = 403
        response._content = b'Forbidden'
        response._content_consumed = True
        blocked = {}
        url = 'https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList'
        with patch('requests.sessions.Session.request', return_value=response) as http:
            for key in ('first-secret', 'first-secret', 'changed-secret'):
                with BudgetSession(blocked=blocked) as session:
                    content, source = market_data._fetch(session, url, {'serviceKey': key},
                        '관세청', 'id', {'KCS_TRADE_API_KEY': key})
                    self.assertIsNone(content)
                    self.assertEqual(source['http_status'], 403)
            self.assertEqual(http.call_count, 2)
        self.assertNotIn('secret', json.dumps(blocked))

    def test_blocked_comtrade_call_skips_the_pacing_wait(self):
        with BudgetSession() as session:
            url = 'https://comtradeapi.un.org/data/v1/get/C/A/HS'
            scope = session.check_provider(url, {'subscription-key': 'test'})
            import time
            session.blocked[scope] = {'until': time.time() + 30, 'status': 429}
            with patch.object(market_data, '_wait_comtrade_turn') as wait:
                content, source = market_data._fetch(session, url, {'subscription-key': 'test'},
                    'UN Comtrade', 'id', {'UN_COMTRADE_API_KEY': 'test'})
            wait.assert_not_called()
            self.assertIsNone(content)
            self.assertEqual(source['http_status'], 429)
