#!/usr/bin/env python3
"""Races the apply against a real writer, at the moments a run cannot control.

Each case guards the rule the apply exists to keep: a run never destroys a
write. The gaps are microseconds wide, so a test cannot hit them by timing;
instead it wraps the engine's swap and scan to do the writer's work at the exact
instant it matters, then lets the real code decide. Writers are separate
processes wherever being seen by the process scan is the point.

Run by test/test.sh, as the invoking user.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import apply as engine  # noqa: E402

OLD = b"same bytes\n" * 400
NEW = b"NEW BYTES FROM THE WRITER\n"
HOLD_OPEN = """
import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)
print("ready", flush=True)
sys.stdin.readline()
os.write(fd, sys.argv[2].encode())
os.close(fd)
"""
MAP_WRITABLE = """
import mmap, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
m = mmap.mmap(fd, 0)
os.close(fd)
print("ready", flush=True)
sys.stdin.readline()
m[:len(sys.argv[2])] = sys.argv[2].encode()
m.flush()
"""
failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'PASS' if ok else 'FAIL'}: writers: {label}{' (' + detail + ')' if detail and not ok else ''}")
    failed += 0 if ok else 1


class Writer:
    """A separate process, so the scan sees it the way it sees any other."""

    def __init__(self, program: str, path: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", program, str(path), NEW.decode()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout is not None and self.proc.stdout.readline() == "ready\n"

    def write_and_exit(self) -> None:
        self.proc.communicate("go\n")


class Fixture:
    def __init__(self, root: Path, name: str) -> None:
        self.dir = root / name
        (self.dir / "a").mkdir(parents=True)
        (self.dir / "b").mkdir()
        self.canonical, self.dup = self.dir / "a" / "f", self.dir / "b" / "f"
        self.canonical.write_bytes(OLD)
        self.dup.write_bytes(OLD)
        self.work_dir = engine.FullPath(str(self.dir / ".work"))
        self.work_fd = engine._open_work_dir(self.work_dir)
        self.logged: list[str] = []
        self.skips = engine._Skips(self.logged.append, False)
        self.swaps = engine._Swaps(self.work_fd, self.work_dir, False, self.skips,
                                   self.logged.append, lambda _: None)

    def run(self) -> None:
        engine.dedupe_group([engine.FullPath(str(self.canonical)), engine.FullPath(str(self.dup))],
                            self.swaps, self.skips, lambda _: None)
        self.swaps.flush()

    def path_has_write(self) -> bool:
        return NEW in self.dup.read_bytes()

    def work_files(self) -> list[str]:
        return os.listdir(self.work_fd)

    def skipped(self, kind: "engine.SkipKind") -> bool:
        return self.skips.counts.get(kind, 0) == 1


def with_hooks(before_swap: Optional[Callable[[], None]] = None,
               before_scan: Optional[Callable[[], None]] = None,
               scan: Optional[Callable[[bool], Any]] = None) -> Callable[[], None]:
    """Fire a writer's action once, just before the engine's next swap or scan.
    Returns the function that restores the engine."""
    real_swap, real_scan = engine._swap, engine._scan_writers
    fired = {"swap": False, "scan": False}

    def swap(*a: Any) -> None:
        if before_swap and not fired["swap"]:
            fired["swap"] = True
            before_swap()
        real_swap(*a)

    def scanner(maps: bool) -> Any:
        if before_scan and not maps and not fired["scan"]:
            fired["scan"] = True
            before_scan()
        return scan(maps) if scan else real_scan(maps)

    engine._swap, engine._scan_writers = swap, scanner

    def restore() -> None:
        engine._swap, engine._scan_writers = real_swap, real_scan
    return restore


def replace(path: Path) -> None:
    """What `git checkout` does to a tracked file."""
    path.unlink()
    path.write_bytes(NEW)


def write_in_place(path: Path) -> None:
    with path.open("r+b") as f:
        f.write(NEW)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="apfsdedupe-writers.", dir="/private/tmp"))
    try:
        # Control: with no writer the apply must still reclaim. Every guard below
        # could otherwise pass by refusing to do anything at all.
        f = Fixture(root, "control")
        before = f.dup.stat().st_ino
        f.run()
        check("control: an idle duplicate is cloned", f.dup.stat().st_ino != before)
        check("control: the bytes are intact", f.dup.read_bytes() == OLD)
        check("control: reclaim is counted", f.swaps.cloned == 1)
        check("control: nothing left in the work directory", f.work_files() == [])

        # A reader is not a risk: it keeps the old inode, whose bytes match. Treating
        # one as a writer would leave most of a busy tree unreclaimed.
        f = Fixture(root, "reader")
        before = f.dup.stat().st_ino
        with f.dup.open("rb"):
            f.run()
        check("a reader does not stop the clone", f.dup.stat().st_ino != before)

        # The temp must never appear beside the file: on a git checkout that reads
        # as an untracked file and fails a clean-tree check.
        f = Fixture(root, "no-temp")
        seen: list[str] = []
        restore = with_hooks(before_swap=lambda: seen.extend(os.listdir(f.dup.parent)))
        f.run()
        restore()
        check("no temp file appears in the duplicate's directory", seen == ["f"], str(seen))

        # A writer holding the file when the run starts, the realistic case: logs
        # and databases. A swap would send everything it writes to an unlinked file.
        f = Fixture(root, "held-from-start")
        before = f.dup.stat().st_ino
        w = Writer(HOLD_OPEN, f.dup)
        f.run()
        w.write_and_exit()
        check("held open from the start: left alone", f.dup.stat().st_ino == before)
        check("held open from the start: reported", f.skipped(engine.SkipKind.IN_USE))
        check("held open from the start: its later write lands", f.path_has_write())

        # A writer arriving after the pre-check, the gap no check before the swap
        # can close. It must be caught after the swap, where the answer cannot go
        # stale. Without the undo its write goes to a deleted file.
        f = Fixture(root, "held-after-precheck")
        before = f.dup.stat().st_ino
        ws: list[Writer] = []
        restore = with_hooks(before_swap=lambda: ws.append(Writer(HOLD_OPEN, f.dup)))
        f.run()
        restore()
        ws[0].write_and_exit()
        check("opened after the pre-check: undone", f.dup.stat().st_ino == before)
        check("opened after the pre-check: its later write lands", f.path_has_write())
        check("opened after the pre-check: nothing left over", f.work_files() == [])

        # A write through a mapping whose fd is closed has no fd for a scan to find.
        f = Fixture(root, "mapped")
        before = f.dup.stat().st_ino
        w = Writer(MAP_WRITABLE, f.dup)
        f.run()
        w.write_and_exit()
        check("mapped writable with its fd closed: left alone", f.dup.stat().st_ino == before)
        check("mapped writable with its fd closed: its write lands", f.path_has_write())

        # The race that started this: a git pull replaces the file after the compare
        # and before the swap. A plain rename put the old bytes back over the pull.
        f = Fixture(root, "replace-in-gap")
        restore = with_hooks(before_swap=lambda: replace(f.dup))
        f.run()
        restore()
        check("replaced between compare and swap: the new file survives", f.dup.read_bytes() == NEW)
        check("replaced between compare and swap: nothing left over", f.work_files() == [])

        f = Fixture(root, "write-in-gap")
        restore = with_hooks(before_swap=lambda: write_in_place(f.dup))
        f.run()
        restore()
        check("written in place in the gap: the write survives", f.path_has_write())

        # Bytes changed while every stat field still matches, as a late-flushed
        # mapping can leave them. Only the byte compare can see this.
        f = Fixture(root, "same-stat")
        real_id = engine._file_id

        def blind_id(st: os.stat_result) -> "engine._FileId":
            return engine._FileId(st.st_ino, 0, st.st_size)

        def rewrite_same_size() -> None:
            displaced = f.dir / ".work" / f.work_files()[0]
            displaced.write_bytes(OLD.replace(b"same", b"SAME", 1))

        engine._file_id = blind_id
        restore = with_hooks(before_scan=rewrite_same_size)
        f.run()
        restore()
        engine._file_id = real_id
        check("changed with identical stat: restored by the byte compare",
              f.dup.read_bytes().startswith(b"SAME"))

        # A write to the path after the swap is the newest version and needs no
        # rescue; undoing here would be what loses it.
        f = Fixture(root, "written-after-swap")
        restore = with_hooks(before_scan=lambda: replace(f.dup))
        f.run()
        restore()
        check("replaced after the swap: the new file stands", f.dup.read_bytes() == NEW)
        check("replaced after the swap: not counted as reclaimed", f.swaps.cloned == 0)
        check("replaced after the swap: nothing left over", f.work_files() == [])

        # Two writers, one on each side of the swap: neither can be preferred, so
        # the only safe outcome is keeping both and telling a person.
        f = Fixture(root, "two-writers")

        def both() -> None:
            (f.dir / ".work" / f.work_files()[0]).write_bytes(b"OTHER WRITER\n")
            replace(f.dup)

        restore = with_hooks(before_scan=both)
        f.run()
        restore()
        kept = [(f.dir / ".work" / n).read_bytes() for n in f.work_files()]
        check("two writers: the path's version stands", f.dup.read_bytes() == NEW)
        check("two writers: the other version is kept", kept == [b"OTHER WRITER\n"], str(kept))
        check("two writers: a warning names it", any("warning:" in m for m in f.logged))

        # A root run that cannot see a process cannot say a file is safe to delete.
        f = Fixture(root, "blind-root")
        before = f.dup.stat().st_ino
        real_euid = os.geteuid
        spot = engine._BlindSpot(4242, "stubborn-daemon", "Operation not permitted")
        restore = with_hooks(scan=lambda maps: engine._WriterScan(set(), [spot]))
        os.geteuid = lambda: 0
        f.run()
        os.geteuid = real_euid
        restore()
        check("blind scan as root: undone, not trusted", f.dup.stat().st_ino == before)
        check("blind scan as root: not reported as a writer", f.skipped(engine.SkipKind.UNVERIFIED))
        check("blind scan as root: nothing left over", f.work_files() == [])
        # The report is the only thing an operator sees, so it must name the
        # process, give the reason, and say that nothing was lost.
        report = "\n".join(f.swaps.blind_report(True, 1))
        check("blind scan as root: the report names the process", "pid 4242 (stubborn-daemon)" in report, report)
        check("blind scan as root: the report gives the reason", "Operation not permitted" in report, report)
        check("blind scan as root: the report says nothing was lost", "Nothing was lost" in report, report)
        f.swaps.blind = {pid: engine._BlindSpot(pid, "p", "x") for pid in range(1, 501)}
        report = "\n".join(f.swaps.blind_report(True, 1))
        check("blind scan as root: a mass failure stays readable", "...and 480 more" in report
              and report.count("pid ") == engine.BLIND_REPORT_MAX, report)

        # Without root the same blindness is the normal case. Refusing would make a
        # per-user run reclaim nothing, so it proceeds and the summary says so.
        f = Fixture(root, "blind-user")
        before = f.dup.stat().st_ino
        restore = with_hooks(scan=lambda maps: engine._WriterScan(set(), [spot]))
        os.geteuid = lambda: 501
        f.run()
        os.geteuid = real_euid
        restore()
        check("blind scan without root: proceeds", f.dup.stat().st_ino != before)
        report = "\n".join(f.swaps.blind_report(False, 0))
        check("blind scan without root: the cost is stated", "could lose data" in report, report)
        check("blind scan without root: the fix is stated", "sudo" in report, report)
        check("blind scan without root: not reported as a fault", "warning" not in report, report)
        check("no blind spots: no report", engine._Swaps(f.work_fd, f.work_dir, False, f.skips,
              f.logged.append, lambda _: None).blind_report(True, 0) == [])

        # The real scan, with no stubs, against what this process can really see.
        scan = engine._scan_writers(maps=False)
        if os.geteuid() == 0:
            check("real scan as root: every process readable", scan.blind == [], str(scan.blind[:3]))
        else:
            check("real scan without root: other users' processes are reported, with a reason",
                  bool(scan.blind) and all(b.pid and b.reason for b in scan.blind), str(scan.blind[:3]))

        # The scan parses kernel structs. If they change, it must stop the run
        # rather than quietly report that nothing is open.
        f = Fixture(root, "self-check")
        check("self-check passes on this macOS", engine._writer_scan_works(f.work_fd))
        restore = with_hooks(scan=lambda maps: engine._WriterScan(set(), []))
        check("self-check fails if the scan finds nothing", not engine._writer_scan_works(f.work_fd))
        restore()
        check("self-check leaves nothing behind", f.work_files() == [])

        # A batch must survive its group and settle all at once: many swaps, one scan.
        f = Fixture(root, "batch")
        extra = [f.dir / "b" / f"f{i}" for i in range(engine.SETTLE_MAX_PENDING * 2 + 3)]
        for path in extra:
            path.write_bytes(OLD)
        scans: list[int] = []
        real_scan = engine._scan_writers

        def counting(maps: bool) -> Any:
            if not maps:
                scans.append(1)
            return real_scan(maps)

        engine._scan_writers = counting
        engine.SETTLE_MAX_SECONDS, saved = 3600.0, engine.SETTLE_MAX_SECONDS
        engine.dedupe_group([engine.FullPath(str(p)) for p in [f.canonical, *extra]],
                            f.swaps, f.skips, lambda _: None)
        f.swaps.flush()
        engine.SETTLE_MAX_SECONDS = saved
        engine._scan_writers = real_scan
        check("batch: every duplicate cloned", f.swaps.cloned == len(extra), str(f.swaps.cloned))
        check("batch: one scan per batch, not per file", len(scans) == 3, str(len(scans)))
        check("batch: nothing left over", f.work_files() == [])
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
