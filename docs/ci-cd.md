# Continuous integration and delivery

`.github/workflows/ci-cd.yml` runs on pull requests to and pushes to `main` and
`development`, and on manual dispatch.

- **Pull-request dependency review**: rejects newly introduced vulnerable dependencies.
- **Lint, format, types, and frontend syntax** (Python 3.12 and Node.js 24): `ruff check`,
  `ruff format --check`, `mypy`, and `node --check` for every page script, with the tools
  from `requirements-dev.txt` and the settings in `pyproject.toml`.
- **Dependency audit**: `pip-audit` checks the complete runtime dependency set against
  published vulnerability advisories on every workflow run.
- **Python 3.10–3.14**: install `requirements.txt`, `pip check`, compile, run the unittest suite.
- **Container**, after both pass: fetch the base image (retrying, then from `mirror.gcr.io`, because
  Docker Hub rate-limits shared runners), build the Docker image, check `python main.py --help`, run the
  tests inside the image, then start it with Gunicorn and fetch the home, login and offline
  pages, the manifest, the service worker and a static script. The tested image is then
  scanned for fixable high/critical vulnerabilities, and an SPDX JSON software bill of
  materials is retained as a workflow artifact for 30 days.
- A successful push publishes the tested image:

| Branch | Tags |
| --- | --- |
| `main` | `ghcr.io/ccmmma/sensor-network-pwa:latest`, `:sha-<full-commit-sha>` |
| `development` | `ghcr.io/ccmmma/sensor-network-pwa:development`, `:sha-<full-commit-sha>` |

Pull requests are reviewed, audited, built, scanned, and tested but never published.

Publication uses the built-in `GITHUB_TOKEN` with `packages: write`. Delivery ends at
the registry; rolling an image out to a host is a separate operator action. To use a
published image, replace the `build` blocks of the `web` and `watchdog` services with
the same `image:` reference, then `docker compose pull && docker compose up -d --no-build`.

Dependabot checks Python packages, GitHub Actions, and the Docker base image weekly and
opens update pull requests against `development`, where the complete workflow validates
them before promotion to `main`.
