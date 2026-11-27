---
name: rook-options-catalyst
description: Research scheduled or breaking catalysts in the Rook Options Desk. Use for earnings, macro announcements, corporate actions, and regulatory events where publication timing or confirmation changes the options analysis.
---

# Rook Options Catalyst

Use the workspace containing `desk_run.py`, normally `~/optionsdesk`. Run `python3 desk_run.py --engine catalyst` for event inventory, historical results, and the next earnings cutoff. Earnings Lab is one workflow in this engine. Read **Catalyst studies** or **Earnings Lab** in the README for the mode that applies.

1. Confirm the event with the original issuer, filing, exchange, or agency. Record the expected release window and the actual publication and desk receipt times separately. A webcast time gives a schedule anchor. It does not establish when results were published.
2. Freeze the universe, timestamped hypothesis, stock benchmark, options terms, entry and exit cutoffs, and cost assumptions before outcome access when running prospective research. Use `catalyst.event_type` to label new studies as earnings, macro, corporate, regulatory, or other. Distinguish a scheduled binary event from a news lead that has no verified release clock.
3. Capture what the market already priced through the comparable chain. For guidance changes, match fiscal period, currency, units, and accounting basis. Treat withdrawals and incomparable ranges as missing coverage. Store cited excerpts where the workflow requires journal evidence.
4. Compare against the price-only or event-free baseline on matched evidence. Score stock direction even when options cannot be priced. Show quote replay, observed paper fills, and missing outcomes as different evidence. Count one issuer publication once across strike or budget variants.

Use `earnings-status`, `capture-status`, and the desk scorecard to check what happened at a cutoff. Keep approved data capture separate from any authorization to record paper orders or send live orders.
