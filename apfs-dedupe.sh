#!/bin/sh
# apfs-dedupe -- reclaim disk by turning duplicate files into APFS clones.
#
# Duplicate detection is delegated to fclones (fast, parallel, battle-tested).
# The apply is ours and runs windowless, crash-safe, and metadata-complete
# (including ACLs); see lib/apply.py and docs/architecture.md.
#
#   sudo ./apfs-dedupe.sh                  # dry-run over /Users
#   sudo ./apfs-dedupe.sh --apply          # actually reclaim
#   ./apfs-dedupe.sh --scope ~/projects    # narrower scope, no sudo
#
# The default scope is all of /Users. Deduping across users is safe: each file
# keeps its own owner/mode/ACL and only storage is shared -- see README.md.
set -eu

SCOPE="/Users"
MIN="1M"
MIN_SET=""
APPLY=""
ALLOW_ROOT=""
INCLUDE_CLOUD=""
INCLUDE_APP_DATA=""
GIT_PRESET=""
VERBOSE=""

usage() {
    cat <<'EOF'
apfs-dedupe -- APFS clone deduplication for macOS.

Usage: apfs-dedupe.sh [options]

  --apply          actually clone duplicates (default: dry-run, changes nothing)
  --scope PATH     directory to scan (default: /Users)
  --min SIZE       ignore files smaller than SIZE (default: 1M). A bare number
                   is BYTES (--min 100000 = ~98 KiB); add a suffix for units:
                   500K, 1M, 2G (decimal) or KiB/MiB/GiB (binary). The default
                   bounds work, not savings: cloning any ordinary allocated
                   duplicate frees at least one 4 KiB block whatever its size;
                   sparse or compressed files can occupy fewer blocks, which is
                   why output separates allocated and logical bytes. 1M just
                   trims the long tail of tiny files for the common case.
  --git            preset for git-/CI-heavy machines: set --min to 1 so the many
                   small content-addressed files (git objects, build caches)
                   where savings actually concentrate there are deduped too.
                   Scans and clones far more files; an explicit --min overrides it.
  --exclude GLOB   skip paths whose name matches GLOB, e.g. --exclude '*.iso'
                   (passed to fclones; quote it so the shell doesn't expand it);
                   repeatable
  --allow-root     permit scanning / or the data-volume root (refused by
                   default so the tool stays aimed at /Users)
  --include-cloud  also scan cloud-backed roots (iCloud Drive, File Providers,
                   Photos libraries) that are excluded by default. WARNING:
                   reading an evicted (dataless) file re-downloads it from the
                   cloud, so a broad scan can pull down gigabytes -- leave this
                   off unless those roots are fully downloaded locally.
  --include-app-data
                   also scan the protected, machine-managed ~/Library data
                   excluded by default: app-private stores (Mail, Messages,
                   Safari, sandbox Containers) plus OS-managed data (Spotlight
                   index, on-device intelligence, daemon containers) -- all
                   TCC-protected and poor dedup targets. Does not grant access:
                   only Full Disk Access does, which a scheduled daemon cannot
                   get, so reaching Desktop/Documents/Downloads means running
                   interactively from a terminal that has it.
  --verbose        print a line per cloned file (in --apply) and per skipped file;
                   default: an --apply run prints just the summary and skips are
                   summarized by reason (a dry-run always prints its full plan)
  -h, --help       show this help

Each duplicate is replaced by a copy-on-write clone of the first file in its
group: independent inode, shared storage, diverge-on-write. All metadata
(mode, owner, timestamps, xattrs, ACLs) is preserved. Files that cannot be
modified (immutable, locked by ACL, permission denied) are skipped, never
forced.

Output: the dry-run plan and the savings summary go to STDOUT; progress, and one
note for any folders that could not be read (privacy protection / permissions)
with Full Disk Access advice, go to STDERR. The summary leads with the files
scanned and how long the scan took, then the reclaim (allocated bytes first,
logical duplicate bytes in parentheses); files examined but left untouched are
summarized by reason. An --apply run prints just that summary by default -- the
per-file `cloned` lines are opt-in under --verbose, since a nightly /Users sweep
clones tens of thousands of files and one line each would bury the log. --verbose
also adds the raw fclones diagnostics and a per-file skip line on STDERR. Save
just the plan with a redirect and still watch progress on screen:
  ./apfs-dedupe.sh > plan.txt                       # dry-run plan + summary in the file
  ./apfs-dedupe.sh --apply --verbose > clones.txt    # full per-file apply record
EOF
}

# Parse options by rotating the positional parameters: each --exclude GLOB is
# pushed back onto $@ as two words, so a glob containing spaces (e.g.
# "Application Support") stays a single argument and is never word-split or
# expanded against the cwd; every other option is consumed into a scalar and
# dropped. argc counts only the original args, so when it hits zero the loop has
# processed them all and $@ holds exactly the fclones --exclude pass-through
# args (which must survive untouched until the fclones call below).
argc=$#
while [ "$argc" -gt 0 ]; do
    argc=$((argc - 1))
    case "$1" in
        --apply) APPLY=1; shift ;;
        --allow-root) ALLOW_ROOT=1; shift ;;
        --include-cloud) INCLUDE_CLOUD=1; shift ;;
        --include-app-data) INCLUDE_APP_DATA=1; shift ;;
        --git) GIT_PRESET=1; shift ;;
        --verbose) VERBOSE=1; shift ;;
        --min) MIN="${2:?--min needs a SIZE}"; MIN_SET=1; shift 2; argc=$((argc - 1)) ;;
        --min=*) MIN="${1#--min=}"; MIN_SET=1; shift ;;
        --scope) SCOPE="${2:?--scope needs a PATH}"; shift 2; argc=$((argc - 1)) ;;
        --scope=*) SCOPE="${1#--scope=}"; shift ;;
        --exclude) set -- "$@" --exclude "${2:?--exclude needs a GLOB}"; shift 2; argc=$((argc - 1)) ;;
        --exclude=*) set -- "$@" --exclude "${1#--exclude=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# --git preset: on git-/CI-heavy trees the savings concentrate in many small
