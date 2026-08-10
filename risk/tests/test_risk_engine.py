"""
Tests for Layer 7. Each rule is tested in isolation first, then a combined
case shows two different weak signals stacking into a stronger one.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from risk.rules import RiskEngine


def test_normal_transaction_approves():
    engine = RiskEngine()
    decision = engine.evaluate(card_number="4532015112830366", amount_cents=5000, entry_mode="05")
    assert decision.outcome == "approve"
    assert decision.reasons == []
    print("Normal transaction: approved, no reasons triggered")


def test_large_amount_declines():
    engine = RiskEngine()
    decision = engine.evaluate(card_number="4532015112830366", amount_cents=2_000_000, entry_mode="05")
    assert decision.outcome == "decline"
    assert any("hard limit" in r for r in decision.reasons)
    print("Very large amount: declined --", decision.reasons)


def test_moderately_large_amount_flags_for_review():
    engine = RiskEngine()
    decision = engine.evaluate(card_number="4532015112830366", amount_cents=300_000, entry_mode="05")
    assert decision.outcome == "review"
    print("Moderately large amount: flagged for review --", decision.reasons)


def test_velocity_escalates_after_repeated_attempts():
    engine = RiskEngine(velocity_review_count=3, velocity_decline_count=5)
    card = "4532015112830366"
    outcomes = []
    for i in range(7):
        decision = engine.evaluate(card_number=card, amount_cents=1000, entry_mode="05")
        outcomes.append(decision.outcome)

    # First few should approve, later ones should escalate as velocity climbs.
    assert outcomes[0] == "approve"
    assert "review" in outcomes
    assert "decline" in outcomes
    print("Velocity escalation over 7 rapid attempts:", outcomes)


def test_manual_entry_with_moderate_amount_triggers_review():
    engine = RiskEngine()
    # $600 alone is under the review threshold ($2,000), but manual entry lowers that bar.
    decision = engine.evaluate(card_number="4532015112830366", amount_cents=60_000, entry_mode="01")
    assert decision.outcome == "review"
    assert any("manually keyed" in r for r in decision.reasons)
    print("Manual entry + moderate amount: flagged for review --", decision.reasons)


def test_different_cards_have_independent_velocity():
    engine = RiskEngine(velocity_review_count=1, velocity_decline_count=2)
    card_a = "4532015112830366"
    card_b = "4111111111111111"

    engine.evaluate(card_number=card_a, amount_cents=1000)
    engine.evaluate(card_number=card_a, amount_cents=1000)
    decision_b = engine.evaluate(card_number=card_b, amount_cents=1000)

    assert decision_b.outcome == "approve", "Card B's velocity should be unaffected by card A's attempts"
    print("Velocity state is correctly scoped per card, not global")


if __name__ == "__main__":
    test_normal_transaction_approves()
    test_large_amount_declines()
    test_moderately_large_amount_flags_for_review()
    test_velocity_escalates_after_repeated_attempts()
    test_manual_entry_with_moderate_amount_triggers_review()
    test_different_cards_have_independent_velocity()
