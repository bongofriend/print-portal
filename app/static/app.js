"use strict";
/* Print Portal front-end. Plain JS, no build step, no external dependencies. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") msg = body.detail;
      else if (Array.isArray(body.detail)) msg = body.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ");
    } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", isError);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), isError ? 6000 : 3000);
}

const fmtSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const fmtTime = (t) => {
  const d = new Date(t * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
               : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
};
const humanReason = (r) => r.replace(/-(report|warning|error)$/, "").replace(/-/g, " ");

/* ------------------------------------------------------------------ navigation */
let currentView = "dashboard";
function show(view) {
  if (currentView === "annotate" && view !== "annotate" && editor.dirty &&
      !confirm("You have unsaved annotations. Leave without saving?")) return;
  currentView = view;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  refresh();
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* ------------------------------------------------------------------ status */
function stateDot(p) {
  if (!p.online || p.state === "stopped" || p.state === "unknown") return "err";
  if (p.state_reasons.some((r) => r.endsWith("-error"))) return "err";
  if (p.state_reasons.length) return "warn";
  return "ok";
}

async function loadStatus() {
  let s;
  try { s = await api("/api/status"); } catch (e) {
    $("#printer-pill").innerHTML = `<span class="dot err"></span> Portal unreachable`;
    return;
  }
  const p = s.printer, sc = s.scanner;
  const dot = stateDot(p);
  const label = !p.online ? "Offline" : p.state.charAt(0).toUpperCase() + p.state.slice(1);
  $("#printer-pill").innerHTML = `<span class="dot ${dot}"></span> ${esc(p.name)} · ${esc(label)}`;

  $("#printer-status").innerHTML = `
    <div class="big"><span class="dot ${dot}"></span>${esc(label)}</div>
    ${p.message ? `<p class="muted small">${esc(p.message)}</p>` : ""}
    <dl>
      <dt>Queue</dt><dd>${esc(p.name)}</dd>
      ${p.model ? `<dt>Model</dt><dd>${esc(p.model)}</dd>` : ""}
      ${p.location ? `<dt>Location</dt><dd>${esc(p.location)}</dd>` : ""}
      <dt>Accepting jobs</dt><dd>${p.accepting_jobs ? "Yes" : "No"}</dd>
      <dt>Jobs in queue</dt><dd>${p.queued_jobs}</dd>
      <dt>Backend</dt><dd>${esc(p.backend)}</dd>
    </dl>
    ${p.state_reasons.length ? `<div class="reasons">${p.state_reasons.map((r) =>
      `<span class="tag ${r.endsWith("-error") ? "err" : "warn"}">${esc(humanReason(r))}</span>`).join("")}</div>` : ""}`;

  $("#supplies").innerHTML = p.markers.length ? p.markers.map((m) => {
    const lvl = m.level;
    const low = lvl !== null && lvl < 15;
    return `<div class="supply">
      <div class="supply-head"><span>${esc(m.name)}</span>
        <span class="${low ? "error" : "muted"}" style="margin:0">${lvl === null ? "unknown" : lvl + "%"}</span></div>
      <div class="bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${lvl ?? ""}" aria-label="${esc(m.name)}">
        <span style="width:${lvl ?? 0}%;background:${esc(m.color)}"></span></div>
    </div>`;
  }).join("") : `<p class="muted">The printer does not report supply levels${p.backend === "cups" ? " (CUPS updates them after the first job)" : ""}.</p>`;

  $("#scanner-status").innerHTML = `
    <div class="big"><span class="dot ${sc.available ? "ok" : "err"}"></span>
      ${sc.busy ? "Scanning…" : sc.available ? "Ready" : "Unavailable"}</div>
    ${sc.message ? `<p class="muted small">${esc(sc.message)}</p>` : ""}
    <dl><dt>Device</dt><dd>${esc(sc.device || "auto-detect")}</dd><dt>Backend</dt><dd>${esc(sc.backend)}</dd></dl>`;
}

/* ------------------------------------------------------------------ jobs */
async function loadJobs() {
  const jobs = await api("/api/jobs");
  const el = $("#jobs");
  if (!jobs.length) { el.innerHTML = `<p class="empty">No print jobs yet.</p>`; return; }
  el.innerHTML = `<table><thead><tr><th>Document</th><th class="hide-sm">Job</th><th class="hide-sm">Pages</th>
    <th class="hide-sm">Submitted</th><th>State</th><th></th></tr></thead><tbody>
    ${jobs.slice(0, 50).map((j) => `<tr>
      <td class="name">${esc(j.title)}<div class="muted small">${j.options.copies}× · ${esc(j.options.sides.replace(/-/g, " "))} · ${esc(j.options.color)}</div></td>
      <td class="hide-sm muted">${esc(j.id)}</td>
      <td class="hide-sm">${j.options.page_ranges ? esc(j.options.page_ranges) : j.pages}</td>
      <td class="hide-sm">${fmtTime(j.submitted)}</td>
      <td><span class="state ${esc(j.state)}">${j.state === "printing" ? '<span class="spinner"></span> ' : ""}${esc(j.state)}</span></td>
      <td class="actions">${["pending", "printing"].includes(j.state)
        ? `<button class="btn small danger" data-cancel="${esc(j.id)}">Cancel</button>` : ""}</td>
    </tr>`).join("")}</tbody></table>`;
}
$("#jobs").addEventListener("click", async (e) => {
  const id = e.target.dataset.cancel;
  if (!id) return;
  try { await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }); toast("Job canceled"); }
  catch (err) { toast(err.message, true); }
  loadJobs();
});
$("#refresh-jobs").addEventListener("click", () => { loadJobs(); loadStatus(); });

