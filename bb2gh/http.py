"""Shared HTTP client with retries and rate-limit handling."""

import logging
import time

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {500, 502, 503, 504}
IDEMPOTENT = {"GET", "HEAD", "PUT", "PATCH", "DELETE"}


class ApiError(Exception):
  def __init__(self, status, method, url, body):
    super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")
    self.status = status
    self.body = body


class HttpClient:
  def __init__(self, base_url, session=None, max_retries=5, sleep=time.sleep, timeout=60):
    self.base_url = base_url.rstrip("/")
    self.session = session or requests.Session()
    self.max_retries = max_retries
    self.sleep = sleep
    self.timeout = timeout

  def request(self, method, path, **kwargs):
    url = path if path.startswith("http") else f"{self.base_url}{path}"
    resp = None
    for attempt in range(self.max_retries + 1):
      try:
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
      except requests.RequestException as err:
        # A failed write may still have been applied server-side, so only idempotent calls are retried.
        if method.upper() not in IDEMPOTENT or attempt >= self.max_retries:
          raise
        wait = min(2 ** attempt, 60)
        log.warning(f"{method} {url} failed ({err}); retrying in {wait}s")
        self.sleep(wait)
        continue
      wait = self._retry_wait(resp, attempt, method)
      if wait is None:
        break
      log.warning(f"{method} {url} returned {resp.status_code}; retrying in {wait:.0f}s")
      self.sleep(wait)
    if resp.status_code >= 400:
      raise ApiError(resp.status_code, method, url, resp.text)
    if resp.status_code == 204 or not resp.content:
      return None
    return resp.json()

  def _retry_wait(self, resp, attempt, method="GET"):
    if attempt >= self.max_retries:
      return None
    code = resp.status_code
    if code in RETRYABLE_STATUS:
      if method.upper() not in IDEMPOTENT:
        return None  # the write may have been applied; retrying could duplicate it
      return min(2 ** attempt, 60)
    rate_limited = code == 429 or (
      code == 403
      and (resp.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in resp.text.lower())
    )
    if not rate_limited:
      return None
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
      return float(retry_after) + 1
    reset = resp.headers.get("X-RateLimit-Reset")
    if resp.headers.get("X-RateLimit-Remaining") == "0" and reset:
      return max(float(reset) - time.time(), 1) + 1
    # GitHub secondary rate limits: wait at least a minute, then back off.
    return 60 * (attempt + 1)
