"""Serverless analysis survives request boundaries and incomplete providers."""
import json
import os
from pathlib import Path
import tempfile
import time
from threading import Barrier
import unittest
from unittest.mock import patch, Mock

from flask import Flask
import requests

from junhee.server import analysis
from junhee.server.engine.request_budget import BudgetSession

ROOT = Path(__file__).resolve().parents[2]


class StepAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'AXPORT_DATABASE_URL': '', 'VERCEL': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.vercel_expiry = patch('junhee.server.engine.market_service.is_vercel', return_value=True)
        self.vercel_expiry.start()
        self.addCleanup(self.vercel_expiry.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = str(Path(self.folder.name) / 'analysis.sqlite3')
        self.svc = analysis.JunheeAnalysis(self.db, ROOT, lambda: {}, inline=True)
        self.svc.deferred = True
        sample = next((ROOT / 'static/samples').glob('*가온반도체*'), None)
        if sample is None:
            import unicodedata
            sample = next(p for p in (ROOT / 'static/samples').glob('*.xlsx')
                          if '가온반도체' in unicodedata.normalize('NFC', p.name))
        source = self.svc.upload('owner', sample.read_bytes(), sample.name)
        self.payload = {'company_id': source['company_id'], 'company': 'AX 반도체',
                        'hs': '854232', 'country': 'US'}

    def start(self):
        item = self.svc.submit_for('owner', 'token', self.payload)
        self.assertEqual(item['status'], 'QUEUED')
        self.assertTrue(analysis._status(item)['stepwise'])
        return item['assessment_id']

    def test_resume_in_new_instances_and_complete_report_without_network(self):
        aid = self.start()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected HTTP')):
            for step in range(3):
                # Each request can land on a different serverless instance.
                svc = analysis.JunheeAnalysis(self.db, ROOT, lambda: {}, inline=True)
                item = svc.advance('owner', 'token', aid)
                self.assertEqual(item['status'], 'COMPLETED' if step == 2 else 'RUNNING', item)
        doc = svc.handoff('owner', None, aid)
        self.assertEqual(doc['schema'], 'handoff-v1')
        self.assertEqual(doc['company_name'], 'AX 반도체')
        self.assertEqual(len(doc['score']['per_country']['미국']['factors']), 5)
        self.assertIn('자료', json.dumps(doc, ensure_ascii=False))
        with svc.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_steps').fetchone()[0], 0)

    def test_duplicate_request_does_not_repeat_leased_step(self):
        aid = self.start()
        with self.svc.store.connection() as db:
            db.execute('UPDATE analysis_steps SET lease_until=? WHERE assessment_id=?', (time.time() + 120, aid))
        with patch.object(self.svc, '_collect_country') as collect:
            self.svc.advance('owner', 'token', aid)
            collect.assert_not_called()

    def test_five_country_collectors_run_concurrently(self):
        aid = self.start()
        barrier = Barrier(5, timeout=3)
        original = self.svc._collect_country
        def collect(*args, **kwargs):
            barrier.wait()
            return original(*args, **kwargs)
        with patch.object(self.svc, '_collect_country', side_effect=collect):
            item = self.svc.advance('owner', 'token', aid)
        self.assertEqual(item['status'], 'RUNNING')
        with self.svc.store.connection() as db:
            data = json.loads(db.execute('SELECT data_json FROM analysis_steps WHERE assessment_id=?', (aid,)).fetchone()[0])
        self.assertEqual(set(data['prepared']['trades']), {'US', 'CN', 'JP', 'DE', 'VN'})

    def test_partial_trade_snapshot_keeps_valid_periods_and_retries_failures(self):
        from datetime import datetime, timezone
        aid = self.start()
        inputs = self.svc.store.get('owner', aid)['inputs']
        snapshot = {'monthly_imports': [{'period': '2026-09', 'status': 'OBSERVED', 'value': 10}],
                    'annual_imports': [], 'kcs_exports': [{'period': '2026-09', 'status': 'FETCH_ERROR', 'value': None}],
                    'sources': [{'id': 'known', 'provider': 'UN Comtrade', 'status': 'OBSERVED',
                                 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'raw': 'not needed'}]}
        self.svc.trade_collector = Mock(return_value=snapshot)
        self.svc._trade(inputs, lambda text: None)
        self.svc.trade_collector = None
        with patch('junhee.server.engine.analysis_market_price.collect_trade', return_value=snapshot) as collector:
            self.svc._trade(inputs, lambda text: None)
        collector.assert_called_once()
        prior = collector.call_args.kwargs['prior']
        self.assertEqual(prior['monthly_imports'][0]['value'], 10)
        self.assertEqual(prior['kcs_exports'][0]['status'], 'FETCH_ERROR')
        self.assertNotIn('raw', prior['sources'][0])

    def test_independent_supplements_run_concurrently(self):
        aid = self.start()
        self.svc.advance('owner', 'token', aid)
        barrier = Barrier(3, timeout=3)
        def supplement(*args, **kwargs):
            barrier.wait()
            return {'status': 'FETCH_ERROR', 'sources': []}
        self.svc.fx_collector = supplement
        with patch.object(self.svc, '_reference', side_effect=supplement), patch.object(self.svc, '_law', side_effect=supplement):
            item = self.svc.advance('owner', 'token', aid)
        self.assertEqual(item['status'], 'RUNNING')
        with self.svc.store.connection() as db:
            data = json.loads(db.execute('SELECT data_json FROM analysis_steps WHERE assessment_id=?', (aid,)).fetchone()[0])
        self.assertTrue({'reference', 'fx', 'law'} <= data['prepared'].keys())

    def test_steps_are_not_expired_by_original_five_minute_request_limit(self):
        aid = self.start()
        with self.svc.store.connection() as db:
            db.execute("UPDATE market_assessments SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (aid,))
        self.assertEqual(self.svc.store.get('owner', aid)['status'], 'QUEUED')

    def test_external_failure_still_produces_report(self):
        aid = self.start()
        with patch.object(self.svc, '_trade', side_effect=requests.Timeout()), \
             patch.object(self.svc, '_reference', side_effect=requests.Timeout()), \
             patch.object(self.svc, '_law', side_effect=requests.Timeout()):
            for _ in range(3):
                item = self.svc.advance('owner', 'token', aid)
        self.assertEqual(item['status'], 'COMPLETED', item)
        self.assertIsNotNone(self.svc.handoff('owner', None, aid))

    def test_advance_is_authenticated_and_owner_scoped(self):
        aid = self.start()
        app = Flask(__name__)
        app.extensions['junhee_analysis'] = self.svc
        app.register_blueprint(analysis.bp)
        client = app.test_client()
        with patch.object(analysis, 'current_user', return_value=None):
            self.assertEqual(client.post(f'/api/analysis/assessments/{aid}/advance').status_code, 401)
        with patch.object(analysis, 'current_user', return_value={'id': 'other'}), \
             patch.object(analysis, 'csrf_ok', return_value=True), \
             patch.object(analysis, 'access_token', return_value='token'):
            self.assertEqual(client.post(f'/api/analysis/assessments/{aid}/advance').status_code, 404)
        self.assertEqual(self.svc.store.get('owner', aid)['status'], 'QUEUED')

    def test_http_submit_steps_and_handoff(self):
        app = Flask(__name__)
        app.extensions['junhee_analysis'] = self.svc
        app.register_blueprint(analysis.bp)
        client = app.test_client()
        with patch.object(analysis, 'current_user', return_value={'id': 'owner'}), \
             patch.object(analysis, 'csrf_ok', return_value=True), \
             patch.object(analysis, 'access_token', return_value='token'):
            response = client.post('/api/analysis/assessments', json=self.payload)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json['status'], 'QUEUED')
            aid = response.json['assessment_id']
            for _ in range(3):
                response = client.post(f'/api/analysis/assessments/{aid}/advance')
                self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['status'], 'COMPLETED')
            doc = client.get(f'/api/analysis/assessments/{aid}/handoff')
            self.assertEqual(doc.status_code, 200)
            self.assertEqual(doc.json['schema'], 'handoff-v1')

    def test_cancel_clears_checkpoint_and_allows_new_analysis(self):
        aid = self.start()
        app = Flask(__name__)
        app.extensions['junhee_analysis'] = self.svc
        app.register_blueprint(analysis.bp)
        with patch.object(analysis, 'current_user', return_value={'id': 'owner'}), \
             patch.object(analysis, 'csrf_ok', return_value=True):
            response = app.test_client().post(f'/api/analysis/assessments/{aid}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['status'], 'FAILED')
        with self.svc.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_steps').fetchone()[0], 0)
        self.assertNotEqual(self.start(), aid)


class BudgetTests(unittest.TestCase):
    def test_expired_budget_makes_no_http_request(self):
        with BudgetSession(-1) as session, patch('requests.sessions.Session.request') as http:
            with self.assertRaises(requests.Timeout):
                session.get('https://example.com')
            http.assert_not_called()

    def test_response_stream_cannot_run_past_budget(self):
        with BudgetSession(1) as session:
            response = requests.Response()
            def chunks(*args, **kwargs):
                yield b'first'
                session.deadline = time.monotonic() - 1
                yield b'late'
            response.iter_content = chunks
            response.close = lambda: None
            with patch('requests.sessions.Session.request', return_value=response):
                result = session.get('https://example.com', stream=True)
                iterator = result.iter_content()
                self.assertEqual(next(iterator), b'first')
                with self.assertRaises(requests.Timeout):
                    next(iterator)