/* ------------------------------------------------------------------ documents */
let documents = [];
async function loadDocuments() {
  documents = await api("/api/documents");
  const el = $("#documents");
  if (!documents.length) { el.innerHTML = `<p class="empty">No documents. Upload one above or import a scan.</p>`; return; }
  el.innerHTML = `<table><thead><tr><th>Name</th><th class="hide-sm">Pages</th><th class="hide-sm">Size</th>
    <th class="hide-sm">Added</th><th></th></tr></thead><tbody>
    ${documents.map((d) => `<tr>
      <td class="name">${esc(d.name)}
        ${d.has_annotations ? '<span class="tag ok">annotated</span>' : ""}
        ${d.source === "scan" ? '<span class="tag">scan</span>' : ""}</td>
      <td class="hide-sm">${d.pages}</td><td class="hide-sm">${fmtSize(d.size)}</td>
      <td class="hide-sm">${fmtTime(d.created)}</td>
      <td class="actions">
        <button class="btn small primary" data-act="print" data-id="${d.id}">Print</button>
        <button class="btn small" data-act="annotate" data-id="${d.id}">Annotate</button>
        <a class="btn small" href="/api/documents/${d.id}/download" download>Download</a>
        <button class="btn small danger" data-act="delete" data-id="${d.id}" aria-label="Delete ${esc(d.name)}">Delete</button>
      </td></tr>`).join("")}</tbody></table>`;
}
$("#documents").addEventListener("click", async (e) => {
  const { act, id } = e.target.dataset;
  if (!act) return;
  const doc = documents.find((d) => d.id === id);
  if (act === "print") openPrintDialog(doc);
  else if (act === "annotate") openEditor(id);
  else if (act === "delete") {
    if (!confirm(`Delete "${doc.name}"?`)) return;
    try { await api(`/api/documents/${id}`, { method: "DELETE" }); toast("Deleted"); }
    catch (err) { toast(err.message, true); }
    if (editor.docId === id) { editor.docId = null; $("#annotate-tab").hidden = true; }
    loadDocuments();
  }
});

