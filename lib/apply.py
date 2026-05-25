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

# Output sinks (see dedupe_group/main): log -> stderr (progress, skip warnings),
# emit -> stdout (the report a user redirects to a file).
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
# Two files on the same device, each stored as a single extent at the same
# offset, share all their storage (a prior clone) -- a physical block belongs to
# one allocation, so an equal offset is proof of sharing, never a coincidence (a
# device offset is meaningful only within its device, so _sole_extent carries the
# device id alongside it). struct log2phys is
# 4-byte *packed* (legacy 32-bit ABI): flags@0, contigbytes@4, devoffset@12 --
# 20 bytes ("=Iqq"); the naturally aligned "Iqq" is 24 bytes and misreads
# devoffset, returning ERANGE. See _sole_extent.
F_LOG2PHYS_EXT = 65
_LOG2PHYS_FMT = "=Iqq"
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


def _sole_extent(fd: int) -> Optional[tuple[int, int, int]]:
    """(device id, physical device offset, length) of the file's storage IFF the
    whole file is a single contiguous extent; None otherwise.

    Returning a value only for a *sole, whole-file* extent is what lets an equal
    result between two files prove they share ALL their storage -- not merely
    byte 0. A physical block belongs to exactly one allocation, so an equal
    device offset spanning the whole file is sharing, never coincidence. The
    weaker "first extents match" is NOT sufficient: a clone whose later range was
    CoW-broken by an identical-bytes rewrite still shares byte 0 and stays
    byte-identical (so fclones groups it), yet its later extents are private --
    skipping it would reclaim nothing forever and over-count its full size as
    saved. Such a file has more than one extent, so its first run is shorter than
    its size and this returns None; it is then treated as not-provably-shared and
    cloned normally. (A fully-cloned but fragmented file is likewise re-cloned --
    a harmless missed optimization, the pre-existing behavior.)

    The device id leads the tuple because a device offset is meaningful only
    within its device: two independent files on different volumes can share an
    offset+length by coincidence, so only an equal device makes the offset proof
    of sharing. The wrapper's `--one-fs` already keeps each group on one volume,
    but the engine runs on arbitrary fclones JSON, so the identity carries the
    device rather than assuming it.

    None also covers an empty file, a leading hole, and a transparently
    compressed (decmpfs) file whose bytes live in an xattr, not the data fork
    (ENOTSUP). fcntl-only: it maps extents and never read()s, so it will not
    fault a dataless iCloud file down from the cloud (that download hazard, on
    the clone/verify path)."""
    st = os.fstat(fd)
    if st.st_size == 0:
        return None
    # IN: contigbytes = bytes to map; devoffset = the file offset to map from (0).
    buf = struct.pack(_LOG2PHYS_FMT, 0, st.st_size, 0)
    try:
        res = fcntl.fcntl(fd, F_LOG2PHYS_EXT, buf)
    except OSError:
        return None   # ENOTSUP (compressed / SIP-protected), ERANGE, ... -> unknown
    # OUT: contigbytes = contiguous run length; devoffset = physical device offset.
    _flags, run, devoff = struct.unpack(_LOG2PHYS_FMT, res)
    # devoffset -1 == a hole at byte 0; run < size == a second extent follows
    # (fragmented or partially CoW-broken), so this is not a sole extent.
    if devoff < 0 or run < st.st_size:
        return None
    return (st.st_dev, devoff, run)


