"""Command-line entry point: `bb2gh migrate` and `bb2gh users`."""

import argparse
import json
import logging
import os
import sys

from .bitbucket import ALL_STATES, DEFAULT_BASE_URL, BitbucketClient
from .github import DEFAULT_API_URL, GitHubClient
from .migrate import Migrator, Options
from .render import UserMap
from .state import State


def build_parser():
  parser = argparse.ArgumentParser(
    prog="bb2gh", description="Migrate pull request history from Bitbucket Cloud to GitHub / GitHub Enterprise."
  )
  parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
  sub = parser.add_subparsers(dest="command", required=True)

  common = argparse.ArgumentParser(add_help=False)
  common.add_argument("--bitbucket-workspace", required=True)
  common.add_argument("--bitbucket-repo", required=True)
  common.add_argument("--bitbucket-api-url", default=DEFAULT_BASE_URL)
  common.add_argument("--states", default=",".join(ALL_STATES),
                      help="comma-separated Bitbucket PR states (default: all)")

  mig = sub.add_parser("migrate", parents=[common], help="migrate pull requests and comments")
  mig.add_argument("--github-repo", required=True, help="target repository as owner/name")
  mig.add_argument("--github-api-url", default=os.environ.get("GITHUB_API_URL", DEFAULT_API_URL),
                   help="GHE Server: https://HOST/api/v3, GHE.com: https://api.SUBDOMAIN.ghe.com")
  mig.add_argument("--pr", type=int, action="append", dest="pr_ids", help="migrate only this PR id (repeatable)")
  mig.add_argument("--limit", type=int, help="migrate at most N pull requests")
  mig.add_argument("--user-map", help="JSON file mapping Bitbucket users to GitHub logins")
  mig.add_argument("--state-file", help="progress file (default: bb2gh-state-<workspace>-<repo>.json)")
  mig.add_argument("--dry-run", action="store_true", help="read everything, write nothing to GitHub")
  mig.add_argument("--keep-branches", action="store_true", help="keep temporary branches for closed PRs")
  mig.add_argument("--notify-users", action="store_true", help="render mapped users as real @mentions")
  mig.add_argument("--delay", type=float, default=1.0, help="seconds between comment writes (default 1.0)")
  mig.add_argument("--label", default="migrated-from-bitbucket")
  mig.add_argument("--branch-prefix", default="bitbucket-pr")

  sub.add_parser("users", parents=[common], help="print a user-map template of everyone active in PRs")
  return parser


def bitbucket_client(args):
  return BitbucketClient.from_credentials(
    args.bitbucket_workspace,
    args.bitbucket_repo,
    token=os.environ.get("BITBUCKET_TOKEN"),
    email=os.environ.get("BITBUCKET_EMAIL"),
    api_token=os.environ.get("BITBUCKET_API_TOKEN"),
    base_url=args.bitbucket_api_url,
  )


def parse_states(raw):
  states = [s.strip().upper() for s in raw.split(",") if s.strip()]
  unknown = set(states) - set(ALL_STATES)
  if unknown:
    raise SystemExit(f"Unknown state(s): {', '.join(sorted(unknown))}")
  return states


def cmd_migrate(args):
  token = os.environ.get("GITHUB_TOKEN")
  if not token:
    raise SystemExit("Set GITHUB_TOKEN")
  mapping = {}
  if args.user_map:
    with open(args.user_map, encoding="utf-8") as fh:
      mapping = json.load(fh)
  state_file = args.state_file or f"bb2gh-state-{args.bitbucket_workspace}-{args.bitbucket_repo}.json"
  migrator = Migrator(
    bitbucket_client(args),
    GitHubClient.from_token(args.github_repo, token, args.github_api_url),
    UserMap(mapping, notify=args.notify_users),
    State(None if args.dry_run else state_file),
    Options(
      dry_run=args.dry_run,
      keep_branches=args.keep_branches,
      delay=0 if args.dry_run else args.delay,
      branch_prefix=args.branch_prefix,
      label=args.label,
    ),
  )
  counts = migrator.run(parse_states(args.states), args.pr_ids, args.limit)
  print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
  if not args.dry_run:
    print(f"Bitbucket -> GitHub mapping saved in {state_file}")
  return 1 if counts.get("failed") else 0


def cmd_users(args):
  bb = bitbucket_client(args)
  users = {}

  def add(user):
    if user and user.get("account_id"):
      users[user["account_id"]] = {"github": "", "name": user.get("display_name", ""),
                                   "nickname": user.get("nickname", "")}

  for summary in bb.list_pull_requests(parse_states(args.states)):
    pr = bb.get_pull_request(summary["id"])
    add(pr.get("author"))
    add(pr.get("closed_by"))
    for p in pr.get("participants", []):
      add(p.get("user"))
    for c in bb.list_comments(summary["id"]):
      add(c.get("user"))
  json.dump(dict(sorted(users.items(), key=lambda kv: kv[1]["name"].lower())), sys.stdout, indent=2)
  print()
  return 0


def main(argv=None):
  args = build_parser().parse_args(argv)
  logging.basicConfig(
    level=logging.DEBUG if args.verbose else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
  )
  return cmd_migrate(args) if args.command == "migrate" else cmd_users(args)


if __name__ == "__main__":
  sys.exit(main())
