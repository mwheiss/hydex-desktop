# COPR source-pipeline fixture

The live `hydex-desktop` package is published in the unified `mheiss/hydex`
COPR project from uploaded, prebuilt SRPMs. Each SRPM reconstructs its already
validated native Desktop payload tier in RHEL/EPEL 7-10 builders with
network access disabled; COPR does not download the upstream package, rerun
patchers, or compile Rust. EL10 uses the full-updater payload, EL8/9 use the
private-runtime compatibility payload, and EL7 uses the split RPM4/gzip pair.

This checked-in `.copr` directory remains a source-pipeline compatibility
fixture. Its deliberately synthetic MIT package exercises signed-artifact
selection, the ordinary installer, feature patching, Hydex injection, and RHEL
compatibility paths. Rejection at the dummy ASAR boundary is expected. Do not
configure the live package to use this SCM fixture.

The maintained release procedure, package-source migration rule, and readback
gates live in
`.codex/skills/hydex-plugin-refresh/references/copr.md` in the Hydex source
repository.
