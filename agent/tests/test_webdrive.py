"""The shopping browser's hard stops: JARVIS may fill a cart but never pay or enter secrets."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent.webdrive import PAYS, SECRET_FIELD  # noqa: E402

# Real button labels from Amazon, Flipkart, Swiggy, Zomato and Domino's checkouts.
MUST_REFUSE = [
    "Place your order", "Place Order", "PLACE ORDER", "Pay ₹349", "Pay Rs. 1,299", "Pay Now", "Pay securely",
    "Proceed to Pay", "Proceed to payment", "Make Payment", "Confirm and pay", "Confirm Order", "Buy Now",
    "Complete purchase", "Submit order",
]
MUST_ALLOW = [
    "Add to Cart", "ADD TO CART", "Add", "Go to Cart", "View Cart", "Proceed to Buy", "Proceed to checkout",
    "Checkout", "Deliver to this address", "Search", "Apply coupon", "Remove", "+", "Veg Extravaganza",
]


class GuardTests(unittest.TestCase):
    def test_pay_and_place_order_buttons_are_refused(self) -> None:
        for label in MUST_REFUSE:
            with self.subTest(label=label):
                self.assertTrue(PAYS.search(label), label)

    def test_cart_and_checkout_buttons_are_allowed(self) -> None:
        for label in MUST_ALLOW:
            with self.subTest(label=label):
                self.assertIsNone(PAYS.search(label), label)

    def test_secret_fields_are_refused(self) -> None:
        for attrs in ["password password current-password", "text cardnumber cc-number", "tel cvv", "text otp one-time-code",
                      "text upi_id Enter UPI ID", "password pin", "text vpa"]:
            with self.subTest(attrs=attrs):
                self.assertTrue(SECRET_FIELD.search(attrs), attrs)

    def test_ordinary_fields_are_allowed(self) -> None:
        for attrs in ["search field-keywords twotabsearchtextbox Search Amazon.in", "text pincode-free address line 1",
                      "text name Full name", "tel phone Mobile number"]:
            with self.subTest(attrs=attrs):
                self.assertIsNone(SECRET_FIELD.search(attrs), attrs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
