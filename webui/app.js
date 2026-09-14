/* JOB.EXE console — vanilla SPA. No build step, no CDNs, no external URLs. */

"use strict";

var API = { tools: "/api/tools", kpis: "/api/kpis", run: "/api/run" };
var TOOLS = null;        // catalog from GET /api/tools
var KPIS = null;         // cached dashboard data
var SERVER_OK = false;

var ROUTES = [
  { hash: "dashboard", label: "DASHBOARD" },
  { hash: "find",      label: "FIND",  group: "FIND" },
  { hash: "tailor",    label: "TAILOR", group: "TAILOR" },
  { hash: "train",     label: "TRAIN", group: "TRAIN" },
  { hash: "win",       label: "WIN",   group: "WIN" },
  { hash: "govern",    label: "GOVERN", group: "GOVERN" }
];

/* ================= auth ================= */

var TOKEN_KEY = "veto_token";

function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}
function setToken(t) {
  try { sessionStorage.setItem(TOKEN_KEY, t); } catch (e) { /* storage unavailable */ }
}
function clearToken() {
  try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
}

function authHeaders(extra) {
  var h = {};
  if (extra) for (var k in extra) if (Object.prototype.hasOwnProperty.call(extra, k)) h[k] = extra[k];
  var t = getToken();
  if (t) h["Authorization"] = "Bearer " + t;
  return h;
}

function isUnauthorized(err) {
  return !!(err && err.status === 401);
}

function showLogin(msg) {
  var errBox = document.getElementById("login-err");
  errBox.classList.add("hidden");
  errBox.textContent = "";
  if (msg) {
    errBox.textContent = msg;
    errBox.classList.remove("hidden");
  }
  var input = document.getElementById("signin-token");
  input.value = "";
  document.getElementById("login-overlay").classList.remove("hidden");
  setTimeout(function () { input.focus(); }, 60);
}
function hideLogin() {
  document.getElementById("login-overlay").classList.add("hidden");
}

/* 401 from the API: the stored token is bad or expired — drop it and gate again */
function handleUnauthorized(msg) {
  /* A 401 can land mid-wizard: the open modal would sit behind the login gate
     as a dead spinner after re-login — close it first */
  var modalOpen = !document.getElementById("modal-overlay").classList.contains("hidden");
  if (modalOpen) closeModal();
  clearToken();
  var pill = document.getElementById("status-pill");
  var txt = document.getElementById("status-text");
  pill.classList.remove("status-online", "status-offline", "status-unknown");
  pill.classList.add("status-offline");
  txt.textContent = "LOCKED";
  var loginMsg = msg || "Invalid or expired token. Paste the token printed in the terminal where you ran serve.";
  if (modalOpen) loginMsg = "Session expired \u2014 an open step was interrupted; please re-run it after unlocking. " + loginMsg;
  showLogin(loginMsg);
}

/* ================= utils ================= */

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function el(tag, cls, html) {
  var d = document.createElement(tag);
  if (cls) d.className = cls;
  if (html != null) d.innerHTML = html;
  return d;
}

function fmtAvg(v) {
  if (v == null || v === "" || isNaN(Number(v))) return "--";
  var n = Number(v);
  if (n > 0 && n <= 1) return Math.round(n * 100) + "%";
  return n.toFixed(1);
}

/* Banner + status pill for server-down state */
function setServerStatus(ok) {
  SERVER_OK = ok;
  var pill = document.getElementById("status-pill");
  var txt = document.getElementById("status-text");
  pill.classList.remove("status-online", "status-offline", "status-unknown");
  if (ok) {
    pill.classList.add("status-online");
    txt.textContent = "ONLINE";
    hideBanner();
  } else {
    pill.classList.add("status-offline");
    txt.textContent = "OFFLINE";
    showBanner('API SERVER DOWN — start webui.py (127.0.0.1:8765) and <button type="button" id="banner-retry">RETRY</button>');
    var r = document.getElementById("banner-retry");
    if (r) r.addEventListener("click", function () { boot(true); });
  }
}

function showBanner(html) {
  var b = document.getElementById("banner");
  b.innerHTML = html;
  b.classList.remove("hidden");
}
function hideBanner() {
  var b = document.getElementById("banner");
  b.classList.add("hidden");
  b.innerHTML = "";
}

