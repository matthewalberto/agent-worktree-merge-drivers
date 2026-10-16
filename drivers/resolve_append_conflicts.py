#!/usr/bin/env python3
"""Post-merge resolver for a few SPECIFIC conflict shapes that are always resolved
the same way by hand.

Run it after `git merge` stops with conflicts. It looks at every unmerged path. If
EVERY one of them matches a shape below exactly, it resolves them all and stages
the results. If even one path doesn't match, it changes NOTHING and exits 1, so you
resolve by hand exactly as you would have anyway.

This is not a general "resolve conflicts smartly" tool. It recognises four shapes
and refuses everything else.

Shape 1: both sides inserted NEW dated entries at the same spot
---------------------------------------------------------------
A log of dated entries (`- **2026-03-14** - title`, with indented sub-items) where
both branches added brand-new entries next to each other. The keyed driver
(`keyed_merge.py --kind=bullet`) declines a whole file when one side ALSO edited an
older entry in place. Git's own merge then resolves the edited region fine, and the
leftover conflict hunk is a pure "both sides inserted" block. Keep both.

It declines when:
  * a hunk line already existed in the merge base (an edit caught in the hunk,
    not an insert; keeping both would duplicate the old entry)
  * the same whole entry appears on both sides
  * the same entry label (plus qualifier) appears on both sides with different
    text AND that label already existed in the base (the same entry edited two
    different ways)
  * any line isn't an entry head or a continuation of one

Entries are written newest-first by label, which suits newest-first logs. Files
listed under "heading_style_files" in the config use `### [2026-03-14] Title`
entries instead, kept in ours-then-theirs order.

Shape 2: a regenerated binary build output, added on both sides
----------------------------------------------------------------
Two branches each generated the same output file (neither had it at the merge
base). For a deterministic pipeline the later build replaces the earlier one, so
the side whose last commit touching that path is LATER wins. Only paths matching
"binary_artifact_globs" in the config are eligible.

Shape 3: a generated file that conflicts alongside its source
-------------------------------------------------------------
A file built from a source file by one command. The only correct content is
whatever the build emits from the MERGED source. If the source itself resolves as
shape 1 (or isn't conflicted), this writes the resolved source, runs the build,
captures the output, then restores both files until everything else has also
classified. A failing build declines. Only pairs listed under "derived_files" in
the config are eligible. (Simpler alternative: mark the generated file
`merge=ours` in .gitattributes and rebuild after every merge.)

Shape 4: both sides appended a disjoint block at the very END of a file
------------------------------------------------------------------------
Free-form notes appended after a `---`, matching no entry style. Nothing is
parsed. Instead it PROVES nothing is lost: collapsing the marked file to each side
must reproduce that side's committed blob exactly. It refuses on: more than one
conflict region, any content after the region, an empty side, a side that is only
separators, any content line present on BOTH sides (keep-both would duplicate
it), and any date label, `vN.N` version or spelled ordinal ("fourth") minted by
both sides (a numbering collision needs a human). Shape 4 is tried only after
shape 1 declines.

Config (optional): `.append-resolver.json` at the repo root, or --config PATH
    {
      "heading_style_files": ["docs/rules.md"],
      "binary_artifact_globs": ["build/previews/*/*.png"],
      "derived_files": {
        "docs/rules-summary.md": {"source": "docs/rules.md",
                                  "build_cmd": ["python", "tools/build_summary.py"]}
      }
    }

Usage (from the repo root, during a conflicted merge)
    python drivers/resolve_append_conflicts.py [--dry-run] [--config PATH]

Exit codes
    0   every unmerged path was resolved and staged (or --dry-run classified them all)
    1   at least one path didn't match a safe shape; nothing was changed
    2   bad invocation, or git not usable
"""

import fnmatch
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

DEFAULT_CONFIG = ".append-resolver.json"

