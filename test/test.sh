#!/bin/sh
# Integration test for the apfs-dedupe apply engine.
#
# These are not change-detectors: each case guards a correctness or safety
# invariant that is the whole reason this tool exists rather than `fclones
# dedupe` -- metadata/ACL fidelity, the dry-run contract, fail-safe on locked
# files, and the false-equal guard. They drive lib/apply.py with hand-built
# fclones-shaped JSON so the apply is exercised directly. One wrapper smoke uses
# real fclones to pin the external JSON/argv contract without duplicating the
# apply-engine safety suite.
#
# Runs as the invoking user (no sudo); scratch lives on the APFS data volume
# so clonefile actually shares extents.
set -eu

HERE=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
ENGINE="$HERE/../lib/apply.py"
SCRIPT="$HERE/../apfs-dedupe.sh"
WORK=$(mktemp -d /private/tmp/apfsdedupe-test.XXXXXX)
# shellcheck disable=SC2329  # invoked indirectly via the trap below
cleanup() { chflags -R nouchg "$WORK" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

FAILED=0
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; FAILED=1; }
assert_eq() {  # assert_eq LABEL ACTUAL EXPECTED
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', want '$3')"; fi
}

report() {  # report CANONICAL DUP  ->  fclones-shaped JSON on stdout
    size=$(stat -f '%z' "$1")
    printf '{"header":{"stats":{"redundant_file_count":1,"redundant_file_size":%s}},' "$size"
    printf '"groups":[{"file_len":%s,"file_hash":"test","files":["%s","%s"]}]}' "$size" "$1" "$2"
}
ino()   { stat -f '%i'  "$1"; }
perms() { stat -f '%Lp' "$1"; }
mtime() { stat -f '%m'  "$1"; }

# ---- --apply clones and preserves all metadata, including the ACL ----
d="$WORK/meta"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/dup"
chmod 644 "$d/canon"; chmod 600 "$d/dup"
xattr -w com.example.tag DUPVAL "$d/dup"
chmod +a "everyone allow read" "$d/dup"
touch -t 202001010101 "$d/dup"
ino_before=$(ino "$d/dup"); mt_before=$(mtime "$d/dup")
report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply >/dev/null 2>&1

if [ "$(ino "$d/dup")" != "$ino_before" ]; then pass "apply: duplicate cloned (inode changed)"; else fail "apply: duplicate not cloned"; fi
if cmp -s "$d/canon" "$d/dup"; then pass "apply: content identical after clone"; else fail "apply: content differs after clone"; fi
assert_eq "apply: mode preserved"  "$(perms "$d/dup")" "600"
assert_eq "apply: mtime preserved" "$(mtime "$d/dup")" "$mt_before"
assert_eq "apply: xattr preserved" "$(xattr -p com.example.tag "$d/dup" 2>/dev/null)" "DUPVAL"
case "$(ls -le "$d/dup")" in
    *"allow read"*) pass "apply: ACL preserved (the headline guarantee)" ;;
    *) fail "apply: ACL was dropped" ;;
esac

# ---- dry-run changes nothing ----
d="$WORK/dry"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/dup"
ino_before=$(ino "$d/dup")
report "$d/canon" "$d/dup" | python3 "$ENGINE" >/dev/null 2>&1
assert_eq "dry-run: nothing changed (inode stable)" "$(ino "$d/dup")" "$ino_before"

# ---- fail-safe on an immutable file, and the skip names the full path ----
d="$WORK/locked"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/dup"
chflags uchg "$d/dup"
ino_before=$(ino "$d/dup")
# Capture stderr, discard stdout (the summary): 2>&1 1>/dev/null inside $() points
# fd2 at the capture pipe, then sends fd1 to /dev/null. --verbose makes the
# per-file skip line (the full-path diagnostic asserted below) reach stderr;
# without it skips are only counted in the summary.
rc=0; err=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply --verbose 2>&1 1>/dev/null) || rc=$?
chflags nouchg "$d/dup"
if [ "$(ino "$d/dup")" = "$ino_before" ] && [ "$rc" -eq 0 ]; then
    pass "fail-safe: immutable file skipped, clean exit"
else
    fail "fail-safe: immutable file mishandled (inode changed or exit $rc)"
fi
# The skip warning must carry the FULL path, not just the basename the
# fd-relative syscalls use: on a whole-/Users run many duplicates share a
# basename, so a basename-only message cannot identify the file that failed.
case "$err" in
    *"$d/dup"*) pass "diagnostics: skip warning names the full duplicate path" ;;
    *) fail "diagnostics: skip warning lacks full path (got: $err)" ;;
esac

# ---- false-equal guard -- same size, different bytes, must NOT clone ----
d="$WORK/falseeq"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"
head -c 1048576 /dev/urandom > "$d/dup"   # same size, different content
ino_before=$(ino "$d/dup"); sum_before=$(shasum "$d/dup" | cut -d' ' -f1)
report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply >/dev/null 2>&1
if [ "$(ino "$d/dup")" = "$ino_before" ] && [ "$(shasum "$d/dup" | cut -d' ' -f1)" = "$sum_before" ]; then
    pass "verify: mismatched content left untouched"
else
    fail "verify: clobbered a file whose content did not match"
fi

