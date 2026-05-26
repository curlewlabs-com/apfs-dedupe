#!/usr/bin/env python3
"""apfs-dedupe apply engine.

Reads an `fclones group --format json` report on stdin and replaces each
duplicate with an APFS clone of the first file in its group, so identical
files share storage (copy-on-write) until one of them is modified.

This exists instead of `fclones dedupe` because the apply differs on three
points that matter when running broadly, as root, against live data:

  1. Windowless, crash-safe, and symlink-safe. We clone canonical into an
     unguessably named temp *relative to a fd for the duplicate's parent
     directory* (opened refusing a symlink in any path component), then
     atomically rename it over the duplicate. The path is never absent and a
     crash leaves the original untouched; resolving every step against that dir
     fd (clonefileat/openat/renameat) keeps a local user who owns an ancestor
     from swapping a path component to redirect a root clone elsewhere.
     (fclones moves the original aside first by path, so the path briefly has
     no file -- and stays that way across a crash.)

  2. Metadata-complete, including ACLs. clonefile(2) gives the clone the
     *source's* metadata, which is wrong -- we want the duplicate's own. A
     single fcopyfile(3) with COPYFILE_METADATA replicates the duplicate's
     mode, owner, timestamps, xattrs AND ACLs onto the clone. (fclones drops
     ACLs.)

  3. Fail-safe. A file we cannot modify -- immutable (uchg), locked by a
     deny ACL, or permission denied -- is skipped with a warning, never
     forced.

clonefile(2) is not content-verified by the kernel (unlike Linux
FIDEDUPERANGE), so after cloning we re-compare the clone against the
duplicate's current bytes, right before the atomic swap. This guards against
the file changing between fclones' scan and the moment we touch it -- a window
of seconds to minutes on a large run, easily long enough for a build to rewrite
an artifact.

macOS / APFS only. See docs/architecture.md for the full rationale.
"""
import argparse
import contextlib
import ctypes
import ctypes.util
import enum
import errno
import fcntl
import json
import os
import platform
import secrets
import stat
import struct
import sys
from collections.abc import Callable, Generator
from typing import NamedTuple, NewType, NoReturn, Optional, TypedDict, cast

# A path that resolves on its own -- absolute, or relative to the process cwd --
# so it is safe to hand to a path-based syscall and to name a file in a
# diagnostic. fclones reports absolute paths; the JSON boundary in main() is the
# one place external strings are branded FullPath.
FullPath = NewType("FullPath", str)
# A single trailing path component, meaningful ONLY relative to a directory fd.
# Branding it apart from FullPath turns the ambiguous-basename mistake -- logging
# a basename as if it named the file, or opening it by path instead of relative
# to the fd -- into a type error rather than a latent diagnostic regression.
Basename = NewType("Basename", str)

# Output sinks (see dedupe_group/main): log -> stderr (progress, and per-file skip
# lines only under --verbose -- otherwise skips are summarized by reason, see
# _Skips). emit -> stdout (the report a user redirects to a file): the dry-run
# plan always, and in --apply a per-file `cloned` line only under --verbose
# (otherwise just the run summary).
Log = Callable[[str], None]
Emit = Callable[[str], None]