async function uploadFiles(files) {
  const prog = $("#upload-progress");
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "progress-item";
    row.innerHTML = `<span class="spinner"></span> Uploading ${esc(file.name)}…`;
    prog.append(row);
    const fd = new FormData();
    fd.append("file", file);
    try {
      await api("/api/documents", { method: "POST", body: fd });
      row.remove();
      toast(`Uploaded ${file.name}`);
    } catch (err) {
      row.innerHTML = `<span class="error">${esc(file.name)}: ${esc(err.message)}</span>`;
      setTimeout(() => row.remove(), 8000);
    }
    loadDocuments();
  }
}
const dz = $("#dropzone");
$("#file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

/* ------------------------------------------------------------------ print dialog */
let printDoc = null;
function openPrintDialog(doc) {
  printDoc = doc;
  $("#print-doc-name").textContent = `“${doc.name}”`;
  $("#print-ann-row").hidden = !doc.has_annotations;
  $("#print-error").textContent = "";
  $("#print-dialog").showModal();
}
$("#print-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "print") return;
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    document_id: printDoc.id,
    include_annotations: f.get("include_annotations") === "on",
    options: {
      copies: Number(f.get("copies")), sides: f.get("sides"), color: f.get("color"), media: f.get("media"),
      page_ranges: f.get("page_ranges").trim(), orientation: f.get("orientation"), fit_to_page: f.get("fit_to_page") === "on",
    },
  };
  const btn = $("#print-submit");
  btn.disabled = true;
  try {
    const job = await api("/api/print", { method: "POST", json: body });
    $("#print-dialog").close();
    toast(`Sent to printer (job ${job.id})`);
    loadJobs();
  } catch (err) {
    $("#print-error").textContent = err.message;
  } finally { btn.disabled = false; }
});

/* ------------------------------------------------------------------ scanning */
let scanPoll = null;
async function loadScans() {
  const scans = await api("/api/scans");
  const el = $("#scans");
  const active = scans.some((s) => s.state === "scanning");
  $("#scan-btn").disabled = active;
  $("#scan-btn").innerHTML = active ? '<span class="spinner"></span> Scanning…' : "Scan";
  clearTimeout(scanPoll);
  if (active) scanPoll = setTimeout(loadScans, 1500);
  if (!scans.length) { el.innerHTML = `<p class="empty">No scans yet.</p>`; return; }
  el.innerHTML = `<table><thead><tr><th>Scan</th><th class="hide-sm">Settings</th><th>State</th><th></th></tr></thead><tbody>
    ${scans.map((s) => `<tr>
      <td class="name">${esc(s.name)}<div class="muted small">${s.state === "done" ? `${s.pages} page${s.pages === 1 ? "" : "s"} · ${fmtSize(s.size)}` : ""}</div></td>
      <td class="hide-sm muted small">${esc(s.options.source)} · ${esc(s.options.mode)} · ${s.options.resolution} dpi · ${esc(s.options.format.toUpperCase())}</td>
      <td><span class="state ${esc(s.state)}">${s.state === "scanning" ? '<span class="spinner"></span> ' : ""}${esc(s.state)}</span>
        ${s.error ? `<div class="error small">${esc(s.error)}</div>` : ""}</td>
      <td class="actions">${s.state === "done" ? `
        <a class="btn small primary" href="/api/scans/${s.id}/download" download>Download</a>
        <a class="btn small" href="/api/scans/${s.id}/download?inline=true" target="_blank" rel="noopener">View</a>
        <button class="btn small" data-act="import" data-id="${s.id}" title="Copy into Documents to annotate or print">To documents</button>` : ""}
        ${s.state !== "scanning" ? `<button class="btn small danger" data-act="delete" data-id="${s.id}">Delete</button>` : ""}
      </td></tr>`).join("")}</tbody></table>`;
}
$("#scan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/scans", { method: "POST", json: {
      source: f.get("source"), mode: f.get("mode"), resolution: Number(f.get("resolution")), format: f.get("format"),
    } });
    toast("Scan started");
  } catch (err) { toast(err.message, true); }
  loadScans();
});
$("#scans").addEventListener("click", async (e) => {
  const { act, id } = e.target.dataset;
  if (!act) return;
  try {
    if (act === "import") {
      const d = await api(`/api/scans/${id}/to-document`, { method: "POST" });
      toast(`Added “${d.name}” to Documents`);
    } else if (act === "delete") {
      if (!confirm("Delete this scan?")) return;
      await api(`/api/scans/${id}`, { method: "DELETE" });
    }
  } catch (err) { toast(err.message, true); }
  loadScans();
});

