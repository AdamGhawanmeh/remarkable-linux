#!/usr/bin/env python3
"""Interactive pairing check against the real reMarkable cloud.

Usage:
    python3 client/tests/test_auth.py            # pair if needed, then fetch a user token
    python3 client/tests/test_auth.py --repair   # discard stored tokens and pair again
    python3 client/tests/test_auth.py -v         # log HTTP hosts and discovery details

Get a one-time code from https://my.remarkable.com/device/desktop/connect.
Tokens are stored in ~/.config/remarkable-linux/auth.json (mode 0600).

This is a manual CLI, not a unit test: it contains no test_* functions, so test
runners that import it collect nothing and never block on the prompt.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.auth import (  # noqa: E402
    PAIRING_URL, AuthClient, AuthError, DeviceRevokedError, decode_jwt_claims,
    tectonic_region, token_expiry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repair", action="store_true", help="forget stored tokens and pair again")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    client = AuthClient()
    print(f"auth file : {client.store.path}")
    print(f"device ID : {client.state.device_id}")

    if args.repair and client.is_paired:
        client.store.clear_tokens()
        client = AuthClient()

    if not client.is_paired:
        print(f"\nOpen {PAIRING_URL} and enter the one-time code shown there.")
        code = input("One-time code: ").strip()
        try:
            client.pair(code)
        except AuthError as err:
            print(f"Pairing failed: {err}", file=sys.stderr)
            return 1
        print(f"Paired via {client.state.auth_host}")
    else:
        print(f"Already paired (auth host {client.state.auth_host}); use --repair to pair again.")

    try:
        token = client.get_user_token()
    except DeviceRevokedError as err:
        print(f"{err}\nRun again with --repair.", file=sys.stderr)
        return 1
    except AuthError as err:
        print(f"User token request failed: {err}", file=sys.stderr)
        return 1

    claims = decode_jwt_claims(token)
    exp = token_expiry(token)
    print("\nUser token validated (well-formed JWT, not expired):")
    print(f"  expires  : {time.strftime('%F %T', time.localtime(exp)) if exp else 'no exp claim'}"
          f"{f' ({int(exp - time.time())}s from now)' if exp else ''}")
    print(f"  tectonic : {tectonic_region(token) or '<none>'}")
    print(f"  gateway  : {client.gateway()}")
    print(f"  claims   : {', '.join(sorted(claims))}")
    print(f"\n{token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
