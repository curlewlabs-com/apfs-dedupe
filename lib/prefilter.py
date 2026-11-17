#!/usr/bin/env python3
"""apfs-dedupe prefilter: keep fclones from re-reading files that are already clones.

A re-run of apfs-dedupe over a tree it has already deduped finds the same
duplicate groups as the first run, and fclones hashes every byte of every one of
them again before the apply recognizes the clones and leaves them alone. This
walk runs first and takes those files out of fclones' input.

It lists the scope with getattrlistbulk(2), which returns a whole directory's
metadata in a few calls and never reads file data, and asks APFS for each file's
clone id (ATTR_CMNEXT_CLONEID) and clone count (ATTR_CMNEXT_CLONE_REFCNT). The
clone id names the data stream a file reads its extents from. clonefile(2) gives
the clone the source's data stream, and the first change to either file -- a
write, a write through a mapping once it reaches the file, a truncate, an
extend, a punched hole -- moves the changed file to a new one. So two regular
files of the same size with the same clone id on the same device read the same
extents: their bytes are equal without reading them. getattrlist(2) documents
the id as the way to find "files that are pure clones of each other", and
test/test_prefilter.py checks each of those changes against the volume it runs on.

ATTR_CMNEXT_PRIVATESIZE is not used: it counts bytes a snapshot still holds as
shared, so a file can report no private bytes and still differ from every other
file on the volume.

For each set of such files only the first is given to fclones, which hashes it as
before. The rest are withheld and written to the --shared-out report, and the
apply engine adds them back to that file's group (or makes them a group of their
own), where its own full extent-map check and, before any change, its byte
compare still run on every one of them. A file whose sharing is not proven this
way is always given to fclones:

  - it has no clone of the same size in the scope (refcount below 2, or the
    others are outside the scope or excluded);
  - it is hard-linked, compressed (decmpfs keeps the bytes outside the data
    stream), dataless, or below --min;
  - its path might match an --exclude pattern (see _MightExclude).

fclones still applies every filter itself: it reads the same --exclude, --min,
--one-fs, --hidden and --no-ignore options on its --stdin input. This walk only
decides which paths to offer it. A directory the walk cannot list, or lists but
cannot describe safely on stdin (a name with a newline, a name that is not
UTF-8), is offered to fclones as a directory, which fclones walks itself, so its
errors and its results are what they were without the prefilter.

Output: fclones' input on stdout, one path per line; the withheld sets as JSON in
--shared-out. --check exits 0 if the prefilter can run with these arguments and
3 (with the reason on stderr) if the wrapper should fall back to a plain fclones
walk.

macOS / APFS only. See docs/architecture.md "Re-runs: hashing only what changed".
"""
import argparse
import ctypes
import ctypes.util
import errno
import fnmatch
import json
import os
import queue
import re
import struct
import sys
import threading
from apply import inspect_workers
from typing import IO, Callable, Iterator, NamedTuple, Optional, Sequence, Union, cast

FALLBACK = 3

# <sys/attr.h>
ATTR_BIT_MAP_COUNT = 5
ATTR_CMN_NAME = 0x00000001
ATTR_CMN_DEVID = 0x00000002
ATTR_CMN_OBJTYPE = 0x00000008
ATTR_CMN_FLAGS = 0x00040000
ATTR_CMN_FILEID = 0x02000000
ATTR_CMN_ERROR = 0x20000000
ATTR_CMN_RETURNED_ATTRS = 0x80000000
ATTR_DIR_MOUNTSTATUS = 0x00000004
DIR_MNTSTATUS_MNTPOINT = 0x00000001
ATTR_FILE_LINKCOUNT = 0x00000001
ATTR_FILE_DATALENGTH = 0x00000200
ATTR_CMNEXT_CLONEID = 0x00000100
ATTR_CMNEXT_CLONE_REFCNT = 0x00001000
FSOPT_PACK_INVAL_ATTRS = 0x00000008
FSOPT_ATTR_CMN_EXTENDED = 0x00000020
# <sys/vnode.h> enum vtype
VREG = 1
VDIR = 2
# <sys/stat.h>
UF_COMPRESSED = 0x00000020
SF_DATALESS = 0x40000000

