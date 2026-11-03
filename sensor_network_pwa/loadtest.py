"""Small dependency-free HTTP load probe for the portal's hot read routes."""

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def probe(url: str, timeout: float) -> tuple[float, int]:
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            response.read()
            status = response.status
    except HTTPError as error:
        status = error.code
    return time.perf_counter() - started, status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("station")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    urls = [
        f"{base}/api/public/station/{args.station}/snapshot?window=hour",
        f"{base}/public/station/{args.station}",
    ]
    for url in urls:
        samples = []
        statuses = []
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = [pool.submit(probe, url, args.timeout) for _ in range(max(1, args.requests))]
            for future in as_completed(futures):
                elapsed, status = future.result()
                samples.append(elapsed * 1000)
                statuses.append(status)
        samples.sort()
        p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
        failures = sum(status >= 400 for status in statuses)
        print(
            f"{url}: requests={len(samples)} failures={failures} "
            f"median_ms={statistics.median(samples):.1f} p95_ms={p95:.1f} max_ms={max(samples):.1f}"
        )
        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
