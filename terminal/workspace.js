"use strict";

window.RookWorkspace = (() => {
  const number = new Intl.NumberFormat("en-US", {maximumFractionDigits: 0});
  const money = new Intl.NumberFormat("en-US", {style:"currency",currency:"USD"});
  const compact = new Intl.NumberFormat("en-US", {notation:"compact",maximumFractionDigits:1});
  const date = new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",timeZone:"UTC"});
  const stamp = new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:"America/New_York",timeZoneName:"short"});
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const time = value => value && Number.isFinite(Date.parse(value)) ? stamp.format(new Date(value)) : "Unavailable";
  const numeric = value => value === null || value === "" || value === undefined ? NaN : Number(value);
  const dollars = value => Number.isFinite(numeric(value)) ? money.format(numeric(value)) : "—";
  const pct = value => Number.isFinite(numeric(value)) ? `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(2)}%` : "—";
  const tone = value => Number(value) < 0 ? "is-down" : Number(value) > 0 ? "is-up" : "";
  const storageKey = "rook.paper-plans.v1";
  const pageSize = 12;
  const state = {symbol:"SPY", sort:"volume", search:"", page:0, range:40, expiry:"", quoteCode:null};
  let root, dataset, chartObserver, selectedLegs, currentPlan, chartHover;

  function paperPlans() {
    try {
      const rows = JSON.parse(localStorage.getItem(storageKey) || "[]");
      return Array.isArray(rows) ? rows.filter(row => row && typeof row.id === "string" &&
        typeof row.symbol === "string" && Number.isFinite(row.loss)) : [];
    } catch { return []; }
  }

  function savePlans(rows) {
    try {
      localStorage.setItem(storageKey, JSON.stringify(rows));
      return true;
    } catch { return false; }
  }

  function economics(values) {
    const {direction, structure, long, short, debit, qty, fees, spot} = values;
    const vertical = structure === "vertical";
    const width = direction === "C" ? short - long : long - short;
    if (![long,debit,fees,spot].every(Number.isFinite) || long <= 0 || debit <= 0 || fees < 0 ||
        !Number.isInteger(qty) || qty < 1 || spot < 0) return {error:"Enter a positive strike and debit, a whole lot count, and nonnegative fees and settlement price."};
    if (vertical && (!Number.isFinite(short) || short <= 0 || width <= 0 || debit + fees / 100 >= width)) {
      return {error:"Set a same-expiry short strike that caps the payoff beyond the long strike. The width must exceed the all-in debit."};
    }
    if (!vertical && direction === "P" && debit + fees / 100 >= long) {
      return {error:"The all-in debit must be below the put strike for a profitable expiration outcome."};
    }
    const intrinsic = x => Math.max(0, direction === "C" ? spot - x : x - spot);
    const loss = (100 * debit + fees) * qty;
    const gross = intrinsic(long) - (vertical ? intrinsic(short) : 0);
    const maxProfit = vertical ? (width * 100 * qty - loss) : direction === "P" ? long * 100 * qty - loss : null;
    const breakeven = long + (direction === "C" ? 1 : -1) * (debit + fees / 100);
    return {loss, maxProfit, breakeven: breakeven >= 0 ? breakeven : null, pnl: gross * 100 * qty - loss};
  }

  function mount(target, market) {
    if (!target) return;
    chartObserver?.disconnect();
    root = target;
    dataset = market ?? {market_rows:[], contracts:[], history:{}};
    state.symbol = dataset.market_rows.some(row => row.symbol === state.symbol) ? state.symbol :
      (dataset.market_rows[0]?.symbol ?? dataset.contracts[0]?.symbol ?? "SPY");
    root.innerHTML = `
      <div id="desk-tape" class="desk-tape" aria-label="Market tape"></div>
      <div class="desk-top-grid">
        <section class="panel desk-chart" aria-label="Underlying chart">
          <header class="desk-panel-head"><div><div class="desk-asset" id="desk-asset"></div><p id="desk-chart-meta"></p></div>
            <div class="desk-toggle" aria-label="Chart sessions">
              ${[20,40,60].map(n => `<button type="button" data-range="${n}" aria-pressed="${n===state.range}">${n}D</button>`).join("")}
            </div></header>
          <div class="desk-chart-canvas" id="desk-chart-area"><canvas id="desk-price-chart" role="img" aria-label="Historical closing prices"></canvas><p id="desk-no-chart" hidden>Closing-price history is unavailable for this symbol.</p></div>
          <footer class="desk-chart-footer"><span><i></i> Daily close</span><span id="desk-chart-readout" aria-live="polite"></span></footer>
        </section>
        <section class="panel desk-ticket" aria-label="Paper spread builder">
          <header class="desk-panel-head"><div><h2>Spread builder</h2><p id="desk-ticket-context">Paper plan</p></div><button type="button" id="desk-saved" class="desk-button desk-quiet">Saved plans</button></header>
          <form id="desk-form">
            <div class="desk-form-grid">
              <label>Structure<select name="structure"><option value="vertical">Debit vertical</option><option value="single">Long option</option></select></label>
              <label>Right<select name="direction"><option value="C">Call</option><option value="P">Put</option></select></label>
              <label>Long strike<input name="long" type="number" min=".01" step="any" value="100"></label>
              <label id="desk-short-label">Short strike<input name="short" type="number" min=".01" step="any" value="105"></label>
              <label>Net debit<input name="debit" type="number" min=".01" step="any" value="1.10"></label>
              <label>Contracts<input name="qty" type="number" min="1" step="1" value="1"></label>
              <label>Round-trip fees / lot<input name="fees" type="number" min="0" step="any" value="2.00"></label>
              <label>Stock at expiration<input name="spot" type="number" min="0" step="any" value="104"></label>
            </div>
            <p id="desk-input-basis" class="desk-meta">Illustrative inputs. Select a captured contract to prefill the ticket.</p>
            <dl id="desk-economics" class="desk-economics" aria-live="polite"></dl>
            <canvas id="desk-payoff" class="desk-payoff" role="img" aria-label="Profit or loss at expiration"></canvas>
            <button type="submit" class="desk-button desk-primary" id="desk-save">Save paper plan</button>
            <p id="desk-plan-status" class="desk-meta" role="status"></p>
          </form>
        </section>
      </div>
      <div class="desk-lower-grid">
        <section class="panel desk-grid-panel">
          <header class="desk-panel-head"><div><h2>Market discovery</h2><p>${number.format(dataset.market_rows.length)} Robinhood symbols · ${esc(time(dataset.discovery_received_at))}</p></div></header>
          <div class="desk-toolbar"><label class="desk-market-search"><span aria-hidden="true">⌕</span><input id="desk-find" type="search" aria-label="Find a ticker" placeholder="Find ticker" value="${esc(state.search)}"><kbd>/</kbd></label>
            <select id="desk-sort" aria-label="Sort market discovery"><option value="volume">Options volume</option><option value="movers">Largest moves</option><option value="symbol">Ticker A–Z</option></select></div>
          <div class="table-wrap" tabindex="0" role="region" aria-label="Market discovery results"><table class="desk-table"><thead><tr><th>Symbol</th><th>Last</th><th>Change</th><th>Options volume</th></tr></thead><tbody id="desk-markets"></tbody></table></div>
          <footer class="desk-pagination"><span id="desk-results"></span><div><button class="desk-button desk-quiet" id="desk-prev" type="button">Previous</button><button class="desk-button desk-quiet" id="desk-next" type="button">Next</button></div></footer>
        </section>
        <section class="panel desk-grid-panel"><header class="desk-panel-head"><div><h2>Captured option quotes</h2><p id="desk-chain-context"></p></div><select id="desk-expiry" aria-label="Filter option expiration"></select></header>
          <div class="table-wrap" tabindex="0" role="region" aria-label="Captured contracts"><table class="desk-table"><thead><tr><th>Contract</th><th>Bid</th><th>Ask</th><th>Quote time</th></tr></thead><tbody id="desk-chain"></tbody></table></div>
          <footer class="desk-chain-note">Recorded quotes for research. Select a contract to model a paper plan.</footer></section>
      </div>
      <dialog id="desk-plans" class="desk-dialog"><header class="desk-panel-head"><div><h2>Saved paper plans</h2><p>Stored in this browser</p></div><form method="dialog"><button type="submit" class="desk-button desk-quiet">Close</button></form></header><div id="desk-plan-list"></div></dialog>`;
    const $ = id => root.querySelector("#" + id);
    $("desk-sort").value = state.sort;
    root.onclick = event => {
      const button = event.target.closest("button");
      if (!button) return;
      if (button.dataset.symbol) {
        selectSymbol(button.dataset.symbol);
        (Array.from(root.querySelectorAll("[data-symbol]")).find(node => node.dataset.symbol === state.symbol) || $("desk-find")).focus();
      }
      else if (button.dataset.range) {
        state.range = Number(button.dataset.range);
        root.querySelectorAll("[data-range]").forEach(node => node.setAttribute("aria-pressed", Number(node.dataset.range) === state.range));
        chartHover = undefined; draw();
      } else if (button.dataset.contract) pickContract(button.dataset.contract);
      else if (button.id === "desk-prev" || button.id === "desk-next") {
        state.page += button.id === "desk-prev" ? -1 : 1; renderMarkets();
        (button.disabled ? $("desk-find") : button).focus();
      } else if (button.id === "desk-saved") {
        renderPlans(); $("desk-plans").showModal();
      } else if (button.dataset.remove) {
        if (savePlans(paperPlans().filter(row => row.id !== button.dataset.remove))) {
          renderPlans(); $("desk-plans").querySelector("button").focus();
        }
      }
    };
    $("desk-find").addEventListener("input", event => {state.search = event.target.value; state.page=0; renderMarkets();});
    $("desk-sort").addEventListener("change", event => {state.sort = event.target.value; state.page=0; renderMarkets();});
    $("desk-expiry").addEventListener("change", event => {state.expiry=event.target.value; renderChain();});
    $("desk-form").addEventListener("input", event => {
      if (["direction","long","short","structure","debit"].includes(event.target.name)) {
        selectedLegs = null;
        $("desk-input-basis").textContent = "Manual payoff inputs.";
      }
      renderTicket();
    });
    $("desk-form").addEventListener("submit", event => {
      event.preventDefault();
      if (!currentPlan || currentPlan.error) return;
      const plans=paperPlans();
      const plan={id:globalThis.crypto?.randomUUID?.() ?? String(Date.now()), symbol:state.symbol,
        created:new Date().toISOString(), ...currentPlan, expiry:selectedLegs?.expiry ?? null};
      $("desk-plan-status").textContent=savePlans([plan,...plans].slice(0,50)) ?
        "Paper plan saved in this browser." : "Browser storage is unavailable.";
    });
    const canvas = $("desk-price-chart");
    canvas.addEventListener("pointermove", event => {
      const bounds = canvas.getBoundingClientRect();
      chartHover = event.clientX - bounds.left; draw();
    });
    canvas.addEventListener("pointerleave", () => {chartHover=undefined; draw();});
    if (typeof ResizeObserver === "function") {
      chartObserver = new ResizeObserver(() => {if (!root.hidden) { draw(); drawPayoff(); }});
      chartObserver.observe($("desk-chart-area"));
    }
    selectSymbol(state.symbol, true);
    $("desk-form").elements.structure.value = "vertical";
    renderTicket();
  }

  function selectSymbol(symbol, initializing=false) {
    state.symbol = symbol; state.quoteCode = null; state.expiry = "";
    selectedLegs = null; chartHover = undefined;
    const select = root.querySelector("#desk-expiry");
    const expiries = [...new Set(dataset.contracts.filter(row => row.symbol===symbol).map(row => row.expiry))].sort();
    select.innerHTML = '<option value="">All expirations</option>' + expiries.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
    select.disabled = !expiries.length;
    const row = dataset.market_rows.find(row => row.symbol === symbol);
    root.querySelector("#desk-asset").innerHTML = `<h2>${esc(symbol)}</h2><strong>${dollars(row?.last)}</strong><span class="${tone(row?.change_pct)}">${pct(row?.change_pct)}</span>`;
    root.querySelector("#desk-ticket-context").textContent = `${symbol} · expiration payoff`;
    root.querySelector("#desk-chain-context").textContent = `${symbol} · ${dataset.option_feed || "Journal snapshots"}`;
    const form=root.querySelector("#desk-form");
    if (Number.isFinite(numeric(row?.last)) && Number(row.last)>0) {
      const step=Number(row.last) >= 20 ? 5 : 1;
      const strike=Math.max(step, Math.floor(Number(row.last)/step)*step);
      form.elements.direction.value="C";
      form.elements.structure.value="vertical";
      form.elements.long.value=strike;
      form.elements.short.value=strike+step;
      form.elements.debit.value="1.10";
      form.elements.spot.value=row.last;
    }
    root.querySelector("#desk-input-basis").textContent = "Illustrative strikes and debit. Select a captured contract to prefill.";
    if (!initializing) root.querySelector("#desk-plan-status").textContent = "";
    renderTape(); renderMarkets(); renderChain(); renderTicket(); draw();
  }

  function renderTape() {
    const preferred=["SPY","QQQ","IWM","NVDA"].map(symbol => dataset.market_rows.find(row => row.symbol === symbol)).filter(Boolean);
    root.querySelector("#desk-tape").innerHTML = preferred.map(row => `<button type="button" data-symbol="${esc(row.symbol)}" class="desk-tape-card" aria-pressed="${row.symbol===state.symbol}"><span>${esc(row.symbol)}</span>
      <strong>${dollars(row.last)}</strong><span class="${tone(row.change_pct)}">${pct(row.change_pct)}</span></button>`).join("");
  }

  function renderMarkets() {
    const sorted=dataset.market_rows.filter(row => row.symbol.toUpperCase().includes(state.search.trim().toUpperCase()));
    if (state.sort === "symbol") sorted.sort((a,b) => a.symbol.localeCompare(b.symbol));
    else if (state.sort === "movers") sorted.sort((a,b) => Math.abs(Number(b.change_pct))-Math.abs(Number(a.change_pct)));
    else sorted.sort((a,b) => Number(b.options_volume)-Number(a.options_volume));
    const maxPage=Math.max(0,Math.ceil(sorted.length/pageSize)-1);
    state.page=Math.min(maxPage,Math.max(0,state.page));
    const start=state.page*pageSize;
    const rows=sorted.slice(start,start+pageSize);
    root.querySelector("#desk-markets").innerHTML=rows.length ? rows.map(row => `<tr class="${row.symbol===state.symbol?"desk-selected":""}"><th scope="row">
      <button type="button" data-symbol="${esc(row.symbol)}" class="desk-text-button" aria-pressed="${row.symbol===state.symbol}">${esc(row.symbol)}</button></th>
      <td>${dollars(row.last)}</td><td class="${tone(row.change_pct)}">${pct(row.change_pct)}</td><td>${compact.format(Number(row.options_volume))}</td></tr>`).join("") :
      '<tr><td colspan="4" class="empty">No symbols match this view.</td></tr>';
    root.querySelector("#desk-results").textContent=rows.length ?
      `${number.format(start+1)}–${number.format(start+rows.length)} of ${number.format(sorted.length)}` : "0 results";
    root.querySelector("#desk-prev").disabled=state.page===0;
    root.querySelector("#desk-next").disabled=state.page===maxPage;
  }

  function renderChain() {
    const rows=dataset.contracts.filter(row => row.symbol === state.symbol && (!state.expiry || state.expiry===row.expiry))
      .sort((a,b) => a.expiry.localeCompare(b.expiry) || a.right.localeCompare(b.right) || a.strike-b.strike);
    root.querySelector("#desk-chain").innerHTML=rows.length ? rows.map(row => `<tr class="${state.quoteCode===row.code?"desk-selected":""}">
      <th scope="row"><button type="button" class="desk-text-button" data-contract="${esc(row.code)}" aria-pressed="${state.quoteCode===row.code}">${esc(row.expiry.slice(5))} · ${dollars(row.strike)} ${row.right==="C"?"C":"P"}</button></th>
      <td>${dollars(row.quote?.bid)}</td><td>${dollars(row.quote?.ask)}</td><td>${esc(time(row.quote?.observed_at))}</td></tr>`).join("") :
      '<tr><td colspan="4" class="empty">No captured contracts for this symbol.</td></tr>';
  }

  function pickContract(code) {
    const row=dataset.contracts.find(item => item.code===code);
    if (!row) return;
    state.quoteCode=code;
    const form=root.querySelector("#desk-form");
    const wing=dataset.contracts.filter(item => item.symbol===row.symbol && item.expiry===row.expiry && item.right===row.right &&
      (row.right==="C" ? Number(item.strike)>Number(row.strike) : Number(item.strike)<Number(row.strike)))
      .sort((a,b) => Math.abs(a.strike-row.strike)-Math.abs(b.strike-row.strike))[0];
    const ask=numeric(row.quote?.ask), bid=numeric(wing?.quote?.bid);
    const paired=wing && ask > 0 && bid > 0 && ask > bid && Math.abs(Date.parse(row.quote.observed_at)-Date.parse(wing.quote.observed_at)) <= 10000;
    form.elements.direction.value=row.right;
    form.elements.long.value=row.strike;
    form.elements.structure.value=paired?"vertical":"single";
    if (paired) form.elements.short.value=wing.strike;
    form.elements.debit.value=ask>0 ? (paired ? ask-bid : ask).toFixed(2) : "";
    const underlying=dataset.market_rows.find(item => item.symbol===row.symbol);
    form.elements.spot.value=underlying?.last ?? row.strike;
    selectedLegs={expiry:row.expiry};
    root.querySelector("#desk-input-basis").textContent=ask>0 ? `${paired?"Synthetic leg debit":"Recorded ask"} · ${time(row.quote.observed_at)}` : "Quote unavailable. Enter a hypothetical debit.";
    renderChain(); renderTicket();
    Array.from(root.querySelectorAll("[data-contract]")).find(node => node.dataset.contract===code)?.focus();
  }

  function renderTicket() {
    const form=root.querySelector("#desk-form");
    const v=form.elements;
    const values={direction:v.direction.value,structure:v.structure.value,...Object.fromEntries(
      ["long","short","debit","qty","fees","spot"].map(key => [key,v[key].valueAsNumber]))};
    const result=economics(values);
    currentPlan={...values,...result};
    root.querySelector("#desk-short-label").hidden=values.structure==="single";
    root.querySelector("#desk-economics").innerHTML=result.error ? `<p class="desk-form-error">${esc(result.error)}</p>` :
      [["Max loss",dollars(result.loss),""],["Max profit",result.maxProfit===null?"Uncapped":dollars(result.maxProfit),""],
       ["Breakeven",dollars(result.breakeven),""],["Scenario P&L",dollars(result.pnl),tone(result.pnl)]]
       .map(([label,value,color]) => `<div><dt>${esc(label)}</dt><dd class="${color}">${esc(value)}</dd></div>`).join("");
    root.querySelector("#desk-save").disabled=Boolean(result.error);
    drawPayoff();
  }

  function drawPayoff() {
    const canvas=root?.querySelector("#desk-payoff"), plan=currentPlan;
    if (!canvas || !plan) return;
    canvas.hidden=Boolean(plan.error);
    if (canvas.hidden || canvas.clientWidth < 40) return;
    const ctx=canvas.getContext("2d");
    if (!ctx) return;
    const width=canvas.clientWidth,height=canvas.clientHeight,dpr=window.devicePixelRatio||1;
    canvas.width=width*dpr;canvas.height=height*dpr;ctx.scale(dpr,dpr);
    const span=Math.max(plan.long*.03, plan.debit*3, plan.structure==="vertical"?Math.abs(plan.short-plan.long):0);
    const low=Math.max(0,Math.min(plan.long,plan.spot,plan.structure==="vertical"?plan.short:plan.long)-span);
    const high=Math.max(plan.long,plan.spot,plan.structure==="vertical"?plan.short:plan.long)+span;
    const samples=Array.from({length:41},(_,i)=>low+(high-low)*i/40);
    const values=samples.map(spot=>economics({...plan,spot}).pnl);
    const floor=Math.min(0,...values),ceiling=Math.max(0,...values);
    const x=i=>12+i*(width-24)/40;
    const y=value=>9+(ceiling-value)*(height-24)/Math.max(ceiling-floor,1);
    ctx.strokeStyle="#5f7389";ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(12,y(0));ctx.lineTo(width-12,y(0));ctx.stroke();ctx.setLineDash([]);
    ctx.strokeStyle="#c7f47a";ctx.lineWidth=1.8;ctx.beginPath();
    values.forEach((value,i)=>i?ctx.lineTo(x(i),y(value)):ctx.moveTo(x(i),y(value)));ctx.stroke();
    ctx.fillStyle="#aab8c9";ctx.font="10px ui-monospace,monospace";
    ctx.fillText(dollars(low),12,height-2);
    const ending=dollars(high);ctx.fillText(ending,width-12-ctx.measureText(ending).width,height-2);
    canvas.setAttribute("aria-label",`Expiration payoff from underlying price ${dollars(low)} to ${dollars(high)}. Scenario profit or loss ${dollars(plan.pnl)}.`);
  }

  function renderPlans() {
    const plans=paperPlans();
    root.querySelector("#desk-plan-list").innerHTML=plans.length ? plans.map(row => `<div class="desk-plan-row">
      <div><strong>${esc(row.symbol)} · ${esc(row.long)}${row.structure==="vertical"?" / "+esc(row.short):""} ${esc(row.direction)}</strong>
      <p>${esc(row.expiry || "Manual expiry")} · ${number.format(row.qty)} contract(s) · Max loss ${dollars(row.loss)} · ${esc(time(row.created))}</p></div>
      <button type="button" class="desk-button desk-quiet" data-remove="${esc(row.id)}">Remove</button></div>`).join("") :
      '<p class="panel-body empty">No paper plans saved.</p>';
  }

  function draw() {
    if (!root || root.hidden) return;
    const rows=(dataset.history[state.symbol] || []).slice(-state.range);
    const canvas=root.querySelector("#desk-price-chart");
    const empty=root.querySelector("#desk-no-chart");
    const w=canvas.clientWidth, h=canvas.clientHeight;
    empty.hidden=rows.length>=2;
    canvas.style.visibility=rows.length>=2?"visible":"hidden";
    root.querySelector("#desk-chart-meta").textContent=rows.length>=2 ?
      `${date.format(new Date(rows[0][0]))} – ${date.format(new Date(rows.at(-1)[0]))} · Recorded daily closes` : "Historical prices";
    if (rows.length<2 || w<40 || h<40) {
      root.querySelector("#desk-chart-readout").textContent="";
      return;
    }
    const ctx=canvas.getContext("2d");
    if (!ctx) return;
    const dpr=window.devicePixelRatio||1;
    canvas.width=w*dpr; canvas.height=h*dpr; ctx.scale(dpr,dpr);
    const pad={l:16,r:64,t:16,b:24};
    const prices=rows.map(row => Number(row[1]));
    const low=Math.min(...prices), high=Math.max(...prices), margin=Math.max((high-low)*.15,high*.002);
    const min=low-margin,max=high+margin;
    const x=i=>pad.l+i*(w-pad.l-pad.r)/(rows.length-1);
    const y=value=>pad.t+(max-value)*(h-pad.t-pad.b)/(max-min);
    ctx.font="11px ui-monospace, monospace"; ctx.textBaseline="middle";
    for(let i=0;i<5;i++) {
      const value=max-(max-min)*i/4, yy=y(value);
      ctx.strokeStyle="#263443";ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();
      ctx.fillStyle="#a1afbd";ctx.fillText(value.toFixed(2),w-pad.r+8,yy);
    }
    for (let j=0;j<5;j++) {
      const i=Math.round((rows.length-1)*j/4), xx=x(i);
      ctx.strokeStyle="#1d2b39";ctx.beginPath();ctx.moveTo(xx,pad.t);ctx.lineTo(xx,h-pad.b);ctx.stroke();
      ctx.fillStyle="#a1afbd";ctx.fillText(date.format(new Date(rows[i][0])),Math.min(xx,w-pad.r-22),h-8);
    }
    const trace=()=>prices.forEach((value,i)=>i?ctx.lineTo(x(i),y(value)):ctx.moveTo(x(i),y(value)));
    ctx.beginPath();trace();ctx.lineTo(x(prices.length-1),h-pad.b);ctx.lineTo(x(0),h-pad.b);ctx.closePath();
    const gradient=ctx.createLinearGradient(0,0,0,h-pad.b);
    gradient.addColorStop(0,"rgba(195,244,112,.23)");gradient.addColorStop(1,"rgba(195,244,112,0)");
    ctx.fillStyle=gradient;ctx.fill();
    ctx.strokeStyle="#c7f47a";ctx.lineWidth=2;ctx.beginPath();trace();ctx.stroke();
    const selected=chartHover===undefined?prices.length-1:Math.max(0,Math.min(prices.length-1,
      Math.round((chartHover-pad.l)*(prices.length-1)/(w-pad.l-pad.r))));
    if (chartHover!==undefined) {
      ctx.strokeStyle="#91a0af";ctx.lineWidth=1;ctx.setLineDash([4,4]);ctx.beginPath();
      ctx.moveTo(x(selected),pad.t);ctx.lineTo(x(selected),h-pad.b);ctx.stroke();ctx.setLineDash([]);
    }
    ctx.fillStyle="#d5ff95";ctx.beginPath();ctx.arc(x(selected),y(prices[selected]),3,0,Math.PI*2);ctx.fill();
    const label=`${date.format(new Date(rows[selected][0]))} · ${dollars(prices[selected])}`;
    canvas.setAttribute("aria-label",`${state.symbol} closing prices, ${rows.length} sessions, last close ${dollars(prices.at(-1))}`);
    root.querySelector("#desk-chart-readout").textContent=label;
  }

  document.addEventListener("keydown", event => {
    if (root && !root.hidden && event.key === "/" && !event.metaKey && !event.ctrlKey &&
        !event.target.matches("input,select,textarea,[contenteditable]") && !root.querySelector("dialog[open]")) {
      event.preventDefault();
      root.querySelector("#desk-find").focus();
    }
  });
  return {mount,economics};
})();
