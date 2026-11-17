(() => {
  "use strict";

  // ---------- visitor id + traffic source (first touch) ----------
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  const randomId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  let visitorId = store.get("is_vid");
  if (!visitorId) { visitorId = randomId(); store.set("is_vid", visitorId); }

  function detectSource() {
    const params = new URLSearchParams(location.search);
    const tagged = params.get("utm_source") || params.get("ref") || params.get("source");
    if (tagged) return tagged.slice(0, 100);
    try {
      const host = document.referrer ? new URL(document.referrer).hostname : "";
      return host && host !== location.hostname ? host : "";
    } catch { return ""; }
  }
  let source = store.get("is_src");
  if (source === null) { source = detectSource(); store.set("is_src", source); }

  function track(event, detail = "") {
    fetch("/api/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ visitor_id: visitorId, event, detail, source }),
      keepalive: true,
    }).catch(() => {});
  }
  // A visit counts only once a person does something: moves the mouse, scrolls, touches
  // the screen or presses a key. Link scanners (e.g. the safety check behind a LinkedIn
  // link) load the page like a normal browser but never interact, so they are not counted.
  // Automated browsers also say so in navigator.webdriver.
  const HUMAN_EVENTS = ["pointermove", "pointerdown", "wheel", "scroll", "touchstart", "keydown"];
  function countVisit(e) {
    if (!e.isTrusted) return; // events faked by a script
    HUMAN_EVENTS.forEach((name) => window.removeEventListener(name, countVisit, true));
    track("visit", location.pathname + location.search);
  }
  if (!navigator.webdriver) {
    HUMAN_EVENTS.forEach((name) => window.addEventListener(name, countVisit, { capture: true, passive: true }));
  }

  // ---------- elements ----------
  const $ = (sel) => document.querySelector(sel);
  const dropzone = $("#dropzone"), fileInput = $("#file-input"), statusBox = $("#status");
  const results = $("#results"), tbody = $("#results-table tbody");
  const modal = $("#modal"), form = $("#modal-form");

  let config = { upload_enabled: false, free_invoices: 8, free_left: 8, max_mb: 10, max_request_mb: 30, busy_today: false };
  let currentRows = [];
  let currentIsSample = false;
  let triedDemo = false;
  let limitModalShown = false;

  // ---------- config / free invoices left ----------
  async function loadConfig() {
    try {
      const res = await fetch("/api/config?visitor_id=" + encodeURIComponent(visitorId));
      if (res.ok) config = await res.json();
    } catch { /* keep defaults */ }
    renderFreeNote();
  }

  function renderFreeNote() {
    const note = $("#free-note"), hint = $("#dz-hint");
    hint.textContent = `PDF only, up to ${config.max_mb} MB each`;
    dropzone.classList.remove("disabled");
    if (!config.upload_enabled) {
      note.textContent = "Live upload is paused right now — try the 10 sample invoices below to see the result.";
      dropzone.classList.add("disabled");
    } else if (config.owner) {
      note.textContent = `Admin mode: no free-invoice limit (up to ${config.free_left} files per upload).`;
    } else if (config.free_left <= 0) {
      note.innerHTML = `You've used your ${config.free_invoices} free invoices. <button class="link-btn" data-open-modal="" data-mode="limit">Get early access</button> to keep going.`;
      dropzone.classList.add("disabled");
    } else {
      note.textContent = `Upload up to ${config.free_left} more invoice${config.free_left === 1 ? "" : "s"} for free. No signup, no card.`;
    }
  }

  // ---------- status line ----------
  function showStatus(text, kind = "info") {
    statusBox.hidden = false;
    statusBox.className = "status" + (kind === "error" ? " error" : "");
    statusBox.innerHTML = (kind === "busy" ? '<span class="spinner"></span>' : "") + `<span></span>`;
    statusBox.lastChild.textContent = text;
  }
  const hideStatus = () => { statusBox.hidden = true; };

  // ---------- upload ----------
  function chooseFiles() {
    if (!config.upload_enabled) { showStatus("Live upload is paused right now. Try the sample invoices instead.", "error"); return; }
    if (config.free_left <= 0) { openModal("limit"); return; }
    fileInput.click();
  }
  dropzone.addEventListener("click", chooseFiles);
  dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); chooseFiles(); } });
  fileInput.addEventListener("change", () => { upload([...fileInput.files]); fileInput.value = ""; });
  ["dragenter", "dragover"].forEach((t) => dropzone.addEventListener(t, (e) => { e.preventDefault(); dropzone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => dropzone.addEventListener(t, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); }));
  dropzone.addEventListener("drop", (e) => {
    if (config.free_left <= 0 && config.upload_enabled) { openModal("limit"); return; }
    upload([...e.dataTransfer.files]);
  });

  async function upload(files) {
    files = files.filter((f) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf");
    if (!files.length) { showStatus("Please choose PDF files.", "error"); return; }
    if (!config.upload_enabled) { showStatus("Live upload is paused right now. Try the sample invoices instead.", "error"); return; }
    if (config.free_left <= 0) { openModal("limit"); return; }
    if (files.length > config.free_left) {
      showStatus(`You have ${config.free_left} free invoice${config.free_left === 1 ? "" : "s"} left — please choose ${config.free_left} file${config.free_left === 1 ? "" : "s"} or fewer.`, "error");
      return;
    }
    const tooBig = files.find((f) => f.size > config.max_mb * 1024 * 1024);
    if (tooBig) { showStatus(`${tooBig.name} is larger than ${config.max_mb} MB.`, "error"); return; }
    const totalSize = files.reduce((sum, f) => sum + f.size, 0);
    if (totalSize > config.max_request_mb * 1024 * 1024) {
      showStatus(`These files add up to more than ${config.max_request_mb} MB. Please upload them in smaller batches.`, "error");
      return;
    }

    const data = new FormData();
    files.forEach((f) => data.append("files", f));
    data.append("visitor_id", visitorId);
    data.append("source", source);
    showStatus(`Reading ${files.length} invoice${files.length === 1 ? "" : "s"}… this usually takes 10–30 seconds.`, "busy");
    dropzone.classList.add("disabled");

    try {
      const res = await fetch("/api/extract", { method: "POST", body: data });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = body.detail || {};
        if (detail.code === "free_limit") { config.free_left = 0; renderFreeNote(); hideStatus(); openModal("limit"); return; }
        showStatus(typeof detail === "string" ? detail : (detail.message || "Something went wrong. Please try again."), "error");
        return;
      }
      hideStatus();
      triedDemo = true;
      config.free_left = body.free_left;
      renderResults(body.rows, false);
      if (config.free_left <= 0) {
        // Let them see their results first, then ask for the pre-order + feedback.
        setTimeout(() => { if (!limitModalShown) openModal("limit"); }, 2500);
      }
    } catch {
      showStatus("Network error. Please check your connection and try again.", "error");
    } finally {
      renderFreeNote();
    }
  }

  // ---------- sample invoices (free, no AI call) ----------
  async function showSamples() {
    showStatus("Loading sample results…", "busy");
    try {
      const res = await fetch("/api/sample-results");
      const body = await res.json();
      hideStatus();
      triedDemo = true;
      track("demo_sample");
      renderResults(body.rows, true);
      results.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch {
      showStatus("Couldn't load the samples. Please refresh the page.", "error");
    }
  }
  $("#sample-btn").addEventListener("click", showSamples);
  $("#hero-sample").addEventListener("click", () => { document.querySelector("#demo").scrollIntoView({ behavior: "smooth" }); showSamples(); });

  // ---------- results table ----------
  const COLS = ["supplier_name", "invoice_number", "invoice_date", "due_date", "subtotal", "tax", "total", "currency"];
  const NUM = new Set(["subtotal", "tax", "total"]);

  function td(text, cls = "") {
    const cell = document.createElement("td");
    if (cls) cell.className = cls;
    cell.textContent = text;
    return cell;
  }

  function renderResults(rows, isSample) {
    currentRows = rows;
    currentIsSample = isSample;
    tbody.innerHTML = "";
    for (const row of rows) {
      const tr = document.createElement("tr");
      const fileCell = td("", "file");
      if (isSample) {
        const a = document.createElement("a");
        a.href = "/samples/" + encodeURIComponent(row.file_name);
        a.target = "_blank"; a.rel = "noopener";
        a.textContent = row.file_name;
        a.title = "Open the original PDF";
        fileCell.appendChild(a);
      } else {
        fileCell.textContent = row.file_name;
      }
      tr.appendChild(fileCell);
      for (const col of COLS) {
        const value = row[col] || "";
        tr.appendChild(td(value || "—", [NUM.has(col) ? "num" : "", value ? "" : "empty"].join(" ").trim()));
      }
      const statusCell = document.createElement("td");
      const pill = document.createElement("span");
      const review = row.needs_review === "YES";
      pill.className = "pill " + (review ? "pill-review" : "pill-ok");
      pill.textContent = review ? "Check" : "OK";
      statusCell.appendChild(pill);
      if (review && row.review_reasons) {
        const reason = document.createElement("span");
        reason.className = "reason";
        reason.textContent = row.review_reasons;
        statusCell.appendChild(reason);
      }
      tr.appendChild(statusCell);
      tbody.appendChild(tr);
    }
    const flagged = rows.filter((r) => r.needs_review === "YES").length;
    $("#results-title").textContent = isSample ? "Sample results (10 example invoices)" : "Your results";
    $("#results-sub").textContent =
      `${rows.length} invoice${rows.length === 1 ? "" : "s"} · ${flagged} flagged for review` +
      (isSample ? " · click a file name to open the original PDF" : "");
    results.hidden = false;

    const oldBanner = $("#limit-banner");
    if (oldBanner) oldBanner.remove();
    if (!isSample && config.free_left <= 0) {
      const banner = document.createElement("div");
      banner.className = "limit-banner";
      banner.id = "limit-banner";
      banner.innerHTML = `<p>That was your last free invoice. Want unlimited access when we launch?</p>
        <button class="btn btn-small" data-open-modal="" data-mode="limit">Get early access</button>`;
      $(".results-foot").before(banner);
    }
  }

  // ---------- CSV download ----------
  $("#download-btn").addEventListener("click", () => {
    const header = ["file_name", ...COLS, "needs_review", "review_reasons"];
    // A cell starting with = + - @ would run as a formula in Excel ("formula injection").
    const esc = (v) => {
      // Plain numbers such as "-120.50" (credit notes) are left alone.
      if (/^[=+\-@\t\r]/.test(v) && !/^[+-]?\d[\d.,]*$/.test(v)) v = "'" + v;
      return /[",\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
    };
    const lines = [header.join(","), ...currentRows.map((r) => header.map((h) => esc(r[h] || "")).join(","))];
    const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = currentIsSample ? "sample_invoices.csv" : "invoices.csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    track("csv_download", currentIsSample ? "sample" : "own");
  });

  // ---------- pre-order + feedback window ----------
  const MODES = {
    limit: {
      title: () => `You've used your ${config.free_invoices} free invoices`,
      lead: "Thanks for trying it! Two quick things help us decide what to build: tell us what worked and what didn't, and whether you'd pay for it. No card needed.",
      submit: "Send & reserve my spot",
    },
    plan: {
      title: () => "Reserve your spot",
      lead: "No card needed. We'll email you when paid access opens.",
      submit: "Reserve my spot",
    },
    feedback: {
      title: () => "Tell us what you think",
      lead: "What's wrong, what's missing, what would make you pay for this? Every message is read by the founder.",
      submit: "Send feedback",
    },
    early: {
      title: () => "Get early access",
      lead: "No card needed. We'll email you when paid access opens.",
      submit: "Reserve my spot",
    },
  };
  let modalMode = "early";
  let lastFocus = null;

  function openModal(mode, plan = "") {
    modalMode = MODES[mode] ? mode : "early";
    if (modalMode === "limit") { limitModalShown = true; track("signup_open", "limit"); }
    else track("signup_open", plan || modalMode);
    const m = MODES[modalMode];
    $("#modal-title").textContent = m.title(plan);
    $("#modal-lead").textContent = m.lead;
    $("#modal-submit").textContent = m.submit;
    form.reset();
    $("#form-error").hidden = true;
    // The rating question only makes sense after trying the demo.
    $("#rating-chips").closest("fieldset").querySelector("legend").textContent =
      triedDemo || modalMode === "limit" ? "How did the extraction do?" : "Have you tried the demo?";
    if (plan) {
      const radio = form.querySelector(`input[name="plan"][value="${CSS.escape(plan)}"]`);
      if (radio) radio.checked = true;
    }
    $("#modal-form-view").hidden = false;
    $("#modal-thanks").hidden = true;
    lastFocus = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    setTimeout(() => form.querySelector(modalMode === "feedback" ? "textarea" : "input")?.focus(), 30);
  }

  function closeModal() {
    modal.hidden = true;
    document.body.style.overflow = "";
    if (lastFocus) lastFocus.focus();
  }

  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-open-modal]");
    if (!opener) return;
    e.preventDefault();
    const plan = opener.dataset.openModal;
    if (plan) track("plan_click", plan);
    openModal(opener.dataset.mode || (plan ? "plan" : "early"), plan);
  });
  $("#modal-close").addEventListener("click", closeModal);
  $("#thanks-close").addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) closeModal(); });
  form.addEventListener("change", (e) => {
    if (e.target.name === "plan") track("plan_click", e.target.value);
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const rating = fd.get("rating") || "";
    const comment = (fd.get("comment") || "").trim();
    const plan = fd.get("plan") || "";
    const email = (fd.get("email") || "").trim();
    const volume = fd.get("monthly_volume") || "";
    const integrations = fd.getAll("integrations");
    const website = fd.get("website") || "";  // honeypot, empty for people
    const errorBox = $("#form-error");
    const fail = (msg) => { errorBox.textContent = msg; errorBox.hidden = false; };

    if (!rating && !comment && !plan && !email) return fail("Please answer at least one question.");
    if (plan === "Yes $19" && !email) return fail("Add your email so we can save your spot.");
    if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) return fail("Please check your email address.");

    const submit = $("#modal-submit");
    submit.disabled = true;
    const post = (url, payload) => fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ visitor_id: visitorId, source, website, ...payload }),
    }).then(async (res) => { if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "error"); });

    try {
      const jobs = [];
      if (rating || comment || (plan && !email)) {
        const note = plan && !email ? `[plan: ${plan}] ` : "";
        jobs.push(post("/api/feedback", { rating, comment: note + comment, monthly_volume: volume }));
      }
      if (email) {
        jobs.push(post("/api/signup", { email, plan, monthly_volume: volume, integrations, comment }));
      }
      await Promise.all(jobs);
      $("#thanks-text").textContent = email
        ? (plan === "Yes $19"
          ? "You're on the list. We'll email you when paid access opens."
          : "Thanks for being honest — that helps a lot. We'll let you know when it launches.")
        : "Your feedback goes straight to the founder and shapes what we build next.";
      $("#modal-form-view").hidden = true;
      $("#modal-thanks").hidden = false;
    } catch (err) {
      fail(typeof err.message === "string" && err.message !== "error" ? err.message : "Couldn't send. Please try again.");
    } finally {
      submit.disabled = false;
    }
  });

  // ---------- pricing seen ----------
  const pricing = document.querySelector("#pricing");
  if ("IntersectionObserver" in window && pricing) {
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) { track("pricing_view"); observer.disconnect(); }
    }, { threshold: 0.35 });
    observer.observe(pricing);
  }

  loadConfig();
})();