async function apiGet(url) {
  var res = await fetch(url, { headers: authHeaders({ "Accept": "application/json" }) });
  var data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    var err = new Error("HTTP " + res.status + " from " + url);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function apiPost(url, body) {
  var res = await fetch(url, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json", "Accept": "application/json" }),
    body: JSON.stringify(body)
  });
  var data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    /* fetch does not reject on 400: attach the parsed JSON body so callers
       can show the server's own error message instead of a generic failure */
    var err = new Error("HTTP " + res.status + " from " + url);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/* Error message carried by a server JSON error body (HTTP 4xx), if any. */
function serverError(err) {
  return (err && err.data && err.data.error) || null;
}

/* ================= modal ================= */

function openModal(html) {
  var box = document.getElementById("modal-box");
  box.innerHTML = html;
  document.getElementById("modal-overlay").classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modal-overlay").classList.add("hidden");
  document.getElementById("modal-box").innerHTML = "";
}
document.addEventListener("DOMContentLoaded", function () {
  document.getElementById("modal-overlay").addEventListener("click", function (e) {
    if (e.target.id === "modal-overlay") closeModal();
  });

  /* login gate: no token, no API calls */
  document.getElementById("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var btn = document.getElementById("login-unlock");
    if (btn && btn.disabled) return; /* boot already in flight — ignore double submit */
    var t = document.getElementById("signin-token").value.trim();
    if (!t) {
      var eb = document.getElementById("login-err");
      /* clear-then-set so the role="alert" region re-announces on repeated submits */
      eb.textContent = "";
      eb.textContent = "Paste the token printed in the terminal where you ran serve.";
      eb.classList.remove("hidden");
      return;
    }
    setToken(t);
    hideLogin();
    /* double-submit guard: keep UNLOCK disabled while boot is in flight */
    if (btn) btn.disabled = true;
    var done = function () { if (btn) btn.disabled = false; };
    try {
      var p = boot(true); /* retry the initial API calls with the new token */
      if (p && typeof p.then === "function") p.then(done, done);
      else done();
    } catch (err) { done(); }
  });

  if (!getToken()) {
    var pill = document.getElementById("status-pill");
    pill.classList.remove("status-unknown");
    pill.classList.add("status-offline");
    document.getElementById("status-text").textContent = "LOCKED";
    showLogin(null);
  } else {
    boot(false);
  }
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closeModal();
  if (e.key !== "Tab") return;
  /* lightweight focus trap: while the login gate is visible, keep Tab cycling
     inside the dialog instead of leaking to the unreachable page behind it */
  var ov = document.getElementById("login-overlay");
  if (!ov || ov.classList.contains("hidden")) return;
  var focusables = ov.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), textarea, select, [tabindex]:not([tabindex="-1"])');
  if (!focusables.length) { e.preventDefault(); return; }
  var first = focusables[0];
  var last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
});

/* ================= catalog helpers ================= */

function findTool(pred) {
  var list = (TOOLS && TOOLS.tools) || [];
  return list.filter(pred)[0] || null;
}
function findAction(tool, pred) {
  var list = (tool && tool.actions) || [];
  return list.filter(pred)[0] || list[0] || null;
}
function toolForId(id) {
  return findTool(function (t) { return String(t.id) === String(id); });
}
function paramDefaults(action) {
  var out = {};
  ((action && action.params) || []).forEach(function (p) {
    if (p.default !== undefined && p.default !== null && p.default !== "") out[p.name] = p.default;
  });
  return out;
}

/* ================= router ================= */

