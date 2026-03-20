"""Download latest 3-month SONIA futures settlement prices.

Primary source: Interactive Brokers via ib_insync (requires TWS/Gateway).
Fallback: Playwright scraping of ICE product page.
"""

import argparse
import calendar
import re
import sys
from datetime import date
from io import StringIO

import pandas as pd

# ICE contract details
SONIA_SYMBOL = "SO3"
SONIA_EXCHANGE = "ICEEU"
SONIA_CURRENCY = "GBP"

# ICE product page (fallback)
PRODUCT_URL = (
    "https://www.ice.com/products/68361266/"
    "Three-Month-SONIA-Index-Futures/data"
)


def fetch_via_ib(
    host: str = "127.0.0.1", port: int = 7497, client_id: int = 1
) -> pd.DataFrame:
    """Fetch SONIA futures settlement prices from Interactive Brokers.

    Requires TWS or IB Gateway running.
    """
    from ib_insync import IB, Future

    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=10)

    try:
        # Use a blank-expiry Future to discover all available contracts
        contract = Future(
            symbol=SONIA_SYMBOL,
            exchange=SONIA_EXCHANGE,
            currency=SONIA_CURRENCY,
        )
        details_list = ib.reqContractDetails(contract)
        if not details_list:
            raise ValueError(f"No contracts found for {SONIA_SYMBOL} on {SONIA_EXCHANGE}")

        rows = []
        for details in details_list:
            c = details.contract
            # Request latest market data snapshot
            ib.qualifyContracts(c)
            ticker = ib.reqMktData(c, snapshot=True)
            ib.sleep(2)  # allow data to arrive

            rows.append(
                {
                    "contract": c.lastTradeDateOrExpiry,
                    "local_symbol": c.localSymbol,
                    "last": ticker.last if ticker.last == ticker.last else None,
                    "close": ticker.close if ticker.close == ticker.close else None,
                    "bid": ticker.bid if ticker.bid == ticker.bid else None,
                    "ask": ticker.ask if ticker.ask == ticker.ask else None,
                    "volume": ticker.volume if ticker.volume == ticker.volume else None,
                }
            )
            ib.cancelMktData(c)

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("contract").reset_index(drop=True)
        return _enrich_with_dates(df)
    finally:
        ib.disconnect()


