#!/usr/bin/env python3
"""Git merge driver: three-way KEYED merge for append-mostly text files.

The problem
-----------
Several agents (or people) work in parallel git worktrees. Each one appends a row
to the same table, log or index, at the same place: the end. A normal 3-way merge
sees "both sides added lines at the same spot" and stops with a CONFLICT.

Git's built-in `merge=union` driver fixes the pure-append case, but it's wrong for
anything that also gets EDITED. Union keeps both sides' lines for a conflicting
hunk, so a row that one side edited and the other carried along unchanged comes
out TWICE, once edited and once stale, with no marker. Nobody is told.

This driver merges by ROW KEY instead of by line position:

  * a row changed on one side only        -> that side wins
  * a row changed identically on both     -> keep it
  * new rows on either side               -> keep both (ours first, then theirs)
  * anything ambiguous                    -> DECLINE

Declining means: run `git merge-file` to write the ordinary conflict markers and
exit non-zero, exactly as git would have without this driver. A bug here can't
ship a silent bad merge. The worst it can do is fall back to normal behaviour.

Kinds (`--kind=...`)
--------------------
  table    A markdown table row (`| a | b |`), keyed by its first cell.
           New rows go at the TAIL.
  bullet   A dated log entry (`- **2026-03-14** - ...`), keyed by the whole line.
           New entries go at the HEAD by default, because logs like this are
           usually newest-first. Use --new-at=tail for oldest-first logs.
  csv      Any non-blank line, keyed by its first CSV field. A row edited on both
           sides gets a FIELD-level 3-way merge: it only declines if the same
           column changed differently on both sides. Rows must be one physical
           line each (no raw newlines inside quoted cells).
  slug     A bare identifier line (`some-name`, optionally `# comment`), keyed by
           the identifier. Good for allowlists and pin lists.
  section  A whole `### <ID> ...` block (heading plus body), keyed by the ID in its
           heading. Good for task lists and rule books where each entry is a block.
  pointer  An index line (`- [Title](target.md) - description`), keyed by the link
           target. Good for an index of notes whose descriptions get rewritten.
  json     A JSON file of keyed records. See keyed_json.py.

Lines that aren't rows for the chosen kind (prose, headings, the table separator,
blank lines) are "skeleton". Skeleton merges 3-way at whole-file level: one side
changed it -> take that side; both changed it identically -> take it; both changed
it differently -> DECLINE.

Configuring the row patterns
----------------------------
  --entry-re=REGEX   bullet kind: what counts as a log entry head. Group 1 is the
                     entry's label. Default: ENTRY_RE below.
  --id-re=REGEX      section kind: what counts as an ID'd heading. Group 1 (or the
                     first non-empty group) is the ID. Default: SECTION_ID_RE below.
  --new-at=head|tail where new rows go (default: head for bullet, tail otherwise).
  --json-config=PATH json kind: the record spec file (default .keyed-json.json at
                     the repo root, where git runs the driver).

Install
-------
    python install.py            # from the repo you want to configure

or by hand:

    git config merge.keyedtable.driver 'python drivers/keyed_merge.py --kind=table %O %A %B %L %P'

and mark files in .gitattributes:

    docs/tools.md merge=keyedtable

Three facts about git's driver config, all checked against real git:

  1. Set `merge.<name>.driver`. Never set `merge.<name>.name` without it: with the
     attribute present and `.name` set but `.driver` missing, git doesn't fall back.
     It dies with "custom merge driver lacks command line" and aborts the merge.
  2. With NO `merge.<name>.*` config at all, git falls back to its normal text merge.
     So a committed .gitattributes line can't break a clone that never installed it.
  3. Keep the script path REPO-RELATIVE. Git runs the driver from the root of the
     working tree doing the merge, so one relative path works in the main checkout
     and in every linked worktree. An absolute path into a worktree dangles as soon
     as that worktree is removed.

Usage (git calls this; %A is both an input and the output file)
    keyed_merge.py [--kind=K] [options] <base %O> <ours %A> <theirs %B> [marker-size %L] [path %P]
    keyed_merge.py --supports=<kind>    # exit 0 if this script implements <kind>, else 2

Exit codes
    0   merged cleanly, result written to %A
    1+  declined, and a real conflict remains: %A holds conflict markers. The value
        is `git merge-file`'s conflict count, passed through.
    2   bad invocation
"""