# ---- the removed verification bypass is rejected before it can act ----
# The byte re-compare is the live-data safety boundary. A stale bypass spelling
# must fail closed at both entrypoints and leave mismatched files untouched.
d="$WORK/removed-noverify"; mkdir "$d"
head -c 4096 /dev/urandom > "$d/canon"
head -c 4096 /dev/urandom > "$d/dup"
sum_before=$(shasum "$d/dup" | cut -d' ' -f1)
rc=0; report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply --no-verify >/dev/null 2>&1 || rc=$?
if [ "$rc" -ne 0 ] && [ "$(shasum "$d/dup" | cut -d' ' -f1)" = "$sum_before" ]; then
    pass "verify: engine rejects removed bypass before touching mismatched bytes"
else
    fail "verify: engine accepted removed bypass or modified mismatched bytes (rc=$rc)"
fi
rc=0; "$SCRIPT" --no-verify --scope "$d" --min 1 >/dev/null 2>&1 || rc=$?
if [ "$rc" -ne 0 ] && [ "$(shasum "$d/dup" | cut -d' ' -f1)" = "$sum_before" ]; then
    pass "verify: wrapper rejects removed bypass before touching mismatched bytes"
else
    fail "verify: wrapper accepted removed bypass or modified mismatched bytes (rc=$rc)"
fi

# ---- the /Users-not-/ guard refuses whole-machine roots ----
# (the guard runs before the fclones check, so this needs no fclones on PATH)
rc=0; "$SCRIPT" --scope / >/dev/null 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then pass "guard: refuses --scope /"; else fail "guard: did not refuse --scope /"; fi