# content-addressed files (git objects, build caches), and cloning any ordinary
# allocated duplicate frees at least one whole 4 KiB block regardless of logical
# size -- so there is no useful size floor for those files. Drop --min to 1
# (every non-empty file; 0-byte files save nothing and fclones --min 1 already
# excludes them), unless the user set --min explicitly, which always wins.
if [ -n "$GIT_PRESET" ] && [ -z "$MIN_SET" ]; then
    MIN=1
fi

[ "$(uname -s)" = "Darwin" ] || { echo "error: macOS only (uses APFS clonefile)." >&2; exit 1; }

# The apply relies on CLONE_NOFOLLOW_ANY for symlink-safe path resolution, which
# Apple's <sys/clonefile.h> first defines in macOS 15; on older systems that flag
# bit is silently ignored -- degrading the safety to nothing -- so refuse apply
# mode rather than clone unsafely. (O_NOFOLLOW_ANY is older, macOS 11, but
# CLONE_NOFOLLOW_ANY is the binding floor.) Dry-run uses none of it and works on
# any macOS version (the Darwin check above still keeps this macOS-only). This is
# a fast precheck; lib/apply.py enforces the same floor itself, since it is
# independently runnable as root.
# (min of {version,15} == 15 iff version >= 15; sort -V handles 15 vs 26 and the
# legacy 10.x ordering correctly.)
if [ -n "$APPLY" ]; then
    OS_VER=$(sw_vers -productVersion 2>/dev/null || echo 0)
    if [ "$(printf '%s\n15\n' "$OS_VER" | sort -V | head -n1)" != "15" ]; then
        echo "error: --apply requires macOS 15+ for symlink-safe cloning (this is macOS $OS_VER)." >&2
        exit 1
    fi
fi

# Resolve to a canonical path (this also validates existence), then refuse the
# whole-machine roots unless explicitly allowed. /Users (the default) and any
# subdirectory are fine. The system volume is a sealed, read-only snapshot, so
# Apple's system files cannot be modified regardless; but the writable
# data-volume root would sweep /Library, /Applications, etc. -- not the point.
RAW_SCOPE="$SCOPE"
SCOPE=$(unset CDPATH; cd -- "$RAW_SCOPE" 2>/dev/null && pwd -P) \
    || { echo "error: scope not found: $RAW_SCOPE" >&2; exit 1; }
if [ -z "$ALLOW_ROOT" ]; then
    case "$SCOPE" in
        / | /System | /System/Volumes/Data)
            echo "error: refusing to dedupe '$SCOPE' -- that sweeps system/Apple areas." >&2
            echo "       Use /Users (the default) or a subdirectory; pass --allow-root to override." >&2
            exit 1 ;;
    esac
fi

