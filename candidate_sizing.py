"""Search explicitly modeled whole-lot sizes under the existing candidate gates."""

from decimal import Decimal

from candidate_review import candidate_review
from market_risk import signed
from optionsdesk import decimal, digest, money
from price_limits import whole_contracts


def size_report(raw, *, require_operational_context=False):
    wealth = decimal(raw["net_liquidation_value_dollars"])
    fractional = decimal(raw["fractional_risk_scale"])
    trade_fraction = decimal(raw["max_order_loss_fraction"])
    stress_fraction = decimal(raw["max_combined_stress_fraction"])
    if not wealth or any(not 0 < value <= 1 for value in (fractional, trade_fraction, stress_fraction)):
        raise ValueError("provide positive equity and sizing fractions in (0, 1]")
    template = raw["one_lot_candidate"]
    price = template["price_case"]
    if type(price["quantity"]) is not int or price["quantity"] != 1:
        raise ValueError("the candidate template must be for one lot")
    book = template["portfolio"]
    cost = decimal(template["tested_limit"])
    loss_per_lot = (100 * cost + decimal(price["round_trip_fees_per_contract_dollars"]) +
                    decimal(price["exit_debit_bound_per_contract_dollars"]))
    portfolio_limit = min(wealth * stress_fraction, decimal(book["risk_limits"]["max_loss_from_mark_dollars"]))
    cap = min(wealth * trade_fraction, decimal(template["available_loss_budget_dollars"]),
              decimal(price["limits"]["max_loss_dollars"]))
    if not cap:
        return {"basis": "supplied_distributions_for_sizing_research", "input_sha256": digest(raw),
                "selected_contracts": 0, "status": "cash", "reasons": ["zero_available_loss_budget"], "sizes": []}
    portfolio = {**book, "risk_limits": {**book["risk_limits"], "max_loss_from_mark_dollars": str(portfolio_limit)}}
    tested, accepted, quantities = [], [], set()
    model_names = None
    for trial in raw["sizes"]:
        quantity = whole_contracts(trial["quantity"], 1, 10**6)
        if quantity in quantities:
            raise ValueError("each lot count needs exactly one scenario set")
        quantities.add(quantity)
        models = trial["models"]
        if model_names is not None and model_names != models.keys():
            raise ValueError("each lot count must use the same model names")
        model_names = models.keys()
        review = candidate_review(
            {**template, "portfolio": portfolio, "available_loss_budget_dollars": str(cap),
             "price_case": {**price, "quantity": quantity, "price_grid": [{"limit": str(cost), "models": models}],
                            "limits": {**price["limits"], "max_loss_dollars": str(cap)}},
             "candidate_positions": [
                 {**leg, "quantity": str(signed(leg["quantity"]) * quantity),
                  **{key: str(decimal(leg.get(key, 0)) * quantity)
                     for key in ("entry_fee_dollars", "exit_fee_dollars")}}
                 for leg in template["candidate_positions"]]},
            require_operational_context=require_operational_context, wealth=wealth,
        )
        logs = [model["expected_log_wealth_growth"] for model in review["entry_price_result"]["models"].values()]
        log_growth = min(map(Decimal, logs)) if all(value is not None for value in logs) else None
        reasons = list(review["reasons"])
        if log_growth is None or log_growth <= 0:
            reasons.append("modeled_log_growth_does_not_beat_cash")
        bound = quantity * loss_per_lot
        if not reasons:
            accepted.append((log_growth, -quantity, bound))
        tested.append({"contracts": quantity, "modeled_loss_bound_dollars": money(bound),
                       "worst_combined_stress_loss_dollars": review["worst_combined_stress_loss_dollars"],
                       "worst_expected_net_dollars": review["entry_price_result"]["worst_model_expected_net_dollars"],
                       "worst_expected_log_growth": str(log_growth) if log_growth is not None else None,
                       "reasons": reasons})
    if not tested:
        raise ValueError("provide at least one modeled lot count")
    reference = max(accepted, default=(Decimal(0), 0, Decimal(0)))
    fractional_cap = reference[2] * fractional
    selected = max((row for row in accepted if row[2] <= fractional_cap), default=(Decimal(0), 0, Decimal(0)))
    for row in tested:
        row["within_fractional_budget"] = row["contracts"] * loss_per_lot <= fractional_cap
    return {"basis": "supplied_distributions_for_sizing_research", "input_sha256": digest(raw),
            "status": "sized_research_candidate" if selected[1] else "cash", "selected_contracts": -selected[1],
            "reference_contracts": -reference[1], "fractional_loss_budget_dollars": money(fractional_cap),
            "reasons": [] if selected[1] else
            ["no_tested_lot_fits_fractional_budget" if accepted else "no_tested_lot_passes"],
            "sizes": sorted(tested, key=lambda row: row["contracts"])}
