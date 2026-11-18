# Contributing

Thanks for your interest in improving `apfs-dedupe`. It is a focused tool, so the
bar for a change is "does this make safe, correct APFS deduplication better" -
bug fixes, safety hardening, and clearer docs are all welcome.

## Before a large change

Open an issue describing the problem first. The apply path carries a lot of
load-bearing safety invariants (symlink-safe fd-anchored steps, content
re-verification against a frozen clone, crash-safety, ACL fidelity, and never
deleting an original that has not been verified), so a short
discussion up front saves rework.

## Running the checks

These are the same three checks CI runs on every push and PR (see the README
"Development" section):

```sh
sh test/test.sh                              # integration tests - macOS 15+, real clonefile + fclones
npx pyright@<version>                        # strict type check of lib/apply.py (the release ci.yml pins)
shellcheck apfs-dedupe.sh install-daily.sh test/test.sh
```

`test/test.sh` also runs `test/test_concurrent_writers.py`, which races the apply
against real writers, and `test/test_inspect.py`, which covers the threaded
inspection. The integration test needs **macOS 15+** (it exercises real `clonefile` /
`CLONE_NOFOLLOW_ANY`) and `fclones` on `PATH` (`brew install fclones`). The type
check and shellcheck run anywhere.

## Conventions

- **Tests ship with the change.** A bug fix or feature includes a test that
  covers it in the same pull request. CI runs the integration suite on macOS, so
  you do not need a Mac to have your change verified - but you do need to add the
  test.
- **Pin dependencies exactly.** A GitHub Action is pinned to its release's
  commit SHA with the version beside it, since a tag can be moved, and tools
  are pinned to exact versions (the `pyright` / `shellcheck` pins in CI), so a
  resolver cannot silently move them. Match that when adding any. That
  includes the Rust release CI builds `fclones` with, and `fclones` itself
  (`cargo install fclones --version <version> --locked`). Renovate proposes
  updates to these pins; the `# renovate:` comment above an inline pin tells
  it what the literal is, so a new inline pin needs one.
- **Comments explain _why_, not _what_.** `lib/apply.py` brands path strings as
  `FullPath` vs `Basename` (`NewType`s that pyright enforces in strict mode) so a
  directory-relative component cannot be passed where a resolvable path belongs;
  keep new path handling within that scheme.
- **Safe by default stays the default.** Dry-run is the default; anything that
  modifies files stays gated behind `--apply` and the existing safety checks.
- **Keep tracked text ASCII.** Use plain hyphens, `->`, and words instead of
  Unicode punctuation or decorative symbols.

## Submitting

Keep each pull request focused on one change, make sure the three checks pass,
and describe the _why_ in the PR body. CI must be green before merge.

## Releasing

A patch or minor release needs no approval: whoever cuts it follows the steps
below in order, on a `main` commit whose required checks have passed. A major
version, a 1.0, or a minor before 1.0 that changes an interface consumers use
needs the maintainer's explicit approval before its tag is pushed.

Releases use fixed `vMAJOR.MINOR.PATCH` tags. The repository blocks updates and
deletions of those tags, and publishing the GitHub Release makes its tag and
release assets immutable. After merging the release commit to `main`, create
and push the version tag, then publish that same tag with `gh release create`.
Do not create a moving version alias unless the release contract is explicitly
expanded to support one.

## License

By submitting a contribution for inclusion in this project, you agree that it
is licensed under the MIT and Apache-2.0 licenses, as the README's License
section describes, without any additional terms or conditions.