/* ------------------------------------------------------------------ annotation editor */
const SIZES = { s: { stroke: 0.002, text: 0.015 }, m: { stroke: 0.004, text: 0.022 }, l: { stroke: 0.008, text: 0.032 } };
const FONT = '"DejaVu Sans", Helvetica, Arial, sans-serif';
const BASE_WIDTH = 850;

const editor = {
  docId: null, doc: null, anns: [], undoStack: [], redoStack: [], tool: "pen", zoom: 1, dirty: false, pages: [],
};

function setDirty(d) {
  editor.dirty = d;
  $("#save-state").textContent = d ? "Unsaved changes" : "Saved";
}
function pushUndo() {
  editor.undoStack.push(JSON.stringify(editor.anns));
  if (editor.undoStack.length > 200) editor.undoStack.shift();
  editor.redoStack = [];
}

async function openEditor(docId) {
  if (editor.dirty && editor.docId !== docId && !confirm("Discard unsaved annotations on the current document?")) return;
  const [doc, ann] = await Promise.all([api(`/api/documents/${docId}`), api(`/api/documents/${docId}/annotations`)]);
  Object.assign(editor, { docId, doc, anns: ann.annotations, undoStack: [], redoStack: [] });
  setDirty(false);
  $("#ann-title").textContent = doc.name;
  $("#annotate-tab").hidden = false;
  editor.dirty = false;
  show("annotate");
  buildPages();
}

function buildPages() {
  const container = $("#pages");
  container.innerHTML = "";
  const cssW = Math.round(BASE_WIDTH * editor.zoom);
  const pxW = Math.min(3000, Math.round(cssW * (window.devicePixelRatio || 1)));
  editor.pages = editor.doc.page_sizes.map(([w, h], i) => {
    const el = document.createElement("div");
    el.className = "page";
    el.dataset.tool = editor.tool;
    el.style.width = `${cssW}px`;
    el.style.aspectRatio = `${w} / ${h}`;
    el.innerHTML = `<img alt="Page ${i + 1}" loading="lazy" src="/api/documents/${editor.docId}/pages/${i}?width=${pxW}">
      <canvas></canvas><span class="page-no">${i + 1} / ${editor.doc.pages}</span>`;
    container.append(el);
    const page = { index: i, el, canvas: $("canvas", el), draft: null };
    attachPointer(page);
    return page;
  });
  $("#zoom-label").textContent = `${Math.round(editor.zoom * 100)}%`;
  requestAnimationFrame(redrawAll);
}

function sizeCanvas(page) {
  const r = page.el.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  if (page.canvas.width !== w || page.canvas.height !== h) { page.canvas.width = w; page.canvas.height = h; }
  return { W: w, H: h };
}

function drawAnn(ctx, a, W, H) {
  ctx.save();
  if (a.type === "pen") {
    ctx.strokeStyle = a.color; ctx.lineWidth = Math.max(a.width * W, 1);
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(a.points[0][0] * W, a.points[0][1] * H);
    if (a.points.length === 1) ctx.lineTo(a.points[0][0] * W + 0.1, a.points[0][1] * H);
    for (const [x, y] of a.points.slice(1)) ctx.lineTo(x * W, y * H);
    ctx.stroke();
  } else if (a.type === "highlight") {
    ctx.globalCompositeOperation = "multiply"; ctx.globalAlpha = 0.4; ctx.fillStyle = a.color;
    ctx.fillRect(a.x * W, a.y * H, a.w * W, a.h * H);
  } else if (a.type === "whiteout") {
    ctx.fillStyle = "#ffffff"; ctx.fillRect(a.x * W, a.y * H, a.w * W, a.h * H);
  } else if (a.type === "rect") {
    ctx.strokeStyle = a.color; ctx.lineWidth = Math.max(a.width * W, 1);
    ctx.strokeRect(a.x * W, a.y * H, a.w * W, a.h * H);
  } else if (a.type === "text") {
    const size = a.size * W;
    ctx.font = `${size}px ${FONT}`; ctx.fillStyle = a.color; ctx.textBaseline = "alphabetic";
    const asc = ctx.measureText("Hg").fontBoundingBoxAscent ?? size * 0.93;
    a.text.split("\n").forEach((line, i) => ctx.fillText(line, a.x * W, a.y * H + asc + i * size * 1.2));
  }
  ctx.restore();
}

