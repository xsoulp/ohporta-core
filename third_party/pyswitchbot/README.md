# Vendored PySwitchbot

The complete `switchbot/` Python package at the repository root is copied,
without source changes, from the PyPI PySwitchbot 2.7.0 wheel. Keeping the
package beside the command scripts makes their existing `switchbot` imports
resolve to this repository's source, including when the scripts are launched
by absolute path from another working directory.

- Upstream: https://github.com/sblibs/pySwitchbot
- Release: https://pypi.org/project/PySwitchbot/2.7.0/
- Artifact: `pyswitchbot-2.7.0-py3-none-any.whl`
- Artifact SHA-256: `33510d23b66710aec357c86a54eb8701a4cdc02955d9a18cd83366a806b53bec`
- Imported: 2026-10-27
- License: MIT; the original copyright and permission notice are in [LICENSE](LICENSE).
- Original release metadata and upstream description: [UPSTREAM-METADATA](UPSTREAM-METADATA).

No upstream repository checkout, submodule, PySwitchbot package download, or
automatic source update is needed to install or run this project. The runtime
dependencies from the wheel metadata are listed in the root `requirements.txt`;
installing those still requires a package index or locally cached packages.

Maintain the code here as needed. If deliberately importing another upstream
release, review its source and dependency changes, preserve its license, and
update this provenance record and `UPSTREAM-METADATA`. Record local source
changes below so they remain distinguishable from the imported release.

## Local source changes

None.
