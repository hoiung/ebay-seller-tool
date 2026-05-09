"""Issue #33 Phase 1 AC 1.4 — AP #18 cross-module wiring exerciser.

Verifies the FULL `respond_to_best_offer` wiring (NOT just `dict2xml` in
isolation — that's AC 1.3's job in tests/test_best_offers.py): imports the
production async function, mocks `execute_with_retry` to capture the payload
that crosses the cross-module seam, invokes the function, and asserts the
captured `CounterOfferPrice` field has the canonical ebaysdk shape:

    {"#text": "52.00", "@attrs": {"currencyID": "GBP"}}

This proves:
  1. `from .listings import _decimal_str` import resolves at module load
  2. `_decimal_str(52.0)` rounds to `"52.00"` (Decimal-based, float-drift-safe)
  3. The dict literal in the `if action == "Counter"` branch produces the
     canonical CounterOfferPrice structure + the new CounterOfferQuantity
     int element (Stage 5 — Code 21921 "counteroffer quantity is required" fix)
  4. The structure survives the `asyncio.to_thread(execute_with_retry, ...)`
     boundary unchanged (the seam where Code 5 originally surfaced live)

Synthetic IDs only — does NOT reference any real eBay listing or offer.

Non-destructive — `execute_with_retry` is mocked, no eBay API call fires.

USAGE:
  uv run python scripts/sample_invocation_issue33.py --counter
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from unittest.mock import MagicMock, patch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--counter",
        action="store_true",
        help="Exercise the Counter action payload-build path",
    )
    args = parser.parse_args()
    if not args.counter:
        parser.error("must pass --counter (the only mode this script exercises)")

    # Import inside main so module-load errors surface AFTER argparse exits cleanly
    # on -h / no-args.
    from ebay.best_offers import respond_to_best_offer  # noqa: PLC0415

    captured: dict = {}

    def fake_execute(verb: str, payload: dict) -> MagicMock:
        captured["verb"] = verb
        captured["payload"] = payload
        return MagicMock(reply=MagicMock(Ack="Success", Errors=None))

    with patch("ebay.client.execute_with_retry", side_effect=fake_execute):
        asyncio.run(
            respond_to_best_offer(
                item_id="999999999999",
                offer_id="SYNTHETIC123",
                action="Counter",
                counter_price_gbp=52.0,
                counter_quantity=1,
            )
        )

    print(f"verb: {captured['verb']!r}")
    print(f"payload.ItemID: {captured['payload']['ItemID']!r}")
    print(f"payload.BestOfferID: {captured['payload']['BestOfferID']!r}")
    print(f"payload.Action: {captured['payload']['Action']!r}")
    print(f"payload.CounterOfferPrice: {captured['payload']['CounterOfferPrice']!r}")
    print(f"payload.CounterOfferQuantity: {captured['payload']['CounterOfferQuantity']!r}")

    expected_price = {"#text": "52.00", "@attrs": {"currencyID": "GBP"}}
    actual_price = captured["payload"]["CounterOfferPrice"]
    assert actual_price == expected_price, f"expected {expected_price!r}, got {actual_price!r}"
    assert captured["payload"]["CounterOfferQuantity"] == 1, (
        f"expected CounterOfferQuantity=1, got {captured['payload']['CounterOfferQuantity']!r}"
    )

    assert captured["verb"] == "RespondToBestOffer"
    assert captured["payload"]["Action"] == "Counter"
    assert "CounterOfferPrice" in captured["payload"]
    assert "CounterOfferQuantity" in captured["payload"]

    print("OK — canonical shape verified end-to-end across the execute_with_retry seam")
    return 0


if __name__ == "__main__":
    sys.exit(main())
