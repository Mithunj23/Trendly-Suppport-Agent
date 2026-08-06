"""
Unit tests for tools.py — the deterministic policy engine. These don't need
an LLM or API key; they prove the business logic itself is correct for every
edge case the assignment's orders.json deliberately plants. Run with:

    pytest tests/test_tools.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tools  # noqa: E402


def test_identify_customer_by_email():
    r = tools.identify_customer("ananya.rao@example.com")
    assert r["found"] and r["customer_id"] == "C-100"


def test_identify_customer_not_found():
    r = tools.identify_customer("nobody@nowhere.com")
    assert not r["found"]


def test_get_order_authorized():
    r = tools.get_order("TR-4521", customer_id="C-100")
    assert r["found"]
    assert r["order"]["order_id"] == "TR-4521"


def test_get_order_wrong_customer_refused():
    """Data-leakage guardrail: C-101 must not see C-100's order."""
    r = tools.get_order("TR-4521", customer_id="C-101")
    assert not r["found"]
    assert r["error"] == "not_your_order"


def test_get_order_unauthenticated_refused():
    r = tools.get_order("TR-4521", customer_id=None)
    assert not r["found"]
    assert r["error"] == "not_authenticated"


def test_get_order_not_found():
    r = tools.get_order("TR-9999", customer_id="C-100")
    assert not r["found"]
    assert r["error"] == "no_such_order"


def test_happy_path_return_eligible():
    """TR-4530: clean happy path — in window, returnable category, not final sale."""
    v = tools.check_return_eligibility("TR-4530", "TR-KRT-033", "return", customer_id="C-101")
    assert v["eligible"] is True


def test_outside_window_refused():
    """TR-4523: delivered 2026-06-05, far outside 30-day window as of AS_OF_DATE."""
    v = tools.check_return_eligibility("TR-4523", "TR-JKT-008", "return", customer_id="C-102")
    assert v["eligible"] is False
    assert "outside_30_day_window" in v["reasons"]


def test_jewellery_non_returnable_even_within_window():
    """TR-4527: within window, but jewellery — must be refused on category, not date."""
    v = tools.check_return_eligibility("TR-4527", "TR-EAR-042", "return", customer_id="C-102")
    assert v["eligible"] is False
    assert "non_returnable_category" in v["reasons"]
    assert "outside_30_day_window" not in v["reasons"]


def test_innerwear_non_returnable():
    """TR-4522: socks are innerwear category -> non-returnable."""
    v = tools.check_return_eligibility("TR-4522", "TR-SOK-031", "return", customer_id="C-101")
    assert v["eligible"] is False
    assert "non_returnable_category" in v["reasons"]


def test_innerwear_order_other_item_still_eligible():
    """Same order TR-4522, but the tee (apparel) should be fine on its own."""
    v = tools.check_return_eligibility("TR-4522", "TR-TSH-002", "return", customer_id="C-101")
    assert v["eligible"] is True


def test_final_sale_refund_refused_but_exchange_allowed():
    """TR-4528: final sale -> refund refused, size exchange allowed."""
    v_return = tools.check_return_eligibility("TR-4528", "TR-SHR-009", "return", customer_id="C-103")
    assert v_return["eligible"] is False
    assert "final_sale_exchange_only" in v_return["reasons"]

    v_exchange = tools.check_return_eligibility("TR-4528", "TR-SHR-009", "exchange", customer_id="C-103")
    assert v_exchange["eligible"] is True


def test_cancelled_order_refused():
    """TR-4529: cancelled order, no return can be raised."""
    v = tools.check_return_eligibility("TR-4529", "TR-SCF-027", "return", customer_id="C-100")
    assert v["eligible"] is False
    assert "order_cancelled" in v["reasons"]


def test_lost_in_transit_escalates_not_a_return():
    """TR-4526: lost parcel must be escalated, never processed as a return."""
    v = tools.check_return_eligibility("TR-4526", "TR-BAG-011", "return", customer_id="C-101")
    assert v["eligible"] is False
    assert v["escalate"] is True
    assert v["escalation_reason"] == "lost_parcel"


def test_not_yet_delivered_refused():
    """TR-4525: delayed, never delivered — nothing to return yet."""
    v = tools.check_return_eligibility("TR-4525", "TR-SNK-017", "return", customer_id="C-103")
    assert v["eligible"] is False
    assert "not_yet_delivered" in v["reasons"]


def test_initiate_return_reverifies_eligibility():
    """initiate_return must refuse even if called directly on an ineligible item."""
    r = tools.initiate_return("TR-4523", "TR-JKT-008", "change_of_mind", customer_id="C-102")
    assert r["created"] is False
    assert "outside_30_day_window" in r["verdict"]["reasons"]


def test_initiate_return_happy_path_creates_record():
    r = tools.initiate_return("TR-4530", "TR-KRT-033", "change_of_mind", customer_id="C-101")
    assert r["created"] is True
    assert r["record"]["return_id"].startswith("RET-")
    assert r["record"]["refund_shipping_fee"] is False  # change of mind -> not refunded per 3.2


def test_initiate_return_trendly_error_refunds_shipping_fee():
    r = tools.initiate_return("TR-4530", "TR-KRT-033", "damaged", customer_id="C-101")
    assert r["created"] is True
    assert r["record"]["refund_shipping_fee"] is True


def test_exchange_limit_second_request_needs_human():
    """4.4: one exchange per item; a second on the same SKU needs human approval."""
    first = tools.initiate_exchange("TR-4528", "TR-SHR-009", "L", customer_id="C-103")
    assert first["created"] is True

    second = tools.initiate_exchange("TR-4528", "TR-SHR-009", "M", customer_id="C-103")
    assert second["created"] is False
    assert second["verdict"]["escalate"] is True
    assert second["verdict"]["escalation_reason"] == "second_exchange_approval"


def test_wrong_customer_cannot_check_eligibility():
    v = tools.check_return_eligibility("TR-4530", "TR-KRT-033", "return", customer_id="C-999")
    assert v["eligible"] is False
    assert "not_your_order" in v["reasons"]


def test_escalate_to_human_creates_ticket():
    t = tools.escalate_to_human("lost_parcel", "Customer's parcel lost in transit.", customer_id="C-101", order_id="TR-4526")
    assert t["ticket_id"].startswith("TCK-")
    assert t["reason_category"] == "lost_parcel"
