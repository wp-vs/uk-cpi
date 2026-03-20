"""Download latest 3-month SONIA futures settlement prices from ICE."""

import argparse
import json
import sys
from datetime import datetime

import pandas as pd
import requests

# ICE product ID for Three Month SONIA Index Futures
PRODUCT_ID = 68361266
HUB_ID = 2606

ICE_API_URL = (
    "https://www.ice.com/marketdata/DelayedMarkets.shtml"
    f"?getContractsAsJson&productId={PRODUCT_ID}&hubId={HUB_ID}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": (
        f"https://www.ice.com/products/{PRODUCT_ID}/"
        "Three-Month-SONIA-Index-Futures/data"
    ),
}


def fetch_sonia_futures() -> pd.DataFrame:
    """Fetch latest 3-month SONIA futures settlement prices from ICE.

    Returns a DataFrame with columns: contract_month, settlement_price,
    last_price, volume, open_interest, change.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # First hit the product page to establish cookies/session
    product_url = (
        f"https://www.ice.com/products/{PRODUCT_ID}/"
        "Three-Month-SONIA-Index-Futures/data"
    )
    session.get(product_url, timeout=15)

    # Now fetch the JSON data
    resp = session.get(ICE_API_URL, timeout=15)
    resp.raise_for_status()

    data = resp.json()

    rows = []
    for contract in data:
        row = {
            "contract_month": contract.get("marketStrip", ""),
            "market_id": contract.get("marketId", ""),
            "last_price": _parse_price(contract.get("lastPrice")),
            "settlement_price": _parse_price(contract.get("settlementPrice")),
            "change": _parse_float(contract.get("change")),
            "volume": _parse_int(contract.get("volume")),
            "open_interest": _parse_int(contract.get("openInterest")),
            "last_time": contract.get("lastTime", ""),
        }
        rows.append(row)

    df = pd.DataFrame(rows)

    if not df.empty:
        df = df.sort_values("contract_month").reset_index(drop=True)

    return df


def _parse_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_float(value) -> float | None:
    if value is None:
        return None
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
        "-o", "--output",
        help="Output CSV file path (default: print to stdout)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of CSV",
    )
    args = parser.parse_args()

    try:
        df = fetch_sonia_futures()
    except requests.RequestException as e:
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