# Same label and entry pattern as keyed_merge.py. Group 1 = label, group 2 = qualifier.
LABEL = r"(?:\d{4}-\d{2}-\d{2}|\d{6})(?:[a-z]\d{0,2})?"
BULLET_RE = re.compile(
    r"^- (?:[^\w\s*\[]{1,4}\s+)?\*\*(" + LABEL + r")"
    r"(?:\s*[-\u2013\u2014/]\s*(?:\d{2,4}(?:-\d{2}){0,2}[a-z]?|[a-z]))?"
    r"(?:\s*\(([^)]*)\))?\s*(?:\*\*|[-\u2013\u2014,:])"
)
HEADING_RE = re.compile(r"^### \[(" + LABEL + r")\]\s+\S")

# A continuation line belongs to the entry above it: indented and non-blank.
CONTINUATION_RE = re.compile(r"^[ \t]+\S")

CONFLICT_START = re.compile(r"^<<<<<<< ")
CONFLICT_MID = re.compile(r"^=======\r?$")
CONFLICT_BASE = re.compile(r"^\|\|\|\|\|\|\| ")  # diff3 style: not handled
CONFLICT_END = re.compile(r"^>>>>>>> ")

ORDINAL_WORDS = (
    "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
    "seventeenth", "eighteenth", "nineteenth", "twentieth",
)
MINTED_LABEL_RES = (
    re.compile(r"^#{2,3}\s+\[?(" + LABEL + r")\]?(?![\w-])"),
    re.compile(r"^-\s+\*\*(" + LABEL + r")(?![\w-])"),
)
VERSION_RE = re.compile(r"\bv(\d+\.\d+)\b")
# A markdown thematic break is a separator, not content.
HRULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")


class Unresolvable(Exception):
    """Raised the moment ANY unmerged path fails to match a safe shape."""


class EntryStyle:
    def __init__(self, label, head_re, owns_continuation, sort_newest_first):
        self.label = label
        self.head_re = head_re
        self.owns_continuation = owns_continuation
        self.sort_newest_first = sort_newest_first


# A blank line also belongs to the entry above it. A blank line with no head above
# it in the same hunk side still declines (the hunk starts mid-entry).
BULLET_STYLE = EntryStyle("dated bullet", BULLET_RE,
                          lambda ln: bool(CONTINUATION_RE.match(ln)) or ln.strip() == "", True)
# Heading entries own every following line up to the next heading.
HEADING_STYLE = EntryStyle("dated heading", HEADING_RE, lambda ln: True, False)


def entry_key(m):
    q = m.group(2) if (m.re.groups or 0) >= 2 else None
    return m.group(1) + ("(" + q.strip().lower() + ")" if q else "")