import csv
import difflib
import io
import os
import re
import subprocess
import sys

try:  # some consoles are not UTF-8; never crash on a stray byte in a message
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

KINDS = ("table", "bullet", "csv", "slug", "section", "pointer", "json")

# A table separator row: | --- | :---: | ---: |   (never a data row)
SEP_RE = re.compile(r"^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$")

# A date label: ISO `2026-03-14` or compact `YYMMDD`, with an optional session
# suffix (`2026-03-14b`, `2026-03-14b2`) so two entries from one day key apart.
LABEL = r"(?:\d{4}-\d{2}-\d{2}|\d{6})(?:[a-z]\d{0,2})?"

# A dated log entry head. Deliberately NARROW: only `- **<date>` followed by a real
# delimiter counts. Treating every `- ` bullet as a row would let this driver
# reorder ordinary prose lists, whose order can carry meaning.
#   - an optional emoji/symbol before the bold date
#   - an optional date range (`2026-03-14/15`, `2026-03-14 - 2026-03-16`)
#   - an optional (qualifier)
#   - then the closing `**` or a dash, comma or colon introducing the title
ENTRY_RE = re.compile(
    r"^- (?:[^\w\s*\[]{1,4}\s+)?\*\*(" + LABEL + r")"
    r"(?:\s*[-\u2013\u2014/]\s*(?:\d{2,4}(?:-\d{2}){0,2}[a-z]?|[a-z]))?"
    r"(?:\s*\(([^)]*)\))?\s*(?:\*\*|[-\u2013\u2014,:])"
)

# A bare identifier line, optionally followed by `# comment`. Lowercase only, so a
# prose sentence fails at its first space, '.' or ')'.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:[ \t]+#.*)?[ \t]*$")

# An index pointer line: `- [Title](some-note.md) - ...`. Keyed by the target.
# The target must end `.md`, so ordinary prose bullets stay skeleton.
POINTER_RE = re.compile(r"^- \[[^\]]+\]\(([A-Za-z0-9][A-Za-z0-9_/.-]*\.md)\)")

# An ID'd h3 heading: `### [2026-03-14] Some rule` or `### T-123 - Some task`.
# The key is the stable ID, not the heading text, so a heading rewritten in place
# (a task marked DONE, a rule struck through) is an edit, not a delete plus add.
# The optional `~~` accepts a struck-through retired entry.
SECTION_ID_RE = re.compile(
    r"^###\s+(?:~~)?(?:\[(" + LABEL + r")\]|([A-Z][A-Z0-9]{0,9}-?\d{1,6}))(?=[\s:.\u2013\u2014-]|$)"
)

# Only an h1/h2 heading closes an open section. An un-ID'd h3 inside a block (a
# sub-heading in a task body) stays part of that block.
SECTION_END_RE = re.compile(r"^#{1,2}[ \t]")


class Decline(Exception):
    """Raised on any ambiguity. The caller falls back to a normal conflicted merge."""


class Config:
    def __init__(self, kind="table", entry_re=None, id_re=None, new_at=None, json_config=None):
        self.kind = kind
        self.entry_re = re.compile(entry_re) if entry_re else ENTRY_RE
        self.id_re = re.compile(id_re) if id_re else SECTION_ID_RE
        self.new_at = new_at or ("head" if kind == "bullet" else "tail")
        self.json_config = json_config


