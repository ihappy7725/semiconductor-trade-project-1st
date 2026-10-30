"""SQLite locally; private PostgreSQL tables for a shared Vercel installation.

The adapter covers only the SQL used by this application. Tokens remain encrypted
by SessionStore, and uploaded bytes stay in the database, never in public static/.
"""
import os
from pathlib import Path
import re
import sqlite3


def is_vercel():
    return os.environ.get('VERCEL') == '1'


def remote_enabled():
    return bool(os.environ.get('AXPORT_DATABASE_URL', '').strip())


def prepare_directory(path):
    if not remote_enabled():
        Path(path).mkdir(parents=True, exist_ok=True)


class Row:
    def __init__(self, names, values):
        self.names, self.values = names, values

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return self.values[self.names.index(key)] if isinstance(key, str) else self.values[key]

    def __iter__(self):
        return iter(self.values)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def _row(self, values):
        if values is None:
            return None
        return Row([column.name for column in self.cursor.description], values)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        for row in self.cursor:
            yield self._row(row)


# Explicit primary keys/column order for the application's SQLite REPLACE calls.
UPSERTS = {
    'junhee_handoff': (('user_id', 'assessment_id', 'doc_json'), 2),
    'junhee_company_files': (('user_id', 'source_id', 'file_name'), 2),
    'junhee_job_owner': (('assessment_id', 'pid'), 1),
    'analysis_trade_cache': (('key', 'created', 'result_json'), 1),
    'analysis_public_cache': (('key', 'created', 'result_json'), 1),
}


def postgres_sql(sql, journal=False):
    if journal:
        sql = re.sub(r'\bworkspace_source_deletions\b', 'workspace_source_deletion_journal', sql)
    sql = re.sub(r'\bBLOB\b', 'BYTEA', sql)
    sql = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', sql)
    sql = sql.replace('MAX(available_at, excluded.available_at)',
                      'GREATEST(auth_mail_cooldowns.available_at, excluded.available_at)')
    match = re.match(r'INSERT OR REPLACE INTO (\w+)\s*(?:\([^)]*\))?\s*VALUES', sql, re.I)
    if match:
        table = match.group(1)
        columns, key_count = UPSERTS[table]
        sql = re.sub(r'INSERT OR REPLACE', 'INSERT', sql, count=1, flags=re.I)
        sql += ' ON CONFLICT (' + ','.join(columns[:key_count]) + ') DO UPDATE SET '
        sql += ','.join(f'{column}=excluded.{column}' for column in columns[key_count:])
    # These queries are application constants; values always use bound parameters.
    return sql.replace('%', '%%').replace('?', '%s')


class PostgresConnection:
    def __init__(self, url, scope):
        import psycopg
        self.driver = psycopg
        self.journal = scope == 'journal'
        self.conn = psycopg.connect(url, sslmode='require', connect_timeout=10,
                                   prepare_threshold=None)
        try:
            # Preserve SQLite's write reservation across instances. The deletion
            # journal has its own lock, so it can commit within a workspace save.
            self.conn.execute("SET LOCAL lock_timeout = '10s'")
            self.conn.execute("SET LOCAL statement_timeout = '20s'")
            lock_id = {'auth': 71001, 'analysis': 71002, 'journal': 71003}[scope]
            self.conn.execute('SELECT pg_advisory_xact_lock(%s)', (lock_id,))
            self.conn.execute('CREATE SCHEMA IF NOT EXISTS axport_server')
            self.conn.execute('SET LOCAL search_path TO axport_server')
        except Exception:
            self.conn.close()
            raise

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith('PRAGMA') or sql.strip().upper() == 'BEGIN IMMEDIATE':
            return None
        try:
            return Cursor(self.conn.execute(postgres_sql(sql, self.journal), params))
        except self.driver.Error:
            raise sqlite3.OperationalError('Shared database operation failed') from None

    def executemany(self, sql, params):
        try:
            with self.conn.cursor() as cursor:
                cursor.executemany(postgres_sql(sql, self.journal), params)
        except self.driver.Error:
            raise sqlite3.OperationalError('Shared database operation failed') from None

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        try:
            self.conn.commit()
        except self.driver.Error:
            raise sqlite3.OperationalError('Shared database transaction failed') from None

    def rollback(self):
        try:
            self.conn.rollback()
        except self.driver.Error:
            raise sqlite3.OperationalError('Shared database transaction failed') from None

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.rollback() if exc_type else self.commit()
        except self.driver.Error:
            raise sqlite3.OperationalError('Shared database transaction failed') from None


def connect(path, *, scope, timeout=10):
    url = os.environ.get('AXPORT_DATABASE_URL', '').strip()
    if url:
        try:
            return PostgresConnection(url, scope)
        except Exception:
            # Do not include the connection string/password in error responses.
            raise sqlite3.OperationalError('Shared database connection failed') from None
    if is_vercel():
        raise sqlite3.OperationalError('AXPORT_DATABASE_URL is required on Vercel')
    db = sqlite3.connect(path, timeout=timeout)
    db.row_factory = sqlite3.Row
    return db
