import json

import pytest

from bb2gh.http import ApiError
from bb2gh.migrate import Migrator, Options
from bb2gh.render import UserMap
from bb2gh.state import State

REPO = {"full_name": "acme/widgets"}
ALICE = {"account_id": "a1", "display_name": "Alice", "nickname": "alice"}
BOB = {"account_id": "b2", "display_name": "Bob", "nickname": "bob"}


def make_pr(pr_id, state="MERGED", src_sha="aaaaaaaaaaaa", dst_sha="bbbbbbbbbbbb"):
  return {
    "id": pr_id,
    "title": f"PR {pr_id}",
    "state": state,
    "description": "Fixes #3, cc @bob",
    "author": ALICE,
    "closed_by": BOB if state != "OPEN" else None,
    "created_on": "2024-01-02T03:04:05.000000+00:00",
    "updated_on": "2024-01-03T03:04:05.000000+00:00",
    "source": {"branch": {"name": "feature/x"}, "commit": {"hash": src_sha}, "repository": REPO},
    "destination": {"branch": {"name": "main"}, "commit": {"hash": dst_sha}, "repository": REPO},
    "participants": [{"user": BOB, "role": "REVIEWER", "approved": True}],
    "links": {"html": {"href": f"https://bitbucket.org/acme/widgets/pull-requests/{pr_id}"}},
  }


def comment(cid, text, user=BOB, inline=None, parent=None):
  c = {"id": cid, "content": {"raw": text}, "user": user, "created_on": f"2024-01-02T0{cid}:00:00+00:00"}
  if inline:
    c["inline"] = inline
  if parent:
    c["parent"] = {"id": parent}
  return c


class FakeBitbucket:
  def __init__(self, prs, comments):
    self.prs = {pr["id"]: pr for pr in prs}
    self.comments = comments

  def list_pull_requests(self, states):
    return [pr for pr in self.prs.values() if pr["state"] in states]

  def get_pull_request(self, pr_id):
    return self.prs[pr_id]

  def list_comments(self, pr_id):
    return self.comments.get(pr_id, [])


class FakeGitHub:
  def __init__(self, commits=("aaaaaaaaaaaa", "bbbbbbbbbbbb"), branches=(), reject_inline=False, reject_pull=False):
    self.commits = set(commits)
    self.branches = set(branches)
    self.reject_inline = reject_inline
    self.reject_pull = reject_pull
    self.calls = []
    self.next_id = 100

  def _id(self):
    self.next_id += 1
    return self.next_id

  def resolve_commit(self, sha):
    return (sha + "0" * 28) if sha in self.commits else None

  def branch_exists(self, name):
    return name in self.branches

  def ensure_branch(self, name, sha):
    self.calls.append(("ensure_branch", name, sha))
    self.branches.add(name)

  def delete_branch(self, name):
    self.calls.append(("delete_branch", name))
    self.branches.discard(name)

  def create_pull(self, title, head, base, body, draft=False):
    if self.reject_pull:
      raise ApiError(422, "POST", "/pulls", json.dumps({"errors": [{"message": "No commits between"}]}))
    self.calls.append(("create_pull", head, base, body))
    return self._id()

  def create_issue(self, title, body):
    self.calls.append(("create_issue", title, body))
    return self._id()

  def close_pull(self, number):
    self.calls.append(("close_pull", number))

  def close_issue(self, number, reason="completed"):
    self.calls.append(("close_issue", number, reason))

  def add_labels(self, number, labels):
    self.calls.append(("add_labels", number, tuple(labels)))

  def create_issue_comment(self, number, body):
    self.calls.append(("issue_comment", number, body))
    return self._id()

  def create_review_comment(self, number, body, commit_id, path, line=None, side="RIGHT"):
    if self.reject_inline:
      raise ApiError(422, "POST", "/comments", "line must be part of the diff")
    self.calls.append(("review_comment", number, path, line, side))
    return self._id()

  def reply_review_comment(self, number, comment_id, body):
    self.calls.append(("reply", number, comment_id))
    return self._id()


def names(gh):
  return [c[0] for c in gh.calls]


def run(bb, gh, tmp_path, **opts):
  state = State(str(tmp_path / "state.json"))
  migrator = Migrator(bb, gh, UserMap({"b2": "bob-gh"}), state, Options(delay=0, **opts), sleep=lambda s: None)
  return migrator, migrator.run(), state


def test_merged_pr_becomes_closed_pull_with_threaded_review_comments(tmp_path):
  comments = {1: [
    comment(1, "looks good"),
    comment(2, "nit here", inline={"path": "app.py", "to": 10, "from": None}),
    comment(3, "fixed", user=ALICE, parent=2),
  ]}
  gh = FakeGitHub()
  _, counts, state = run(FakeBitbucket([make_pr(1)], comments), gh, tmp_path)

  assert counts == {"pull": 1}
  assert names(gh) == [
    "ensure_branch", "ensure_branch", "create_pull", "issue_comment", "review_comment", "reply",
    "add_labels", "close_pull", "delete_branch", "delete_branch",
  ]
  pull = gh.calls[2]
  assert pull[1:3] == ("bitbucket-pr/1/head", "bitbucket-pr/1/base")
  assert "Bob (`bob-gh`) (approved)" in pull[3]
  assert "@<!-- -->bob" in pull[3] and "#<!-- -->3" in pull[3]
  assert gh.calls[4][2:] == ("app.py", 10, "RIGHT")
  assert ("add_labels", 101, ("migrated-from-bitbucket", "bitbucket:merged")) in gh.calls
  assert state.pr(1)["done"] is True


