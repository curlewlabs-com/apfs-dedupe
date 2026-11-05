#!/usr/bin/env python3
"""The threaded inspection and the run's progress output.

Threads see files earlier than the apply acts on them, so what these cases guard
is that this never changes an outcome: the same answers as a serial run, and a
fresh look before anything is modified. Files are real because extent maps and
hard links are what is being read.

Run by test/test.sh, as the invoking user.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply as engine  # noqa: E402
from test_concurrent_writers import MAP_WRITABLE, Writer  # noqa: E402

BODY = b"same bytes\n" * 400
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'PASS' if ok else 'FAIL'}: inspect: {name}{' (' + detail + ')' if detail and not ok else ''}")
    failed += 0 if ok else 1


def full(path: Path) -> "engine.FullPath":
    return engine.FullPath(str(path))


def fingerprint(seen: "engine._Inspected") -> tuple[Optional[int], Optional[int], Any]:
    return (seen.st.st_ino if seen.st else None, seen.error.errno if seen.error else None,
            seen.extent_map)


def run_main(argv: list[str], report: dict[str, Any], workers: int) -> tuple[int, str, str]:
    """engine.main in this process, because the thread count is the thing to pin."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv, sys.stdin, engine._inspect_workers
    sys.argv = ["apply.py", *argv]
    sys.stdin = io.StringIO(json.dumps(report))
    engine._inspect_workers = lambda *_: workers
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = engine.main()
    finally:
        sys.argv, sys.stdin, engine._inspect_workers = saved
    return rc, out.getvalue(), err.getvalue()


def test_worker_count() -> None:
    def sysctls(values: dict[str, int]) -> Any:
        return values.get

    # An Intel Mac has one level, or on older releases no such sysctl. Either way
    # it has no efficiency cores, and the run must still work.
    check("workers: no perflevels means 1", engine._inspect_workers(sysctls({})) == 1)
    check("workers: a single level means 1",
          engine._inspect_workers(sysctls({"hw.nperflevels": 1, "hw.perflevel0.logicalcpu": 8})) == 1)
    check("workers: efficiency cores on a two-level chip",
          engine._inspect_workers(sysctls({"hw.nperflevels": 2, "hw.perflevel0.logicalcpu": 10,
                                           "hw.perflevel1.logicalcpu": 4})) == 4)
    # Guards against reading perflevel1 as "the efficiency level" when a chip has
    # more levels than that.
    check("workers: the slowest level on a three-level chip",
          engine._inspect_workers(sysctls({"hw.nperflevels": 3, "hw.perflevel1.logicalcpu": 6,
                                           "hw.perflevel2.logicalcpu": 2})) == 2)
    check("workers: a missing core count means 1",
          engine._inspect_workers(sysctls({"hw.nperflevels": 2})) == 1)
    check("workers: this Mac gives at least 1", engine._inspect_workers() >= 1)
    check("sysctl: reads a real value", (engine._sysctl_int("hw.ncpu") or 0) > 0)
    check("sysctl: a missing name is None", engine._sysctl_int("hw.no.such.sysctl") is None)