command -v fclones >/dev/null 2>&1 || { echo "error: fclones not found -- install with: brew install fclones" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found (install the Xcode Command Line Tools)." >&2; exit 1; }

# Reading other users' files under /Users requires root; without it fclones
# silently skips what it cannot read, undercounting savings.
if [ "$(id -u)" -ne 0 ] && [ "$SCOPE" = "/Users" ]; then
    echo "note: not running as root; files you cannot read will be skipped. Use sudo for /Users." >&2
fi

HERE=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
APPLY_ENGINE="$HERE/lib/apply.py"

REPORT=$(mktemp)
FCERR=$(mktemp)
trap 'rm -f "$REPORT" "$FCERR"' EXIT INT TERM

# Cloud-backed files (iCloud Drive, third-party File Providers, Photos library
# originals) can be "dataless" -- evicted to the cloud, holding no local bytes.
# fclones hashes by reading, and reading a dataless file faults it back down from
# the cloud, so a scan over /Users could silently re-download gigabytes of
# deliberately-evicted content -- in a dry-run too, since the scan is fclones'.
# Keep those roots out of the scan by default; --include-cloud opts back in (only
# safe when they are fully downloaded locally). lib/apply.py additionally skips
# any dataless file that still reaches it. See docs/architecture.md.
if [ -z "$INCLUDE_CLOUD" ]; then
    set -- "$@" \
        --exclude '**/Library/Mobile Documents/**' \
        --exclude '**/Library/CloudStorage/**' \
        --exclude '**/*.photoslibrary/**'
fi

# Protected, churning, or machine-managed ~/Library data, excluded by default
# and re-included with --include-app-data. Two classes, same rationale (TCC-
# protected and a poor dedup target):
#   - app-private stores -- Mail, Messages, Safari, the per-app sandbox
#     Containers -- live databases holding little duplicate data and much that
#     is sensitive;
#   - OS-managed data -- Spotlight's index, the on-device intelligence/Siri
#     stores, sandboxed daemon data -- machine-generated and constantly
#     rewritten, with nothing worth deduping.
# With --include-app-data on an unattended scheduled job, macOS denies access to
# these (errno "Operation not permitted"); the denial lands at fclones' scan, so
# the wrapper folds those scan-time denials into one counted note (see the fclones
# call below) -- the engine never sees them, since denied paths never enter its
# JSON.
# NOTE: --include-app-data does not grant access -- only Full Disk Access does, and
# a LaunchDaemon (run via /bin/sh) cannot be granted it cleanly, so the way to dedup
# the TCC-protected user folders (Desktop/Documents/Downloads) is to run
# apfs-dedupe interactively from a terminal that has Full Disk Access. See
# docs/architecture.md.
if [ -z "$INCLUDE_APP_DATA" ]; then
    set -- "$@" \
        --exclude '**/Library/Mail/**' \
        --exclude '**/Library/Messages/**' \
        --exclude '**/Library/Safari/**' \
        --exclude '**/Library/Containers/**' \
        --exclude '**/Library/Group Containers/**' \
        --exclude '**/Library/Metadata/CoreSpotlight/**' \
        --exclude '**/Library/IntelligencePlatform/**' \
        --exclude '**/Library/Biome/**' \
        --exclude '**/Library/Trial/**' \
        --exclude '**/Library/DuetExpertCenter/**' \
        --exclude '**/Library/DoNotDisturb/**' \
        --exclude '**/Library/Suggestions/**' \
        --exclude '**/Library/Daemon Containers/**' \
        --exclude '**/Library/AppleMediaServices/**'
fi

# The Trash holds files pending deletion -- cloning them shares storage about to
# be freed anyway, never a useful target -- and it is TCC-protected too. Excluded
# unconditionally: there is no case for deduping the Trash, so no opt-in.
set -- "$@" \
    --exclude '**/.Trash/**' \
    --exclude '**/.Trashes/**'

echo "Scanning $SCOPE (files >= $MIN) with fclones..." >&2
# --no-ignore --hidden: build caches and dotfiles are exactly where the dupes
#   live, and fclones honours .gitignore + skips hidden by default.
# --one-fs: never group across volumes, where clonefile would silently fall
#   back to a full copy.
# "$@" holds the --exclude pairs (the cloud-root and app-private defaults above
# plus any the user passed), quoted so a glob with spaces stays one argument and
# is not expanded against the cwd.
#
# fclones' stderr is captured rather than streamed. A run without Full Disk Access
# is denied at readdir on every TCC-protected folder it meets (Desktop, Documents,
# Downloads), and fclones emits one "Failed to read dir ...: Permission denied /
# Operation not permitted" line per folder. Those paths never enter the JSON, so
# the engine -- which sees only the report -- cannot summarize them; the wrapper
# must. Left raw they bury the result, so by default fold them into one counted
# note carrying the same Full Disk Access advice the engine gives, and pass every
# other fclones line (its progress/summary, real warnings) straight through.
rc=0
scan_start=$(date +%s)
fclones group --no-ignore --hidden --one-fs --min "$MIN" "$@" \
    --format json "$SCOPE" >"$REPORT" 2>"$FCERR" || rc=$?
scan_secs=$(( $(date +%s) - scan_start ))

# Match the OS strerror text, not fclones' surrounding wording: the errno string
# is stable across fclones versions. It is English here because launchd jobs run
# in the C locale and an interactive shell is overwhelmingly English; a non-English
# interactive run simply falls through to the raw passthrough below, never silence.
# A non-zero fclones exit is a real failure (bad args, crash), not a denial it
# recovered from, so replay everything and propagate it rather than hide it behind
# a summary. --verbose also keeps the raw lines.
PERM_RE='Permission denied|Operation not permitted'
if [ "$rc" -ne 0 ] || [ -n "$VERBOSE" ]; then
    cat "$FCERR" >&2
else
    denied=$(grep -cE "$PERM_RE" "$FCERR" 2>/dev/null || true)
    grep -vE "$PERM_RE" "$FCERR" >&2 || true
    if [ "${denied:-0}" -gt 0 ]; then
        echo "note: $denied folder(s) could not be read (privacy protection or permissions) and were skipped." >&2
        echo "      To include privacy-protected folders (Desktop, Documents, Downloads, ...), run" >&2
        echo "      apfs-dedupe yourself from a terminal with Full Disk Access; a scheduled run" >&2
        echo "      cannot reach them. Re-run with --verbose to list them." >&2
    fi
fi
if [ "$rc" -ne 0 ]; then
    echo "error: fclones exited with status $rc" >&2
    exit "$rc"
fi

# Files fclones actually considered (after the --min filter), read best-effort
# from its scan log to put a scan-size figure in the run summary. This is the one
# place the wrapper reads fclones' wording rather than a stable errno string
# (contrast the denial fold above, where matching the wrong text would hide real
# denials): it is purely informational, so a wording change in a future fclones
# just drops the count from the summary -- the engine then reports the duration
# alone -- never a wrong number and never a failure. English-only like the rest
# (C-locale launchd jobs, overwhelmingly-English interactive shells). Commas in
# large counts are stripped; anything not left a clean integer is discarded.
# Anchored on fclones' exact "Found N (SIZE) files matching selection criteria"
# shape (the count is always followed by the size in parens): a format that does
# not match drops the count rather than capturing a partial number.
files_scanned=$(sed -n 's/.*Found \([0-9][0-9,]*\) (.*files matching selection criteria.*/\1/p' "$FCERR" | tail -n1 | tr -d ',')
case "$files_scanned" in
    '' | *[!0-9]*) files_scanned="" ;;