# copyfile(3) flags, from <copyfile.h>:
#   COPYFILE_ACL(1) | COPYFILE_STAT(2) | COPYFILE_XATTR(4) == METADATA, no DATA.
# This is the one call that carries ACLs across, which is the gap in fclones.
COPYFILE_METADATA = 0x0007
# clonefile(2) flag (macOS 15+), from <sys/clonefile.h>: fail with ELOOP if *any*
# component of the source or destination path is a symlink -- the final component
# included (verified: CLONE_NOFOLLOW_ANY refuses a symlinked source, whereas the
# weaker CLONE_NOFOLLOW would clone the link itself). This keeps a local user who
# owns an ancestor of the clone source -- or the canonical itself -- from racing
# in a symlink to redirect the read onto an arbitrary same-volume file; it is the
# clone-side analog of O_NOFOLLOW_ANY. Firmlinks such as /Users still resolve.
# It first appears in macOS 15 (Apple's <sys/clonefile.h> through macOS 14 defines
# only CLONE_NOFOLLOW/NOOWNERCOPY/ACL), so 15 is the apply's floor -- the binding
# one, since O_NOFOLLOW_ANY is older (macOS 11). See _supports_symlink_safe_clone.
CLONE_NOFOLLOW_ANY = 0x0008
# "Resolve relative paths against the current working directory" sentinel for
# the *at syscalls, used for the clone's absolute-path source. <fcntl.h>
AT_FDCWD = -2
# open(2) flag (macOS 11+), from <fcntl.h>: fail with ELOOP if *any*
# component of the path is a symlink -- the equivalent of Linux openat2's
# RESOLVE_NO_SYMLINKS. O_NOFOLLOW only guards the final component. Firmlinks
# (e.g. /Users) are not symlinks and still resolve, so the default scope works.
O_NOFOLLOW_ANY = 0x20000000
# fcntl(2) op (from <sys/fcntl.h>): F_LOG2PHYS_EXT maps a file offset+length to
# the physical device offset and contiguous run length of the extent backing it.
# Walking it from byte 0 to EOF yields the file's full extent map; two files on
# the same device with identical maps share all their storage (a prior clone) --
# a physical block belongs to one allocation, so equal device offsets are proof
# of sharing, never coincidence (an offset is meaningful only within its device,
# so _extent_map carries the device id alongside the segments). struct log2phys
# is 4-byte *packed* (legacy 32-bit ABI): flags@0, contigbytes@4, devoffset@12 --
# 20 bytes ("=Iqq"); the naturally aligned "Iqq" is 24 bytes and misreads
# devoffset, returning ERANGE. See _extent_map.
F_LOG2PHYS_EXT = 65
_LOG2PHYS_FMT = "=Iqq"
# F_LOG2PHYS_EXT reports a hole (an unallocated range) with a negative device
# offset; normalize every hole to this sentinel so equal-length holes compare
# equal regardless of the exact negative value returned. A real data extent's
# device offset is a large non-negative number, never this.
_HOLE = -1
# stat(2) st_flags bit (from <sys/stat.h>): the file is a "dataless" placeholder
# -- a cloud-backed file (iCloud Drive, a File Provider) whose contents have been
# evicted to reclaim space. It reports its full logical size but holds no local
# blocks; any read() faults the bytes back down from the cloud. Reading or
# cloning one would re-download deliberately-evicted content, so we never touch
# it: the wrapper excludes the cloud roots from the fclones scan (where the
# download would happen, since fclones hashes by reading), and this is the
# apply-path backstop for a file that reaches the engine unread -- a cached
# fclones run, or direct invocation on hand-built JSON.
SF_DATALESS = 0x40000000

_lib = ctypes.CDLL(ctypes.util.find_library("System"), use_errno=True)
# int clonefileat(int src_dirfd, const char *src, int dst_dirfd,
#                 const char *dst, uint32_t flags)              <sys/clonefile.h>
_lib.clonefileat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_char_p, ctypes.c_uint32)
_lib.clonefileat.restype = ctypes.c_int
# int fcopyfile(int from_fd, int to_fd, copyfile_state_t, copyfile_flags_t)
_lib.fcopyfile.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
_lib.fcopyfile.restype = ctypes.c_int


def _raise_errno() -> NoReturn:
    e = ctypes.get_errno()
    raise OSError(e, os.strerror(e))


def _clonefileat(src_abs: FullPath, dst_dirfd: int, dst_name: Basename) -> None:
    """Clone src_abs into dst_name relative to dst_dirfd. The destination
    resolves against the dir fd, not a path, so a swapped parent component
    cannot redirect where the clone lands; CLONE_NOFOLLOW_ANY refuses a symlink
    anywhere in the source path (or as the destination), so the source cannot be
    redirected either."""
    if _lib.clonefileat(AT_FDCWD, os.fsencode(src_abs),
                        dst_dirfd, os.fsencode(dst_name), CLONE_NOFOLLOW_ANY) != 0:
        _raise_errno()