def _split_line_ending(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _first_group(m):
    for g in m.groups():
        if g:
            return g
    return m.group(0)


def is_row(line, cfg):
    kind = cfg.kind
    if kind == "pointer":
        return bool(POINTER_RE.match(_split_line_ending(line)[0]))
    if kind == "section":
        # `line` is a FOLDED block here (see fold_sections); its first line is the heading.
        return bool(cfg.id_re.match(line))
    if kind == "bullet":
        return bool(cfg.entry_re.match(line))
    if kind == "csv":
        return line.strip() != ""
    if kind == "slug":
        # Match the body, not the stripped line: an indented line is prose continuation.
        return bool(SLUG_RE.match(_split_line_ending(line)[0]))
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|") and len(s) > 1):
        return False
    return not SEP_RE.match(line)


def _csv_fields(line):
    """Parse one physical CSV line, or None if it isn't a self-contained record.
    An odd quote count means a quoted cell continues onto the next line, which this
    kind doesn't support, so the caller declines."""
    body, _ = _split_line_ending(line)
    if body.count('"') % 2 != 0:
        return None
    try:
        rows = list(csv.reader([body]))
    except csv.Error:
        return None
    if len(rows) != 1:
        return None
    return rows[0]


def key_of(line, cfg):
    kind = cfg.kind
    if kind == "pointer":
        m = POINTER_RE.match(_split_line_ending(line)[0])
        if not m:
            raise Decline("pointer row has no .md link target: %s" % line.strip()[:60])
        return m.group(1).lower()
    if kind == "section":
        m = cfg.id_re.match(line)
        if not m:
            raise Decline("section has no ID in its heading: %s" % line.split("\n", 1)[0][:60])
        return _first_group(m).lower()
    if kind == "bullet":
        # The whole line is the key. An edited entry reads as delete plus add and
        # declines. That's on purpose: a reworded log entry is a judgement call.
        return line.strip()
    if kind == "slug":
        parts = _split_line_ending(line)[0].split()
        if not parts:
            raise Decline("slug row is blank")
        return parts[0]
    if kind == "csv":
        fields = _csv_fields(line)
        if fields is None:
            raise Decline("csv row isn't a one-line record: %s" % line.strip()[:60])
        if not fields or not fields[0].strip():
            raise Decline("csv row has an empty first field: %s" % line.strip()[:60])
        return fields[0].strip()
    cells = line.strip().split("|")
    if len(cells) < 3:
        raise Decline("row has no parseable first cell: %s" % line.strip()[:60])
    key = cells[1].strip()
    if not key:
        raise Decline("row has an empty first cell: %s" % line.strip()[:60])
    return key


def merge_csv_row(base_line, ours_line, theirs_line):
    """Field-level 3-way merge of one CSV row edited on both sides.
    Returns the merged line, or None if any single column changed differently on
    both sides, or the column count differs."""
    b, o, t = _csv_fields(base_line), _csv_fields(ours_line), _csv_fields(theirs_line)
    if b is None or o is None or t is None:
        return None
    if not (len(b) == len(o) == len(t)):
        return None
    merged = []
    for bc, oc, tc in zip(b, o, t):
        if oc == tc:
            merged.append(oc)
        elif oc == bc:
            merged.append(tc)
        elif tc == bc:
            merged.append(oc)
        else:
            return None
    ending = _split_line_ending(ours_line)[1] or _split_line_ending(theirs_line)[1]
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="").writerow(merged)
    return buf.getvalue() + ending


def fold_sections(lines, cfg):
    """Join each ID'd `### ` block into ONE string, so the rest of the driver can
    keep treating a row as a single list element. `"".join(...)` reassembles the
    file byte for byte. A block runs from its ID'd heading to the next ID'd
    heading, the next h1/h2, or end of file."""
    out, buf = [], None
    for ln in lines:
        body = _split_line_ending(ln)[0]
        starts = bool(cfg.id_re.match(body))
        if starts or (buf is not None and SECTION_END_RE.match(body)):
            if buf is not None:
                out.append("".join(buf))
                buf = None
            if starts:
                buf = [ln]
                continue
        if buf is not None:
            buf.append(ln)
        else:
            out.append(ln)
    if buf is not None:
        out.append("".join(buf))
    return out


def parse(lines, cfg):
    """Split into (skeleton, groups). groups[i] = rows sitting BEFORE skeleton[i].

    Section IDs need not be unique (a dated rule book can hold several rules from
    one day), so for that kind the Nth occurrence of an ID gets key `id#N`. All
    three sides share the same history in the same order, so the Nth one means the
    same entry everywhere. A deletion shifts later ordinals, which declines, as any
    deletion does."""
    skeleton, groups, seen = [], {}, {}
    for ln in lines:
        if is_row(ln, cfg):
            k = key_of(ln, cfg)
            if cfg.kind == "section":
                seen[k] = seen.get(k, 0) + 1
                k = "%s#%d" % (k, seen[k])
            groups.setdefault(len(skeleton), []).append((k, ln))
        else:
            skeleton.append(ln)
    return skeleton, groups


