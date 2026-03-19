"""
TradeStation API Data Depth Test
================================
Quick script to check how far back TradeStation provides 5-minute CL
futures data via the @CL=11VOR continuous contract symbology.

Prerequisites:
  1. TradeStation account (funded with $10K minimum for free API access)
  2. API Key (Client ID) and Secret from TradeStation developer portal
     - Go to: https://api.tradestation.com/docs/faq#how-do-i-get-an-api-key
     - Or contact TradeStation support to request API credentials

Usage:
  # Step 1: Get your authorization code (opens browser)
  python scripts/test_tradestation_depth.py --setup --client-id YOUR_CLIENT_ID

  # Step 2: Exchange auth code for access token and test data depth
  python scripts/test_tradestation_depth.py --client-id YOUR_CLIENT_ID --client-secret YOUR_SECRET --auth-code CODE_FROM_STEP1

  # Step 3 (after you have a token): Just test depth directly
  python scripts/test_tradestation_depth.py --token YOUR_ACCESS_TOKEN
"""

import argparse
import json
import sys
import webbrowser
from datetime import datetime
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip install requests")
    sys.exit(1)

# TradeStation API endpoints
TS_AUTH_URL = "https://signin.tradestation.com/authorize"
TS_TOKEN_URL = "https://signin.tradestation.com/oauth/token"
TS_API_BASE = "https://api.tradestation.com/v3"
TS_SIM_API_BASE = "https://sim-api.tradestation.com/v3"

# The symbol we want to test - volume-rolled, ratio-adjusted CL continuous
TEST_SYMBOL = "@CL=11VOR"     # Volume crossover roll + Ratio adjustment
TEST_SYMBOL_ALT = "@CL"       # Default continuous (simpler, in case custom fails)
REDIRECT_URI = "http://localhost"