def _fcopy_metadata(src_fd: int, dst_fd: int) -> None:
    """Replicate src_fd's mode/owner/times/xattrs/ACLs onto dst_fd -- not its
    data. Operating on open fds means no path is re-resolved mid-apply."""
    if _lib.fcopyfile(src_fd, dst_fd, None, COPYFILE_METADATA) != 0:
        _raise_errno()


@contextlib.contextmanager
def _openfd(name: Basename, flags: int, dirfd: int) -> Generator[int, None, None]:
    """Open name relative to dirfd, never following a final-component symlink."""
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    try:
        yield fd
    finally:
        os.close(fd)


def _open_parent_dir(dup: FullPath) -> int:
    """Open dup's parent directory as a fd, refusing to traverse a symlink in
    *any* path component (O_NOFOLLOW_ANY, not just the final one). This is what
    makes the fd-anchored apply safe end to end: an attacker who owns an
    ancestor of dup cannot swap an intermediate component for a symlink to hand
    us a descriptor for a directory we never intended to touch."""
    parent = os.path.dirname(dup) or "."
    return os.open(parent, os.O_RDONLY | os.O_DIRECTORY | O_NOFOLLOW_ANY | os.O_CLOEXEC)


def _human(n: int) -> str:
    f = float(n)
    # Step down the sub-TiB units; anything still >= 1024 after GiB falls
    # through to TiB (the last unit, so no overflow past it).
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(f) < 1024.0:
            return f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{f:.1f} TiB"


def _human_duration(seconds: int) -> str:
    """A scan duration for the run summary: bare seconds under a minute, then
    Mm SSs, then Hh MMm. Whole seconds is all the wrapper's `date +%s` scan
    timing resolves -- plenty for a sweep measured in minutes."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _same_content_fd(fa: int, fb: int, bufsize: int = 1 << 20) -> bool:
    """True if the two open fds hold identical bytes. Reads from the fds
    directly so the answer reflects the bytes right now (no stat-signature
    cache, unlike filecmp.cmp)."""
    if os.fstat(fa).st_size != os.fstat(fb).st_size:
        return False
    os.lseek(fa, 0, os.SEEK_SET)
    os.lseek(fb, 0, os.SEEK_SET)
    while True:
        ca = os.read(fa, bufsize)
        cb = os.read(fb, bufsize)
        if ca != cb:
            return False
        if not ca:
            return True


def _extent_map(fd: int) -> Optional[tuple[int, tuple[tuple[int, int], ...]]]:
    """(device id, full extent map) of the file's storage, or None if the layout
    cannot be determined. The map is the ordered tuple of (device_offset, run)
    segments backing the file from byte 0 to EOF, a hole written as (_HOLE, run)
    since it holds no blocks.

    Two files share ALL their storage IFF they are on the same device and their
    maps are equal: a physical block belongs to exactly one allocation, so an
    equal device offset for every data extent proves the two reference the same
    blocks (a clone), never a coincidence, and equal holes line up the gaps. This
    generalizes the earlier single-whole-file-extent check -- a fragmented or
    sparse clone is recognized too, so a re-run leaves it alone instead of
    re-cloning it and re-counting its size as reclaimed. The proof stays
    one-directional: any difference, or any segment we cannot map, falls back to
    cloning -- the check only ever fails to prove sharing, never asserts sharing
    that isn't there. A clone whose later range was CoW-broken by an
    identical-bytes rewrite (still byte-identical, so fclones groups it) has a
    diverged extent at a different device offset, so its map differs and it is
    cloned, re-sharing the broken range.

    The device id leads the result because a device offset is meaningful only
    within its device: two independent files on different volumes can share an
    offset by coincidence, so only an equal device makes equal offsets proof. The
    wrapper's `--one-fs` already keeps each group on one volume, but the engine
    runs on arbitrary fclones JSON, so the identity carries the device rather than
    assuming it.

    None (clone normally) covers an empty file and any extent the kernel will not
    map: a transparently compressed (decmpfs) file whose bytes live in an xattr,
    not the data fork (ENOTSUP), a SIP-protected file, ERANGE, etc. fcntl-only: it
    maps extents and never read()s, so it will not fault a dataless iCloud file
    down from the cloud (that download hazard is on the clone/verify path)."""
    st = os.fstat(fd)
    if st.st_size == 0:
        return None
    segments: list[tuple[int, int]] = []
    offset = 0
    while offset < st.st_size:
        # IN: contigbytes = bytes left to map; devoffset = the file offset to map
        # from. OUT: contigbytes = the contiguous run at that offset; devoffset =
        # its physical device offset, negative for a hole.
        buf = struct.pack(_LOG2PHYS_FMT, 0, st.st_size - offset, offset)
        try:
            res = fcntl.fcntl(fd, F_LOG2PHYS_EXT, buf)
        except OSError:
            return None   # ENOTSUP (compressed / SIP-protected), ERANGE, ... -> unknown
        _flags, run, devoff = struct.unpack(_LOG2PHYS_FMT, res)
        if run <= 0:
            return None   # no forward progress -> the map cannot be trusted
        segments.append((_HOLE if devoff < 0 else devoff, run))
        offset += run
    # The walk must land exactly on EOF; a run that overshot means the mapping
    # disagrees with the size we stat'd, so the map cannot be trusted.
    if offset != st.st_size:
        return None
    return (st.st_dev, tuple(segments))


def _extent_map_of(path: FullPath) -> Optional[tuple[int, tuple[tuple[int, int], ...]]]:
    """_extent_map for a path: open it read-only -- refusing a final-component
    symlink -- and map it, returning None on any open failure (gone, no
    permission, a symlink raced in after the lstat). O_NOFOLLOW (not
    O_NOFOLLOW_ANY) keeps this probe working on every macOS version, as the
    dry-run must: it is read-only and fails safe to "clone normally", so it does
    not need the apply path's stronger any-component guard."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return _extent_map(fd)
    finally:
        os.close(fd)