# ---- privileged ops are fd-anchored, so a swapped parent directory
# cannot redirect the clone (root symlink/TOCTOU privilege-escalation guard) ----
# clonefile follows symlinks in the destination *path*, so doing the
# clone/rename by path in a user-writable directory lets a local user who owns
# that directory swap it for a symlink mid-apply and redirect a root clone into
# an arbitrary same-volume location. The engine pins the duplicate's parent by
# fd and uses clonefileat/openat/renameat; the swap below must NOT redirect the
# write into the attacker's directory.
d="$WORK/redirect"; mkdir "$d" "$d/real" "$d/attacker"
head -c 4096 /dev/urandom > "$d/canon"
cp "$d/canon" "$d/real/dup"
rc=0
PYTHONPATH="$HERE/../lib" python3 - "$d" <<'PY' || rc=$?
import apply, os, sys
base = sys.argv[1]
real = os.path.join(base, "real")
dirfd = os.open(real, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
# Attacker owns the parent: after we hold the fd, swap the real dir for a
# symlink to a directory they control.
os.rename(real, os.path.join(base, "real_moved"))
os.symlink(os.path.join(base, "attacker"), real)
ok = apply._clone_over(os.path.join(base, "canon"), dirfd, "dup", apply._Skips(lambda m: None, False))
os.close(dirfd)
sys.exit(0 if ok else 4)
PY
canon_sum=$(shasum "$d/canon" | cut -d' ' -f1)
dup_sum=$(shasum "$d/real_moved/dup" 2>/dev/null | cut -d' ' -f1)
if [ "$rc" -eq 0 ] && [ "$dup_sum" = "$canon_sum" ] && [ -z "$(ls -A "$d/attacker")" ]; then
    pass "fd-anchor: swapped parent dir cannot redirect the clone"
else
    fail "fd-anchor: redirect not prevented (rc=$rc, attacker-dir='$(ls -A "$d/attacker" 2>/dev/null)')"
fi

# ---- a symlink in an INTERMEDIATE path component is refused ----
# O_NOFOLLOW guards only the final component, so an attacker who owns an
# ancestor of the duplicate could swap an intermediate directory for a symlink
# to redirect where the parent fd lands. _open_parent_dir uses O_NOFOLLOW_ANY,
# so a symlink anywhere in the path makes the open fail (skip) instead. The
# redirect target below EXISTS, so a final-component-only guard would wrongly
# succeed -- this distinguishes O_NOFOLLOW_ANY from plain O_NOFOLLOW.
d="$WORK/intermediate"; mkdir -p "$d/real/sub" "$d/attacker/sub"
head -c 1024 /dev/urandom > "$d/real/sub/dup"
mv "$d/real" "$d/real_moved"
ln -s "$d/attacker" "$d/real"   # 'real' is now a symlink; real/sub resolves to attacker/sub (exists)
rc=0
PYTHONPATH="$HERE/../lib" python3 - "$d/real/sub/dup" <<'PY' || rc=$?
import apply, os, sys
try:
    os.close(apply._open_parent_dir(sys.argv[1]))
    sys.exit(0)   # opened => followed the intermediate symlink (vulnerable)
except OSError:
    sys.exit(3)   # refused => safe
PY
if [ "$rc" -eq 3 ]; then
    pass "path: intermediate symlink component is refused (O_NOFOLLOW_ANY)"
else
    fail "path: intermediate symlink component not refused (rc=$rc)"
fi

# ---- a symlink in the clone SOURCE path is refused ----
# The clone itself must refuse a symlinked source path -- otherwise a local user
# who owns an ancestor of the canonical could race in an intermediate symlink
# and have root clone an arbitrary same-volume file (e.g. a root-only secret)
# into a file they own.
# CLONE_NOFOLLOW_ANY must make the clone fail (skip), leaving the duplicate
# untouched. The redirect target exists, so a final-only guard would clone it.
d="$WORK/srcsym"; mkdir -p "$d/realsrc" "$d/secret" "$d/dest"
head -c 2048 /dev/urandom > "$d/secret/canon"   # stands in for a root-only file
head -c 2048 /dev/urandom > "$d/dest/dup"
dup_before=$(shasum "$d/dest/dup" | cut -d' ' -f1)
ln -s "$d/secret" "$d/realsrc/link"             # canonical path traverses this symlink
rc=0
PYTHONPATH="$HERE/../lib" python3 - "$d" <<'PY' || rc=$?
import apply, os, sys
base = sys.argv[1]
dirfd = apply._open_parent_dir(os.path.join(base, "dest", "dup"))
canonical = os.path.join(base, "realsrc", "link", "canon")   # via realsrc/link -> secret
ok = apply._clone_over(canonical, dirfd, "dup", apply._Skips(lambda m: None, False))
os.close(dirfd)
sys.exit(0 if ok else 3)   # ok=False (skipped) is the safe outcome
PY
dup_after=$(shasum "$d/dest/dup" | cut -d' ' -f1)
if [ "$rc" -eq 3 ] && [ "$dup_before" = "$dup_after" ]; then
    pass "source: symlinked clone-source path is refused (CLONE_NOFOLLOW_ANY)"
else
    fail "source: symlinked source not refused (rc=$rc, dup changed: $([ "$dup_before" = "$dup_after" ] && echo no || echo yes))"
fi

# ---- a symlink as the clone-source FINAL component is refused ----
# Distinct from the intermediate-component case: here canonical *itself* is the
# symlink. CLONE_NOFOLLOW_ANY refuses a symlink in ANY source component, the
# last one included, so the clone fails (skip) rather than cloning the link
# target's bytes into the duplicate.
d="$WORK/srcfinal"; mkdir -p "$d/secret" "$d/dest"
head -c 2048 /dev/urandom > "$d/secret/realcanon"   # the file the symlink points at
head -c 2048 /dev/urandom > "$d/dest/dup"
dup_before=$(shasum "$d/dest/dup" | cut -d' ' -f1)
ln -s "$d/secret/realcanon" "$d/canon_link"         # canonical's final component is a symlink
rc=0
PYTHONPATH="$HERE/../lib" python3 - "$d" <<'PY' || rc=$?
import apply, os, sys
base = sys.argv[1]
dirfd = apply._open_parent_dir(os.path.join(base, "dest", "dup"))
ok = apply._clone_over(os.path.join(base, "canon_link"), dirfd, "dup", apply._Skips(lambda m: None, False))
os.close(dirfd)
sys.exit(0 if ok else 3)
PY
dup_after=$(shasum "$d/dest/dup" | cut -d' ' -f1)
if [ "$rc" -eq 3 ] && [ "$dup_before" = "$dup_after" ]; then
    pass "source: symlinked final component is refused (CLONE_NOFOLLOW_ANY)"
else
    fail "source: symlinked final component not refused (rc=$rc)"
fi

# ---- the apply-mode macOS version gate is enforced in the engine ----
# apply.py is itself a runnable root-capable engine, so it must refuse --apply
# below macOS 15 on its own, not only via the shell wrapper. macOS 15 is the
# floor because <sys/clonefile.h> first defines CLONE_NOFOLLOW_ANY there; on
# 11-14 O_NOFOLLOW_ANY exists but CLONE_NOFOLLOW_ANY does not, so the source-path
# guard is silently ignored -- those versions MUST be refused even though they
# look "recent". Pure version-string logic, exercised across that 11-14 trap and
# the fail-closed unparseable cases.
rc=0
PYTHONPATH="$HERE/../lib" python3 - <<'PY' || rc=$?
import apply, sys
cases = {"10.15": False, "11.0": False, "12.6": False, "13.7": False,
         "14.7.1": False, "15": True, "15.0": True, "15.4.1": True,
         "26.5": True, "": False, "garbage": False}
bad = [v for v, want in cases.items() if apply._supports_symlink_safe_clone(v) != want]
sys.exit(0 if not bad else 4)
PY
if [ "$rc" -eq 0 ]; then
    pass "version gate: 15+ allowed, 11-14 (no CLONE_NOFOLLOW_ANY) and unparseable refused"
else
    fail "version gate: boundary logic wrong"
fi

# ---- a hard-linked duplicate is left intact (never break hard links) --
# A duplicate that is also hard-linked to a path outside its group must NOT be
# cloned: cloning would break the hard link (splitting one inode into two
# independent files) and reclaim nothing, since the other link still pins the
# inode's blocks. The dry-run projection must skip it too, so it matches apply --
# otherwise the dry run promises space that --apply can never deliver.
d="$WORK/hardlink"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/dup"
ln "$d/dup" "$d/sibling"            # dup now has st_nlink=2, linked to an out-of-group path
ino_before=$(ino "$d/dup")
errfile="$d/err.log"
out=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply 2>"$errfile")
err=$(cat "$errfile")
if [ "$(ino "$d/dup")" = "$ino_before" ] && [ "$(ino "$d/dup")" = "$(ino "$d/sibling")" ]; then
    pass "hard link: linked duplicate left intact (not cloned, link preserved)"
else
    fail "hard link: linked duplicate modified (dup inode $(ino "$d/dup"), sibling $(ino "$d/sibling"))"
fi
case "$out" in
    *"across 0 files"*) pass "hard link: apply reclaim count excludes the hard-linked dup" ;;
    *) fail "hard link: apply counted a hard-linked dup (got: $out)" ;;
esac
# Default output summarizes skips by reason on stdout and streams nothing
# per-file to stderr; --verbose (exercised by the fail-safe case) is what
# restores the per-file line.
case "$out" in
    *"hard-linked elsewhere"*) pass "hard link: skip counted in the summary breakdown" ;;
    *) fail "hard link: skip not summarized (got: $out)" ;;
esac
case "$err" in
    *"skip "*) fail "hard link: per-file skip leaked to stderr without --verbose (got: $err)" ;;
    *) pass "hard link: per-file skip suppressed by default (summarized, not streamed)" ;;
