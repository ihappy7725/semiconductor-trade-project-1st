"""Resolve the public Supabase URL from explicit settings or its DB endpoint."""
import re
from urllib.parse import unquote, urlsplit


def valid_project_url(value):
    try:
        parts = urlsplit(value)
        parts.port
        return bool(parts.scheme == 'https' and parts.hostname
                    and not parts.username and not parts.password
                    and not parts.query and not parts.fragment
                    and parts.path in ('', '/')
                    and not any(c.isspace() for c in value))
    except ValueError:
        return False


def resolve_supabase_url(value, database_url):
    value = value.strip().strip('\"\'')
    if value.startswith('SUPABASE_URL='):
        value = value.partition('=')[2].strip().strip('\"\'')
    if valid_project_url(value):
        return value.rstrip('/')
    try:
        db = urlsplit(database_url.strip())
        db.port
        if db.scheme not in ('postgres', 'postgresql') or not db.hostname:
            return value
        # Infer only from Supabase-owned endpoints, never an arbitrary DB host.
        if db.hostname.endswith('.pooler.supabase.com'):
            project = re.fullmatch(r'postgres\.([a-z0-9]{20})', unquote(db.username or ''))
        else:
            project = re.fullmatch(r'db\.([a-z0-9]{20})\.supabase\.co', db.hostname)
        if project:
            return f'https://{project.group(1)}.supabase.co'
    except ValueError:
        pass
    return value
