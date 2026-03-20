"""Download latest 3-month SONIA futures settlement prices from ICE."""

import argparse
import json
import re
import sys

import pandas as pd

# ICE product page for Three Month SONIA Index Futures
PRODUCT_URL = (
    "https://www.ice.com/products/68361266/"
    "Three-Month-SONIA-Index-Futures/data"
)


def fetch_sonia_futures(headless: bool = True) -> pd.DataFrame:
    """Fetch latest 3-month SONIA futures settlement prices from ICE.

    Uses Playwright to render the ICE product page (React SPA) and extract
    the settlement price table.

    Args:
        headless: Run browser in headless mode (default True).

    Returns a DataFrame with columns: contract, last, change, settlement,
    volume, open_interest.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "playwright is required. Install with: "
            "pip install playwright && python -m playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        # Try to intercept the XHR response that loads contract data
        captured_data = []

        def handle_response(response):
            url = response.url
            if "getContractsAsJson" in url or "DelayedMarkets" in url:
                try:
                    captured_data.append(response.json())
                except Exception:
                    pass

        page.on("response", handle_response)
        page.goto(PRODUCT_URL, wait_until="networkidle", timeout=30000)

        # If we captured JSON from the API, use that directly
        if captured_data:
            df = _parse_json_data(captured_data[0])
            browser.close()
            return df

        # Otherwise, fall back to scraping the rendered HTML table
        # Wait for the data table to appear
        page.wait_for_selector("table", timeout=15000)
        html = page.content()
        browser.close()

    return _parse_html_table(html)


def _parse_json_data(data: list | dict) -> pd.DataFrame:
    """Parse contract data from ICE's internal JSON API response."""
    if isinstance(data, dict):
        data = [data]

    rows = []
    for contract in data:
        row = {
            "contract": contract.get("marketStrip", ""),
            "last": _parse_num(contract.get("lastPrice")),
            "change": _parse_num(contract.get("change")),
            "settlement": _parse_num(contract.get("settlementPrice")),
            "volume": _parse_int(contract.get("volume")),
            "open_interest": _parse_int(contract.get("openInterest")),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("contract").reset_index(drop=True)
    return df


def _parse_html_table(html: str) -> pd.DataFrame:
    """Parse the settlement price table from ICE's rendered HTML."""
    tables = pd.read_html(html)
    if not tables:
        raise ValueError("No tables found on the ICE product page")

    # Find the table that looks like a futures settlement table
    for table in tables:
        cols_lower = [str(c).lower() for c in table.columns]
        if any("settle" in c for c in cols_lower) or any(
            "contract" in c for c in cols_lower
        ):
            # Normalise column names
            table.columns = [
                re.sub(r"\s+", "_", str(c).strip().lower())
                for c in table.columns
            ]
            return table

    # If no matching table found, return the largest table
    largest = max(tables, key=len)
    largest.columns = [
        re.sub(r"\s+", "_", str(c).strip().lower()) for c in largest.columns
    ]
    return largest


def _parse_num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Download latest 3-month SONIA futures settlement prices from ICE"
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV file path (default: print to stdout)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of CSV",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (useful for debugging)",
    )
    args = parser.parse_args()

    try:
        df = fetch_sonia_futures(headless=not args.no_headless)
    except Exception as e:
        print(f"Error fetching data: {e}", file=sys.stderr)
        sys.exit(1)

    if df.empty:
        print("No data returned.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        output = df.to_json(orient="records", indent=2)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Saved {len(df)} contracts to {args.output}")
        else:
            print(output)
    else:
        if args.output:
            df.to_csv(args.output, index=False)
            print(f"Saved {len(df)} contracts to {args.output}")
        else:
            print(df.to_csv(index=False), end="")


if __name__ == "__main__":
    main()