BULK_BUFFER = 256 * 1024

_COMMON = (ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_ERROR | ATTR_CMN_NAME | ATTR_CMN_DEVID
           | ATTR_CMN_OBJTYPE | ATTR_CMN_FLAGS | ATTR_CMN_FILEID)
_DIR = ATTR_DIR_MOUNTSTATUS
_FILE = ATTR_FILE_LINKCOUNT | ATTR_FILE_DATALENGTH
_EXT = ATTR_CMNEXT_CLONEID | ATTR_CMNEXT_CLONE_REFCNT


class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


_lib = ctypes.CDLL(ctypes.util.find_library("System"), use_errno=True)
# int getattrlistbulk(int dirfd, struct attrlist *, void *buf, size_t, uint64_t options)
_lib.getattrlistbulk.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_uint64)
_lib.getattrlistbulk.restype = ctypes.c_int

# With FSOPT_PACK_INVAL_ATTRS every requested attribute of the groups that apply
# to an entry is packed, zero-filled where the volume has no value, so a file
# record and a directory record each have one fixed layout: a file carries no
# directory attributes and a directory no file attributes. The returned attribute
# set says which values are real. Each attribute is 4-byte aligned, in bit order
# within its group, with ATTR_CMN_ERROR first. Decoding is where the walk spends
# its time, so it is two unpacks per entry.
_HEAD = struct.Struct(
    "=I"       # record length
    "5I"       # attribute_set_t returned: common, vol, dir, file, extended
    "I"        # ATTR_CMN_ERROR
    "iI"       # ATTR_CMN_NAME: attrreference_t (offset from itself, length)
    "i"        # ATTR_CMN_DEVID
    "I"        # ATTR_CMN_OBJTYPE
    "I"        # ATTR_CMN_FLAGS
    "Q")       # ATTR_CMN_FILEID
_FILE_TAIL = struct.Struct(
    "=I"       # ATTR_FILE_LINKCOUNT
    "q"        # ATTR_FILE_DATALENGTH
    "Q"        # ATTR_CMNEXT_CLONEID
    "I")       # ATTR_CMNEXT_CLONE_REFCNT
_DIR_TAIL = struct.Struct(
    "=I"       # ATTR_DIR_MOUNTSTATUS
    "Q"        # ATTR_CMNEXT_CLONEID
    "I")       # ATTR_CMNEXT_CLONE_REFCNT
_NAME_REF = 4 + 20 + 4
_LENGTH = struct.Struct("=I")


class Entry(NamedTuple):
    """One directory entry as getattrlistbulk described it. objtype is None when
    the kernel reported an error for the entry instead of its attributes, and
    clone_id / clone_refcnt are None where the volume does not report them."""
    name: bytes
    objtype: Optional[int]
    dev: int
    flags: int
    mounted_on: bool
    nlink: int
    size: int
    clone_id: Optional[int]
    clone_refcnt: Optional[int]


def _parse_entry(buf: memoryview, base: int) -> Entry:
    (length, common, _vol, dirs, files, ext, err, name_off, name_len, dev, objtype, flags,
     _fileid) = _HEAD.unpack_from(buf, base)
    start = base + _NAME_REF + name_off
    name = bytes(buf[start:start + name_len]).split(b"\0", 1)[0]
    known = bool(common & ATTR_CMN_OBJTYPE) and not (common & ATTR_CMN_ERROR and err)
    mounted_on, nlink, size = False, 0, -1
    clone_id: Optional[int] = None
    clone_refcnt: Optional[int] = None
    if files and _HEAD.size + _FILE_TAIL.size <= length:
        nlink, size, cid, refcnt = _FILE_TAIL.unpack_from(buf, base + _HEAD.size)
        if not files & ATTR_FILE_LINKCOUNT:
            nlink = 0
        if not files & ATTR_FILE_DATALENGTH:
            size = -1
    elif dirs and _HEAD.size + _DIR_TAIL.size <= length:
        mstatus, cid, refcnt = _DIR_TAIL.unpack_from(buf, base + _HEAD.size)
        mounted_on = bool(dirs & ATTR_DIR_MOUNTSTATUS and mstatus & DIR_MNTSTATUS_MNTPOINT)
    else:
        return Entry(name, None, dev, flags, False, 0, -1, None, None)
    if ext & ATTR_CMNEXT_CLONEID:
        clone_id = cid
    if ext & ATTR_CMNEXT_CLONE_REFCNT:
        clone_refcnt = refcnt
    return Entry(name, objtype if known else None, dev, flags, mounted_on, nlink, size,
                 clone_id, clone_refcnt)


