"""Serverless regressions. Optional SQL integration against a disposable Postgres.

Set AXPORT_TEST_DATABASE_URL to a disposable local database to run the integration
cases. These tests write private tables; never point them at a production DB.
"""
import io
import os
import sqlite3
import unittest
from unittest.mock import patch


class DeploymentTests(unittest.TestCase):
    def test_supabase_url_can_be_recovered_from_its_own_pooler(self):
        from junhee.server.configuration import resolve_supabase_url
        ref = 'abcdefghijklmnopqrst'
        db = f'postgresql://postgres.{ref}:test@aws-0-eu-west-1.pooler.supabase.com:6543/postgres'
        for configured in ('', 'postgresql://wrong-value', 'https://supabase.com/dashboard/project/example', 'https://[invalid'):
            self.assertEqual(resolve_supabase_url(configured, db), f'https://{ref}.supabase.co')
        self.assertEqual(resolve_supabase_url('https://custom.example/', db), 'https://custom.example')
        self.assertEqual(resolve_supabase_url('SUPABASE_URL="https://custom.example"', db), 'https://custom.example')

    def test_supabase_url_is_not_inferred_from_an_untrusted_host(self):
        from junhee.server.configuration import resolve_supabase_url, valid_project_url
        for host in ('attacker.example', 'pooler.supabase.com.attacker.example'):
            db = f'postgresql://postgres.abcdefghijklmnopqrst:test@{host}:6543/postgres'
            self.assertEqual(resolve_supabase_url('', db), '')
        self.assertFalse(valid_project_url('https://[invalid'))

    def test_bad_database_values_are_rejected_without_exposing_them(self):
        from junhee.server.database import connect
        for value in ('sb_secret_private', 'postgresql://user:[YOUR-PASSWORD]@host:6543/postgres',
                      'postgresql://user:private#password@host:6543/postgres'):
            with self.subTest(value_type=value.split(':')[0]):
                with patch.dict(os.environ, {'AXPORT_DATABASE_URL': value}):
                    with self.assertRaises(sqlite3.Error) as error:
                        connect('', scope='auth')
                    self.assertIn('AXPORT_DB_URL_INVALID', str(error.exception))
                    self.assertNotIn(value, str(error.exception))

    def test_database_failures_report_safe_actionable_categories(self):
        from junhee.server.database import connect
        url = 'postgresql://postgres.project:private_password@host:6543/postgres'
        cases = [(ImportError('private driver details'), 'AXPORT_DB_DRIVER_MISSING'),
                 (RuntimeError('password authentication failed private_password'), 'AXPORT_DB_AUTH_FAILED'),
                 (RuntimeError('could not translate host private_password'), 'AXPORT_DB_HOST_NOT_FOUND'),
                 (RuntimeError('connection timeout private_password'), 'AXPORT_DB_TIMEOUT'),
                 (RuntimeError('Tenant or user not found private_password'), 'AXPORT_DB_USER_NOT_FOUND')]
        for exception, category in cases:
            with self.subTest(category=category):
                with patch.dict(os.environ, {'AXPORT_DATABASE_URL': url}):
                    with patch('junhee.server.database.PostgresConnection', side_effect=exception):
                        with self.assertLogs('junhee.server.database', level='ERROR') as logs:
                            with self.assertRaises(sqlite3.Error) as error:
                                connect('', scope='auth')
                self.assertIn(category, str(error.exception))
                self.assertNotIn('private_password', str(error.exception))
                self.assertNotIn('private_password', ''.join(logs.output))

    def test_vercel_never_falls_back_to_ephemeral_sessions(self):
        from junhee.server.database import connect
        with patch.dict(os.environ, {'VERCEL': '1', 'AXPORT_DATABASE_URL': ''}):
            with self.assertRaisesRegex(sqlite3.Error, 'AXPORT_DATABASE_URL'):
                connect('unused.sqlite3', scope='auth')

    def test_vercel_disables_background_static_writer(self):
        from junhee.server.widget_refresh import enabled
        with patch.dict(os.environ, {'VERCEL': '1', 'AXPORT_WIDGET_REFRESH': '1'}):
            self.assertFalse(enabled())

    def test_upload_limit_rejects_before_workbook_parsing(self):
        from junhee.server.analysis import upload_bytes
        from junhee.server.engine.market_service import MarketError
        with patch.dict(os.environ, {'VERCEL': '1'}):
            self.assertEqual(upload_bytes(io.BytesIO(b'x')), b'x')
            with self.assertRaises(MarketError) as error:
                upload_bytes(io.BytesIO(b'x' * (4 * 1024 * 1024 + 1)))
            self.assertIn('4 MB', error.exception.message)


