"""Bound one collection step without fabricating missing observations."""
import time

import requests


class BudgetSession(requests.Session):
    def __init__(self, seconds=60):
        super().__init__()
        self.deadline = time.monotonic() + seconds

    @property
    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def request(self, method, url, **kwargs):
        remaining = self.remaining
        if remaining < 0.1:
            raise requests.Timeout('collection_step_budget_exhausted')
        configured = kwargs.get('timeout', (5, 20))
        connect, read = configured if isinstance(configured, tuple) else (configured, configured)
        kwargs['timeout'] = (min(connect, remaining / 2), min(read, remaining / 2))
        streaming = kwargs.get('stream', False)
        kwargs['stream'] = True
        response = super().request(method, url, **kwargs)
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