def fetch_via_playwright(headless: bool = True) -> pd.DataFrame:
    """Fetch SONIA futures settlement prices by scraping ICE's product page.

    Uses Playwright to render the React SPA and intercept API responses
    or parse the rendered HTML table.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        # Try to intercept the XHR response that loads contract data
        captured_data = []

        def handle_response(response):
            if "getContractsAsJson" in response.url or "DelayedMarkets" in response.url:
                try:
                    captured_data.append(response.json())
                except Exception:
                    pass

        page.on("response", handle_response)
        page.goto(PRODUCT_URL, wait_until="networkidle", timeout=30000)

        if captured_data:
            df = _parse_json_data(captured_data[0])
            browser.close()
            return df

        # Fall back to scraping the rendered HTML table
        page.wait_for_selector("table", timeout=15000)
        html = page.content()
        browser.close()

    return _enrich_with_dates(_parse_html_table(html))


def fetch_sonia_futures(
    source: str = "auto",
    headless: bool = True,
    ib_host: str = "127.0.0.1",
    ib_port: int = 7497,
    ib_client_id: int = 1,
) -> pd.DataFrame:
    """Fetch SONIA futures prices.

    Args:
        source: "ib" for Interactive Brokers only, "ice" for ICE scraping
            only, or "auto" to try IB first then fall back to ICE.
        headless: Run Playwright in headless mode (ignored for IB source).
        ib_host: TWS/Gateway host.
        ib_port: TWS/Gateway port (7497=TWS paper, 7496=TWS live,
            4002=Gateway paper, 4001=Gateway live).
        ib_client_id: IB client ID.
    """
    if source == "ib":
        return fetch_via_ib(ib_host, ib_port, ib_client_id)

    if source == "ice":
        return fetch_via_playwright(headless=headless)

    # auto: try IB first, fall back to ICE
    try:
        return fetch_via_ib(ib_host, ib_port, ib_client_id)
    except Exception as ib_err:
        print(
            f"IB not available ({ib_err}), falling back to ICE scraping...",
            file=sys.stderr,
        )
        return fetch_via_playwright(headless=headless)


def _parse_json_data(data: list | dict) -> pd.DataFrame:
    """Parse contract data from ICE's internal JSON API response."""
    if isinstance(data, dict):
        data = [data]

    rows = []
    for contract in data:
        rows.append(
            {
                "contract": contract.get("marketStrip", ""),
                "last": _parse_num(contract.get("lastPrice")),
                "change": _parse_num(contract.get("change")),
                "settlement": _parse_num(contract.get("settlementPrice")),
                "volume": _parse_int(contract.get("volume")),
                "open_interest": _parse_int(contract.get("openInterest")),
            }
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("contract").reset_index(drop=True)
    return _enrich_with_dates(df)


def _parse_html_table(html: str) -> pd.DataFrame:
    """Parse the settlement price table from ICE's rendered HTML."""
    tables = pd.read_html(StringIO(html))
    if not tables:
        raise ValueError("No tables found on the ICE product page")

    for table in tables:
        cols_lower = [str(c).lower() for c in table.columns]
        if any("settle" in c for c in cols_lower) or any(
            "contract" in c for c in cols_lower
        ):
            table.columns = [
                re.sub(r"\s+", "_", str(c).strip().lower())
                for c in table.columns
            ]
            return table

    largest = max(tables, key=len)
    largest.columns = [
        re.sub(r"\s+", "_", str(c).strip().lower()) for c in largest.columns
    ]
    return largest


# Month code mapping for contract labels like "Jun26", "Mar27"
_MONTH_CODES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# IMM quarterly cycle: Mar -> Jun -> Sep -> Dec -> Mar ...
_PREV_IMM_MONTH = {3: 12, 6: 3, 9: 6, 12: 9}


def _third_wednesday(year: int, month: int) -> date:
    """Return the 3rd Wednesday of a given month (IMM date)."""
    # calendar.monthcalendar returns weeks Mon=0..Sun=6
    cal = calendar.monthcalendar(year, month)
    # Wednesday is index 2; find the 3rd occurrence
    wednesdays = [week[2] for week in cal if week[2] != 0]
    return date(year, month, wednesdays[2])


def _contract_period(label: str) -> tuple[date, date] | None:
    """Derive the reference period (start, end) from a contract label.

    SONIA 3-month futures reference the compounded SONIA rate between
    two consecutive IMM dates (3rd Wednesdays of quarterly months).
    E.g. "Jun26" covers 3rd-Wed-Mar-2026 to 3rd-Wed-Jun-2026.
    """
    m = re.match(r"^([A-Za-z]{3})(\d{2})$", label.strip())
    if not m:
        return None
    month_str, year_str = m.group(1).lower(), m.group(2)
    month = _MONTH_CODES.get(month_str)
    if month is None or month not in _PREV_IMM_MONTH:
        return None
    year = 2000 + int(year_str)

    end_date = _third_wednesday(year, month)
    prev_month = _PREV_IMM_MONTH[month]
    prev_year = year - 1 if prev_month == 12 else year
    start_date = _third_wednesday(prev_year, prev_month)
    return start_date, end_date


def _enrich_with_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Add contract_start and contract_end columns and filter junk rows."""
    if df.empty:
        return df

    # Identify the contract column
    contract_col = None
    for col in df.columns:
        if "contract" in col.lower() or "local_symbol" in col.lower():
            contract_col = col
            break
    if contract_col is None:
        return df

    # Filter out pack/bundle rows and rows with chart garbage
    mask = df[contract_col].astype(str).str.match(r"^[A-Za-z]{3}\d{2}$")
    df = df[mask].copy()

    periods = df[contract_col].apply(_contract_period)
    df["contract_start"] = periods.apply(lambda p: p[0] if p else None)
    df["contract_end"] = periods.apply(lambda p: p[1] if p else None)
    return df.reset_index(drop=True)


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
        description="Download latest 3-month SONIA futures settlement prices"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output CSV file path (default: print to stdout)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON instead of CSV",
    )
    parser.add_argument(
        "--source", choices=["auto", "ib", "ice"], default="auto",
        help="Data source: ib (Interactive Brokers), ice (scrape ICE), "
        "auto (try IB then ICE). Default: auto",
    )
    parser.add_argument(
        "--ib-port", type=int, default=7497,
        help="TWS/Gateway port (default: 7497)",
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Show browser window for ICE scraping (debugging)",
    )
    args = parser.parse_args()

    try:
        df = fetch_sonia_futures(
            source=args.source,
            headless=not args.no_headless,
            ib_port=args.ib_port,
        )
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
