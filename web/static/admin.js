(() => {
  const $ = (s) => document.querySelector(s);
  const KEY = "is_admin_token";
  const getToken = () => { try { return sessionStorage.getItem(KEY) || ""; } catch { return ""; } };
  const setToken = (t) => { try { sessionStorage.setItem(KEY, t); } catch {} };

  // Same browser id the main site uses. Sending it marks this browser as the owner's,
  // so your own visits (past and future) are left out of the stats.
  const visitorId = (() => {
    try {
      let id = localStorage.getItem("is_vid");
      if (!id) { id = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2); localStorage.setItem("is_vid", id); }
      return id;
    } catch { return ""; }
  })();

  async function api(path) {
    const res = await fetch(path, { headers: { "X-Admin-Token": getToken(), "X-Visitor-Id": visitorId } });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res;
  }

  function table(el, columns, rows, empty) {
    el.innerHTML = "";
    const head = el.createTHead().insertRow();
    columns.forEach(([, label]) => { const th = document.createElement("th"); th.textContent = label; head.appendChild(th); });
    const body = el.createTBody();
    if (!rows.length) { const c = body.insertRow().insertCell(); c.colSpan = columns.length; c.textContent = empty; c.className = "muted"; return; }
    rows.forEach((row) => {
      const tr = body.insertRow();
      columns.forEach(([key, , cls]) => { const c = tr.insertCell(); c.textContent = row[key] ?? ""; if (cls) c.className = cls; });
    });
  }

  const pct = (a, b) => b ? Math.round(a * 100 / b) + "%" : "—";

  // ---------- period: today by default, in the admin's own time zone ----------
  const PERIOD_KEY = "is_admin_period";
  let period = (() => { try { return JSON.parse(sessionStorage.getItem(PERIOD_KEY)) || { key: "today" }; } catch { return { key: "today" }; } })();
  const pad = (n) => String(Math.abs(n)).padStart(2, "0");
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parseYmd = (v) => { const [y, m, d] = v.split("-").map(Number); return new Date(y, m - 1, d); };
  // Local midnight with its UTC offset, e.g. 2026-11-17T00:00:00+03:00.
  const iso = (d) => { const off = -d.getTimezoneOffset(); return `${ymd(d)}T00:00:00${off >= 0 ? "+" : "-"}${pad(Math.trunc(off / 60))}:${pad(off % 60)}`; };
  const fmtDay = (d) => d.toLocaleDateString(undefined, { day: "numeric", month: "short" });

  function periodRange(p) {
    const today = midnight(new Date());
    switch (p.key) {
      case "today": return [today, addDays(today, 1), `Today, ${fmtDay(today)}`];
      case "yesterday": return [addDays(today, -1), today, `Yesterday, ${fmtDay(addDays(today, -1))}`];
      case "7d": return [addDays(today, -6), addDays(today, 1), "Last 7 days"];
      case "30d": return [addDays(today, -29), addDays(today, 1), "Last 30 days"];
      case "day": { const d = parseYmd(p.day); return [d, addDays(d, 1), fmtDay(d) + ", " + d.getFullYear()]; }
      default: return [null, null, "All time"];
    }
  }

  function setPeriod(p) {
    period = p;
    try { sessionStorage.setItem(PERIOD_KEY, JSON.stringify(p)); } catch {}
    load();
  }

  function statsUrl() {
    const [start, end] = periodRange(period);
    const q = new URLSearchParams({ tz: String(-new Date().getTimezoneOffset()) });
    if (start) { q.set("start", iso(start)); q.set("end", iso(end)); }
    return "/api/admin/stats?" + q;
  }

  function renderPeriod() {
    const [, , label] = periodRange(period);
    document.querySelectorAll("[data-period]").forEach((b) => b.classList.toggle("active", b.dataset.period === period.key));
    const input = $("#period-day");
    input.max = ymd(new Date());
    input.value = period.key === "day" ? period.day : "";
    input.classList.toggle("active", period.key === "day");
    document.querySelectorAll(".period-label").forEach((el) => { el.textContent = label; });
  }

  function render(s) {
    const f = Object.fromEntries(s.funnel.map((x) => [x.step, x.visitors]));
    const visits = f["Visited the site"];
    const tiles = [
      [visits, "visitors"],
      [f["Tried the demo"], `tried the demo (${pct(f["Tried the demo"], visits)})`],
      [f["Left feedback"], "left feedback"],
      [f["Joined the waitlist"], `joined the waitlist (${pct(f["Joined the waitlist"], visits)})`],
      [s.invoices_processed.total, period.key === "today" ? "invoices processed" : `invoices processed (${s.invoices_processed.today} today)`],
    ];
    $("#tiles").innerHTML = "";
    tiles.forEach(([n, label]) => { const d = document.createElement("div"); d.className = "tile"; d.innerHTML = "<b></b><span></span>"; d.firstChild.textContent = n; d.lastChild.textContent = label; $("#tiles").appendChild(d); });

    const max = Math.max(1, ...s.funnel.map((x) => x.visitors));
    $("#funnel").innerHTML = "";
    s.funnel.forEach((x) => {
      const row = document.createElement("div");
      row.className = "bar-row";
      row.innerHTML = `<span></span><div class="bar-track"><div class="bar"></div></div><span class="bar-val"></span>`;
      row.querySelector(".bar").style.width = (x.visitors * 100 / max) + "%";
      row.firstChild.textContent = x.step;
      row.lastChild.textContent = `${x.visitors} · ${pct(x.visitors, visits)}`;
      $("#funnel").appendChild(row);
    });

    const dmax = Math.max(1, ...s.daily_visitors.map((d) => d.visitors));
    $("#days").innerHTML = "";
    s.daily_visitors.forEach((d) => {
      const col = document.createElement("div");
      col.className = "day";
      col.title = `${d.day}: ${d.visitors} visitors · click to see this day`;
      col.classList.toggle("selected", (period.key === "day" && period.day === d.day) || (period.key === "today" && d.day === ymd(new Date())));
      col.addEventListener("click", () => setPeriod(d.day === ymd(new Date()) ? { key: "today" } : { key: "day", day: d.day }));
      col.innerHTML = `<span></span><i></i><span></span>`;
      col.querySelector("i").style.height = (d.visitors * 80 / dmax) + "%";
      col.firstChild.textContent = d.visitors || "";
      col.lastChild.textContent = d.day.slice(5);
      $("#days").appendChild(col);
    });

    table($("#visits"), [["created_at", "When"], ["visitor", "Browser tag"], ["source", "Source"], ["device", "Device"], ["page", "Page"]], s.visits || [], "No visits in this period");
    table($("#plans"), [["plan", "Plan"], ["clicks", "Clicked", "num"], ["signups", "Reserved", "num"]], s.plans, "No plan clicks yet");
    table($("#sources"), [["source", "Source"], ["visitors", "Visitors", "num"], ["signups", "Sign-ups", "num"]], s.sources, "No visits yet");
    table($("#signups"), [["created_at", "When"], ["email", "Email"], ["plan", "Plan"], ["monthly_volume", "Invoices/mo"], ["integrations", "Integrations"], ["comment", "Comment", "wrap-text"], ["source", "Source"]], s.signups, "No sign-ups yet");
    table($("#feedback"), [["created_at", "When"], ["rating", "Rating"], ["monthly_volume", "Invoices/mo"], ["comment", "Comment", "wrap-text"], ["source", "Source"]], s.feedback, "No feedback yet");
    $("#updated").textContent = "Updated " + new Date().toLocaleString();
  }

  async function load() {
    try {
      renderPeriod();
      const res = await api(statsUrl());
      render(await res.json());
      $("#login").hidden = true; $("#dash").hidden = false;
    } catch (err) {
      $("#dash").hidden = true; $("#login").hidden = false;
      if (getToken()) { $("#login-error").textContent = err.message; $("#login-error").hidden = false; }
    }
  }

  document.querySelectorAll("[data-period]").forEach((b) => b.addEventListener("click", () => setPeriod({ key: b.dataset.period })));
  $("#period-day").addEventListener("change", (e) => { if (e.target.value) setPeriod({ key: "day", day: e.target.value }); });

  $("#login").addEventListener("submit", (e) => { e.preventDefault(); setToken($("#token").value.trim()); load(); });
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-export]");
    if (!btn) return;
    const res = await api(`/api/admin/export/${btn.dataset.export}.csv`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a"); a.href = url; a.download = btn.dataset.export + ".csv"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  load();
})();