def _sole_extent_of(path: FullPath) -> Optional[tuple[int, int, int]]:
    """_sole_extent for a path: open it read-only -- refusing a final-component
    symlink -- and probe, returning None on any open failure (gone, no
    permission, a symlink raced in after the lstat). O_NOFOLLOW (not
    O_NOFOLLOW_ANY) keeps this probe working on every macOS version, as the
    dry-run must: it is read-only and fails safe to "clone normally", so it does
    not need the apply path's stronger any-component guard."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return _sole_extent(fd)
    finally:
        os.close(fd)


def _is_dataless(st_flags: int) -> bool:
    """True if st_flags marks a cloud-evicted (dataless) file -- one whose bytes
    live in the cloud and would be faulted back down by any read(). Reading or
    cloning it would re-download deliberately-evicted content, so callers skip
    it. Detected from the flag alone (a stat field), never by touching data."""
    return bool(st_flags & SF_DATALESS)


def _skip(log: Log, path: FullPath, reason: str) -> None:
    """Log that `path` was skipped, and why. `path` is typed FullPath, not
    Basename, so the dirfd-relative component the syscalls use can never be
    logged in place of the file's resolvable name -- the ambiguous-basename
    regression this branding exists to prevent. Skips are diagnostics: they go
    to stderr (log), never into the stdout report."""
    log(f"skip {path}: {reason}")


def _clone_over(canonical: FullPath, dirfd: int, dup_path: FullPath,
                log: Log) -> bool:
    """Replace dup_path with a clone of canonical. Every privileged step is
    anchored on dirfd -- clonefileat/openat/renameat/unlinkat, never a
    re-resolved path -- so a local user who owns the parent directory cannot
    swap a path component to redirect the clone, the metadata copy, or the
    rename into somewhere they do not already control. The fd-relative syscalls
    address the duplicate by its basename (resolved against dirfd); logs use the
    full dup_path so a failure on a whole-/Users run names the actual file, not
    just an ambiguous basename -- a distinction the Basename/FullPath types now
    enforce. Returns True iff the duplicate was cloned."""
    dup_name = Basename(os.path.basename(dup_path))
    tmp_name = Basename(".apfsdedupe-" + secrets.token_hex(16))
    try:
        # Clone first: tmp is a frozen copy-on-write snapshot of canonical's
        # *current* bytes, so a write to canonical after this point cannot
        # change what we install.
        _clonefileat(canonical, dirfd, tmp_name)
    except OSError as e:
        _skip(log, dup_path, str(e))
        return False
    try:
        with _openfd(dup_name, os.O_RDONLY, dirfd) as src_fd, \
             _openfd(tmp_name, os.O_RDWR, dirfd) as dst_fd:
            # Re-compare the exact bytes we are about to install (tmp) against
            # the duplicate's *current* bytes, right before the swap -- the one
            # check that closes the scan-to-apply window. Never act on a match
            # that went stale.
            if not _same_content_fd(dst_fd, src_fd):
                _skip(log, dup_path, f"changed since scan, no longer identical to {canonical}")
                return False
            _fcopy_metadata(src_fd, dst_fd)   # restore the duplicate's own metadata (incl. ACL)
        os.rename(tmp_name, dup_name, src_dir_fd=dirfd, dst_dir_fd=dirfd)  # atomic
        return True
    except OSError as e:
        # Immutable, deny-ACL, permission, cross-device, ... -- fail safe.
        _skip(log, dup_path, str(e))
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


def dedupe_group(files: list[FullPath], apply: bool,
                 log: Log, emit: Emit) -> _GroupResult:
    """Clone files[1:] from files[0]. Returns a _GroupResult.

    Two output channels: emit() carries the report a user wants to keep -- the
    per-file plan (dry-run) or the per-file actions (apply) -- and goes to
    stdout, so `> plan.txt` captures it. log() carries progress and per-file
    skip warnings and goes to stderr.

    A duplicate stored as the same single whole-file extent as the canonical was
    cloned on an earlier run; it is counted in already_shared (and its size in
    already_shared_bytes) and left untouched -- in apply and dry-run alike, so a
    re-run reclaims nothing new and the dry-run projection of an already-deduped
    tree shows ~0 reclaimable -- instead of pointlessly rebuilding the same
    sharing and churning the inode."""
    canonical = files[0]
    try:
        cst = os.lstat(canonical)
    except OSError as e:
        _skip(log, canonical, f"cannot stat (skipping group): {e}")
        return _GroupResult(0, 0, 0, 0, 0, 0)
    if not stat.S_ISREG(cst.st_mode):
        return _GroupResult(0, 0, 0, 0, 0, 0)
    # A dataless (cloud-evicted) canonical holds no local bytes; cloning from it
    # would fault the whole file down from the cloud. Skip the group rather than
    # re-download deliberately-evicted content -- the wrapper already keeps the
    # cloud roots out of the scan, this guards a cached/standalone run.
    if _is_dataless(cst.st_flags):
        _skip(log, canonical, "dataless (cloud-evicted); skipping group to avoid forcing a download")
        return _GroupResult(0, 0, 0, 0, 0, 0)

    # The canonical's storage as a single whole-file extent, computed once (None
    # if it is fragmented, compressed, empty, or unreadable -- which disables the
    # check, cloning every duplicate exactly as before). A duplicate stored as
    # the same sole extent is already a full clone; see _sole_extent for why only
    # a sole extent proves whole-file sharing, not merely a shared byte 0.
    canonical_ext = _sole_extent_of(canonical)

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
            _skip(log, dup, str(e))
            continue
        if not stat.S_ISREG(dst.st_mode):
            continue
        # A dataless (cloud-evicted) duplicate would be faulted down from the
        # cloud by the content re-verify (which read()s it) or the clone. Skip it
        # rather than re-download evicted content (backstop to the scan exclude).
        if _is_dataless(dst.st_flags):
            _skip(log, dup, "dataless (cloud-evicted); skipped to avoid forcing a download")
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
            _skip(log, dup, f"hard-linked ({dst.st_nlink} links); cloning would "
                  f"break the link and reclaim nothing")
            continue
        # Already cloned on an earlier run? Stored as the same sole whole-file
        # extent means dup and canonical share ALL their storage, so re-cloning
        # would only churn the inode and reclaim nothing. Count it as
        # already-saved and skip -- in dry-run too, so the projection matches a
        # real re-run.
        if canonical_ext is not None and _sole_extent_of(dup) == canonical_ext:
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
            _skip(log, dup, f"cannot open parent dir: {e}")
            continue
        try:
            if _clone_over(canonical, dirfd, dup, log):
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

    # Two channels: the report a user keeps (plan/actions + summary) -> stdout,
    # so `> plan.txt` captures it; progress and warnings -> stderr.
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    def emit(msg: str) -> None:
        print(msg)

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
        gr = dedupe_group(files, apply=args.apply, log=log, emit=emit)
        total_cloned += gr.cloned
        total_reclaimed_logical += gr.reclaimed_logical
        total_reclaimed_allocated += gr.reclaimed_allocated
        total_already_shared += gr.already_shared
        total_already_shared_logical += gr.already_shared_logical
        total_already_shared_allocated += gr.already_shared_allocated

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
