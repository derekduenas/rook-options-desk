"""Compare exact option expressions under one portfolio and one decision horizon."""

from decimal import Decimal

from candidate_sizing import size_report
from optionsdesk import digest, identifier, utc


def expression_report(raw, *, require_operational_context=False):
    limits = {key: raw[key] for key in ("net_liquidation_value_dollars", "fractional_risk_scale",
                                      "max_order_loss_fraction", "max_combined_stress_fraction")}
    cutoff, exit_at = utc(raw["as_of"]), utc(raw["exit_at"])
    if exit_at <= cutoff:
        raise ValueError("the common exit must follow the decision cutoff")
    candidates, seen, common_models = [], set(), None
    for item in raw["expressions"]:
        name = identifier(item["name"])
        if name in seen:
            raise ValueError("expression names must be distinct")
        seen.add(name)
        candidate = item["one_lot_candidate"]
        case = candidate["price_case"]
        contract = case["contract"]
        if utc(case["as_of"]) != cutoff or contract["symbol"] != raw["underlying"]:
            raise ValueError("expressions must share the decision cutoff and underlying")
        if any(utc(leg["expiry_at"]) < exit_at for leg in candidate["candidate_positions"]):
            raise ValueError("the common exit cannot follow a candidate expiry")
        for size in item["sizes"]:
            models = size["models"]
            if common_models is not None and common_models != models.keys():
                raise ValueError("expressions and sizes must use the same scenario families")
            common_models = models.keys()
            if any(utc(model["exit_at"]) != exit_at for model in models.values()):
                raise ValueError("each liquidation model must use the common exit")
        sizing = size_report({
            **limits, "sizes": item["sizes"], "one_lot_candidate": {
                **{key: candidate[key] for key in ("price_case", "tested_limit", "candidate_positions")},
                "portfolio": raw["portfolio"],
                "available_loss_budget_dollars": raw["available_loss_budget_dollars"],
                **({"operational_context": raw["operational_context"]} if "operational_context" in raw else {}),
            },
        }, require_operational_context=require_operational_context)
        quantity = sizing["selected_contracts"]
        selected = next((row for row in sizing["sizes"] if row["contracts"] == quantity), None)
        candidates.append({"name": name, "long_strike": contract["long_strike"],
                           "short_strike": contract.get("short_strike"), "right": contract["right"],
                           "expiry": contract["expiry"], "tested_limit": candidate["tested_limit"],
                           "selected_contracts": quantity, "selected_size": selected,
                           "sizing_cash_reasons": sizing["reasons"], "sizes": sizing["sizes"]})
    if not candidates:
        raise ValueError("provide at least one expression")
    ranked = sorted((row for row in candidates if row["selected_size"] is not None), key=lambda row: (
        -Decimal(row["selected_size"]["worst_expected_log_growth"]),
        Decimal(row["selected_size"]["modeled_loss_bound_dollars"]), row["name"],
    ))
    return {"basis": "supplied_expression_and_size_distributions", "input_sha256": digest(raw),
            "as_of": cutoff, "exit_at": exit_at,
            "status": "research_candidate" if ranked else "cash",
            "selected_expression": ranked[0]["name"] if ranked else None,
            "ranking": [row["name"] for row in ranked], "expressions": candidates}