def step1_get_auth_url(client_id: str) -> str:
    """Generate the authorization URL and open it in the browser."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "audience": "https://api.tradestation.com",
        "scope": "MarketData ReadAccount",
    }
    url = f"{TS_AUTH_URL}?{urlencode(params)}"
    print(f"\n{'='*60}")
    print("STEP 1: Authorization")
    print(f"{'='*60}")
    print(f"\nOpening browser for TradeStation login...")
    print(f"\nAfter login, you'll be redirected to a URL like:")
    print(f"  http://localhost/?code=XXXXXXXXXXXX")
    print(f"\nCopy the 'code' value from the URL and use it in Step 2.")
    print(f"\nURL: {url}\n")
    webbrowser.open(url)
    return url


def step2_get_token(client_id: str, client_secret: str, auth_code: str) -> dict:
    """Exchange authorization code for access token."""
    print(f"\n{'='*60}")
    print("STEP 2: Getting Access Token")
    print(f"{'='*60}")

    payload = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
    }

    resp = requests.post(TS_TOKEN_URL, data=payload)
    if resp.status_code != 200:
        print(f"\nERROR: Token request failed (HTTP {resp.status_code})")
        print(f"Response: {resp.text}")
        sys.exit(1)

    token_data = resp.json()
    access_token = token_data.get("access_token", "")
    print(f"\n✅ Access token obtained! (expires in {token_data.get('expires_in', '?')}s)")
    print(f"\nSave this token for future calls:")
    print(f"  --token {access_token[:20]}...{access_token[-10:]}")

    return token_data


def test_data_depth(access_token: str, use_sim: bool = False):
    """Test how far back we can get CL 5-minute data."""
    base = TS_SIM_API_BASE if use_sim else TS_API_BASE
    headers = {"Authorization": f"Bearer {access_token}"}

    print(f"\n{'='*60}")
    print("DATA DEPTH TEST")
    print(f"{'='*60}")
    print(f"API: {'Simulated' if use_sim else 'Live'}")

    # Test multiple symbols in case custom continuous doesn't work
    symbols_to_test = [
        ("@CL=11VOR", "Volume-rolled, Ratio-adjusted"),
        ("@CL=11VON", "Volume-rolled, Unadjusted"),
        ("@CL",       "Default continuous"),
    ]

    for symbol, desc in symbols_to_test:
        print(f"\n{'─'*50}")
        print(f"Testing: {symbol} ({desc})")
        print(f"{'─'*50}")

        # Try fetching bars going back as far as possible
        # Use firstdate to request from a specific historical date
        test_dates = [
            "2008-01-01",   # 18 years back (ideal)
            "2010-01-01",   # 16 years back
            "2012-01-01",   # 14 years back
            "2015-01-01",   # 11 years back
            "2018-01-01",   # 8 years back
            "2020-01-01",   # 6 years back
        ]

        for start_date in test_dates:
            # Request a small window starting from the test date
            # to see if data exists at that point
            end_date_str = f"{start_date[:4]}-01-15"  # 2 weeks of data

            params = {
                "symbol": symbol,
                "interval": "5",
                "unit": "Minute",
                "firstdate": start_date,
                "lastdate": end_date_str,
                "sessiontemplate": "Default",
            }

            url = f"{base}/marketdata/barcharts/{symbol}"
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)

                if resp.status_code == 200:
                    data = resp.json()
                    bars = data.get("Bars", [])
                    if bars:
                        first_bar = bars[0]
                        last_bar = bars[-1]
                        print(f"  ✅ {start_date}: {len(bars)} bars found")
                        print(f"     First: {first_bar.get('TimeStamp', 'N/A')}")
                        print(f"     Last:  {last_bar.get('TimeStamp', 'N/A')}")
                        print(f"     Close: ${first_bar.get('Close', 'N/A')}")
                    else:
                        print(f"  ❌ {start_date}: No bars returned (empty)")
                elif resp.status_code == 401:
                    print(f"  🔒 Auth failed — token may be expired. Re-run Step 2.")
                    return
                else:
                    error_msg = resp.text[:200] if resp.text else "No details"
                    print(f"  ❌ {start_date}: HTTP {resp.status_code} — {error_msg}")

            except requests.exceptions.Timeout:
                print(f"  ⏱️  {start_date}: Request timed out")
            except Exception as e:
                print(f"  ❌ {start_date}: Error — {e}")

        # Also try a large barsback request to find the absolute earliest data
        print(f"\n  Testing max depth with barsback...")
        params = {
            "symbol": symbol,
            "interval": "5",
            "unit": "Minute",
            "barsback": "500000",  # ~4 years of 5-min bars
            "sessiontemplate": "Default",
        }
        url = f"{base}/marketdata/barcharts/{symbol}"
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                bars = data.get("Bars", [])
                if bars:
                    first_ts = bars[0].get("TimeStamp", "N/A")
                    last_ts = bars[-1].get("TimeStamp", "N/A")
                    print(f"  ✅ barsback=500000: Got {len(bars)} bars")
                    print(f"     Earliest bar: {first_ts}")
                    print(f"     Latest bar:   {last_ts}")

                    # Calculate span
                    try:
                        first_dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                        span = last_dt - first_dt
                        years = span.days / 365.25
                        print(f"     Span: {span.days} days (~{years:.1f} years)")
                    except Exception:
                        pass

                    # If we got the full 500K, there might be more data
                    if len(bars) >= 499000:
                        print(f"     ⚠️  Hit the 500K limit — more data likely exists further back!")
                else:
                    print(f"  ❌ barsback=500000: No bars returned")
            else:
                print(f"  ❌ barsback=500000: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  ❌ barsback=500000: Error — {e}")

    print(f"\n{'='*60}")
    print("TEST COMPLETE")
    print(f"{'='*60}")
    print("\nLook for the earliest bar date above.")
    print("If data goes back to 2010 or earlier → TradeStation has enough depth ✅")
    print("If data only goes back to 2016+ → Consider Databento instead")


def main():
    parser = argparse.ArgumentParser(
        description="Test TradeStation API data depth for CL futures"
    )
    parser.add_argument("--setup", action="store_true",
                        help="Step 1: Open browser for authorization")
    parser.add_argument("--client-id", type=str,
                        help="TradeStation API Client ID")
    parser.add_argument("--client-secret", type=str,
                        help="TradeStation API Client Secret")
    parser.add_argument("--auth-code", type=str,
                        help="Authorization code from Step 1 redirect")
    parser.add_argument("--token", type=str,
                        help="Skip auth — use an existing access token directly")
    parser.add_argument("--sim", action="store_true",
                        help="Use simulated API endpoint instead of live")

    args = parser.parse_args()

    if args.setup:
        if not args.client_id:
            print("ERROR: --client-id required for --setup")
            sys.exit(1)
        step1_get_auth_url(args.client_id)
        return

    if args.token:
        # Skip auth, go straight to testing
        test_data_depth(args.token, use_sim=args.sim)
        return

    if args.client_id and args.client_secret and args.auth_code:
        # Do the full flow: get token then test
        token_data = step2_get_token(args.client_id, args.client_secret, args.auth_code)
        test_data_depth(token_data["access_token"], use_sim=args.sim)
        return

    print("Usage:")
    print("  Step 1: python scripts/test_tradestation_depth.py --setup --client-id YOUR_ID")
    print("  Step 2: python scripts/test_tradestation_depth.py --client-id YOUR_ID --client-secret YOUR_SECRET --auth-code CODE")
    print("  Direct: python scripts/test_tradestation_depth.py --token YOUR_TOKEN")
    print("\nSee script header for full instructions.")


if __name__ == "__main__":
    main()
