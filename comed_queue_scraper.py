"""
ComEd Interconnection Queue Scraper
====================================
Scrapes the paginated table from:
  https://www.comed.com/smart-energy/my-green-power-connection/developers-contractors/smaller-generators/interconnection-queue

Output: JSON file (comed_queue_data.json) — ready for Power Automate or Excel.

Dependencies (install once):
    pip install playwright
    python -m playwright install chromium

Usage:
    python comed_queue_scraper.py

    # Optional: specify output path
    python comed_queue_scraper.py --output my_data.json
"""

import json
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    print("ERROR: playwright is not installed.")
    print("Run: pip install playwright && python -m playwright install chromium")
    sys.exit(1)


TARGET_URL = (
    "https://www.comed.com/smart-energy/my-green-power-connection/"
    "developers-contractors/smaller-generators/interconnection-queue"
)

TABLE_SELECTOR = "table"
WAIT_AFTER_NAV_MS = 2000


def scrape_table_on_page(page) -> tuple[list[str], list[dict]]:
    """Extract headers and rows from the visible table."""
    table = page.query_selector(TABLE_SELECTOR)
    if not table:
        return [], []

    # --- Headers ---
    headers = []
    header_cells = table.query_selector_all("thead th, thead td")
    if not header_cells:
        header_cells = table.query_selector_all("tr:first-child th, tr:first-child td")

    for cell in header_cells:
        text = " ".join(cell.inner_text().split())
        headers.append(text)

    # --- Rows ---
    rows = []
    body_rows = table.query_selector_all("tbody tr")
    if not body_rows:
        all_rows = table.query_selector_all("tr")
        body_rows = all_rows[1:] if len(all_rows) > 1 else []

    for tr in body_rows:
        cells = tr.query_selector_all("td")
        if not cells:
            continue
        values = [" ".join(c.inner_text().split()) for c in cells]
        if headers:
            row_dict = {
                headers[i] if i < len(headers) else f"column_{i}": values[i]
                for i in range(max(len(headers), len(values)))
            }
        else:
            row_dict = {f"column_{i}": v for i, v in enumerate(values)}
        rows.append(row_dict)

    return headers, rows


def get_first_row_key(page) -> str:
    """Fingerprint of the first data row — used to detect when page stops changing."""
    try:
        table = page.query_selector(TABLE_SELECTOR)
        if not table:
            return ""
        first_row = table.query_selector("tbody tr:first-child")
        if not first_row:
            return ""
        return first_row.inner_text().strip()
    except Exception:
        return ""


def get_next_button(page):
    """
    Return the Next page button if it exists and is not disabled.
    Uses JavaScript for a thorough disabled-state check.
    """
    candidates = page.query_selector_all("button, a, [role='button']")
    for el in candidates:
        try:
            text = (el.inner_text() or "").strip().lower()
            aria = (el.get_attribute("aria-label") or "").strip().lower()
            title = (el.get_attribute("title") or "").strip().lower()

            is_next = (
                text in ("next", "›", "»", ">", "next page")
                or "next" in aria
                or "next" in title
            )
            if not is_next:
                continue

            if not el.is_visible():
                continue

            # Thorough JS disabled check — covers disabled attr, aria-disabled,
            # CSS class on element AND parent <li> (Bootstrap pagination pattern)
            is_disabled = page.evaluate("""el => {
                if (el.disabled) return true;
                if (el.getAttribute('aria-disabled') === 'true') return true;
                const cls = (el.className || '');
                if (/\\bdisabled\\b/i.test(cls)) return true;
                let p = el.parentElement;
                while (p) {
                    if (/\\bdisabled\\b/i.test(p.className || '')) return true;
                    if (p.tagName === 'NAV' || p.tagName === 'TABLE') break;
                    p = p.parentElement;
                }
                return false;
            }""", el)

            if not is_disabled:
                return el

        except Exception:
            continue

    return None


def scrape(output_path: str = "comed_queue_data.json") -> list[dict]:
    all_rows: list[dict] = []
    headers: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print(f"Loading: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60_000)

        try:
            page.wait_for_selector(TABLE_SELECTOR, timeout=20_000)
        except PlaywrightTimeoutError:
            print("WARNING: No table found within 20s. Page structure may have changed.")
            browser.close()
            return []

        page_num = 1
        prev_first_row = ""

        while True:
            print(f"  Scraping page {page_num}...", end=" ", flush=True)
            h, rows = scrape_table_on_page(page)
            if not headers and h:
                headers = h
            all_rows.extend(rows)
            print(f"{len(rows)} rows collected (total: {len(all_rows)})")

            # --- Stop condition 1: Next button disabled or missing ---
            next_btn = get_next_button(page)
            if not next_btn:
                print("  Next button not found or disabled — done.")
                break

            # --- Stop condition 2: Page content didn't change (loop guard) ---
            current_first_row = get_first_row_key(page)
            if current_first_row and current_first_row == prev_first_row:
                print("  Page content unchanged after navigation — done.")
                break
            prev_first_row = current_first_row

            # Click Next and wait
            next_btn.click()
            page.wait_for_timeout(WAIT_AFTER_NAV_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                pass

            # --- Stop condition 3: First row same as before clicking ---
            new_first_row = get_first_row_key(page)
            if new_first_row and new_first_row == current_first_row:
                print("  Table did not update after clicking Next — last page reached.")
                break

            page_num += 1
            if page_num > 500:
                print("WARNING: Hit 500-page safety cap. Stopping.")
                break

        browser.close()

    # --- Build output ---
    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_url": TARGET_URL,
        "total_records": len(all_rows),
        "columns": headers,
        "data": all_rows,
    }

    out_path = Path(output_path)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(all_rows)} records -> {out_path.resolve()}")
    return all_rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape ComEd Interconnection Queue table")
    parser.add_argument(
        "--output",
        default="comed_queue_data.json",
        help="Output JSON file path (default: comed_queue_data.json)",
    )
    args = parser.parse_args()
    rows = scrape(output_path=args.output)
    if not rows:
        print("No data was collected. See warnings above.")
        sys.exit(1)
