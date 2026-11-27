# Options Desk architecture

Options Desk has a local system of record and a separately deployed private terminal. The same browser components render the local journal and the hosted publication model.

| Component | Responsibility |
|---|---|
| Provider adapters | Normalize Robinhood and Alpaca observations. Preserve the feed label, quote time, receipt time, and dated open interest when the provider supplies it. |
| Local research journal | Store market captures and frozen decisions in SQLite. A decision selects only observations available at its cutoff. Replay verifies the decision hash before pricing. |
| Intelligence reviews | Store analyst provenance, cited claims, source availability, cutoff, and actual receipt. Optional paired forecasts use the same frozen scenarios and costs. Check quote freshness at receipt. |
| Publication exporter | Read one consistent journal view. Select explicit decision IDs and project the market workspace into the versioned static snapshot with separate field allowlists. |
| Hosted terminal | Load relative assets and the versioned read model. Site access is owner-private. The workspace provides market search, close charts, recorded contract summaries, an expiration payoff builder, and browser-local paper plans. |

## Publication contract

The deployment contains five files under `dist/` plus the hosting manifest. The snapshot carries publication time, feed-level counts, the market workspace, and selected decisions. Those decisions include payoff summaries, scenario outcomes, and cited review excerpts. The market workspace includes reduced stock and ETF discovery rows, chart closes, and allowlisted contract and timestamped quote summaries. Raw broker payloads, broker identifiers, account and position details, research monitor records, full journal contents, and credentials stay on the local side.

The selected historical AAPL and EXE decisions are reference-only and their paired forecasts are unscored. The hosted workspace shows recorded market data and updates on redeployment. Paper plans are hypothetical, use browser-local storage, and have no broker order connection.

## Release verification

Require the local payoff and timing tests, the field-projection regression test, and the static browser flow to pass. Publish from the exact pushed source commit. Inspect the archive before deployment to confirm only the hosting manifest and static build are included.
