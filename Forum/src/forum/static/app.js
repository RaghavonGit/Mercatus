"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls) => { const n = document.createElement(tag); if (cls) n.className = cls; return n; };

const state = {
  running: false,
  paymentId: null,
  paymentTimer: null,
  ledgerTimer: null,
  cursors: { mercator: 0, emptor: 0 },
  seenEvents: new Set(),
};

/* ---------------- system health ---------------- */

async function refreshHealth() {
  let data;
  try {
    data = await (await fetch("/api/system/health")).json();
  } catch { return; }
  setPill("pill-mercator", "Mercator", data.mercator.reachable);
  setPill("pill-ollama", "Ollama", data.ollama.reachable);
  $("#btn-start").hidden = data.mercator.reachable;
  const shopReady = data.mercator.reachable && data.ollama.reachable;
  $("#btn-shop").disabled = state.running || !shopReady;
}

function setPill(id, label, up) {
  const p = $("#" + id);
  p.dataset.state = up ? "up" : "down";
  p.innerHTML = "";
  p.append(label + " ", Object.assign(el("b"), { textContent: up ? "up" : "down" }));
}

$("#btn-start").addEventListener("click", async () => {
  $("#btn-start").textContent = "starting…";
  $("#btn-start").disabled = true;
  try { await fetch("/api/system/start", { method: "POST" }); } catch {}
  $("#btn-start").textContent = "Start Mercator";
  $("#btn-start").disabled = false;
  refreshHealth();
});

/* ---------------- manual pick ---------------- */

$("#manual").addEventListener("change", async (e) => {
  $("#manual-box").hidden = !e.target.checked;
  const sel = $("#manual-product");
  if (e.target.checked && !sel.options.length) {
    try {
      const { products } = await (await fetch("/api/catalog")).json();
      for (const p of products) {
        const o = el("option");
        o.value = p.id;
        o.textContent = `${p.name} — INR ${p.price_inr}${p.in_stock ? "" : " (out of stock)"}`;
        sel.append(o);
      }
    } catch {
      sel.append(Object.assign(el("option"), { textContent: "catalog unavailable", value: "" }));
    }
  }
});

/* ---------------- run pipeline (SSE over fetch) ---------------- */

$("#btn-shop").addEventListener("click", runPipeline);

async function runPipeline() {
  if (state.running) return;
  resetRun();
  state.running = true;
  $("#btn-shop").disabled = true;

  const body = { goal: $("#goal").value, budget: Number($("#budget").value) };
  if ($("#manual").checked) {
    const pid = $("#manual-product").value;
    if (pid) body.picks_override = [{ product_id: pid, quantity: Number($("#manual-qty").value) || 1 }];
  }

  startLedgerPolling();

  let resp;
  try {
    resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    finishRun();
    return markBlocked("connect", String(err));
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data: "));
      if (line) handleEvent(JSON.parse(line.slice(6)));
    }
  }
  finishRun();
}

function handleEvent(ev) {
  switch (ev.stage) {
    case "connected":
      activate("connect");
      detail("connect", ev.endpoint);
      done("connect");
      break;
    case "catalog":
      activate("catalog");
      detail("catalog", `${ev.total_count} products · ${ev.affordable_count} within budget`);
      done("catalog");
      break;
    case "deciding":
      activate("decision");
      detail("decision", `asking ${ev.model}…`);
      break;
    case "decision": {
      activate("decision");
      const names = ev.picks.map((p) => `${p.quantity}× ${p.name ?? p.product_id} (INR ${p.price_inr ?? "?"})`);
      detail("decision", ev.source === "manual-override" ? "chosen by the operator" : names.join(", "));
      const q = $('.timeline li[data-step="decision"] .reason');
      if (ev.reasoning) { q.hidden = false; q.textContent = ev.reasoning; }
      done("decision");
      break;
    }
    case "validation": {
      activate("validation");
      const ul = $('.timeline li[data-step="validation"] .checks');
      ul.innerHTML = "";
      for (const c of ev.checks) {
        const li = el("li", c.ok ? "ok" : "no");
        li.textContent = c.name;
        ul.append(li);
      }
      if (ev.ok) done("validation"); else markBlocked("validation", ev.reason);
      break;
    }
    case "pending":
      activate("pending");
      detail("pending", `INR ${ev.amount} · expires in ~${ev.expire_hours ?? "?"}h`);
      done("pending");
      showPayment(ev);
      break;
    case "settled": {
      const h = $('.timeline li[data-step="pending"] h4');
      if (h) h.textContent = "Settled by autopay";
      activate("pending");
      detail("pending", `INR ${ev.amount} · drawn from the prepaid envelope — no link to pay`);
      done("pending");
      showSettled(ev);
      break;
    }
    case "blocked":
      markBlocked(mapStage(ev.at), ev.reason);
      break;
    case "done":
      break;
  }
}

