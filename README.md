# agent-worktree-merge-drivers

Git merge drivers for repos where several AI coding agents work at once, each in
its own git worktree, and all of them append to the same shared markdown files.

## Why it matters

- **The problem:** running several agents at once is the fastest way to get more done, until every merge stops on a conflict someone has to fix by hand. Git's built-in `union` driver avoids the stop but can silently duplicate an edited row.
- **What this saves:** safe merges happen on their own, and anything ambiguous still stops for a person, so speed never costs correctness.
- **Where it came from:** built so I can run several agents in parallel on my own workspace every day.

If you run agents in parallel, you've seen this. Every agent adds a row to the same
tools table, a line to the same changelog, a pointer to the same notes index.
Each one appends at the end. When you merge the branches back, git sees "both sides
added lines at the same place" and stops with a conflict. Every time. Somebody has
to resolve it by hand, and the answer is almost always "keep both".

These drivers make git do that for you, but only when it's safe.

## Why not `merge=union`?

Git ships a `union` driver that keeps both sides' lines. It's fine for a log that's
never edited. It's wrong for anything else.

Say one agent edits row 3 of a table and adds row 9, and another agent just adds
row 10. Union keeps both sides of the conflicting hunk, so row 3 comes out twice:
once edited, once stale. There's no conflict marker, so nobody notices. In an index
that gets loaded into an agent's context, that's a silent duplicate that grows
every merge.

These drivers merge by **key** instead of by line position. A row edited on one
side wins. New rows from both sides are kept. Anything ambiguous is declined.

## The safety rule: decline, don't guess

When a driver isn't sure, it doesn't guess. It writes the normal git conflict
markers and exits non-zero, exactly as git would have without it. So the worst a
bug in here can do is give you the conflict you'd have had anyway. It can't ship a
quietly wrong merge.

It declines when:

- the same row was edited differently on both sides
- a row was deleted on one side
- both sides added a row with the same key but different content
- both sides changed the surrounding prose differently
- a row sits next to a line the other side changed, so its place is unclear

## What's in here

| File | What it does |
|---|---|
| `drivers/keyed_merge.py` | The merge driver. One script, seven kinds of file. |
| `drivers/keyed_json.py` | Record-by-record merge for JSON files of keyed records. |
| `drivers/resolve_append_conflicts.py` | A post-merge resolver for a few conflict shapes that are always resolved the same way by hand. All or nothing. |
| `drivers/merge_json_records.py` | Resolves an already-conflicted JSON file from the index stages. |
| `install.py` | Configures the drivers in `.git/config`, with a capability probe. |
| `examples/` | Sample `.gitattributes`, JSON spec and resolver config. |
| `tests/test_drivers.py` | End-to-end tests against real temp git repos. |

## Kinds

| Kind | Driver name | A row is | Keyed by | New rows go |
|---|---|---|---|---|
| `table` | `keyedtable` | a markdown table row | first cell | at the end |
| `bullet` | `keyedbullets` | a dated log entry, `- **2026-03-14** - ...` | whole line | at the top |
| `csv` | `keyedcsv` | a CSV line | first field | at the end |
| `slug` | `keyedslugs` | a bare identifier, optional `# comment` | the identifier | at the end |
| `section` | `keyedsections` | a whole `### T-12 - ...` block | the ID in the heading | at the end |
| `pointer` | `keyedpointers` | `- [Title](note.md) - ...` | the link target | at the end |
| `json` | `keyedjson` | a record in a JSON collection | its id field | after ours |

A few details worth knowing:

- **csv** gets one more chance before declining. If both sides edited the same row,
  it merges field by field and only declines if the same column changed both ways.
- **section** keys on the ID, not the heading text. So marking a task `(DONE)` in
  its heading is an edit, not a delete plus an add.
- **pointer** keys on the link target. Rewriting a description is an edit. Two
  sides rewriting the same one is a visible conflict, not a silent duplicate.
- **bullet** keys on the whole line, so an edited entry declines. A reworded log
  entry is a judgement call. The post-merge resolver handles the common case.

Everything that isn't a row (headings, prose, blank lines, the table separator) is
"skeleton". It merges at whole-file level: one side changed it, take that side;
both changed it differently, decline.

The row patterns are configurable. `--entry-re=REGEX` changes what counts as a log
entry, `--id-re=REGEX` changes what counts as a section ID, and `--new-at=head|tail`
changes where new rows go. Group 1 of each regex is the key.

