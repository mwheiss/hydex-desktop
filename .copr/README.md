# COPR source contract

The hydex-desktop COPR package uses this repository as an SCM source with the
make_srpm method. upstream-artifact.json selects one immutable GitHub release
asset and SHA-256. The source-RPM builder embeds that Debian package, the exact
Git tree, and the immutable Hydex runtime before target builds run.

The checked-in entry is deliberately dummy. It points to a synthetic MIT
package and drives the ordinary install.sh, feature-patch, Hydex, and RHEL
compatibility paths. Failure at the synthetic ASAR is expected and is kept
visible in COPR. Do not replace the entry with an OpenAI package unless you
have confirmed redistribution rights for both the GitHub release and the
resulting public COPR repository.
