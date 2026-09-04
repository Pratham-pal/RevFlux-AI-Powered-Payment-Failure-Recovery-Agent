"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const inr = (n) => "₹" + Math.round(Number(n) || 0).toLocaleString("en-IN");

let CONFIG = { razorpay_enabled: false, scenarios: [] };
let RETRY_N = 0;
let SESSION_SAVED = 0;
let RUNNING = false;
let FALLBACK_DETECTED = false;

function checkFallback(data) {
  if (FALLBACK_DETECTED) return;
  const configuredBackend = data.classifier_backend;
  const actualSource = data.classification && data.classification.source;
  if (configuredBackend && configuredBackend !== "offline" && actualSource === "fallback") {
    FALLBACK_DETECTED = true;
    $("#fallback-banner").classList.remove("hidden");
    const badge = $("#backend-badge");
    badge.textContent = "backend: " + configuredBackend + " → OFFLINE (fallback!)";
    badge.className = "px-2 py-1 rounded border border-destructive/40 bg-destructive/15 text-destructive font-semibold";
  }
}

// ---------------------------------------------------------------------------
// theme-aware chart helpers (ECharts)
// ---------------------------------------------------------------------------

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const CHARTS = [];
function makeChart(id) {
  const el = $(id);
  const chart = echarts.init(el);
  CHARTS.push(chart);
  return chart;
}
window.addEventListener("resize", () => CHARTS.forEach((c) => c.resize()));