def merge_group(base, ours, theirs, cfg):
    """Three-way merge one contiguous block of keyed rows."""
    for name, rows in (("base", base), ("ours", ours), ("theirs", theirs)):
        keys = [k for k, _ in rows]
        if len(set(keys)) != len(keys):
            raise Decline("duplicate row key in %s" % name)
    b, o, t = dict(base), dict(ours), dict(theirs)
    out = []
    for k, bl in base:
        if k not in o or k not in t:
            raise Decline("row deleted on one side: %s" % k)
        ol, tl = o[k], t[k]
        if ol == tl:
            out.append(ol)
        elif ol == bl:
            out.append(tl)
        elif tl == bl:
            out.append(ol)
        elif cfg.kind == "csv":
            merged = merge_csv_row(bl, ol, tl)
            if merged is None:
                raise Decline("row edited differently on both sides (same column): %s" % k)
            out.append(merged)
        else:
            raise Decline("row edited differently on both sides: %s" % k)
    added = []
    for k, ol in ours:
        if k in b:
            continue
        if k in t and t[k] != ol:
            raise Decline("row added on both sides with different content: %s" % k)
        added.append(ol)
    for k, tl in theirs:
        if k in b or k in o:
            continue
        added.append(tl)
    return (added + out) if cfg.new_at == "head" else (out + added)


def rebuild(skeleton, groups):
    out = []
    for i, sl in enumerate(skeleton):
        out.extend(groups.get(i, []))
        out.append(sl)
    out.extend(groups.get(len(skeleton), []))
    return out


def merge_skeleton(base_sk, ours_sk, theirs_sk):
    """Three-way merge of the non-row lines, at whole-skeleton level."""
    if ours_sk == theirs_sk:
        return ours_sk
    if base_sk == ours_sk:
        return theirs_sk
    if base_sk == theirs_sk:
        return ours_sk
    raise Decline("non-row lines changed differently on both sides")