function currentRoute() {
  var h = (location.hash || "#dashboard").replace(/^#/, "");
  return ROUTES.filter(function (r) { return r.hash === h; })[0] || ROUTES[0];
}

function markNav(route) {
  Array.prototype.forEach.call(document.querySelectorAll(".nav-link"), function (a) {
    a.classList.toggle("active", a.getAttribute("data-route") === route.hash);
  });
}

function render() {
  var route = currentRoute();
  markNav(route);
  var view = document.getElementById("view");
  if (!SERVER_OK) {
    view.innerHTML = "";
    view.appendChild(el("div", "empty",
      '<span class="big">NO UPLINK</span>' +
      "The API server is unreachable. Start <b>webui.py</b> (127.0.0.1:8765) then reload."));
    return;
  }
  if (route.hash === "dashboard") { renderDashboard(view); return; }
  renderGroupPage(view, route);
}

window.addEventListener("hashchange", render);

/* ================= dashboard ================= */

function kpiCards(k) {
  var cards = [
    { label: "APPLICATIONS",  value: k.applications != null ? k.applications : "--", cls: "" },
    { label: "AVG FIT SCORE", value: fmtAvg(k.avg_fit), cls: "" },
    { label: "ACTIVE STREAK", value: (k.streak_days != null ? k.streak_days : "--") + "d", cls: "amber" },
    { label: "INTERVIEWS",    value: k.interviews != null ? k.interviews : "--", cls: "" },
    { label: "OFFERS",        value: k.offers != null ? k.offers : "--", cls: "" },
    { label: "FOLLOW-UPS DUE", value: k.followups_due != null ? k.followups_due : "--", cls: k.followups_due > 0 ? "red" : "" }
  ];
  return '<div class="kpi-grid">' + cards.map(function (c) {
    return '<div class="kpi"><div class="kpi-label">' + c.label + '</div>' +
      '<div class="kpi-value ' + c.cls + '">' + esc(c.value) + "</div></div>";
  }).join("") + "</div>";
}

function funnelHtml(funnel) {
  funnel = funnel || {};
  var stages = [
    { key: "applied",   label: "APPLIED",   cls: "blue" },
    { key: "phone",     label: "PHONE",     cls: "" },
    { key: "interview", label: "INTERVIEW", cls: "amber" },
    { key: "offer",     label: "OFFER",     cls: "" },
    { key: "rejected",  label: "REJECTED",  cls: "red" }
  ];
  var vals = stages.map(function (s) { return Number(funnel[s.key]) || 0; });
  var max = Math.max.apply(null, vals.concat([1]));
  var total = vals.reduce(function (a, b) { return a + b; }, 0);
  if (!total) return '<div class="empty"><span class="big">NO FUNNEL DATA</span>Log an application and this chart fills in.</div>';
  return stages.map(function (s, i) {
    var v = vals[i];
    return '<div class="bar-row"><div class="bar-label">' + s.label + '</div>' +
      '<div class="bar-track"><div class="bar-fill ' + s.cls + '" style="width:' +
      Math.round(v / max * 100) + '%"></div></div>' +
      '<div class="bar-num">' + v + "</div></div>";
  }).join("");
}

function fitDistHtml(k) {
  var bins = null;
  if (Array.isArray(k.fit_distribution)) {
    bins = k.fit_distribution; // [{label,count}] or [[label,count]]
  } else if (Array.isArray(k.fit_scores) && k.fit_scores.length) {
    var edges = [0, 60, 70, 80, 90, 101];
    var counts = [0, 0, 0, 0, 0];
    k.fit_scores.forEach(function (v) {
      var n = Number(v); if (isNaN(n)) return;
      for (var i = 0; i < edges.length - 1; i++) {
        if (n >= edges[i] && n < edges[i + 1]) { counts[i]++; break; }
      }
    });
    var labels = ["<60", "60-69", "70-79", "80-89", "90+"];
    bins = labels.map(function (l, i) { return { label: l, count: counts[i] }; });
  }
  if (!bins || !bins.length) {
    return '<div class="empty"><span class="big">NO FIT DATA</span>Run a fit-score workflow to populate this.</div>';
  }
  var max = Math.max.apply(null, bins.map(function (b) {
    return Number(Array.isArray(b) ? b[1] : b.count) || 0;
  }).concat([1]));
  return bins.map(function (b) {
    var label = Array.isArray(b) ? b[0] : (b.label || "?");
    var v = Number(Array.isArray(b) ? b[1] : b.count) || 0;
    return '<div class="bar-row"><div class="bar-label">' + esc(label) + '</div>' +
      '<div class="bar-track"><div class="bar-fill" style="width:' +
      Math.round(v / max * 100) + '%"></div></div>' +
      '<div class="bar-num">' + v + "</div></div>";
  }).join("");
}

function heatmapHtml(activity) {
  activity = Array.isArray(activity) ? activity : [];
  if (!activity.length) {
    return '<div class="empty"><span class="big">NO ACTIVITY</span>Log reps to light up the grid.</div>';
  }
  var map = {};
  activity.forEach(function (a) {
    if (a && a.date) map[a.date] = Number(a.count) || 0;
  });
  var dates = Object.keys(map).sort();
  var last30 = dates.slice(-30);
  // pad to multiple of 7 for a clean grid
  while (last30.length % 7 !== 0) last30.unshift(null);
  var counts = last30.map(function (d) { return d ? (map[d] || 0) : 0; });
  var max = Math.max.apply(null, counts.concat([1]));
  var cells = last30.map(function (d, i) {
    var c = counts[i];
    var lvl = c === 0 ? 0 : Math.min(5, Math.ceil(c / max * 5));
    var tip = d ? d + " : " + c + " rep" + (c === 1 ? "" : "s") : "no data";
    return '<div class="heat-cell h' + lvl + '" data-tip="' + esc(tip) + '"></div>';
  }).join("");
  /* month labels: month of each week column, shown only when it changes */
  var MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  var mLabels = [], prev = null, col, row, d, m;
  for (col = 0; col < 7; col++) {
    m = null;
    for (row = 0; row * 7 + col < last30.length; row++) {
      d = last30[row * 7 + col];
      if (d && /^\d{4}-\d{2}-\d{2}/.test(d)) {
        m = MONTHS[parseInt(d.slice(5, 7), 10) - 1];
        break;
      }
    }
    mLabels.push(m !== prev ? (m || "") : "");
    if (m) prev = m;
  }
  var months = '<div class="heat-months">' + mLabels.map(function (lbl) {
    return '<div class="heat-month">' + lbl + "</div>";
  }).join("") + "</div>";
  var legend = '<div class="heat-legend"><span>LESS</span>' +
    [0, 1, 2, 3, 4, 5].map(function (l) {
      return '<div class="heat-cell h' + l + '"></div>';
    }).join("") + "<span>MORE</span></div>";
  return months + '<div class="heatmap">' + cells + "</div>" + legend;
}

/* Extract [{label, value}] pairs from arbitrary skill-gap payloads */
function pairsFrom(data) {
  var items = null;
  if (Array.isArray(data)) items = data;
  else if (data && typeof data === "object") {
    var key = Object.keys(data).filter(function (k) {
      return Array.isArray(data[k]) && data[k].length;
    })[0];
    if (key) items = data[key];
  }
  if (!items) return [];
  return items.map(function (it) {
    if (typeof it === "string") return { label: it, value: 1 };
    if (it && typeof it === "object") {
      var keys = Object.keys(it);
      var labelKey = keys.filter(function (k) { return /skill|name|label|gap/i.test(k); })[0] ||
                     keys.filter(function (k) { return typeof it[k] === "string"; })[0];
      var valKey = keys.filter(function (k) { return /score|count|freq|weight|priority/i.test(k); })[0] ||
                   keys.filter(function (k) { return typeof it[k] === "number"; })[0];
      if (!labelKey) return null;
      return { label: String(it[labelKey]), value: valKey ? Number(it[valKey]) || 0 : 1 };
    }
    return null;
  }).filter(Boolean).slice(0, 8);
}

function renderDashboard(view) {
  view.innerHTML = "";
  var h = el("h1", "page-title", "DASHBOARD");
  view.appendChild(h);

  var wrap = el("div", null, "");
  view.appendChild(wrap);
  wrap.innerHTML = '<div class="boot">LOADING<span class="blink">_</span></div>';

  apiGet(API.kpis).then(function (k) {
    KPIS = k || {};
    var html = kpiCards(KPIS);
    var zero = (Number(KPIS.applications) || 0) === 0 &&
               (Number(KPIS.interviews) || 0) === 0 &&
               (Number(KPIS.offers) || 0) === 0;
    if (zero) {
      html += '<div class="empty"><span class="big">NO APPLICATIONS YET</span>' +
        "Fresh install detected. Run the profile wizard to set up your profile.<br><br>" +
        '<button class="btn" id="dash-onboard">LAUNCH PROFILE WIZARD</button></div>';
    }
    html += '<h2 class="sec-title">APPLICATION FUNNEL</h2><div class="panel">' + funnelHtml(KPIS.funnel) + "</div>";
    html += '<h2 class="sec-title">FIT SCORE DISTRIBUTION</h2><div class="panel">' + fitDistHtml(KPIS) + "</div>";
    html += '<h2 class="sec-title">30-DAY ACTIVITY</h2><div class="panel">' + heatmapHtml(KPIS.activity_30d) + "</div>";
    html += '<h2 class="sec-title">TOP SKILL GAPS</h2><div class="panel" id="skillgaps"><div class="boot">ANALYZING<span class="blink">_</span></div></div>';
    wrap.innerHTML = html;

    var ob = document.getElementById("dash-onboard");
    if (ob) ob.addEventListener("click", function () { launchOnboarding(); });
    loadSkillGaps(document.getElementById("skillgaps"));
  }).catch(function (err) {
    if (isUnauthorized(err)) { handleUnauthorized(); return; }
    setServerStatus(false);
    wrap.innerHTML = '<div class="empty"><span class="big">KPI FEED DOWN</span>Could not load /api/kpis.</div>';
  });
}

function loadSkillGaps(slot) {
  if (!slot) return;
  var tool = findTool(function (t) { return /skill.?gap/i.test(String(t.id)) || /skill.?gap/i.test(String(t.name)); });
  if (!tool) {
    slot.innerHTML = '<div class="empty"><span class="big">NO SKILL-GAP TOOL</span>Tool not found in the catalog.</div>';
    return;
  }
  var action = findAction(tool, function (a) { return /analy/i.test(String(a.id)) || /analy/i.test(String(a.description || "")); });
  if (!action) {
    slot.innerHTML = '<div class="empty"><span class="big">NO ANALYZE ACTION</span>No analyze action on the skill-gap tool.</div>';
    return;
  }
  apiPost(API.run, { tool: tool.id, action: action.id, params: paramDefaults(action) })
    .then(function (res) {
      if (!res || res.ok === false) {
        slot.innerHTML = '<div class="empty"><span class="big">GAP ANALYSIS UNAVAILABLE</span>' +
          esc((res && res.error) || "server refused") + "</div>";
        return;
      }
      var pairs = pairsFrom(res.data);
      if (!pairs.length) {
        slot.innerHTML = '<div class="empty"><span class="big">NO GAPS FOUND</span>Nothing to report right now.</div>';
        return;
      }
      var max = Math.max.apply(null, pairs.map(function (p) { return p.value; }).concat([1]));
      slot.innerHTML = pairs.map(function (p) {
        return '<div class="bar-row"><div class="bar-label">' + esc(p.label) + '</div>' +
          '<div class="bar-track"><div class="bar-fill amber" style="width:' +
          Math.round(p.value / max * 100) + '%"></div></div>' +
          '<div class="bar-num">' + esc(p.value) + "</div></div>";
      }).join("");
    })
    .catch(function (err) {
      if (isUnauthorized(err)) { handleUnauthorized(); return; }
      var serverMsg = serverError(err);
      slot.innerHTML = '<div class="empty"><span class="big">GAP ANALYSIS UNAVAILABLE</span>' +
        esc(serverMsg || "Could not reach the server.") + "</div>";
    });
}

/* ================= group pages ================= */

function renderGroupPage(view, route) {
  view.innerHTML = "";
  view.appendChild(el("h1", "page-title", route.label));

  var list = ((TOOLS && TOOLS.tools) || []).filter(function (t) {
    return String(t.group || "").toUpperCase() === route.group;
  });
  if (!list.length) {
    view.appendChild(el("div", "empty",
      '<span class="big">NO TOOLS</span>No "' + esc(route.group) + '" tools in the catalog. ' +
      "Check that webui.py is serving /api/tools."));
    return;
  }
  var grid = el("div", "tool-grid", "");
  list.forEach(function (t) {
    var actions = t.actions || [];
    var card = el("div", "tool-card", "");
    card.appendChild(el("h3", null, esc(t.name || t.id)));
    card.appendChild(el("p", "desc", esc(t.description || "No description.")));
    card.appendChild(el("div", "actions-count", actions.length + " action" + (actions.length === 1 ? "" : "s")));
    var btn = el("button", "btn", "LAUNCH WORKFLOW");
    btn.addEventListener("click", function () { openWizard(t); });
    card.appendChild(btn);
    grid.appendChild(card);
  });
  view.appendChild(grid);
}

/* ================= wizard modal ================= */

var WIZ = null; // {tool, action, step, params, result, error, confirm}

function stepBar(steps, current) {
  return '<div class="steps">' + steps.map(function (s, i) {
    return '<div class="step' + (i === current ? " on" : "") + '">' + esc(s) + "</div>";
  }).join("") + "</div>";
}

function openWizard(tool) {
  var actions = tool.actions || [];
  if (!actions.length) {
    openModal('<button class="btn secondary close-x" id="m-close">X</button>' +
      '<h2>' + esc(tool.name || tool.id) + "</h2>" +
      '<div class="err">This tool has no actions in the catalog.</div>');
    document.getElementById("m-close").addEventListener("click", closeModal);
    return;
  }
  WIZ = { tool: tool, action: actions.length === 1 ? actions[0] : null, step: 0, params: {}, result: null, error: null, confirm: null };
  renderWizard();
}

function paramInput(p, val) {
  var type = String(p.type || "string").toLowerCase();
  var name = esc(p.name);
  var req = p.required ? ' <span class="req">*</span>' : "";
  var help = p.help ? '<span class="fhelp">' + esc(p.help) + "</span>" : "";
  var v = val !== undefined && val !== null ? val : (p.default !== undefined && p.default !== null ? p.default : "");
  var common = ' name="' + name + '" data-param="' + name + '"';

  if (/bool/.test(type)) {
    var checked = (v === true || v === "true") ? " checked" : "";
    return '<label class="field"><span class="flabel">' + name + req + "</span>" +
      '<input type="checkbox" class="big"' + common + checked + ">" + help + "</label>";
  }
  if (/int|float|number/.test(type)) {
    return '<label class="field"><span class="flabel">' + name + req + "</span>" +
      '<input type="number"' + common + ' value="' + esc(v) + '">' + help + "</label>";
  }
  var longText = /text|body|desc|letter|cover|message|notes|summary|achieve|keyword|bullet/i.test(p.name) ||
                 /long|paragraph|multi/i.test(p.help || "");
  if (/array|list/.test(type)) longText = true;
  if (longText) {
    return '<label class="field"><span class="flabel">' + name + req + "</span>" +
      '<textarea' + common + ">" + esc(v) + "</textarea>" + help + "</label>";
  }
  return '<label class="field"><span class="flabel">' + name + req + "</span>" +
    '<input type="text"' + common + ' value="' + esc(v) + '">' + help + "</label>";
}

function collectParams(action) {
  var out = {};
  (action.params || []).forEach(function (p) {
    var input = document.querySelector('[data-param="' + p.name + '"]');
    if (!input) return;
    var type = String(p.type || "string").toLowerCase();
    var raw = input.type === "checkbox" ? (input.checked ? "true" : "false") : input.value;
    if (/bool/.test(type)) { out[p.name] = (raw === "true"); return; }
    if (/int/.test(type)) { out[p.name] = raw === "" ? raw : parseInt(raw, 10); return; }
    if (/float|number/.test(type)) { out[p.name] = raw === "" ? raw : parseFloat(raw); return; }
    if (/array|list/.test(type)) {
      var s = String(raw).trim();
      if (!s) { out[p.name] = []; return; }
      try { out[p.name] = JSON.parse(s); return; } catch (e) { /* fall through */ }
      out[p.name] = s.split(/\n+/).map(function (x) { return x.trim(); }).filter(Boolean);
      return;
    }
    out[p.name] = raw;
  });
  return out;
}

function validateParams(action, params) {
  var missing = [];
  (action.params || []).forEach(function (p) {
    if (!p.required) return;
    var v = params[p.name];
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) missing.push(p.name);
  });
  return missing;
}