function redraw(page) {
  const { W, H } = sizeCanvas(page);
  const ctx = page.canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  for (const a of editor.anns) if (a.page === page.index) drawAnn(ctx, a, W, H);
  if (page.draft) drawAnn(ctx, page.draft, W, H);
}
const redrawAll = () => editor.pages.forEach(redraw);
window.addEventListener("resize", () => currentView === "annotate" && redrawAll());

function hitTest(a, x, y, page) {
  const tol = 0.01;
  if (a.type === "pen") {
    return a.points.some(([px, py], i) => {
      const [qx, qy] = a.points[i + 1] || [px, py];
      const dx = qx - px, dy = qy - py, len2 = dx * dx + dy * dy;
      const t = len2 ? Math.max(0, Math.min(1, ((x - px) * dx + (y - py) * dy) / len2)) : 0;
      return Math.hypot(x - (px + t * dx), y - (py + t * dy)) < tol + a.width;
    });
  }
  if (a.type === "text") {
    const ctx = page.canvas.getContext("2d");
    const W = page.canvas.width, H = page.canvas.height;
    ctx.font = `${a.size * W}px ${FONT}`;
    const lines = a.text.split("\n");
    const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) / W;
    const h = (lines.length * a.size * 1.2 * W) / H;
    return x >= a.x - tol && x <= a.x + w + tol && y >= a.y - tol && y <= a.y + h + tol;
  }
  return x >= a.x - tol && x <= a.x + a.w + tol && y >= a.y - tol && y <= a.y + a.h + tol;
}

function attachPointer(page) {
  const pos = (e) => {
    const r = page.canvas.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
  };
  let start = null;
  page.canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const [x, y] = pos(e);
    const color = $("#ann-color").value, size = SIZES[$("#ann-size").value];
    if (editor.tool === "eraser") {
      const idx = editor.anns.findLastIndex((a) => a.page === page.index && hitTest(a, x, y, page));
      if (idx >= 0) { pushUndo(); editor.anns.splice(idx, 1); setDirty(true); redraw(page); }
      return;
    }
    if (editor.tool === "text") { e.preventDefault(); openTextInput(page, x, y, color, size.text); return; }
    page.canvas.setPointerCapture(e.pointerId);
    start = [x, y];
    if (editor.tool === "pen") page.draft = { type: "pen", page: page.index, color, width: size.stroke, points: [[x, y]] };
    else page.draft = { type: editor.tool, page: page.index, color: editor.tool === "highlight" && color === "#d32f2f" ? "#ffeb3b" : color,
                        width: size.stroke, x, y, w: 0, h: 0 };
  });
  page.canvas.addEventListener("pointermove", (e) => {
    if (!page.draft) return;
    const [x, y] = pos(e);
    if (page.draft.type === "pen") {
      const last = page.draft.points.at(-1);
      if (Math.hypot(x - last[0], y - last[1]) > 0.0015) page.draft.points.push([x, y]);
    } else {
      Object.assign(page.draft, { x: Math.min(x, start[0]), y: Math.min(y, start[1]), w: Math.abs(x - start[0]), h: Math.abs(y - start[1]) });
    }
    redraw(page);
  });
  const finish = () => {
    const d = page.draft;
    page.draft = null;
    if (!d) return;
    const round = (v) => Math.round(v * 10000) / 10000;
    if (d.type === "pen") d.points = d.points.map(([x, y]) => [round(x), round(y)]);
    else if (d.w < 0.003 || d.h < 0.003) { redraw(page); return; }  // ignore accidental clicks
    pushUndo(); editor.anns.push(d); setDirty(true); redraw(page);
  };
  page.canvas.addEventListener("pointerup", finish);
  page.canvas.addEventListener("pointercancel", () => { page.draft = null; redraw(page); });
}

