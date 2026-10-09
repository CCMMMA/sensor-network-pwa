# Continuous integration and delivery

`.github/workflows/ci-cd.yml` runs on pull requests to `main`, pushes to `main`, and
manual dispatch.

- Python 3.10–3.14: install `requirements.txt`, `pip check`, compile, run the unittest suite.
- After those pass: build the Docker image, check `python main.py --help`, and run the
  tests inside the image.
- Only a successful push to `main` publishes:

```text
ghcr.io/ccmmma/sensor-network-pwa:latest
ghcr.io/ccmmma/sensor-network-pwa:sha-<full-commit-sha>
```

Publication uses the built-in `GITHUB_TOKEN` with `packages: write`. Delivery ends at
the registry; rolling an image out to a host is a separate operator action. To use a
published image, replace the `build` blocks of the `web` and `watchdog` services with
the same `image:` reference, then `docker compose pull && docker compose up -d --no-build`.