function renderWizard() {
  var t = WIZ.tool, a = WIZ.action;
  var head = '<button class="btn secondary close-x" id="m-close">X</button>' +
    "<h2>" + esc(t.name || t.id) + "</h2>" +
    '<div class="mdesc">' + esc(t.description || "") + "</div>";

  /* step 0: pick action */
  if (WIZ.step === 0 && !a) {
    openModal(head + stepBar(["ACTION", "PARAMS", "REVIEW", "RESULT"], 0) +
      '<div class="action-list">' + (t.actions || []).map(function (x, i) {
        return '<button class="action-pick" data-i="' + i + '"><span class="aid">' +
          esc(x.id) + '</span><span class="adesc">' + esc(x.description || "") + "</span></button>";
      }).join("") + "</div>" +
      '<div class="modal-foot"><button class="btn secondary" id="w-cancel">CANCEL</button></div>');
    bindWizardChrome();
    Array.prototype.forEach.call(document.querySelectorAll(".action-pick"), function (b) {
      b.addEventListener("click", function () {
        WIZ.action = t.actions[Number(b.getAttribute("data-i"))];
        WIZ.step = 1;
        renderWizard();
      });
    });
    return;
  }

  /* step 1: params form */
  if (WIZ.step === 1) {
    var fields = (a.params || []).map(function (p) { return paramInput(p, WIZ.params[p.name]); }).join("");
    if (!fields) fields = '<div class="empty">This action takes no parameters.</div>';
    openModal(head + stepBar(["ACTION", "PARAMS", "REVIEW", "RESULT"], 1) +
      '<div class="mdesc">ACTION: <b>' + esc(a.id) + "</b> — " + esc(a.description || "") + "</div>" +
      '<div id="w-err"></div>' + fields +
      '<div class="modal-foot">' +
      '<button class="btn secondary" id="w-back">' + ((t.actions || []).length > 1 ? "BACK" : "CANCEL") + "</button>" +
      '<button class="btn" id="w-next">REVIEW [>]</button></div>');
    bindWizardChrome();
    document.getElementById("w-back").addEventListener("click", function () {
      WIZ.params = collectParams(a);
      if ((t.actions || []).length > 1) { WIZ.action = null; WIZ.step = 0; renderWizard(); }
      else closeModal();
    });
    document.getElementById("w-next").addEventListener("click", function () {
      var params = collectParams(a);
      var missing = validateParams(a, params);
      if (missing.length) {
        document.getElementById("w-err").innerHTML =
          '<div class="err">Missing required parameter(s): ' + esc(missing.join(", ")) + "</div>";
        return;
      }
      WIZ.params = params;
      WIZ.step = 2;
      WIZ.error = null;
      renderWizard();
    });
    return;
  }

  /* step 2: review + run */
  if (WIZ.step === 2) {
    var rows = (a.params || []).map(function (p) {
      var v = WIZ.params[p.name];
      return "<tr><td><b>" + esc(p.name) + "</b>" + (p.required ? ' <span class="req">*</span>' : "") +
        "</td><td>" + esc(typeof v === "object" ? JSON.stringify(v) : String(v)) + "</td></tr>";
    }).join("");
    var table = rows ? '<div class="table-wrap"><table class="data"><tr><th>PARAM</th><th>VALUE</th></tr>' + rows + "</table></div>"
                     : '<div class="empty">No parameters.</div>';
    openModal(head + stepBar(["ACTION", "PARAMS", "REVIEW", "RESULT"], 2) +
      '<div class="mdesc">ACTION: <b>' + esc(a.id) + "</b> — confirm the exact params sent to the server.</div>" +
      '<div id="w-err">' + (WIZ.error ? '<div class="err">' + esc(WIZ.error) + "</div>" : "") + "</div>" +
      table +
      '<div class="modal-foot">' +
      '<button class="btn secondary" id="w-back">[&lt;] EDIT PARAMS</button>' +
      '<button class="btn" id="w-run">RUN</button></div>');
    bindWizardChrome();
    document.getElementById("w-back").addEventListener("click", function () { WIZ.step = 1; renderWizard(); });
    document.getElementById("w-run").addEventListener("click", function () { runWizardAction(false, null); });
    return;
  }

  /* step 3: results */
  if (WIZ.step === 3) {
    openModal(head + stepBar(["ACTION", "PARAMS", "REVIEW", "RESULT"], 3) +
      '<div id="w-result"></div>' +
      '<div class="modal-foot">' +
      '<button class="btn secondary" id="w-again">RUN AGAIN</button>' +
      '<button class="btn secondary" id="w-close">CLOSE</button></div>');
    bindWizardChrome();
    var slot = document.getElementById("w-result");
    renderResult(slot, WIZ.result);
    document.getElementById("w-again").addEventListener("click", function () { WIZ.step = 1; renderWizard(); });
    document.getElementById("w-close").addEventListener("click", closeModal);
    return;
  }
}

