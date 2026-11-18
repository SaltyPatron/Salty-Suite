# Salty-Suite

Developer and IT tools, each its own product: one implementation, its own version and release, configured by its
callers rather than by names baked into it. Laplace uses them the way any other user does: a pinned release.

| Product | What it does | Version |
|---|---|---|
| [host-lock](host-lock/) | One machine's shared resource held shared or exclusive, with writer preference, deadlines and holder diagnostics (`flock`, POSIX sh) | [`host-lock/VERSION`](host-lock/VERSION) |

A release of a product is a tag `<product>-v<version>`; its tarball is attached to the GitHub Release.
