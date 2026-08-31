# Changelog

All notable user-facing changes are documented here.

## 1.0.4 - 2026-08-30

### Fixed

- Prevent uploaded analysis-result directories from accumulating indefinitely; results older than 24 hours are removed when a new uploaded job starts.
- Make GitHub releases immutable and publish them only from a matching version tag.
- Align the analytics methodology with the plugin's agriculture-only public scope.
- Correct UI wording that overstated rate strategy, basemap resolution, and DJI controller readiness.

### Changed

- Remove the retired no-op setup-guide module and committed development patch artifact.
- Add read-only CI permissions, workflow timeouts, release/version validation, and public quality badges.
- Document a direct private vulnerability-reporting channel.
