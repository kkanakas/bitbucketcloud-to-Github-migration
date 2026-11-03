"""GitHub / GitHub Enterprise REST API client (only the calls the migration needs)."""

from urllib.parse import quote

import requests

from .http import ApiError, HttpClient

DEFAULT_API_URL = "https://api.github.com"


class GitHubClient:
  def __init__(self, owner, repo, http):
    self.owner = owner
    self.repo = repo
    self.http = http
    self.repo_path = f"/repos/{quote(owner)}/{quote(repo)}"

  @classmethod
  def from_token(cls, full_name, token, api_url=DEFAULT_API_URL):
    owner, _, repo = full_name.partition("/")
    if not owner or not repo:
      raise ValueError(f"GitHub repository must look like owner/name, got {full_name!r}")
    session = requests.Session()
    session.headers.update({
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    })
    return cls(owner, repo, HttpClient(api_url, session))

  def _req(self, method, path, **kwargs):
    return self.http.request(method, f"{self.repo_path}{path}", **kwargs)

  def resolve_commit(self, sha):
    """Expand a (possibly short) SHA to the full SHA; None when the commit is absent."""
    if not sha:
      return None
    try:
      return self._req("GET", f"/commits/{quote(sha)}")["sha"]
    except ApiError as err:
      if err.status in (404, 422):
        return None
      raise

  def first_parent(self, sha):
    try:
      parents = self._req("GET", f"/commits/{quote(sha)}")["parents"]
    except ApiError as err:
      if err.status in (404, 422):
        return None
      raise
    return parents[0]["sha"] if parents else None

  def branch_exists(self, name):
    try:
      self._req("GET", f"/git/ref/heads/{quote(name, safe='/')}")
      return True
    except ApiError as err:
      if err.status == 404:
        return False
      raise

  def ensure_branch(self, name, sha):
    """Create the branch at sha, or force-move it there if it already exists."""
    try:
      self._req("POST", "/git/refs", json={"ref": f"refs/heads/{name}", "sha": sha})
    except ApiError as err:
      if err.status != 422 or "already exists" not in err.body:
        raise
      self._req("PATCH", f"/git/refs/heads/{quote(name, safe='/')}", json={"sha": sha, "force": True})

  def delete_branch(self, name):
    try:
      self._req("DELETE", f"/git/refs/heads/{quote(name, safe='/')}")
    except ApiError as err:
      if err.status not in (404, 422):
        raise

  def create_pull(self, title, head, base, body, draft=False):
    data = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
    return self._req("POST", "/pulls", json=data)["number"]

  def close_pull(self, number):
    self._req("PATCH", f"/pulls/{number}", json={"state": "closed"})

  def create_issue(self, title, body):
    return self._req("POST", "/issues", json={"title": title, "body": body})["number"]

  def close_issue(self, number, reason="completed"):
    self._req("PATCH", f"/issues/{number}", json={"state": "closed", "state_reason": reason})

  def add_labels(self, number, labels):
    self._req("POST", f"/issues/{number}/labels", json={"labels": labels})

  def create_issue_comment(self, number, body):
    return self._req("POST", f"/issues/{number}/comments", json={"body": body})["id"]

  def create_review_comment(self, number, body, commit_id, path, line=None, side="RIGHT"):
    data = {"body": body, "commit_id": commit_id, "path": path}
    if line:
      data.update({"line": line, "side": side})
    else:
      data["subject_type"] = "file"
    return self._req("POST", f"/pulls/{number}/comments", json=data)["id"]

  def reply_review_comment(self, number, comment_id, body):
    return self._req("POST", f"/pulls/{number}/comments/{comment_id}/replies", json={"body": body})["id"]
