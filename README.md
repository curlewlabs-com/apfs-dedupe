# apfs-dedupe

[![CI](https://github.com/curlewlabs-com/apfs-dedupe/actions/workflows/ci.yml/badge.svg)](https://github.com/curlewlabs-com/apfs-dedupe/actions/workflows/ci.yml)

Reclaim disk on macOS by replacing byte-identical duplicate files with **APFS
clones** — independent files that share storage copy-on-write until one is modified.

Finding duplicates is a solved problem — [`fclones`](https://github.com/pkolaczk/fclones)
does it well, so this tool delegates detection to it. The hard part is the **apply**,
and that's the point of `apfs-dedupe`: it is **crash-safe** (no instant where the file
is missing, even if the process dies mid-swap), **re-verifies the bytes** against a
frozen clone immediately before replacing, stays **symlink- and TOCTOU-safe** when run
as root over user-writable directories, and preserves a file's metadata in full —
**including its ACLs**, which other reflink dedupers drop.

**Dry-run is the default. It shows what it would reclaim and changes nothing — you
opt into changes with `--apply`.**

## Quick start

```sh
brew install fclones                      # the one dependency
git clone https://github.com/curlewlabs-com/apfs-dedupe.git
cd apfs-dedupe
./apfs-dedupe.sh --scope ~/Projects       # dry-run: shows what it would reclaim
```

```
Scanning /Users/you/Projects (files >= 1M) with fclones...
fclones: Found 3 (7.3 MB) redundant files

DRY RUN -- nothing changed. Re-run with --apply to reclaim the space below.

would clone /Users/you/Projects/webapp/node_modules/ui-kit/bundle.js -> /Users/you/Projects/webapp-backup/node_modules/ui-kit/bundle.js  (4.0 MiB allocated (4.0 MiB logical))
would clone /Users/you/Projects/webapp/assets.tar -> /Users/you/Projects/webapp-backup/assets.tar  (2.0 MiB allocated (2.0 MiB logical))
would clone /Users/you/Projects/webapp/icon.png -> /Users/you/Projects/webapp-backup/icon.png  (1.0 MiB allocated (1.0 MiB logical))
scanned: 412 files in 2s
would reclaim: 7.0 MiB allocated (7.0 MiB logical) across 3 files
```

Like the plan? Add `--apply` to do it (requires macOS 15+):

```sh
./apfs-dedupe.sh --apply --scope ~/Projects
```

To sweep every account on the machine, run it as root over the default `/Users`
scope: `sudo ./apfs-dedupe.sh` (still a dry-run), then `sudo ./apfs-dedupe.sh --apply`.

## Why not just `fclones dedupe`?

[`fclones`](https://github.com/pkolaczk/fclones) can do the dedupe apply too
(`fclones dedupe`); on macOS that path has two gaps:

- **A vanishing-file window.** `fclones dedupe` renames the original aside, *then*
  clones over it — so the path briefly has no file, and stays that way if the
  process dies between the two steps.
- **Dropped ACLs.** `clonefile` copies the *source's* metadata; dedupers restore
  POSIX bits / owner / times / xattrs but lose the file's ACLs.

`apfs-dedupe` does the apply itself, correctly — every step relative to a file
descriptor for the duplicate's parent directory, so no path is re-resolved
mid-apply (see [Safety](#safety) for why):

```
dirfd = open(dirname(dup), O_NOFOLLOW_ANY)        # no symlink in any component
clonefileat(canonical -> dirfd:tmp)               # shared extents; CLONE_NOFOLLOW_ANY
compare(tmp, dup)                                  # current bytes still match
fcopyfile(dup_fd -> tmp_fd, COPYFILE_METADATA)    # dup's mode/owner/times/xattrs/ACLs
renameat(dirfd: tmp -> dup)                        # atomic: dup's path is never absent
```

That `fcopyfile(COPYFILE_METADATA)` is what carries ACLs across; the
temp-then-atomic-rename removes the window and is crash-safe — a crash leaves the
original untouched.

## Deduping all of /Users, across every user

Running as root over the whole machine is the intended use, and it is safe. The
apply restores each *replaced* file's own owner, group, mode, and ACL — the
canonical file is never modified — so every file keeps its identity and the only
thing that ever crosses a user boundary is the shared bytes, which were identical
to begin with. Because the clone is copy-on-write, a later write by any user
diverges that file instead of touching anyone else's, and access stays gated by
each file's own permissions (sharing storage grants no new read access). It does
not even matter which file in a group is chosen as the canonical. `clonefile` is
same-volume only, and all of `/Users` lives on one APFS volume.

`/` and the writable data-volume root are refused unless you pass `--allow-root`,
and the macOS system volume is a sealed read-only snapshot — so Apple's system
files can't be modified in any case (they would simply error and be skipped).

## Safety

- **Dry-run by default.** Nothing changes until you pass `--apply`.
- **Content re-verify against a frozen clone, right before the swap.** `clonefile`
  is not content-verified by the kernel (unlike Linux `FIDEDUPERANGE`), so we clone
  first — a copy-on-write snapshot — then re-compare *that* against the duplicate
  immediately before replacing. It never installs bytes that went stale since the
  scan (seconds to minutes earlier on a large run), a concurrent write to the
  original can't affect the result, and this check is always on.
- **Windowless, crash-safe** apply (above).
- **Symlink-safe, fd-anchored apply.** Running as root over user-writable
  directories, the clone, content re-verify, metadata copy, and atomic swap are
  all done relative to a file descriptor for the duplicate's parent — acquired
  with `O_NOFOLLOW_ANY` (no symlink in any path component) and then used via
  `clonefileat`/`openat`/`renameat` — so a local user can't swap a path
  component to redirect a root clone into an arbitrary location.
- **Fails safe.** Immutable (`uchg`), deny-ACL, or permission-denied files are
  skipped with a warning, never forced.
- **`--one-fs`** so it never crosses volumes into a silent full copy.
- **Never breaks hard links.**
- **Reports allocated space separately from logical bytes.** Sparse and
  APFS-compressed files can be much larger logically than the blocks they occupy,
  so summaries lead with the estimated allocated bytes reclaimed and keep the
  logical duplicate byte count in parentheses.
- **Skips files already cloned on a re-run.** A second sweep compares physical
  extents to detect duplicates that already share storage, leaves them untouched
  instead of rebuilding the same clone, and reports the space earlier runs already
  saved (`already saved by earlier clones: …`). Re-running is cheap and changes
  nothing once a tree is deduped — see
  [docs/architecture.md](docs/architecture.md).
- **Won't re-download cloud files.** iCloud Drive, third-party File Provider, and
  Photos library roots are excluded from the scan by default: reading an evicted
  (dataless) file would fault it back down from the cloud — the opposite of
  reclaiming space. Pass `--include-cloud` to scan them when they are fully local.
- **Stays out of app-private and machine-managed data.** App-private stores (Mail,
  Messages, Safari, per-app sandbox containers) and OS-managed `~/Library` trees
  (the Spotlight index, on-device intelligence, daemon containers) are excluded by
  default — TCC-protected and poor dedup targets; the **Trash** is excluded
  unconditionally. Pass `--include-app-data` to include the `~/Library` set. The
  TCC-protected user folders (Desktop/Documents/Downloads) stay in scope but are
  reachable only with **Full Disk Access** — grant it to your terminal for an
  interactive run; a scheduled run via `/bin/sh` cannot get it (see [Usage](#usage)).

## Requirements

Requires macOS (APFS), `fclones`, and `python3` (Xcode Command Line Tools —
already present on most dev machines). `--apply` additionally requires **macOS
15+**: it uses `CLONE_NOFOLLOW_ANY` for symlink-safe path resolution, which
Apple's headers first define in macOS 15, and refuses to run on older systems.
Dry-run uses none of that and works on any macOS version.

## Usage

```
apfs-dedupe.sh [--apply] [--scope PATH] [--min SIZE] [--exclude GLOB] [--verbose]
```

- `--scope PATH` — narrow the scan if you want (default `/Users`, which covers
  every user; deduping across users is safe — see above).
- `--min SIZE` — ignore files smaller than this (default `1M`). A bare number is
  **bytes** (`--min 100000` ≈ 98 KiB); add a suffix for units — `500K`, `1M`,
  `2G` (decimal) or `KiB`/`MiB`/`GiB` (binary). The default bounds *work*, not
  savings: cloning any ordinary allocated duplicate frees at least one 4 KiB block
  whatever its size, so on git-/CI-heavy trees — where savings hide in many small
  files — use a low `--min` or the `--git` preset below.
- `--git` — preset for git-/CI-heavy machines: lowers `--min` to `1` so the many
  small content-addressed files (git objects, build caches) where savings
  concentrate there get deduped too. Scans and clones far more files; an explicit
  `--min` overrides it.
- `--exclude GLOB` — skip paths matching `GLOB`, e.g. `--exclude '*.iso'`; quote it
  so your shell doesn't expand it first. Repeatable.
- `--allow-root` — permit scanning `/` or the data-volume root (refused by default;
  the tool is meant for `/Users`).
- `--include-cloud` — also scan cloud-backed roots (iCloud Drive,
  `~/Library/CloudStorage`, Photos libraries) that are excluded by default.
  **Warning:** reading an evicted file re-downloads it; only safe when those roots
  are fully downloaded locally.
- `--include-app-data` — also scan the app-private and OS-managed `~/Library` data
  excluded by default (Mail, Messages, Safari, per-app sandbox containers; the
  Spotlight index, on-device intelligence and daemon-container stores) — all
  TCC-protected and poor dedup targets. This flag does **not** grant access: the
  TCC-protected user folders that stay in scope (Desktop, Documents, Downloads) are
  reachable only with **Full Disk Access**, which can't be granted from a CLI.
  Grant it to your terminal app in System Settings → Privacy & Security for an
  interactive run. A scheduled LaunchAgent/LaunchDaemon runs via `/bin/sh`, so the
  only thing to grant would be the system shell — Full Disk Access for *every*
  shell script, which this tool won't recommend; the daemon therefore stays out of
  those folders, and a periodic interactive run covers them.
- `--verbose` — print a line per cloned file (in `--apply`) and per skipped file.
  By default an `--apply` run prints just the summary and skips are summarized by
  reason (see [Output](#output)); `--verbose` restores the per-file `cloned` line on
  stdout, adds a per-file skip line on stderr, and surfaces the raw `fclones`
  diagnostics for the folders it couldn't read. A dry-run always prints its full plan.

### Output

The dry-run plan **and** the savings summary go to **stdout**; progress
(`Scanning…`, fclones's own logs) goes to **stderr**. The summary leads with the
files scanned and how long the scan took, then the reclaim; files left untouched
are **summarized by reason** at the end, not streamed one per line — and folders an
un-granted run couldn't read are folded into a single counted note on stderr with
Full Disk Access advice, rather than one line each. An `--apply` run prints just
that summary by default — the per-file `cloned` lines are **opt-in** under
`--verbose`, since a nightly `/Users` sweep can clone tens of thousands of files
and one line each would bury the log. Reclaim figures lead with estimated allocated
bytes and show logical duplicate bytes in parentheses, because sparse or compressed
files can occupy fewer blocks than their logical size. So a plain redirect saves
just the report while progress still shows on screen:

```sh
./apfs-dedupe.sh > plan.txt                      # dry-run plan + summary saved; progress on screen
./apfs-dedupe.sh --apply --verbose > clones.txt   # full per-file apply record on disk
./apfs-dedupe.sh --verbose ...                    # also list every skipped file (else summarized)
```

## Why didn't free space change? Snapshots

If `--apply` reports gigabytes of allocated space reclaimed but `df` shows little
or no change, the space is almost certainly pinned by **APFS snapshots** — most
often Time Machine's **local** snapshots, which macOS takes hourly. Snapshots are
copy-on-write: one taken while a duplicate still held its own blocks keeps
referencing those blocks, so the blocks dedup frees stay attached to the snapshot
instead of returning to free space. The allocated figure is the best filesystem
estimate of block reclaim; the *realized* free space lags until the snapshots
holding the pre-dedup state are gone.

Local Time Machine snapshots expire on their own (~24 hours, sooner under disk
pressure), so the space comes back by itself. To reclaim it now:

```sh
# purge up to ~20 GiB of snapshot-pinned space (bytes, then urgency 1-4); raise as needed
sudo tmutil thinlocalsnapshots /System/Volumes/Data 21474836480 4
df -h /System/Volumes/Data        # confirm
```

This removes only **local** snapshots — your actual Time Machine backups on the
external/network destination are untouched; the only cost is local hourly rollback
points. New snapshots capture the *deduped* state, so they don't re-pin the freed
blocks: once the old snapshots clear, the reclaim sticks. After an `--apply`, the
tool prints this reminder when local snapshots are present (a note only — it never
deletes snapshots; that's your call).

## Schedule a daily run

`install-daily.sh` sets up a scheduled run so duplicates created since the last
run are reclaimed automatically.

**Per-user (default)** — a **LaunchAgent** that runs as you, every day at 02:00,
over your home directory:

```sh
./install-daily.sh                  # scope defaults to $HOME
./install-daily.sh --scope ~/code   # or a narrower scope
./install-daily.sh --min 1M         # override the default --git / --min 1 preset
./install-daily.sh --print          # preview what would be installed; install nothing
./install-daily.sh --uninstall      # remove it
```

It runs **as you, no root**, with the same safe defaults as the CLI — cloud-backed
roots, app-private and OS-managed `~/Library` data, and the Trash excluded, and
`--git` (`--min 1`) so small duplicates are caught too. It runs via `/bin/sh`, so
it cannot get Full Disk Access and does not reach Desktop/Documents/Downloads — run
the CLI by hand from a Full-Disk-Access terminal for those. The first run does the
real work; later runs are cheap, because already-cloned files are detected and
skipped. Output is appended to `~/Library/Logs/apfs-dedupe.log`, which the daily
run keeps size-capped — gzipping older logs beside it — so it can't grow without
bound (it self-rotates because a `newsyslog` rule would need root, which this
install doesn't use).

A LaunchAgent runs only while you're logged in, so if the Mac is asleep at 02:00
the run happens at the next wake.

**All users (`--system`)** — a root **LaunchDaemon** that runs every day at 02:00
over all of `/Users`, covering every account:

```sh
sudo ./install-daily.sh --system                 # scope defaults to /Users, --min 1M
sudo ./install-daily.sh --system --min 1         # scan every non-empty file
./install-daily.sh --system --print              # preview what would be installed; install nothing
sudo ./install-daily.sh --system --uninstall     # remove it
```

It runs **as root** whether or not anyone is logged in, writes
`/Library/LaunchDaemons/com.curlewlabs.apfs-dedupe.system.plist`, and appends to
`/Library/Logs/apfs-dedupe.log` (created `root:wheel` `0600`, because it can name
paths under every user's home). A `newsyslog` rule
(`/etc/newsyslog.d/com.curlewlabs.apfs-dedupe.conf`, removed by `--uninstall`) keeps
that log size-capped and gzipped — macOS's own rotator, with archives kept
`root:wheel 0600` like the log itself. The default system floor is `--min 1M` for
whole-machine recurring runs; pass `--min 1` if you want the daemon to scan every
non-empty file daily.

Known limitation: `--system` stores this checkout's script path and the installer
shell's `fclones`/`python3` search path in a root daemon. That is appropriate for a
self-managed personal machine or trusted CI host; a future hardening pass can
require root-owned, non-group/world-writable tool paths before aiming this at
adversarial multi-user machines.

## What it does not do (yet)

- **Non-APFS / Linux.** APFS `clonefile` only.

## Development

```sh
sh test/test.sh                              # integration tests (macOS 15+, real clonefile, fclones)
npx pyright@1.1.409                          # strict type check of lib/apply.py (CI's exact pin)
shellcheck apfs-dedupe.sh install-daily.sh test/test.sh   # CI pins shellcheck 0.11.0
```

`lib/apply.py` brands path strings as `FullPath` vs `Basename` (distinct
`NewType`s), so a directory-relative component can't be passed — or logged — where
a resolvable path belongs; `pyright` (strict) enforces it. All three checks run
in CI on every PR.

## License

MIT. Duplicate detection is performed by [fclones](https://github.com/pkolaczk/fclones) (MIT).
