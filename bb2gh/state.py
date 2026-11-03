"""JSON state file so interrupted runs resume without duplicating PRs or comments."""

import json
import os


class State:
  def __init__(self, path):
    self.path = path
    self.data = {"version": 1, "prs": {}}
    if path and os.path.exists(path):
      with open(path, encoding="utf-8") as fh:
        self.data = json.load(fh)

  def pr(self, pr_id):
    return self.data["prs"].setdefault(str(pr_id), {"comments": {}, "branches": [], "done": False})

  def save(self):
    if not self.path:
      return
    tmp = f"{self.path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
      json.dump(self.data, fh, indent=2, sort_keys=True)
    os.replace(tmp, self.path)
