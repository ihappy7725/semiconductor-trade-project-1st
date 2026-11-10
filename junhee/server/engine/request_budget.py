"""Bound one collection step without fabricating missing observations."""
import time
import hashlib
import json
from urllib.parse import urlsplit

import requests


class ProviderUnavailable(requests.RequestException):
    retryable = False

    def __init__(self, status):
        self.http_status = status
        super().__init__('provider_temporarily_unavailable')


class BudgetSession(requests.Session):
    def __init__(self, seconds=60, blocked=None):
        super().__init__()
        self.deadline = time.monotonic() + seconds
        self.blocked = blocked if blocked is not None else {}

    @property
    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def check_provider(self, url, params=None):
        params = params or {}
        credentials = {k: v for k, v in params.items() if k.lower() in ('servicekey', 'subscription-key', 'oc', 'authkey')}
        location = urlsplit(url)
        scope = hashlib.sha256(json.dumps([location.netloc, location.path, credentials], sort_keys=True).encode()).hexdigest()
        failure = self.blocked.get(scope, {})
        if failure.get('until', 0) > time.time():
            raise ProviderUnavailable(failure['status'])
        return scope

    def request(self, method, url, **kwargs):
        scope = self.check_provider(url, kwargs.get('params'))
        remaining = self.remaining
        if remaining < 0.1:
            raise requests.Timeout('collection_step_budget_exhausted')
        configured = kwargs.get('timeout', (5, 20))
        connect, read = configured if isinstance(configured, tuple) else (configured, configured)
        kwargs['timeout'] = (min(connect, remaining / 2), min(read, remaining / 2))
        streaming = kwargs.get('stream', False)
        kwargs['stream'] = True
        response = super().request(method, url, **kwargs)
        if response.status_code in (401, 403, 429):
            ttl = 300
            if response.status_code == 429:
                try:
                    ttl = min(60, max(1, float(response.headers.get('Retry-After', 30))))
                except (ValueError, TypeError):
                    ttl = 30
            self.blocked[scope] = {'until': time.time() + ttl, 'status': response.status_code}
        original = response.iter_content
        def chunks(*args, **options):
            for chunk in original(*args, **options):
                if self.remaining < 0.1:
                    response.close()
                    raise requests.Timeout('collection_step_budget_exhausted')
                yield chunk
        response.iter_content = chunks
        if not streaming:
            try:
                response.content
            except Exception:
                response.close()
                raise
        return response
