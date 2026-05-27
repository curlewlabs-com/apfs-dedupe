# Architecture

## Split of responsibilities

Finding duplicates well — pre-filtering by size, parallel hashing, correct
grouping at scale — is solved. [`fclones`](https://github.com/pkolaczk/fclones)
does it fast and is widely used, so `apfs-dedupe` shells out to it for detection
and consumes its JSON report. The value this project adds is entirely in the
**apply** step, where the mature tools have macOS-specific gaps.

## The apply, and why it differs from `fclones dedupe`

For each duplicate `dup` that should become a clone of the group's first file
`canonical`, the engine (`lib/apply.py`) opens a file descriptor for `dup`'s
parent directory and works relative to it — see
[§ Symlink-safe, fd-anchored apply](#4-symlink-safe-fd-anchored-apply) for why
every step is fd-relative rather than path-based:

```
dirfd = open(dirname(dup), O_NOFOLLOW_ANY | O_DIRECTORY)  # no symlink in any component
clonefileat(canonical -> dirfd:tmp, CLONE_NOFOLLOW_ANY)   # tmp = shared extents
compare(tmp, dup)                                         # current bytes still match
fcopyfile(open(dirfd:dup) -> open(dirfd:tmp), COPYFILE_METADATA)  # dup's own metadata
renameat(dirfd: tmp -> dup)                               # atomic replace
```

Three properties fall out of this ordering that `fclones dedupe` does not have on
macOS:

### 1. Windowless and crash-safe

`fclones` routes its reflink through a generic `safe_remove` wrapper that renames
the original aside *first*, then clones the canonical into the original's name,
then deletes the saved-aside copy. Between the rename and the clone the path has
no file — observable by a concurrent reader, and left that way across a crash in
that interval (the data is stranded under a temp name).

Cloning into a temp and then `rename(2)`-ing it over the duplicate inverts this:
the duplicate's path always resolves to either the old file or the new clone,
never nothing, and a crash before the rename leaves the original completely
untouched (only an orphaned `.apfsdedupe-*` temp file to sweep). This is strictly safer on both the
concurrent-reader and crash-recovery axes, and needs no exotic primitive — a plain
atomic rename suffices (no `renamex_np`/`RENAME_SWAP` required).

### 2. Metadata-complete, including ACLs

`clonefile(2)` gives the clone the **source's** metadata, but we want the
duplicate's own (owner, mode, times, and — the part the other tools drop — its
ACL). macOS `fcopyfile(3)` with `COPYFILE_METADATA` (`= COPYFILE_ACL | COPYFILE_STAT
| COPYFILE_XATTR`, no `COPYFILE_DATA`) replicates all of that between the open
file descriptors in one call without touching the data. That single call is what
closes the ACL gap.

### 3. Fail-safe

A file that cannot be modified — `uchg` immutable, a deny-write ACL, or plain
permission denied — makes `clonefileat`/`fcopyfile`/`renameat` error. The engine
catches per file, logs a warning, removes any temp, and moves on. It never clears
a flag or overrides an ACL to force a dedup.

### 4. Symlink-safe, fd-anchored apply

The apply runs as root over user-writable directories, so the duplicate's
parent directory is controlled by a potential attacker. `clonefile(2)` follows
symlinks in the *destination path*, including intermediate components: cloning
to `tmp/clone` where `tmp` is a symlink creates the file under the symlink's
target. A temp chosen by path is therefore not enough — even an unguessable
name in a freshly created `mkdtemp` directory, because a local user who owns the
parent can replace that directory with a symlink in the window between its
creation and the clone, redirecting a root clone (with bytes they also control,
since the "canonical" can be their file) into an arbitrary same-volume path such
as a LaunchDaemon plist. `/Users` and `/Library` share the data volume
`clonefile` is restricted to, so the same-volume limit is no barrier. `mkdtemp`
plus `CLONE_NOFOLLOW` stops a *pre-planted* symlink and a *final-component*
symlink, but not this intermediate-component race.

The engine closes it by anchoring every privileged step on a file descriptor
for the duplicate's parent directory. The parent fd is acquired with
`O_NOFOLLOW_ANY` (macOS 11+), which fails if *any* path component is a
symlink — not just `O_NOFOLLOW`, which guards only the final one and would let
an attacker who owns an ancestor swap an intermediate component to redirect the
open. (Firmlinks such as `/Users` are not symlinks, so they still resolve.) The
clone (`clonefileat`), the content re-verify and metadata copy (`openat` +
`fcopyfile`), the atomic swap (`renameat`), and cleanup (`unlinkat`) all resolve
names against that fd — the real directory inode — so swapping a path component
after the fd is open cannot redirect any of them either. The clone itself uses
`clonefileat` with `CLONE_NOFOLLOW_ANY`, which refuses a symlink in any
component of the source *or* destination path; that also closes the source
side, where the canonical is still referenced by absolute path. Without it, a
local user who owns an ancestor of the canonical could race in an intermediate
symlink to redirect the read onto an arbitrary same-volume file and have its
bytes cloned into a file they own. The temp name is unguessable as a final layer
of defense.

The fd-relative calls take a bare directory-relative component, while the clone
source and every diagnostic take a path that resolves on its own. The engine
brands these as distinct types (`Basename` vs `FullPath`) so `pyright --strict`
(run in CI) rejects passing — or logging — one where the other belongs, keeping
that safety-relevant distinction from silently drifting.

`CLONE_NOFOLLOW_ANY` is also the binding OS-version floor. Apple's
`<sys/clonefile.h>` first defines it in **macOS 15**; releases 11–14 have
`O_NOFOLLOW_ANY` but not it, and silently ignore the unknown flag bit (the clone
follows the symlink). So `--apply` requires macOS 15+ — enforced both by the
shell wrapper as a fast precheck and, authoritatively, by `lib/apply.py` itself
(`_supports_symlink_safe_clone`), since the engine is independently runnable as
root. Dry-run uses none of these primitives and so works on any macOS version
(the floor gates `--apply` only); the tool is macOS-only regardless, since it is
built on APFS `clonefile`.

## Why we re-verify content before clobbering

Linux `FIDEDUPERANGE` is the gold standard because the kernel re-compares the byte
ranges under lock before sharing them, so a dedup can never merge mismatched data.
macOS has no such ioctl; `clonefile` shares whatever the canonical holds at clone
time. If a file changed between fclones' scan and our apply (a TOCTOU), blindly
cloning would destroy the changed bytes. So in `--apply` mode we clone canonical
into a temp first — a frozen copy-on-write snapshot — then re-compare *that
snapshot* against `dup`'s current bytes immediately before the atomic swap
every time. Cloning first means a write to canonical after the snapshot cannot
change what we install; comparing right before the swap closes the scan-to-apply
window, which is seconds to minutes on a large run. The only irreducible residue
is a write to `dup` itself in the microseconds between the compare and the rename
— unavoidable on macOS without a kernel-verified dedupe ioctl, and negligible
beside the window we close. (File locking would not help: it is advisory, so a
non-cooperating writer ignores it.)

## Skipping files already cloned on an earlier run

A re-run finds the same duplicate groups as the first, but the duplicates are now
clones — so cloning them again would only churn inodes and reclaim nothing. The
engine detects this and leaves them alone, in `--apply` and dry-run alike.

The check walks each file's **full extent map** — `fcntl(F_LOG2PHYS_EXT)` from byte 0
to EOF — and trusts a match **only when the canonical and the duplicate have identical
maps on the same device**. Then every data extent sits at the same physical device
offset, which is *proof* the two reference the same blocks (a clone), never a
coincidence, because a physical block belongs to exactly one allocation; and any holes
line up. The compared identity carries the device id, since an offset is meaningful only
within its device: the wrapper's `--one-fs` already keeps a group on one volume, but the
engine runs on arbitrary `fclones` JSON, so it reconfirms rather than assumes.

A *partial* match is never enough, and the full-map walk is what rejects it: a clone
whose later range was CoW-broken by an identical-bytes rewrite still shares its first
extent and stays byte-identical (so `fclones` still groups it), yet its rewritten extent
now sits at a different device offset — so its map differs from the canonical's and it is
cloned normally, re-sharing the diverged range. The error is one-directional and safe:
the check only ever *fails to prove* sharing (a missed reclaim — the file is cloned,
never corrupted); it never asserts sharing that isn't there. A fragmented or sparse
clone, by contrast, has the *same* map as its source and is correctly recognized, so a
re-run reclaims nothing on it instead of re-cloning it and re-counting its size — which
is why re-running over an already-deduped tree is cheap whatever the file sizes.

"Unknown" — map nothing, clone normally — covers an empty file and any extent the kernel
will not map: a transparently compressed (decmpfs) file whose bytes live in an xattr
rather than the data fork (`ENOTSUP`), a SIP-protected file, `ERANGE`, etc. That
fail-safe is why the check is safe to run in a dry-run and on every macOS version: it is
`fcntl`-only — it never `read()`s, so it will not fault a dataless iCloud file down from
the cloud — opens read-only, and falls back to "clone normally" on any error.

The space the recognized clones represent is reported on its own line —
`already saved by earlier clones: <allocated> allocated (<logical> logical) across <n> files`
— so a run shows both what it reclaimed this time and what earlier runs already
saved, recomputed from the filesystem each scan rather than tracked in any state
file.

## Reclaim accounting

`fclones` reports logical duplicate bytes (`st_size`), but logical bytes are not
always the same as reclaimable disk blocks: sparse files and APFS-compressed files
can be much larger logically than the storage they occupy. The apply engine
therefore tracks both. Per-file and summary output lead with the duplicate's
allocated bytes (`st_blocks * 512`) and keep logical bytes as secondary context:
`reclaimed: <allocated> allocated (<logical> logical) across <n> files`.

For this run's fresh clones, allocated bytes come from the duplicate's stat record
before replacement, which is the closest estimate of the blocks the duplicate inode
stops owning. For duplicates already shared by an earlier clone, there is no
pre-dedupe state file; the already-saved allocated figure is recomputed from the
current clone's reported allocation and is best-effort, while the logical count
still reflects the duplicate byte payload represented by those paths.

## Reclaimed space and snapshots

Freeing a duplicate's blocks only returns them to the container's free space once
nothing else references them. An APFS **snapshot** — notably Time Machine's hourly
*local* snapshots — is copy-on-write and pins the blocks it captured, so blocks freed
by a dedup run that a snapshot still references are retained for the snapshot rather
than returned. The allocated reclaim the tool reports is the best filesystem estimate
of freed duplicate blocks, but `df` lags until the snapshots holding the pre-dedup
state expire (local Time Machine snapshots auto-expire within ~24h, sooner under disk
pressure) or are thinned (`tmutil thinlocalsnapshots`). New snapshots capture the
deduped state, so they do not re-pin the freed blocks. The tool never touches snapshots
itself — deleting them is a data-retention decision left to the operator; the wrapper
only prints a reminder after `--apply` when local snapshots are present. See the
README ("Why didn't free space change? Snapshots") for the operator-facing steps.

## Defaults, and why

- **Dry-run unless `--apply`.** This modifies files, often as root across all of
  `/Users`. The default must show its work and change nothing.
- **`fclones group --no-ignore --hidden`.** By default fclones honours `.gitignore`
  and skips dotfiles — which excludes exactly the build caches and `build/` trees
  where duplicates concentrate. Without these flags a sweep looks like it ran but
  reclaims almost nothing.
- **`--one-fs`.** Never group across volumes; `cp -c`/`clonefile` would otherwise
  silently fall back to a full copy with no error and no saving.
- **Excludes cloud-backed roots by default.** iCloud Drive (`~/Library/Mobile
  Documents`), third-party File Providers (`~/Library/CloudStorage`), and Photos
  libraries (`*.photoslibrary`) can hold *dataless* files — evicted to the cloud,
  reporting their full size but holding no local bytes. `fclones` hashes by
  reading, and reading a dataless file faults it back down from the cloud, so
  scanning these roots could silently re-download gigabytes of deliberately-evicted
  content — in a dry-run too, since the scan is `fclones`'. They are kept out of
  the scan unless `--include-cloud` is passed, and `lib/apply.py` additionally
  skips any dataless file that still reaches it (a cached or standalone run). The
  download happens at scan time, so a scan-level exclude — not merely an apply-time
  skip — is what actually prevents it.
- **Excludes protected and machine-managed `~/Library` data by default.** Two
  classes, same rationale — TCC-protected *and* poor dedup targets, re-included
  with `--include-app-data`. App-private stores (Mail, Messages, Safari, the
  per-app sandbox `Containers`/`Group Containers`) are live databases that churn,
  holding little duplicate data and much that is sensitive. OS-managed data
  (Spotlight's index, `Metadata/CoreSpotlight`; the on-device intelligence and
  proactive-suggestion stores `IntelligencePlatform`, `Biome`, `Trial`,
  `DuetExpertCenter`, `Suggestions`; media-services and Focus state
  `AppleMediaServices`, `DoNotDisturb`; and sandboxed `Daemon Containers`) is
  machine-generated, constantly rewritten, and worth nothing deduped. Scanning either makes macOS prompt on an interactive run
  or deny access (`Operation not permitted`) on the scheduled job; the denial lands
  at `fclones`' scan (a denied folder never enters the report), so the *wrapper* —
  not the engine — folds those scan-time denials into one counted note rather than
  a per-folder warning (see "Skips are summarized by reason" below). The list is
  best-effort noise reduction for the largest such trees, not exhaustive — a missed
  one only costs a counted skip, never safety.
- **Excludes the Trash always.** `~/.Trash` and the volume `.Trashes` hold files
  pending deletion — cloning shares storage about to be freed anyway — and are
  TCC-protected too. There is no case for deduping them, so this exclude has no
  opt-in.
- **Full Disk Access is the boundary for the user-document folders.** Desktop,
  Documents, and Downloads are TCC-protected, so neither the excludes above nor
  `--include-app-data` reaches them; only **Full Disk Access** does, and the tool
  cannot grant TCC itself. An interactive run inherits the TCC access of the
  terminal it runs from, so granting Terminal/iTerm Full Disk Access lets a
  hands-on `apfs-dedupe` reach those folders. The scheduled `LaunchDaemon` cannot:
  it runs via `/bin/sh`, so the only binary to grant would be the system shell —
  Full Disk Access for *every* shell script, which the tool will not recommend —
  and a background job receives no TCC prompt regardless. So the division of labor
  is: the daemon covers everything outside the TCC-protected user folders, and a
  periodic interactive run from an FDA-granted terminal covers those. (Granting a
  dedicated, signed helper binary Full Disk Access would let the daemon reach them
  too, but signing/notarizing one is out of scope; see "Out of scope".)
- **Skips are summarized by reason, not streamed.** A broad run leaves many files
  untouched — privacy-protected paths above all — and a per-item warning for each
  buries the result. Denials occur at two layers, and each is folded into a count.
  At **scan** time, a folder the run cannot read (a TCC-protected user folder
  without Full Disk Access) fails in `fclones` at `readdir`, so its files never
  reach the engine; the wrapper captures `fclones`' stderr and collapses those
  per-folder permission denials (matched by the OS `strerror` text, stable across
  `fclones` versions) into one counted note, passing `fclones`' own progress and
  any non-permission warning straight through. At **apply/examine** time, the
  engine classifies each skip on a file that *did* enter the report (permission,
  symlinked component, hard-linked, changed-since-scan, cloud-evicted, unreadable)
  and prints a counted breakdown at the end of the summary. Both advise on the one
  reason a user can act on: granting Full Disk Access. `--verbose` restores the raw
  `fclones` lines and the engine's per-file skip line (with the full path) on
  stderr for debugging.
- **An `--apply` run prints a summary, not a line per clone.** A nightly `/Users`
  sweep can clone tens of thousands of files, and one `cloned …` line each would
  bury the log. So `--apply` emits only the run summary by default, and `--verbose`
  restores the per-file `cloned` audit line on stdout. A dry-run is the exception —
  its per-file plan is the whole deliverable, so it always prints. The summary
  leads with the files scanned and the scan duration (the wrapper times the
  `fclones` scan and passes both into the engine via `--scan-seconds` /
  `--files-scanned`), then duplicates found, space already saved by earlier clones,
  space reclaimed, and the skip breakdown. The files-scanned count is read
  best-effort from `fclones`' own scan log and is simply dropped if a future
  `fclones` rewords that line — informational only, unlike the permission fold
  above, which matches the stable `strerror` text because getting it wrong would
  hide real denials.
- **`--min 1M`, with a `--git` preset that drops to `1`.** APFS allocates ordinary
  file data in 4 KiB blocks and does not inline regular-file data, so cloning any
  ordinary allocated duplicate frees at least one whole block (4 KiB) regardless
  of its logical size — there is **no size threshold below which dedup is futile**
  for those files (a 1-byte and a 4096-byte allocated file each save exactly one
  block; `4096`/`4097` are not principled floors). Sparse or compressed files can
  occupy fewer blocks than their logical size, which is why the report separates
  allocated and logical bytes. The 1 MiB default is therefore not about *where*
  savings are — it bounds *work* (files scanned and
  cloned) for the common case, where chasing the long tail of tiny files costs more
  inode churn than it returns. A git- or CI-heavy machine inverts that: its savings
  concentrate in a large number of small, content-addressed files (git loose
  objects, build caches) that 1 MiB skips entirely. `--git` lowers `--min` to `1`
  (every non-empty file; 0-byte files save nothing and are excluded) to capture
  them, trading a much larger scan-and-clone count for that reclaim. An explicit
  `--min` always overrides the preset.
- **Whole-`/Users`, across users, in one pass — the intended default.** It is
  safe because the apply restores each *replaced* file's own owner/mode/ACL and
  never modifies the canonical, so every file keeps its identity and only physical
  extents are shared. Copy-on-write means a later write diverges rather than
  corrupting a twin; grouped files are byte-identical already, so shared storage
  exposes nothing new (access stays gated by each file's own permissions); and it
  does not matter which file is chosen as canonical, since each replaced file's
  metadata is restored from itself. `clonefile` is same-volume only and `/Users`
  is one APFS volume, so there is no cross-volume hazard either.
- **Refuses whole-machine roots.** `--scope /` and the data-volume root
  (`/System/Volumes/Data`) are rejected unless `--allow-root`, to keep the tool
  aimed at user data. The system volume is a sealed read-only snapshot, so Apple's
  system files cannot be modified in any case — they would error and be skipped.

## Out of scope (for now)

### Non-APFS / Linux

This is APFS-only by construction. The portable equivalent on Linux is reflink
(`FICLONE`) on btrfs/XFS/bcachefs/ZFS — and `FIDEDUPERANGE` there is both in-place
(no window) and kernel-verified, so a Linux build would look quite different. ext4
has no copy-on-write primitive at all.

### Full Disk Access for the scheduled daemon

The `LaunchDaemon` runs via `/bin/sh`, and TCC attributes a grant to the
executable that runs — the system shell here, not the script — so there is no
narrow binary to grant Full Disk Access; granting `/bin/sh` would extend it to
every shell script on the machine. The clean fix is a dedicated, code-signed
helper binary that performs the file I/O itself and is granted Full Disk Access
specifically, but signing and notarizing a distributable binary (a Developer ID
account, and a stable Designated Requirement so the grant survives updates) is a
disproportionate amount of machinery for the payoff. Until then the daemon
deliberately does not reach the TCC-protected user folders; a periodic
interactive run from a Full-Disk-Access terminal covers them (see "Full Disk
Access is the boundary for the user-document folders" above).