esac

if [ -z "$APPLY" ]; then
    echo >&2
    echo "DRY RUN -- nothing changed. Re-run with --apply to reclaim the space below." >&2
    echo >&2
fi

# Engine flags. The --exclude pass-through args in "$@" were consumed by the
# fclones call above, so the positional list is free to reuse for the engine's
# argv -- quoted, so nothing re-splits.
set --
[ -n "$APPLY" ] && set -- "$@" --apply
[ -n "$VERBOSE" ] && set -- "$@" --verbose
set -- "$@" --scan-seconds "$scan_secs"
[ -n "$files_scanned" ] && set -- "$@" --files-scanned "$files_scanned"
python3 "$APPLY_ENGINE" "$@" <"$REPORT"

# After an --apply, APFS/Time Machine *local* snapshots (taken hourly) are
# copy-on-write and pin the blocks they captured, so blocks just freed can stay
# attached to a snapshot instead of returning to free space -- `df` then shows
# little change and the reclaim looks like it failed. Surface that once when
# local snapshots are present. A note only: never delete snapshots, which is a
# data-retention decision for the operator. See README ("Why didn't free space
# change? Snapshots"). (The grep sits in the `if` condition, so a
# no-match exit doesn't trip `set -e`.)
if [ -n "$APPLY" ] && tmutil listlocalsnapshots / 2>/dev/null | grep -q 'com\.apple\.TimeMachine'; then
    echo >&2
    echo "note: local Time Machine snapshots may still hold the space reclaimed above" >&2
    echo "      until they expire (~24h, sooner under disk pressure). To reclaim it now" >&2
    echo "      without touching your backups:" >&2
    echo "        sudo tmutil thinlocalsnapshots /System/Volumes/Data 21474836480 4" >&2
    echo "      See the README section \"Why didn't free space change? Snapshots\"." >&2
fi
