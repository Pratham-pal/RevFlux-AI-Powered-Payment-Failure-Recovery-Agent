"""Razorpay test-mode integration (order creation only).

The demo uses Razorpay Checkout in *test mode*: a real order is created on
Razorpay's servers and a real (test) payment attempt is made in the browser.
No real money moves. If no test keys are configured, the site falls back to a
built-in mock checkout that produces the same failure-event shape.

Set these in a local `.env` file or the host's environment:
    RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxx
    RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
"""

from __future__ import annotations

import os

try:
    import razorpay
except ImportError:  # pragma: no cover
    razorpay = None  # type: ignore

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()


def is_enabled() -> bool:
    """True when a real Razorpay test-mode checkout can be used."""
    return bool(razorpay and KEY_ID and KEY_SECRET and KEY_ID.startswith("rzp_"))


def create_order(amount_inr: float, receipt: str) -> dict:
    """Create a test-mode order; return what the browser Checkout needs."""
    client = razorpay.Client(auth=(KEY_ID, KEY_SECRET))
    order = client.order.create({
        "amount": int(round(amount_inr * 100)),  # paise
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1,
        "notes": {"demo": "payment-failure-recovery-agent"},
    })
    return {
        "provider": "razorpay",
        "order_id": order["id"],
        "key_id": KEY_ID,
        "amount": order["amount"],
        "currency": order["currency"],
    }
