"""Read-only Bitbucket Cloud REST API 2.0 client."""

from urllib.parse import quote

import requests

from .http import HttpClient

DEFAULT_BASE_URL = "https://api.bitbucket.org/2.0"
ALL_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")


class BitbucketClient:
  def __init__(self, workspace, repo, http):
    self.workspace = workspace
    self.repo = repo
    self.http = http
    self.repo_path = f"/repositories/{quote(workspace)}/{quote(repo)}"

  @classmethod
  def from_credentials(cls, workspace, repo, token=None, email=None, api_token=None,
                       base_url=DEFAULT_BASE_URL):
    """Use a repository/workspace access token (Bearer) or an Atlassian API token (Basic)."""
    session = requests.Session()
    session.headers["Accept"] = "application/json"
    if token:
      session.headers["Authorization"] = f"Bearer {token}"
    elif email and api_token:
      session.auth = (email, api_token)
    else:
      raise ValueError("Set BITBUCKET_TOKEN, or BITBUCKET_EMAIL and BITBUCKET_API_TOKEN")
    return cls(workspace, repo, HttpClient(base_url, session))

  def _paginate(self, path, params=None):
    url = f"{self.repo_path}{path}"
    while url:
      page = self.http.request("GET", url, params=params)
      yield from page.get("values", [])
      url = page.get("next")
      params = None  # the "next" link already carries the query string

  def list_pull_requests(self, states=ALL_STATES):
    params = [("state", s) for s in states] + [("pagelen", "50")]
    prs = list(self._paginate("/pullrequests", params))
    return sorted(prs, key=lambda pr: pr["id"])

  def get_pull_request(self, pr_id):
    return self.http.request("GET", f"{self.repo_path}/pullrequests/{pr_id}")

  def list_comments(self, pr_id):
    comments = self._paginate(f"/pullrequests/{pr_id}/comments", [("pagelen", "100")])
    live = [c for c in comments if not c.get("deleted")]
    return sorted(live, key=lambda c: (c.get("created_on", ""), c["id"]))
