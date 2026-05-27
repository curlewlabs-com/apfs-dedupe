#!/bin/sh
# Install (or remove) a scheduled apfs-dedupe run that reclaims space from
# duplicates created since the last run. Two modes:
#
#   per-user (default)  a LaunchAgent that runs as you, daily at 02:00, over your
#                       home directory. No root; only touches files you own.
#   --system            a LaunchDaemon that runs as root, daily at 02:00, over all
#                       of /Users (every account). Needs sudo to install.
#
#   ./install-daily.sh                            # per-user agent; scope defaults to $HOME
#   ./install-daily.sh --scope DIR                # per-user agent, custom scope
#   ./install-daily.sh --print                    # print the generated plist; install nothing
#   ./install-daily.sh --uninstall                # remove the per-user agent
#   sudo ./install-daily.sh --system              # all-users daemon; scope defaults to /Users
#   sudo ./install-daily.sh --system --min 1      # all-users daemon; scan every non-empty file
#   sudo ./install-daily.sh --system --uninstall  # remove the all-users daemon
#
# Both use the CLI's safe defaults: cloud-backed roots (so nothing is faulted
# down from the cloud), the protected and machine-managed ~/Library data
# (app-private Mail/Messages/Safari/containers plus OS-managed stores like the
# Spotlight index and daemon containers -- TCC-protected, poor dedup targets),
# and the Trash are excluded. They differ
# in the work floor: the per-user agent uses --git (--min 1) to catch the many
# small files where savings hide on a dev machine, while the all-users daemon
# uses --min 1M -- a nightly whole-/Users rescan at --min 1 would hash hundreds
# of thousands of files for diminishing return, and the large recurring
# duplication on a shared host (simulator clones, cached asset bundles) is all
# well above 1M. The first run does the real work; later runs are cheap because
# already-cloned files are detected and skipped.
#
# A LaunchAgent runs only while you are logged in (a sleeping Mac runs it at next
# wake); a LaunchDaemon runs regardless of login, which is what an unattended
# all-users host wants. Output is appended to ~/Library/Logs/apfs-dedupe.log
# (agent) or /Library/Logs/apfs-dedupe.log (daemon).
#
# Known limitation: the daemon uses the script and toolchain paths available at
# install time, including Homebrew paths. That is fine for the intended
# self-managed-machine use case; a future hardening pass can require root-owned
# paths if this is aimed at adversarial multi-user hosts.
set -eu

HERE=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
TOOL="$HERE/apfs-dedupe.sh"

MODE="user"
SCOPE=""
MIN=""
ACTION="install"
while [ $# -gt 0 ]; do
    case "$1" in
        --system) MODE="system"; shift ;;
        --scope) SCOPE="${2:?--scope needs a PATH}"; shift 2 ;;
        --scope=*) SCOPE="${1#--scope=}"; shift ;;
        --min) MIN="${2:?--min needs a SIZE}"; shift 2 ;;
        --min=*) MIN="${1#--min=}"; shift ;;
        --print) ACTION="print"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        -h|--help)
            sed -n '2,/^set -eu/p' "$0" | sed -e '/^set -eu/d' -e 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# Escape the three characters that would break the plist XML. Paths with spaces
# are fine inside an XML <string>; &, <, > are not, so a home or repo path
# containing them stays well-formed.
xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

# Per-user agent (default) vs all-users daemon (--system) differ in launchd
# domain, plist location, log path, default scope, and default dedup work floor;
# see the header comment for the why. WORK_FLAGS_XML is the mode-specific slice
# of the plist's ProgramArguments, between the shared --apply and --scope SCOPE.
if [ "$MODE" = "system" ]; then
    LABEL="com.curlewlabs.apfs-dedupe.system"
    PLIST="/Library/LaunchDaemons/$LABEL.plist"
    LOG="/Library/Logs/apfs-dedupe.log"
    DOMAIN="system"
    : "${SCOPE:=/Users}"
    : "${MIN:=1M}"
    WORK_FLAGS_XML=$(printf '        <string>--min</string>\n        <string>%s</string>' "$(xml_escape "$MIN")")
else
    LABEL="com.curlewlabs.apfs-dedupe.daily"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    LOG="$HOME/Library/Logs/apfs-dedupe.log"
    DOMAIN="gui/$(id -u)"
    : "${SCOPE:=$HOME}"
    if [ -n "$MIN" ]; then
        WORK_FLAGS_XML=$(printf '        <string>--min</string>\n        <string>%s</string>' "$(xml_escape "$MIN")")
    else
        WORK_FLAGS_XML=$(printf '        <string>--git</string>')
    fi
fi

# Resolve the scope to an absolute, real path (validating it exists). launchd
# runs the job from its own working directory, so a relative scope stored in
# the plist would later resolve against the wrong directory.
RAW_SCOPE="$SCOPE"
SCOPE=$(unset CDPATH; cd -- "$RAW_SCOPE" 2>/dev/null && pwd -P) \
    || { echo "error: scope not found: $RAW_SCOPE" >&2; exit 1; }