function mapStage(at) {
  return ({ discover: "catalog", filter: "catalog", decide: "decision",
            validate: "validation", purchase: "pending" }[at]) || "connect";
}

/* ---------------- timeline helpers ---------------- */

function step(name) { return $(`.timeline li[data-step="${name}"]`); }
function activate(name) { step(name)?.classList.add("active"); }
function done(name) { const s = step(name); if (s) { s.classList.add("active", "done"); s.classList.remove("blocked"); } }
function detail(name, text) { const d = step(name)?.querySelector(".detail"); if (d) d.textContent = text; }
function markBlocked(name, reason) {
  const s = step(name);
  if (s) { s.classList.add("active", "blocked"); s.classList.remove("done"); }
  detail(name, "BLOCKED: " + reason);
}

function resetRun() {
  for (const li of document.querySelectorAll(".timeline li")) {
    li.classList.remove("active", "done", "blocked");
    const d = li.querySelector(".detail"); if (d) d.textContent = "";
    const c = li.querySelector(".checks"); if (c) c.innerHTML = "";
    const r = li.querySelector(".reason"); if (r) { r.hidden = true; r.textContent = ""; }
  }
  $("#payment-card").hidden = true;
  $("#settled-card").hidden = true;
  const h = $('.timeline li[data-step="pending"] h4');
  if (h) h.textContent = "Payment link created";
  state.paymentId = null;
  stopPaymentPolling();
}

function finishRun() {
  state.running = false;
  refreshHealth();
}

/* ---------------- payment ---------------- */

function showPayment(ev) {
  const card = $("#payment-card");
  card.hidden = false;
  $("#pay-amount").textContent = "INR " + ev.amount;
  $("#pay-link").href = ev.payment_link_url;
  $("#pay-meta").textContent = `link ${ev.payment_link_id}  ·  ${ev.payment_link_url}`;
  setBadge("pending");
  state.paymentId = ev.payment_link_id;
  startPaymentPolling();
}

function setBadge(status) {
  const b = $("#pay-badge");
  b.dataset.status = status;
  b.textContent = status.toUpperCase();
}

function showSettled(ev) {
  $("#settled-card").hidden = false;
  $("#settled-amount").textContent = "INR " + ev.amount;
  $("#settled-via").textContent = ev.settled_via;
  refreshSpend();
}

$("#pay-refresh").addEventListener("click", pollPaymentOnce);

function startPaymentPolling() {
  stopPaymentPolling();
  state.paymentTimer = setInterval(pollPaymentOnce, 3000);
  pollPaymentOnce();
}
function stopPaymentPolling() {
  if (state.paymentTimer) clearInterval(state.paymentTimer);
  state.paymentTimer = null;
}

async function pollPaymentOnce() {
  if (!state.paymentId) return;
  let r;
  try { r = await (await fetch("/api/payment/" + encodeURIComponent(state.paymentId))).json(); }
  catch { return; }
  const status = r.status || "unknown";
  if (["paid", "cancelled", "expired", "partially_paid"].includes(status)) {
    setBadge(status === "partially_paid" ? "cancelled" : status);
    stopPaymentPolling();
    refreshSpend();
  } else {
    setBadge("pending");
  }
}

