# Changelog

## 0.2.0 - 2026-02-19
- Added cross-platform service automation:
  - `scripts/install-service.sh`
  - `scripts/status-service.sh`
  - `scripts/uninstall-service.sh`
  - Linux `systemd --user` scripts
- Added bootstrap and release scripts:
  - `scripts/bootstrap.sh`
  - `scripts/release-build.sh`
- Generalized service labels and removed user-specific hardcoding.
- Made `WORKSPACE_ROOT` default portable (`~/Projects` if exists, else `~`).
- Expanded deployment/security/SEO documentation:
  - `docs/DEPLOYMENT.md`
  - `docs/SECURITY.md`
  - `docs/SEO.md`
- Updated package version to `0.2.0`.
