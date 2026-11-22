"""Take the guide's screenshots (docs/guide/*.jpg) from a running copy of the app.

Use demo data, never your own: run the app on the test suite's fictional companies (tests/synthetic.py), e.g.

    ORACLE_EDGE_DATA=/tmp/oe_demo ORACLE_EDGE_PORT=8799 python -m oracle_edge
    python tools/guide_screenshots.py http://127.0.0.1:8799 CSC

Needs Playwright with a Chromium-based browser (set CHROME to its path if it isn't found)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "guide"


def main(base: str, ticker: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    opts = {"executable_path": os.environ["CHROME"]} if os.environ.get("CHROME") else {}
    with sync_playwright() as p:
        b = p.chromium.launch(**opts)
        pg = b.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)

        def shot(name, loc=None, full=False):
            path = OUT / f"{name}.jpg"
            if loc is not None:
                loc.scroll_into_view_if_needed()
                pg.wait_for_timeout(400)
                loc.screenshot(path=path, type="jpeg", quality=82)
            else:
                pg.screenshot(path=path, type="jpeg", quality=82, full_page=full)
            print("wrote", path.name)

        card = lambda text: pg.locator(".oe-card", has_text=text).first  # noqa: E731

        pg.goto(f"{base}/")
        pg.wait_for_timeout(4000)
        shot("today")

        pg.goto(f"{base}/screener")
        pg.wait_for_timeout(6000)
        shot("screener-map", card("Quality vs Wonderfulness"))
        grid = pg.locator(".ag-root-wrapper").first
        shot("screener-table", grid)

        pg.goto(f"{base}/stock/{ticker}")
        pg.wait_for_timeout(5000)
        shot("stock-worth", card("What it's worth"))
        shot("stock-targets", card("Price targets"))
        shot("stock-chart", card("Price vs value"))
        for tab, name, wait in (("Wonderful?", "stock-wonderful", 6000), ("Total return", "stock-total-return", 5000)):
            pg.get_by_role("tab", name=tab).click()
            pg.wait_for_timeout(wait)
            shot(name)

        pg.goto(f"{base}/market")
        pg.wait_for_timeout(5000)
        shot("market-buffett", card("US Buffett Indicator, market value and GDP"))
        b.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765", sys.argv[2] if len(sys.argv) > 2 else "CSC")
