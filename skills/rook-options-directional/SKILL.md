---
name: rook-options-directional
description: Research directional stock and options setups for the Rook Options Desk. Use for relative strength, trend or breakout tests, options-flow candidates, and audits of whether a contract hit.
---

# Rook Options Directional

Use the workspace containing `desk_run.py`, normally `~/optionsdesk`. Run `python3 desk_run.py --engine directional` for the current historical benchmark and any saved intake context. Use an explicit market session, benchmark, decision cutoff, and forward horizon for new research.

## Follow the evidence from stock to option

1. Check stock direction against a relevant benchmark and the available market regime. Use completed bars at the decision cutoff. Compare against a fixed simple baseline under the same dates and conditions. Keep overlapping forward returns and stocks sharing dates from inflating the effective sample.
2. Match flow by ticker, exact expiration, right, and strike. Volume, open interest, and return columns generate candidates. A public print does not establish opening buyer flow, dealer positioning, or a single-leg intent.
3. For a pasted contract, report three observations separately when available: whether the stock touched expiration breakeven, whether the option traded at or below the reference entry and above it in a later interval, and the P&L implied by a feasible-size exit bid against the reference entry. Calls use strike plus debit at expiration. Puts use strike minus debit. An intraday print sequence does not establish the user's fill.
4. Compare any proposed long option or spread against shares and cash. Hand off contract execution and limit evaluation to `rook-options-execution` when the user asks for price, sizing, or viability. Report dates and quotes that were missing and identify any end-of-day proxies.

Use the Catalyst engine for event-dependent hypotheses and the Volatility engine for the price of convexity or volatility relative value. Keep the Directional engine useful during ordinary sessions without relying on an earnings calendar.
