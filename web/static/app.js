"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const inr = (n) => "₹" + Math.round(Number(n) || 0).toLocaleString("en-IN");

let CONFIG = { razorpay_enabled: false, scenarios: [] };
let RETRY_N = 0;
let SESSION_SAVED = 0;
let RUNNING = false;

const CAT_STYLE = {
  hard_decline: "bg-rose-500/15 text-rose-300 border border-rose-500/30",
  soft_recoverable: "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30",
  needs_reauth: "bg-amber-500/15 text-amber-300 border border-amber-500/30",
  needs_customer_action: "bg-sky-500/15 text-sky-300 border border-sky-500/30",
  needs_review: "bg-violet-500/15 text-violet-300 border border-violet-500/30",
};
const OUTCOME_STYLE = {
  recovered: "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30",
  still_failed: "bg-slate-500/15 text-slate-300 border border-slate-500/30",
  escalated: "bg-amber-500/15 text-amber-300 border border-amber-500/30",
  no_action_taken: "bg-rose-500/15 text-rose-300 border border-rose-500/30",
};

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

async function boot() {
  try {
    CONFIG = await (await fetch("/api/config")).json();
  } catch (e) {
    CONFIG = { razorpay_enabled: false, classifier_backend: "unknown", scenarios: [] };
  }

  $("#backend-badge").textContent = "backend: " + (CONFIG.classifier_backend || "?");
  $("#classify-backend").textContent = CONFIG.classifier_backend || "?";
  $("#rzp-badge").textContent = CONFIG.razorpay_enabled
    ? "checkout: Razorpay test mode" : "checkout: mock (no keys)";

  const sel = $("#scenario");
  sel.innerHTML = "";
  CONFIG.scenarios.forEach((s) => {
    const o = document.createElement("option");
    o.value = s.id; o.textContent = s.label; sel.appendChild(o);
  });
  updateBlurb();
  sel.addEventListener("change", updateBlurb);

  $$(".retrybtn").forEach((b) => b.addEventListener("click", () => {
    RETRY_N = Number(b.dataset.n);
    $$(".retrybtn").forEach((x) => x.classList.remove("border-brand", "text-white"));
    b.classList.add("border-brand", "text-white");
  }));
  $$(".retrybtn")[0].click();

  $("#pay").addEventListener("click", onPay);

  if (CONFIG.razorpay_enabled) {
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    document.head.appendChild(s);
    $("#pay-note").textContent = "Opens Razorpay test checkout — choose “Failure” to trigger the agent.";
  } else {
    $("#pay-note").textContent = "Mock checkout — the payment attempt is simulated locally.";
  }

  $$(".tabbtn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
}

function updateBlurb() {
  const s = CONFIG.scenarios.find((x) => x.id === $("#scenario").value);
  $("#scenario-blurb").textContent = s ? s.blurb : "";
}

function switchTab(tab) {
  $$(".tabbtn").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  ["demo", "batch", "arch"].forEach((t) =>
    $("#tab-" + t).classList.toggle("hidden", t !== tab));
  if (tab === "batch") loadBatch();
}

// ---------------------------------------------------------------------------
// payment
// ---------------------------------------------------------------------------

async function onPay() {
  if (RUNNING) return;
  const amount = Number($("#amount").value) || 0;
  const scenario = $("#scenario").value;
  if (amount <= 0) return;

  let order;
  try {
    order = await postJSON("/api/create-order", { amount, scenario });
  } catch (e) { order = { provider: "mock" }; }

  if (order.provider === "razorpay" && window.Razorpay) {
    openRazorpay(order, scenario, amount);
  } else {
    await runRecover({ scenario, amount, retry_count_so_far: RETRY_N, razorpay_error: null });
  }
}

function openRazorpay(order, scenario, amount) {
  const rzp = new window.Razorpay({
    key: order.key_id,
    order_id: order.order_id,
    amount: order.amount,
    currency: order.currency || "INR",
    name: "PropelFit",
    description: "Annual Membership",
    theme: { color: "#7c9082" },
    handler: function () {
      $("#pay-note").textContent =
        "Payment succeeded — to see the agent, reopen and choose “Failure” (or a failing test card).";
    },
  });
  rzp.on("payment.failed", function (resp) {
    const err = resp && resp.error ? resp.error : {};
    runRecover({
      scenario, amount, retry_count_so_far: RETRY_N,
      razorpay_error: {
        code: err.code, description: err.description,
        reason: err.reason, source: err.source, step: err.step,
      },
    });
  });
  rzp.open();
}

// ---------------------------------------------------------------------------
// the pipeline animation
// ---------------------------------------------------------------------------

function setStage(id, state) { $(id).setAttribute("data-state", state); }
function resetPipeline() {
  ["#s-failure", "#s-classify", "#s-policy", "#s-exec", "#s-audit"].forEach((s) => setStage(s, "idle"));
  $("#failure-body").innerHTML = "";
  $("#classify-reasoning").textContent = "";
  $("#classify-conf").textContent = "";
  $("#category-badge").className = "ml-auto text-xs px-2 py-0.5 rounded-md hidden";
  $("#policy-checks").innerHTML = "";
  $("#decision-body").innerHTML = "";
  $("#attempt-dots").innerHTML = "";
  $("#outcome-badge").className = "ml-auto text-xs px-2 py-0.5 rounded-md hidden";
  $("#audit-body").innerHTML = "";
  $("#comparison").classList.add("hidden");
}

async function typeInto(elSel, text, speed = 12) {
  const el = $(elSel);
  el.textContent = "";
  el.classList.add("type-cursor");
  for (let i = 0; i < text.length; i++) {
    el.textContent += text[i];
    if (i % 2 === 0) await sleep(speed);
  }
  el.classList.remove("type-cursor");
}

async function runRecover(payload) {
  RUNNING = true;
  $("#pay").disabled = true;
  $("#empty-state").classList.add("hidden");
  $("#pipeline").classList.remove("hidden");
  resetPipeline();

  let data;
  try {
    data = await postJSON("/api/recover", payload);
  } catch (e) {
    $("#audit-body").textContent = "Error: " + e.message;
    RUNNING = false; $("#pay").disabled = false;
    return;
  }

  const ev = data.event;
  const rz = data.razorpay_error;

  // 1 — failure captured
  setStage("#s-failure", "active");
  const rows = [
    ["failure_reason", ev.failure_reason],
    ["payment_method", ev.payment_method],
    ["transaction_type", ev.transaction_type],
    ["amount", inr(ev.amount)],
    ["retries so far", ev.retry_count_so_far],
    ["customer history",
      `${ev.customer_history.prior_successful_payments} ok / ${ev.customer_history.prior_failures} fail · ${ev.customer_history.account_age_days}d old`],
  ];
  if (rz && rz.description) rows.push(["razorpay error", `${rz.description} (${rz.reason || rz.code || "?"})`]);
  $("#failure-body").innerHTML = rows.map(([k, v]) =>
    `<div><span class="text-slate-500">${k}:</span> <span class="text-slate-200">${v}</span></div>`).join("");
  await sleep(700);
  setStage("#s-failure", "done");

  // 2 — classifier
  setStage("#s-classify", "active");
  await sleep(250);
  await typeInto("#classify-reasoning", data.classification.reasoning || "(no reasoning)");
  const cb = $("#category-badge");
  cb.textContent = data.classification.category;
  cb.className = "ml-auto text-xs px-2 py-0.5 rounded-md " + (CAT_STYLE[data.classification.category] || "");
  let confLine =
    `source: ${data.classification.source} · confidence ${data.classification.confidence}`;
  if (data.ideal) {
    confLine += data.ideal.classifier_correct
      ? `  ·  ✓ matches ideal (${data.ideal.category})`
      : `  ·  ✗ ideal was ${data.ideal.category}`;
  }
  $("#classify-conf").textContent = confLine;
  await sleep(350);
  setStage("#s-classify", "done");

  // 3 — policy engine
  setStage("#s-policy", "active");
  for (const c of data.guardrail_trace) {
    const icon = c.status === "block" ? "⛔" : c.status === "info" ? "→" : "✓";
    const color = c.status === "block" ? "text-rose-300"
      : c.status === "info" ? "text-slate-400" : "text-emerald-300";
    const row = document.createElement("div");
    row.className = "fadein flex gap-2";
    row.innerHTML =
      `<span class="${color}">${icon}</span>
       <span class="text-slate-300"><span class="text-slate-200 font-medium">${c.label}</span>
       — ${c.detail}</span>`;
    $("#policy-checks").appendChild(row);
    await sleep(520);
  }
  await sleep(150);
  setStage("#s-policy", "done");

  // 4 — executor
  setStage("#s-exec", "active");
  const d = data.decision, ar = data.agent_run;
  let decHtml = `<div><span class="text-slate-500">action:</span>
    <span class="text-white font-medium">${d.action}</span></div>
    <div class="text-slate-400 mt-0.5">${d.rationale}</div>`;
  if (d.action === "smart_retry" && d.scheduled_retry_at) {
    decHtml += `<div class="text-slate-400 mt-0.5">scheduled: ${d.scheduled_retry_at}
      (${d.retry_delay_hours}h out)</div>`;
  }
  $("#decision-body").innerHTML = decHtml;

  const nAttempts = Math.max(ar.total_attempts, ar.final_outcome === "no_action_taken" ? 0 : 1);
  for (let i = 0; i < ar.total_attempts; i++) {
    const dot = document.createElement("span");
    const recovered = ar.final_outcome === "recovered" && i === ar.total_attempts - 1;
    dot.className = "w-3 h-3 rounded-full fadein " + (recovered ? "bg-emerald-400" : "bg-slate-600");
    dot.title = "attempt " + (i + 1);
    $("#attempt-dots").appendChild(dot);
    await sleep(360);
  }
  const ob = $("#outcome-badge");
  ob.textContent = ar.final_outcome;
  ob.className = "ml-auto text-xs px-2 py-0.5 rounded-md " + (OUTCOME_STYLE[ar.final_outcome] || "");
  await sleep(300);
  setStage("#s-exec", "done");

  // 5 — audit
  setStage("#s-audit", "active");
  $("#audit-body").innerHTML =
    `Logged <span class="text-slate-200">${ar.total_attempts + (["no_action_taken"].includes(ar.final_outcome) ? 1 : 0)}</span>
     decision line(s) for <span class="text-slate-200">${ev.transaction_id}</span> —
     category, reasoning, rules fired, outcome, timestamp.`;
  await sleep(400);
  setStage("#s-audit", "done");

  renderComparison(data);
  appendLedger(data);

  RUNNING = false;
  $("#pay").disabled = false;
}

function renderComparison(data) {
  const c = data.comparison, n = data.naive_run, ar = data.agent_run;
  $("#comparison").classList.remove("hidden");

  $("#naive-body").innerHTML = [
    `${n.attempts} blind retries, no cooldown`,
    `outcome: <span class="font-medium">${n.recovered ? "recovered" : "still failed"}</span>`,
    `wasted retries: <span class="text-rose-300 font-medium">${n.wasted_retries}</span> (${inr(n.wasted_cost_inr)})`,
    c.extra_issuer_declines_avoided
      ? `<span class="text-rose-300">+${c.extra_issuer_declines_avoided} extra declines pushed at the issuer</span>` : "",
    `hard-decline protection: <span class="text-rose-300">none</span>`,
  ].filter(Boolean).map((x) => `<div>${x}</div>`).join("");

  $("#agent-body").innerHTML = [
    `${ar.total_attempts} attempt(s), ${data.decision.action}`,
    `outcome: <span class="font-medium">${ar.final_outcome}</span>`,
    `wasted retries: <span class="text-emerald-300 font-medium">${c.agent_wasted_retries}</span>`,
    data.decision.scheduled_retry_at ? `timed for: ${data.decision.scheduled_retry_at}` : "",
    `guardrails: <span class="text-emerald-300">enforced</span>`,
  ].filter(Boolean).map((x) => `<div>${x}</div>`).join("");

  const s = c.savings_inr;
  $("#savings-headline").textContent =
    s > 0 ? `${inr(s)} saved on this transaction` : "Same spend — agent adds timing + cooldown discipline";
  $("#savings-narrative").textContent = c.narrative;
}

function appendLedger(data) {
  $("#ledger-wrap").classList.remove("hidden");
  const c = data.comparison, ev = data.event, ar = data.agent_run;
  SESSION_SAVED += Math.max(0, c.savings_inr);
  $("#ledger-total").textContent = inr(SESSION_SAVED);
  const tr = document.createElement("tr");
  tr.className = "fadein border-b border-line/40";
  tr.innerHTML = `
    <td class="p-2 text-slate-400">${ev.transaction_id}</td>
    <td class="p-2">${ev.failure_reason}</td>
    <td class="p-2">${data.classification.category}</td>
    <td class="p-2">${data.decision.action}</td>
    <td class="p-2">${ar.final_outcome}</td>
    <td class="p-2">${c.naive_recovered ? "recovered" : "failed"}</td>
    <td class="p-2 text-right text-emerald-300">${inr(Math.max(0, c.savings_inr))}</td>`;
  $("#ledger-body").prepend(tr);
}

// ---------------------------------------------------------------------------
// batch tab
// ---------------------------------------------------------------------------

let BATCH_LOADED = false;
let AUDIT_ROWS = [];

async function loadBatch() {
  if (BATCH_LOADED) return;
  BATCH_LOADED = true;
  try {
    const summary = await (await fetch("/api/batch")).json();
    AUDIT_ROWS = await (await fetch("/api/batch/audit?limit=1000")).json();
    renderBatch(summary);
    $("#batch-loading").classList.add("hidden");
    $("#batch-content").classList.remove("hidden");
  } catch (e) {
    $("#batch-loading").classList.add("hidden");
    const box = $("#batch-error");
    box.classList.remove("hidden");
    box.textContent = "No batch results yet. Run `python main.py` from the project root, then reload.";
  }
}

function tile(label, value, sub) {
  return `<div class="rounded-xl border border-line bg-panel p-4">
    <div class="text-xs uppercase tracking-wide text-slate-500">${label}</div>
    <div class="text-2xl font-bold text-white mt-1">${value}</div>
    <div class="text-xs text-slate-400 mt-1">${sub || ""}</div></div>`;
}

function bars(container, entries, fmt) {
  const max = Math.max(...entries.map((e) => e.value), 1);
  $(container).innerHTML = entries.map((e) => `
    <div>
      <div class="flex justify-between text-slate-300 mb-0.5">
        <span>${e.label}</span><span class="text-slate-400">${fmt(e)}</span>
      </div>
      <div class="bar-track h-2"><div class="bar-fill" style="width:${(e.value / max) * 100}%"></div></div>
    </div>`).join("");
}

function renderBatch(s) {
  const h = s.headline, r = s.decision_to_retry, fp = s.false_positive_cost, hd = s.hard_decline_restraint;
  const cq = s.classifier_quality;
  $("#batch-tiles").innerHTML = [
    tile("Recovery rate", (h.recovery_rate * 100).toFixed(1) + "%", `${h.recovered_count} / ${h.total_count} payments`),
    tile("Amount recovered", inr(h.amount_recovered_inr), `of ${inr(h.amount_at_risk_inr)} at risk`),
    tile("Retry precision / recall", (r.precision * 100).toFixed(0) + "% / " + (r.recall * 100).toFixed(0) + "%",
      `${r.false_positive} false positives`),
    tile("False-positive cost", inr(fp.estimated_cost_inr), `${fp.wasted_retry_attempts} wasted retries · ${hd.correctly_left_alone}/${hd.ground_truth_leave_alone} hard declines left alone`),
  ].join("");

  if (cq) {
    const wrap = document.createElement("div");
    wrap.className = "rounded-xl border border-line bg-panel p-4";
    wrap.innerHTML = `
      <div class="font-semibold text-white mb-1">Classifier vs. flat <code>failure_reason</code> lookup</div>
      <div class="text-xs text-slate-400 mb-3">${cq.twist_records} of ${h.total_count} records are context-dependent cases where the raw failure code is misleading.</div>
      <div class="grid sm:grid-cols-3 gap-3 text-sm">
        <div><div class="text-slate-500 text-xs">overall category accuracy</div>
          <div class="text-lg text-white">${(cq.category_accuracy_llm*100).toFixed(0)}%
          <span class="text-slate-500 text-sm">vs ${(cq.category_accuracy_heuristic*100).toFixed(0)}% flat</span></div></div>
        <div><div class="text-slate-500 text-xs">on the ${cq.twist_records} context cases</div>
          <div class="text-lg text-violet-300">${(cq.twist_accuracy_llm*100).toFixed(0)}%
          <span class="text-slate-500 text-sm">vs ${(cq.twist_accuracy_heuristic*100).toFixed(0)}% flat</span></div></div>
        <div><div class="text-slate-500 text-xs">LLM / heuristic disagreements</div>
          <div class="text-lg text-white">${cq.llm_heuristic_disagreements}</div></div>
      </div>`;
    $("#batch-tiles").after(wrap);
  }

  bars("#batch-reason-bars",
    Object.entries(s.recovery_by_failure_reason).map(([k, v]) => ({
      label: k, value: v.recovery_rate * 100, recovered: v.recovered, total: v.total,
    })).sort((a, b) => b.value - a.value),
    (e) => `${e.recovered}/${e.total} · ${e.value.toFixed(0)}%`);

  bars("#batch-outcome-bars",
    Object.entries(s.outcomes).map(([k, v]) => ({ label: k, value: v })).sort((a, b) => b.value - a.value),
    (e) => e.value);

  $("#batch-guardrails").innerHTML = Object.entries(s.guardrail_activity)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<div class="flex justify-between"><code class="text-slate-400">${k}</code><span>${v}</span></div>`).join("");

  const cats = [...new Set(AUDIT_ROWS.map((r) => r.category))].sort();
  const outs = [...new Set(AUDIT_ROWS.map((r) => r.outcome))].sort();
  $("#audit-cat").innerHTML = '<option value="">all categories</option>' + cats.map((c) => `<option>${c}</option>`).join("");
  $("#audit-outcome").innerHTML = '<option value="">all outcomes</option>' + outs.map((c) => `<option>${c}</option>`).join("");
  ["#audit-search", "#audit-cat", "#audit-outcome"].forEach((sel) =>
    $(sel).addEventListener("input", renderAudit));
  renderAudit();
}

function renderAudit() {
  const q = $("#audit-search").value.toLowerCase();
  const cat = $("#audit-cat").value, out = $("#audit-outcome").value;
  const rows = AUDIT_ROWS.filter((r) =>
    (!q || r.transaction_id.toLowerCase().includes(q) || (r.reasoning || "").toLowerCase().includes(q)) &&
    (!cat || r.category === cat) && (!out || r.outcome === out));
  $("#audit-count").textContent = `${rows.length} decisions`;
  $("#audit-body").innerHTML = rows.slice(0, 400).map((r) => `
    <tr class="border-b border-line/30">
      <td class="p-2 text-slate-400">${r.transaction_id}</td>
      <td class="p-2">${r.failure_reason}</td>
      <td class="p-2">${r.attempt_number}</td>
      <td class="p-2">${r.category}</td>
      <td class="p-2">${r.action_taken}</td>
      <td class="p-2">${r.outcome}</td>
      <td class="p-2 text-slate-400 max-w-[420px] truncate" title="${(r.reasoning || "").replace(/"/g, "&quot;")}">${r.reasoning || ""}</td>
    </tr>`).join("");
}

// ---------------------------------------------------------------------------

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.status + "";
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

boot();