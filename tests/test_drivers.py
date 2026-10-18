"""End-to-end tests: build a real temp git repo, make two branches that both append
to the same file, merge, and check the result.

Run with pytest:        python -m pytest -q
or without pytest:      python tests/test_drivers.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DRIVER = os.path.join(ROOT, "drivers", "keyed_merge.py")
RESOLVER = os.path.join(ROOT, "drivers", "resolve_append_conflicts.py")
JSON_CLI = os.path.join(ROOT, "drivers", "merge_json_records.py")

KINDS = ["table", "bullet", "csv", "slug", "section", "pointer", "json"]


def _fwd(p):
    return p.replace("\\", "/")


class Repo:
    def __init__(self, drivers=True):
        self.dir = tempfile.mkdtemp(prefix="keyedmerge-")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        if drivers:
            for kind in KINDS:
                name = "keyed" + {"table": "table", "bullet": "bullets", "csv": "csv", "slug": "slugs",
                                  "section": "sections", "pointer": "pointers", "json": "json"}[kind]
                cmd = '"%s" "%s" --kind=%s %%O %%A %%B %%L %%P' % (_fwd(sys.executable), _fwd(DRIVER), kind)
                self.git("config", "merge.%s.driver" % name, cmd)

    def git(self, *args, check=True):
        r = subprocess.run(["git", "-C", self.dir] + list(args), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError("git %s failed: %s %s" % (" ".join(args), r.stdout, r.stderr))
        return r

    def write(self, rel, text):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def read(self, rel):
        with open(os.path.join(self.dir, rel), encoding="utf-8", newline="") as fh:
            return fh.read()

    def commit(self, msg, date=None):
        self.git("add", "-A")
        env = dict(os.environ)
        if date:
            env["GIT_COMMITTER_DATE"] = date
            env["GIT_AUTHOR_DATE"] = date
        r = subprocess.run(["git", "-C", self.dir, "commit", "-q", "-m", msg], capture_output=True,
                           text=True, env=env)
        assert r.returncode == 0, r.stderr

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def scenario(path, base, ours, theirs, attr=None, drivers=True, extra_files=None):
    """base on main; `theirs` on branch `side`; `ours` on main; merge side into main.
    Returns (repo, merge returncode)."""
    repo = Repo(drivers=drivers)
    if attr:
        repo.write(".gitattributes", "%s merge=%s\n" % (path, attr))
    for rel, text in (extra_files or {}).items():
        repo.write(rel, text)
    repo.write(path, base)
    repo.commit("base", "2026-01-01T10:00:00+00:00")
    repo.git("checkout", "-q", "-b", "side")
    repo.write(path, theirs)
    repo.commit("theirs", "2026-01-02T10:00:00+00:00")
    repo.git("checkout", "-q", "main")
    repo.write(path, ours)
    repo.commit("ours", "2026-01-03T10:00:00+00:00")
    r = repo.git("merge", "--no-edit", "side", check=False)
    return repo, r.returncode


def has_markers(text):
    return "<<<<<<<" in text or ">>>>>>>" in text


# ---------------------------------------------------------------- driver probe

def test_supports_probe():
    for kind in KINDS:
        assert subprocess.run([sys.executable, DRIVER, "--supports=" + kind]).returncode == 0
    assert subprocess.run([sys.executable, DRIVER, "--supports=nope"],
                          capture_output=True).returncode == 2


def test_bad_option_is_rejected():
    r = subprocess.run([sys.executable, DRIVER, "--kind=table", "--typo=1", "a", "b", "c"],
                       capture_output=True)
    assert r.returncode == 2


# ---------------------------------------------------------------- table

TABLE_BASE = "# Tools\n\n| Tool | What |\n|---|---|\n| lint | checks style |\n| fmt | formats code |\n"


def test_table_both_append_keeps_both():
    ours = TABLE_BASE + "| build | builds the site |\n"
    theirs = TABLE_BASE + "| deploy | ships the site |\n"
    repo, rc = scenario("tools.md", TABLE_BASE, ours, theirs, "keyedtable")
    try:
        out = repo.read("tools.md")
        assert rc == 0, out
        assert not has_markers(out)
        assert out == TABLE_BASE + "| build | builds the site |\n| deploy | ships the site |\n"
    finally:
        repo.cleanup()


def test_table_edit_plus_append_does_not_duplicate():
    """The case `merge=union` gets wrong: one side edits a row and appends, the
    other appends. The edited row must appear once, edited."""
    ours = TABLE_BASE.replace("checks style", "checks style and types") + "| build | builds |\n"
    theirs = TABLE_BASE + "| deploy | ships |\n"
    repo, rc = scenario("tools.md", TABLE_BASE, ours, theirs, "keyedtable")
    try:
        out = repo.read("tools.md")
        assert rc == 0, out
        assert out.count("| lint |") == 1
        assert "checks style and types" in out
        assert "| build |" in out and "| deploy |" in out
    finally:
        repo.cleanup()


def test_table_declines_same_row_edited_both_sides():
    ours = TABLE_BASE.replace("checks style", "checks style (strict)") + "| build | builds |\n"
    theirs = TABLE_BASE.replace("checks style", "checks style (loose)") + "| deploy | ships |\n"
    repo, rc = scenario("tools.md", TABLE_BASE, ours, theirs, "keyedtable")
    try:
        assert rc != 0
        assert has_markers(repo.read("tools.md"))
    finally:
        repo.cleanup()


def test_table_declines_row_added_both_sides_with_same_key():
    ours = TABLE_BASE + "| build | builds the site |\n"
    theirs = TABLE_BASE + "| build | builds the docs |\n"
    repo, rc = scenario("tools.md", TABLE_BASE, ours, theirs, "keyedtable")
    try:
        assert rc != 0
        assert has_markers(repo.read("tools.md"))
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- bullet

LOG_BASE = "# Decisions\n\n- **2026-01-01** - Use Postgres.\n- **2025-12-20** - Ship weekly.\n\n## Notes\n"


def test_bullet_new_entries_go_to_head():
    ours = LOG_BASE.replace("# Decisions\n\n", "# Decisions\n\n- **2026-01-03** - Add a cache.\n")
    theirs = LOG_BASE.replace("# Decisions\n\n", "# Decisions\n\n- **2026-01-02b** - Drop the cron job.\n")
    repo, rc = scenario("decisions.md", LOG_BASE, ours, theirs, "keyedbullets")
    try:
        out = repo.read("decisions.md")
        assert rc == 0, out
        lines = out.splitlines()
        assert lines[2] == "- **2026-01-03** - Add a cache."
        assert lines[3] == "- **2026-01-02b** - Drop the cron job."
        assert lines[4] == "- **2026-01-01** - Use Postgres."
    finally:
        repo.cleanup()


def test_bullet_ignores_prose_bullets():
    """A plain prose bullet is skeleton, not a row: two different prose edits decline."""
    base = "# Notes\n\n- first point\n"
    ours = "# Notes\n\n- first point, extended\n"
    theirs = "# Notes\n\n- first point, reworded\n"
    repo, rc = scenario("notes.md", base, ours, theirs, "keyedbullets")
    try:
        assert rc != 0
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- csv

CSV_BASE = "id,name,status,note\n1,alpha,open,\n2,beta,open,\n"


def test_csv_field_level_merge_on_same_row():
    ours = "id,name,status,note\n1,alpha,closed,\n2,beta,open,\n3,gamma,open,\n"
    theirs = 'id,name,status,note\n1,alpha,open,"called, left a message"\n2,beta,open,\n4,delta,open,\n'
    repo, rc = scenario("rows.csv", CSV_BASE, ours, theirs, "keyedcsv")
    try:
        out = repo.read("rows.csv")
        assert rc == 0, out
        assert '1,alpha,closed,"called, left a message"' in out
        assert "3,gamma" in out and "4,delta" in out
    finally:
        repo.cleanup()


def test_csv_no_final_newline_keeps_appended_rows_apart():
    """A file whose last row has no final newline, with a row appended on each side.
    The appended rows must each get their own line, not be glued onto one."""
    base = "id,name,status,note\n1,alpha,open,\n2,beta,open,"
    ours = base + "\n3,gamma,open,"
    theirs = base + "\n4,delta,open,"
    repo, rc = scenario("rows.csv", base, ours, theirs, "keyedcsv")
    try:
        out = repo.read("rows.csv")
        assert rc == 0, out
        assert "3,gamma,open," in out.splitlines() and "4,delta,open," in out.splitlines(), out
    finally:
        repo.cleanup()


def test_csv_declines_same_column_both_sides():
    ours = "id,name,status,note\n1,alpha,closed,\n2,beta,open,\n"
    theirs = "id,name,status,note\n1,alpha,won,\n2,beta,open,\n"
    repo, rc = scenario("rows.csv", CSV_BASE, ours, theirs, "keyedcsv")
    try:
        assert rc != 0
        assert has_markers(repo.read("rows.csv"))
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- slug

SLUG_BASE = "# Pinned\n\nkeep-tests-green\nnever-commit-secrets\n"


def test_slug_comment_edit_wins_once():
    ours = SLUG_BASE.replace("keep-tests-green", "keep-tests-green  # CI blocks merges") + "small-commits\n"
    theirs = SLUG_BASE + "review-before-push\n"
    repo, rc = scenario("pinned.txt", SLUG_BASE, ours, theirs, "keyedslugs")
    try:
        out = repo.read("pinned.txt")
        assert rc == 0, out
        assert out.count("keep-tests-green") == 1
        assert "# CI blocks merges" in out
        assert "small-commits" in out and "review-before-push" in out
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- section

SEC_BASE = ("# Tasks\n\n"
            "### T-1 - Set up CI\n\nRun tests on push.\n\n"
            "### T-2 - Write docs\n\nCover install.\n")


def test_section_retitle_and_new_blocks():
    ours = SEC_BASE.replace("### T-1 - Set up CI", "### T-1 - Set up CI (DONE)") + \
        "\n### T-3 - Add caching\n\nCache deps.\n"
    theirs = SEC_BASE + "\n### T-4 - Add a badge\n\nShow CI status.\n"
    repo, rc = scenario("tasks.md", SEC_BASE, ours, theirs, "keyedsections")
    try:
        out = repo.read("tasks.md")
        assert rc == 0, out
        assert "### T-1 - Set up CI (DONE)" in out
        assert out.count("### T-1") == 1
        assert out.index("### T-3") < out.index("### T-4")
    finally:
        repo.cleanup()


def test_section_declines_same_block_edited_both_sides():
    ours = SEC_BASE.replace("Cover install.", "Cover install and upgrade.")
    theirs = SEC_BASE.replace("Cover install.", "Cover install on Windows.")
    repo, rc = scenario("tasks.md", SEC_BASE, ours, theirs, "keyedsections")
    try:
        assert rc != 0
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- pointer

PTR_BASE = ("# Index\n\n"
            "- [Run tests first](run-tests-first.md) - before any push\n"
            "- [Small commits](small-commits.md) - one change each\n")


def test_pointer_rewrite_and_add():
    ours = PTR_BASE.replace("before any push", "before any push or release")
    theirs = PTR_BASE + "- [Pin versions](pin-versions.md) - exact versions in CI\n"
    repo, rc = scenario("INDEX.md", PTR_BASE, ours, theirs, "keyedpointers")
    try:
        out = repo.read("INDEX.md")
        assert rc == 0, out
        assert out.count("(run-tests-first.md)") == 1
        assert "before any push or release" in out
        assert "(pin-versions.md)" in out
    finally:
        repo.cleanup()


def test_pointer_declines_both_rewrite_same_target():
    ours = PTR_BASE.replace("one change each", "one logical change each")
    theirs = PTR_BASE.replace("one change each", "small and focused")
    repo, rc = scenario("INDEX.md", PTR_BASE, ours, theirs, "keyedpointers")
    try:
        assert rc != 0
        assert has_markers(repo.read("INDEX.md"))
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- json

SPEC = {"paths": {"data/tasks.json": {"collections": {"tasks": "id"},
                                       "recompute": {"count": "count:tasks", "updated": "max"}}}}


def _doc(tasks, updated):
    return json.dumps({"updated": updated, "count": len(tasks), "tasks": tasks}, indent=2) + "\n"


def test_json_records_merge():
    base = _doc([{"id": "a", "done": False}], "2026-01-01")
    ours = _doc([{"id": "a", "done": True}, {"id": "b", "done": False}], "2026-01-03")
    theirs = _doc([{"id": "a", "done": False}, {"id": "c", "done": False}], "2026-01-02")
    repo, rc = scenario("data/tasks.json", base, ours, theirs, "keyedjson",
                        extra_files={".keyed-json.json": json.dumps(SPEC)})
    try:
        out = json.loads(repo.read("data/tasks.json"))
        assert rc == 0
        assert [t["id"] for t in out["tasks"]] == ["a", "b", "c"]
        assert out["tasks"][0]["done"] is True
        assert out["count"] == 3
        assert out["updated"] == "2026-01-03"
    finally:
        repo.cleanup()


def test_json_declines_same_record_changed_both_sides():
    base = _doc([{"id": "a", "owner": "x"}], "2026-01-01")
    ours = _doc([{"id": "a", "owner": "y"}], "2026-01-02")
    theirs = _doc([{"id": "a", "owner": "z"}], "2026-01-02")
    repo, rc = scenario("data/tasks.json", base, ours, theirs, "keyedjson",
                        extra_files={".keyed-json.json": json.dumps(SPEC)})
    try:
        assert rc != 0
    finally:
        repo.cleanup()


def test_json_declines_unlisted_path():
    base = _doc([{"id": "a"}], "2026-01-01")
    ours = _doc([{"id": "a"}, {"id": "b"}], "2026-01-02")
    theirs = _doc([{"id": "a"}, {"id": "c"}], "2026-01-02")
    repo, rc = scenario("other.json", base, ours, theirs, "keyedjson",
                        extra_files={".keyed-json.json": json.dumps(SPEC)})
    try:
        assert rc != 0
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- direct call, no repo

def test_driver_direct_decline_leaves_markers():
    d = tempfile.mkdtemp(prefix="keyeddirect-")
    try:
        paths = {}
        for name, text in (("base", TABLE_BASE),
                           ("ours", TABLE_BASE.replace("formats code", "formats Python")),
                           ("theirs", TABLE_BASE.replace("formats code", "formats Go"))):
            paths[name] = os.path.join(d, name)
            with open(paths[name], "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        r = subprocess.run([sys.executable, DRIVER, "--kind=table", paths["base"], paths["ours"],
                            paths["theirs"], "7", "tools.md"], capture_output=True, cwd=d)
        assert r.returncode != 0
        with open(paths["ours"], encoding="utf-8") as fh:
            assert has_markers(fh.read())
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- post-merge resolver

def run_resolver(repo, *args):
    return subprocess.run([sys.executable, RESOLVER] + list(args), cwd=repo.dir,
                          capture_output=True, text=True)


def test_resolver_keeps_both_new_entries():
    """No driver installed: plain git conflicts, then the resolver keeps both."""
    base = "# Log\n\n- **2026-01-01** - First.\n"
    ours = "# Log\n\n- **2026-01-03** - Ours.\n  - a sub-item\n- **2026-01-01** - First.\n"
    theirs = "# Log\n\n- **2026-01-02** - Theirs.\n- **2026-01-01** - First.\n"
    repo, rc = scenario("log.md", base, ours, theirs, drivers=False)
    try:
        assert rc != 0
        r = run_resolver(repo)
        assert r.returncode == 0, r.stderr
        out = repo.read("log.md")
        assert not has_markers(out)
        assert out == ("# Log\n\n- **2026-01-03** - Ours.\n  - a sub-item\n"
                       "- **2026-01-02** - Theirs.\n- **2026-01-01** - First.\n")
        assert repo.git("diff", "--name-only", "--diff-filter=U").stdout.strip() == ""
    finally:
        repo.cleanup()


def test_resolver_declines_and_changes_nothing_on_edit_edit():
    base = "# Log\n\n- **2026-01-01** - First.\n"
    ours = "# Log\n\n- **2026-01-01** - First, revised by us.\n"
    theirs = "# Log\n\n- **2026-01-01** - First, revised by them.\n"
    repo, rc = scenario("log.md", base, ours, theirs, drivers=False)
    try:
        assert rc != 0
        before = repo.read("log.md")
        r = run_resolver(repo)
        assert r.returncode == 1
        assert repo.read("log.md") == before
    finally:
        repo.cleanup()


def test_resolver_all_or_nothing():
    """One resolvable file and one unresolvable file: nothing is touched."""
    repo = Repo(drivers=False)
    try:
        repo.write("log.md", "# Log\n\n- **2026-01-01** - First.\n")
        repo.write("code.py", "x = 1\n")
        repo.commit("base")
        repo.git("checkout", "-q", "-b", "side")
        repo.write("log.md", "# Log\n\n- **2026-01-02** - Theirs.\n- **2026-01-01** - First.\n")
        repo.write("code.py", "x = 2\n")
        repo.commit("theirs")
        repo.git("checkout", "-q", "main")
        repo.write("log.md", "# Log\n\n- **2026-01-03** - Ours.\n- **2026-01-01** - First.\n")
        repo.write("code.py", "x = 3\n")
        repo.commit("ours")
        assert repo.git("merge", "--no-edit", "side", check=False).returncode != 0
        before = repo.read("log.md")
        r = run_resolver(repo)
        assert r.returncode == 1
        assert repo.read("log.md") == before
    finally:
        repo.cleanup()


TAIL_BASE = "# Notes on caching\n\nUse a short TTL.\n"


def test_resolver_tail_append():
    ours = TAIL_BASE + "\n---\n\n## Update\n\nWarm the cache on deploy.\n"
    theirs = TAIL_BASE + "\n---\n\n## Another update\n\nNever cache auth responses.\n"
    repo, rc = scenario("caching.md", TAIL_BASE, ours, theirs, drivers=False)
    try:
        assert rc != 0
        r = run_resolver(repo)
        assert r.returncode == 0, r.stderr
        out = repo.read("caching.md")
        assert not has_markers(out)
        assert "Warm the cache on deploy." in out and "Never cache auth responses." in out
        assert out.index("Warm the cache") < out.index("Never cache auth")
    finally:
        repo.cleanup()


def test_resolver_tail_append_declines_shared_content():
    """Both sides added the same line, but in different places. Keep-both would
    print it twice, so the resolver must decline."""
    ours = TAIL_BASE + "\nOurs only.\nSame line on both sides.\nMore of ours.\n"
    theirs = TAIL_BASE + "\nSame line on both sides.\nTheirs only.\n"
    repo, rc = scenario("caching.md", TAIL_BASE, ours, theirs, drivers=False)
    try:
        assert rc != 0
        assert run_resolver(repo).returncode == 1
    finally:
        repo.cleanup()


def test_resolver_tail_append_declines_minted_ordinal():
    ours = TAIL_BASE + "\nThe fourth rule: log every cache miss.\n"
    theirs = TAIL_BASE + "\nThe fourth rule: expire on write.\n"
    repo, rc = scenario("caching.md", TAIL_BASE, ours, theirs, drivers=False)
    try:
        assert rc != 0
        assert run_resolver(repo).returncode == 1
    finally:
        repo.cleanup()


# ---------------------------------------------------------------- hand JSON resolution

def test_merge_json_records_cli():
    base = _doc([{"id": "a", "done": False}], "2026-01-01")
    ours = _doc([{"id": "a", "done": False}, {"id": "b", "done": False}], "2026-01-03")
    theirs = _doc([{"id": "a", "done": True}, {"id": "c", "done": False}], "2026-01-02")
    repo, rc = scenario("tasks.json", base, ours, theirs, drivers=False)
    try:
        assert rc != 0
        r = subprocess.run([sys.executable, JSON_CLI, "tasks.json", "--collection", "tasks", "--id", "id",
                            "--recompute", "count=count", "--recompute", "updated=max"],
                           cwd=repo.dir, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        out = json.loads(repo.read("tasks.json"))
        assert [t["id"] for t in out["tasks"]] == ["a", "b", "c"]
        assert out["tasks"][0]["done"] is True
        assert out["count"] == 3 and out["updated"] == "2026-01-03"
    finally:
        repo.cleanup()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL  %s: %s" % (name, exc))
    print("\n%d passed, %d failed" % (len(tests) - failed, failed))
    sys.exit(1 if failed else 0)
