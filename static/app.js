"use strict";
(() => {
  // ================================================================ утиліти
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ls = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };
  const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
  const ago = (ts) => {
    if (!ts) return "ніколи";
    const s = Math.round(Date.now() / 1000 - ts);
    if (s < 60) return "щойно";
    if (s < 3600) return Math.round(s / 60) + " хв тому";
    if (s < 86400) return Math.round(s / 3600) + " год тому";
    return fmtDate(ts);
  };
  const fmtNum = (n, d = 4) => Number(n).toLocaleString("uk-UA", { maximumFractionDigits: d });
  const icon = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const I = {
    copy: icon('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>'),
    share: icon('<path d="M12 3v12M7 8l5-5 5 5"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/>'),
    route: icon('<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21Z"/><circle cx="12" cy="9.5" r="2.5"/>'),
    star: icon('<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9L12 3Z"/>'),
    trash: icon('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
    key: icon('<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3"/>'),
    shield: icon('<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z"/>'),
    out: icon('<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>'),
    list: icon('<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>'),
  };

  function toast(text, ms = 2200) {
    const t = $("#toast");
    t.textContent = text; t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), ms);
  }

  async function api(path, opts = {}) {
    const o = { method: "GET", ...opts, headers: { "X-Requested-With": "kd", ...(opts.headers || {}) } };
    if (o.body && typeof o.body !== "string") {
      o.body = JSON.stringify(o.body);
      o.headers["Content-Type"] = "application/json";
    }
    const r = await fetch(path, o);
    if (r.status === 401) {
      location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
      throw new Error("unauthorized");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = j.detail;
      const err = new Error(typeof d === "string" ? d : d?.message || "Помилка " + r.status);
      err.kind = d?.kind; err.status = r.status;
      throw err;
    }
    return j;
  }

  async function copy(text, label = "Скопійовано") {
    try { await navigator.clipboard.writeText(text); toast(label); }
    catch {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove(); toast(label);
    }
  }

  // ================================================================ стан
  let ME = null;
  let FEAT = {};
  let current = null;           // поточна ділянка
  let bookmarks = [];
  const CAD_RE = /^\d{10}:\d{2}:\d{3}:\d{4}$/;

  // ================================================================ карта
  const view = ls.get("kd_view", { c: [49.0, 31.3], z: 6 });
  const map = L.map("map", { zoomControl: false, attributionControl: true, maxZoom: 20, tap: true })
    .setView(view.c, view.z);
  map.attributionControl.setPrefix(false);

  const bases = {
    sat: L.layerGroup([
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        { maxZoom: 20, maxNativeZoom: 19, attribution: "Esri, Maxar, Earthstar Geographics" }),
      // підписи населених пунктів (CARTO тепер вимагає API-ключ і малює водяний знак)
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        { maxZoom: 20, maxNativeZoom: 19, attribution: "Esri" }),
    ]),
    osm: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 20, maxNativeZoom: 19, attribution: "© OpenStreetMap" }),
  };
  let baseKey = ls.get("kd_base", "sat");
  bases[baseKey].addTo(map);
  let dzkLayer = null;
  const hl = L.featureGroup().addTo(map);
  let meMarker = null;

  map.on("moveend", () => ls.set("kd_view", { c: [map.getCenter().lat, map.getCenter().lng], z: map.getZoom() }));
  map.on("zoomend", updateLayerHint);
  const zoomClass = () => map.getContainer().classList.toggle("z-lo", map.getZoom() < 16);
  map.on("zoomend", zoomClass); zoomClass();

  function setBase(k) {
    if (!bases[k] || k === baseKey) return;
    map.removeLayer(bases[baseKey]);
    bases[k].addTo(map);
    if (dzkLayer && map.hasLayer(dzkLayer)) dzkLayer.bringToFront();
    baseKey = k; ls.set("kd_base", k);
    document.querySelectorAll("[data-base]").forEach((b) => b.classList.toggle("on", b.dataset.base === k));
  }

  let dzk2Layer = null;
  function initDzk2() {
    if (!FEAT.layer2) return;
    dzk2Layer = L.vectorParcels({
      url: "/tiles/dzk2/{z}/{x}/{y}.pbf", minZoom: FEAT.layer2_min_zoom || 14, maxZoom: 20,
      maxNativeZoom: FEAT.layer2_max_native_zoom || 18, zIndex: 6,
    });
    $("#dzk2Toggle").closest("label").hidden = false;
    const on = ls.get("kd_dzk2", true);
    $("#dzk2Toggle").checked = on;
    if (on) dzk2Layer.addTo(map);
    $("#dzk2Toggle").addEventListener("change", (e) => {
      e.target.checked ? dzk2Layer.addTo(map) : map.removeLayer(dzk2Layer);
      ls.set("kd_dzk2", e.target.checked);
      updateLayerHint();
    });
  }

  function initDzk() {
    if (!FEAT.layer) {
      $("#dzkToggle").closest("label").hidden = true;
      return;
    }
    dzkLayer = L.tileLayer("/tiles/dzk/{z}/{x}/{y}.img", {
      minZoom: FEAT.layer_min_zoom || 12, maxZoom: 20, maxNativeZoom: FEAT.layer_max_native_zoom || 19, opacity: 0.9, zIndex: 5,
      errorTileUrl: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
    });
    // Джерело тайлів обмежує частоту запитів — тайл, що не прийшов, перезапитуємо
    // пізніше з наростаючою паузою (розкидано в часі, щоб не створювати новий залп)
    const RETRY = [3000, 8000, 20000];
    dzkLayer.on("tileerror", ({ tile, coords }) => {
      const n = +(tile.dataset.retry || 0);
      if (n >= RETRY.length) return;
      tile.dataset.retry = n + 1;
      setTimeout(() => {
        if (!tile.isConnected || !map.hasLayer(dzkLayer)) return;
        tile.src = dzkLayer.getTileUrl(coords) + "?r=" + (n + 1);
      }, RETRY[n] + Math.random() * 2000);
    });
    const on = ls.get("kd_dzk", !FEAT.layer2);
    $("#dzkToggle").checked = on;
    if (on) dzkLayer.addTo(map);
    updateLayerHint();
  }
  function updateLayerHint() {
    const h = $("#layerHint");
    if (!FEAT.layer && !FEAT.layer2) { h.hidden = false; h.textContent = "Шар ділянок не налаштовано — доступний пошук за номером."; return; }
    const mins = [];
    if (FEAT.layer && $("#dzkToggle").checked) mins.push(FEAT.layer_min_zoom || 12);
    if (FEAT.layer2 && $("#dzk2Toggle").checked) mins.push(FEAT.layer2_min_zoom || 14);
    if (!mins.length) { h.hidden = true; return; }
    const min = Math.min(...mins);
    h.hidden = map.getZoom() >= min;
    h.textContent = `Межі ділянок видно при наближенні (масштаб ${min}+).`;
  }

  // ================================================================ пошук
  const q = $("#q");
  const fmtCad = (digits) => {
    const d = digits.slice(0, 19);
    const parts = [d.slice(0, 10), d.slice(10, 12), d.slice(12, 15), d.slice(15, 19)].filter(Boolean);
    return parts.join(":");
  };
  q.addEventListener("input", () => {
    const digits = q.value.replace(/\D/g, "");
    const f = fmtCad(digits);
    if (q.value !== f) q.value = f;
    $("#qClear").hidden = !q.value;
    renderRecent();
    if (digits.length === 19) { q.blur(); runSearch(f); }
  });
  q.addEventListener("focus", renderRecent);
  q.addEventListener("blur", () => setTimeout(() => ($("#recent").hidden = true), 150));
  $("#qClear").addEventListener("click", () => { q.value = ""; $("#qClear").hidden = true; q.focus(); });
  $("#searchForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = fmtCad(q.value.replace(/\D/g, ""));
    if (!CAD_RE.test(f)) return toast("Потрібно 19 цифр номера");
    q.blur(); runSearch(f);
  });

  function pushRecent(cad) {
    const r = ls.get("kd_recent", []).filter((x) => x.cad !== cad);
    r.unshift({ cad, t: Date.now() });
    ls.set("kd_recent", r.slice(0, 10));
  }
  function renderRecent() {
    const ul = $("#recent");
    const prefix = q.value.replace(/\D/g, "");
    const items = ls.get("kd_recent", []).filter((x) => x.cad.replace(/\D/g, "").startsWith(prefix)).slice(0, 6);
    if (!items.length || document.activeElement !== q || prefix.length === 19) { ul.hidden = true; return; }
    ul.innerHTML = items.map((x) => `<li><button type="button" data-cad="${esc(x.cad)}">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>${esc(x.cad)}
      <small>${esc(new Date(x.t).toLocaleDateString("uk-UA"))}</small></button></li>`).join("");
    ul.hidden = false;
  }
  $("#recent").addEventListener("pointerdown", (e) => {
    const b = e.target.closest("[data-cad]");
    if (!b) return;
    e.preventDefault();
    q.value = b.dataset.cad; q.blur(); $("#recent").hidden = true; runSearch(b.dataset.cad);
  });

  let searchSeq = 0;
  async function runSearch(cad, { fit = true, fresh = false } = {}) {
    const seq = ++searchSeq;
    q.value = cad; $("#qClear").hidden = false;
    sheetLoading(`Шукаю ${cad}…`);
    try {
      const p = await api(`/api/parcel/search?q=${encodeURIComponent(cad)}${fresh ? "&fresh=1" : ""}`);
      if (seq !== searchSeq) return;
      pushRecent(cad);
      showParcel(p, { fit });
    } catch (e) {
      if (seq !== searchSeq || e.message === "unauthorized") return;
      sheetError(e);
    }
  }

  // ================================================================ клік по карті
  map.on("click", async (e) => {
    if (!FEAT.identify) { if (!$("#sheet").hidden) closeSheet(); return; }
    if (map.getZoom() < 14) { toast("Наблизьте карту, щоб вибрати ділянку"); return; }
    const seq = ++searchSeq;
    const pin = L.circleMarker(e.latlng, { radius: 6, color: "#fff", weight: 2, fillColor: "#1f6f5c", fillOpacity: 1 });
    hl.clearLayers(); hl.addLayer(pin);
    sheetLoading("Визначаю ділянку…");
    try {
      const r = await api(`/api/parcel/at?lat=${e.latlng.lat.toFixed(7)}&lng=${e.latlng.lng.toFixed(7)}&z=${map.getZoom()}`);
      if (seq !== searchSeq) return;
      const items = r.items || [];
      if (!items.length) { closeSheet(); hl.clearLayers(); toast("Тут ділянок не знайдено"); return; }
      if (items.length === 1) return showParcel(items[0], { fit: false });
      sheetHtml(`<p class="sub">Тут кілька ділянок:</p><ul class="pick">${items.map((p, i) =>
        `<li><button data-i="${i}">${esc(p.cadnum || "без номера")}</button></li>`).join("")}</ul>`);
      $("#sheetBody").querySelectorAll("[data-i]").forEach((b) =>
        b.addEventListener("click", () => showParcel(items[+b.dataset.i], { fit: false })));
    } catch (err) {
      if (seq === searchSeq && err.message !== "unauthorized") sheetError(err);
    }
  });

  // ================================================================ шторка
  const sheet = $("#sheet");
  const ro = new ResizeObserver(() => {
    const mobile = innerWidth < 720;
    const h = sheet.hidden || !mobile ? 0 : Math.min(sheet.offsetHeight, innerHeight * 0.5);
    document.documentElement.style.setProperty("--sheet-h", h + "px");
  });
  ro.observe(sheet);

  function sheetHtml(html) {
    $("#sheetBody").innerHTML = html;
    sheet.hidden = false;
    sheet.style.transform = "";
    $("#layersPop").hidden = true;
  }
  function sheetLoading(text) { current = null; sheetHtml(`<div class="loading"><span class="spinner"></span>${esc(text)}</div>`); }
  function sheetError(e) {
    let extra = "";
    if (e.kind === "session") extra = ME?.user.role === "admin"
      ? `<button class="btn small" id="goAdmin">Оновити сесію ЄДЕССБ</button>` : "";
    sheetHtml(`<p class="err">${esc(e.message)}</p>${extra}`);
    $("#goAdmin")?.addEventListener("click", () => openPanel("admin"));
    if (e.kind === "session") refreshMe();
  }
  function closeSheet() {
    sheet.hidden = true; current = null; hl.clearLayers();
    if (new URLSearchParams(location.search).has("cad")) history.replaceState(history.state, "", location.pathname);
  }

  // свайп вниз — закрити
  (() => {
    let y0 = null, dy = 0;
    const start = (y) => { y0 = y; dy = 0; sheet.classList.add("dragging"); };
    const move = (y) => { if (y0 === null) return; dy = Math.max(0, y - y0); sheet.style.transform = `translateY(${dy}px)`; };
    const end = () => {
      if (y0 === null) return;
      sheet.classList.remove("dragging");
      if (dy > 90) closeSheet();
      else if (dy < 6) { const d = $("details.more", sheet); if (d) d.open = !d.open; sheet.style.transform = ""; }
      else sheet.style.transform = "";
      y0 = null;
    };
    const g = $("#grab");
    g.addEventListener("touchstart", (e) => start(e.touches[0].clientY), { passive: true });
    g.addEventListener("touchmove", (e) => move(e.touches[0].clientY), { passive: true });
    g.addEventListener("touchend", end);
    g.addEventListener("mousedown", (e) => { start(e.clientY); const mm = (ev) => move(ev.clientY);
      const mu = () => { end(); removeEventListener("mousemove", mm); removeEventListener("mouseup", mu); };
      addEventListener("mousemove", mm); addEventListener("mouseup", mu); });
    // свайп вниз по самій шторці, якщо вона прокручена до верху
    let ty = null;
    sheet.addEventListener("touchstart", (e) => { ty = sheet.scrollTop <= 0 && !e.target.closest(".grab,textarea") ? e.touches[0].clientY : null; }, { passive: true });
    sheet.addEventListener("touchmove", (e) => { if (ty !== null && e.touches[0].clientY - ty > 12 && y0 === null) start(ty); if (y0 !== null) move(e.touches[0].clientY); }, { passive: true });
    sheet.addEventListener("touchend", () => { if (y0 !== null) end(); ty = null; });
  })();

  const FIELD_LABELS = {
    purpose: "Цільове призначення", purpose_code: "Код КВЦПЗ", category: "Категорія земель",
    ownership: "Форма власності", address: "Адреса / розташування",
  };

  function areaText(p) {
    if (p.area !== undefined && p.area !== null && p.area !== "") {
      const n = parseFloat(String(p.area).replace(",", "."));
      return isNaN(n) ? esc(p.area) : `${fmtNum(n)} ${esc(p.unit || "га")}`;
    }
    if (p.area_calc_ha) return `≈ ${fmtNum(p.area_calc_ha)} га`;
    return null;
  }

  function showParcel(p, { fit = true } = {}) {
    current = p;
    const cad = p.cadnum || "";
    if (cad) {
      const u = new URL(location.href); u.searchParams.set("cad", cad);
      history.replaceState(history.state, "", u.pathname + u.search);
    }
    // геометрія
    hl.clearLayers();
    if (p.geometry) {
      // темна «підкладка» + яскравий контур — читається і на супутнику, і на схемі
      hl.addLayer(L.geoJSON(p.geometry, { style: { color: "#000", weight: 6, opacity: 0.45, fill: false }, interactive: false }));
      const gj = L.geoJSON(p.geometry, {
        style: { color: "#ffd400", weight: 3, fillColor: "#ffd400", fillOpacity: 0.22 },
        interactive: false,
      });
      hl.addLayer(gj);
      // поворотні точки з тими самими номерами, що й у режимі «на місцевості»
      const cr = window.KDAR ? KDAR.corners(p.geometry) : { points: [] };
      if (cr.points.length && cr.points.length <= 80) {
        for (const c of cr.points) {
          hl.addLayer(L.marker([c.lat, c.lng], {
            interactive: false, keyboard: false,
            icon: L.divIcon({ className: "corner-num", html: String(c.n), iconSize: [18, 18] }),
          }));
        }
      }
      if (fit) fitParcel(gj.getBounds());
    } else if (p.centroid && fit) {
      map.setView([p.centroid[1], p.centroid[0]], 17);
    }

    const area = areaText(p);
    const subParts = [];
    if (area) subParts.push(`<b>${area}</b>`);
    if (p.purpose) subParts.push(esc(p.purpose));
    const bm = bookmarks.find((b) => b.cadnum === cad);

    const rows = [];
    const official = parseFloat(String(p.area ?? "").replace(",", "."));
    if (!isNaN(official) && p.area_calc_ha && Math.abs(p.area_calc_ha - official) / official > 0.05)
      rows.push(["Площа за геометрією", `${fmtNum(p.area_calc_ha)} га <small>(відрізняється від офіційної)</small>`]);
    else if (isNaN(official) && p.area_calc_ha) rows.push(["Площа", `≈ ${fmtNum(p.area_calc_ha)} га <small>(за геометрією)</small>`]);
    for (const k of Object.keys(FIELD_LABELS)) {
      if (!p[k]) continue;
      // код уже є на початку тексту призначення («01.09 Для …»)
      if (k === "purpose_code" && String(p.purpose || "").startsWith(p.purpose_code)) continue;
      rows.push([FIELD_LABELS[k], esc(p[k])]);
    }
    for (const f of p.fields || []) {
      let v = esc(f.value);
      if (/valuation_value$/.test(f.key) && !isNaN(parseFloat(f.value))) v = fmtNum(parseFloat(f.value), 2);
      const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(f.value));
      if (iso) v = `${iso[3]}.${iso[2]}.${iso[1]}`;
      rows.push([esc(f.label), v]);
    }
    if (p.centroid) {
      const c = `${p.centroid[1].toFixed(6)}, ${p.centroid[0].toFixed(6)}`;
      rows.push(["Центр (lat, lng)", `<button class="link" data-copy="${c}" style="border:0;background:none;padding:0;color:var(--accent)">${c}</button>`, true]);
    }

    sheetHtml(`
      <button class="cad" id="cadCopy" title="Скопіювати">${esc(cad || "Ділянка")}</button>
      ${subParts.length ? `<p class="sub">${subParts.join(" · ")}</p>` : ""}
      ${p.stale ? `<div class="warn">Показано збережені дані від ${fmtDate(p.cached_at)}. ${esc(p.warning || "")}</div>`
        : p.warning ? `<div class="warn">${p.source === "dzk2" ? "⚠️ " : ""}${esc(p.warning)}</div>` : ""}
      <div class="actions">
        <button id="aCopy">${I.copy}Номер</button>
        <button id="aShare">${I.share}Поділитись</button>
        <button id="aRoute" ${p.centroid ? "" : "disabled"}>${I.route}Маршрут</button>
        <button id="aSave" class="${bm ? "on" : ""}" ${cad ? "" : "disabled"}>${I.star}${bm ? "Збережено" : "Зберегти"}</button>
      </div>
      ${p.geometry && window.KDAR ? `<button class="btn-ar" id="aAR">📷 Точки на місцевості</button>` : ""}
      <details class="more" ${innerWidth >= 720 ? "open" : ""}>
        <summary>Деталі</summary>
        <dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") || "<dd>Немає даних</dd>"}</dl>
        ${p.cached_at && !p.stale ? `<p class="hint">Дані з кешу (${ago(p.cached_at)}) · <a href="#" id="refresh">оновити</a></p>` : ""}
      </details>
      <div id="noteWrap" ${bm ? "" : "hidden"}>
        <textarea class="note-box" id="note" placeholder="Нотатка до ділянки…" maxlength="300">${esc(bm?.note || "")}</textarea>
      </div>`);

    $("#cadCopy").onclick = $("#aCopy").onclick = () => cad && copy(cad, "Номер скопійовано");
    $("#aShare").onclick = () => share(cad);
    $("#aRoute").onclick = () => {
      const [lng, lat] = p.centroid;
      window.open(`https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}`, "_blank", "noopener");
    };
    $("#aSave").onclick = () => toggleBookmark(p);
    $("#aAR")?.addEventListener("click", () => KDAR.open(p));
    $("#refresh")?.addEventListener("click", (e) => { e.preventDefault(); runSearch(cad, { fit: false, fresh: true }); });
    $("#sheetBody").querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => copy(b.dataset.copy)));
    const note = $("#note");
    let nt;
    note.addEventListener("input", () => {
      clearTimeout(nt);
      nt = setTimeout(() => saveBookmark(p, note.value, true), 700);
    });
  }

  function fitParcel(bounds) {
    const mobile = innerWidth < 720;
    map.fitBounds(bounds, {
      paddingTopLeft: [mobile ? 30 : 460, mobile ? 90 : 90],
      paddingBottomRight: [30, mobile ? Math.round(innerHeight * 0.38) : 40],
      maxZoom: 18,
    });
  }

  async function share(cad) {
    const url = `${location.origin}/?cad=${encodeURIComponent(cad)}`;
    if (navigator.share) {
      try { await navigator.share({ title: `Ділянка ${cad}`, text: `Кадастровий номер ${cad}`, url }); } catch {}
    } else copy(url, "Посилання скопійовано");
  }

  // ================================================================ закладки
  async function loadBookmarks() { bookmarks = await api("/api/bookmarks").catch(() => []); }
  async function saveBookmark(p, note, silent) {
    const c = p.centroid || [];
    await api("/api/bookmarks", { method: "POST", body: { cadnum: p.cadnum, note, lat: c[1], lng: c[0] } });
    await loadBookmarks();
    if (!silent) toast("Збережено");
  }
  async function toggleBookmark(p) {
    const bm = bookmarks.find((b) => b.cadnum === p.cadnum);
    try {
      if (bm) {
        await api(`/api/bookmarks/${bm.id}`, { method: "DELETE" });
        await loadBookmarks(); toast("Видалено зі збережених");
      } else await saveBookmark(p, "");
      if (current === p) showParcel(p, { fit: false });
      if (!bm) $("#note")?.focus();
    } catch (e) { toast(e.message); }
  }

  // ================================================================ кнопки карти
  $("#layersBtn").addEventListener("click", () => { $("#layersPop").hidden = !$("#layersPop").hidden; });
  document.querySelectorAll("[data-base]").forEach((b) => {
    b.classList.toggle("on", b.dataset.base === baseKey);
    b.addEventListener("click", () => setBase(b.dataset.base));
  });
  $("#dzkToggle").addEventListener("change", (e) => {
    if (!dzkLayer) return;
    e.target.checked ? dzkLayer.addTo(map) : map.removeLayer(dzkLayer);
    ls.set("kd_dzk", e.target.checked);
    updateLayerHint();
  });
  map.on("movestart", () => ($("#layersPop").hidden = true));

  $("#locateBtn").addEventListener("click", () => {
    const b = $("#locateBtn");
    if (!navigator.geolocation) return toast("Геолокація недоступна");
    b.classList.add("busy");
    navigator.geolocation.getCurrentPosition((pos) => {
      b.classList.remove("busy"); b.classList.add("on");
      const ll = [pos.coords.latitude, pos.coords.longitude];
      if (meMarker) map.removeLayer(meMarker);
      meMarker = L.layerGroup([
        L.circle(ll, { radius: pos.coords.accuracy, color: "#1a73e8", weight: 1, fillOpacity: 0.1, interactive: false }),
        L.circleMarker(ll, { radius: 7, color: "#fff", weight: 2.5, fillColor: "#1a73e8", fillOpacity: 1, interactive: false }),
      ]).addTo(map);
      map.flyTo(ll, Math.max(map.getZoom(), 17), { duration: 0.8 });
    }, (err) => {
      b.classList.remove("busy");
      toast(err.code === 1 ? "Немає дозволу на геолокацію" : "Не вдалося визначити місце");
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 });
  });

  // ================================================================ панель
  const panel = $("#panel");
  const stack = [];
  function openPanel(name, push = true) {
    if (panel.hidden) {
      panel.hidden = false; $("#scrim").hidden = innerWidth < 720;
      history.pushState({ panel: 1 }, "");
    }
    if (push) stack.push(name);
    $("#panelBack").style.visibility = stack.length > 1 ? "visible" : "hidden";
    VIEWS[name]();
  }
  function closePanel(fromPop) {
    if (panel.hidden) return;
    panel.hidden = true; $("#scrim").hidden = true; stack.length = 0;
    if (!fromPop && history.state?.panel) history.back();
    if (location.hash) history.replaceState(history.state, "", location.pathname + location.search);
  }
  const back = () => { stack.pop(); stack.length ? openPanel(stack[stack.length - 1], false) : closePanel(); };
  $("#panelBack").onclick = back;
  $("#panelClose").onclick = () => closePanel();
  $("#scrim").onclick = () => closePanel();
  $("#menuBtn").onclick = () => openPanel("menu");
  addEventListener("popstate", () => closePanel(true));
  addEventListener("keydown", (e) => { if (e.key === "Escape") panel.hidden ? closeSheet() : closePanel(); });
  const setPanel = (title, html) => { $("#panelTitle").textContent = title; $("#panelBody").innerHTML = html; $("#panelBody").scrollTop = 0; };

  const VIEWS = {
    menu() {
      const u = ME.user;
      const isAdmin = u.role === "admin";
      setPanel("Меню", `
        <div class="me"><span class="avatar">${esc(initials(u.name))}</span>
          <div><b>${esc(u.name)}</b><small>@${esc(u.username)}${isAdmin ? " · адміністратор" : ""}</small></div></div>
        <ul class="menu">
          <li><button data-go="bookmarks">${I.star}Збережені ділянки<span class="badge">${bookmarks.length}</span></button></li>
          <li><button data-go="password">${I.key}Змінити пароль</button></li>
          ${isAdmin ? `<li><button data-go="admin">${I.shield}Адміністрування
             <span class="badge ${ME.edessb_ok === false ? "bad" : ME.edessb_ok ? "ok" : ""}">ЄДЕССБ ${ME.edessb_ok === false ? "✕" : ME.edessb_ok ? "✓" : "?"}</span></button></li>` : ""}
          <li><button id="logout">${I.out}Вийти</button></li>
        </ul>
        <p class="hint" style="margin-top:20px">Порада: додайте сторінку на головний екран телефону — працюватиме як застосунок.</p>`);
      $("#panelBody").querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => openPanel(b.dataset.go)));
      $("#logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); location.replace("/login"); };
    },

    async bookmarks() {
      setPanel("Збережені", `<div class="loading"><span class="spinner"></span>Завантаження…</div>`);
      await loadBookmarks();
      if (!bookmarks.length) return setPanel("Збережені", `<p class="empty">Тут з'являться ділянки, які ви збережете зірочкою.</p>`);
      setPanel("Збережені", `<ul class="list">${bookmarks.map((b) => `
        <li><button class="link grow" data-cad="${esc(b.cadnum)}"><span class="mono">${esc(b.cadnum)}</span>
          ${b.note ? `<small>${esc(b.note)}</small>` : `<small>${fmtDate(b.created_at)}</small>`}</button>
          <button class="icon-btn" data-del="${b.id}" aria-label="Видалити">${I.trash}</button></li>`).join("")}</ul>`);
      $("#panelBody").querySelectorAll("[data-cad]").forEach((b) => (b.onclick = () => { closePanel(); runSearch(b.dataset.cad); }));
      $("#panelBody").querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
        if (!confirm("Видалити зі збережених?")) return;
        await api(`/api/bookmarks/${b.dataset.del}`, { method: "DELETE" }); VIEWS.bookmarks();
      }));
    },

    password() {
      setPanel("Змінити пароль", `<form class="form" id="pwForm">
        <label>Поточний пароль<input type="password" name="old" autocomplete="current-password" required></label>
        <label>Новий пароль<input type="password" name="new" autocomplete="new-password" minlength="8" required></label>
        <button class="btn primary">Зберегти</button>
        <p class="hint">Інші пристрої буде розлогінено.</p></form>`);
      $("#pwForm").onsubmit = async (e) => {
        e.preventDefault();
        try { await api("/api/me/password", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); toast("Пароль змінено"); back(); }
        catch (err) { toast(err.message, 3500); }
      };
    },

    async admin() {
      setPanel("Адміністрування", `<div class="loading"><span class="spinner"></span>Завантаження…</div>`);
      let s, users;
      try { [s, users] = await Promise.all([api("/api/admin/edessb"), api("/api/admin/users")]); }
      catch (e) { return setPanel("Адміністрування", `<p class="err">${esc(e.message)}</p>`); }
      const st = s.status;
      const pending = users.filter((u) => u.status === "pending");
      const others = users.filter((u) => u.status !== "pending");
      const stateTxt = st.ok === true ? "Сесія активна" : st.ok === false ? "Сесія неактивна" : "Не перевірялась";
      setPanel("Адміністрування", `
        <h3>Сесія ЄДЕССБ</h3>
        <div class="status-card">
          <div class="big"><span class="dot ${st.ok ? "ok" : st.ok === false ? "bad" : ""}"></span>${stateTxt}</div>
          <p>${esc(st.message || "")}</p>
          <p>Перевірка: ${ago(st.checked_at)} (${esc(st.source || "—")}) · остання успішна: ${ago(st.last_ok_at)}</p>
          <p>Cookies: ${s.cookies.count} шт., оновлено ${ago(s.cookies.updated_at)}${s.cookies.source ? " · " + esc(s.cookies.source) : ""}</p>
          ${!s.config.keepalive_urls.length ? `<p class="err">keepalive_urls не задано в config/upstream.toml</p>` : ""}
          ${!s.config.telegram ? `<p>Telegram-сповіщення вимкнені</p>` : ""}
        </div>
        <div class="btn-row" style="margin-top:10px"><button class="btn small" id="chk">Перевірити зараз</button></div>
        <form class="form" id="ckForm">
          <label>Нові cookies<textarea name="text" placeholder="Значення session_edessb, рядок «Cookie: …» або «Copy as cURL (bash)» запиту" required></textarea></label>
          <button class="btn primary">Зберегти і перевірити</button>
          <p class="hint">Як отримати: увійдіть в ЄДЕССБ → F12 → Application → Cookies → https://admin.e-construction.gov.ua → скопіюйте Value рядка session_edessb. Для тайлів: Network → запит до data.gki.com.ua → Copy as cURL (bash) — cookies ЄДЕССБ при цьому не зачіпаються. Не натискайте «Вийти» в ЄДЕССБ — це вб\'є сесію і тут.</p>
        </form>

        ${pending.length ? `<h3>Очікують підтвердження (${pending.length})</h3><ul class="list">${pending.map(userRow).join("")}</ul>` : ""}
        <h3>Користувачі (${others.length})</h3>
        <ul class="list">${others.map(userRow).join("")}</ul>
        <ul class="menu" style="margin-top:16px"><li><button data-go="audit">${I.list}Журнал дій</button></li></ul>`);

      $("#chk").onclick = async (e) => {
        e.target.disabled = true; e.target.textContent = "Перевіряю…";
        try { const r = await api("/api/admin/edessb/check", { method: "POST" }); toast(r.status.ok ? "Сесія активна ✓" : "Сесія неактивна: " + r.status.message, 4000); }
        catch (err) { toast(err.message); }
        refreshMe(); VIEWS.admin();
      };
      $("#ckForm").onsubmit = async (e) => {
        e.preventDefault();
        const btn = e.target.querySelector("button"); btn.disabled = true; btn.textContent = "Перевіряю…";
        try {
          const r = await api("/api/admin/edessb/cookies", { method: "POST", body: Object.fromEntries(new FormData(e.target)) });
          toast(`Збережено ${r.count} cookies. ${r.status.ok ? "Сесія активна ✓" : "Але сесія неактивна: " + r.status.message}`, 5000);
        } catch (err) { toast(err.message, 4000); }
        refreshMe(); VIEWS.admin();
      };
      $("#panelBody").querySelector("[data-go=audit]").onclick = () => openPanel("audit");
      $("#panelBody").querySelectorAll("[data-toggle]").forEach((b) => (b.onclick = () => { const el = $("#ua" + b.dataset.toggle); el.hidden = !el.hidden; }));
      $("#panelBody").querySelectorAll("[data-act]").forEach((b) => (b.onclick = () => userAction(b.dataset.act, +b.dataset.id, b.dataset.name)));
    },

    async audit() {
      setPanel("Журнал дій", `<div class="loading"><span class="spinner"></span></div>`);
      const rows = await api("/api/admin/audit");
      setPanel("Журнал дій", rows.length ? `<ul class="list">${rows.map((r) => `<li><div class="grow">
        <span><b>${esc(r.action)}</b> · ${esc(r.username || "—")}</span><small>${fmtDate(r.ts)} · ${esc(r.detail || "")}</small></div></li>`).join("")}</ul>`
        : `<p class="empty">Порожньо</p>`);
    },
  };

  function userRow(u) {
    const self = u.id === ME.user.id;
    const badge = u.status === "pending" ? `<span class="badge">очікує</span>` : u.status === "blocked" ? `<span class="badge bad">заблок.</span>` : u.role === "admin" ? `<span class="badge ok">адмін</span>` : "";
    let acts = "";
    if (!self) {
      if (u.status === "pending") acts = `<button class="btn small primary" data-act="approve" data-id="${u.id}">Прийняти</button>
        <button class="btn small danger" data-act="delete" data-id="${u.id}" data-name="${esc(u.username)}">Відхилити</button>`;
      else acts = `<button class="btn small" data-toggle="${u.id}" aria-label="Дії">•••</button>`;
    }
    const more = self || u.status === "pending" ? "" : `<div class="btn-row user-acts" id="ua${u.id}" hidden>
      <button class="btn small" data-act="${u.status === "blocked" ? "unblock" : "block"}" data-id="${u.id}">${u.status === "blocked" ? "Розблокувати" : "Заблокувати"}</button>
      <button class="btn small" data-act="${u.role === "admin" ? "demote" : "promote"}" data-id="${u.id}">${u.role === "admin" ? "Зняти адміна" : "Зробити адміном"}</button>
      <button class="btn small" data-act="password" data-id="${u.id}">Новий пароль</button>
      <button class="btn small danger" data-act="delete" data-id="${u.id}" data-name="${esc(u.username)}">Видалити</button></div>`;
    return `<li class="user"><div class="grow"><span><b>${esc(u.name)}</b> ${badge}</span>
      <small>@${esc(u.username)} · ${u.last_seen ? "був(ла) " + ago(u.last_seen) : "ще не входив(ла)"}</small></div>${acts}${more}</li>`;
  }

  async function userAction(act, id, name) {
    try {
      if (act === "approve") await api(`/api/admin/users/${id}`, { method: "PATCH", body: { status: "active" } });
      else if (act === "delete") { if (!confirm(`Видалити @${name}?`)) return; await api(`/api/admin/users/${id}`, { method: "DELETE" }); }
      else if (act === "block" || act === "unblock") await api(`/api/admin/users/${id}`, { method: "PATCH", body: { status: act === "block" ? "blocked" : "active" } });
      else if (act === "promote" || act === "demote") await api(`/api/admin/users/${id}`, { method: "PATCH", body: { role: act === "promote" ? "admin" : "user" } });
      else if (act === "password") {
        const pw = prompt("Новий пароль (мінімум 8 символів):");
        if (!pw) return;
        await api(`/api/admin/users/${id}`, { method: "PATCH", body: { password: pw } });
      }
      toast("Готово"); VIEWS.admin();
    } catch (e) { toast(e.message, 3500); }
  }

  const initials = (n) => (n || "?").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();

  // ================================================================ статус
  function renderStatus() {
    const b = $("#banner");
    const admin = ME.user.role === "admin";
    $("#menuBtn").textContent = initials(ME.user.name);
    if (admin && ME.edessb_ok === false) $("#menuBtn").dataset.dot = ""; else delete $("#menuBtn").dataset.dot;
    if (ME.edessb_ok === false) {
      b.hidden = false;
      b.textContent = admin ? "Сесія ЄДЕССБ неактивна — натисніть, щоб оновити cookies" : "Пошук ділянок тимчасово недоступний. Межі на карті працюють. Адміністратора повідомлено.";
      b.style.cursor = admin ? "pointer" : "";
      b.onclick = admin ? () => openPanel("admin") : null;
    } else b.hidden = true;
  }
  async function refreshMe() {
    try { ME = await api("/api/me"); FEAT = ME.features; renderStatus(); } catch {}
  }

  // ================================================================ старт
  (async () => {
    await refreshMe();
    if (!ME) return;
    initDzk2();
    initDzk();
    loadBookmarks();
    const cad = new URLSearchParams(location.search).get("cad");
    if (cad && CAD_RE.test(cad)) runSearch(cad);
    if (location.hash === "#admin" && ME.user.role === "admin") openPanel("admin");
    setInterval(() => { if (!document.hidden) refreshMe(); }, 120000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshMe(); });
  })();
})();
