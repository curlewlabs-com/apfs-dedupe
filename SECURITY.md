# Security Policy

`apfs-dedupe` runs as root and rewrites files in place across user home
directories, so its safety properties are part of its security surface. The
apply is symlink-safe and anchored to a directory file descriptor, content is
re-verified against a frozen clone immediately before each atomic swap, and a
crash leaves the original file untouched. The README "Safety" section and
[`docs/architecture.md`](docs/architecture.md) document the full threat model.

## Supported versions

This is a pre-1.0 project: fixes land on `main` and in the latest tagged
release. There are no maintained older release branches.

## Reporting a vulnerability

Report security issues **privately** — do not open a public issue. Use GitHub's
private vulnerability reporting: open the repository's **Security** tab and
choose **"Report a vulnerability"**, which opens a private advisory visible only
to the maintainers.

Helpful details to include:

- macOS version (`sw_vers -productVersion`) and Apple silicon vs Intel
- The exact command, including whether it ran with `--apply` and as root
- What you observed versus expected
- A minimal reproduction, if you have one

This is a small project, so responses are best-effort rather than on a fixed
SLA; we will acknowledge and work toward a fix as soon as we reasonably can.

## Scope

In scope: path resolution and symlink handling during the apply, privilege
boundaries when running as root over multi-user trees, metadata/ACL fidelity,
and the scheduled LaunchAgent/LaunchDaemon install. The `--system` daemon's use
of install-time tool paths is a documented limitation (README "Schedule a daily
run"), not a separate vulnerability.
