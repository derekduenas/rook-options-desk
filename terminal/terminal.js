"use strict";
const $ = id => document.getElementById(id);
const escape = value => String(value ?? "—").replace(/[&<>"']/g, char => ({
  "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
}[char]));
const dollars = value => value == null ? "—" : new Intl.NumberFormat("en-US", {
  style:"currency",currency:"USD",maximumFractionDigits:2,minimumFractionDigits:2
}).format(Number(value));
const stamp = value => value ? new Intl.DateTimeFormat("en-US", {
  month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:"America/Los_Angeles"
}).format(new Date(value)) + " PT" : "—";
const whole = value => new Intl.NumberFormat("en-US").format(value);
const pill = (label, color = "") => `<span class="pill ${color}">${escape(label)}</span>`;
const symbol = decision => escape(decision.plan.symbol);
const strike = decision => `$${escape(decision.plan.long_strike)} / $${escape(decision.plan.short_strike)}`;
const titles = {
  workspace:["Trading terminal","Market discovery, contract quotes, and paper spreads."],
  overview:["Desk overview","Pricing, evidence, and decision replay."],
  decisions:["Decision journal","Inspect the snapshots and assumptions used in each decision."],
  intelligence:["Frontier intelligence","Cited analyst reviews and paired shadow forecasts."],
  directional:["Directional engine","Price trends, relative strength, and options flow."],
  volatility:["Volatility engine","Variance forecasts, implied volatility, and nonlinear risk."],
  "catalyst-engine":["Catalyst engine","Research around dated events, including earnings."],
  scanner:["Opportunity scanner","Regular-session watchlist from recent stock and option quotes."],
  intake:["Market intake","Market regime, news evidence, and option quote quality."],
  catalysts:["Catalyst studies","Compare the price of upside across option structures."],
  earnings:["Earnings Lab","Frozen event decisions, historical replay, and paper outcomes."],
  monitor:["Research monitor","Capture coverage, measured outcomes, and paper execution."],
  risk:["Risk and validation","Historical research, forward coverage, and pricing rehearsals."],
  sources:["Data sources","Inspect provenance and market-data coverage."],
  architecture:["System architecture","Market data, research decisions, intelligence, and publication."]
};
let data, selected, chartObserver;
const engineViews = ["directional","volatility","catalyst-engine"];

function currentView() {
  const view = location.hash.slice(1);
  const localOnly = [...engineViews,"catalysts","earnings","monitor","risk","intake","scanner"].includes(view);
  return Object.hasOwn(titles, view) && !(localOnly && data.mode === "published_snapshot") ? view : "workspace";
}

function metric(label, value, description) {
  return `<div class="metric"><small>${escape(label)}</small><strong>${escape(value)}</strong><span>${escape(description)}</span></div>`;
}
function renderRows() {
  if (!data) return;
  const query = $("search").value.trim().toLowerCase();
  const results = data.decisions.filter(d => [d.plan.symbol, d.plan.name, d.plan.expiry]
    .some(value => value.toLowerCase().includes(query)));
  $("rows").innerHTML = results.length ? `<div class="table-wrap"><table><thead><tr>
    <th>Ticker</th><th>Call strikes</th><th>Expiration</th><th>Net debit</th><th>Quote status</th><th>Review</th>
  </tr></thead><tbody>${results.map(d => `<tr>
    <td><button class="ticker-button" data-select="${escape(d.decision_id)}" aria-pressed="${selected === d.decision_id}">${symbol(d)}</button></td>
    <td class="number">${strike(d)}</td><td>${escape(d.plan.expiry)}</td>
    <td class="number">${dollars(d.analysis.synthetic_ask_debit_dollars)}</td>
    <td>${pill(d.analysis.quote_status === "stale" ? "STALE" : d.analysis.quote_status.toUpperCase(), d.analysis.quote_status === "fresh" ? "" : "amber")}</td>
    <td>${d.review ? escape(d.review.verdict) : "—"}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="empty">No decisions match this filter.</p>';
}
function payoff(decision) {
  const a = decision.analysis;
  const noBreakeven = Number(a.standalone_max_profit_dollars) < 0
    || (a.standalone_max_profit_dollars != null && a.expiration_breakeven_stock_price === null);
  const facts = [["Spread debit",dollars(a.synthetic_ask_debit_dollars)],["Max loss",dollars(a.standalone_max_loss_dollars)],
    ["Best-case P&L",dollars(a.standalone_max_profit_dollars)],
    ["Breakeven",noBreakeven ? "Unreachable" : dollars(a.expiration_breakeven_stock_price)]];
  return `<section class="panel"><div class="panel-head"><div><div class="asset-line"><strong class="asset">${symbol(decision)}</strong>
    ${pill(a.decision_status === "reference_only" ? "REFERENCE ONLY" : a.decision_status.toUpperCase(), a.quote_status === "fresh" ? "" : "amber")}</div>
    <p>${strike(decision)} call spread · ${escape(decision.plan.expiry)}</p></div></div>
    <div class="panel-body"><div class="quote-grid">${facts.map(([name,value]) => `<div><small>${escape(name)}</small><strong>${escape(value)}</strong></div>`).join("")}</div>
    ${a.scenarios?.length ? '<div class="chart-wrap"><canvas id="payoff-chart" role="img" aria-label="Profit and loss at expiration across stock prices"></canvas></div>' : '<p>No eligible spread quotes.</p>'}
    <p class="caption">Expiration payoff per spread · Conservative entry based on displayed leg quotes</p>
    ${a.scenarios?.length ? `<div class="comparison">${a.scenarios.map(s => `<span>${dollars(s.terminal_stock_price)} <strong>${dollars(s.pnl_dollars)}</strong></span>`).join("")}</div>` : ""}
    </div></section>`;
}
function review(decision) {
  const r = decision.review;
  return `<section class="panel"><div class="panel-head"><div><h2>Intelligence brief</h2><p>${r ? escape(r.analyst) + " · " + stamp(r.as_of) : "No review recorded"}</p></div>
    ${r ? pill(r.verdict.toUpperCase(), r.verdict === "pass" ? "amber" : "") : ""}</div>
    <div class="panel-body review-block">${r ? `<p>${escape(r.summary)}</p>
      <dl><dt>Invalidation</dt><dd>${escape(r.invalidation)}</dd></dl>
      <div class="evidence">${r.evidence.map(item => `<details><summary>${escape(item.title)} ${pill(item.status.toUpperCase(),"neutral")}</summary>
        <p>${escape(item.claim)}</p><p>Available ${stamp(item.available_at)}</p>${/^https?:\/\//i.test(item.source) ? `<a href="${escape(item.source)}" target="_blank" rel="noopener noreferrer">Open source ↗</a>` : `<p>Journal decision <span class="number">${escape(decision.decision_id.slice(0,12))}</span></p>`}</details>`).join("")}</div>
      <div class="comparison">${pill(r.comparison.status.toUpperCase(),"neutral")}
        ${r.comparison.delta_dollars == null ? escape(r.comparison.reason) : `Baseline <strong>${dollars(r.comparison.baseline)}</strong> Enhanced <strong>${dollars(r.comparison.enhanced)}</strong> Difference <strong>${dollars(r.comparison.delta_dollars)}</strong>`}
      </div>` : "<p>Import a cited review for this decision to compare evidence with the frozen payoff.</p>"}</div></section>`;
}

