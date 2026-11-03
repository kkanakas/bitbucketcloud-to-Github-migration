"""Turn Bitbucket PR data into GitHub-flavored Markdown."""

import re
from datetime import datetime, timezone

MAX_BODY = 65000  # GitHub rejects bodies above 65536 characters

CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
BB_MENTION = re.compile(r"@\{([^}]+)\}")
PLAIN_MENTION = re.compile(r"(?<![\w`])@([A-Za-z0-9][\w.-]*)")
ISSUE_REF = re.compile(r"(?<![\w&/])#(\d+)")


class UserMap:
  """Maps Bitbucket users (account_id, uuid or nickname) to GitHub logins."""

  def __init__(self, mapping=None, notify=False):
    self.mapping = {}
    for key, value in (mapping or {}).items():
      login = value.get("github") if isinstance(value, dict) else value
      if login:
        self.mapping[key] = login
        self.mapping[key.lower()] = login
    self.notify = notify
    self.seen = {}  # account_id/uuid -> user object, used to resolve @{id} mentions

  def remember(self, user):
    if not user:
      return
    for key in ("account_id", "uuid"):
      if user.get(key):
        self.seen[user[key]] = user

  def login_for(self, user):
    for key in ("account_id", "uuid", "nickname"):
      value = user.get(key)
      if value and (value in self.mapping or value.lower() in self.mapping):
        return self.mapping.get(value) or self.mapping[value.lower()]
    return None

  def mention(self, login):
    return f"@{login}" if self.notify else f"`{login}`"

  def describe(self, user):
    if not user:
      return "Unknown user"
    name = user.get("display_name") or user.get("nickname") or "Unknown user"
    login = self.login_for(user)
    return f"{name} ({self.mention(login)})" if login else name


def fmt_date(value):
  if not value:
    return "unknown date"
  try:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo:
      parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")
  except ValueError:
    return value


def short(sha):
  return (sha or "")[:12] or "unknown"


def convert_text(text, users):
  """Rewrite mentions and #refs outside code so GitHub does not ping or link the wrong things."""
  parts = CODE_SPLIT.split(text or "")
  for i in range(0, len(parts), 2):  # even indexes sit outside code spans
    part = PLAIN_MENTION.sub(lambda m: _plain_mention(m.group(1), users), parts[i])
    part = BB_MENTION.sub(lambda m: _bb_mention(m.group(1), users), part)
    parts[i] = ISSUE_REF.sub(r"#<!-- -->\1", part)
  return "".join(parts)


def _plain_mention(name, users):
  """Only mapped users may become real mentions; anything else is defused so nobody random is pinged."""
  login = users.login_for({"nickname": name})
  if login and users.notify:
    return f"@{login}"
  if login:
    return f"`{login}`"
  return f"@<!-- -->{name}"


def _bb_mention(user_id, users):
  user = users.seen.get(user_id) or {"account_id": user_id}
  login = users.login_for(user)
  if login:
    return users.mention(login)
  return user.get("display_name") or f"`{user_id}`"


def truncate(body):
  if len(body) <= MAX_BODY:
    return body
  cut = body[:MAX_BODY]
  if cut.count("```") % 2:
    cut += "\n```"
  return cut + "\n\n_[truncated during migration]_"


def code(text):
  """Inline code span that survives backticks inside the text."""
  text = str(text)
  fence = "``" if "`" in text else "`"
  return f"{fence} {text} {fence}" if "`" in text else f"`{text}`"


def pr_link(pr):
  return pr.get("links", {}).get("html", {}).get("href", "")


def pr_body(pr, users, note=None):
  src, dst = pr.get("source", {}), pr.get("destination", {})
  src_repo = src.get("repository", {}).get("full_name", "")
  dst_repo = dst.get("repository", {}).get("full_name", "")
  src_label = f"{src_repo}:" if src_repo and src_repo != dst_repo else ""
  state = pr.get("state", "UNKNOWN")

  meta = [
    f"> **Migrated from Bitbucket Cloud:** [{dst_repo} PR #{pr['id']}]({pr_link(pr)})",
    f"> **Author:** {users.describe(pr.get('author'))} · **Created:** {fmt_date(pr.get('created_on'))}",
    f"> **Source:** `{src_label}{src.get('branch', {}).get('name', '?')}` ({short(src.get('commit', {}).get('hash'))})"
    f" → **Destination:** `{dst.get('branch', {}).get('name', '?')}` ({short(dst.get('commit', {}).get('hash'))})",
  ]
  if state == "OPEN":
    meta.append("> **State:** OPEN")
  else:
    closer = f" by {users.describe(pr['closed_by'])}" if pr.get("closed_by") else ""
    meta.append(f"> **State:** {state}{closer} on {fmt_date(pr.get('updated_on'))}")
  if pr.get("merge_commit"):
    meta.append(f"> **Merge commit:** {pr['merge_commit'].get('hash')}")

  reviewers = [p for p in pr.get("participants", []) if p.get("role") == "REVIEWER" or p.get("approved")]
  if reviewers:
    rendered = []
    for p in reviewers:
      status = "approved" if p.get("approved") else (p.get("state") or "no decision").replace("_", " ")
      rendered.append(f"{users.describe(p.get('user'))} ({status})")
    meta.append(f"> **Reviewers:** {', '.join(rendered)}")
  if note:
    meta.append(f">\n> _{note}_")

  description = convert_text(pr.get("description") or pr.get("summary", {}).get("raw") or "", users)
  return truncate("\n".join(meta) + "\n\n---\n\n" + (description or "_No description provided._"))


def comment_body(comment, users, context=None):
  link = comment.get("links", {}).get("html", {}).get("href")
  header = f"> **{users.describe(comment.get('user'))}** commented on {fmt_date(comment.get('created_on'))}"
  if link:
    header += f" · [original]({link})"
  if comment.get("updated_on") and comment.get("updated_on") != comment.get("created_on"):
    header += " · edited"
  lines = [header]
  if context:
    lines.append(f"> {context}")
  text = convert_text(comment.get("content", {}).get("raw", ""), users)
  return truncate("\n".join(lines) + "\n\n" + (text or "_(empty comment)_"))


def inline_context(inline):
  path = code(inline.get("path", "?"))
  if inline.get("to"):
    return f"On {path} line {inline['to']}"
  if inline.get("from"):
    return f"On {path} line {inline['from']} (old version)"
  return f"On {path}"