## Install

Copy `drivers/` and `install.py` into your repo (anywhere you like), then:

```sh
python install.py
```

That sets `merge.keyedtable.driver` and friends in `.git/config`. Then mark your
files in `.gitattributes` (see `examples/gitattributes.example`):

```
docs/tools.md       merge=keyedtable
docs/decisions.md   merge=keyedbullets
notes/INDEX.md      merge=keyedpointers
```

For the `json` kind, also add a `.keyed-json.json` at the repo root that says which
files to merge and what their records look like (see
`examples/keyed-json.example.json`). Unlisted JSON files always decline.

Three things about git's driver config that are easy to get wrong:

1. **Set `merge.<name>.driver`, never `merge.<name>.name` on its own.** With the
   attribute set and only `.name` configured, git doesn't fall back. It aborts the
   merge with "custom merge driver lacks command line".
2. **No config at all is safe.** If a clone never ran `install.py`, git ignores the
   attribute and does a normal merge. So committing `.gitattributes` can't break
   anyone.
3. **Keep the script path repo-relative.** Git runs the driver from the root of the
   working tree doing the merge. A relative path works in the main checkout and in
   every worktree. An absolute path into a worktree breaks when that worktree is
   removed.

`.git/config` isn't versioned, so a new clone loses the drivers without any error.
Merges just start conflicting again. Run `install.py` from your setup script, not
once by hand. It's idempotent.

It also probes the script with `--supports=<kind>` before setting each driver. If
your `.gitattributes` names a kind that the committed script doesn't implement
yet, that driver stays unset and git falls back to a normal merge.

## The post-merge resolver

Some conflicts still reach you. `resolve_append_conflicts.py` handles four shapes
that are always resolved the same way:

1. **Both sides added new dated entries at the same spot** in a log, and the
   driver declined because one side also edited an older entry. Git already merged
   the edit. The leftover hunk is pure "both inserted". Keep both, newest first.
2. **A regenerated build output** (an image, a video) added on both sides. The
   later build wins. Only paths you list are eligible.
3. **A generated file** conflicting next to its source. Resolve the source, run the
   build, take its output. Only pairs you list are eligible.
4. **Both sides appended a disjoint block at the end** of a notes file. Nothing is
   parsed. It proves nothing is lost: collapsing the conflict to each side must
   give back that side's committed file exactly.

Run it after a merge stops:

```sh
git merge feature-branch                       # stops with conflicts
python drivers/resolve_append_conflicts.py     # resolves and stages, or changes nothing
git commit
```

It's all or nothing. If even one conflicted file doesn't match a shape exactly, it
touches nothing and exits 1. Use `--dry-run` to see what it would do.

Each shape has its own refusals. For example, shape 1 declines if any line in the
hunk already existed in the merge base (that means an edit got caught in the hunk,
and keeping both would duplicate the old line). Shape 4 declines if both sides
added the same content line, or both sides minted the same label, version number or
"fourth"-style ordinal. Those need a person.

Optional config goes in `.append-resolver.json` (see
`examples/append-resolver.example.json`).

## When not to use a keyed driver

Before you register a file, check how much of it is rows. On a file that's mostly
skeleton, a keyed driver is **stricter** than git's normal merge: git happily merges
two edits to different paragraphs, but the skeleton rule declines whenever both
sides touched prose. Register files where most of the content is rows, and leave
prose documents on the default merge.

For the same reason, the patterns are narrow on purpose. `bullet` only matches
`- **<date>` followed by a real delimiter, so an ordinary bullet list, whose order
might matter, is never reordered.

## Tests

```sh
python -m pip install pytest
python -m pytest -q
```

Or without pytest: `python tests/test_drivers.py`.

Each test builds a throwaway git repo, makes two branches that append to the same
file, merges them with the drivers installed, and checks the result. About half
the tests are decline cases, because the decline path is the part that matters.

Needs Python 3.9+ and git 2.28+. No other dependencies.

## License

MIT. See `LICENSE`.

---

Built by [Matthew Alberto](https://github.com/matthewalberto). Need a version of this for your team or institution? Get in touch on [LinkedIn](https://www.linkedin.com/in/matthewalberto) or at [matthewalberto.com](https://matthewalberto.com).
