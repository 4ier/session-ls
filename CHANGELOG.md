# Changelog

## 0.2.1

- Carries the versioned `session_ls.api` and `session_ls.storage` modules, the
  literal decoded-text search, and the parser fixes that had been developed in the
  4top checkout: Claude 2.1.x metadata-only session files are recognized instead of
  rejected, a non-directory matched by a history pattern is skipped instead of
  reported as an unavailable source, and both parsers carry `PARSER_VERSION` 2.
- The 0.2.0 release on PyPI was built from that checkout and its metadata pointed
  here; this release is built from this repository, which is now the package's home.
- `session-ls` is unchanged as an interface: six JSON fields, no dependencies.

## 0.1.0

- First release: list and search session history across pi, codex, claude and
  cursor, with full-text search and an independent, stdlib-only CLI.