_ATTRS = _AttrList(ATTR_BIT_MAP_COUNT, 0, _COMMON, 0, _DIR, _FILE, _EXT)
# One buffer per thread for the whole walk, read through a memoryview: allocating
# or copying 256 KiB per directory costs more than listing a small one.
_local = threading.local()


def _buffer() -> "tuple[ctypes.Array[ctypes.c_char], memoryview]":
    try:
        return cast("tuple[ctypes.Array[ctypes.c_char], memoryview]", _local.buf)
    except AttributeError:
        buf = ctypes.create_string_buffer(BULK_BUFFER)
        pair = (buf, memoryview(buf).cast("B"))
        _local.buf = pair
        return pair


def list_dir(path: str) -> list[Entry]:
    """Every entry of the directory at path, described by getattrlistbulk. Raises
    OSError if the directory cannot be opened or read. O_NOFOLLOW: the walk never
    follows a symlink, as fclones does not."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        al = _ATTRS
        buf, view = _buffer()
        entries: list[Entry] = []
        while True:
            n = _lib.getattrlistbulk(fd, ctypes.byref(al), buf, BULK_BUFFER,
                                 FSOPT_ATTR_CMN_EXTENDED | FSOPT_PACK_INVAL_ATTRS)
            if n < 0:
                e = ctypes.get_errno()
                raise OSError(e, os.strerror(e), path)
            if n == 0:
                return entries
            raw = view
            base = 0
            for _ in range(n):
                (length,) = _LENGTH.unpack_from(raw, base)
                if length < _HEAD.size:
                    raise OSError(errno.EBADMSG, "short getattrlistbulk record", path)
                entries.append(_parse_entry(raw, base))
                base += length
    finally:
        os.close(fd)


# ---- --exclude patterns ----------------------------------------------------
# fclones matches each --exclude glob against the absolute path, with ** as any
# characters, * and ? as any but '/', and a relative glob anchored at its working
# directory; on --stdin input it also drops a file whose path plus '/' a pattern
# matches as a prefix. Rather than re-implement that exactly, the walk uses two
# deliberately one-sided approximations, so a mismatch can only cost speed:
#
#   _Prune      SUBSET:   a directory it prunes, fclones would skip too. Pruning
#                         less than fclones only means offering it paths it drops.
#   _MightExclude SUPERSET: every path fclones could exclude, and more. A file it
#                         flags is never withheld, so a withheld file is one
#                         fclones would have kept; flagging more only means
#                         hashing a file the prefilter could have skipped.

_SEGMENT_SAFE = re.compile(r"[^*?\[\]{}\\/]*(?:[*?][^*?\[\]{}\\/]*)*")


class _Prune:
    """Patterns of the one shape that names whole directories: **/a/b/** where each
    segment is a literal or uses only * and ?, matched within one component (the
    narrower reading, so the result stays a subset of what fclones excludes)."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self.rules: list[tuple[str, ...]] = []
        for p in patterns:
            if not (p.startswith("**/") and p.endswith("/**")):
                continue
            segs = tuple(p[3:-3].split("/"))
            if segs and all(s and s != "**" and _SEGMENT_SAFE.fullmatch(s) for s in segs):
                self.rules.append(segs)

    def matches(self, path: str) -> bool:
        if not self.rules:
            return False
        parts = path.split("/")
        for segs in self.rules:
            n = len(segs)
            if len(parts) > n and all(fnmatch.fnmatchcase(parts[-n + i], segs[i]) for i in range(n)):
                return True
        return False


def _literal_chunks(glob: str) -> list[str]:
    """The glob's literal text, split at each wildcard construct ([...], {...}, *,
    ?, an escape). Any string the glob matches, under any reading of those
    constructs and any anchoring, contains these chunks in this order."""
    chunks: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if c in "\\*?[{":
            chunks.append("".join(cur))
            cur = []
            if c == "\\":
                i += 2
            elif c in "[{":
                j = glob.find("]" if c == "[" else "}", i + 1)
                i = len(glob) if j < 0 else j + 1
            else:
                i += 1
        else:
            cur.append(c)
            i += 1
    chunks.append("".join(cur))
    return [c for c in chunks if c]


class _MightExclude:
    """True for any path an --exclude might drop: one that contains each pattern's
    literal chunks in order. That ignores where a wildcard may match and where a
    pattern is anchored, so it covers fclones' rules whatever their details, and
    it is linear in the path, unlike a regex with a leading .* per pattern. The
    path is tried with a '/' appended too, for fclones' prefix test on --stdin
    input."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self.patterns = [_literal_chunks(p) for p in patterns]

    @staticmethod
    def _contains_in_order(text: str, chunks: Sequence[str]) -> bool:
        at = 0
        for chunk in chunks:
            at = text.find(chunk, at)
            if at < 0:
                return False
            at += len(chunk)
        return True

    def matches(self, path: str) -> bool:
        probe = path + "/"
        return any(self._contains_in_order(probe, chunks) for chunks in self.patterns)


# ---- --min --------------------------------------------------------------------
_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1000, "kib": 1024, "m": 1024**2, "mb": 1000**2,
          "mib": 1024**2, "g": 1024**3, "gb": 1000**3, "gib": 1024**3, "t": 1024**4,
          "tb": 1000**4, "tib": 1024**4}


def parse_min(text: str) -> Optional[int]:
    """--min as a byte count no smaller than fclones' reading of it, or None if it
    does not parse. Each single-letter unit takes its binary value, the larger of
    the two readings, so a size this admits is one fclones admits too."""
    m = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]*)\s*", text)
    if not m:
        return None
    unit = _UNITS.get(m.group(2).lower())
    if unit is None:
        return None
    return int(m.group(1)) * unit


# ---- the walk -----------------------------------------------------------------
def _describable(name: bytes) -> Optional[str]:
    """The name as fclones can read it back from a line of stdin, or None: it
    reads UTF-8 lines and strips a trailing CR."""
    if b"\n" in name or b"\r" in name:
        return None
    try:
        return name.decode("utf-8")
    except UnicodeDecodeError:
        return None


class Walk:
    """The scope's files split into fclones' input and the withheld clone sets."""

    def __init__(self, scope: str, min_size: int, excludes: Sequence[str]) -> None:
        self.scope = scope
        self.min_size = max(1, min_size)
        self.prune = _Prune(excludes)
        self.might_exclude = _MightExclude(excludes)
        self.scope_dev = os.lstat(scope).st_dev
        # (dev, clone id, size) -> files sharing that data stream, in walk order.
        self.clones: dict[tuple[int, int, int], list[str]] = {}

    def _candidate(self, path: str, e: Entry) -> bool:
        return (e.clone_id is not None and e.clone_id != 0 and e.clone_refcnt is not None
                and e.clone_refcnt >= 2 and e.nlink == 1 and e.size >= self.min_size
                and not e.flags & (UF_COMPRESSED | SF_DATALESS)
                and not self.might_exclude.matches(path))

    def run(self, out: Callable[[str], None], workers: int = 1) -> None:
        """Offer every input to out, except the clone sets, held back for sets().
        Directories are listed on worker threads, since listing waits on the disk
        and getattrlistbulk releases the GIL; everything else, out() included, runs
        on this thread. Plain queues rather than futures: waiting on a set of
        futures rescans the whole set, which is quadratic over a tree's
        directories."""
        todo: "queue.SimpleQueue[Optional[str]]" = queue.SimpleQueue()
        done: "queue.SimpleQueue[tuple[str, Union[list[Entry], OSError]]]" = queue.SimpleQueue()

        def lister() -> None:
            while True:
                d = todo.get()
                if d is None:
                    return
                try:
                    done.put((d, list_dir(d)))
                except OSError as e:
                    done.put((d, e))

        threads = [threading.Thread(target=lister, daemon=True) for _ in range(max(1, workers))]
        for t in threads:
            t.start()
        try:
            todo.put(self.scope)
            outstanding = 1
            while outstanding:
                d, listed = done.get()
                outstanding -= 1
                for sub in self._visit(d, listed, out):
                    todo.put(sub)
                    outstanding += 1
        finally:
            for _ in threads:
                todo.put(None)
        for paths in self.clones.values():
            paths.sort()        # the path offered does not depend on thread timing
            out(paths[0])

    def _visit(self, d: str, listed: "Union[list[Entry], OSError]",
               out: Callable[[str], None]) -> list[str]:
        """Sort one listed directory's entries into out() and the clone sets;
        return the subdirectories to list."""
        if isinstance(listed, OSError):
            out(d)              # fclones lists it, and reports why it cannot
            return []
        entries = listed
        names = [_describable(e.name) for e in entries]
        if any(n is None for n in names):
            out(d)              # fclones walks it; nothing in it is withheld
            return []
        subdirs: list[str] = []
        prefix = d if d.endswith("/") else d + "/"
        for e, name in zip(entries, names):
            assert name is not None
            path = prefix + name
            if e.objtype is None:
                out(path)       # unknown: fclones decides what it is
            elif e.objtype == VDIR:
                if e.mounted_on or e.dev != self.scope_dev:
                    continue                    # --one-fs
                if self.prune.matches(path):
                    continue
                if e.flags & SF_DATALESS:
                    out(path)                   # as before: fclones' call
                    continue
                subdirs.append(path)
            elif e.objtype == VREG:
                if self._candidate(path, e):
                    assert e.clone_id is not None
                    self.clones.setdefault((e.dev, e.clone_id, e.size), []).append(path)
                else:
                    out(path)
            # symlinks and special files: fclones skips them without -L/-S
        return subdirs

    def sets(self) -> Iterator[tuple[int, list[str]]]:
        """(size, files) for each withheld set; files[0] went to fclones."""
        for (_dev, _cid, size), paths in self.clones.items():
            if len(paths) >= 2:
                yield size, paths


def check(scope: str, min_text: str) -> Optional[str]:
    """Why the prefilter cannot run here, or None if it can."""
    if parse_min(min_text) is None:
        return f"cannot read --min {min_text!r}"
    try:
        list_dir(scope)
    except OSError as e:
        return f"cannot list {scope}: {e.strerror}"
    return None


def main(argv: Optional[Sequence[str]] = None, stdout: IO[str] = sys.stdout) -> int:
    p = argparse.ArgumentParser(description="List fclones' input, withholding proven clones.")
    p.add_argument("--scope", required=True)
    p.add_argument("--min", required=True)
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--check", action="store_true",
                   help="exit 0 if the prefilter can run, 3 (reason on stderr) if not")
    p.add_argument("--shared-out", help="where to write the withheld clone sets (JSON)")
    # Listing waits on the disk, so threads overlap the waits; the count is the
    # apply's (one per efficiency core), for the same reason: a scheduled run is a
    # background job macOS keeps on those cores.
    p.add_argument("--workers", type=int, default=None,
                   help="threads listing directories (default: one per efficiency core)")
    args = p.parse_args(argv)
    reason = check(args.scope, args.min)
    if reason is not None:
        print(f"prefilter: {reason}; hashing every file", file=sys.stderr)
        return FALLBACK
    if args.check:
        return 0
    if not args.shared_out:
        p.error("--shared-out is required")
    min_size = parse_min(args.min)
    assert min_size is not None
    walk = Walk(args.scope, min_size, args.exclude)

    def out(path: str) -> None:
        stdout.write(path + "\n")

    try:
        walk.run(out, args.workers or inspect_workers())
        stdout.flush()
    except BrokenPipeError:
        print("prefilter: fclones stopped reading its input", file=sys.stderr)
        return 1
    sets = list(walk.sets())
    report = {
        "withheld": sum(len(files) - 1 for _size, files in sets),
        "groups": [{"file_len": size, "files": files} for size, files in sets],
    }
    with open(args.shared_out, "w", encoding="utf-8") as f:
        json.dump(report, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
