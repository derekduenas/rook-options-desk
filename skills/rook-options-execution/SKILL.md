---
name: rook-options-execution
description: Evaluate options execution, entry limits, and portfolio risk for the Rook Options Desk. Use for exact-contract pricing, 0DTE cutoff checks, liquidity, realistic fill comparisons, and pre-trade research.
---

# Rook Options Execution

Use the workspace containing `optionsdesk.py`, normally `~/optionsdesk`. Read **Decision inputs and limits**, **Capture and measurement**, or **Volatility and portfolio risk** in the README according to the task.

- Verify OCC identity, deliverable, expiration, and any venue or broker cutoff. Supply exact `contract.expiry_at` for same-day price-limit work. Check market time, receipt time, both quote sides, displayed size, and spread width. Keep the broker cutoff distinct from the exchange's final trading time.
- For a long option, compare a buy ask or declared entry with an exit bid that supports the proposed size. For a vertical, distinguish a package book from crossed single-leg prices. Mark quotes, historical prints, end-of-day labels, and observed fills as different evidence. Quote snapshots establish neither queue position nor guaranteed fill.
- Run `python3 optionsdesk.py price-limits FILE` when the case supplies joint fill and liquidation distributions at each tested limit. Include the baseline and adverse-selection assumptions. Report the highest tested passing limit, the tested limit with the best worst-model expectancy, and why other prices fail. Passing prices may be disjoint. `examples/price-limits.json` is a synthetic rehearsal.
- For portfolio exposure, run `portfolio-stress` on a supplied book and risk mandate. Review joint spot, IV, time, and liquidity shocks, concentration, and gross assignment obligations. Missing account limits or fill evidence leaves sizing or execution readiness unresolved.

Use `archive-replay` for timestamped historical evidence and the scorecard for observed prospective or paper outcomes. Screeners and research scores can support a pass or a researched entry price. Any broker action requires separate existing authorization and its own execution workflow.
