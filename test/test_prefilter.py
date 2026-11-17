#!/usr/bin/env python3
"""The prefilter: which files fclones is spared from hashing, and why that is safe.

The prefilter withholds a file from fclones only when APFS says it reads the same
data stream as another file of the same size. These cases pin that claim to the
volume the suite runs on (every kind of change must move a file off the shared
stream), check the getattrlistbulk decoding against lstat, and check every case
where a file must still be hashed. The --exclude approximations are checked
against real fclones, since fclones is what applies the patterns.

Run by test/test.sh, as the invoking user.
"""
import json
import mmap
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import apply as engine  # noqa: E402
import prefilter  # noqa: E402

F_PUNCHHOLE = 99
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'PASS' if ok else 'FAIL'}: prefilter: {name}{' (' + detail + ')' if detail and not ok else ''}")
    failed += 0 if ok else 1


def clone(src: Path, dst: Path) -> None:
    subprocess.run(["cp", "-c", str(src), str(dst)], check=True)


def entry(path: Path) -> prefilter.Entry:
    for e in prefilter.list_dir(str(path.parent)):
        if e.name == path.name.encode():
            return e
    raise FileNotFoundError(path)


def same_stream(a: Path, b: Path) -> bool:
    ea, eb = entry(a), entry(b)
    return ea.clone_id is not None and ea.clone_id == eb.clone_id


def test_clone_id_semantics(root: Path) -> None:
    """The claim the prefilter rests on, on this volume: a clone shares its
    source's clone id, and every way of changing either file gives the changed
    one a new id, so an equal id never hides different bytes."""
    d = root / "semantics"
    d.mkdir()
    a, b = d / "a", d / "b"

    def fresh() -> None:
        for f in (a, b):
            if f.exists():
                f.unlink()
        a.write_bytes(os.urandom(3 << 20))
        clone(a, b)

    fresh()
    ea, eb = entry(a), entry(b)
    check("a clone shares its source's clone id", same_stream(a, b))
    check("a clone's refcount counts both files",
          ea.clone_refcnt is not None and ea.clone_refcnt >= 2 and eb.clone_refcnt == ea.clone_refcnt)
    plain = d / "plain"
    shutil.copyfile(a, plain)
    check("a plain copy has its own clone id", not same_stream(a, plain))
    plain.unlink()

    def rewrite_same(p: Path) -> None:
        with open(p, "r+b") as f:
            f.seek(1 << 20)
            block = f.read(4096)
            f.seek(1 << 20)
            f.write(block)

    def rewrite_other(p: Path) -> None:
        with open(p, "r+b") as f:
            f.seek(4096)
            f.write(os.urandom(4096))

    def append(p: Path) -> None:
        with open(p, "ab") as f:
            f.write(b"x")

    def truncate_and_regrow(p: Path) -> None:
        os.truncate(p, 1 << 20)
        with open(p, "ab") as f:
            f.write(os.urandom(2 << 20))

    def extend_with_hole(p: Path) -> None:
        os.truncate(p, (3 << 20) + 4096)

    def mmap_write(p: Path) -> None:
        fd = os.open(p, os.O_RDWR)
        try:
            with mmap.mmap(fd, 0) as m:
                m[5000] = (m[5000] + 1) % 256
                m.flush()
        finally:
            os.close(fd)

    def punch_hole(p: Path) -> None:
        import fcntl
        import struct
        fd = os.open(p, os.O_RDWR)
        try:
            fcntl.fcntl(fd, F_PUNCHHOLE, struct.pack("=IIqq", 0, 0, 1 << 20, 1 << 20))
        finally:
            os.close(fd)

    changes = [("rewriting a block with the same bytes", rewrite_same),
               ("rewriting a block with other bytes", rewrite_other),
               ("appending", append), ("truncating then regrowing", truncate_and_regrow),
               ("extending with a hole", extend_with_hole), ("a write through a mapping", mmap_write),
               ("punching a hole", punch_hole)]
    for label, change in changes:
        for victim, other in ((b, a), (a, b)):
            fresh()
            change(victim)
            check(f"{label} ({'clone' if victim == b else 'source'}) moves it to a new clone id",
                  not same_stream(victim, other))


def test_decoding(root: Path) -> None:
    """getattrlistbulk's records, decoded, say what lstat says."""
    d = root / "decode"
    (d / "sub").mkdir(parents=True)
    (d / "f").write_bytes(b"x" * 12345)
    (d / "empty").write_bytes(b"")
    os.link(d / "f", d / "hard")
    os.symlink("f", d / "link")
    got = {e.name.decode(): e for e in prefilter.list_dir(str(d))}
    check("decode: every entry listed", sorted(got) == ["empty", "f", "hard", "link", "sub"],
          str(sorted(got)))
    st = os.lstat(d / "f")
    f = got["f"]
    check("decode: a file's type, size, links and device",
          (f.objtype, f.size, f.nlink, f.dev) == (prefilter.VREG, 12345, 2, st.st_dev),
          str(f))
    check("decode: an empty file has size 0", got["empty"].size == 0)
    check("decode: a directory", got["sub"].objtype == prefilter.VDIR and not got["sub"].mounted_on)
    check("decode: a symlink is neither a file nor a directory",
          got["link"].objtype not in (prefilter.VREG, prefilter.VDIR))
    many = d / "many"
    many.mkdir()
    for i in range(3000):
        (many / f"file-with-a-longish-name-{i:05d}").write_bytes(b"")
    check("decode: a directory larger than one buffer", len(prefilter.list_dir(str(many))) == 3000)