function bindWizardChrome() {
  var c = document.getElementById("m-close");
  if (c) c.addEventListener("click", closeModal);
  var x = document.getElementById("w-cancel");
  if (x) x.addEventListener("click", closeModal);
}

function runWizardAction(confirmed, previewHash) {
  var body = { tool: WIZ.tool.id, action: WIZ.action.id, params: WIZ.params };
  if (confirmed) { body.params = Object.assign({}, WIZ.params, { confirmed: true, preview_hash: previewHash }); }

  var btn = document.getElementById("w-run");
  if (btn) { btn.disabled = true; btn.textContent = "RUNNING..."; }

  apiPost(API.run, body).then(function (res) {
    if (!res) { WIZ.error = "Empty response from server."; WIZ.step = 2; renderWizard(); return; }
    if (res.ok === true) {
      WIZ.result = res.data;
      WIZ.error = null;
      WIZ.step = 3;
      renderWizard();
      return;
    }
    if (res.needs_confirm) {
      /* do NOT auto-confirm — open the confirm modal */
      WIZ.confirm = { preview: res.preview, preview_hash: res.preview_hash };
      openConfirm();
      return;
    }
    WIZ.error = res.error || "Unknown server error.";
    WIZ.step = 2;
    renderWizard();
  }).catch(function (err) {
    if (isUnauthorized(err)) { handleUnauthorized(); return; }
    var serverMsg = serverError(err);
    if (serverMsg) {
      /* server answered 4xx with a JSON error — show its own message */
      WIZ.error = serverMsg;
      WIZ.step = 2;
      renderWizard();
      return;
    }
    setServerStatus(false);
    WIZ.error = "Network error: could not reach the server.";
    WIZ.step = 2;
    renderWizard();
  });
}