function renderChart(decision) {
  chartObserver?.disconnect();
  const canvas = $("payoff-chart");
  if (!canvas) return;
  const draw = () => {
    const ctx = canvas.getContext("2d"), scale = window.devicePixelRatio || 1;
    const {width:w,height:h} = canvas.getBoundingClientRect();
    if (!w || !h) return;
    canvas.width = w * scale; canvas.height = h * scale; ctx.scale(scale,scale);
    const a = decision.analysis, lower = Number(decision.plan.long_strike), upper = Number(decision.plan.short_strike);
    const width = upper-lower, loss = Number(a.standalone_max_loss_dollars), gain = Number(a.standalone_max_profit_dollars);
    const min = lower-width*.45,max = upper+width*.45, pad={left:57,right:14,top:13,bottom:27};
    const x = price => pad.left + (price-min)/(max-min)*(w-pad.left-pad.right);
    const top = Math.max(0,gain)+width*8, bottom = -loss-width*8;
    const y = pnl => pad.top + (top-pnl)/(top-bottom)*(h-pad.top-pad.bottom);
    ctx.font = "10px ui-monospace, monospace"; ctx.fillStyle = "#a7ad9e";
    const grid = (xa,ya,xb,yb,color) => {ctx.beginPath();ctx.moveTo(xa,ya);ctx.lineTo(xb,yb);ctx.strokeStyle=color;ctx.stroke();};
    const guides = [-loss,0];
    if (gain > 0) guides.push(gain);
    for (const pnl of guides) {
      grid(pad.left,y(pnl),w-pad.right,y(pnl),pnl===0?"#59644d":"#343b30");
      ctx.fillText((pnl<0?"−":"")+"$"+Math.abs(pnl).toFixed(0),1,y(pnl)+3);
    }
    for (const [i,price] of [min,lower,(upper+lower)/2,upper,max].entries()) {
      if (w < 340 && (i === 0 || i === 4)) continue;
      grid(x(price),pad.top,x(price),h-pad.bottom,"#30362b");
      ctx.textAlign=i===4?"right":"center";ctx.fillText("$"+(Number.isInteger(price)?price:price.toFixed(1)),x(price),h-6);
    }
    ctx.textAlign="left";
    const be = lower + loss / 100;
    const segments = gain > 0 ? [
      [[[min,-loss],[lower,-loss],[be,0]],"#e5a295","rgba(229,162,149,.11)"],
      [[[be,0],[upper,gain],[max,gain]],"#d5f28d","rgba(213,242,141,.13)"]
    ] : [[[[min,-loss],[lower,-loss],[upper,gain],[max,gain]],"#e5a295","rgba(229,162,149,.11)"]];
    for (const [points,color,fill] of segments) {
      ctx.beginPath();ctx.moveTo(x(points[0][0]),y(0));
      for(const [price,pnl] of points) ctx.lineTo(x(price),y(pnl));
      ctx.lineTo(x(points.at(-1)[0]),y(0));ctx.closePath();ctx.fillStyle=fill;ctx.fill();
      ctx.beginPath();ctx.moveTo(x(points[0][0]),y(points[0][1]));
      for(const [price,pnl] of points.slice(1)) ctx.lineTo(x(price),y(pnl));
      ctx.lineWidth=2;ctx.strokeStyle=color;ctx.stroke();ctx.lineWidth=1;
    }
  };
  chartObserver = new ResizeObserver(draw);
  chartObserver.observe(canvas);
  draw();
}
function renderCatalysts(studies) {
  $("catalysts").innerHTML = studies.length ? studies.map(({study, candidates, study_status, ranked_structures}) => {
    const structures = {outright:"Outright",debit_spread:"Debit spread"};
    const priced = candidates.some(c => c.outcomes);
    const stress = candidates.find(c => c.account_stress)?.account_stress;
    const simulations = candidates.filter(c => c.account_stress?.simulation_sizing);
    const fractionalStress = stress?.repeated_cluster_drawdown_pct != null;
    return `<article class="panel journal"><div class="panel-head"><div><h2>${escape(study.name)}</h2><p>${escape(study.catalyst.title)} · ${stamp(study.catalyst.event_at)}</p></div>${pill(study_status.replaceAll("_"," ").toUpperCase(),study_status === "reference_only" ? "amber" : "")}</div>
      <div class="panel-body"><p>${escape(study.universe_name)} · ${escape(study.expiry)} expiry · ${escape(study.target_scenario)}</p>
      <p class="caption">${ranked_structures.length ? "Highest target return after slippage: " + structures[ranked_structures[0]] : "Paired comparison unavailable."}</p></div>
      <div class="table-wrap"><table><thead><tr><th>Structure</th><th>Quotes</th><th>Entry</th><th>Target P&amp;L</th><th>Target return after slippage</th><th>Contracts at illustrative risk</th></tr></thead><tbody>
      ${candidates.map(c => `<tr><td>${structures[c.structure]}</td><td>${pill(c.quote_status.replaceAll("_"," "),c.quote_status === "fresh" ? "" : "amber")}</td><td class="number">${dollars(c.quoted_entry_debit_dollars)}</td><td class="number">${dollars(c.target_pnl_dollars)}</td><td class="number">${c.stressed_target_r == null ? "—" : escape(c.stressed_target_r) + "R"}</td><td class="number">${escape(c.account_stress?.contracts_within_illustrative_budget)}</td></tr>`).join("")}</tbody></table></div>
      ${priced ? `<div class="table-wrap"><table><thead><tr><th>Expiration scenario</th><th>Stock price</th><th>Outright P&amp;L</th><th>Spread P&amp;L</th></tr></thead><tbody>${study.scenarios.map(s => `<tr><td>${escape(s.name)}</td><td class="number">${dollars(s.terminal_stock_price)}</td>${candidates.map(c => `<td class="number">${dollars(c.outcomes?.find(o => o.name === s.name)?.pnl_dollars)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : ""}
      <div class="panel-body">${stress ? `<p>${fractionalStress ? "Illustrative fractional exposure stress" : "Illustrative account stress"}
        · ${escape(stress.loss_streak_drawdown_pct)}% loss-streak drawdown
        · ${escape(stress.simultaneous_full_loss_pct)}% simultaneous loss
        ${fractionalStress ? `· ${escape(stress.repeated_cluster_drawdown_pct)}% repeated-cluster drawdown` : ""}</p>` : ""}
        <p class="caption">Scenario P&amp;L uses quote-side entry and configured fees. ${candidates[0].expected_pnl_dollars == null ? "No probabilities assigned." : "Probabilities are plan assumptions."}</p></div>
      ${simulations.length ? `<div class="table-wrap"><table><thead><tr><th>Structure</th><th>Hypothetical simulation sizing</th><th>Drawdown limit breach</th><th>Median terminal equity</th></tr></thead><tbody>
      ${simulations.map(({structure,account_stress:simulation}) => {
        const sizing = simulation.simulation_status === "cash_only" ? "Whole contracts · Cash only at this budget"
          : simulation.simulation_sizing === "whole_contracts" ? "Whole contracts" : "Fractional exposure";
        return `<tr><td>${structures[structure]}</td><td>${sizing}</td><td class="number">${escape(simulation.simulated_breach_pct)}%</td>
          <td class="number">${escape(simulation.terminal_equity_multiples.p50)}×</td></tr>`;
      }).join("")}</tbody></table></div>` : ""}</article>`;
  }).join("") : '<p class="empty">No catalyst studies recorded.</p>';
}

const eventClock = new Intl.DateTimeFormat("en-US", {
  month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit",second:"2-digit",
  timeZone:"America/New_York",timeZoneName:"short"
});
const earningsStructures = {outright:"Outright",debit_spread:"Debit spread",underlying:"Underlying (last sale)",cash:"Cash"};

function eventTime(value) {
  const time = new Date(value);
  if (!value || Number.isNaN(time.getTime())) return "—";
  return `<time datetime="${escape(value)}" title="${escape(value)}">${escape(eventClock.format(time))}</time>`;
}

function earningsOutcome(outcome, pending) {
  if (!outcome) return `<span class="earnings-muted">${pending ? "Pending" : "—"}</span>`;
  const pnl = outcome.pnl_dollars;
  const tone = Number(pnl) < 0 ? "danger" : Number(pnl) > 0 ? "accent" : "";
  return `<span class="earnings-outcome"><span>${escape(outcome.status?.replaceAll("_"," "))}</span>
    ${pnl == null ? "" : `<strong class="number ${tone}">${dollars(pnl)}</strong>`}
    ${outcome.classification === "pre_after_release" ? pill("Excluded · after release", "amber") : ""}</span>`;
}

function earningsPhase(title, entry) {
  if (!entry) return `<section class="earnings-phase"><div class="earnings-phase-head"><h4>${title}</h4>
    ${pill("Pending decision","neutral")}</div><p>Awaiting a frozen decision for this cutoff.</p></section>`;
  const {action, classification, candidates, replay, paper} = entry;
  const actionLabel = action === "paper" ? "Paper research" : "Pass";
  return `<section class="earnings-phase" title="${escape(entry.entry_id)}">
    <div class="earnings-phase-head"><h4>${title}</h4>${pill(actionLabel, action === "pass" ? "amber" : "")}
    ${classification ? pill(classification.replaceAll("_"," "),classification === "prospective" ? "neutral" : "amber") : ""}</div>
    <p>Frozen as of ${eventTime(entry.as_of)}${entry.recorded_at ? ` · Recorded ${eventTime(entry.recorded_at)}` : ""}
      ${entry.reason ? ` · ${escape(entry.reason)}` : ""}</p>
    ${candidates?.length ? `<div class="table-wrap" tabindex="0" role="region" aria-label="${title} candidate outcomes"><table>
      <thead><tr><th scope="col">Structure</th><th scope="col">Entry status</th><th scope="col">Qty</th><th scope="col">Entry cost</th>
      <th scope="col">Replay outcome</th><th scope="col">Paper outcome</th></tr></thead><tbody>
      ${candidates.map(candidate => {
        const sized = candidate.quantity > 0;
        const replayPending = !replay && (sized || candidate.structure === "cash");
        const paperPending = action === "paper" && classification === "prospective" && sized && candidate.structure !== "cash";
        const replayOutcome = replay?.outcomes?.find(outcome => outcome.structure === candidate.structure);
        const paperOutcome = paper?.find(outcome => outcome.structure === candidate.structure);
        return `<tr><th scope="row">${escape(earningsStructures[candidate.structure] ?? candidate.structure)}</th>
          <td>${escape(candidate.status?.replaceAll("_"," "))}</td><td class="number">${escape(candidate.quantity)}</td>
          <td class="number">${dollars(candidate.entry_cost_dollars)}</td>
          <td>${earningsOutcome(replayOutcome,replayPending)}</td><td>${earningsOutcome(paperOutcome,paperPending)}</td></tr>`;
      }).join("")}</tbody></table></div>` : ""}</section>`;
}

function renderEarnings(experiments) {
  $("earnings").innerHTML = experiments.length ? experiments.map(({experiment_id, registered_at, protocol, events}) => `
    <article class="panel" title="${escape(experiment_id)}">
      <div class="panel-head"><div><h2>${escape(protocol.name)}</h2><p>Registered ${eventTime(registered_at)}</p></div></div>
      <div class="panel-body earnings-protocol"><p>Universe <strong>${escape(protocol.universe.join(", "))}</strong></p>
        <p>Risk budget <strong class="number">${dollars(protocol.risk_budget_dollars)}</strong></p></div>
      ${events.map((event,index) => {
        const cutoffs = [["Scheduled event",event.event_at],["Pre-earnings cutoff",event.pre_at],
          ["Post-earnings cutoff",event.post_at],["Exit cutoff",event.exit_at]];
        const entries = event.entries ?? [];
        const pre = entries.find(entry => entry.phase === "pre");
        const post = entries.find(entry => entry.phase === "post");
        return `<details class="earnings-event" ${index === 0 ? "open" : ""} title="${escape(event.event_id)}">
          <summary><span class="earnings-event-heading"><strong class="number">${escape(event.symbol)}</strong>
          <span>${escape(event.title)}</span></span></summary>
          <div class="earnings-event-body"><dl class="earnings-cutoffs">${cutoffs.map(([label,time]) => `
            <div><dt>${label}</dt><dd>${eventTime(time)}</dd></div>`).join("")}</dl>
          ${/^https?:\/\//i.test(event.source) ? `<p><a class="earnings-source" href="${escape(event.source)}" target="_blank" rel="noopener noreferrer">Issuer release schedule ↗</a></p>` : ""}
          ${earningsPhase("Pre-earnings",pre)}${earningsPhase("Post-earnings",post)}</div></details>`;
      }).join("")}</article>`).join("")
    : '<article class="panel"><p class="empty">Register an earnings experiment to see its frozen cutoffs and outcomes here.</p></article>';
}

const captureStages = {pre:"Pre-earnings",post:"Post-earnings",exit:"Exit"};
const captureStates = {scheduled:"Scheduled",capture_window:"Capture window",captured:"Captured",partial:"Partial",missing:"Missing"};
const connectionStates = {configured:"Credentials present",credentials_required:"Credentials needed",connector_import_required:"Import required"};

function monitorCaptures({tasks, as_of}) {
  return `<section class="panel"><div class="panel-head"><div><h2>Capture cutoffs</h2>
    <p>Checked ${eventTime(as_of)}. Scheduled rows show quote quality at the check. Completed windows use evidence available by the cutoff.</p></div></div>
    ${tasks.length ? `<div class="table-wrap" tabindex="0" role="region" aria-label="Capture readiness">
      <table><thead><tr><th scope="col">Event</th><th scope="col">Cutoff</th><th scope="col">Status</th>
      <th scope="col">Fresh quote legs</th><th scope="col">Underlying</th><th scope="col">Connection</th>
      <th scope="col">Last capture attempt</th></tr></thead><tbody>
      ${tasks.map(task => {
        const attempt = task.last_attempt;
        const statusColor = task.status === "captured" ? "" : task.status === "scheduled" ? "neutral" : "amber";
        return `<tr><th scope="row"><span class="monitor-stack"><strong class="number">${escape(task.symbol)} · ${escape(captureStages[task.stage])}</strong>
          ${task.experiment ? `<span>${escape(task.experiment)}</span>` : ""}</span></th>
          <td><span class="monitor-stack">${eventTime(task.cutoff)}${task.capture_window_seconds
            ? `<small>Capture within ${escape(task.capture_window_seconds)}s before cutoff</small>` : ""}</span></td>
          <td>${pill(captureStates[task.status] ?? task.status,statusColor)}</td>
          <td><span class="monitor-stack"><span class="number">${escape(task.fresh_option_legs)} / 2${task.fresh_option_legs === 2 && task.synchronized === false ? " · Out of sync" : ""}</span>
            ${(task.quote_legs ?? []).map(leg => `<small>${escape(leg.contract)} · ${escape(leg.status.replaceAll("_"," "))}</small>`).join("")}</span></td>
          <td>${escape(task.underlying_status ?? (task.fresh_underlying ? "fresh" : "awaiting fresh trade"))}</td>
          <td><span class="monitor-stack"><span>Options · ${escape(connectionStates[task.transport?.quote])}</span>
          ${task.feeds ? `<small>${escape(task.feeds.quote.join(" / "))}</small>` : ""}
          <span>Underlying · ${escape(connectionStates[task.transport?.underlying])}</span>
          ${task.feeds ? `<small>${escape(task.feeds.underlying.join(" / "))}</small>` : ""}</span></td>
          <td>${attempt ? `<span class="monitor-stack">${eventTime(attempt.at)}${Object.entries(attempt.feeds ?? {}).map(([kind,state]) =>
              `<span>${kind === "quote" ? "Options" : "Underlying"} · ${escape(state.replaceAll("_"," "))}</span>`).join("")}</span>` : "—"}</td></tr>`;
      }).join("")}</tbody></table></div>` : '<p class="empty">Register an earnings event to schedule its capture cutoffs.</p>'}
  </section>`;
}

function monitorCoverage(cohorts) {
  return `<section class="panel"><div class="panel-head"><div><h2>Cohort coverage</h2>
    <p>One cohort per registered experiment and entry phase.</p></div></div>
    ${cohorts.length ? `<div class="table-wrap" tabindex="0" role="region" aria-label="Cohort coverage counts">
      <table><thead><tr><th scope="col">Experiment</th><th scope="col">Phase</th>
      <th scope="col">Scheduled</th><th scope="col">Pending</th><th scope="col">Missed</th>
      <th scope="col">Eligible</th><th scope="col">Excluded</th><th scope="col">Passes</th><th scope="col">Awaiting exit</th>
      <th scope="col">Complete replay</th><th scope="col">Partial replay</th><th scope="col">Cash-only replay</th><th scope="col">Missing replay</th>
      <th scope="col">Paper P&amp;L samples</th><th scope="col">Excluded paper attempts</th></tr></thead><tbody>
      ${cohorts.map(({name,phase,coverage,paper}) => `<tr><th scope="row">${escape(name)}</th><td>${escape(captureStages[phase])}</td>
        ${[coverage.scheduled,coverage.pending,coverage.missed,coverage.eligible,coverage.excluded,coverage.passes,
          coverage.replay_pending,coverage.replay_complete,coverage.replay_partial,coverage.replay_cash_only ?? 0,coverage.replay_missing,
          Object.values(paper.structures).reduce((sum,row) => sum + row.observed_pnl,0),coverage.excluded_paper_attempts ?? 0]
          .map(count => `<td class="number">${whole(count)}</td>`).join("")}
        </tr>`).join("")}</tbody></table></div>` : '<p class="empty">Coverage starts with a registered earnings experiment.</p>'}
  </section>`;
}

function monitorOutcomes(cohorts) {
  const rows = cohorts.flatMap(({name,phase,replay,paper}) => Object.entries(replay.structures).map(([structure, row]) => ({
    name,phase,structure,replay:row,paper:paper.structures[structure]
  })).filter(row => row.replay.eligible || row.paper?.eligible_candidates || Object.keys(row.replay.entry_blockers ?? {}).length));
  return `<section class="panel"><div class="panel-head"><div><h2>Measured outcomes</h2>
    <p>Positive slippage is a cost. Positive midpoint markouts are favorable. Spread markouts use individual leg midpoints.</p></div></div>
    ${rows.length ? `<div class="table-wrap" tabindex="0" role="region" aria-label="Replay and paper outcomes">
      <table><thead><tr><th scope="col">Cohort</th><th scope="col">Structure</th><th scope="col">Entry blockers</th><th scope="col">Replay P&amp;L / due</th>
      <th scope="col">Replay mean net P&amp;L</th><th scope="col">Eligible recorded paper attempts</th><th scope="col">Paper attempts / due</th>
      <th scope="col">Any fill rate</th><th scope="col">Filled / frozen units</th><th scope="col">Paper P&amp;L samples</th><th scope="col">Paper mean net P&amp;L</th>
      <th scope="col">Round-trip slippage</th><th scope="col">Entry midpoint markouts</th></tr></thead><tbody>
      ${rows.map(({name,phase,structure,replay,paper}) => `<tr><th scope="row"><span class="monitor-stack"><strong>${escape(name)}</strong>
        <span>${escape(captureStages[phase])}</span></span></th><td>${escape(earningsStructures[structure])}</td>
        <td>${Object.entries(replay.entry_blockers ?? {}).map(([status,count]) => `${escape(status.replaceAll("_"," "))} · ${whole(count)}`).join(", ") || "—"}</td>
        <td class="number">${replay.eligible > replay.pending ? `${whole(replay.observed)} / ${whole(replay.eligible - replay.pending)}` : "—"}</td>
        <td class="number">${dollars(replay.mean_net_pnl_dollars)}</td>
        <td class="number">${paper ? whole(paper.attempted) : "—"}</td>
        <td class="number">${paper?.eligible_after_exit ? `${whole(paper.attempted_after_exit)} / ${whole(paper.eligible_after_exit)}` : "—"}</td>
        <td class="number">${paper?.any_fill_rate_pct == null ? "—" : `${escape(paper.any_fill_rate_pct)}%`}</td>
        <td class="number">${paper?.attempted_quantity ? `${whole(paper.filled_quantity)} / ${whole(paper.attempted_quantity)}` : "—"}</td>
        <td class="number">${paper ? whole(paper.observed_pnl) : "—"}</td>
        <td class="number">${dollars(paper?.mean_net_pnl_dollars)}</td>
        <td class="number">${dollars(paper?.mean_round_trip_slippage_dollars)}${paper?.round_trip_slippage_samples ? ` · n=${whole(paper.round_trip_slippage_samples)}` : ""}</td>
        <td class="number">${Object.entries(paper?.entry_markouts ?? {}).filter(([,mark]) => mark.eligible_fills).map(([horizon,mark]) => {
          const gaps = Object.entries(mark.incomplete_reasons ?? {}).map(([reason, count]) =>
            `${reason.replaceAll("_", " ")} (${whole(count)})`).join(", ");
          return `${Number(horizon) === 300 ? "5m" : escape(horizon) + "s"} ${dollars(mark.mean_midpoint_move_dollars)} · ${whole(mark.midpoint_move_samples)}/${whole(mark.eligible_fills)}${gaps ? ` <small>${escape(gaps)}</small>` : ""}`;
        }).join("<br>") || "—"}</td></tr>`).join("")}
      </tbody></table></div>` : '<p class="empty">Awaiting the first replay outcome or recorded paper attempt.</p>'}
  </section>`;
}

function monitorEvidence(evidence) {
  if (!evidence?.length) return "";
  return `<article class="panel"><div class="panel-head"><div><h2>Research evidence</h2><p>Recent cited claims available to the desk</p></div></div>
    <div class="table-wrap" tabindex="0" role="region" aria-label="Recent research evidence"><table><thead><tr>
      <th scope="col">Subject</th><th scope="col">Relationship</th><th scope="col">Source excerpt</th><th scope="col">Provenance</th>
    </tr></thead><tbody>${evidence.map(row => `<tr><th scope="row">${escape(row.subject)}</th>
      <td>${escape(row.predicate.replaceAll("_"," "))} · ${escape(row.object)}</td>
      <td>${escape(row.excerpt)} ${/^https:\/\//i.test(row.source) ? `<a class="earnings-source" href="${escape(row.source)}" target="_blank" rel="noopener noreferrer">Open source ↗</a>` : ""}</td>
      <td><span class="monitor-stack">${pill(row.origin === "model" ? "Model proposal" : "Human claim", row.origin === "model" ? "amber" : "neutral")}
      ${eventTime(row.recorded_at)}</span></td></tr>`).join("")}</tbody></table></div></article>`;
}

function monitorComparisons({cohorts, phase_comparisons}) {
  const names = new Map(cohorts.map(row => [row.experiment_id,row.name]));
  const withinPhase = cohorts.flatMap(cohort => (cohort.replay.comparisons ?? []).map(row => ({
    ...row, cohort:`${cohort.name} · ${captureStages[cohort.phase]}`,
    left:earningsStructures[row.left],right:earningsStructures[row.right]
  })));
  const acrossPhases = (phase_comparisons ?? []).map(row => ({...row,
    cohort:`${names.get(row.experiment_id)} · ${earningsStructures[row.structure]}`,
    left:captureStages[row.left],right:captureStages[row.right]
  }));
  const rows = [...withinPhase,...acrossPhases].filter(row => row.paired || row.left_only || row.right_only);
  return `<section class="panel"><div class="panel-head"><div><h2>Matched replay comparisons</h2>
    <p>Mean difference uses left minus right on the same events. The historical archive runs separately through <code>archive-replay</code>.</p></div></div>
    ${rows.length ? `<div class="table-wrap" tabindex="0" role="region" aria-label="Matched replay comparisons"><table>
      <thead><tr><th scope="col">Cohort</th><th scope="col">Left</th><th scope="col">Right</th><th scope="col">Matched</th>
      <th scope="col">Left only</th><th scope="col">Right only</th><th scope="col">Mean difference</th><th scope="col">95% interval</th></tr></thead><tbody>
      ${rows.map(row => `<tr><th scope="row">${escape(row.cohort)}</th><td>${escape(row.left)}</td><td>${escape(row.right)}</td>
        <td class="number">${whole(row.paired)}</td><td class="number">${whole(row.left_only)}</td><td class="number">${whole(row.right_only)}</td>
        <td class="number">${dollars(row.mean_delta_dollars)}</td>
        <td class="number">${row.interval_95_dollars ? row.interval_95_dollars.map(dollars).join(" to ") : "—"}</td></tr>`).join("")}
      </tbody></table></div>` : '<p class="empty">Awaiting the first comparable replay outcomes.</p>'}</section>`;
}

function monitorEventQuantiles(report) {
  if (!report?.reports?.length) return "";
  const score = (row, arm) => row.scores?.[arm]?.mean_pinball_loss_bps ?? "—";
  return `<section class="panel"><div class="panel-head"><div><h2>Post-release forecast comparison</h2>
    <p>Paired return forecasts. Lower loss is better. ${whole(report.review_sample_event_days)} decision days required for review.</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Experiment</th><th>Decision days</th><th>Numerical loss (bps)</th>
    <th>Cited-claims loss (bps)</th><th>Status</th></tr></thead><tbody>${report.reports.map(row =>
      `<tr><th scope="row">${escape(row.name)}</th><td>${whole(row.scored_event_days)}</td>
      <td>${escape(score(row,"numerical"))}</td><td>${escape(score(row,"with_claims"))}</td>
      <td>${escape(row.status.replaceAll("_"," "))}</td></tr>`).join("")}</tbody></table></div></section>`;
}

function renderResearchMonitor(monitor) {
  if (!monitor) {
    $("monitor").innerHTML = '<article class="panel"><p class="empty">Generate a new local snapshot to load the research monitor.</p></article>';
    return;
  }
  const {capture,scorecard} = monitor;
  const {cohorts} = scorecard;
  const captured = capture.tasks.filter(task => task.status === "captured").length;
  const due = capture.tasks.filter(task => ["captured","partial","missing"].includes(task.status)).length;
  const eligible = cohorts.reduce((sum,row) => sum + row.coverage.eligible,0);
  const completed = cohorts.reduce((sum,row) => sum + row.coverage.replay_complete,0);
  const attempts = cohorts.flatMap(row => Object.values(row.paper.structures)).reduce((sum,row) => sum + row.attempted,0);
  $("monitor").innerHTML = `<div class="metrics">
    ${metric("Captured cutoffs",due ? `${whole(captured)} / ${whole(due)}` : "—",`${whole(due)} due · ${whole(capture.tasks.length - due)} awaiting cutoff`)}
    ${metric("Eligible decisions",whole(eligible),"Prospective pre- and post-earnings phases")}
    ${metric("Complete replays",whole(completed),"All eligible structure outcomes priced")}
    ${metric("Eligible paper attempts",whole(attempts),"Recorded fills, partial fills, and unfilled attempts")}</div>
    ${monitorCaptures(capture)}${monitorCoverage(cohorts)}${monitorOutcomes(cohorts)}${monitorComparisons(scorecard)}
    ${monitorEventQuantiles(monitor.event_quantiles)}${monitorEvidence(monitor.evidence)}`;
}

function renderEngine(view, engines) {
  const engine = engines?.[view === "catalyst-engine" ? "catalyst" : view];
  if (!engine) {
    $("engines").innerHTML = '<article class="panel"><p class="empty">Run the local desk to load this engine’s results.</p></article>';
    return;
  }
  if (view === "directional") {
    const {benchmark,screener} = engine;
    $("engines").innerHTML = `<div class="metrics">
      ${metric("Benchmark",benchmark?.benchmark ?? "—","Market-relative forecast")}
      ${metric("Forecast windows",benchmark ? whole(benchmark.distinct_windows) : "—","Non-overlapping, no event filter")}
      ${metric("Direction correct",benchmark ? `${whole(benchmark.correct)} / ${whole(benchmark.symbol_windows)}` : "—","Relative-strength challenger")}
      ${metric("Flow rows audited",screener ? whole(screener.rows) : "—","Stock breakeven and sampled option quotes")}</div>
      <article class="panel panel-body"><h2>Price and flow evidence</h2>
        ${benchmark ? `<p>Compares ${whole(benchmark.lookback_sessions)}-session relative strength with the next ${whole(benchmark.horizon_sessions)} sessions. Always forecasting outperformance got ${whole(benchmark.always_outperform_correct)} cases right. Stock return forecasts do not measure option returns.</p>` : '<p class="empty">No directional benchmark for this journal.</p>'}
        ${screener ? `<p>The screener audit recorded ${whole(screener.stock_breakeven_touches)} stock price touches and ${whole(screener.option_quote_snapshots)} option quote snapshots.</p>` : ""}
      </article>`;
    return;
  }
  if (view === "volatility") {
    const {benchmark,overnight,regime_diagnostics:diagnostics} = engine;
    const names = {ewma:"EWMA",mean_20:"20-session mean",power_law:"Power-law variance"};
    $("engines").innerHTML = `<div class="metrics">
      ${metric("Forecast windows",benchmark ? whole(benchmark.distinct_test_windows) : "—","Non-overlapping five-session windows")}
      ${metric("Symbol windows",benchmark ? whole(benchmark.symbol_windows) : "—","Historical daily stock closes")}</div>
      <article class="panel"><div class="panel-head"><div><h2>Realized variance benchmark</h2><p>Mean QLIKE. Lower is better.</p></div></div>
      ${benchmark ? `<div class="table-wrap"><table><thead><tr><th>Model</th><th>Mean QLIKE</th></tr></thead><tbody>${Object.entries(benchmark.mean_qlike).sort((a,b) => a[1]-b[1]).map(([model,score]) => `<tr><th scope="row">${escape(names[model] || model)}</th><td>${Number(score).toFixed(4)}</td></tr>`).join("")}</tbody></table></div>` : '<p class="empty">No matched volatility history for this journal.</p>'}</article>
      <article class="panel"><div class="panel-head"><div><h2>Overnight variance challenger</h2><p>Mean QLIKE on matched test windows${overnight?.archive ? ` · ${escape(overnight.archive.symbol)} archived bars · assumed availability` : ""}</p></div></div>
      ${overnight?.mean_qlike ? `<div class="table-wrap"><table><thead><tr><th>Model</th><th>${whole(overnight.complete_test_windows)} test windows</th></tr></thead><tbody>
        ${Object.entries(overnight.mean_qlike).map(([key,value]) => `<tr><th scope="row">${escape(({har:"HAR",har_overnight:"HAR with overnight inputs",recent_mean:"22-session mean",power_law:"Power-law variance"})[key] ?? key)}</th><td>${Number(value).toFixed(4)}</td></tr>`).join("")}
        </tbody></table></div>` : `<p class="empty">${overnight ? "Awaiting enough complete forecast windows." : "Awaiting timestamped five-minute sessions."}</p>`}</article>
      <article class="panel panel-body"><h2>Jump and correlation diagnostics</h2>
      ${diagnostics ? `<p>As of ${stamp(diagnostics.as_of)}. Hawkes jump probability ${(100 * diagnostics.fixed_hawkes_probability_of_jump).toFixed(1)}% versus ${(100 * diagnostics.poisson_count_baseline_probability).toFixed(1)}% for the count baseline over ${whole(diagnostics.horizon_seconds)} seconds.</p>
      <p>${diagnostics.correlation.status === "computed" ? `Largest correlation mode ${(100 * diagnostics.correlation.largest_eigenvalue_share).toFixed(1)}% · Normalized spectral entropy ${diagnostics.correlation.normalized_spectral_entropy.toFixed(2)}.` : "Awaiting sufficient aligned returns."}</p>` : '<p class="empty">Awaiting timestamped jumps and aligned returns.</p>'}</article>
      <article class="panel panel-body"><h2>Volatility surface and Greeks</h2><p>Use synchronized chains to inspect skew and event variance. Joint spot, IV, and time shocks are available in <a href="#risk">Risk and validation</a>.</p></article>`;
    return;
  }
  const next = engine.next_earnings_cutoff;
  const study = engine.earnings_benchmark;
  const typed = Object.entries(engine.event_types).map(([kind,count]) => `${escape(kind)} ${whole(count)}`).join(" · ");
  $("engines").innerHTML = `<div class="metrics">
    ${metric("Frozen catalyst studies",whole(engine.frozen_catalyst_studies),"Macro, corporate, regulatory, and earnings events")}
    ${metric("Historical earnings events",study ? whole(study.events) : "—","Earnings research within Catalyst")}
    ${metric("Next earnings cutoff",next ? `${next.symbol} · ${next.stage}` : "—",next ? stamp(next.cutoff) : "No upcoming observations")}</div>
    <article class="panel panel-body"><h2>Event coverage</h2>
      ${typed ? `<p>Frozen study counts by type · ${typed}</p>` : '<p class="empty">No frozen catalyst studies.</p>'}
      ${study ? `<p>Earnings challenger scored ${whole(study.relative_strength.correct)} of ${whole(study.events)}. Always down or flat scored ${whole(study.always_down_or_flat.correct)}.</p>` : ""}
      <p><a href="#earnings">Open Earnings Lab</a> · <a href="#catalysts">Open Catalyst studies</a></p>
    </article>`;
}

function renderOpportunityScan(scan, error) {
  if (!scan || error) {
    $("scanner").innerHTML = `<article class="panel panel-body"><p>${escape(error || "Awaiting scanner configuration and incoming quotes.")}</p></article>`;
    return;
  }
  const state = {market_closed:"Regular session closed",calendar_coverage_ended:"Calendar coverage ended",
    watchlist_available:"Watchlist available",no_eligible_watchlist_leads:"No contracts passed the screen"}[scan.status];
  const row = item => `<tr><th scope="row">${escape(item.contract)}</th>
    <td>${item.underlying_move_pct == null ? "—" : Number(item.underlying_move_pct).toFixed(2) + "%"}</td>
    <td>${dollars(item.bid)} / ${dollars(item.ask)}</td><td>${dollars(item.one_lot_reserve_dollars)}</td>
    <td>${item.reasons.length ? escape(item.reasons.map(reason => reason.replaceAll("_", " ")).join(", ")) : "Watchlist lead"}</td></tr>`;
  $("scanner").innerHTML = `<div class="metrics">
    ${metric("Scanner status",state ?? scan.status,`Evaluated ${stamp(scan.as_of)}`)}
    ${metric("Contracts screened",whole(scan.tested),`${whole(scan.leads.length)} watchlist leads`)}
    ${metric("Option feed",scan.feeds.option_feed.join(" / "),scan.next_open_at ? `Next open ${eventTime(scan.next_open_at)}` : "Regular-session calendar")}
    </div>
    ${scan.discovery?.last_received_at ? `<article class="panel"><div class="panel-head"><div><h2>Robinhood market discovery</h2>
      <p>${whole(scan.discovery.distinct_usable_symbols)} distinct symbols · Received ${stamp(scan.discovery.last_received_at)}${scan.discovery.count_discrepancy
        ? ` · Robinhood reported ${whole(scan.discovery.unpartitioned_matches)} matches without partitioning` : ""}</p></div></div>
      <div class="table-wrap"><table><thead><tr><th>Underlying</th><th>Scanner price</th><th>Change</th><th>Options volume</th></tr></thead>
      <tbody>${scan.discovery.most_active.map(item => `<tr><th scope="row">${escape(item.symbol)}</th><td>${dollars(item.last)}</td>
      <td>${Number(item.change_pct).toFixed(2)}%</td><td>${whole(item.options_volume)}</td></tr>`).join("")}</tbody></table></div></article>` : ""}
    <article class="panel"><div class="panel-head"><div><h2>Contract watchlist</h2>
    <p>Ranked by spread and confirmed underlying movement. Pricing and portfolio review remain separate steps.</p></div></div>
    ${scan.tested ? `<div class="table-wrap"><table><thead><tr><th scope="col">Contract</th><th scope="col">Underlying move</th>
      <th scope="col">Bid / ask</th><th scope="col">One-lot reserve</th><th scope="col">Screen result</th></tr></thead>
      <tbody>${[...scan.leads,...scan.excluded].map(row).join("")}</tbody></table></div>` : '<p class="panel-body empty">Awaiting the next configured regular session.</p>'}</article>`;
}

let scannerRefreshing = false;
async function refreshOpportunityScan() {
  if (scannerRefreshing || !data || data.mode === "published_snapshot" || currentView() !== "scanner" ||
      document.hidden || $("desk-snapshot")) return;
  scannerRefreshing = true;
  try {
    const response = await fetch("/api/scanner", {cache:"no-store"});
    if (!response.ok) throw new Error("Scanner input unavailable.");
    data.opportunity_scan = await response.json();
    delete data.opportunity_scan_error;
  } catch {
    data.opportunity_scan_error = "Scanner refresh failed. Waiting for a new result.";
  } finally {
    scannerRefreshing = false;
    if (currentView() === "scanner") renderOpportunityScan(data.opportunity_scan, data.opportunity_scan_error);
  }
}

function renderIntake(intake) {
  if (!intake) {
    $("intake").innerHTML = '<article class="panel"><p class="empty">No market intake snapshot for this run.</p></article>';
    return;
  }
  const human = value => value?.replaceAll("_"," ") ?? "—";
  const pct = value => value == null ? "—" : Number(value).toFixed(2) + "%";
  const {regime,news,liquidity} = intake;
  const ageSeconds = (Date.now() - Date.parse(intake.as_of)) / 1000;
  const historical = regime.snapshot_stale || ageSeconds > Number(intake.max_quote_age_seconds);
  const sourceCounts = intake.news_coverage ?? {};
  $("intake").innerHTML = `<div class="metrics">
    ${metric("Regime",human(regime.label),`${historical ? "Historical snapshot" : "Observed snapshot"} · ${stamp(intake.as_of)}`)}
    ${metric("Breadth",`${regime.breadth_up} / ${regime.breadth_expected}`,`${regime.breadth_observed} benchmarks observed`)}
    ${metric("20-session realized volatility",pct(regime.volatility?.realized_20_session_pct),regime.volatility ? `${Math.round(regime.volatility.historical_percentile)}th historical percentile` : "No usable baseline")}
    ${metric("Regular session close",stamp(regime.close_at),human(regime.session))}</div>
    <section class="panel"><div class="panel-head"><div><h2>Market moves</h2><p>Compared with the prior regular-session close</p></div></div>
      <div class="panel-body"><p>${Object.entries(regime.stock_changes_pct).map(([s,p]) => `${escape(s)} ${pct(p)}`).join(" · ") || "No eligible regular-session quotes."}</p></div></section>
    <section class="panel"><div class="panel-head"><div><h2>News and releases</h2><p>${escape(sourceCounts.note || "Source-timestamped articles and releases")}</p></div></div>
      <div class="panel-body">${news.length ? news.map(item => `<p><a class="earnings-source" href="${escape(item.source)}" target="_blank" rel="noopener noreferrer">${escape(item.title)}</a>
        <br><small>${escape(item.provider)} · ${stamp(item.published_at)} · ${escape(human(item.evidence))}${item.tags.length ? " · " + escape(item.tags.join(", ")) : ""}</small></p>`).join("") : '<p class="empty">No eligible news in the captured source window.</p>'}</div></section>
    <section class="panel"><div class="panel-head"><div><h2>Option liquidity</h2><p>Sampled single-leg books at the snapshot time</p></div></div>
      ${liquidity.length ? `<div class="table-wrap"><table><thead><tr><th>Contract</th><th>Bid / ask</th><th>Spread / mid</th><th>Size per side</th><th>Screen result</th></tr></thead><tbody>
      ${liquidity.map(row => `<tr><th scope="row">${escape(row.contract)}</th><td>${dollars(row.bid)} / ${dollars(row.ask)}</td>
      <td>${pct(row.spread_pct_of_mid)}</td><td>${escape(row.available_per_side)}</td><td>${row.reasons.length ? escape(row.reasons.map(human).join(", ")) : "Passes quote screen"}</td></tr>`).join("")}</tbody></table></div>` : '<p class="empty">No sampled option books.</p>'}</section>`;
}

function renderRisk(run, error) {
  if (!run) {
    $("risk").innerHTML = `<article class="panel"><p class="empty">${escape(error || "No completed local desk run.")}</p></article>`;
    return;
  }
  const {journal,synthetic} = run;
  const limit = synthetic.price_limits;
  const review = synthetic.candidate_review;
  const sizing = synthetic.candidate_sizing;
  const expression = synthetic.expression_review;
  const exits = synthetic.exit_replay;
  $("risk").innerHTML = `<div class="metrics">
    ${metric("Journal integrity",journal.integrity,`Run ${stamp(run.generated_at)}`)}
    ${metric("Decisions replayed",whole(journal.decisions_replayed),`${whole(journal.observations)} observations`)}
    ${metric("Research engines",run.engines ? whole(Object.values(run.engines).filter(engine => engine.status === "historical_research").length) : "—","Engines with research evidence")}
    ${metric("Highest tested passing limit",dollars(limit.highest_passing_tested_limit),"Synthetic pricing rehearsal")}</div>
    ${review ? `<section class="panel"><div class="panel-head"><div><h2>Combined decision rehearsal</h2><p>Synthetic candidate and portfolio</p></div>
      ${pill(review.status === "passes_research_gates" ? "Research gates pass" : "Pass on trade", review.reasons.length ? "amber" : "")}</div>
      <div class="panel-body"><p>Tested limit ${dollars(review.selected_limit)} · Combined stress ${dollars(review.worst_combined_stress_loss_dollars)} · Stress ceiling ${dollars(review.portfolio_stress_limit_dollars)}</p>
      ${review.reasons.length ? `<p>${escape(review.reasons.map(reason => reason.replaceAll("_"," ")).join(" · "))}</p>` : ""}</div></section>` : ""}
    ${sizing ? `<section class="panel"><div class="panel-head"><div><h2>Whole-contract sizing</h2>
    <p>Synthetic fills and portfolio shocks. Scaled loss budget ${dollars(sizing.fractional_loss_budget_dollars)}.</p></div>
    ${pill(sizing.selected_contracts ? `${whole(sizing.selected_contracts)} selected` : "Cash")}</div>
    <div class="table-wrap"><table><thead><tr><th>Lots</th><th>Order loss bound</th><th>Combined stress loss</th>
    <th>Worst expected net P&amp;L</th><th>Decision</th></tr></thead><tbody>${sizing.sizes.map(row =>
      `<tr><th scope="row">${whole(row.contracts)}</th><td>${dollars(row.modeled_loss_bound_dollars)}</td>
      <td>${dollars(row.worst_combined_stress_loss_dollars)}</td><td>${dollars(row.worst_expected_net_dollars)}</td>
      <td>${row.reasons.length ? escape(row.reasons.map(reason => reason.replaceAll("_", " ")).join(", ")) :
         row.contracts === sizing.selected_contracts ? "Selected" : row.within_fractional_budget ? "Eligible" : "Above scaled budget"}</td></tr>`).join("")}</tbody></table></div></section>` : ""}
    ${expression ? `<section class="panel"><div class="panel-head"><div><h2>Spread comparison</h2><p>Synthetic pricing at a common exit horizon</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Expression</th><th>Modeled lots</th><th>Worst expected net P&amp;L</th><th>Decision</th></tr></thead><tbody>
      ${expression.expressions.map(row => `<tr><th scope="row">${escape(row.name)}</th><td>${whole(row.selected_contracts)}</td>
      <td>${dollars(row.selected_size?.worst_expected_net_dollars)}</td>
      <td>${row.name === expression.selected_expression ? "Selected" : row.selected_contracts ? "Eligible" : "Cash"}</td></tr>`).join("")}
      </tbody></table></div></section>` : ""}
    ${exits ? `<section class="panel"><div class="panel-head"><div><h2>Exit policy comparison</h2><p>Synthetic package bids after the exit decision</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Policy</th><th>Trigger</th><th>Net P&amp;L</th></tr></thead><tbody>
      ${exits.policies.map(row => `<tr><th scope="row">${row.policy === "scheduled_exit" ? "Scheduled" : "Target or stop"}</th><td>${escape(row.trigger)}</td>
      <td>${row.status === "modeled_exit" ? dollars(row.net_pnl_dollars) : "Unresolved exit"}</td></tr>`).join("")}
      </tbody></table></div></section>` : ""}
    <section class="panel"><div class="panel-head"><div><h2>Limit-price rehearsal</h2><p>Synthetic fill and exit distributions</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Tested limit</th><th>Worst-model expected P&L</th><th>Tail loss after fill</th><th>Decision</th></tr></thead><tbody>
      ${limit.prices.map(row => `<tr><th scope="row">${dollars(row.limit)}</th><td>${dollars(row.worst_model_expected_net_dollars)}</td><td>${dollars(row.maximum_model_tail_loss_given_fill_dollars)}</td>
        <td>${row.reasons.length ? escape(row.reasons.map(reason => reason.replaceAll("_"," ")).join(", ")) : "Passes modeled constraints"}</td></tr>`).join("")}
    </tbody></table></div></section>
    <section class="panel"><div class="panel-head"><div><h2>Greek stress rehearsal</h2><p>Synthetic mark changes before exit costs</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Scenario</th><th>Spot</th><th>Implied volatility</th><th>Time</th><th>Mark change</th></tr></thead><tbody>
      ${Object.entries(synthetic.risk_attribution).map(([name,row]) => `<tr><th scope="row">${escape(name)}</th><td>${dollars(row.spot)}</td>
        <td>${dollars(row.implied_volatility)}</td><td>${dollars(row.time)}</td><td>${dollars(row.mark_change)}</td></tr>`).join("")}
    </tbody></table></div></section>
    ${synthetic.hedge_replay ? `<section class="panel"><div class="panel-head"><div><h2>Hedge policy replay</h2><p>Synthetic quotes and modeled fills</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>Policy</th><th>Shares traded</th><th>Hedge costs</th><th>Sampled drawdown</th><th>Settlement P&amp;L</th></tr></thead><tbody>
      ${synthetic.hedge_replay.map(row => `<tr><th scope="row">${escape(({
        unhedged: "Unhedged", delta_rebalance: "Delta rebalancing", no_trade_band: "No-trade band",
      })[row.policy] || row.policy)}</th><td>${whole(row.shares_traded)}</td><td>${dollars(row.modeled_hedge_friction_dollars)}</td>
        <td>${dollars(row.max_sampled_drawdown_dollars)}</td><td>${row.status === "unpriced_terminal_exit" ? "Unpriced exit" : dollars(row.terminal_pnl_dollars)}</td></tr>`).join("")}
    </tbody></table></div></section>` : ""}`;
}

function render() {
  if (!data) return;
  const view = currentView();
  const [title,description] = titles[view];
  $("title").textContent = title;
  $("description").textContent = description;
  document.querySelectorAll("[data-view]").forEach(link => {
    if (link.dataset.view === view || (link.dataset.view === "catalyst-engine" && ["earnings","catalysts"].includes(view))) {
      link.setAttribute("aria-current", "page");
    }
    else link.removeAttribute("aria-current");
  });
  const decision = data.decisions.find(d => d.decision_id === selected);
  const capture = data.sources.map(source => source.last_received_at).sort().at(-1);
  $("capture-time").textContent = capture ? "Last capture " + stamp(capture) : "No capture";
  const published = data.mode === "published_snapshot";
  document.querySelectorAll("[data-local-route]").forEach(link => { link.hidden = published; });
  const command = $("command");
  command.hidden = published || view !== "overview" || !data.desk_run;
  if (!command.hidden) {
    const run = data.desk_run;
    const phases = run.journal.eligible_prospective_phases;
    command.innerHTML = `<div><h2>${phases ? "Research evidence available" : "Awaiting forward observations"}</h2>
      <p>${whole(phases)} eligible forward phases · Desk run ${stamp(run.generated_at)}</p></div>
      <div class="command-links"><a href="#intake">Market intake</a><a href="#risk">Risk checks</a><a href="#architecture">System map</a></div>`;
  }
  document.querySelectorAll("[data-engine]").forEach(link => { link.hidden = published; });
  $("intake-nav").hidden = published;
  $("scanner-nav").hidden = published;
  $("monitor-nav").hidden = published;
  $("risk-nav").hidden = published;
  const catalystView = view === "catalysts";
  const earningsView = view === "earnings";
  const monitorView = view === "monitor";
  const riskView = view === "risk";
  const engineView = engineViews.includes(view);
  const intakeView = view === "intake";
  const scannerView = view === "scanner";
  const terminalView = view === "workspace";
  const researchView = catalystView || earningsView || monitorView || riskView || engineView || intakeView || scannerView || terminalView;
  $("trading-workspace").hidden = !terminalView;
  if (terminalView) window.RookWorkspace.mount($("trading-workspace"), data.workspace_market);
  $("scanner").hidden = !scannerView;
  $("intake").hidden = !intakeView;
  $("engines").hidden = !engineView;
  $("catalysts").hidden = !catalystView;
  $("earnings").hidden = !earningsView;
  $("monitor").hidden = !monitorView;
  $("risk").hidden = !riskView;
  if (catalystView) renderCatalysts(data.catalyst_studies ?? []);
  if (earningsView) renderEarnings(data.earnings_experiments ?? []);
  if (monitorView) renderResearchMonitor(data.research_monitor);
  if (riskView) renderRisk(data.desk_run, data.desk_run_error);
  if (engineView) {
    renderEngine(view, data.desk_run?.engines);
    const intake = data.desk_run?.intake;
    if (intake) {
      $("engines").insertAdjacentHTML("beforeend", `<article class="panel panel-body"><h2>Shared market intake</h2>
        <p>${escape(intake.regime.label.replaceAll("_"," "))} at ${stamp(intake.as_of)} · <a href="#intake">Open snapshot</a></p></article>`);
    }
  }
  if (intakeView) renderIntake(data.desk_run?.intake);
  if (scannerView) renderOpportunityScan(data.opportunity_scan, data.opportunity_scan_error);
  if (published) $("desk-mode").innerHTML = "Private site<small>Published snapshot</small>";
  $("published-at").textContent = published ? "Snapshot generated " + stamp(data.generated_at) : "Local journal view";
  $("metrics").hidden = view !== "overview";
  $("metrics").innerHTML = [
    metric("Market observations",whole(data.observations),"Journaled quotes and reference data"),
    metric("Frozen decisions",whole(data.decisions.length),"Replayable payoff scenarios"),
    metric("Cited reviews",whole(data.decisions.filter(d => d.review).length),"Analyst evidence attached"),
    metric("Fresh at decision",whole(data.decisions.filter(d => d.analysis.quote_status === "fresh").length),"Recorded quote-age check")
  ].join("");
  $("detail").hidden = researchView || view === "sources" || view === "architecture";
  $("detail").classList.toggle("single",view === "intelligence");
  chartObserver?.disconnect();
  const picker = `<div class="decision-picker">${data.decisions.map(d => `<button class="ticker-button" data-select="${escape(d.decision_id)}" aria-pressed="${selected === d.decision_id}">${symbol(d)}</button>`).join("")}</div>`;
  $("detail").innerHTML = decision ? (view === "intelligence" ? picker + review(decision) : payoff(decision) + review(decision)) : '<p class="empty">No decisions recorded.</p>';
  $("journal").hidden = researchView || view === "sources" || view === "intelligence" || view === "architecture";
  $("sources").hidden = view !== "sources";
  $("architecture").hidden = view !== "architecture";
  $("source-list").innerHTML = data.sources.length ? `<div class="table-wrap"><table><thead><tr><th>Provider</th><th>Feed</th><th>Captures</th><th>Last receipt</th></tr></thead>
    <tbody>${data.sources.map(s => `<tr><td>${escape(s.source)}</td><td>${escape(s.feed)}</td><td class="number">${whole(s.captures)}</td><td>${stamp(s.last_received_at)}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="empty">No market-data captures.</p>';
  renderRows();
  if (decision && (view === "overview" || view === "decisions")) renderChart(decision);
}
async function load() {
  const snapshot = $("desk-snapshot");
  const endpoint = document.documentElement.dataset.deskEndpoint;
  $("refresh").disabled = true;
  $("workspace").setAttribute("aria-busy","true");
  const previous = data;
  const previousSelection = selected;
  try {
    let incoming;
    if (snapshot) {
      $("refresh").hidden = true;
      incoming = JSON.parse(snapshot.textContent);
    } else {
      const response = await fetch(endpoint || "/api/desk",{cache:"no-store"});
      if (!response.ok) throw new Error("Could not read the research data.");
      incoming = await response.json();
    }
    if (!Array.isArray(incoming?.decisions) || !Array.isArray(incoming.sources)
        || (endpoint && (incoming.schema_version !== 1 || incoming.mode !== "published_snapshot"))) {
      throw new Error("Unsupported snapshot.");
    }
    data = incoming;
    if (!data.decisions.some(d => d.decision_id === selected)) selected = data.decisions[0]?.decision_id;
    render();
    $("notice").hidden = true;
  } catch {
    if (data !== previous && previous) {
      data = previous;
      selected = previousSelection;
      render();
    } else {
      data = previous;
    }
    $("notice").textContent = previous ? "Refresh failed. Showing the last loaded snapshot."
      : snapshot ? "The embedded research snapshot could not be read."
      : "The research data could not be loaded. Try refreshing.";
    $("notice").hidden = false;
  } finally {
    $("workspace").setAttribute("aria-busy","false");
    $("refresh").disabled = false;
  }
}
document.addEventListener("click",event => {
  const button = event.target.closest("[data-select]");
  if (!button) return;
  selected = button.dataset.select;
  render();
  Array.from(document.querySelectorAll("[data-select]")).find(item => item.dataset.select === selected && item.offsetParent)?.focus();
});
$("search").addEventListener("input",renderRows);
$("refresh").addEventListener("click",load);
window.addEventListener("hashchange",render);
window.setInterval(refreshOpportunityScan, 5000);
load();
