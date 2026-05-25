# Contributing

Thanks for your interest in improving `apfs-dedupe`. It is a focused tool, so the
bar for a change is "does this make safe, correct APFS deduplication better" —
bug fixes, safety hardening, and clearer docs are all welcome.

## Before a large change

Open an issue describing the problem first. The apply path carries a lot of
load-bearing safety invariants (symlink-safe fd-anchored steps, content
re-verification against a frozen clone, crash-safety, ACL fidelity), so a short
discussion up front saves rework.

## Running the checks

These are the same three checks CI runs on every push and PR (see the README
"Development" section):

```sh
sh test/test.sh                              # integration tests — macOS 15+, real clonefile + fclones
npx pyright@1.1.409                          # strict type check of lib/apply.py (CI's exact pin)
shellcheck apfs-dedupe.sh install-daily.sh test/test.sh
```

The integration test needs **macOS 15+** (it exercises real `clonefile` /
`CLONE_NOFOLLOW_ANY`) and `fclones` on `PATH` (`brew install fclones`). The type
check and shellcheck run anywhere.

## Conventions

- **Tests ship with the change.** A bug fix or feature includes a test that
  covers it in the same pull request. CI runs the integration suite on macOS, so
  you do not need a Mac to have your change verified — but you do need to add the
  test.
- **Pin dependencies exactly.** GitHub Actions and tool versions are pinned to
  exact versions (the `pyright` / `shellcheck` / action pins in CI), so a
  resolver cannot silently move them. Match that when adding any.
- **Comments explain _why_, not _what_.** `lib/apply.py` brands path strings as
  `FullPath` vs `Basename` (`NewType`s that pyright enforces in strict mode) so a
  directory-relative component cannot be passed where a resolvable path belongs;
  keep new path handling within that scheme.
- **Safe by default stays the default.** Dry-run is the default; anything that
  modifies files stays gated behind `--apply` and the existing safety checks.

## Submitting

Keep each pull request focused on one change, make sure the three checks pass,
and describe the _why_ in the PR body. CI must be green before merge.