esac
# Dry-run must make the same projection, not over-promise reclaimable space.
dry=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" 2>/dev/null)
case "$dry" in
    *"across 0 files"*) pass "hard link: dry-run projection also excludes it" ;;
    *) fail "hard link: dry-run over-counted a hard-linked dup (got: $dry)" ;;
esac

# ---- a permission-denied skip is bucketed as such, and the summary advises
# granting Full Disk Access -- the one skip reason a user can act on. A directory
# with no permissions stands in for a TCC-protected folder the run cannot read:
# the duplicate beneath it must be left untouched and surfaced as a permission
# skip, not a per-file warning. (Non-root, like the rest of this suite -- root
# would bypass the mode bits.) ----
d="$WORK/perm"; mkdir "$d" "$d/locked"
head -c 1048576 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/locked/dup"
ino_before=$(ino "$d/locked/dup")
chmod 000 "$d/locked"            # no search/read -> the engine cannot reach the dup
out=$(report "$d/canon" "$d/locked/dup" | python3 "$ENGINE" --apply 2>/dev/null)
chmod 755 "$d/locked"            # restore before asserting (and so cleanup can recurse)
assert_eq "permission: unreadable duplicate left untouched" "$(ino "$d/locked/dup")" "$ino_before"
case "$out" in
    *"unreadable (privacy protection or permissions)"*) pass "permission: skip bucketed as a permission denial" ;;
    *) fail "permission: skip not bucketed as permission (got: $out)" ;;
esac
case "$out" in
    *"Full Disk Access"*) pass "permission: summary advises granting Full Disk Access" ;;
    *) fail "permission: summary missing the Full Disk Access advice (got: $out)" ;;
esac

# ---- scan-time permission denials from fclones are folded into one counted note
# with Full Disk Access advice, not streamed per-folder; --verbose restores the
# raw fclones lines. This is the OTHER permission layer from the engine case above
# and the common one: fclones fails at readdir on an unreadable folder (a
# TCC-protected folder on a run without Full Disk Access), so those paths never
# enter the JSON -- the wrapper, not the engine, must summarize them. A real
# duplicate pair sits alongside so the run still has work to report. Needs real
# fclones. (Non-root, like the rest of the suite -- root bypasses the mode bits.) -
d="$WORK/scanperm"; mkdir -p "$d/scope/readable" "$d/scope/denied"
head -c 1048576 /dev/urandom > "$d/scope/readable/canon"; cp "$d/scope/readable/canon" "$d/scope/readable/dup"
printf 'unreadable\n' > "$d/scope/denied/x"
chmod 000 "$d/scope/denied"            # fclones cannot readdir this -> scan-time denial
err=$("$SCRIPT" --scope "$d/scope" --min 1 2>&1 >/dev/null)
verbose_err=$("$SCRIPT" --scope "$d/scope" --min 1 --verbose 2>&1 >/dev/null)
chmod 755 "$d/scope/denied"            # restore before asserting (and so cleanup can recurse)
# Default: the raw per-folder "Permission denied" line is suppressed...
case "$err" in
    *"Permission denied"*) fail "scan-perm: raw fclones permission line leaked without --verbose (got: $err)" ;;
    *) pass "scan-perm: raw per-folder denial suppressed by default" ;;
esac
# ...and replaced by one counted note that advises granting Full Disk Access.
case "$err" in
    *"could not be read"*"Full Disk Access"*) pass "scan-perm: denials summarized with Full Disk Access advice" ;;
    *) fail "scan-perm: summarized denial note/advice missing (got: $err)" ;;
esac
# --verbose surfaces the raw fclones diagnostic for debugging.
case "$verbose_err" in
    *"Permission denied"*) pass "scan-perm: --verbose restores the raw fclones permission line" ;;
    *) fail "scan-perm: --verbose did not surface the raw fclones line (got: $verbose_err)" ;;
esac

# ---- a hard fclones failure (non-zero exit) is surfaced and propagated, never
# swallowed behind the permission summary. Capturing fclones' stderr to fold the
# denial noise must not also hide a real failure (bad args, crash): stub fclones
# to fail with a diagnostic on stderr; the wrapper must replay that diagnostic and
# exit non-zero, not print a clean summary. ----
d="$WORK/fcfail"; mkdir -p "$d/bin" "$d/scope"
cat > "$d/bin/fclones" <<'EOF'
#!/bin/sh
echo "fclones: error: something broke" >&2
exit 2
EOF
chmod +x "$d/bin/fclones"
rc=0; err=$(PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" 2>&1 >/dev/null) || rc=$?
if [ "$rc" -ne 0 ]; then pass "fcfail: wrapper propagates a non-zero fclones exit"; else fail "fcfail: wrapper swallowed a fclones failure (rc=0)"; fi
case "$err" in
    *"something broke"*) pass "fcfail: fclones' own diagnostic is surfaced, not hidden by the summary" ;;
    *) fail "fcfail: fclones diagnostic lost (got: $err)" ;;
esac