const CAT_STYLE = {
  hard_decline: "bg-destructive/15 text-destructive border border-destructive/30",
  soft_recoverable: "bg-primary/15 text-primary border border-primary/30",
  needs_reauth: "bg-chart-2/15 text-chart-2 border border-chart-2/30",
  needs_customer_action: "bg-chart-3/15 text-chart-3 border border-chart-3/30",
  needs_review: "bg-chart-4/15 text-chart-4 border border-chart-4/30",
};
const OUTCOME_STYLE = {
  recovered: "bg-primary/15 text-primary border border-primary/30",
  still_failed: "bg-muted text-muted-foreground border border-border",
  escalated: "bg-chart-4/15 text-chart-4 border border-chart-4/30",
  no_action_taken: "bg-destructive/15 text-destructive border border-destructive/30",
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

  // Fire-and-forget: load the Ollama model into memory now, so the first
  // real "Attempt payment" click doesn't eat a 10-15s cold-start on camera.
  // Ollama unloads it again after ~5min idle, so re-warm periodically too.
  if (CONFIG.classifier_backend === "ollama") {
    fetch("/api/warm", { method: "POST" }).catch(() => {});
    setInterval(() => fetch("/api/warm", { method: "POST" }).catch(() => {}), 4 * 60 * 1000);
  }

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
    $$(".retrybtn").forEach((x) => x.classList.remove("border-primary", "text-primary"));
    b.classList.add("border-primary", "text-primary");
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

  // Show visible progress immediately — the classify call can take 10-20s+
  // on a cold CPU backend, and a wall of dim idle cards for that long reads
  // as broken, not slow.
  setStage("#s-failure", "active");
  $("#failure-body").innerHTML =
    `<div class="col-span-2 text-muted-foreground">capturing event…</div>`;
  setStage("#s-classify", "active");
  $("#classify-reasoning").innerHTML =
    `<span class="text-muted-foreground">waiting on classifier<span class="type-cursor"></span> (can take up to 20s on a cold local model)…</span>`;

  let data;
  try {
    data = await postJSON("/api/recover", payload);
  } catch (e) {
    $("#audit-body").textContent = "Error: " + e.message;
    RUNNING = false; $("#pay").disabled = false;
    return;
  }

  checkFallback(data);

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
    `<div><span class="text-muted-foreground">${k}:</span> <span class="text-foreground">${v}</span></div>`).join("");
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
    const color = c.status === "block" ? "text-destructive"
      : c.status === "info" ? "text-muted-foreground" : "text-primary";
    const row = document.createElement("div");
    row.className = "fadein flex gap-2";
    row.innerHTML =
      `<span class="${color}">${icon}</span>
       <span class="text-foreground/80"><span class="text-foreground font-medium">${c.label}</span>
       — ${c.detail}</span>`;
    $("#policy-checks").appendChild(row);
    await sleep(520);
  }
  await sleep(150);
  setStage("#s-policy", "done");

  // 4 — executor
  setStage("#s-exec", "active");
  const d = data.decision, ar = data.agent_run;
  let decHtml = `<div class="font-mono"><span class="text-muted-foreground">action:</span>
    <span class="text-foreground font-medium">${d.action}</span></div>
    <div class="text-muted-foreground mt-0.5">${d.rationale}</div>`;
  if (d.action === "smart_retry" && d.scheduled_retry_at) {
    decHtml += `<div class="text-muted-foreground mt-0.5 font-mono">scheduled: ${d.scheduled_retry_at}
      (${d.retry_delay_hours}h out)</div>`;
  }
  $("#decision-body").innerHTML = decHtml;

  const nAttempts = Math.max(ar.total_attempts, ar.final_outcome === "no_action_taken" ? 0 : 1);
  for (let i = 0; i < ar.total_attempts; i++) {
    const dot = document.createElement("span");
    const recovered = ar.final_outcome === "recovered" && i === ar.total_attempts - 1;
    dot.className = "w-3 h-3 rounded-full fadein " + (recovered ? "bg-primary" : "bg-muted-foreground/40");
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
    `Logged <span class="text-foreground font-mono">${ar.total_attempts + (["no_action_taken"].includes(ar.final_outcome) ? 1 : 0)}</span>
     decision line(s) for <span class="text-foreground font-mono">${ev.transaction_id}</span> —
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
    `outcome: <span class="font-medium">${n.recovered ? `recovered ${inr(n.amount_recovered)}` : "still failed"}</span>`,
    `wasted retries: <span class="text-destructive font-medium">${n.wasted_retries}</span> (${inr(n.wasted_cost_inr)})`,
    c.extra_issuer_declines_avoided
      ? `<span class="text-destructive">+${c.extra_issuer_declines_avoided} extra declines pushed at the issuer</span>` : "",
    `hard-decline protection: <span class="text-destructive">none</span>`,
  ].filter(Boolean).map((x) => `<div>${x}</div>`).join("");

  $("#agent-body").innerHTML = [
    `${ar.total_attempts} attempt(s), ${data.decision.action}`,
    `outcome: <span class="font-medium">${ar.final_outcome === "recovered" ? `recovered ${inr(ar.amount_recovered)}` : ar.final_outcome}</span>`,
    `wasted retries: <span class="text-primary font-medium">${c.agent_wasted_retries}</span>`,
    data.decision.scheduled_retry_at ? `timed for: ${data.decision.scheduled_retry_at}` : "",
    `guardrails: <span class="text-primary">enforced</span>`,
  ].filter(Boolean).map((x) => `<div>${x}</div>`).join("");

  $("#savings-headline").textContent = savingsHeadline(c, n, ar);
  $("#savings-narrative").textContent = c.narrative;
}

function savingsHeadline(c, n, ar) {
  const agentAmt = ar.amount_recovered || 0, naiveAmt = n.amount_recovered || 0;
  const retrySavings = c.savings_inr || 0;

  if (agentAmt > 0 && naiveAmt === 0) {
    return `${inr(agentAmt)} recovered — the naive approach would have lost this payment entirely`
      + (retrySavings > 0 ? ` (plus ${inr(retrySavings)} in wasted retries avoided)` : "");
  }
  if (agentAmt > 0 && naiveAmt > 0) {
    return `Same ${inr(agentAmt)} recovered either way — the agent got there with`
      + (retrySavings > 0 ? ` ${inr(retrySavings)} less wasted spend and` : "")
      + ` proper timing and cooldown discipline`;
  }
  if (agentAmt === 0 && naiveAmt > 0) {
    return `Naive got lucky this time (recovered ${inr(naiveAmt)} on a blind retry) — but it still burned `
      + `${inr(retrySavings)} on attempts that shouldn't have been made; the agent's approach doesn't rely on luck`;
  }
  // neither recovered
  return retrySavings > 0
    ? `Both still failed — but the agent avoided ${inr(retrySavings)} in wasted retries and issuer damage`
    : `Both still failed — same spend either way, no wasted retries on this one`;
}

