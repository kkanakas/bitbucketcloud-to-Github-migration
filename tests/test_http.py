import pytest

from bb2gh.http import ApiError, HttpClient


class FakeResponse:
  def __init__(self, status, body="{}", headers=None):
    self.status_code = status
    self.text = body
    self.content = body.encode()
    self.headers = headers or {}

  def json(self):
    import json
    return json.loads(self.text)


class FakeSession:
  def __init__(self, responses):
    self.responses = list(responses)
    self.urls = []

  def request(self, method, url, **kwargs):
    self.urls.append(url)
    return self.responses.pop(0)


def test_retries_secondary_rate_limit_using_retry_after():
  waits = []
  session = FakeSession([
    FakeResponse(403, "You have exceeded a secondary rate limit", {"Retry-After": "5"}),
    FakeResponse(201, '{"id": 1}'),
  ])
  client = HttpClient("https://ghe.example.com/api/v3/", session, sleep=waits.append)
  assert client.request("POST", "/repos/a/b/issues") == {"id": 1}
  assert waits == [6.0]
  assert session.urls[0] == "https://ghe.example.com/api/v3/repos/a/b/issues"


def test_plain_403_raises_without_retry():
  client = HttpClient("https://x", FakeSession([FakeResponse(403, "Resource not accessible")]), sleep=None)
  with pytest.raises(ApiError) as err:
    client.request("GET", "/y")
  assert err.value.status == 403


def test_absolute_next_links_pass_through():
  session = FakeSession([FakeResponse(200, '{"values": []}')])
  HttpClient("https://api.bitbucket.org/2.0", session).request("GET", "https://api.bitbucket.org/2.0/next?page=2")
  assert session.urls == ["https://api.bitbucket.org/2.0/next?page=2"]


def test_post_is_not_retried_on_network_error():
  import pytest
  import requests
  from bb2gh.http import HttpClient

  class S:
    calls = 0

    def request(self, *a, **k):
      S.calls += 1
      raise requests.ConnectionError("boom")

  client = HttpClient("http://x", S(), sleep=lambda s: None)
  with pytest.raises(requests.ConnectionError):
    client.request("POST", "/a")
  assert S.calls == 1
  with pytest.raises(requests.ConnectionError):
    client.request("GET", "/a")
  assert S.calls == 1 + 6