def anchor_map(src_sk, dst_sk):
    """Map row-anchor indexes in src_sk's index space onto dst_sk's. An anchor `i`
    means "these rows sit right BEFORE skeleton line i" (i == len means after the
    last line). Returns None for the identity mapping."""
    if src_sk == dst_sk:
        return None
    m = {}
    sm = difflib.SequenceMatcher(None, src_sk, dst_sk, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                m[i1 + k] = j1 + k
    # The tail anchor survives only if both skeletons END on the same line; if dst
    # appended prose after it, tail rows would silently jump past that prose.
    # Exception: if everything dst appended is BLANK lines, keep the rows right after
    # the last shared line. A blank line labels nothing, so nothing gets misfiled.
    if src_sk and dst_sk:
        j_last = m.get(len(src_sk) - 1)
        if j_last is not None:
            if j_last == len(dst_sk) - 1:
                m[len(src_sk)] = len(dst_sk)
            elif all(not dst_sk[j].strip() for j in range(j_last + 1, len(dst_sk))):
                m[len(src_sk)] = j_last + 1
    return m


def remap_groups(groups, m, label):
    if m is None:
        return dict(groups)
    out = {}
    for i, rows in groups.items():
        if not rows:
            continue
        if i not in m:
            raise Decline("rows in %s are anchored to a line the other side changed" % label)
        j = m[i]
        if j in out:
            raise Decline("two row groups in %s collapsed onto one anchor" % label)
        out[j] = rows
    return out


def read_lines(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8").splitlines(keepends=True)


def keyed_merge(base_p, ours_p, theirs_p, cfg):
    prep = (lambda x: fold_sections(x, cfg)) if cfg.kind == "section" else (lambda x: x)
    base_sk, base_g = parse(prep(read_lines(base_p)), cfg)
    ours_sk, ours_g = parse(prep(read_lines(ours_p)), cfg)
    theirs_sk, theirs_g = parse(prep(read_lines(theirs_p)), cfg)
    merged_sk = merge_skeleton(base_sk, ours_sk, theirs_sk)
    base_g = remap_groups(base_g, anchor_map(base_sk, merged_sk), "base")
    ours_g = remap_groups(ours_g, anchor_map(ours_sk, merged_sk), "ours")
    theirs_g = remap_groups(theirs_g, anchor_map(theirs_sk, merged_sk), "theirs")
    merged = {}
    for i in set(base_g) | set(ours_g) | set(theirs_g):
        merged[i] = merge_group(base_g.get(i, []), ours_g.get(i, []), theirs_g.get(i, []), cfg)
    return rebuild(merged_sk, merged)


def fallback(base_p, ours_p, theirs_p, marker_size, path):
    """Write the ordinary conflicted file into %A, as git would have.

    `git merge-file`'s exit code is the real conflict count, so pass it through.
    The skeleton check above is coarser than a real line-level diff3, so a hunk
    this driver declines often still merges clean here, and that's reported as 0."""
    label = path or "file"
    cmd = ["git", "merge-file"]
    if marker_size:
        cmd.append("--marker-size=%s" % marker_size)
    cmd += ["-L", "%s (ours)" % label, "-L", "%s (base)" % label, "-L", "%s (theirs)" % label,
            ours_p, base_p, theirs_p]
    return subprocess.run(cmd, check=False).returncode


def write_atomic(path, data):
    """Encode first, write a temp file beside the target, then os.replace. A
    failure leaves the target untouched instead of truncated."""
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def parse_args(argv):
    opts = {"kind": "table"}
    args = []
    for a in argv:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k.replace("-", "_")] = v
        else:
            args.append(a)
    return opts, args


def main(argv):
    for a in argv[1:]:
        if a.startswith("--supports="):
            want = a.split("=", 1)[1]
            if want in KINDS:
                return 0
            sys.stderr.write("keyed_merge: does not implement --kind=%s\n" % want)
            return 2

    opts, args = parse_args(argv[1:])
    kind = opts.pop("kind")
    if kind not in KINDS:
        sys.stderr.write("keyed_merge: unknown --kind=%s (want one of %s)\n" % (kind, ", ".join(KINDS)))
        return 2
    unknown = set(opts) - {"entry_re", "id_re", "new_at", "json_config"}
    if unknown:
        sys.stderr.write("keyed_merge: unknown option(s): %s\n" % ", ".join(sorted(unknown)))
        return 2
    if opts.get("new_at") not in (None, "head", "tail"):
        sys.stderr.write("keyed_merge: --new-at must be head or tail\n")
        return 2
    if len(args) < 3:
        sys.stderr.write(__doc__.split("Usage (git calls this")[1].strip() + "\n")
        return 2
    try:
        cfg = Config(kind, **opts)
    except re.error as exc:
        sys.stderr.write("keyed_merge: bad regex: %s\n" % exc)
        return 2

    base_p, ours_p, theirs_p = args[0], args[1], args[2]
    marker_size = args[3] if len(args) > 3 else None
    path = args[4] if len(args) > 4 else None

    try:
        if kind == "json":
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import keyed_json
            spec = keyed_json.spec_for(path, cfg.json_config)
            rb = lambda p: open(p, "rb").read()
            out = keyed_json.merge_bytes(rb(base_p), rb(ours_p), rb(theirs_p), spec)
        else:
            out = "".join(keyed_merge(base_p, ours_p, theirs_p, cfg)).encode("utf-8")
    except Decline as why:
        sys.stderr.write("keyed_merge: declining (%s) -> normal conflict\n" % why)
        return fallback(base_p, ours_p, theirs_p, marker_size, path)
    except Exception as exc:  # never let a bug here corrupt a merge
        sys.stderr.write("keyed_merge: declining (%s: %s) -> normal conflict\n" % (type(exc).__name__, exc))
        return fallback(base_p, ours_p, theirs_p, marker_size, path)

    write_atomic(ours_p, out)
    sys.stderr.write("keyed_merge: merged %s cleanly (%s)\n" % (path or ours_p, kind))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