function openTextInput(page, x, y, color, size) {
  $(".text-input")?.blur();
  const ta = document.createElement("textarea");
  ta.className = "text-input";
  const r = page.el.getBoundingClientRect();
  Object.assign(ta.style, { left: `${x * 100}%`, top: `${y * 100}%`, color, fontSize: `${size * r.width}px` });
  ta.placeholder = "Type, Enter to place";
  page.el.append(ta);
  setTimeout(() => ta.focus());
  let done = false;
  const commit = (keep) => {
    if (done) return;
    done = true;
    const text = ta.value.replace(/\s+$/, "");
    ta.remove();
    if (keep && text) {
      pushUndo();
      // textarea has 1px border + 2px/4px padding; place the text where it was typed
      const ox = 5 / r.width, oy = 3 / r.height;
      editor.anns.push({ type: "text", page: page.index, color, size, x: Math.max(0, x + ox), y: Math.max(0, y + oy), text });
      setDirty(true); redraw(page);
    }
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); commit(true); }
    if (e.key === "Escape") commit(false);
    e.stopPropagation();
  });
  ta.addEventListener("blur", () => commit(true));
}

function setTool(tool) {
  editor.tool = tool;
  $$(".tool").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  editor.pages.forEach((p) => { p.el.dataset.tool = tool; });
}
$$(".tool").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));

function undo() {
  if (!editor.undoStack.length) return;
  editor.redoStack.push(JSON.stringify(editor.anns));
  editor.anns = JSON.parse(editor.undoStack.pop());
  setDirty(true); redrawAll();
}
function redo() {
  if (!editor.redoStack.length) return;
  editor.undoStack.push(JSON.stringify(editor.anns));
  editor.anns = JSON.parse(editor.redoStack.pop());
  setDirty(true); redrawAll();
}
$("#undo").addEventListener("click", undo);
$("#redo").addEventListener("click", redo);
const setZoom = (z) => { editor.zoom = Math.min(3, Math.max(0.4, z)); buildPages(); };
$("#zoom-in").addEventListener("click", () => setZoom(editor.zoom + 0.2));
$("#zoom-out").addEventListener("click", () => setZoom(editor.zoom - 0.2));

async function saveAnnotations() {
  try {
    await api(`/api/documents/${editor.docId}/annotations`, { method: "PUT", json: { annotations: editor.anns } });
    setDirty(false);
    editor.doc.has_annotations = editor.anns.length > 0;
    return true;
  } catch (err) { toast(`Save failed: ${err.message}`, true); return false; }
}
$("#ann-save").addEventListener("click", async () => { if (await saveAnnotations()) toast("Annotations saved"); });
$("#ann-download").addEventListener("click", async () => {
  if (editor.dirty && !(await saveAnnotations())) return;
  location.href = `/api/documents/${editor.docId}/download`;
});
$("#ann-print").addEventListener("click", async () => {
  if (editor.dirty && !(await saveAnnotations())) return;
  openPrintDialog({ ...editor.doc, has_annotations: editor.anns.length > 0 });
});

document.addEventListener("keydown", (e) => {
  if (currentView !== "annotate" || e.target.matches("input, textarea, select")) return;
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
  else if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); }
  else if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); $("#ann-save").click(); }
  else if (!mod) {
    const t = { p: "pen", h: "highlight", r: "rect", t: "text", w: "whiteout", e: "eraser" }[e.key.toLowerCase()];
    if (t) setTool(t);
  }
});
window.addEventListener("beforeunload", (e) => { if (editor.dirty) e.preventDefault(); });

/* ------------------------------------------------------------------ refresh loop */
async function refresh() {
  try {
    loadStatus();
    if (currentView === "dashboard") await loadJobs();
    if (currentView === "documents") await loadDocuments();
    if (currentView === "scan") await loadScans();
  } catch (err) { toast(err.message, true); }
}
refresh();
setInterval(() => { if (!document.hidden && currentView !== "annotate") refresh(); }, 5000);