def _is_dataless(st_flags: int) -> bool:
    """True if st_flags marks a cloud-evicted (dataless) file -- one whose bytes
    live in the cloud and would be faulted back down by any read(). Reading or
    cloning it would re-download deliberately-evicted content, so callers skip
    it. Detected from the flag alone (a stat field), never by touching data."""
    return bool(st_flags & SF_DATALESS)


class SkipKind(enum.Enum):
    """Why a duplicate was left untouched, classified at the point of the skip.
    Knowing the reason there lets the run summary break skips down -- and advise
    on the one the user can fix -- without re-parsing log lines. Definition order
    is the order the summary lists them."""
    PERMISSION = "permission"
    SYMLINK = "symlink"
    HARD_LINKED = "hardlink"
    CHANGED = "changed"
    DATALESS = "dataless"
    UNREADABLE = "unreadable"


# How each skip reason reads in the summary breakdown -- phrased for someone
# scanning a nightly log, not the raw errno the --verbose per-file line keeps.
_SKIP_LABEL: dict[SkipKind, str] = {
    SkipKind.PERMISSION: "unreadable (privacy protection or permissions)",
    SkipKind.SYMLINK: "reached through a symlinked path component",
    SkipKind.HARD_LINKED: "hard-linked elsewhere (a clone would break the link)",
    SkipKind.CHANGED: "changed between the scan and the clone",
    SkipKind.DATALESS: "cloud-evicted (a clone would re-download them)",
    SkipKind.UNREADABLE: "could not be examined",
}


def _oserror_kind(e: OSError) -> SkipKind:
    """Bucket a filesystem OSError for the skip summary. EPERM/EACCES is the
    privacy-protection / permission case -- the only one the user can act on (by
    granting Full Disk Access); ELOOP is O_NOFOLLOW_ANY refusing a symlinked path
    component; anything else is lumped as unreadable."""
    if e.errno in (errno.EPERM, errno.EACCES):
        return SkipKind.PERMISSION
    if e.errno == errno.ELOOP:
        return SkipKind.SYMLINK
    return SkipKind.UNREADABLE