def walk(scope: Path, min_size: int = 1, excludes: Optional[list[str]] = None
         ) -> tuple[list[str], list[list[str]], set[str]]:
    """What the prefilter offers fclones, its withheld sets, and every file fclones
    reaches from what it was offered (a directory is walked whole)."""
    w = prefilter.Walk(str(scope), min_size, excludes or [])
    offered: list[str] = []
    w.run(offered.append, 2)
    reach: set[str] = set()
    for p in offered:
        if os.path.isdir(p) and not os.path.islink(p):
            for d, _dirs, files in os.walk(p):
                reach.update(os.path.join(d, f) for f in files
                             if not os.path.islink(os.path.join(d, f)))
        else:
            reach.add(p)
    return offered, [files for _size, files in w.sets()], reach


def all_files(scope: Path) -> set[str]:
    return {os.path.join(d, f) for d, _dirs, files in os.walk(scope) for f in files
            if not os.path.islink(os.path.join(d, f))}


def test_walk(root: Path) -> None:
    d = root / "walk"
    (d / "deep" / "er").mkdir(parents=True)
    (d / "other" / "x").mkdir(parents=True)
    body = os.urandom(65536)
    (d / "a").write_bytes(body)
    clone(d / "a", d / "deep" / "er" / "b")
    clone(d / "a", d / "deep" / "c")
    (d / "plain").write_bytes(body)
    (d / "deep" / "er" / "sibling").write_bytes(b"s")
    (d / "other" / "x" / "y").write_bytes(b"y")
    offered, sets, reach = walk(d)
    members = sorted(str(p) for p in (d / "a", d / "deep" / "er" / "b", d / "deep" / "c"))
    check("walk: a clone set is one set", len(sets) == 1 and sorted(sets[0]) == members, str(sets))
    withheld = set(sets[0][1:]) if sets else set()
    check("walk: fclones reaches exactly the files not withheld",
          reach == all_files(d) - withheld, str(sorted(reach ^ (all_files(d) - withheld))))
    check("walk: the set's first file is offered", bool(sets) and sets[0][0] in reach)
    check("walk: the file offered is the set's lowest inode, as fclones orders a group",
          bool(sets) and sets[0][0] == min(members, key=lambda p: os.lstat(p).st_ino))
    check("walk: a subtree holding nothing withheld is offered as one directory",
          str(d / "other") in offered and not any(p.startswith(str(d / "other") + "/") for p in offered),
          str(offered))
    nothing, sets2, reach2 = walk(d / "other")
    check("walk: a scope holding nothing withheld is offered as itself",
          nothing == [str(d / "other")] and not sets2 and reach2 == all_files(d / "other"), str(nothing))

    # Each of these has a clone, and must still be hashed. One real set keeps
    # the scope split, so the cases are offered on their own.
    cases = root / "cases"
    cases.mkdir()
    (cases / "set1").write_bytes(body + b"1")
    clone(cases / "set1", cases / "set2")
    (cases / "small").write_bytes(b"tiny")
    clone(cases / "small", cases / "small2")
    (cases / "hard").write_bytes(body)
    clone(cases / "hard", cases / "hard2")
    os.link(cases / "hard2", cases / "hard3")
    (cases / "ex").mkdir()
    (cases / "keep").write_bytes(os.urandom(4096))
    clone(cases / "keep", cases / "ex" / "keep2")
    odd = cases / "odd"
    (odd / "nl\nname").mkdir(parents=True)
    (odd / "nl\nname" / "n").write_bytes(body + b"n")
    clone(odd / "nl\nname" / "n", odd / "nl\nname" / "n2")
    (odd / "o").write_bytes(body + b"o")
    clone(odd / "o", odd / "o2")
    (cases / "locked").mkdir()
    (cases / "locked" / "l").write_bytes(b"l")
    os.chmod(cases / "locked", 0)
    try:
        offered, sets, reach = walk(cases, min_size=100, excludes=["**/ex/**"])
    finally:
        os.chmod(cases / "locked", 0o755)
    check("walk: the control set is withheld",
          [sorted(s) for s in sets] == [[str(cases / "set1"), str(cases / "set2")]], str(sets))
    check("walk: a clone below --min is hashed", {str(cases / "small"), str(cases / "small2")} <= reach)
    check("walk: a hard-linked clone is hashed",
          {str(cases / n) for n in ("hard", "hard2", "hard3")} <= reach)
    check("walk: a pruned exclude is not offered",
          not any(p.startswith(str(cases / "ex")) for p in offered), str(offered))
    check("walk: a clone whose twin is excluded is hashed", str(cases / "keep") in reach)
    check("walk: a directory holding a name stdin cannot carry goes to fclones whole",
          str(odd) in offered and not any(p.startswith(str(odd) + "/") for p in offered))
    check("walk: an unreadable directory goes to fclones, which reports it",
          str(cases / "locked") in offered)

    # A compressed clone set is hashed: decmpfs keeps the bytes outside the stream.
    comp = root / "compressed"
    comp.mkdir()
    (comp / "src").write_bytes(b"compress me " * 50000)
    subprocess.run(["ditto", "--hfsCompression", str(comp / "src"), str(comp / "z")], check=True)
    clone(comp / "z", comp / "z2")
    compressed = bool(os.lstat(comp / "z").st_flags & prefilter.UF_COMPRESSED)
    offered, sets, reach = walk(comp)
    check("walk: a compressed clone set is hashed",
          not compressed or ({str(comp / "z"), str(comp / "z2")} <= reach and not sets),
          f"compressed={compressed} sets={sets}")


