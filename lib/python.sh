# shellcheck shell=sh
# Finding a Python new enough for the engine, for apfs-dedupe.sh and
# install-daily.sh, which source this file.

# Where Homebrew installs commands: Apple silicon, then Intel. sudo and launchd
# both start a command with a PATH of their own that leaves these out, so
# python3 on that PATH is the /usr/bin/python3 macOS ships, which is too old.
# Overridable only so the tests can stand in a directory of their own.
APFS_DEDUPE_BREW_BINS=${APFS_DEDUPE_BREW_BINS:-/opt/homebrew/bin /usr/local/bin}

python_is_new_enough() {
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null
}

# Set PYTHON to the interpreter to run the engine with, or say where it looked
# and return 1. APFS_DEDUPE_PYTHON, which a scheduled job records, is the only
# candidate when it is set; otherwise python3 on PATH, then python3 in each of
# APFS_DEDUPE_BREW_BINS, whichever is first to be 3.12 or newer.
find_python() {
    if [ -n "${APFS_DEDUPE_PYTHON:-}" ]; then
        PYTHON=$APFS_DEDUPE_PYTHON
        python_is_new_enough "$PYTHON" && return 0
        echo "error: APFS_DEDUPE_PYTHON is $PYTHON, which does not run or is older than Python 3.12." >&2
        echo "       For a scheduled run, re-run install-daily.sh so the job records a newer one." >&2
        return 1
    fi
    if PYTHON=$(command -v python3 2>/dev/null) && python_is_new_enough "$PYTHON"; then
        return 0
    fi
    looked="python3 on PATH${PYTHON:+ ($PYTHON)}"
    for dir in $APFS_DEDUPE_BREW_BINS; do
        PYTHON=$dir/python3
        if [ -x "$PYTHON" ] && python_is_new_enough "$PYTHON"; then
            return 0
        fi
        looked="$looked, $PYTHON"
    done
    echo "error: apfs-dedupe needs Python 3.12 or newer, and none of these is: $looked." >&2
    echo "       Install one (for example: brew install python)." >&2
    return 1
}
