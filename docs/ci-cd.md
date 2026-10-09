# Continuous integration and delivery

`.github/workflows/ci-cd.yml` runs on pull requests to and pushes to `main` and
`development`, and on manual dispatch.

- **Lint, format and types** (Python 3.12): `ruff check`, `ruff format --check` and `mypy`,
  with the tools from `requirements-dev.txt` and the settings in `pyproject.toml`.
- **Python 3.10–3.14**: install `requirements.txt`, `pip check`, compile, run the unittest suite.
- **Container**, after both pass: build the Docker image, check `python main.py --help`, run the
  tests inside the image, then start it with Gunicorn and fetch the home, login and offline
  pages, the manifest, the service worker and a static script.
- A successful push publishes the tested image:

| Branch | Tags |
| --- | --- |
| `main` | `ghcr.io/ccmmma/sensor-network-pwa:latest`, `:sha-<full-commit-sha>` |
| `development` | `ghcr.io/ccmmma/sensor-network-pwa:development`, `:sha-<full-commit-sha>` |

Pull requests are built and tested but never published.

Publication uses the built-in `GITHUB_TOKEN` with `packages: write`. Delivery ends at
the registry; rolling an image out to a host is a separate operator action. To use a
published image, replace the `build` blocks of the `web` and `watchdog` services with
the same `image:` reference, then `docker compose pull && docker compose up -d --no-build`.
