# bitbucketcloud-to-Github-migration

`bb2gh` copies pull request history from a Bitbucket Cloud repository into GitHub Enterprise (Server, GHE.com, or github.com). It moves every PR, its description, reviewers, approvals, general comments, inline comments and reply threads.

GitHub Enterprise Importer migrates Bitbucket Server and Data Center, not Bitbucket Cloud. This tool fills that gap.

## How it works

Push the code first; `bb2gh` only moves PR history. For each Bitbucket PR, the tool:

1. Looks up the PR's source and destination commits in the GitHub repo.
2. Creates temporary branches (`bitbucket-pr/<id>/head` and `/base`) at those commits and opens a GitHub PR between them. The diff matches what reviewers saw in Bitbucket.
3. Posts each comment in order. Inline comments land on the same file and line as review comments, and replies stay threaded.
4. Labels the PR `migrated-from-bitbucket` and `bitbucket:<state>`, closes merged, declined and superseded PRs, and deletes the temporary branches. GitHub keeps the closed PR's diff.

Open PRs reuse the live source and destination branches when they exist, so reviews can continue on GitHub.

Some PRs can't become GitHub PRs: a declined PR whose branch got deleted, a PR from a fork, or a PR GitHub rejects with "no commits between". The tool recreates those as issues with the full description and comments, and states the reason at the top.

Every migrated item opens with a header that names the original author, dates, branches, commits, reviewers and a link back to Bitbucket. GitHub attributes the post to the token owner, so the header carries the authorship.

## Quick start

```bash
pip install .

# 1. Mirror the code (skip if the GitHub repo already has it)
git clone --mirror https://bitbucket.org/acme/widgets.git
cd widgets.git && git push --mirror https://ghe.example.com/acme/widgets.git && cd ..

# 2. Credentials
export GITHUB_TOKEN=ghp_...                 # repo scope, or fine-grained: contents, pull requests, issues (read/write)
export BITBUCKET_TOKEN=...                  # repository or workspace access token with pullrequest:read
# or: export BITBUCKET_EMAIL=you@acme.com BITBUCKET_API_TOKEN=...   (Atlassian API token)

# 3. Build a user map (optional, recommended)
bb2gh users --bitbucket-workspace acme --bitbucket-repo widgets > usermap.json
# fill in each "github" field

# 4. Preview, then run
bb2gh migrate --bitbucket-workspace acme --bitbucket-repo widgets \
  --github-repo acme/widgets --github-api-url https://ghe.example.com/api/v3 \
  --user-map usermap.json --dry-run

bb2gh migrate --bitbucket-workspace acme --bitbucket-repo widgets \
  --github-repo acme/widgets --github-api-url https://ghe.example.com/api/v3 \
  --user-map usermap.json
```

Try one PR first with `--pr 42`, or a small batch with `--limit 10`.

## Options

| Flag | Purpose |
| --- | --- |
| `--github-api-url` | `https://HOST/api/v3` for GHE Server, `https://api.SUBDOMAIN.ghe.com` for GHE.com. Also reads `GITHUB_API_URL`. |
| `--states` | Comma-separated subset of `OPEN,MERGED,DECLINED,SUPERSEDED`. Default: all. |
| `--pr N` | Migrate only PR N. Repeat the flag for more. |
| `--limit N` | Stop after N PRs. |
| `--user-map FILE` | JSON map from Bitbucket `account_id`, `uuid` or nickname to GitHub login. See `usermap.example.json`. |
| `--notify-users` | Render mapped users as real `@mentions`. Off by default, so a migration won't flood inboxes. |
| `--state-file FILE` | Progress file. Default `bb2gh-state-<workspace>-<repo>.json`. |
| `--dry-run` | Read Bitbucket and GitHub, write nothing, report what would happen. |
| `--keep-branches` | Keep the temporary branches for closed PRs. |
| `--delay SECONDS` | Pause between comment writes. Default 1.0, which stays under GitHub's secondary rate limits. |
| `--label`, `--branch-prefix` | Rename the label or the temporary branch prefix. |

## Resuming and re-running

The state file records every PR and comment the tool has written, saved after each write. Re-run the same command after a crash, a rate-limit stop or a Ctrl-C, and the tool picks up where it stopped without duplicating anything. The same file holds the Bitbucket-to-GitHub number mapping for later link rewriting. Delete an entry to migrate that PR again.

The HTTP layer retries 5xx errors and waits out GitHub rate limits (`Retry-After`, `X-RateLimit-Reset`, secondary-limit 403s).

## Text handling

- Bitbucket `@{account_id}` mentions become the mapped GitHub login, or the person's display name.
- Plain `@name` and `#123` references outside code get a hidden break (`@<!-- -->name`). Without that break, GitHub would ping unrelated users and link the wrong issues.
- Bodies over GitHub's 65,536-character limit get truncated with a marker.

## Limits

- PR and issue numbers won't match Bitbucket's; GitHub shares one counter across both. Use the state file for the mapping.
- Bitbucket inline comments point at the diff when the comment went in. When GitHub rejects that line (the code moved), the comment posts as a regular comment that names the file and line.
- GitHub can't mark a migrated PR as merged through the API. Merged PRs close with the `bitbucket:merged` label and the merge commit in the header.
- The tool skips attachments, Bitbucket tasks, reactions and build statuses.
- Deleted Bitbucket comments stay deleted.

## Development

```bash
pip install -e '.[test]'
python -m pytest
```
