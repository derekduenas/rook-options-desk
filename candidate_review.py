"""Apply the shared price and portfolio gates to one research candidate."""

from collections import defaultdict
from decimal import Decimal

from broker_reconcile import reconcile
from market_risk import portfolio_report, signed
from market_scan import regime
from optionsdesk import NEW_YORK, contract, decimal, digest, money, timestamp, utc
from price_limits import price_limit_report


def operational_review(context, cutoff, positions, candidates):
    if context is None:
        return {"status": "review_required", "reasons": ["missing_operational_context"]}
    reasons = []
    account = context.get("account_state")
    if account is None:
        reasons.append("missing_account_state")
    else:
        maximum_age = min(Decimal(30), decimal(account["max_age_seconds"]))
        reconciliation = reconcile({**account, "as_of": cutoff, "max_age_seconds": str(maximum_age)})
        reasons.extend(reconciliation["reasons"])
        if reconciliation["status"] == "matched":
            stress_book = defaultdict(Decimal)
            for position in positions:
                instrument = position["symbol"]
                if position["kind"] == "option":
                    expiry = timestamp(position["expiry_at"]).astimezone(NEW_YORK).date().isoformat()
                    instrument = contract(instrument, expiry, position["strike"], position["right"])
                stress_book[instrument] += signed(position["quantity"])
            broker = account["broker"]
            broker_book = {key: signed(value) for key, value in broker["positions"].items()}
            if {key: value for key, value in stress_book.items() if value} != {
                key: value for key, value in broker_book.items() if value
            }:
                reasons.append("portfolio_does_not_match_broker")
            if broker["working_orders"]:
                reasons.append("working_orders_outside_portfolio_stress")

    intake = context.get("market_intake")
    if intake is None:
        reasons.append("missing_market_intake")
    else:
        observed_cutoff = utc(intake["as_of"])
        if observed_cutoff > cutoff:
            reasons.append("market_intake_unavailable_at_cutoff")
        else:
            # An old snapshot cannot gain evidence as its evaluation time moves forward.
            frozen = regime(intake, observed_cutoff, cutoff)
            if frozen["snapshot_stale"]:
                reasons.append("market_intake_stale")
            if frozen["label"] == "unclassified" or regime(intake, cutoff, cutoff)["label"] == "unclassified":
                reasons.append("market_regime_unclassified")

    cutoffs = context.get("entry_cutoffs", {})
    for leg in candidates:
        expiry = utc(leg["expiry_at"])
        code = contract(leg["symbol"], timestamp(expiry).astimezone(NEW_YORK).date().isoformat(),
                        leg["strike"], leg["right"])
        if code not in cutoffs:
            reasons.append(f"missing_entry_cutoff:{code}")
            continue
        limit = cutoffs[code]
        last_entry = utc(limit["last_entry_at"])
        if utc(limit["known_at"]) > cutoff:
            reasons.append(f"entry_cutoff_unavailable:{code}")
        elif last_entry > expiry or last_entry <= cutoff:
            reasons.append(f"entry_cutoff_invalid_or_reached:{code}")
    return {"status": "review_required" if reasons else "supplied_checks_passed", "reasons": reasons}


def candidate_review(raw, *, require_operational_context=False, wealth=None):
    case, book = raw["price_case"], raw["portfolio"]
    cutoff = utc(case["as_of"])
    if cutoff != utc(book["as_of"]):
        raise ValueError("price and portfolio must use the same decision cutoff")
    price = price_limit_report(case, wealth=wealth)
    selected = decimal(raw["tested_limit"])
    row = next((row for row in price["prices"] if Decimal(row["limit"]) == selected), None)
    if row is None:
        raise ValueError("the selected entry must be a tested limit")
    terms, quantity = case["contract"], case["quantity"]
    expected = [(decimal(terms["long_strike"]), quantity)]
    if terms.get("short_strike") is not None:
        expected.append((decimal(terms["short_strike"]), -quantity))
    legs = raw["candidate_positions"]
    observed = []
    expiries = set()
    for leg in legs:
        expiry = utc(leg["expiry_at"])
        expiries.add(expiry)
        if (leg["kind"] != "option" or leg["symbol"] != terms["symbol"] or leg["right"] != terms["right"]
                or timestamp(expiry).astimezone(NEW_YORK).date().isoformat() != terms["expiry"]):
            raise ValueError("candidate legs do not match the tested option")
        observed.append((decimal(leg["strike"]), Decimal(str(leg["quantity"]))))
    if (sorted(observed) != sorted(expected) or len(expiries) != 1
            or ("expiry_at" in terms and utc(terms["expiry_at"]) not in expiries)):
        raise ValueError("candidate strikes, expiry and signed quantities must match the tested structure")
    if sum(decimal(leg["entry_price"]) * Decimal(str(leg["quantity"])) for leg in legs) != selected * quantity:
        raise ValueError("candidate entry debit does not match the tested limit and size")
    fees = sum(decimal(leg.get(key, 0)) for leg in legs for key in ("entry_fee_dollars", "exit_fee_dollars"))
    if fees != decimal(case["round_trip_fees_per_contract_dollars"]) * quantity:
        raise ValueError("candidate fees must match the price model")

    remaining = decimal(raw["available_loss_budget_dollars"])
    limits = book["risk_limits"]
    stress_limit = decimal(limits["max_loss_from_mark_dollars"])
    decimal(limits["max_gross_assignment_cash_dollars"])
    risk = portfolio_report({**book, "positions": [*book["positions"], *legs]})
    mark = sum(Decimal(leg["value_dollars"]) for leg in risk["current"]["positions"][-len(legs):])
    entry_fees = sum(decimal(leg.get("entry_fee_dollars", 0)) for leg in legs)
    initial_drag = max(Decimal(0), selected * quantity * 100 + entry_fees - mark)
    stress_losses = [max(Decimal(0), initial_drag - Decimal(row["change_from_current_mark_dollars"]))
                     for row in risk["scenarios"]]
    stressed_loss = max(initial_drag, *stress_losses)
    reasons = list(row["reasons"])
    if Decimal(row["modeled_loss_bound_dollars"]) > remaining:
        reasons.append("available_loss_budget")
    for check in risk["risk_limits"]["checks"]:
        if check["status"] != "within_limit":
            reasons.append(check["limit"] + "_" + check["status"])
    if stressed_loss > stress_limit:
        reasons.append("entry_adjusted_portfolio_stress_limit")
    operational = {"status": "not_evaluated", "reasons": []}
    if require_operational_context or "operational_context" in raw:
        operational = operational_review(raw.get("operational_context"), cutoff, book["positions"], legs)
        reasons.extend(operational["reasons"])
    return {"basis": "research_review_only", "input_sha256": digest(raw), "as_of": cutoff,
            "status": "passes_research_gates" if not reasons else "pass_on_candidate", "reasons": reasons,
            "selected_limit": str(selected), "entry_price_result": row,
            "available_loss_budget_dollars": money(remaining), "entry_cost_vs_model_mark_dollars": money(initial_drag),
            "worst_combined_stress_loss_dollars": money(stressed_loss),
            "portfolio_stress_limit_dollars": money(stress_limit), "portfolio_checks": risk["risk_limits"]["checks"],
            "operational_review": operational}
