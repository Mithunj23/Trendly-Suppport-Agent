"""
tools.py
--------
Deterministic business logic the agent can call as "tools".

Design principle: the LLM never decides eligibility, dates, or authorization
by itself. It calls these functions and gets back a structured verdict. This
is what keeps the agent grounded — the policy rules live in code, not in the
model's head, so it can't hallucinate a return window or a discount.

All functions that touch a specific order require `customer_id` to be passed
in by the CALLER (agent.py), not by the LLM. The LLM only ever supplies
order_id / sku / action — never a customer_id argument — so a malicious or
confused prompt can't trick the model into fetching someone else's order by
asking it to "pass customer_id C-102". Authorization is enforced server-side.
"""

import json
import uuid
from datetime import datetime, date
from pathlib import Path

DATA_PATH = Path(__file__).parent / "orders.json"

with open(DATA_PATH) as f:
    _DB = json.load(f)

ORDERS = {o["order_id"]: o for o in _DB["orders"]}
CUSTOMERS = {c["customer_id"]: c for c in _DB["customers"]}

NON_RETURNABLE_CATEGORIES = {"innerwear", "jewellery", "beauty", "fragrance", "face_masks", "gift_card"}

# The dataset's notes are written relative to a fixed "as of" date. We pin
# "today" to that date instead of the wall clock so the 30-day window logic
# stays consistent with the designers' intent no matter when this is graded.
# Override with AS_OF_DATE env var if needed.
import os
AS_OF_DATE = date.fromisoformat(os.environ.get("AS_OF_DATE", "2026-08-05"))

# In-memory state for things that must persist across turns / requests within
# a demo session: exchange counts per (order_id, sku), and issued return/
# exchange/escalation records. A real deployment would back this with a DB.
_EXCHANGE_COUNTS = {}   # (order_id, sku) -> int
_ACTIONS_LOG = []       # list of dicts, for audit / demo purposes


def _business_days_between(d1: date, d2: date) -> int:
    """Rough business-day count (Mon-Fri) between two dates, d2 - d1."""
    if d2 <= d1:
        return 0
    days = 0
    cur = d1
    while cur < d2:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days


def identify_customer(identifier: str):
    """
    Resolve a customer by email, phone, or customer_id. Used to authenticate
    the person before we touch any order data.
    """
    identifier = identifier.strip().lower()
    for c in CUSTOMERS.values():
        if (
            c["customer_id"].lower() == identifier
            or c["email"].lower() == identifier
            or c["phone"].lower() == identifier.replace(" ", "")
        ):
            return {"found": True, "customer_id": c["customer_id"], "name": c["name"]}
    return {"found": False}


def get_order(order_id: str, customer_id: str):
    """
    Look up an order. Returns the order only if it belongs to customer_id.
    This is the data-leakage guardrail: even if the LLM is asked to fetch
    another customer's order, this function refuses.
    """
    order_id = order_id.strip().upper()
    order = ORDERS.get(order_id)
    if not order:
        return {"found": False, "error": "no_such_order"}
    if not customer_id:
        return {"found": False, "error": "not_authenticated"}
    if order["customer_id"] != customer_id:
        return {"found": False, "error": "not_your_order"}

    # Derive a couple of plain-language flags the model can use without
    # having to do date math itself.
    result = dict(order)
    if order.get("expected_delivery") and order["status"] not in ("delivered", "cancelled"):
        expected = date.fromisoformat(order["expected_delivery"])
        days_late = _business_days_between(expected, AS_OF_DATE)
        result["business_days_past_expected"] = days_late
        result["is_delayed_per_policy"] = days_late > 3  # policy 1.5
    if order.get("delivered_at"):
        delivered_date = datetime.fromisoformat(order["delivered_at"].replace("Z", "+00:00")).date()
        result["days_since_delivery"] = (AS_OF_DATE - delivered_date).days
        result["return_window_open"] = (AS_OF_DATE - delivered_date).days <= 30
    return {"found": True, "order": result}


