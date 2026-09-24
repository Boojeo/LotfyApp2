"""Minimal reproduction for Capital.com support.

Uses only `requests` and Capital.com's own documented session example, so the
result cannot be blamed on the trading bot. Asks for the credentials when run
and never prints them, the CST or the X-SECURITY-TOKEN -- the output is safe
to send to support.

    py tools\\session_check.py
"""

import getpass

import requests

SERVERS = {
    "live": "https://api-capital.backend-capital.com",
    "demo": "https://demo-api-capital.backend-capital.com",
}

identifier = input("Login email: ").strip()
password = getpass.getpass("API key password (hidden): ")
api_key = getpass.getpass("API key (hidden): ").strip()

for name, url in SERVERS.items():
    # Exactly the request in Capital.com's documentation.
    response = requests.post(
        url + "/api/v1/session",
        json={"identifier": identifier, "password": password},
        headers={"X-CAP-API-KEY": api_key},
        timeout=20,
    )
    print(f"\n{name.upper()}  POST {url}/api/v1/session")
    print(f"  HTTP status:        {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:300]}
    if response.ok:
        print(f"  currentAccountId:   {body.get('currentAccountId')}")
        for account in body.get("accounts", []):
            print(f"  account:            {account.get('accountId')}  "
                  f"type={account.get('accountType')}  "
                  f"preferred={account.get('preferred')}")
        print(f"  CST header present: {'CST' in response.headers}")
    else:
        print(f"  response body:      {body}")