# PATH for the scheduled job. A LaunchAgent/LaunchDaemon inherits a minimal PATH
# that excludes Homebrew, where fclones is installed (python3 is in /usr/bin), so
# the job would fail "fclones not found" without this. Prepend the dirs of the
# fclones/python3 we can resolve now to the usual Homebrew (Apple-silicon and
# Intel) and system locations, so the job finds the same tools the installing
# shell has.
job_path() {
    p="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    for tool in fclones python3; do
        dir=$(command -v "$tool" 2>/dev/null) || continue
        dir=$(dirname -- "$dir")
        case ":$p:" in
            *":$dir:"*) ;;
            *) p="$dir:$p" ;;
        esac
    done
    printf '%s' "$p"
}

gen_plist() {
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(xml_escape "$LABEL")</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>$(xml_escape "$TOOL")</string>
        <string>--apply</string>
$WORK_FLAGS_XML
        <string>--scope</string>
        <string>$(xml_escape "$SCOPE")</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(xml_escape "$(job_path)")</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$(xml_escape "$LOG")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$LOG")</string>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
PLIST
}

if [ "$ACTION" = "print" ]; then
    gen_plist
    exit 0
fi

# Installing or removing the system daemon writes /Library/LaunchDaemons and the
# system launchd domain, both root-only. --print never does (handled above), so
# it stays usable without sudo as the preview/test seam.
if [ "$MODE" = "system" ] && [ "$(id -u)" -ne 0 ]; then
    echo "error: --system $ACTION needs root (writes /Library/LaunchDaemons and the" >&2
    echo "       system launchd domain). Re-run with sudo." >&2
    exit 1
fi

if [ "$ACTION" = "uninstall" ]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    if [ "$MODE" = "system" ]; then
        echo "Removed the all-users apfs-dedupe daemon ($LABEL)."
    else
        echo "Removed the daily apfs-dedupe agent ($LABEL)."
    fi
    exit 0
fi

# install
# The per-user agent runs as you and needs no privileges; installing it as root
# would schedule the daily --apply under root (gui/0, /var/root) instead. Refuse
# that -- the --system daemon is the supported way to run the sweep as root. The
# --uninstall path above stays usable as any user for cleanup.
if [ "$MODE" != "system" ] && [ "$(id -u)" -eq 0 ]; then
    echo "error: run this as your normal user, not root." >&2
    echo "       Per-user install sets up a LaunchAgent that runs as you and needs no" >&2
    echo "       privileges; for an all-users root sweep use: sudo $0 --system." >&2
    exit 1
fi
[ -x "$TOOL" ] || { echo "error: $TOOL not found or not executable." >&2; exit 1; }
mkdir -p "$(dirname -- "$PLIST")" "$(dirname -- "$LOG")"
gen_plist > "$PLIST"
# launchd refuses to load a LaunchDaemon plist that is group/world-writable or
# not owned by root:wheel (a privilege-safety check). We create it as root in
# system mode, so set ownership/mode explicitly rather than trust the umask.
if [ "$MODE" = "system" ]; then
    chown root:wheel "$PLIST"
    chmod 644 "$PLIST"
    # The daemon log contains full paths across every user's home. Create it
    # root-readable only before launchd opens StandardOutPath/StandardErrorPath.
    touch "$LOG"
    chown root:wheel "$LOG"
    chmod 600 "$LOG"
fi
# Reject a malformed plist before asking launchd to load it.
plutil -lint "$PLIST" >/dev/null || { echo "error: generated plist is invalid: $PLIST" >&2; exit 1; }

# Replace any previous instance, then load. A gui/ bootstrap can fail when run
# outside a GUI login session; in that mode the plist is still in place and will
# load at next login, so report the diagnostic without failing the install. A
# system LaunchDaemon has no GUI-session excuse, so bootstrap failure is fatal.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rc=0
err=$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1) || rc=$?
if [ "$MODE" = "system" ]; then who=root; else who=$(id -un); fi
if [ "$rc" -eq 0 ]; then
    echo "Installed: apfs-dedupe will run daily at 02:00 over $SCOPE (as $who)."
else
    echo "Installed the plist at $PLIST, but launchctl could not load it from here" >&2
    echo "(rc=$rc)${err:+: $err}" >&2
    echo "Load it with: launchctl bootstrap $DOMAIN \"$PLIST\"" >&2
    if [ "$MODE" = "system" ]; then
        exit "$rc"
    fi
fi
echo "Logs: $LOG"
if [ "$MODE" = "system" ]; then
    echo "Remove with: sudo $0 --system --uninstall"
else
    echo "Remove with: $0 --uninstall"
fi
