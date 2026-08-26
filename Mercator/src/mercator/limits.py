"""Shared numeric resource limits.

Zero-import, like ``reasons.py`` — a real cart_id/idempotency_key is short
(``cart_<32 hex chars>`` or a UUID4 string), so an oversized value can only
be an attempt to exhaust memory, disk (via the ledger), or hashing time.
Rejecting it early costs nothing legitimate.
"""

MAX_ID_LENGTH = 256
"""Max length for any client-supplied identifier: idempotency_key, cart_id,
product_id."""

MAX_IDEMPOTENCY_ENTRIES = 50_000
"""Max distinct idempotency keys held in memory at once. A key already
stored keeps replaying past this cap; a brand-new key is refused
*before* its operation runs, so refusing never costs a duplicate charge."""

MAX_CARTS = 50_000
"""Max carts held in memory at once. Oldest cart not currently checked out
or mid-checkout is evicted first; evicting a cart only risks a later
CART_NOT_FOUND, which is fail-closed."""