class _Skips:
    """Running tally of duplicates left untouched, by reason (see SkipKind).

    On a broad run the skips are the bulk of the output and most -- privacy-
    protected paths above all -- are nothing to act on one file at a time, so by
    default they are counted here and reported as an advised summary rather than
    streamed. --verbose restores the per-file line. `path` is the resolvable
    FullPath, never the dirfd-relative Basename the syscalls use, so a --verbose
    line names the file rather than an ambiguous basename -- the regression that
    branding exists to prevent."""

    def __init__(self, log: Log, verbose: bool) -> None:
        self._log = log
        self._verbose = verbose
        self.counts: dict[SkipKind, int] = {}

    def add(self, kind: SkipKind, path: FullPath, detail: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if self._verbose:
            self._log(f"skip {path}: {detail}")

    def total(self) -> int:
        return sum(self.counts.values())


def _clone_over(canonical: FullPath, dirfd: int, dup_path: FullPath,
                skips: _Skips) -> bool:
    """Replace dup_path with a clone of canonical. Every privileged step is
    anchored on dirfd -- clonefileat/openat/renameat/unlinkat, never a
    re-resolved path -- so a local user who owns the parent directory cannot
    swap a path component to redirect the clone, the metadata copy, or the
    rename into somewhere they do not already control. The fd-relative syscalls
    address the duplicate by its basename (resolved against dirfd); skip records
    use the full dup_path so a failure on a whole-/Users run names the actual
    file, not just an ambiguous basename -- a distinction the Basename/FullPath
    types now enforce. Returns True iff the duplicate was cloned."""
    dup_name = Basename(os.path.basename(dup_path))
    tmp_name = Basename(".apfsdedupe-" + secrets.token_hex(16))
    try:
        # Clone first: tmp is a frozen copy-on-write snapshot of canonical's
        # *current* bytes, so a write to canonical after this point cannot
        # change what we install.
        _clonefileat(canonical, dirfd, tmp_name)
    except OSError as e:
        skips.add(_oserror_kind(e), dup_path, str(e))
        return False
    try:
        with _openfd(dup_name, os.O_RDONLY, dirfd) as src_fd, \
             _openfd(tmp_name, os.O_RDWR, dirfd) as dst_fd:
            # Re-compare the exact bytes we are about to install (tmp) against
            # the duplicate's *current* bytes, right before the swap -- the one
            # check that closes the scan-to-apply window. Never act on a match
            # that went stale.
            if not _same_content_fd(dst_fd, src_fd):
                skips.add(SkipKind.CHANGED, dup_path, f"changed since scan, no longer identical to {canonical}")
                return False
            _fcopy_metadata(src_fd, dst_fd)   # restore the duplicate's own metadata (incl. ACL)
        os.rename(tmp_name, dup_name, src_dir_fd=dirfd, dst_dir_fd=dirfd)  # atomic
        return True
    except OSError as e:
        # Immutable, deny-ACL, permission, cross-device, ... -- fail safe.
        skips.add(_oserror_kind(e), dup_path, str(e))
        return False
    finally:
        # On success the rename consumed tmp_name (so this unlink no-ops with
        # ENOENT); otherwise it removes the leftover clone.
        try:
            os.unlink(tmp_name, dir_fd=dirfd)
        except OSError:
            pass


class _GroupResult(NamedTuple):
    """What dedupe_group did with one group. cloned/reclaimed_* are this run's
    work; already_shared_* are duplicates that were already clones from an
    earlier run and were left untouched -- the latter are the "space earlier runs
    already saved" the summary reports alongside this run's reclaim."""
    cloned: int
    reclaimed_logical: int
    reclaimed_allocated: int
    already_shared: int
    already_shared_logical: int
    already_shared_allocated: int


def _allocated_bytes(st: os.stat_result) -> int:
    """Allocated bytes reported by stat(2). This is the number closest to the
    space a clone can return for sparse or compressed duplicates; st_size remains
    useful as the logical duplicate payload size, but can overstate disk reclaim."""
    return st.st_blocks * 512


def _space_detail(logical: int, allocated: int) -> str:
    """User-facing storage report: allocated first, logical second. Allocated is
    the best estimate of freed disk blocks; logical is the duplicate byte count
    fclones grouped."""
    return f"{_human(allocated)} allocated ({_human(logical)} logical)"


def dedupe_group(files: list[FullPath], apply: bool, verbose: bool,
                 skips: _Skips, emit: Emit) -> _GroupResult:
    """Clone files[1:] from files[0]. Returns a _GroupResult.

    Two output channels: emit() carries the report a user keeps and goes to
    stdout, so `> plan.txt` captures it. In a dry-run that is the per-file plan,
    always emitted -- it is the whole deliverable. In --apply it is a per-file
    `cloned` line emitted only under --verbose: on a broad nightly apply those
    lines are the bulk of the log (one per cloned file) while the run summary
    already reports the totals, so they are opt-in. Skips are tallied by reason on
    `skips` for the run summary; the per-file skip line likewise goes to stderr
    only under --verbose (see _Skips).

    A duplicate with the same full extent map as the canonical, on the same
    device, was cloned on an earlier run; it is counted in already_shared (and
    its size in already_shared_bytes) and left untouched -- in apply and dry-run
    alike, so a re-run reclaims nothing new and the dry-run projection of an
    already-deduped tree shows ~0 reclaimable -- instead of pointlessly rebuilding
    the same sharing and churning the inode."""
    canonical = files[0]
    try:
        cst = os.lstat(canonical)
    except OSError as e:
        skips.add(_oserror_kind(e), canonical, f"cannot stat (skipping group): {e}")
        return _GroupResult(0, 0, 0, 0, 0, 0)
    if not stat.S_ISREG(cst.st_mode):
        return _GroupResult(0, 0, 0, 0, 0, 0)
    # A dataless (cloud-evicted) canonical holds no local bytes; cloning from it
    # would fault the whole file down from the cloud. Skip the group rather than
    # re-download deliberately-evicted content -- the wrapper already keeps the
    # cloud roots out of the scan, this guards a cached/standalone run.
    if _is_dataless(cst.st_flags):
        skips.add(SkipKind.DATALESS, canonical, "dataless (cloud-evicted); skipping group to avoid forcing a download")
        return _GroupResult(0, 0, 0, 0, 0, 0)

    # The canonical's full extent map, computed once (None if it is compressed,
    # empty, or otherwise unmappable -- which disables the check, cloning every
    # duplicate as before). A duplicate with the same map on the same device is
    # already a full clone; see _extent_map for why an equal map proves whole-file
    # sharing, fragmented and sparse files included.
    canonical_map = _extent_map_of(canonical)

    cloned = 0
    reclaimed_logical = 0
    reclaimed_allocated = 0
    already_shared = 0
    already_shared_logical = 0
    already_shared_allocated = 0
    for dup in files[1:]:
        try:
            dst = os.lstat(dup)
        except OSError as e:
            skips.add(_oserror_kind(e), dup, str(e))
            continue
        if not stat.S_ISREG(dst.st_mode):
            continue
        # A dataless (cloud-evicted) duplicate would be faulted down from the
        # cloud by the content re-verify (which read()s it) or the clone. Skip it
        # rather than re-download evicted content (backstop to the scan exclude).
        if _is_dataless(dst.st_flags):
            skips.add(SkipKind.DATALESS, dup, "dataless (cloud-evicted); skipped to avoid forcing a download")
            continue
        # dup and canonical are already the same inode (fclones listed two names
        # for one file, or they are hard links to each other): nothing to
        # reclaim, and cloning would needlessly split them. Leave them intact.
        if (dst.st_dev, dst.st_ino) == (cst.st_dev, cst.st_ino):
            continue
        # dup has more than one link, so another path (outside this group) also
        # names its inode. Cloning over this name would break that hard link --
        # silently turning two names for one inode into two independent files --
        # and reclaim nothing, since the other link still pins the inode's
        # blocks. Skip it (in dry-run too, so the projection matches apply)
        # rather than break a link or miscount st_size as reclaimable.
        if dst.st_nlink > 1:
            skips.add(SkipKind.HARD_LINKED, dup, f"hard-linked ({dst.st_nlink} links); "
                      f"cloning would break the link and reclaim nothing")
            continue
        # Already cloned on an earlier run? The same full extent map on the same
        # device means dup and canonical share ALL their storage, so re-cloning
        # would only churn the inode and reclaim nothing. Count it as
        # already-saved and skip -- in dry-run too, so the projection matches a
        # real re-run. A partially CoW-broken clone has a divergent extent, so its
        # map differs and it is cloned normally.
        if canonical_map is not None and _extent_map_of(dup) == canonical_map:
            already_shared += 1
            already_shared_logical += dst.st_size
            already_shared_allocated += _allocated_bytes(dst)
            continue

        space = _space_detail(dst.st_size, _allocated_bytes(dst))
        if not apply:
            emit(f"would clone {canonical} -> {dup}  ({space})")
            cloned += 1
            reclaimed_logical += dst.st_size
            reclaimed_allocated += _allocated_bytes(dst)
            continue

        # Anchor the apply on a fd for the duplicate's parent directory.
        # clonefile(2) follows symlinks in the destination *path*, so doing the
        # clone/rename by path in a user-writable directory lets a local user
        # redirect a root clone into an arbitrary same-volume location (a
        # privilege escalation). Acquiring the parent fd with no symlink in any
        # component, then resolving every step against that fd
        # (clonefileat/openat/renameat in _clone_over), means a swapped path
        # component cannot redirect them. See docs/architecture.md
        # "Symlink-safe, fd-anchored apply".
        try:
            dirfd = _open_parent_dir(dup)
        except OSError as e:
            skips.add(_oserror_kind(e), dup, f"cannot open parent dir: {e}")
            continue
        try:
            if _clone_over(canonical, dirfd, dup, skips):
                if verbose:
                    emit(f"cloned {canonical} -> {dup}  ({space})")
                cloned += 1
                reclaimed_logical += dst.st_size
                reclaimed_allocated += _allocated_bytes(dst)
        finally:
            os.close(dirfd)
    return _GroupResult(cloned, reclaimed_logical, reclaimed_allocated,
                        already_shared, already_shared_logical, already_shared_allocated)


def _supports_symlink_safe_clone(mac_ver: str) -> bool:
    """True if mac_ver (e.g. '15.0', '26.5') is macOS 15 or newer -- the first
    release whose <sys/clonefile.h> defines CLONE_NOFOLLOW_ANY, which the apply's
    source-path symlink safety depends on. On older systems that flag bit is
    silently ignored (clonefile follows the symlink), reopening the source-path
    race. O_NOFOLLOW_ANY alone is older (macOS 11), but CLONE_NOFOLLOW_ANY is
    the binding constraint, so 15 is the floor. An empty or unparseable version
    fails closed.

    macOS dropped the 10.x scheme at 11, so every supported release is a whole
    major (11, 12, ... 15, 26); the floor is therefore just major >= 15."""
    try:
        major = int(mac_ver.split(".")[0])
    except (AttributeError, ValueError):
        return False
    return major >= 15


# Shape of the `fclones group --format json` report we read on stdin. Only the
# fields we consume are declared; fclones emits more (file_len, file_hash, ...)
# which the cast at the boundary lets ride along untyped. Paths arrive as the
# absolute strings fclones resolved, so the report's `files` are branded FullPath
# here -- this TypedDict + the cast in main() are the single trusted point where
# external strings become FullPath.
class _FclonesGroup(TypedDict):
    files: list[FullPath]


class _FclonesStats(TypedDict, total=False):
    redundant_file_size: int
    redundant_file_count: int


def main() -> int:
    p = argparse.ArgumentParser(
        description="Apply APFS-clone deduplication from an fclones JSON report read on stdin.")
    p.add_argument("--apply", action="store_true",
                   help="actually clone duplicates (default: dry-run, changes nothing)")
    p.add_argument("--verbose", action="store_true",
                   help="print a line per cloned file (in --apply) and per skipped file "
                        "(default: an --apply run prints just the summary; a dry-run always "
                        "prints its full plan)")
    # Scan stats supplied by the wrapper, which is what runs fclones: it times the
    # scan and reads the files-considered count from fclones' log. Optional and
    # hidden -- they are a wrapper->engine seam, not for direct use; run on
    # hand-built JSON without them and the summary just omits the scan line.
    p.add_argument("--scan-seconds", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--files-scanned", type=int, default=None, help=argparse.SUPPRESS)
    args = p.parse_args()

    # Authoritative apply-mode safety gate: the engine -- not just the shell
    # wrapper, which can be bypassed -- refuses to run where CLONE_NOFOLLOW_ANY
    # would be silently ignored and reopen the symlink race.
    if args.apply and not _supports_symlink_safe_clone(platform.mac_ver()[0]):
        print("error: --apply requires macOS 15+ for symlink-safe cloning "
              "(CLONE_NOFOLLOW_ANY, absent before macOS 15).", file=sys.stderr)
        return 1

    try:
        report = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"error: could not parse fclones JSON report: {e}", file=sys.stderr)
        return 1

    # report is Any off json.load; the casts pin the two slices we read to their
    # typed shapes (and brand the paths FullPath) at this one trusted boundary.
    groups = cast("list[_FclonesGroup]", report.get("groups", []))
    stats = cast("_FclonesStats", report.get("header", {}).get("stats", {}))

    # Two channels: the report a user keeps -> stdout, so `> plan.txt` captures it
    # (the dry-run plan and the summary always; per-file `cloned` lines in --apply
    # only under --verbose); progress and per-file skip lines -> stderr.
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    def emit(msg: str) -> None:
        print(msg)

    # Skips are tallied by reason and reported in the summary; a per-file line
    # goes to stderr only under --verbose, since on a broad run they are the bulk
    # of the output and mostly not actionable file-by-file.
    skips = _Skips(log, args.verbose)

    total_cloned = 0
    total_reclaimed_logical = 0
    total_reclaimed_allocated = 0
    total_already_shared = 0
    total_already_shared_logical = 0
    total_already_shared_allocated = 0
    for g in groups:
        files = g.get("files", [])
        if len(files) < 2:
            continue
        gr = dedupe_group(files, apply=args.apply, verbose=args.verbose, skips=skips, emit=emit)
        total_cloned += gr.cloned
        total_reclaimed_logical += gr.reclaimed_logical
        total_reclaimed_allocated += gr.reclaimed_allocated
        total_already_shared += gr.already_shared
        total_already_shared_logical += gr.already_shared_logical
        total_already_shared_allocated += gr.already_shared_allocated

    # Lead with the scan stats the wrapper supplied (files it considered, how long
    # the scan took). Omitted when the engine is run directly on hand-built JSON;
    # the duration can stand alone if the files count could not be read.
    if args.scan_seconds is not None:
        if args.files_scanned is not None:
            print(f"scanned: {args.files_scanned} files in {_human_duration(args.scan_seconds)}")
        else:
            print(f"scan completed in {_human_duration(args.scan_seconds)}")

    found = stats.get("redundant_file_size")
    if found is not None:
        print(f"duplicates found by fclones: {_human(found)} logical "
              f"in {stats.get('redundant_file_count', '?')} files")
    # Space earlier runs already saved: duplicates we found still sharing
    # storage from a prior clone, left untouched this run (see dedupe_group).
    if total_already_shared:
        print("already saved by earlier clones: "
              f"{_space_detail(total_already_shared_logical, total_already_shared_allocated)} "
              f"across {total_already_shared} files")
    verb = "reclaimed" if args.apply else "would reclaim"
    print(f"{verb}: {_space_detail(total_reclaimed_logical, total_reclaimed_allocated)} "
          f"across {total_cloned} files")

    # Skips, broken down by reason rather than streamed per-file (see _Skips).
    # Advise only on the permission case -- the one a user can fix, by granting
    # Full Disk Access -- since the rest (hard links, cloud-evicted, ...) are
    # expected and not actionable.
    if skips.total():
        width = len(str(max(skips.counts.values())))
        print(f"skipped: {skips.total()} files")
        for kind in SkipKind:
            n = skips.counts.get(kind, 0)
            if n:
                print(f"  {n:>{width}}  {_SKIP_LABEL[kind]}")
        if skips.counts.get(SkipKind.PERMISSION):
            print("  to include privacy-protected folders (Desktop, Documents, "
                  "Downloads, ...), run apfs-dedupe yourself from a terminal with "
                  "Full Disk Access; a scheduled run cannot reach them")
        if not args.verbose:
            print("  re-run with --verbose to list every skipped file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