def test_same_answers(root: Path) -> None:
    tree = root / "same"
    tree.mkdir()
    paths: list["engine.FullPath"] = []
    # Enough files to cross several task boundaries, each with its own bytes so
    # that a result delivered to the wrong path shows up as a wrong map.
    for i in range(engine.INSPECT_CHUNK * 5 + 3):
        p = tree / f"f{i}"
        p.write_bytes(f"{i}\n".encode() * 2000)
        paths.append(full(p))
    (tree / "link").symlink_to(tree / "f0")
    paths[7] = full(tree / "link")
    paths[9] = full(tree / "missing")
    serial = [fingerprint(s) for s in engine._inspect_stream(paths, 1)]
    threaded = [fingerprint(s) for s in engine._inspect_stream(paths, 3)]
    check("stream: threads give the same answers in the same order", serial == threaded)
    check("stream: a missing file is reported, not raised", serial[9][1] is not None)
    check("stream: a symlink is not mapped", serial[7][2] is None and serial[7][0] is not None)
    check("stream: empty input yields nothing", list(engine._inspect_stream([], 3)) == [])

    # One group far bigger than a task, between two small ones, because splitting
    # a stream back into groups is where an off-by-one would pair a file with the
    # wrong inspection.
    groups = [paths[:2], paths[2:engine.INSPECT_CHUNK * 3], paths[engine.INSPECT_CHUNK * 3:]]
    got = list(engine._inspect_groups(groups, 3))
    check("groups: each group comes back whole, in order",
          [files for files, _ in got] == groups and all(len(f) == len(i) for f, i in got))
    check("groups: inspections line up with their files",
          [fingerprint(s) for _, i in got for s in i] == serial)


def test_read_ahead_is_bounded() -> None:
    pulled = 0

    def endless() -> Iterator["engine.FullPath"]:
        nonlocal pulled
        while True:
            pulled += 1
            yield engine.FullPath("/nonexistent/apfs-dedupe-test")

    stream = engine._inspect_stream(endless(), 2)
    next(stream)
    limit = 2 * engine.INSPECT_QUEUE_PER_WORKER * engine.INSPECT_CHUNK
    check("read-ahead: stops at the queue bound", pulled == limit, f"{pulled} != {limit}")
    stream.close()


def test_worker_failure_is_not_swallowed() -> None:
    def explode(_: Any) -> Any:
        raise ValueError("bug in a worker")

    real, engine._inspect = engine._inspect, explode
    try:
        list(engine._inspect_stream([engine.FullPath("/x")], 2))
        check("failure: a worker's exception reaches the run", False)
    except ValueError:
        check("failure: a worker's exception reaches the run", True)
    finally:
        engine._inspect = real


def test_threaded_run(root: Path) -> None:
    tree = root / "run"
    tree.mkdir()
    (tree / "a").write_bytes(BODY)
    (tree / "b").write_bytes(BODY)
    (tree / "c").write_bytes(b"other\n" * 900)
    subprocess.run(["cp", "-c", str(tree / "c"), str(tree / "d")], check=True)
    report = {"groups": [{"files": [str(tree / "a"), str(tree / "b")]},
                         {"files": [str(tree / "c"), str(tree / "d")]},
                         {"files": [str(tree / "alone")]}]}
    work = ["--work-dir", str(tree / ".work")]

    plans = [run_main([], report, n) for n in (1, 3)]
    check("dry run: threads plan the same as one", plans[0][1] == plans[1][1], plans[1][1])
    rc, out, err = run_main(["--apply", *work], report, 3)
    check("apply: threaded run succeeds", rc == 0, err)
    check("apply: clones the fresh duplicate only", "across 1 files" in out.split("reclaimed:")[1], out)
    check("apply: counts the existing clone as already saved", "across 1 files" in out.split("already saved")[1], out)
    check("apply: bytes intact", (tree / "b").read_bytes() == BODY)
    check("output: announces the stage, thread count included",
          "checking 4 files in 2 groups on 3 threads" in err, err)
    check("output: one thread reads as one", "on 1 thread..." in plans[0][2], plans[0][2])
    check("output: stdout stays the report a user keeps", "checking" not in out)
    rc, out, _ = run_main(["--apply", *work], report, 3)
    check("re-run: reclaims nothing", rc == 0 and "across 0 files" in out.split("reclaimed:")[1], out)
    _, out, err = run_main(["--apply", *work], {"groups": []}, 3)
    check("output: nothing to do announces nothing", "checking" not in err, err)


