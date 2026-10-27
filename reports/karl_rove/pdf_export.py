"""PDF export via headless Chromium (Playwright)."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def html_to_pdf(html: str, output_path: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        msg = "install playwright: uv sync --extra reports && playwright install chromium"
        raise RuntimeError(msg) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=str(output_path),
            format="Letter",
            print_background=True,
            display_header_footer=True,
            header_template=(
                "<div style='font-size:8px;width:100%;text-align:center;"
                "color:#555;margin-top:0.15in;'>Karl Rove Report — Confidential</div>"
            ),
            footer_template=(
                "<div style='font-size:7px;width:100%;text-align:center;"
                "color:#777;margin-bottom:0.1in;'>"
                "<span class='pageNumber'></span> / <span class='totalPages'></span>"
                "</div>"
            ),
            margin={"top": "0.85in", "bottom": "0.75in", "left": "0.65in", "right": "0.65in"},
        )
        browser.close()
    logger.info("wrote PDF path=%s bytes=%s", output_path, output_path.stat().st_size)