# ---- --exclude globs reach fclones intact (no word-split, no globbing) --
# The wrapper builds fclones's argument vector. An exclude glob with a space
# (e.g. "Application Support") must arrive as ONE argument, and a glob such as
# *.log must be passed literally, not expanded against the cwd. We stub fclones
# to record its argv and emit an empty report (so the real wrapper runs end to
# end through the real engine), invoke it from a cwd that contains a file the
# glob would match, and assert both excludes survived whole.
d="$WORK/excludes"; mkdir -p "$d/bin" "$d/scope"
cat > "$d/bin/fclones" <<EOF
#!/bin/sh
for a in "\$@"; do printf '%s\n' "\$a"; done > "$d/argv"
printf '{"header":{"stats":{}},"groups":[]}\n'
EOF
chmod +x "$d/bin/fclones"
touch "$d/scope/decoy.log"   # *.log would expand to this if globbed in the cwd
( cd "$d/scope" && PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" \
    --exclude "App Support" --exclude '*.log' ) >/dev/null 2>&1 || true
if grep -qxF 'App Support' "$d/argv" 2>/dev/null; then
    pass "excludes: a glob with spaces survives as a single fclones argument"
else
    fail "excludes: spaced glob was split (argv: $(tr '\n' '|' < "$d/argv" 2>/dev/null))"
fi
if grep -qxF '*.log' "$d/argv" 2>/dev/null; then
    pass "excludes: a glob is passed literally, not expanded against the cwd"
else
    fail "excludes: glob was expanded against the cwd (argv: $(tr '\n' '|' < "$d/argv" 2>/dev/null))"
fi

# ---- the plan/actions go to STDOUT, not stderr (so '> file' keeps them) --
# The per-file report is the dry-run's primary output; it must be on stdout, or a
# user redirecting with '> plan.txt' loses everything but the summary (progress
# and warnings stay on stderr). Capture stdout only (2>/dev/null) and assert the
# plan line is there; apply records each clone on stdout too, for an audit trail.
d="$WORK/stdout"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/dup"
plan=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" 2>/dev/null)
case "$plan" in
    *"would clone "*"$d/dup"*) pass "dry-run: the plan is on stdout (captured by > file)" ;;
    *) fail "dry-run: plan missing from stdout (got: $plan)" ;;
esac
head -c 1048576 /dev/urandom > "$d/canon2"; cp "$d/canon2" "$d/dup2"
applied=$(report "$d/canon2" "$d/dup2" | python3 "$ENGINE" --apply 2>/dev/null)
case "$applied" in
    *"cloned "*"$d/dup2"*) pass "apply: each clone is recorded on stdout (audit trail)" ;;
    *) fail "apply: clone not recorded on stdout (got: $applied)" ;;
esac

# ---- the wrapper consumes real fclones JSON and feeds the apply engine ----
# The direct engine cases above deliberately hand-build fclones-shaped reports so
# they can focus on apply invariants. This smoke covers the separate external
# contract: the real wrapper command, fclones JSON output, and engine stdin path
# must still compose end to end.
d="$WORK/real-fclones"; mkdir "$d"
head -c 4096 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/dup"
dry=$("$SCRIPT" --scope "$d" --min 1 2>/dev/null)
case "$dry" in
    *"would clone "*"$d/canon"*"$d/dup"*|*"would clone "*"$d/dup"*"$d/canon"*)
        pass "wrapper: real fclones dry-run reports the duplicate pair" ;;
    *) fail "wrapper: real fclones dry-run did not report the duplicate pair (got: $dry)" ;;
esac
a_before=$(ino "$d/canon"); b_before=$(ino "$d/dup")
applied=$("$SCRIPT" --apply --scope "$d" --min 1 2>/dev/null)
if cmp -s "$d/canon" "$d/dup" \
   && { [ "$(ino "$d/canon")" != "$a_before" ] || [ "$(ino "$d/dup")" != "$b_before" ]; }; then
    pass "wrapper: real fclones apply preserves bytes and replaces one duplicate"
else
    fail "wrapper: real fclones apply did not clone a duplicate (got: $applied)"
fi

# ---- _human formats sizes at the unit boundaries, incl. the GiB->TiB
# fall-through (>= 1024 GiB must stay labelled TiB -- the last unit -- not fall
# off the table). Guards the loop's terminal case; pure formatting, so the
# function is checked by direct import rather than through a clone. ----
rc=0
PYTHONPATH="$HERE/../lib" python3 - <<'PY' || rc=$?
import apply, sys
cases = {
    0: "0.0 B", 1023: "1023.0 B", 1024: "1.0 KiB", 1048576: "1.0 MiB",
    1073741824: "1.0 GiB", 1099511627776: "1.0 TiB",
    1024 * 1099511627776: "1024.0 TiB",   # 1 PiB: stays in TiB, not off-table
}
bad = [v for n, v in cases.items() if apply._human(n) != v]
sys.exit(0 if not bad else 4)
PY
if [ "$rc" -eq 0 ]; then
    pass "format: _human unit boundaries and the GiB->TiB fall-through"
else
    fail "format: _human boundary formatting is wrong"
fi

# ---- a duplicate already cloned on an earlier run is detected via its
# physical extent and left untouched -- not re-cloned -- with its space reported
# as already-saved, in apply and dry-run alike (via F_LOG2PHYS_EXT). This
# is the whole point of the re-run check: a second sweep must reclaim nothing
# new and must not churn the inode, and the dry-run must project the same so it
# never promises space a real re-run can't deliver. macOS `cp` (no -c) makes an
# independent copy, so the FIRST run genuinely clones -- guarding against a
# false positive that would skip fresh duplicates. ----
d="$WORK/alreadyshared"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"; cp "$d/canon" "$d/dup"
out1=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply 2>/dev/null)
ino1=$(ino "$d/dup")
case "$out1" in
    *"already saved"*) fail "already-shared: first run wrongly flagged a fresh copy as already-shared (got: $out1)" ;;
    *"reclaimed: 1.0 MiB allocated (1.0 MiB logical) across 1 files"*) pass "already-shared: first run clones a fresh duplicate (no false positive)" ;;
    *) fail "already-shared: first run did not clone (got: $out1)" ;;
