---
name: rook-options-volatility
description: Analyze IV surfaces, realized variance, skew, gamma, advanced Greeks, or scenario P&L in the Rook Options Desk. Use for vol crush, term structure, dealer-gamma evidence, and volatility model comparisons.
---

# Rook Options Volatility

Use the workspace containing `optionsdesk.py` and `advanced_risk.py`, normally `~/optionsdesk`. Read **Volatility and portfolio risk** in the README for the supported schemas.

- Run `python3 desk_run.py --engine volatility` for the saved walk-forward forecast comparison. The power-law filter is a volatility forecast challenger. Its daily-close QLIKE result measures variance forecast error.
- For a supplied surface case, run `python3 optionsdesk.py iv-surface FILE`. Use synchronized bid, mid, and ask derived IV at comparable forward moneyness. Give calendar and strike diagnostics and note American-exercise and discrete-dividend limitations of the European proxy.
- For a book, enable `advanced_greeks` in `portfolio-stress`. Report signed exposure per underlying with units. Vega and vanna use one IV point, theta and charm one calendar day, and gamma a $1 stock move. Use joint full repricing for large moves and position-specific IV shifts for skew stress. Attribute pre-cost mark changes to spot, IV, and time and report execution and assignment exposure separately.
- For earnings variance, bracket a single event with one expiry before and two after, use comparable option rights and moneyness, and retain bid/ask bounds. Label the estimate as market-implied under the background-variance assumption. Use the Catalyst skill for the event timing and evidence.

Keep portfolio gamma distinct from estimated dealer gamma. When signed inventory is unavailable, describe any public positioning proxy or sign sensitivities. Research models such as SVI, scheduled jumps, and rough volatility compete against the existing baseline under timestamped inputs and out-of-sample evaluation.