/* ---------------- spend ---------------- */

async function refreshSpend() {
  let s;
  try { s = await (await fetch("/api/spend")).json(); } catch { return; }
  $("#spend-window").textContent = s.window_hours;
  $("#spend-figure").textContent = "INR " + s.paid_inr_24h;
  const cap = s.cumulative_cap_inr;
  const fill = $("#spend-fill");
  if (cap) {
    fill.style.width = Math.min(100, (s.paid_inr_24h / cap) * 100) + "%";
    $("#spend-sub").textContent = `cumulative cap INR ${cap} (rolling ${s.window_hours}h). per-transaction cap INR ${s.per_txn_cap_inr}.`;
  } else {
    fill.style.width = s.paid_inr_24h > 0 ? "12%" : "0";
    $("#spend-sub").textContent = `no cumulative cap set (test mode). per-transaction cap INR ${s.per_txn_cap_inr ?? "?"}.`;
  }
}

/* ---------------- ledger ---------------- */

function startLedgerPolling() {
  if (state.ledgerTimer) return;
  state.ledgerTimer = setInterval(pollLedger, 2000);
  pollLedger();
}

async function pollLedger() {
  let data;
  try {
    data = await (await fetch(`/api/ledger?after_mercator=${state.cursors.mercator}&after_emptor=${state.cursors.emptor}`)).json();
  } catch { return; }

  setChain("chain-emptor", data.chains.emptor);
  setChain("chain-mercator", data.chains.mercator);
  state.cursors = data.cursors;

  const list = $("#events");
  for (const ev of data.events) {
    const key = ev.actor + ":" + ev.seq;
    if (state.seenEvents.has(key)) continue;
    state.seenEvents.add(key);
    list.append(renderEvent(ev));
    $("#ledger-empty").hidden = true;
  }
  list.scrollTop = list.scrollHeight;
}

function setChain(id, info) {
  const c = $("#" + id);
  c.dataset.valid = info.valid === null || info.valid === undefined ? "unknown" : String(info.valid);
  c.textContent = `${id === "chain-emptor" ? "emptor" : "mercator"} chain · ${info.entries || 0}`;
}

function renderEvent(ev) {
  const li = el("li");
  const top = el("div", "ev-top");
  const actor = el("span", "ev-actor " + ev.actor);
  actor.textContent = ev.actor;
  const type = el("span", "ev-type");
  type.textContent = ev.event_type;
  const seq = el("span", "ev-seq");
  seq.textContent = "#" + ev.seq + " · " + ev.entry_hash;
  top.append(actor, type, seq);

  const data = el("p", "ev-data");
  data.textContent = summarize(ev.event_type, ev.data);

  li.append(top, data);
  return li;
}

function summarize(type, d) {
  try {
    if (type === "goal_received") return `"${d.goal}"  budget INR ${d.budget_inr}`;
    if (type === "catalog_retrieved") return `${d.catalog_size} products, ${d.affordable_count} affordable`;
    if (type === "llm_decision") return `[${d.source}] ${JSON.stringify(d.picks)}  — ${d.reasoning || "(no reasoning)"}`;
    if (type === "validation_result") return `ok=${d.ok}  total INR ${d.total_inr}` + (d.reason ? `  reason=${d.reason}` : "");
    if (type === "checkout_result") return d.status ? `status=${d.status}` : `ok=${d.ok}` + (d.reason ? ` reason=${d.reason}` : "");
    if (type === "guardrail_check") return `${d.check}: ${d.passed ? "pass" : "FAIL"}` + (d.reason ? ` (${d.reason})` : "");
  } catch {}
  return JSON.stringify(d);
}

/* ---------------- boot ---------------- */

refreshHealth();
refreshSpend();
startLedgerPolling();
setInterval(refreshHealth, 4000);