esac
# Second apply: dup now shares canon's extents. Skip it (inode stable), reclaim
# nothing, report the space as already saved.
out2=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply 2>/dev/null)
assert_eq "already-shared: re-run does not re-clone (inode stable)" "$(ino "$d/dup")" "$ino1"
case "$out2" in
    *"already saved by earlier clones: 1.0 MiB allocated (1.0 MiB logical) across 1 files"*) pass "already-shared: re-run reports space already saved" ;;
    *) fail "already-shared: re-run missed the already-saved report (got: $out2)" ;;
esac
case "$out2" in
    *"reclaimed: 0.0 B allocated (0.0 B logical) across 0 files"*) pass "already-shared: re-run reclaims nothing new" ;;
    *) fail "already-shared: re-run double-counted the reclaim (got: $out2)" ;;
esac
# Dry-run on the already-cloned pair: same projection, so it doesn't over-promise.
dry=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" 2>/dev/null)
case "$dry" in
    *"already saved by earlier clones: 1.0 MiB allocated (1.0 MiB logical) across 1 files"*) pass "already-shared: dry-run reports space already saved" ;;
    *) fail "already-shared: dry-run missed the already-saved report (got: $dry)" ;;
esac
case "$dry" in
    *"would reclaim: 0.0 B allocated (0.0 B logical) across 0 files"*) pass "already-shared: dry-run projects ~0 further reclaimable" ;;
    *) fail "already-shared: dry-run over-projected reclaimable space (got: $dry)" ;;
esac

# ---- sparse files report allocated reclaim separately from logical bytes ----
# A sparse duplicate can have a large logical size while holding no allocated
# blocks. Counting only st_size would make the primary reclaim figure look like
# disk space that never existed, so the summary must lead with allocated bytes
# and keep the fclones/logical byte count as secondary context. The canonical is
# dense so the all-hole duplicate's map differs from it -- a would-clone, not the
# identical-map already-shared case the next test covers.
d="$WORK/sparse"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/canon"                            # dense 1 MiB canonical
dd if=/dev/zero of="$d/dup" bs=1 count=0 seek=1048576 2>/dev/null    # all-hole dup: 1 MiB logical, 0 blocks
dry=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" 2>/dev/null)
case "$dry" in
    *"would reclaim: 0.0 B allocated (1.0 MiB logical) across 1 files"*) pass "accounting: sparse duplicate reports allocated bytes before logical bytes" ;;
    *) fail "accounting: sparse duplicate over-reported physical reclaim (got: $dry)" ;;
esac

# ---- a partially-shared file is never trusted as already-shared
# (regression guard for the partial-share false-positive). A clone whose later
# range is CoW-broken by an identical-bytes rewrite shares its FIRST extent but
# has a private rewritten extent, yet stays byte-identical -- so fclones still
# groups it. Trusting that would skip it forever and over-report its full size as
# saved. The engine compares the FULL extent map, so the diverged extent makes
# `multi`'s map differ from the original's: a mismatch it clones (re-sharing the
# broken range), never a trusted already-shared. ----
d="$WORK/partialshare"; mkdir "$d"
head -c 1048576 /dev/urandom > "$d/single"        # one contiguous extent
cp -c "$d/single" "$d/multi"                       # full clone, then...
python3 - "$d/multi" <<'PY'                         # ...CoW-break an interior block with its OWN bytes
import os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
off = 512 * 1024
os.pwrite(fd, os.pread(fd, 4096, off), off)         # identical bytes back -> CoW break, still byte-identical
os.fsync(fd); os.close(fd)
PY
if cmp -s "$d/single" "$d/multi"; then
    pass "partial-share: identical-bytes rewrite keeps the file byte-identical (so fclones still groups it)"
else
    fail "partial-share: rewrite changed bytes -- test premise invalid"
fi
rc=0
PYTHONPATH="$HERE/../lib" python3 - "$d/single" "$d/multi" <<'PY' || rc=$?
import apply, os, sys
single = apply._extent_map_of(sys.argv[1])   # one contiguous extent
multi = apply._extent_map_of(sys.argv[2])    # partially CoW-broken -> a DIFFERENT map
# A multi-extent file now maps cleanly (no longer "unknown"), but the diverged
# extent gives `multi` a different map than `single`, so the engine sees a
# mismatch and clones it rather than trusting it as already-shared. The identity
# leads with the device id, so an offset -- meaningful only within its device --
# cannot be mistaken for sharing across volumes.
ok = (single is not None and multi is not None and multi != single
      and single[0] == os.stat(sys.argv[1]).st_dev)
sys.exit(0 if ok else 5)
PY
if [ "$rc" -eq 0 ]; then
    pass "partial-share: a partially CoW-broken clone has a divergent map (mismatch -> cloned, not trusted)"
else
    fail "partial-share: partial clone wrongly matched or device-blind (could over-report/skip)"
fi

