"""Core migration logic: one Bitbucket PR becomes one GitHub PR (or issue) plus its comments."""

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass

from .bitbucket import ALL_STATES
from .http import ApiError
from .render import comment_body, inline_context, pr_body

log = logging.getLogger(__name__)

MISSING_COMMIT_NOTE = (
  "The source or destination commit of this pull request does not exist in the GitHub "
  "repository, so the migration recreated it as an issue."
)


@dataclass
class Options:
  dry_run: bool = False
  keep_branches: bool = False
  delay: float = 1.0
  branch_prefix: str = "bitbucket-pr"
  label: str = "migrated-from-bitbucket"


class Migrator:
  def __init__(self, bb, gh, users, state, options=None, sleep=time.sleep):
    self.bb = bb
    self.gh = gh
    self.users = users
    self.state = state
    self.opts = options or Options()
    self.sleep = sleep

  def run(self, states=ALL_STATES, only_ids=None, limit=None):
    ids = list(only_ids) if only_ids else [pr["id"] for pr in self.bb.list_pull_requests(states)]
    if limit:
      ids = ids[:limit]
    log.info(f"Migrating {len(ids)} pull request(s)")
    counts = Counter()
    for pr_id in ids:
      try:
        counts[self.migrate_pr(pr_id)] += 1
      except ApiError as err:
        log.error(f"PR #{pr_id} failed: {err}")
        counts["failed"] += 1
      except Exception:
        log.exception(f"PR #{pr_id} failed unexpectedly")
        counts["failed"] += 1
    return counts

  def migrate_pr(self, pr_id):
    rec = self.state.pr(pr_id)
    if rec["done"]:
      log.info(f"PR #{pr_id}: already migrated to #{rec['number']}, skipping")
      return "skipped"

    pr = self.bb.get_pull_request(pr_id)
    comments = self.bb.list_comments(pr_id)
    self._remember_users(pr, comments)

    if self.opts.dry_run:
      return self._plan(pr, comments)

    if not rec.get("number"):
      self._create_target(pr, rec)
      self.sleep(self.opts.delay)
    self._migrate_comments(comments, rec)
    self._finalize(pr, rec)
    log.info(f"PR #{pr_id} ({pr['state']}) -> {rec['kind']} #{rec['number']} with {len(comments)} comment(s)")
    return rec["kind"]

  def _remember_users(self, pr, comments):
    self.users.remember(pr.get("author"))
    self.users.remember(pr.get("closed_by"))
    for p in pr.get("participants", []):
      self.users.remember(p.get("user"))
    for c in comments:
      self.users.remember(c.get("user"))

  def _plan(self, pr, comments):
    shas = self._resolve_shas(pr)
    kind = "pull" if all(shas) else "issue"
    inline = sum(1 for c in comments if c.get("inline"))
    log.info(
      f"[dry-run] PR #{pr['id']} ({pr['state']}) -> would create {kind} with "
      f"{len(comments)} comment(s), {inline} inline: {pr['title']}"
    )
    return f"dry-run-{kind}"

  def _resolve_shas(self, pr):
    head = self.gh.resolve_commit(pr.get("source", {}).get("commit", {}).get("hash"))
    base = self.gh.resolve_commit(pr.get("destination", {}).get("commit", {}).get("hash"))
    merge_hash = (pr.get("merge_commit") or {}).get("hash")
    if pr.get("state") == "MERGED" and merge_hash:
      # After a merge the head is already reachable from the destination tip, which GitHub rejects
      # ("no commits between"); diff against the destination as it was just before the merge instead.
      merge_sha = self.gh.resolve_commit(merge_hash)
      before = self.gh.first_parent(merge_sha) if merge_sha else None
      if before:
        base = before
    return head, base

  def _create_target(self, pr, rec):
    head_sha, base_sha = self._resolve_shas(pr)
    note = MISSING_COMMIT_NOTE
    if head_sha and base_sha:
      head, base = self._prepare_branches(pr, rec, head_sha, base_sha)
      is_open = pr["state"] == "OPEN"
      try:
        number = self.gh.create_pull(
          pr["title"], head, base, pr_body(pr, self.users), draft=bool(is_open and pr.get("draft"))
        )
        rec.update(kind="pull", number=number, head_sha=head_sha)
        self.state.save()
        return
      except ApiError as err:
        if err.status != 422:
          raise
        note = f"GitHub rejected the pull request ({_error_message(err)}), so the migration recreated it as an issue."
        log.warning(f"PR #{pr['id']}: {note}")
        self._delete_branches(rec)

    number = self.gh.create_issue(pr["title"], pr_body(pr, self.users, note=note))
    rec.update(kind="issue", number=number, note=note)
    self.state.save()

  def _prepare_branches(self, pr, rec, head_sha, base_sha):
    """Open PRs reuse live branches when they exist; everything else gets temporary branches."""
    prefix = f"{self.opts.branch_prefix}/{pr['id']}"
    src, dst = pr["source"], pr["destination"]
    same_repo = src.get("repository", {}).get("full_name") == dst.get("repository", {}).get("full_name")
    is_open = pr["state"] == "OPEN"

    src_branch = src.get("branch", {}).get("name")
    if is_open and same_repo and src_branch and self.gh.branch_exists(src_branch):
      head = src_branch
    else:
      head = self._temp_branch(rec, f"{prefix}/head", head_sha)

    dst_branch = dst.get("branch", {}).get("name")
    if is_open and dst_branch and self.gh.branch_exists(dst_branch):
      base = dst_branch
    else:
      base = self._temp_branch(rec, f"{prefix}/base", base_sha)
    return head, base

  def _temp_branch(self, rec, name, sha):
    self.gh.ensure_branch(name, sha)
    if name not in rec["branches"]:
      rec["branches"].append(name)
      self.state.save()
    return name

  def _delete_branches(self, rec):
    for name in rec["branches"]:
      self.gh.delete_branch(name)
    rec["branches"] = []
    self.state.save()

  def _migrate_comments(self, comments, rec):
    for comment in comments:
      cid = str(comment["id"])
      if cid in rec["comments"]:
        continue
      rec["comments"][cid] = self._post_comment(comment, rec)
      self.state.save()
      self.sleep(self.opts.delay)

  def _post_comment(self, comment, rec):
    number = rec["number"]
    inline = comment.get("inline")
    parent_id = str(comment["parent"]["id"]) if comment.get("parent") else None
    parent = rec["comments"].get(parent_id) if parent_id else None

    if rec["kind"] == "pull":
      try:
        if parent and parent["kind"] == "review":
          gh_id = self.gh.reply_review_comment(number, parent["id"], comment_body(comment, self.users))
          return {"kind": "review", "id": gh_id}
        if inline and not parent_id:
          line, side = (inline["to"], "RIGHT") if inline.get("to") else (inline.get("from"), "LEFT")
          gh_id = self.gh.create_review_comment(
            number, comment_body(comment, self.users), rec["head_sha"], inline["path"], line, side
          )
          return {"kind": "review", "id": gh_id}
      except ApiError as err:
        if err.status != 422:
          raise
        log.info(f"Comment {comment['id']}: GitHub refused the inline position, posting as a regular comment")

    context = []
    if inline:
      context.append(inline_context(inline))
    if parent_id:
      parent_link = comment["parent"].get("links", {}).get("html", {}).get("href")
      context.append(f"In reply to [this comment]({parent_link})" if parent_link else "In reply to an earlier comment")
    body = comment_body(comment, self.users, " · ".join(context) or None)
    return {"kind": "issue", "id": self.gh.create_issue_comment(number, body)}

  def _finalize(self, pr, rec):
    state = pr["state"]
    if not rec.get("labeled"):
      self.gh.add_labels(rec["number"], [self.opts.label, f"bitbucket:{state.lower()}"])
      rec["labeled"] = True
      self.state.save()
    if state != "OPEN":
      if rec["kind"] == "pull":
        self.gh.close_pull(rec["number"])
      else:
        self.gh.close_issue(rec["number"], "completed" if state == "MERGED" else "not_planned")
      if not self.opts.keep_branches:
        self._delete_branches(rec)
    rec["done"] = True
    self.state.save()


def _error_message(err):
  try:
    data = json.loads(err.body)
  except ValueError:
    return f"HTTP {err.status}"
  details = [e.get("message") for e in data.get("errors", []) if isinstance(e, dict) and e.get("message")]
  return "; ".join(details) or data.get("message") or f"HTTP {err.status}"