function appendLedger(data) {
  $("#ledger-wrap").classList.remove("hidden");
  const c = data.comparison, ev = data.event, ar = data.agent_run;
  SESSION_SAVED += Math.max(0, c.savings_inr);
  $("#ledger-total").textContent = inr(SESSION_SAVED);
  const tr = document.createElement("tr");
  tr.className = "fadein border-b border-border/40";
  tr.innerHTML = `
    <td class="p-2 text-muted-foreground">${ev.transaction_id}</td>
    <td class="p-2">${ev.failure_reason}</td>
    <td class="p-2">${data.classification.category}</td>
    <td class="p-2">${data.decision.action}</td>
    <td class="p-2">${ar.final_outcome}</td>
    <td class="p-2">${c.naive_recovered ? "recovered" : "failed"}</td>
    <td class="p-2 text-right text-primary">${inr(Math.max(0, c.savings_inr))}</td>`;
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
    $("#batch-loading").classList.add("hidden");
    $("#batch-content").classList.remove("hidden");
    renderBatch(summary);
  } catch (e) {
    $("#batch-loading").classList.add("hidden");
    const box = $("#batch-error");
    box.classList.remove("hidden");
    box.textContent = "No batch results yet. Run `python main.py` from the project root, then reload.";
  }
}

function tile(label, value, sub) {
  return `<div class="rounded border border-border bg-card p-4">
    <div class="text-xs uppercase tracking-wide text-muted-foreground">${label}</div>
    <div class="text-2xl font-bold text-foreground font-mono mt-1">${value}</div>
    <div class="text-xs text-muted-foreground mt-1">${sub || ""}</div></div>`;
}

function renderReason3D(byReason) {
  const entries = Object.entries(byReason).sort((a, b) => b[1].recovery_rate - a[1].recovery_rate);
  const primary = cssVar("--primary"), chart5 = cssVar("--chart-5");
  const fg = cssVar("--foreground"), mutedFg = cssVar("--muted-foreground"), border = cssVar("--border");
  const chart = makeChart("#chart-reason3d");
  chart.setOption({
    tooltip: {
      formatter: (p) => `<b>${p.data.reason}</b><br/>${p.data.recovered}/${p.data.total} · ${p.data.value[2]}%`,
    },
    xAxis3D: { type: "category", data: entries.map(([k]) => k),
      axisLabel: { color: mutedFg, fontSize: 10, interval: 0, rotate: 25 }, axisLine: { lineStyle: { color: border } } },
    yAxis3D: { type: "category", data: ["recovery rate"], show: false },
    zAxis3D: { type: "value", max: 100, axisLabel: { color: mutedFg, formatter: "{value}%" }, axisLine: { lineStyle: { color: border } } },
    grid3D: {
      boxWidth: 160, boxDepth: 46, boxHeight: 70,
      viewControl: { autoRotate: true, autoRotateSpeed: 7, alpha: 22, beta: 30, distance: 190 },
      light: { main: { intensity: 1.3, shadow: false }, ambient: { intensity: 0.45 } },
      axisLine: { lineStyle: { color: border } },
      splitLine: { lineStyle: { color: border, opacity: 0.3 } },
    },
    series: [{
      type: "bar3D",
      data: entries.map(([k, v], i) => ({
        value: [i, 0, Math.round(v.recovery_rate * 100)], reason: k, recovered: v.recovered, total: v.total,
      })),
      shading: "lambert",
      barSize: 16,
      itemStyle: { color: primary, opacity: 0.92 },
      emphasis: { itemStyle: { color: chart5 } },
      animationDurationUpdate: 600,
    }],
  });
}

function renderOutcomesChart(outcomes) {
  const chart = makeChart("#chart-outcomes");
  const palette = { recovered: cssVar("--primary"), still_failed: cssVar("--muted-foreground"),
    escalated: cssVar("--chart-4"), no_action_taken: cssVar("--destructive") };
  const data = Object.entries(outcomes).map(([k, v]) => ({ name: k, value: v, itemStyle: { color: palette[k] || cssVar("--chart-3") } }));
  chart.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0, textStyle: { color: cssVar("--muted-foreground"), fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
    series: [{
      type: "pie", radius: ["42%", "72%"], center: ["50%", "44%"],
      itemStyle: { borderColor: cssVar("--card"), borderWidth: 2 },
      label: { color: cssVar("--foreground"), fontSize: 11, formatter: "{b}\n{c}" },
      emphasis: { scaleSize: 8 },
      data,
      animationType: "scale", animationEasing: "elasticOut", animationDelay: () => Math.random() * 200,
    }],
  });
}