class Stale:
    """A duplicate inspected before something else changes it, as a slow file
    ahead of it in the queue would allow."""

    def __init__(self, root: Path, name: str) -> None:
        self.dir = root / name
        self.dir.mkdir()
        self.canonical, self.dup = self.dir / "canonical", self.dir / "dup"
        self.canonical.write_bytes(BODY)
        self.dup.write_bytes(BODY)
        self.paths = [full(self.canonical), full(self.dup)]
        self.inspected = [engine._inspect(p) for p in self.paths]
        self.work_dir = full(self.dir / ".work")
        self.skips = engine._Skips(lambda _: None, False)
        self.swaps = engine._Swaps(engine._open_work_dir(self.work_dir), self.work_dir, False,
                                   self.skips, lambda _: None, lambda _: None)

    def apply(self) -> None:
        engine.dedupe_group(self.paths, self.inspected, self.swaps, self.skips, lambda _: None)
        self.swaps.flush()

    def replace_dup(self) -> None:
        fresh = self.dir / "fresh"
        fresh.write_bytes(BODY)
        fresh.rename(self.dup)


def test_stale_inspections(root: Path) -> None:
    # The inspection says this file has one link. Going by that would break the
    # hard link it has now.
    s = Stale(root, "linked")
    s.replace_dup()
    os.link(s.dup, s.dir / "other-name")
    ino = s.dup.stat().st_ino
    s.apply()
    check("stale: a hard link made since inspection is kept",
          s.dup.stat().st_ino == ino and s.skips.counts.get(engine.SkipKind.HARD_LINKED) == 1)

    s = Stale(root, "gone")
    s.dup.unlink()
    s.apply()
    check("stale: a file deleted since inspection is skipped",
          s.skips.total() == 1 and s.swaps.cloned == 0 and not s.dup.exists())

    # in_use goes by inode and is the only check that sees a writer whose fd is
    # closed, so the inode has to be the file's current one.
    s = Stale(root, "mapped")
    s.replace_dup()
    writer = Writer(MAP_WRITABLE, s.dup)
    s.apply()
    writer.write_and_exit()
    check("stale: a file mapped under a new inode is left alone",
          s.skips.counts.get(engine.SkipKind.IN_USE) == 1 and s.swaps.cloned == 0, str(s.skips.counts))

    s = Stale(root, "plain")
    s.apply()
    check("stale: an unchanged file still clones", s.swaps.cloned == 1)


def test_progress() -> None:
    def setup(swaps: Any) -> tuple[list[str], list[float], "engine._Progress"]:
        lines: list[str] = []
        now = [1000.0]
        skips = engine._Skips(lambda _: None, False)
        skips.add(engine.SkipKind.CHANGED, engine.FullPath("/x"), "")
        return lines, now, engine._Progress(200, swaps, skips, lines.append, lambda: now[0])

    lines, now, progress = setup(SimpleNamespace(cloned=5))
    progress.advance(50)
    now[0] += engine.PROGRESS_SECONDS - 1
    progress.advance(1)
    check("progress: silent before the interval", lines == [], str(lines))
    now[0] += 1
    progress.advance(49)
    check("progress: reports counts at the interval",
          lines == ["progress: 100 of 200 files checked (50%), 5 cloned, 1 skipped, 1m00s elapsed"],
          str(lines))
    now[0] += 1
    progress.advance(1)
    check("progress: not again until the next interval", len(lines) == 1, str(lines))

    lines, now, progress = setup(None)
    now[0] += engine.PROGRESS_SECONDS
    progress.advance(2)
    check("progress: a dry run has no clone count", "cloned" not in lines[0], str(lines))


def main() -> int:
    # Resolved because the work directory refuses a symlink in its path, and the
    # temp directory is reached through /var, which is one.
    root = Path(tempfile.mkdtemp(prefix="apfsdedupe-inspect.")).resolve()
    try:
        test_worker_count()
        test_same_answers(root)
        test_read_ahead_is_bounded()
        test_worker_failure_is_not_swallowed()
        test_threaded_run(root)
        test_stale_inspections(root)
        test_progress()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
