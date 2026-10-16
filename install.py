#!/usr/bin/env python3
"""Install the keyed merge drivers into a repo's .git/config. Safe to run often.

    python install.py                 # configure the repo you're standing in
    python install.py --repo PATH     # configure another repo
    python install.py --dry-run       # print what it would set

.gitattributes is versioned but the driver COMMAND lives in .git/config, which
isn't. A fresh clone or a new machine silently loses it, and nothing errors: git
just falls back to its normal text merge and conflicts come back. So run this
from a setup script or a post-checkout hook, not once by hand.

It follows three rules:

  * It sets `merge.<name>.driver` only, never `merge.<name>.name` on its own (that
    combination makes git abort the merge).
  * It writes a REPO-RELATIVE script path when the script lives inside the repo,
    so one config works in every linked worktree.
  * It PROBES the script with `--supports=<kind>` before configuring each driver.
    A .gitattributes line and the script that serves it can land in different
    commits; if the script can't serve a kind yet, that driver stays unset and git
    falls back to a normal merge. It never points git at a command that can't run.

Driver names it configures (use these in .gitattributes):

    keyedtable  keyedbullets  keyedcsv  keyedslugs  keyedsections  keyedpointers  keyedjson
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "drivers", "keyed_merge.py")

DRIVERS = [
    ("keyedtable", "table"),
    ("keyedbullets", "bullet"),
    ("keyedcsv", "csv"),
    ("keyedslugs", "slug"),
    ("keyedsections", "section"),
    ("keyedpointers", "pointer"),
    ("keyedjson", "json"),
]


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--python", default="python", help="interpreter name git should run (default: python)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    top = git(a.repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print("not a git repo: %s" % a.repo)
        return 2
    root = os.path.normpath(top.stdout.strip())

    script = os.path.normpath(SCRIPT)
    try:
        rel = os.path.relpath(script, root)
        inside = not rel.startswith("..") and not os.path.isabs(rel)
    except ValueError:  # different drive on Windows
        inside = False
    shown = rel.replace(os.sep, "/") if inside else script.replace(os.sep, "/")
    if not inside:
        print("note: the script is outside this repo, so an absolute path is used: %s" % shown)

    changed = 0
    for name, kind in DRIVERS:
        probe = subprocess.run([sys.executable, script, "--supports=" + kind], capture_output=True)
        if probe.returncode != 0:
            print("skip %-14s the script doesn't implement --kind=%s" % (name, kind))
            continue
        cmd = '%s "%s" --kind=%s %%O %%A %%B %%L %%P' % (a.python, shown, kind)
        key = "merge.%s.driver" % name
        have = git(root, "config", "--get", key).stdout.strip()
        if have == cmd:
            print("ok   %-14s already set" % name)
            continue
        if a.dry_run:
            print("set  %-14s %s  (dry run)" % (name, cmd))
            continue
        r = git(root, "config", key, cmd)
        if r.returncode != 0:
            print("FAIL %-14s %s" % (name, r.stderr.strip()))
            continue
        print("set  %-14s %s" % (name, cmd))
        changed += 1
    print("%d driver(s) changed" % changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