# ---- a genuinely multi-extent clone (fragmented/sparse) IS recognized as
# already-shared, so a re-run leaves it alone instead of re-cloning it and
# re-counting its size as reclaimed. Regression guard for the multi-extent blind
# spot: before the full-extent-map check the engine trusted only a single
# whole-file extent, so every large (multi-extent) file -- and any sparse file --
# was re-cloned every run and re-counted. F_PUNCHHOLE forces a deterministic
# multi-extent layout (data | hole | data); `cp -c` clones it, and clonefile
# reproduces the whole map, hole included, so the clone's map equals the
# canonical's and the engine must report it as already-saved, reclaiming nothing.
d="$WORK/multiextent"; mkdir "$d"
python3 - "$d/canon" <<'PY'
import fcntl, os, struct, sys
F_PUNCHHOLE = 99
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
os.write(fd, b"\xab" * (16 * 1024 * 1024))
# struct fpunchhole { uint32 fp_flags; uint32 reserved; off_t offset; off_t length };
# offset and length are block-aligned, leaving data | 8 MiB hole | data.
fcntl.fcntl(fd, F_PUNCHHOLE, struct.pack("=IIqq", 0, 0, 4 * 1024 * 1024, 8 * 1024 * 1024))
os.fsync(fd); os.close(fd)
PY
cp -c "$d/canon" "$d/dup"                          # clonefile: dup shares canon's full map, hole included
# Premise: canon really is multi-extent and the clone's map matches it -- a
# single-extent file would have passed the old check too and proven nothing.
rc=0
PYTHONPATH="$HERE/../lib" python3 - "$d/canon" "$d/dup" <<'PY' || rc=$?
import apply, sys
canon = apply._extent_map_of(sys.argv[1])
dup = apply._extent_map_of(sys.argv[2])
ok = canon is not None and len(canon[1]) > 1 and dup == canon
sys.exit(0 if ok else 7)
PY
assert_eq "multi-extent: premise (canon is multi-extent; clone's map matches)" "$rc" "0"
ino_before=$(ino "$d/dup")
out=$(report "$d/canon" "$d/dup" | python3 "$ENGINE" --apply 2>/dev/null)
assert_eq "multi-extent: re-run leaves the multi-extent clone untouched (inode stable)" "$(ino "$d/dup")" "$ino_before"
case "$out" in
    *"already saved by earlier clones: "*" across 1 files"*) pass "multi-extent: a fragmented/sparse clone is recognized as already-shared" ;;
    *) fail "multi-extent: fragmented/sparse clone NOT recognized -- would be re-cloned and re-counted (got: $out)" ;;
esac
case "$out" in
    *"reclaimed: 0.0 B allocated (0.0 B logical) across 0 files"*) pass "multi-extent: re-run reclaims nothing new (no double-count)" ;;
    *) fail "multi-extent: re-run re-counted the clone as reclaimed (got: $out)" ;;
esac

# ---- cloud-backed roots are excluded from the scan by default, so a
# broad run never reads -- and re-downloads -- evicted iCloud Drive / File
# Provider / Photos content; --include-cloud opts back in. The download would
# happen at fclones read-time, so the protection must be a scan exclude (not just
# an apply-time skip). Stub fclones to record its argv and assert the defaults
# are present, and dropped under --include-cloud. ----
d="$WORK/cloud"; mkdir -p "$d/bin" "$d/scope"
cat > "$d/bin/fclones" <<EOF
#!/bin/sh
for a in "\$@"; do printf '%s\n' "\$a"; done > "$d/argv"
printf '{"header":{"stats":{}},"groups":[]}\n'
EOF
chmod +x "$d/bin/fclones"
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" >/dev/null 2>&1 || true
if grep -qxF '**/Library/Mobile Documents/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/Library/CloudStorage/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/*.photoslibrary/**' "$d/argv" 2>/dev/null; then
    pass "dataless: cloud roots (iCloud Drive, CloudStorage, Photos) are excluded from the scan by default"
else
    fail "dataless: default cloud excludes missing (argv: $(tr '\n' '|' < "$d/argv" 2>/dev/null))"
fi
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" --include-cloud >/dev/null 2>&1 || true
if grep -qxF '**/Library/Mobile Documents/**' "$d/argv" 2>/dev/null; then
    fail "dataless: --include-cloud should drop the default cloud excludes but did not"
else
    pass "dataless: --include-cloud opts back into scanning the cloud roots"
fi

# ---- a dataless (cloud-evicted) file is detected from the SF_DATALESS
# st_flags bit alone -- never by reading it, which would fault it down from the
# cloud -- and is not confused with an unrelated flag like UF_COMPRESSED. This is
# the apply-path backstop's detection logic (full skip behavior needs a real File
# Provider to exercise, so the flag logic is what we unit-test). ----
rc=0
PYTHONPATH="$HERE/../lib" python3 - <<'PY' || rc=$?
import apply, sys
SF = 0x40000000          # SF_DATALESS
UF_COMPRESSED = 0x20     # a different st_flags bit, must NOT read as dataless
cases = {0: False, SF: True, UF_COMPRESSED: False, SF | UF_COMPRESSED: True}
bad = [hex(f) for f, want in cases.items() if apply._is_dataless(f) != want]
sys.exit(0 if not bad else 6)
PY
if [ "$rc" -eq 0 ]; then
    pass "dataless: SF_DATALESS detected from st_flags (and not confused with UF_COMPRESSED)"
else
    fail "dataless: flag detection is wrong"
fi

# ---- the --git preset lowers --min to 1 -- every non-empty file frees
# a whole 4 KiB block when deduped, so there is no useful floor for git-/CI-heavy
# trees -- and an explicit --min still overrides it, in either order.
# Stub fclones to report the --min value it actually received. ----
d="$WORK/gitpreset"; mkdir -p "$d/bin" "$d/scope"
cat > "$d/bin/fclones" <<EOF
#!/bin/sh
prev=""; for a in "\$@"; do [ "\$prev" = "--min" ] && printf 'min=%s\n' "\$a"; prev="\$a"; done > "$d/argv"
printf '{"header":{"stats":{}},"groups":[]}\n'
EOF
chmod +x "$d/bin/fclones"
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" --git >/dev/null 2>&1 || true
assert_eq "min: --git preset lowers --min to 1" "$(cat "$d/argv")" "min=1"
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" --git --min 4096 >/dev/null 2>&1 || true
assert_eq "min: explicit --min after --git wins" "$(cat "$d/argv")" "min=4096"
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" --min 4096 --git >/dev/null 2>&1 || true
assert_eq "min: explicit --min before --git wins (order-independent)" "$(cat "$d/argv")" "min=4096"