def load_config(path):
    cfg = {"heading_style_files": [], "binary_artifact_globs": [], "derived_files": {}}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def git(args, check=True):
    r = run(["git"] + args)
    if check and r.returncode != 0:
        raise Unresolvable("git %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def write_bytes_atomic(path, data):
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def unmerged_paths():
    out = git(["diff", "--name-only", "--diff-filter=U"])
    return [p.strip() for p in out.splitlines() if p.strip()]


def stage_of(path):
    stages = set()
    for line in git(["ls-files", "-u", "--", path]).splitlines():
        parts = line.split()
        if len(parts) >= 3:
            stages.add(int(parts[2]))
    return stages


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        raise Unresolvable("%s: not valid UTF-8 text" % path)


def split_conflict_segments(lines):
    """Split into ('text', [..]) and ('conflict', ours, theirs) segments. Returns
    None for anything but the plain <<<<<<< / ======= / >>>>>>> shape."""
    segments = []
    i, n = 0, len(lines)
    while i < n:
        if CONFLICT_START.match(lines[i]):
            i += 1
            ours = []
            while i < n and not CONFLICT_MID.match(lines[i]) and not CONFLICT_BASE.match(lines[i]):
                if CONFLICT_START.match(lines[i]):
                    return None
                ours.append(lines[i])
                i += 1
            if i >= n or CONFLICT_BASE.match(lines[i]):
                return None
            i += 1
            theirs = []
            while i < n and not CONFLICT_END.match(lines[i]):
                if CONFLICT_START.match(lines[i]) or CONFLICT_MID.match(lines[i]):
                    return None
                theirs.append(lines[i])
                i += 1
            if i >= n:
                return None
            i += 1
            segments.append(("conflict", ours, theirs))
        else:
            text = []
            while i < n and not CONFLICT_START.match(lines[i]):
                text.append(lines[i])
                i += 1
            segments.append(("text", text))
    return segments


def base_text(path):
    r = run(["git", "show", ":1:%s" % path])
    if r.returncode != 0:
        raise Unresolvable("%s: no merge-base version (:1:)" % path)
    return r.stdout


def parse_entries(side_lines, path, side_name, style):
    entries = []
    for ln in side_lines:
        if style.head_re.match(ln):
            entries.append([ln])
        elif entries and style.owns_continuation(ln):
            entries[-1].append(ln)
        else:
            raise Unresolvable("%s: a line on the %s side is neither a %s nor a continuation of one"
                               % (path, side_name, style.label))
    return ["".join(e) for e in entries]


def plan_entry_file(path, style):
    """Shape 1. Return the resolved text, or raise Unresolvable."""
    lines = read_text(path)
    if not any(CONFLICT_START.match(ln) for ln in lines):
        raise Unresolvable("%s: reported unmerged but has no conflict markers" % path)
    segments = split_conflict_segments(lines)
    if segments is None:
        raise Unresolvable("%s: conflict markers aren't the plain 3-way shape" % path)
    base = base_text(path)
    base_lines = set(ln.rstrip("\r\n") for ln in base.splitlines())
    base_keys = set()
    for ln in base.splitlines():
        m = style.head_re.match(ln)
        if m:
            base_keys.add(entry_key(m))

    out, resolved_any = [], False
    for seg in segments:
        if seg[0] == "text":
            out.extend(seg[1])
            continue
        _, ours, theirs = seg
        if not ours or not theirs:
            raise Unresolvable("%s: a hunk is empty on one side (not a two-sided insert)" % path)
        ours_entries = parse_entries(ours, path, "ours", style)
        theirs_entries = parse_entries(theirs, path, "theirs", style)
        if set(ours_entries) & set(theirs_entries):
            raise Unresolvable("%s: the same whole entry is on both sides" % path)
        for ln in ours + theirs:
            if ln.strip() == "":
                continue  # a blank line carries no identity
            if ln.rstrip("\r\n") in base_lines:
                raise Unresolvable("%s: a hunk line already existed in the merge base "
                                   "(an in-place edit, not a pure insert)" % path)

        def keys_of(entries, side_name):
            keys = {}
            for e in entries:
                m = style.head_re.match(e)
                k = entry_key(m)
                if k in keys and keys[k] != e:
                    raise Unresolvable("%s: two different entries on the %s side share label %s"
                                       % (path, side_name, k))
                keys[k] = e
            return keys

        ok, tk = keys_of(ours_entries, "ours"), keys_of(theirs_entries, "theirs")
        for k in set(ok) & set(tk):
            if k in base_keys:
                raise Unresolvable("%s: label %s is on both sides with different text and existed "
                                   "in the base (the same entry edited two ways)" % (path, k))
        merged = ours + theirs
        rebuilt = "".join(ours_entries) + "".join(theirs_entries)
        if style.sort_newest_first and rebuilt == "".join(merged):
            # Stable sort, so same-label entries keep their written order.
            out.extend(sorted(ours_entries + theirs_entries,
                              key=lambda e: style.head_re.match(e).group(1), reverse=True))
        else:
            out.extend(merged)
        resolved_any = True
    if not resolved_any:
        raise Unresolvable("%s: no conflict hunks found" % path)
    return "".join(out)


def _norm(lines):
    return [ln.rstrip("\r\n") for ln in lines]


def _minted_keys(seg):
    keys = set()
    for ln in seg:
        body = ln.rstrip("\r\n")
        for rx in MINTED_LABEL_RES:
            m = rx.match(body)
            if m:
                keys.add("label:" + m.group(1).lower())
        for v in VERSION_RE.findall(body):
            keys.add("version:v" + v)
        low = body.lower()
        for w in ORDINAL_WORDS:
            if re.search(r"\b%s\b" % w, low):
                keys.add("ordinal:" + w)
    return keys


def plan_tail_append(path):
    """Shape 4. Return the resolved text, or raise Unresolvable."""
    if stage_of(path) != {1, 2, 3}:
        raise Unresolvable("%s: not a plain content conflict on an existing file" % path)
    segments = split_conflict_segments(read_text(path))
    if segments is None:
        raise Unresolvable("%s: conflict markers aren't the plain shape" % path)
    conflicts = [s for s in segments if s[0] == "conflict"]
    if len(conflicts) != 1:
        raise Unresolvable("%s: %d conflict regions; this shape handles exactly one"
                           % (path, len(conflicts)))
    _, ours, theirs = conflicts[0]
    idx = segments.index(conflicts[0])
    tail = []
    for seg in segments[idx + 1:]:
        tail.extend(seg[1] if seg[0] == "text" else [])
    if any(ln.strip() for ln in tail):
        raise Unresolvable("%s: content follows the conflict, so it isn't at the tail" % path)
    if not any(ln.strip() for ln in ours) or not any(ln.strip() for ln in theirs):
        raise Unresolvable("%s: one side's block is empty" % path)

    # The two-sided collapse proof: the whole licence for keep-both.
    head_blob = run(["git", "show", "HEAD:%s" % path])
    merge_blob = run(["git", "show", "MERGE_HEAD:%s" % path])
    if head_blob.returncode != 0 or merge_blob.returncode != 0:
        raise Unresolvable("%s: couldn't read both sides' blobs" % path)
    prefix = []
    for seg in segments[:idx]:
        prefix.extend(seg[1] if seg[0] == "text" else [])
    if _norm(prefix + ours + tail) != _norm(head_blob.stdout.splitlines()):
        raise Unresolvable("%s: collapsing to OURS doesn't reproduce HEAD's blob" % path)
    if _norm(prefix + theirs + tail) != _norm(merge_blob.stdout.splitlines()):
        raise Unresolvable("%s: collapsing to THEIRS doesn't reproduce MERGE_HEAD's blob" % path)

    def content(seg):
        return set(ln for ln in _norm(seg) if ln.strip() and not HRULE_RE.match(ln))

    ob, tb = content(ours), content(theirs)
    if not ob or not tb:
        raise Unresolvable("%s: one side is nothing but separators" % path)
    shared = ob & tb
    if shared:
        raise Unresolvable("%s: %d content line(s) are on both sides; keep-both would duplicate "
                           "them (e.g. %r)" % (path, len(shared), sorted(shared)[0][:60]))
    collide = _minted_keys(ours) & _minted_keys(theirs)
    if collide:
        raise Unresolvable("%s: both sides mint the same identifier(s) %s" % (path, sorted(collide)))
    return "".join(prefix + ours + theirs + tail)


def plan_binary_artifact(path):
    """Shape 2. Return 'ours' or 'theirs'."""
    stages = stage_of(path)
    if stages != {2, 3}:
        raise Unresolvable("%s: not a plain add/add conflict (stages %s)" % (path, sorted(stages)))
    ours_date = git(["log", "-1", "--format=%cI", "HEAD", "--", path], check=False).strip()
    theirs_date = git(["log", "-1", "--format=%cI", "MERGE_HEAD", "--", path], check=False).strip()
    if not ours_date or not theirs_date:
        raise Unresolvable("%s: couldn't find the last commit date on both sides" % path)
    # Compare as timestamps, so different time zones can't flip the answer.
    from datetime import datetime
    o, t = datetime.fromisoformat(ours_date), datetime.fromisoformat(theirs_date)
    if o == t:
        raise Unresolvable("%s: both sides committed at the same second; no later build" % path)
    return "theirs" if t > o else "ours"


def plan_derived_file(path, all_paths, cfg):
    """Shape 3. Writes the resolved source, runs the build, captures the output,
    then restores both files' original bytes in a finally."""
    spec = cfg["derived_files"][path]
    source, build_cmd = spec["source"], spec["build_cmd"]
    source_conflicted = source in all_paths
    resolved_source = None
    if source_conflicted:
        resolved_source = plan_entry_file(source, style_for(source, cfg))
    with open(path, "rb") as f:
        derived_original = f.read()
    source_original = None
    if source_conflicted:
        with open(source, "rb") as f:
            source_original = f.read()
    try:
        if source_conflicted:
            write_bytes_atomic(source, resolved_source.encode("utf-8"))
        try:
            r = run(build_cmd)
        except OSError as exc:
            raise Unresolvable("%s: couldn't run build command %s: %s" % (path, build_cmd, exc))
        if r.returncode != 0:
            raise Unresolvable("%s: build command exited %d: %s"
                               % (path, r.returncode, (r.stderr or r.stdout).strip()[:200]))
        with open(path, "rb") as f:
            derived_resolved = f.read()
    finally:
        write_bytes_atomic(path, derived_original)
        if source_conflicted:
            write_bytes_atomic(source, source_original)
    return derived_resolved


def style_for(path, cfg):
    return HEADING_STYLE if path in cfg["heading_style_files"] else BULLET_STYLE


def classify_and_plan(paths, cfg):
    plan = {}
    for path in paths:
        if any(fnmatch.fnmatch(path, g) for g in cfg["binary_artifact_globs"]):
            plan[path] = ("binary", plan_binary_artifact(path))
        elif path in cfg["derived_files"]:
            plan[path] = ("derived", plan_derived_file(path, paths, cfg))
        elif path.endswith(".md"):
            try:
                plan[path] = ("entries", plan_entry_file(path, style_for(path, cfg)))
            except Unresolvable as entry_exc:
                try:
                    plan[path] = ("tailappend", plan_tail_append(path))
                except Unresolvable as tail_exc:
                    raise Unresolvable("%s; and not a pure tail append either: %s" % (entry_exc, tail_exc))
        else:
            raise Unresolvable("%s: doesn't match a known safe shape" % path)
    return plan


def apply_plan(plan):
    for path, (kind, value) in plan.items():
        if kind in ("entries", "tailappend"):
            write_bytes_atomic(path, value.encode("utf-8"))
        elif kind == "binary":
            git(["checkout", "--%s" % value, "--", path])
        else:
            write_bytes_atomic(path, value)
        git(["add", "--", path])


DESCRIBE = {
    "entries": "new dated entries on both sides, kept both",
    "tailappend": "disjoint tail append, kept both (collapse proof exact)",
    "binary": "regenerated build output, took the later side",
    "derived": "generated file, rebuilt from the resolved source",
}


def main(argv):
    dry_run = "--dry-run" in argv[1:]
    config_path = DEFAULT_CONFIG
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 >= len(argv):
            sys.stderr.write("--config needs a path\n")
            return 2
        config_path = argv[i + 1]
    try:
        cfg = load_config(config_path)
        paths = unmerged_paths()
    except (Unresolvable, ValueError) as exc:
        sys.stderr.write("resolve_append_conflicts: %s\n" % exc)
        return 2
    if not paths:
        sys.stderr.write("resolve_append_conflicts: no unmerged paths, nothing to do\n")
        return 0
    try:
        plan = classify_and_plan(paths, cfg)
    except Unresolvable as exc:
        sys.stderr.write("resolve_append_conflicts: declining all %d path(s): %s\n" % (len(paths), exc))
        return 1
    for path, (kind, value) in plan.items():
        extra = " (%s)" % value if kind == "binary" else ""
        sys.stderr.write("resolve_append_conflicts: %s -> %s%s\n" % (path, DESCRIBE[kind], extra))
    if dry_run:
        sys.stderr.write("resolve_append_conflicts: --dry-run, nothing changed\n")
        return 0
    apply_plan(plan)
    sys.stderr.write("resolve_append_conflicts: resolved all %d path(s)\n" % len(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