/* ================= confirm flow ================= */

function openConfirm() {
  var c = WIZ.confirm;
  openModal(
    '<button class="btn secondary close-x" id="m-close">X</button>' +
    "<h2>CONFIRM EXECUTION</h2>" +
    '<div class="mdesc">The server requires explicit approval before running <b>' +
    esc(WIZ.action.id) + "</b>. Review the exact text below.</div>" +
    '<div class="confirm-preview">' + esc(c.preview || "(no preview text returned)") + "</div>" +
    '<label class="confirm-row"><input type="checkbox" class="big" id="cf-check">' +
    '<span class="clabel">I approve this exact text — execute</span></label>' +
    '<div id="cf-err"></div>' +
    '<div class="modal-foot">' +
    '<button class="btn secondary" id="cf-cancel">CANCEL</button>' +
    '<button class="btn danger" id="cf-go" disabled>EXECUTE</button></div>'
  );
  var check = document.getElementById("cf-check");
  var go = document.getElementById("cf-go");
  document.getElementById("m-close").addEventListener("click", closeModal);
  document.getElementById("cf-cancel").addEventListener("click", function () {
    /* back to review, nothing executed */
    WIZ.confirm = null;
    WIZ.step = 2;
    renderWizard();
  });
  check.addEventListener("change", function () { go.disabled = !check.checked; });
  go.addEventListener("click", function () {
    if (!check.checked) return; /* belt and suspenders */
    go.disabled = true; go.textContent = "EXECUTING...";
    apiPost(API.run, {
      tool: WIZ.tool.id,
      action: WIZ.action.id,
      params: Object.assign({}, WIZ.params, { confirmed: true, preview_hash: c.preview_hash })
    }).then(function (res) {
      if (res && res.ok === true) {
        WIZ.result = res.data;
        WIZ.confirm = null;
        WIZ.error = null;
        WIZ.step = 3;
        renderWizard();
      } else if (res && res.needs_confirm) {
        document.getElementById("cf-err").innerHTML =
          '<div class="err">Server asked for confirmation again. Check the new preview.</div>';
        WIZ.confirm = { preview: res.preview, preview_hash: res.preview_hash };
        go.disabled = false; go.textContent = "EXECUTE";
      } else {
        WIZ.error = (res && res.error) || "Execution failed.";
        WIZ.confirm = null;
        WIZ.step = 2;
        renderWizard();
      }
    }).catch(function (err) {
      if (isUnauthorized(err)) { handleUnauthorized(); return; }
      var serverMsg = serverError(err);
      WIZ.error = serverMsg || "Network error during confirmed execution.";
      if (!serverMsg) setServerStatus(false);
      WIZ.confirm = null;
      WIZ.step = 2;
      renderWizard();
    });
  });
}

