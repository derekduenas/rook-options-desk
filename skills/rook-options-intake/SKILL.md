---
name: rook-options-intake
description: Build or check the Rook Options Desk market intake from session hours, market regime, news, and option liquidity. Use for premarket briefs, news scans, and opens or closes that affect the desk's three engines.
---

# Rook Options Intake

Use the workspace containing `desk_run.py` and `market_scan.py`, normally `~/optionsdesk`. Read **Capture and measurement** in its README for the input contract. The latest saved input is `data/market-scan-input.json`. A local run evaluates that input and does not fetch fresh data.

- Confirm clock, exchange session, holidays, and any product or broker-specific cutoff that affects the requested contracts. Keep premarket, regular, and after-hours prints distinct.
- For current work, use connected market-data tools first. Collect SPY history through the last completed session and current quotes for SPY, QQQ, IWM, RSP, and SMH. Match prior closes to that session. Preserve both market timestamp and actual receipt timestamp. Call the breadth basket a proxy.
- For news, record headline, canonical source, related symbols, publication time, and actual receipt time. Keep headlines as leads until corroborated. Assign `primary_release` only when the underlying issuer or agency release has been verified. Describe partial coverage explicitly if a connector is inaccessible or a timestamp is missing.
- For sampled options, verify the exact contract, then collect both quote sides and displayed sizes. Keep quote-screen results separate from fills and liquidity that could disappear when an order arrives.

When refreshing the desk, write the normalized snapshot using the existing file shape and run `python3 desk_run.py`. Read `intake` from `data/research-run.json`. Report the observation time, what moved, the regime and evidence behind it, the next relevant cutoff, and any gaps. An incomplete breadth basket stays unclassified. A stale snapshot is a historical observation.
