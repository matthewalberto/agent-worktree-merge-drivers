#!/usr/bin/env python3
"""Resolve a merge conflict in a keyed-record JSON file that is ALREADY conflicted.

Use this when no driver was configured at merge time (a fresh clone, a file not
yet registered), or to retry a file with different options. It reads the three
index stages (:1 base, :2 ours, :3 theirs) and merges record by record with the
same engine as the `json` driver kind (keyed_json.py).

    python drivers/merge_json_records.py <path> --collection tasks --id id
    python drivers/merge_json_records.py <path> --collection records \\
        --recompute total=count --recompute updated=max

  --collection KEY   the top-level key holding the records (repeatable)
  --id FIELD         the id field for a list collection; omit for a dict collection.
                     With several --collection flags, give one --id per collection,
                     in the same order ("" for a dict collection).
  --recompute F=HOW  HOW is count, count:<collection> or max

Any record or top-level field changed differently on both sides is listed and
nothing is written (exit 1). Resolve those by hand. On success the file is
written atomically. Then `git add <path>`.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keyed_json  # noqa: E402


def stage(n, path):
    r = subprocess.run(["git", "show", ":%d:%s" % (n, path)], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Merge a conflicted keyed-record JSON file.")
    ap.add_argument("path")
    ap.add_argument("--collection", action="append", required=True)
    ap.add_argument("--id", action="append", default=None)
    ap.add_argument("--recompute", action="append", default=[])
    a = ap.parse_args(argv)

    ids = a.id or [""] * len(a.collection)
    if len(ids) == 1 and len(a.collection) > 1:
        ids = ids * len(a.collection)
    if len(ids) != len(a.collection):
        ap.error("give one --id per --collection")
    spec = {"collections": dict(zip(a.collection, ids)), "recompute": {}}
    for item in a.recompute:
        if "=" not in item:
            ap.error("--recompute takes field=count|count:<collection>|max")
        k, v = item.split("=", 1)
        spec["recompute"][k] = v

    base, ours, theirs = stage(1, a.path), stage(2, a.path), stage(3, a.path)
    if ours is None or theirs is None:
        print("not a both-sides conflict in the index: %s" % a.path)
        return 2
    try:
        B = keyed_json._parse(base, "base")
        O = keyed_json._parse(ours, "ours")
        T = keyed_json._parse(theirs, "theirs")
        res, conflicts = keyed_json.merge_docs(B, O, T, spec)
    except keyed_json.JsonDecline as why:
        print("cannot merge: %s" % why)
        return 1
    if conflicts:
        print("CONFLICTS (both sides changed these differently; resolve by hand):")
        for c in conflicts[:30]:
            print("  " + c)
        print("total %d" % len(conflicts))
        return 1

    data = keyed_json.dump_like(res, ours)
    tmp = a.path + ".tmp%d" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, a.path)
    print("merged %s. Now run: git add %s" % (a.path, a.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
