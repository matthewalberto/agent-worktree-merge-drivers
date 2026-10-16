"""Keyed-record merge for JSON files.

Used two ways:
  * by keyed_merge.py --kind=json, as a git merge driver
  * by merge_json_records.py, to resolve a conflict already sitting in the index

A file is merged ONLY if its path is listed in the spec file (default
`.keyed-json.json` at the repo root). Per path, the spec names:

  collections  top-level key -> id field. A list collection holds objects keyed by
               that field. A dict collection is keyed by its own keys (give "").
  recompute    field -> "count[:collection]" (the merged record count) or "max"
               (the larger of both sides, compared as strings: good for ISO dates).
  sortBy       collection -> list of fields its writers sort by, to keep the
               merged list in the same order a writer would produce.

Example spec:

    {
      "paths": {
        "data/tasks.json": {
          "collections": {"tasks": "id"},
          "recompute": {"count": "count:tasks", "updated": "max"}
        }
      }
    }

Per record: changed on one side -> that side; changed identically -> either;
added on one side -> kept; deleted on one side and untouched on the other ->
deleted; changed differently on both sides -> decline. Other top-level fields
merge the same way, whole.

It also declines on: an unlisted path, unparseable JSON, a duplicate id on any
side (keying would silently collapse two records), and a list collection holding
a non-object. Output keeps ours' key order, indent and line ending.
"""

import json
import os

DEFAULT_SPEC_FILE = ".keyed-json.json"


class JsonDecline(Exception):
    pass


def load_spec_file(spec_file=None):
    spec_file = spec_file or DEFAULT_SPEC_FILE
    if not os.path.exists(spec_file):
        raise JsonDecline("no spec file at %s" % spec_file)
    with open(spec_file, encoding="utf-8") as fh:
        return json.load(fh)


def spec_for(path, spec_file=None):
    cfg = load_spec_file(spec_file)
    p = (path or "").replace("\\", "/")
    for k, v in cfg.get("paths", {}).items():
        if p == k or p.endswith("/" + k):
            return v
    raise JsonDecline("path not listed in the spec file: %s" % path)


def _parse(raw, label):
    if raw is None or raw.strip() == b"":
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise JsonDecline("%s is not valid JSON (%s)" % (label, e))


def _records(doc, coll, idf, label):
    c = doc.get(coll)
    if c is None:
        return {}, []
    if isinstance(c, dict):
        return dict(c), list(c)
    if not isinstance(c, list):
        raise JsonDecline("%s.%s is neither a list nor a dict" % (label, coll))
    out, order = {}, []
    for x in c:
        if not isinstance(x, dict) or idf not in x:
            raise JsonDecline("%s.%s holds a record without '%s'" % (label, coll, idf))
        k = x[idf]
        if k in out:
            raise JsonDecline("%s.%s has a duplicate %s %r" % (label, coll, idf, k))
        out[k] = x
        order.append(k)
    return out, order


_MISSING = object()


def _pick(b, o, t):
    if o == t:
        return o
    if o == b:
        return t
    if t == b:
        return o
    raise ValueError


def merge_docs(B, O, T, spec):
    """Merge three parsed documents. Returns (merged_doc, conflicts)."""
    if not isinstance(O, dict) or not isinstance(T, dict):
        raise JsonDecline("top level is not an object")
    B = B if isinstance(B, dict) else {}
    colls = spec.get("collections", {})
    recompute = spec.get("recompute", {})
    merged_colls, conflicts = {}, []
    for coll, idf in colls.items():
        bm, _ = _records(B, coll, idf, "base")
        om, oorder = _records(O, coll, idf, "ours")
        tm, torder = _records(T, coll, idf, "theirs")
        out = {}
        for k in set(bm) | set(om) | set(tm):
            try:
                v = _pick(bm.get(k, _MISSING), om.get(k, _MISSING), tm.get(k, _MISSING))
            except ValueError:
                conflicts.append("%s[%r]" % (coll, k))
                continue
            if v is not _MISSING:
                out[k] = v
        order = [k for k in oorder if k in out]
        seen = set(order)
        order += [k for k in torder if k in out and k not in seen]
        seen = set(order)
        order += [k for k in out if k not in seen]
        is_dict = isinstance(O.get(coll, T.get(coll)), dict)
        vals = {k: out[k] for k in order} if is_dict else [out[k] for k in order]
        sort_by = spec.get("sortBy", {}).get(coll)
        if sort_by and not is_dict:
            vals.sort(key=lambda r: tuple(str(r.get(f) or "") for f in sort_by))
        merged_colls[coll] = vals
    top = {}
    for k in (set(O) | set(T)) - set(colls) - set(recompute):
        try:
            v = _pick(B.get(k, _MISSING), O.get(k, _MISSING), T.get(k, _MISSING))
        except ValueError:
            conflicts.append("<top-level> " + k)
            continue
        top[k] = v
    res = {}
    for k in list(O) + [k for k in T if k not in O]:
        if k in merged_colls:
            res[k] = merged_colls[k]
        elif k in recompute:
            how, field = (recompute[k] + ":").split(":")[:2]
            if how == "count":
                if not merged_colls:
                    raise JsonDecline("count recompute for %s but no collections" % k)
                src = merged_colls.get(field) if field else next(iter(merged_colls.values()))
                res[k] = len(src)
            elif how == "max":
                res[k] = max(str(O.get(k) or ""), str(T.get(k) or ""))
            else:
                raise JsonDecline("unknown recompute %r for %s" % (recompute[k], k))
        elif k in top and top[k] is not _MISSING:
            res[k] = top[k]
    return res, conflicts


def dump_like(res, ours_raw):
    """Serialise with ours' indent and line ending."""
    head = ours_raw[:10].replace(b"\r", b"")
    indent = 1 if head.startswith(b'{\n "') else 2
    text = json.dumps(res, indent=indent, ensure_ascii=False) + "\n"
    if b"\r\n" in ours_raw[:400]:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def merge_bytes(base_raw, ours_raw, theirs_raw, spec):
    """-> merged bytes. Raises JsonDecline when anything changed differently on both sides."""
    B, O, T = _parse(base_raw, "base"), _parse(ours_raw, "ours"), _parse(theirs_raw, "theirs")
    res, conflicts = merge_docs(B, O, T, spec)
    if conflicts:
        raise JsonDecline("changed differently on both sides: %s%s"
                          % (", ".join(conflicts[:8]), " ..." if len(conflicts) > 8 else ""))
    return dump_like(res, ours_raw)