/* ================= result rendering ================= */

function looksLikeSvg(s) {
  return typeof s === "string" && /^\s*<svg[\s>]/i.test(s);
}
function looksLikeDiff(s) {
  return typeof s === "string" &&
    /(^|\n)(diff --git|--- |\+\+\+ |@@ |[+-][^+-])/m.test(s);
}

function looksLikeMarkdown(s) {
  /* small markdown subset: ## headings, **bold**, - list items */
  return typeof s === "string" &&
    /(^|\n)\s*#{1,3}\s|\*\*[^*\n]+\*\*|(^|\n)\s*[-*]\s/m.test(s);
}

function mdInline(s) {
  /* s is already HTML-escaped */
  return s.replace(/\*\*([^*<>]+)\*\*/g, "<strong>$1</strong>");
}

function markdownHtml(s) {
  /* convert the small markdown subset to HTML; escape first, then markup */
  var lines = esc(s).split("\n");
  var out = [];
  var inList = false;
  lines.forEach(function (ln) {
    var h = /^\s*#{1,3}\s+(.*)$/.exec(ln);
    var li = /^\s*[-*]\s+(.*)$/.exec(ln);
    if (h) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push("<h3>" + mdInline(h[1]) + "</h3>");
    } else if (li) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + mdInline(li[1]) + "</li>");
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      if (/^\s*$/.test(ln)) return;
      out.push("<p>" + mdInline(ln) + "</p>");
    }
  });
  if (inList) out.push("</ul>");
  return '<div class="md">' + out.join("") + "</div>";
}

function diffHtml(s) {
  var lines = esc(s).split("\n").map(function (ln) {
    var cls = "";
    if (/^\+[^+]/.test(ln) || /^\+\+\+ /.test(ln)) cls = "dadd";
    else if (/^-[^-]/.test(ln) || /^--- /.test(ln)) cls = "ddel";
    else if (/^@@/.test(ln)) cls = "dhunk";
    return cls ? '<span class="' + cls + '">' + ln + "</span>" : ln;
  }).join("\n");
  return '<pre class="dump diff">' + lines + "</pre>";
}

function renderString(container, s) {
  if (looksLikeSvg(s)) {
    var w = el("div", "radar-wrap", "");
    w.innerHTML = s; /* server-generated radar chart */
    container.appendChild(w);
    return;
  }
  if (looksLikeMarkdown(s)) {
    /* markdown reports (e.g. jd_decoder) render as HTML, not raw text */
    var m = el("div", null, "");
    m.innerHTML = markdownHtml(s);
    container.appendChild(m);
    return;
  }
  if (looksLikeDiff(s)) {
    var d = el("div", null, "");
    d.innerHTML = diffHtml(s);
    container.appendChild(d);
    return;
  }
  container.appendChild(el("pre", "dump", esc(s)));
}