def fclones_kept(scope: Path, paths: list[Path], excludes: list[str]) -> set[str]:
    """Which of paths fclones keeps on --stdin input with these excludes. All the
    files hold the same bytes, so every kept file lands in one group."""
    args = ["fclones", "group", "--stdin", "--no-ignore", "--hidden", "--min", "1",
            "--format", "json"]
    for g in excludes:
        args += ["--exclude", g]
    r = subprocess.run(args, input="".join(f"{p}\n" for p in paths), capture_output=True,
                       text=True, check=True, cwd=scope)
    groups = json.loads(r.stdout)["groups"]
    return {f for g in groups for f in g["files"]}


def test_excludes_against_fclones(root: Path) -> None:
    """_MightExclude must flag every path fclones drops, and _Prune must prune only
    directories whose files fclones drops; both are checked on fclones itself."""
    d = root / "globs"
    rels = ["a/target/x", "a/targetx/y", "a/b/target", "Library/Mobile Documents/z",
            "Library/Mobile DocumentsX/z", "p/foo.photoslibrary/w", "p/foophotoslibrary/w",
            ".Trash/v", "u", "plain/t", "plain/sub/s", "q/[x]/r", "brace/ab/c", "brace/ad/c",
            "deep/one/two/three"]
    paths = [d / r for r in rels]
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"same\n" * 10)
    patterns = ["**/target/**", "**/Library/Mobile Documents/**", "**/*.photoslibrary/**",
                "**/.Trash/**", "plain/*", "**/\\[x\\]/**", "**/brace/a{b,c}/**", "**/one/*/three",
                "*.nothing"]
    kept = fclones_kept(d, paths, patterns)
    check("globs: fclones keeps some and drops some", 0 < len(kept) < len(paths), str(sorted(kept)))
    might = prefilter._MightExclude(patterns)  # pyright: ignore[reportPrivateUsage]
    missed = [str(p) for p in paths if str(p) not in kept and not might.matches(str(p))]
    check("globs: every path fclones drops is flagged (superset)", not missed, str(missed))
    prune = prefilter._Prune(patterns)  # pyright: ignore[reportPrivateUsage]
    wrongly = [str(p) for p in paths if any(prune.matches(str(a)) for a in p.parents)
               and str(p) in kept]
    check("globs: pruning drops nothing fclones keeps (subset)", not wrongly, str(wrongly))
    check("globs: the default directory excludes are pruned",
          all(prune.matches(str(d / r)) for r in ["a/target", "Library/Mobile Documents",
                                                 "p/foo.photoslibrary", ".Trash"]))


def test_min() -> None:
    check("min: a bare number is bytes", prefilter.parse_min("100000") == 100000)
    check("min: 1M is at least fclones' reading", (prefilter.parse_min("1M") or 0) >= 1000000)
    check("min: 1MiB is binary", prefilter.parse_min("1MiB") == 1 << 20)
    check("min: nonsense is refused", prefilter.parse_min("1Q") is None and
          prefilter.parse_min("x") is None)


def test_merge() -> None:
    F = engine.FullPath
    groups = [[F("/c"), F("/r")], [F("/x"), F("/y")]]
    shared = [[F("/r"), F("/r2"), F("/r3")], [F("/s"), F("/s2")]]
    merged = engine.merge_shared(groups, shared)
    check("merge: withheld clones join their file's group",
          merged[0] == [F("/c"), F("/r"), F("/r2"), F("/r3")], str(merged))
    check("merge: an untouched group is kept as it was", merged[1] == [F("/x"), F("/y")])
    check("merge: a set fclones grouped with nothing is a group of its own",
          merged[2] == [F("/s"), F("/s2")] and len(merged) == 3, str(merged))
    check("merge: the report's groups are not modified", groups[0] == [F("/c"), F("/r")])


def main() -> int:
    root = Path(tempfile.mkdtemp(dir="/private/tmp", prefix="apfsdedupe-prefilter."))
    try:
        test_clone_id_semantics(root)
        test_decoding(root)
        test_walk(root)
        test_excludes_against_fclones(root)
        test_min()
        test_merge()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