def test_rerun_skips_finished_prs(tmp_path):
  bb = FakeBitbucket([make_pr(1)], {1: [comment(1, "hi")]})
  run(bb, FakeGitHub(), tmp_path)
  gh = FakeGitHub()
  _, counts, _ = run(bb, gh, tmp_path)
  assert counts == {"skipped": 1}
  assert gh.calls == []


def test_resume_does_not_duplicate_posted_comments(tmp_path):
  bb = FakeBitbucket([make_pr(1)], {1: [comment(1, "one"), comment(2, "two")]})
  state = State(str(tmp_path / "state.json"))
  rec = state.pr(1)
  rec.update(kind="pull", number=55, head_sha="a" * 40)
  rec["comments"]["1"] = {"kind": "issue", "id": 9}
  state.save()

  gh = FakeGitHub()
  run(bb, gh, tmp_path)
  assert [c for c in gh.calls if c[0] == "issue_comment"] == [("issue_comment", 55, gh.calls[0][2])]
  assert "two" in gh.calls[0][2]


def test_missing_commit_falls_back_to_closed_issue(tmp_path):
  comments = {7: [comment(1, "inline", inline={"path": "a.py", "to": 2})]}
  gh = FakeGitHub(commits=("bbbbbbbbbbbb",))
  _, counts, _ = run(FakeBitbucket([make_pr(7, state="DECLINED")], comments), gh, tmp_path)

  assert counts == {"issue": 1}
  assert names(gh) == ["create_issue", "issue_comment", "add_labels", "close_issue"]
  assert "recreated it as an issue" in gh.calls[0][2]
  assert "On `a.py` line 2" in gh.calls[1][2]
  assert gh.calls[3] == ("close_issue", 101, "not_planned")


def test_rejected_pull_cleans_up_branches_and_uses_issue(tmp_path):
  gh = FakeGitHub(reject_pull=True)
  _, counts, state = run(FakeBitbucket([make_pr(2)], {}), gh, tmp_path)
  assert counts == {"issue": 1}
  assert ("delete_branch", "bitbucket-pr/2/head") in gh.calls
  assert "No commits between" in state.pr(2)["note"]


def test_inline_rejection_posts_regular_comment_with_context(tmp_path):
  comments = {1: [comment(1, "old line", inline={"path": "b.py", "from": 4})]}
  gh = FakeGitHub(reject_inline=True)
  run(FakeBitbucket([make_pr(1)], comments), gh, tmp_path)
  posted = [c for c in gh.calls if c[0] == "issue_comment"]
  assert "On `b.py` line 4 (old version)" in posted[0][2]


def test_open_pr_reuses_live_branches_and_stays_open(tmp_path):
  gh = FakeGitHub(branches={"feature/x", "main"})
  run(FakeBitbucket([make_pr(3, state="OPEN")], {}), gh, tmp_path)
  assert ("create_pull", "feature/x", "main") == gh.calls[0][:3]
  assert "close_pull" not in names(gh) and "delete_branch" not in names(gh)


def test_dry_run_writes_nothing(tmp_path):
  gh = FakeGitHub()
  state = State(None)
  counts = Migrator(FakeBitbucket([make_pr(1)], {}), gh, UserMap(), state, Options(dry_run=True)).run()
  assert counts == {"dry-run-pull": 1}
  assert gh.calls == []


def test_non_422_errors_count_as_failures(tmp_path):
  class Broken(FakeGitHub):
    def create_pull(self, *a, **k):
      raise ApiError(500, "POST", "/pulls", "boom")

  _, counts, _ = run(FakeBitbucket([make_pr(1)], {}), Broken(), tmp_path)
  assert counts == {"failed": 1}


@pytest.mark.parametrize("notify, expected", [(False, "`bob-gh`"), (True, "@bob-gh")])
def test_bitbucket_mentions_map_to_github_logins(tmp_path, notify, expected):
  from bb2gh.render import convert_text

  users = UserMap({"b2": "bob-gh"}, notify=notify)
  users.remember(BOB)
  out = convert_text("ping @{b2} and `@{b2}` in code", users)
  assert out.startswith(f"ping {expected} and")
  assert out.endswith("`@{b2}` in code")


def test_notify_does_not_ping_unmapped_users():
  from bb2gh.render import convert_text

  users = UserMap({"bobnick": "bob-gh"}, notify=True)
  out = convert_text("hi @bobnick and @torvalds", users)
  assert out == "hi @bob-gh and @<!-- -->torvalds"


def test_truncate_closes_open_fence():
  from bb2gh.render import MAX_BODY, truncate

  out = truncate("```\n" + "x" * (MAX_BODY + 10))
  assert out.count("```") % 2 == 0


def test_state_rejects_other_target(tmp_path):
  import pytest
  from bb2gh.state import State

  state = State(str(tmp_path / "s.json"))
  state.bind("a/b")
  state.save()
  with pytest.raises(ValueError):
    State(str(tmp_path / "s.json")).bind("c/d")
