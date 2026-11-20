"""Shared HTTP client with a simple rate limiter and retries."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx


class RateLimiter:
    def __init__(self, per_second: float):
        self.min_interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class Http:
    def __init__(self, user_agent: str, per_second: float = 5.0, timeout: float = 60.0):
        self.limiter = RateLimiter(per_second)
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
        )

    def get(self, url: str, params: dict | None = None, retries: int = 3) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(retries):
            self.limiter.wait()
            try:
                r = self.client.get(url, params=params)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    last_exc = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    continue
                return r
            except httpx.HTTPError as e:
                last_exc = e
                time.sleep(2 * (attempt + 1))
        raise last_exc or RuntimeError(f"GET failed: {url}")

    def download(self, url: str, dest: Path, progress=None) -> Path:
        """Stream a large file to disk (written to a temp name, renamed when complete)."""
        tmp = dest.with_suffix(dest.suffix + ".part")
        self.limiter.wait()
        with self.client.stream("GET", url, timeout=None) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0)) or None
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done / total)
        tmp.replace(dest)
        return dest
