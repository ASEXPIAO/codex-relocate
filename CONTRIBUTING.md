# Contributing

Keep the project small: one migration engine, one window, no runtime dependencies or network features.

For a bug report, include Windows/Python or release version, the operation phase, filesystem types, and a reproduction using synthetic files. Redact usernames and project paths. Do not attach application databases, authentication files, or private manifests.

Before opening a pull request, run `python -m unittest discover -s tests -v` on Windows. Changes to copy, switch, rollback or cleanup require a regression test that demonstrates data preservation under failure. Destructive operations must validate ownership and boundaries, and must never traverse a directory link.

Keep English documentation first and Chinese second. Default UI language is English. Add both labels when adding a visible control.
