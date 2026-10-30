"""
Shared HTTP helper: retries temporary failures, fails fast on real errors,
and always reports the server's actual message.
"""
import time

import requests

HEADERS = {"User-Agent": "forge26-supply-agent/0.1 (hackathon project)"}
RETRYABLE = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """A data source failed. The message says which one and why."""


def request(method, url, *, retries=2, backoff=1.5, timeout=25, headers=None, **kwargs):
    merged = {**HEADERS, **(headers or {})}
    last_error = None

    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, headers=merged, timeout=timeout, **kwargs)
            if r.status_code < 400:
                return r
            msg = f"HTTP {r.status_code} from {url}: {r.text[:200]}"
            if r.status_code not in RETRYABLE:
                raise SourceError(msg)  # e.g. 400 bad request: retrying won't help
            last_error = SourceError(msg)
        except requests.RequestException as e:
            last_error = SourceError(f"{type(e).__name__} calling {url}: {e}")

        if attempt < retries:
            time.sleep(backoff * (attempt + 1))

    raise last_error