function renderArrayOfObjects(container, rows) {
  var cols = [];
  rows.forEach(function (r) {
    Object.keys(r || {}).forEach(function (k) { if (cols.indexOf(k) < 0) cols.push(k); });
  });
  if (!cols.length) { container.appendChild(el("pre", "dump", esc(JSON.stringify(rows, null, 2)))); return; }
  var t = el("table", "data", "");
  var thead = "<tr>" + cols.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr>";
  var tb = rows.map(function (r) {
    return "<tr>" + cols.map(function (c) {
      var v = r ? r[c] : undefined;
      return "<td>" + esc(v === null || v === undefined ? "" : (typeof v === "object" ? JSON.stringify(v) : String(v))) + "</td>";
    }).join("") + "</tr>";
  }).join("");
  t.innerHTML = thead + tb;
  var tw = el("div", "table-wrap", "");
  tw.appendChild(t);
  container.appendChild(tw);
}

function renderResult(container, data) {
  container.innerHTML = "";
  container.appendChild(el("h2", "sec-title", "RESULT"));
  var block = el("div", "result-block", "");
  container.appendChild(block);

  if (data === null || data === undefined) {
    block.appendChild(el("div", "empty", "Action completed with no output."));
    return;
  }
  if (typeof data === "string") { renderString(block, data); return; }
  if (Array.isArray(data)) {
    if (data.length && data.every(function (x) { return x && typeof x === "object" && !Array.isArray(x); })) {
      renderArrayOfObjects(block, data);
    } else if (data.length) {
      block.appendChild(el("pre", "dump", esc(data.map(String).join("\n"))));
    } else {
      block.appendChild(el("div", "empty", "Empty list returned."));
    }
    return;
  }
  if (typeof data === "object") {
    /* check for embedded radar SVG / diff strings first */
    var embedded = false;
    Object.keys(data).forEach(function (k) {
      var v = data[k];
      if (typeof v === "string" && (looksLikeSvg(v) || looksLikeDiff(v))) {
        block.appendChild(el("h2", "sec-title", esc(k)));
        renderString(block, v);
        embedded = true;
      }
    });
    var rest = {};
    Object.keys(data).forEach(function (k) {
      var v = data[k];
      if (typeof v === "string" && (looksLikeSvg(v) || looksLikeDiff(v))) return;
      rest[k] = v;
    });
    var keys = Object.keys(rest);
    if (!keys.length && embedded) return;
    /* table-able list values get tables */
    var rendered = false;
    keys.forEach(function (k) {
      var v = rest[k];
      if (Array.isArray(v) && v.length && v.every(function (x) { return x && typeof x === "object" && !Array.isArray(x); })) {
        block.appendChild(el("h2", "sec-title", esc(k)));
        renderArrayOfObjects(block, v);
        delete rest[k];
        rendered = true;
      }
    });
    var rkeys = Object.keys(rest);
    if (rkeys.length) {
      var dl = el("dl", "kv", "");
      rkeys.forEach(function (k) {
        var v = rest[k];
        dl.appendChild(el("dt", null, esc(k)));
        var val = (v === null || v === undefined) ? "" : (typeof v === "object" ? JSON.stringify(v, null, 2) : String(v));
        var dd = el("dd", null, "");
        if (typeof v === "string" && looksLikeMarkdown(v)) {
          dd.innerHTML = markdownHtml(val);
        } else {
          dd.innerHTML = esc(val);
        }
        dl.appendChild(dd);
      });
      block.appendChild(dl);
    } else if (!rendered && !embedded) {
      block.appendChild(el("pre", "dump", esc(JSON.stringify(data, null, 2))));
    }
    return;
  }
  block.appendChild(el("pre", "dump", esc(String(data))));
}

/* ================= onboarding ================= */

function launchOnboarding() {
  var tool = toolForId("wizard") ||
             findTool(function (t) { return /wizard|onboard/i.test(String(t.id)) || /wizard|onboard/i.test(String(t.name)); });
  if (!tool) {
    openModal('<button class="btn secondary close-x" id="m-close">X</button>' +
      "<h2>ONBOARDING</h2>" +
      '<div class="err">No wizard/onboarding tool found in the tool catalog.</div>');
    document.getElementById("m-close").addEventListener("click", closeModal);
    return;
  }
  openWizard(tool);
}

/* ================= boot ================= */

function boot(retry) {
  setServerStatus(true);
  hideBanner();
  var pill = document.getElementById("status-pill");
  pill.classList.remove("status-online", "status-offline");
  pill.classList.add("status-unknown");
  document.getElementById("status-text").textContent = "PROBING";

  return Promise.all([
    apiGet(API.tools).catch(function (e) { return { _err: e }; }),
    apiGet(API.kpis).catch(function (e) { return { _err: e }; })
  ]).then(function (pair) {
    var tools = pair[0], kpis = pair[1];
    if (isUnauthorized(tools._err) || isUnauthorized(kpis._err)) {
      handleUnauthorized();
      return;
    }
    if (tools._err || kpis._err || !tools.tools) {
      setServerStatus(false);
      render();
      return;
    }
    TOOLS = tools;
    KPIS = kpis;
    setServerStatus(true);
    render();
  });
}