# ---- after --apply, the tool warns that local Time Machine snapshots
# may still hold the reclaimed space (a common source of confusion) -- a note
# only, on stderr, never deleting snapshots; and no note when none are present.
# Stub fclones (empty report) and tmutil to control whether snapshots "exist". ----
d="$WORK/snapnote"; mkdir -p "$d/bin" "$d/scope"
cat > "$d/bin/fclones" <<'EOF'
#!/bin/sh
printf '{"header":{"stats":{}},"groups":[]}\n'
EOF
chmod +x "$d/bin/fclones"
cat > "$d/bin/tmutil" <<'EOF'
#!/bin/sh
echo "com.apple.TimeMachine.2026-05-25-020000.local"
EOF
chmod +x "$d/bin/tmutil"
note=$(PATH="$d/bin:$PATH" "$SCRIPT" --apply --scope "$d/scope" 2>&1 >/dev/null)
case "$note" in
    *thinlocalsnapshots*) pass "snapshots: --apply warns that local snapshots may hold the reclaimed space" ;;
    *) fail "snapshots: apply did not surface the snapshot note (got: $note)" ;;
esac
cat > "$d/bin/tmutil" <<'EOF'
#!/bin/sh
EOF
note2=$(PATH="$d/bin:$PATH" "$SCRIPT" --apply --scope "$d/scope" 2>&1 >/dev/null)
case "$note2" in
    *thinlocalsnapshots*) fail "snapshots: emitted the note when no local snapshots exist (got: $note2)" ;;
    *) pass "snapshots: no note when there are no local snapshots" ;;
esac

# ---- app-private Library data (Mail/Messages/Safari/Containers) is
# excluded from the scan by default -- it is TCC-protected (so scanning prompts
# for or is denied access) and a poor dedup target -- and --include-app-data opts
# back in. Same flag-gated argv construction as the cloud excludes, an
# independent branch with a real consequence (privacy + prompt noise), so stub
# fclones to record its argv and assert the defaults are present, then dropped
# under the flag. ----
d="$WORK/appdata"; mkdir -p "$d/bin" "$d/scope"
cat > "$d/bin/fclones" <<EOF
#!/bin/sh
for a in "\$@"; do printf '%s\n' "\$a"; done > "$d/argv"
printf '{"header":{"stats":{}},"groups":[]}\n'
EOF
chmod +x "$d/bin/fclones"
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" >/dev/null 2>&1 || true
if grep -qxF '**/Library/Mail/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/Library/Messages/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/Library/Safari/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/Library/Containers/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/Library/Group Containers/**' "$d/argv" 2>/dev/null; then
    pass "app-data: Mail/Messages/Safari/Containers excluded from the scan by default"
else
    fail "app-data: default app-private excludes missing (argv: $(tr '\n' '|' < "$d/argv" 2>/dev/null))"
fi
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" --include-app-data >/dev/null 2>&1 || true
if grep -qxF '**/Library/Mail/**' "$d/argv" 2>/dev/null; then
    fail "app-data: --include-app-data should drop the default app-private excludes but did not"
else
    pass "app-data: --include-app-data opts back into scanning app-private Library data"
fi

# ---- OS-managed ~/Library data and the Trash are excluded from the scan too --
# The machine-generated, churning, TCC-protected stores beside the app-private
# ones (Spotlight index, on-device intelligence, daemon containers) and the Trash
# (files pending deletion) are never useful dedup targets. --include-app-data
# re-includes the ~/Library set; the Trash exclude is unconditional. Stub fclones
# to record argv and assert both. ----
d="$WORK/osdata"; mkdir -p "$d/bin" "$d/scope"
cat > "$d/bin/fclones" <<EOF
#!/bin/sh
for a in "\$@"; do printf '%s\n' "\$a"; done > "$d/argv"
printf '{"header":{"stats":{}},"groups":[]}\n'
EOF
chmod +x "$d/bin/fclones"
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" >/dev/null 2>&1 || true
if grep -qxF '**/Library/Metadata/CoreSpotlight/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/Library/Daemon Containers/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/.Trash/**' "$d/argv" 2>/dev/null \
   && grep -qxF '**/.Trashes/**' "$d/argv" 2>/dev/null; then
    pass "system-data: OS-managed Library trees and the Trash excluded by default"
else
    fail "system-data: default system/Trash excludes missing (argv: $(tr '\n' '|' < "$d/argv" 2>/dev/null))"
fi
PATH="$d/bin:$PATH" "$SCRIPT" --scope "$d/scope" --include-app-data >/dev/null 2>&1 || true
if grep -qxF '**/Library/Metadata/CoreSpotlight/**' "$d/argv" 2>/dev/null; then
    fail "system-data: --include-app-data should drop the OS-managed excludes but did not"
elif grep -qxF '**/.Trash/**' "$d/argv" 2>/dev/null; then
    pass "system-data: --include-app-data re-includes Library data but the Trash stays excluded"
else
    fail "system-data: Trash exclude should be unconditional but was dropped"
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "all tests passed"; else echo "some tests FAILED"; fi
exit "$FAILED"