function renderGuardrailsChart(activity) {
  const chart = makeChart("#chart-guardrails");
  const entries = Object.entries(activity).sort((a, b) => a[1] - b[1]);
  chart.setOption({
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 190, right: 30, top: 5, bottom: 5 },
    xAxis: { type: "value", axisLabel: { color: cssVar("--muted-foreground"), fontSize: 10 },
      splitLine: { lineStyle: { color: cssVar("--border") } } },
    yAxis: { type: "category", data: entries.map((e) => e[0]),
      axisLabel: { color: cssVar("--muted-foreground"), fontFamily: "JetBrains Mono, monospace", fontSize: 10 },
      axisLine: { lineStyle: { color: cssVar("--border") } } },
    series: [{
      type: "bar", data: entries.map((e) => e[1]), barWidth: 12,
      itemStyle: { color: cssVar("--chart-3"), borderRadius: [0, 3, 3, 0] },
      animationDelay: (i) => i * 60,
    }],
  });
}

function renderAccuracyChart(cq, h) {
  const wrap = $("#batch-accuracy-card");
  wrap.innerHTML = `
    <div class="font-semibold text-foreground mb-1">Classifier vs. flat <code class="font-mono">failure_reason</code> lookup</div>
    <div class="text-xs text-muted-foreground mb-3">${cq.twist_records} of ${h.total_count} records are context-dependent cases where the raw failure code is misleading. LLM / heuristic disagreed on <b class="text-foreground font-mono">${cq.llm_heuristic_disagreements}</b> records.</div>
    <div id="chart-accuracy" class="h-[220px]"></div>`;
  const chart = makeChart("#chart-accuracy");
  const cats = ["overall", "clean", "twist"];
  const llm = [cq.category_accuracy_llm, cq.clean_accuracy_llm, cq.twist_accuracy_llm].map((v) => +(v * 100).toFixed(1));
  const heur = [cq.category_accuracy_heuristic, cq.clean_accuracy_heuristic, cq.twist_accuracy_heuristic].map((v) => +(v * 100).toFixed(1));
  chart.setOption({
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => v + "%" },
    legend: { top: 0, textStyle: { color: cssVar("--muted-foreground"), fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
    grid: { left: 40, right: 20, top: 34, bottom: 24 },
    xAxis: { type: "category", data: cats, axisLabel: { color: cssVar("--muted-foreground") },
      axisLine: { lineStyle: { color: cssVar("--border") } } },
    yAxis: { type: "value", max: 100, axisLabel: { color: cssVar("--muted-foreground"), formatter: "{value}%" },
      splitLine: { lineStyle: { color: cssVar("--border") } } },
    series: [
      { name: "LLM", type: "bar", data: llm, barGap: "20%", itemStyle: { color: cssVar("--primary"), borderRadius: [3, 3, 0, 0] }, animationDelay: (i) => i * 100 },
      { name: "flat heuristic", type: "bar", data: heur, itemStyle: { color: cssVar("--chart-4"), borderRadius: [3, 3, 0, 0] }, animationDelay: (i) => i * 100 + 80 },
    ],
  });
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

  if (cq) renderAccuracyChart(cq, h);
  renderReason3D(s.recovery_by_failure_reason);
  renderOutcomesChart(s.outcomes);
  renderGuardrailsChart(s.guardrail_activity);

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
    <tr class="border-b border-border/30">
      <td class="p-2 text-muted-foreground">${r.transaction_id}</td>
      <td class="p-2">${r.failure_reason}</td>
      <td class="p-2">${r.attempt_number}</td>
      <td class="p-2">${r.category}</td>
      <td class="p-2">${r.action_taken}</td>
      <td class="p-2">${r.outcome}</td>
      <td class="p-2 text-muted-foreground max-w-[420px] truncate" title="${(r.reasoning || "").replace(/"/g, "&quot;")}">${r.reasoning || ""}</td>
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