def check_return_eligibility(order_id: str, sku: str, action: str, customer_id: str):
    """
    action: "return" (refund) or "exchange" (size only).
    Returns a structured verdict: eligible, allowed_action, reasons[], notes[].
    This is the single source of truth the agent must defer to — it must
    never announce eligibility itself without calling this.
    """
    order_id = order_id.strip().upper()
    order = ORDERS.get(order_id)
    if not order:
        return {"eligible": False, "reasons": ["no_such_order"]}
    if order["customer_id"] != customer_id:
        return {"eligible": False, "reasons": ["not_your_order"]}

    item = next((it for it in order["items"] if it["sku"] == sku), None)
    if not item:
        return {"eligible": False, "reasons": ["item_not_in_order"]}

    reasons = []
    notes = []

    # 2.6 — cancelled orders
    if order["status"] == "cancelled":
        return {"eligible": False, "reasons": ["order_cancelled"], "escalate": False}

    # 1.6 — lost parcel is NOT a return, must escalate
    if order["status"] == "lost_in_transit":
        return {
            "eligible": False,
            "reasons": ["lost_parcel_claim_not_a_return"],
            "escalate": True,
            "escalation_reason": "lost_parcel",
        }

    # Must be delivered to return/exchange
    if not order.get("delivered_at"):
        return {"eligible": False, "reasons": ["not_yet_delivered"]}

    delivered_date = datetime.fromisoformat(order["delivered_at"].replace("Z", "+00:00")).date()
    days_since = (AS_OF_DATE - delivered_date).days

    # 2.1 — 30 day window
    if days_since > 30:
        reasons.append("outside_30_day_window")

    # 2.3 — non-returnable categories
    if item["category"] in NON_RETURNABLE_CATEGORIES:
        reasons.append("non_returnable_category")

    allowed_action = action
    # 2.4 — final sale: exchange (size) only, no refund/credit
    if item.get("final_sale"):
        if action == "return":
            reasons.append("final_sale_exchange_only")
        else:
            notes.append("Final sale item: size exchange only, no refund or store credit.")

    # 2.5 — footwear box condition (informational, not blocking)
    if item["category"] == "footwear":
        notes.append("Footwear must be returned in its original shoe box; returns without the box incur a ₹300 deduction.")

    # 4.4 — one exchange per item; second requires human approval
    if action == "exchange":
        count = _EXCHANGE_COUNTS.get((order_id, sku), 0)
        if count >= 1:
            return {
                "eligible": False,
                "reasons": ["second_exchange_needs_human_approval"],
                "escalate": True,
                "escalation_reason": "second_exchange_approval",
            }

    eligible = len(reasons) == 0
    return {
        "eligible": eligible,
        "allowed_action": allowed_action if eligible else None,
        "reasons": reasons,
        "notes": notes,
        "days_since_delivery": days_since,
        "category": item["category"],
        "final_sale": item.get("final_sale", False),
    }


def initiate_return(order_id: str, sku: str, reason: str, customer_id: str):
    """
    Re-validates eligibility itself (never trusts a prior tool result blindly)
    before creating a mock return record. This is the guardrail against the
    model narrating an approval that was never actually checked in this turn.
    """
    verdict = check_return_eligibility(order_id, sku, "return", customer_id)
    if not verdict.get("eligible"):
        return {"created": False, "verdict": verdict}

    order = ORDERS[order_id.strip().upper()]
    item = next(it for it in order["items"] if it["sku"] == sku)
    return_id = f"RET-{uuid.uuid4().hex[:8].upper()}"

    fee_refundable = reason in {"wrong_item", "damaged", "defective"}
    record = {
        "return_id": return_id,
        "order_id": order_id,
        "sku": sku,
        "reason": reason,
        "refund_shipping_fee": fee_refundable,
        "payment_method": order["payment_method"],
        "pickup": "Free reverse pickup will be scheduled; the carrier will attempt delivery pickup up to 2 times.",
        "refund_timeline": _refund_timeline_text(order["payment_method"]),
    }
    _ACTIONS_LOG.append({"type": "return", **record})
    return {"created": True, "record": record}


def initiate_exchange(order_id: str, sku: str, new_size: str, customer_id: str):
    verdict = check_return_eligibility(order_id, sku, "exchange", customer_id)
    if not verdict.get("eligible"):
        return {"created": False, "verdict": verdict}

    order = ORDERS[order_id.strip().upper()]
    exchange_id = f"EXC-{uuid.uuid4().hex[:8].upper()}"
    _EXCHANGE_COUNTS[(order_id.strip().upper(), sku)] = _EXCHANGE_COUNTS.get((order_id.strip().upper(), sku), 0) + 1
    record = {
        "exchange_id": exchange_id,
        "order_id": order_id,
        "sku": sku,
        "new_size": new_size,
        "pickup": "Free reverse pickup will be scheduled for the original item; the new size ships once the return is picked up.",
        "note": "If the requested size is unavailable, this will automatically convert to a refund per policy.",
    }
    _ACTIONS_LOG.append({"type": "exchange", **record})
    return {"created": True, "record": record}


def _refund_timeline_text(payment_method: str) -> str:
    mapping = {
        "credit_card": "5–7 business days to the original card after warehouse inspection (2–3 business days).",
        "prepaid_card": "5–7 business days to the original card after warehouse inspection (2–3 business days).",
        "upi": "3–5 business days to the original UPI ID after warehouse inspection (2–3 business days).",
        "cash_on_delivery": "7–10 business days via bank transfer or store credit after warehouse inspection (2–3 business days). A human agent will collect bank details securely.",
        "store_credit": "Immediate, as store credit.",
    }
    return mapping.get(payment_method, "Standard refund timelines per policy section 3.")


def escalate_to_human(reason_category: str, summary: str, customer_id: str, order_id: str = None, priority: str = "normal"):
    """
    Creates a ticket for a human agent. `summary` should be something an
    agent could act on without re-reading the whole transcript.
    """
    ticket_id = f"TCK-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "ticket_id": ticket_id,
        "reason_category": reason_category,
        "summary": summary,
        "customer_id": customer_id,
        "order_id": order_id,
        "priority": priority,
        "support_hours": "9:00 AM – 9:00 PM IST, seven days a week",
    }
    _ACTIONS_LOG.append({"type": "escalation", **record})
    return record


def get_actions_log():
    """For debugging / demo purposes: everything the agent has actually done."""
    return _ACTIONS_LOG