@unittest.skipUnless(os.environ.get('AXPORT_TEST_DATABASE_URL'), 'disposable PostgreSQL not configured')
class PostgresIntegration(unittest.TestCase):
    def setUp(self):
        from uuid import uuid4
        import psycopg
        self.user_id = str(uuid4())
        self.env = patch.dict(os.environ, {
            'AXPORT_DATABASE_URL': os.environ['AXPORT_TEST_DATABASE_URL'], 'VERCEL': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        # The disposable loopback test engine has no TLS. Production always
        # requires TLS in database.py; this override exists only in this test.
        original = psycopg.connect
        self.driver = patch.object(psycopg, 'connect',
                                   side_effect=lambda *a, **kw: original(*a, **{**kw, 'sslmode': 'disable'}))
        self.driver.start()
        self.addCleanup(self.driver.stop)

    def test_encrypted_session_survives_store_recreation_and_revocation(self):
        from junhee.server.auth_core import AuthError, SessionStore
        from unittest.mock import Mock
        user = {'id': self.user_id, 'email': 'test@example.com'}
        tokens = {'access_token': 'private-access', 'refresh_token': 'private-refresh', 'expires_in': 3600}
        first = SessionStore('/read-only/never-created/auth.db', 'x' * 48)
        sid = first.create(tokens, user)
        second = SessionStore('/another-instance/auth.db', 'x' * 48)
        provider = Mock()
        provider.get_user.return_value = user
        self.assertEqual(second.resolve_credentials(sid, provider), (user, 'private-access'))
        with second._connect() as db:
            row = db.execute('SELECT token_box FROM auth_sessions WHERE sid_hash=?', (second._key(sid),)).fetchone()
        self.assertNotIn('private-access', row[0])
        with second._connect() as db:
            db.execute('UPDATE auth_sessions SET access_expires_at=? WHERE sid_hash=?', (0, second._key(sid)))
        provider.refresh.return_value = {**tokens, 'access_token': 'refreshed-access', 'user': user}
        self.assertEqual(second.resolve_credentials(sid, provider), (user, 'refreshed-access'))
        self.assertEqual(first.revoke(sid), 'refreshed-access')
        with self.assertRaises(AuthError):
            second.resolve_credentials(sid, provider)

    def test_mail_cooldown_is_shared_between_instances(self):
        from junhee.server.auth_core import MailCooldown
        first = MailCooldown('/read-only/auth.db', 'x' * 48)
        second = MailCooldown('/another-instance/auth.db', 'x' * 48)
        email = self.user_id + '@example.com'
        self.assertEqual(first.reserve(email), 0)
        self.assertGreater(second.reserve(email), 0)
        self.assertEqual(second.extend(email, 120), 120)
        first.release(email)
        self.assertEqual(second.reserve(email), 0)

    def test_uploads_assessments_and_upserts_survive_instance_changes(self):
        from junhee.server.engine.market_service import MarketError, MarketStore
        first = MarketStore('/read-only/analysis.db')
        source = first.add_source(self.user_id, 'private.xlsx', b'private bytes', {'sha256': self.user_id})
        second = MarketStore('/another-instance/analysis.db')
        self.assertEqual(bytes(second.source(self.user_id, source)['content']), b'private bytes')
        with self.assertRaises(MarketError):
            second.source('another-user', source)
        assessment = first.create(self.user_id, {'company_id': 'upload:' + source}, source)
        second.update(self.user_id, assessment, 'COMPLETED', 'done', result={'ok': True})
        self.assertEqual(first.get(self.user_id, assessment)['result'], {'ok': True})
        # Both cache tables must support SQLite's INSERT OR REPLACE contract.
        with first.connection() as db:
            for table in ('analysis_trade_cache', 'analysis_public_cache'):
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, created REAL NOT NULL, result_json TEXT NOT NULL)')
                for value in ('old', 'new'):
                    db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?)', (self.user_id, 1234567890.123, value))
                row = db.execute(f'SELECT created,result_json FROM {table} WHERE key=?', (self.user_id,)).fetchone()
                self.assertEqual(row['result_json'], 'new')
                self.assertEqual(row[0], 1234567890.123)

    def test_workspace_deletion_journal_and_source_are_separate(self):
        from junhee.server.engine.market_service import MarketStore
        store = MarketStore('/read-only/analysis.db')
        store._journal_prepare_many(self.user_id, [{'id': 'file-1', 'company_id': 'upload:test'}])
        intents = store._journal_intents(self.user_id)
        self.assertEqual(intents[0]['item_id'], 'file-1')
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workspace_source_deletions WHERE user_id=?', (self.user_id,)).fetchone()[0], 0)
        store._journal_complete(self.user_id, [('file-1', intents[0]['token'])])
        self.assertEqual(store._journal_intents(self.user_id), [])

    def test_http_upload_download_and_contact_use_shared_database(self):
        from junhee.tests.test_accounts import ENV, TOKENS, USER, csrf, load_app
        from pathlib import Path
        import re
        from unittest.mock import Mock
        env = {**ENV, 'VERCEL': '1', 'AXPORT_WIDGET_REFRESH': '1'}
        app = load_app(env)
        self.assertTrue(app.extensions['junhee_analysis'].inline)
        self.assertTrue(app.config['SESSION_COOKIE_SECURE'] is False)  # ENV explicitly uses HTTP for tests
        provider = Mock()
        provider.get_user.return_value = USER
        provider.sign_in.return_value = dict(TOKENS)
        app.extensions['junhee_accounts']['provider'] = provider
        client = app.test_client()
        token = csrf(client.get('/app').get_data(as_text=True))
        client.post('/auth/login', data={'csrf_token': token, 'email': USER['email'], 'password': 'pw'})
        html = client.get('/app').get_data(as_text=True)
        self.assertIn('파일당 최대 4 MB', html)
        headers = {'X-CSRF-Token': re.search(r'data-csrf="([^"]+)"', html).group(1)}
        source = Path(__file__).resolve().parents[2] / 'static' / 'samples' / '기업데이터_예시1_한울메모리.xlsx'
        content = source.read_bytes()
        response = client.post('/api/analysis/company-files', headers=headers,
                               data={'file': (io.BytesIO(content), 'company.xlsx')})
        self.assertEqual(response.status_code, 201, response.get_json())
        source_id = response.get_json()['company_id'][7:]
        downloaded = client.get(f'/api/analysis/sources/{source_id}/file')
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, content)
        # Complete the real engine in the request, without calling external APIs.
        import requests
        svc = app.extensions['junhee_analysis']
        svc.repository = None
        svc.key_provider = lambda: {}
        with patch('requests.sessions.Session.request', side_effect=requests.ConnectionError('offline test')):
            assessed = client.post('/api/analysis/assessments', headers=headers,
                                   json={'company_id': 'upload:' + source_id, 'company': '테스트 기업',
                                         'hs': '854232', 'country': 'US'})
        self.assertEqual(assessed.status_code, 202, assessed.get_json())
        self.assertEqual(assessed.get_json()['status'], 'COMPLETED', assessed.get_json())
        aid = assessed.get_json()['assessment_id']
        from junhee.server.analysis import JunheeAnalysis
        another_instance = JunheeAnalysis('/read-only/another.db', svc.root, lambda: {}, inline=True)
        self.assertEqual(another_instance.handoff(USER['id'], None, aid)['schema'], 'handoff-v1')
        response = client.post('/api/contact', json={'type': '의견', 'field': '',
                               'message': 'Vercel 데이터베이스 문의 저장 테스트입니다.'})
        self.assertEqual(response.status_code, 200, response.get_json())


if __name__ == '__main__':
    unittest.main()
