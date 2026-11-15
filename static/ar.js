"use strict";
/* «Точки на місцевості»: поворотні точки ділянки через камеру телефона.
   Камера + GPS + компас → мітки Т1, Т2… з відстанню і лінії меж поверх відео.
   Точність орієнтовна (GPS 3–10 м, компас 5–15°); калібрування прибирає
   постійні похибки. Без камери/компаса — режим «стрілка». */
(() => {
  const D2R = Math.PI / 180, R = 6378137;
  const EYE = 1.5;                   // висота камери над землею, м
  const MAX_DIST = 600;              // далі не малюємо
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtD = (d) => (d < 10 ? d.toFixed(1).replace(".", ",") : Math.round(d)) + " м";
  const wrap180 = (a) => ((a % 360) + 540) % 360 - 180;

  // ------------------------------------------------------------ геометрія
  function localFrame(lat0, lng0) {
    const kx = Math.cos(lat0 * D2R) * D2R * R, ky = D2R * R;
    return { to: (lat, lng) => [(lng - lng0) * kx, (lat - lat0) * ky], from: (x, y) => [lat0 + y / ky, lng0 + x / kx] };
  }

  function dp(pts, tol) {               // Дуглас–Пекер для відкритого ланцюга, повертає індекси
    const keep = new Set([0, pts.length - 1]);
    const stack = [[0, pts.length - 1]];
    while (stack.length) {
      const [a, b] = stack.pop();
      const [ax, ay] = pts[a], [bx, by] = pts[b];
      const L = Math.hypot(bx - ax, by - ay) || 1e-9;
      let md = -1, mi = -1;
      for (let i = a + 1; i < b; i++) {
        const d = Math.abs((bx - ax) * (ay - pts[i][1]) - (ax - pts[i][0]) * (by - ay)) / L;
        if (d > md) { md = d; mi = i; }
      }
      if (md > tol) { keep.add(mi); stack.push([a, mi], [mi, b]); }
    }
    return [...keep].sort((x, y) => x - y);
  }

  function simplifyRing(ring) {          // ring: [[x,y]...] без повтору першої точки
    if (ring.length <= 4) return ring;
    let far = 0, fd = -1;
    ring.forEach(([x, y], i) => { const d = Math.hypot(x - ring[0][0], y - ring[0][1]); if (d > fd) { fd = d; far = i; } });
    const a = ring.slice(0, far + 1), b = ring.slice(far).concat([ring[0]]);
    const ka = dp(a, 0.7).map((i) => a[i]), kb = dp(b, 0.7).map((i) => b[i]);
    let out = ka.concat(kb.slice(1, -1));
    // прибрати майже прямі вершини (відхилення < 8°)
    for (let changed = true; changed && out.length > 3;) {
      changed = false;
      for (let i = 0; i < out.length && out.length > 3; i++) {
        const p = out[(i - 1 + out.length) % out.length], c = out[i], n = out[(i + 1) % out.length];
        const a1 = Math.atan2(c[1] - p[1], c[0] - p[0]), a2 = Math.atan2(n[1] - c[1], n[0] - c[0]);
        if (Math.abs(wrap180((a2 - a1) / D2R)) < 8) { out.splice(i, 1); changed = true; i--; }
      }
    }
    return out;
  }

  /** Поворотні точки з GeoJSON (Polygon/MultiPolygon, WGS84).
   *  → {points:[{n, lat, lng}], rings:[[індекси точок]]}; нумерація за годинниковою
   *  стрілкою, починаючи з північно-західної точки (як на кадастрових планах). */
  function corners(geom) {
    if (!geom) return { points: [], rings: [] };
    const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.type === "MultiPolygon" ? geom.coordinates : [];
    if (!polys.length || !polys[0][0]) return { points: [], rings: [] };
    const [lng0, lat0] = polys[0][0][0];
    const F = localFrame(lat0, lng0);
    const points = [], rings = [];
    for (const poly of polys) {
      const outer = (poly[0] || []).map(([lng, lat]) => F.to(lat, lng));
      if (outer.length > 1 && outer[0][0] === outer.at(-1)[0] && outer[0][1] === outer.at(-1)[1]) outer.pop();
      if (outer.length < 3) continue;
      let r = simplifyRing(outer);
      let area = 0;
      r.forEach((p, i) => { const q = r[(i + 1) % r.length]; area += p[0] * q[1] - q[0] * p[1]; });
      if (area > 0) r = r.reverse();                         // за годинниковою (y — північ)
      let s = 0, best = -Infinity;
      r.forEach(([x, y], i) => { if (y - x > best) { best = y - x; s = i; } });
      r = r.slice(s).concat(r.slice(0, s));
      const idx = [];
      for (const [x, y] of r) {
        const [lat, lng] = F.from(x, y);
        points.push({ n: points.length + 1, lat, lng });
        idx.push(points.length - 1);
      }
      rings.push(idx);
    }
    return { points, rings };
  }

  // ------------------------------------------------------------ сенсори
  // Магнітне схилення для України ≈ +6…+10° (східне), росте зі сходом.
  const declination = (lng) => 1 + 0.25 * lng;

  // ------------------------------------------------------------ стан і UI
  let S = null;

  function open(parcel, opts = {}) {
    parcel = parcel || null;
    const c = parcel ? corners(parcel.geometry) : { points: [], rings: [] };
    if (parcel && !c.points.length) { alert("Для цієї ділянки немає контуру"); return; }
    close();
    S = {
      parcel, pts: c.points, rings: c.rings, mode: "cam", pos: null, rawHeading: null, headAcc: null,
      heading: null, pitch: 0, headOff: 0, posOff: [0, 0], target: null, near: false,
      fovLong: +(localStorage.getItem("kd_ar_fov") || 67), stream: null, watch: null, raf: 0,
      absolute: false, rawBasis: null, yawOff: null, oriCount: 0,
      fixes: [], frozen: null, shown: null,
      // рентген: ділянки навколо з векторного шару
      xray: opts.xray ?? !parcel, around: null, F0: null, loadedAt: null, loading: false, xscreen: [],
    };
    const el = document.createElement("div");
    el.id = "ar";
    el.innerHTML = `
      <video id="arVideo" playsinline muted autoplay></video>
      <canvas id="arCanvas"></canvas>
      <div class="ar-cross" id="arCross"></div>
      <div class="ar-arrow" id="arArrow" hidden>
        <svg viewBox="0 0 100 100"><path d="M50 6 L80 78 L50 62 L20 78 Z"/></svg>
        <div class="ar-big" id="arBig">—</div>
        <div class="ar-sub" id="arSub"></div>
      </div>
      <div class="ar-top">
        <button class="ar-btn" id="arClose" aria-label="Закрити">✕</button>
        <div class="ar-title"><b>${esc(parcel ? parcel.cadnum : "Рентген кадастру")}</b><span id="arStat">Очікую GPS…</span></div>
      </div>
      ${parcel && parcel.partial_geometry ? `<div class="ar-warn">Контур неповний (старий шар) — частини точок може не бути.</div>` : ""}
      <div class="ar-bottom">
        <div class="ar-target" id="arTarget">${parcel ? "Торкніться мітки, щоб обрати ціль" : "Торкніться підпису ділянки, щоб відкрити її картку"}</div>
        <div class="ar-row">
          ${parcel ? `<button class="ar-btn wide" id="arMode">🧭 Стрілка</button>` : ""}
          <button class="ar-btn wide" id="arXray">${(opts.xray ?? !parcel) ? "🩻 Рентген: увімк." : "🩻 Рентген"}</button>
          <button class="ar-btn wide" id="arCal">🎯 Калібрувати</button>
        </div>
        <div class="ar-row"><button class="ar-btn wide" id="arFreeze">📌 Зафіксувати</button></div>
        <p class="ar-note">Точність орієнтовна (±3–10 м). Для виносу меж в натуру потрібен землевпорядник.</p>
      </div>
      <div class="ar-sheet" id="arCalSheet" hidden></div>`;
    document.body.appendChild(el);
    document.documentElement.classList.add("ar-open");

    $("#arClose").onclick = close;
    $("#arMode") && ($("#arMode").onclick = () => setMode(S.mode === "cam" ? "arrow" : "cam"));
    $("#arXray").onclick = () => { S.xray = !S.xray; $("#arXray").textContent = S.xray ? "🩻 Рентген: увімк." : "🩻 Рентген"; if (S.xray) loadAround(); };
    $("#arCal").onclick = openCal;
    $("#arFreeze").onclick = toggleFreeze;
    $("#arCanvas").addEventListener("click", onTapCanvas);
    start();
  }

  async function start() {
    // iOS: дозвіл на компас — лише у відповідь на дію користувача (ми якраз у ній)
    try {
      if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
        const r = await DeviceOrientationEvent.requestPermission();
        if (r !== "granted") status("Немає дозволу на компас — лише відстані");
      }
    } catch { /* старі iOS / десктоп */ }
    if ("ondeviceorientationabsolute" in window) window.addEventListener("deviceorientationabsolute", onOri);
    window.addEventListener("deviceorientation", onOri);

    if (navigator.geolocation) {
      S.watch = navigator.geolocation.watchPosition(onPos, (e) => status(e.code === 1 ? "Немає дозволу на геолокацію" : "GPS недоступний"),
        { enableHighAccuracy: true, maximumAge: 1000, timeout: 20000 });
    } else status("Геолокація недоступна");

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false,
      });
      if (!S) { stream.getTracks().forEach((t) => t.stop()); return; }   // AR закрили, поки чекали дозволу
      S.stream = stream;
      const v = $("#arVideo");
      v.srcObject = S.stream;
      await v.play().catch(() => {});
    } catch {
      if (!S) return;
      setMode("arrow");
      $("#arMode").disabled = true;
      status("Камера недоступна — режим стрілки");
    }
    loop();
  }

  function close() {
    if (!S) return;
    cancelAnimationFrame(S.raf);
    S.stream?.getTracks().forEach((t) => t.stop());
    if (S.watch != null) navigator.geolocation.clearWatch(S.watch);
    window.removeEventListener("deviceorientationabsolute", onOri);
    window.removeEventListener("deviceorientation", onOri);
    $("#ar")?.remove();
    document.documentElement.classList.remove("ar-open");
    S = null;
  }

  function status(t) { const e = $("#arStat"); if (e) e.textContent = t; }

  function onPos(p) {
    if (!S) return;
    const fix = { lat: p.coords.latitude, lng: p.coords.longitude, acc: Math.max(2, p.coords.accuracy || 10), t: Date.now() };
    S.fixes.push(fix);
    S.fixes = S.fixes.filter((f) => Date.now() - f.t < 20000).slice(-20);
    if (S.frozen) { S.pos = { ...S.frozen, acc: fix.acc }; return; }

    // стоїмо? — усі фікси за останні 20 с у межах ~3 м один від одного
    const F = localFrame(fix.lat, fix.lng);
    const xy = S.fixes.map((f) => ({ ...f, p: F.to(f.lat, f.lng) }));
    const still = xy.length >= 3 && xy.every((f) => Math.hypot(f.p[0], f.p[1]) < 3);
    let est = fix;
    if (still) {
      // зважене середнє (вага 1/acc²): прибирає стрибки, коли стоїмо на точці
      let sw = 0, sx = 0, sy = 0, sa = 0;
      for (const f of xy) { const w = 1 / (f.acc * f.acc); sw += w; sx += f.p[0] * w; sy += f.p[1] * w; sa += f.acc * w; }
      const [lat, lng] = F.from(sx / sw, sy / sw);
      est = { lat, lng, acc: Math.max(1.5, (sa / sw) / Math.sqrt(xy.length)) };
    }
    // мертва зона: не рухаємо картинку через зміну меншу, ніж третина точності
    if (S.shown) {
      const [dx, dy] = localFrame(S.shown.lat, S.shown.lng).to(est.lat, est.lng);
      if (Math.hypot(dx, dy) < Math.max(0.7, est.acc * 0.3)) { S.pos = { ...S.shown, acc: est.acc }; return; }
    }
    S.shown = { lat: est.lat, lng: est.lng };
    S.pos = est;
    if (S.xray) loadAround();
  }

  function toggleFreeze() {
    if (!S.pos) return;
    S.frozen = S.frozen ? null : { lat: S.pos.lat, lng: S.pos.lng };
    $("#arFreeze").textContent = S.frozen ? "📌 Зафіксовано" : "📌 Зафіксувати";
    $("#arFreeze").classList.toggle("on", !!S.frozen);
  }

  // Матриця повороту «пристрій → світ» (W3C: Rz(alpha)·Rx(beta)·Ry(gamma)).
  // Світ: x — схід, y — північ, z — угору. Пристрій: x — вправо, y — вгору екрана, z — з екрана.
  function deviceBasis(alpha, beta, gamma) {
    const a = alpha * D2R, b = beta * D2R, g = gamma * D2R;
    const cA = Math.cos(a), sA = Math.sin(a), cB = Math.cos(b), sB = Math.sin(b), cG = Math.cos(g), sG = Math.sin(g);
    // стовпці R: образи осей пристрою у світі
    const X = [cA * cG - sA * sB * sG, sA * cG + cA * sB * sG, -cB * sG];
    const Y = [-sA * cB, cA * cB, sB];
    const Z = [cA * sG + sA * sB * cG, sA * sG - cA * sB * cG, cB * cG];
    // задня камера дивиться вздовж -Z пристрою; «верх» кадру у портреті — +Y; «право» — +X
    return { fw: [-Z[0], -Z[1], -Z[2]], rt: X, up: Y };
  }
  const yawOf = (v) => (Math.atan2(v[0], v[1]) / D2R + 360) % 360;   // азимут вектора (0 — північ)
  const rotZ = (v, deg) => { const r = deg * D2R, c = Math.cos(r), s = Math.sin(r); return [v[0] * c + v[1] * s, -v[0] * s + v[1] * c, v[2]]; };

  function onOri(e) {
    if (!S || e.alpha == null) return;
    const isAbs = e.absolute || e.type === "deviceorientationabsolute";
    if (e.type === "deviceorientation" && S.absolute) return;   // Android: є краще джерело
    if (e.type === "deviceorientationabsolute") S.absolute = true;

    // 1) плавна орієнтація з гіроскопа/акселерометра (без тремтіння компаса)
    const basis = deviceBasis(e.alpha, e.beta || 0, e.gamma || 0);
    S.rawBasis = basis;
    const gyroYaw = yawOf(basis.fw);

    // 2) абсолютний курс компаса — лише щоб повільно поправляти дрейф гіроскопа
    let compass = null;
    if (typeof e.webkitCompassHeading === "number" && e.webkitCompassHeading >= 0) {
      compass = e.webkitCompassHeading;                   // iOS: магнітний курс
      S.headAcc = e.webkitCompassAccuracy;
    } else if (isAbs) {
      compass = gyroYaw;                                  // Android: alpha вже від півночі
    }
    if (compass != null) {
      compass = (compass + declination(S.pos?.lng ?? 30) + 360) % 360;   // → географічна північ
      const diff = wrap180(compass - gyroYaw);
      const bad = S.headAcc != null && (S.headAcc < 0 || S.headAcc > 25);
      if (S.yawOff == null) S.yawOff = diff;
      else if (!bad) {
        // комплементарний фільтр: швидко на старті (перша секунда), далі повільно
        // (0,3% за подію ≈ стала часу 7 с на 50 Гц) — шум компаса не проходить, дрейф гіроскопа знімається
        const k = S.oriCount < 50 ? 0.15 : 0.003;
        S.yawOff = wrap180(S.yawOff + wrap180(diff - S.yawOff) * k);
      }
    }
    S.oriCount = (S.oriCount || 0) + 1;
    S.pitch = Math.asin(Math.max(-1, Math.min(1, basis.fw[2]))) / D2R;
  }

  // Базис камери у світі з усіма поправками (компас + ручне калібрування)
  function cameraBasis() {
    if (!S.rawBasis || S.yawOff == null) return null;
    const d = S.yawOff + S.headOff;
    return { fw: rotZ(S.rawBasis.fw, d), rt: rotZ(S.rawBasis.rt, d), up: rotZ(S.rawBasis.up, d) };
  }
  const heading = () => { const b = cameraBasis(); return b ? yawOf(b.fw) : null; };

  // ------------------------------------------------------------ розрахунки
  function rel() {                                        // точки відносно користувача, м (схід, північ)
    if (!S.pos) return null;
    const F = localFrame(S.pos.lat, S.pos.lng);
    return S.pts.map((p) => {
      const [x, y] = F.to(p.lat, p.lng);
      const dx = x - S.posOff[0], dy = y - S.posOff[1];
      return { p, dx, dy, d: Math.hypot(dx, dy), b: (Math.atan2(dx, dy) / D2R + 360) % 360 };
    });
  }

  function camera(W, H) {
    const v = $("#arVideo");
    const vw = v.videoWidth || W, vh = v.videoHeight || H;
    const fv = (Math.max(vw, vh) / 2) / Math.tan(S.fovLong / 2 * D2R);
    const scale = Math.max(W / vw, H / vh);              // object-fit: cover
    return { f: fv * scale, ...cameraBasis() };
  }

  function project(cam, W, H, dx, dy) {
    const v = [dx, dy, -EYE];
    const z = v[0] * cam.fw[0] + v[1] * cam.fw[1] + v[2] * cam.fw[2];
    if (z < 0.5) return null;
    const x = v[0] * cam.rt[0] + v[1] * cam.rt[1];
    const y = v[0] * cam.up[0] + v[1] * cam.up[1] + v[2] * cam.up[2];
    return [W / 2 + cam.f * x / z, H / 2 - cam.f * y / z];
  }

  // ------------------------------------------------------------ малювання
  function loop() {
    if (!S) return;
    try { (S.mode === "cam" ? drawCam : drawArrow)(); } catch (e) { console.error(e); }
    updateStatus();
    S.raf = requestAnimationFrame(loop);
  }

  function updateStatus() {
    const parts = [];
    if (!S.pos) parts.push("Очікую GPS…");
    else parts.push(`GPS ±${Math.round(S.pos.acc)} м`);
    if (S.frozen) parts.push("позицію зафіксовано");
    if (heading() == null) parts.push(S.rawBasis ? "компас: чекаю…" : "компас: немає");
    else if (S.headAcc != null && (S.headAcc < 0 || S.headAcc > 25)) parts.push("компас ненадійний — опишіть телефоном вісімку");
    if (S.posOff[0] || S.posOff[1] || S.headOff) parts.push("калібровано");
    status(parts.join(" · "));
  }

  function drawCam() {
    const cv = $("#arCanvas");
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const W = cv.clientWidth, H = cv.clientHeight;
    if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const rp = rel();
    S.screen = []; S.xscreen = [];
    if (!rp) return;
    if (rp.length) {
      const near = rp.reduce((a, b) => (b.d < a.d ? b : a));
      if (S.target == null) S.target = near.p.n;
      showTarget(rp);
    }
    if (heading() == null) { hint(ctx, W, H, S.parcel ? "Немає компаса — перейдіть у режим «Стрілка»" : "Немає компаса"); return; }
    const cam = camera(W, H);
    if (S.xray) drawXray(ctx, cam, W, H);

    // межі: кожне ребро розбиваємо на відрізки, щоб лінія правильно «лягала» на землю
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    for (const ring of S.rings) {
      for (let i = 0; i < ring.length; i++) {
        const a = rp[ring[i]], b = rp[ring[(i + 1) % ring.length]];
        if (Math.min(a.d, b.d) > MAX_DIST) continue;
        const len = Math.hypot(b.dx - a.dx, b.dy - a.dy);
        const n = Math.max(2, Math.min(150, Math.ceil(len / 1.5)));
        let path = [];
        const flush = () => {
          if (path.length > 1) {
            for (const [w, c] of [[6, "rgba(0,0,0,.45)"], [3, "#ffd400"]]) {
              ctx.lineWidth = w; ctx.strokeStyle = c; ctx.beginPath();
              path.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
            }
          }
          path = [];
        };
        for (let k = 0; k <= n; k++) {
          const t = k / n;
          const s = project(cam, W, H, a.dx + (b.dx - a.dx) * t, a.dy + (b.dy - a.dy) * t);
          if (s && Math.abs(s[0]) < W * 4 && Math.abs(s[1]) < H * 4) path.push(s); else flush();
        }
        flush();
      }
    }

    // мітки: дальні першими, ближні поверх
    const order = rp.filter((r) => r.d <= MAX_DIST).sort((a, b) => b.d - a.d);
    for (const r of order) {
      const s = project(cam, W, H, r.dx, r.dy);
      if (!s || s[0] < -40 || s[0] > W + 40 || s[1] < -40 || s[1] > H + 40) continue;
      const isT = r.p.n === S.target;
      const rad = Math.max(7, Math.min(18, 18 - r.d / 12)) + (isT ? 3 : 0);
      ctx.beginPath(); ctx.arc(s[0], s[1], rad, 0, Math.PI * 2);
      ctx.fillStyle = isT ? "#ff3b30" : "#ffd400"; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = "#fff"; ctx.stroke();
      ctx.fillStyle = isT ? "#fff" : "#111"; ctx.font = `700 ${Math.round(rad * 0.95)}px system-ui`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(r.p.n, s[0], s[1] + 0.5);
      const label = `Т${r.p.n} · ${fmtD(r.d)}`;
      ctx.font = "600 13px system-ui"; ctx.lineWidth = 4; ctx.strokeStyle = "rgba(0,0,0,.7)";
      ctx.strokeText(label, s[0], s[1] - rad - 11); ctx.fillStyle = "#fff"; ctx.fillText(label, s[0], s[1] - rad - 11);
      S.screen.push({ n: r.p.n, x: s[0], y: s[1], r: rad + 12 });
    }

    // ціль поза кадром — стрілка біля краю екрана
    const t = rp.find((r) => r.p.n === S.target);
    if (t && !S.xray) {
      const da = wrap180(t.b - heading());
      const s = project(cam, W, H, t.dx, t.dy);
      if (!s || s[0] < 0 || s[0] > W || Math.abs(da) > 70) {
        const left = da < 0, y = H * 0.45, x = left ? 26 : W - 26;
        ctx.save(); ctx.translate(x, y); ctx.rotate(left ? Math.PI : 0);
        ctx.beginPath(); ctx.moveTo(14, 0); ctx.lineTo(-10, -14); ctx.lineTo(-10, 14); ctx.closePath();
        ctx.fillStyle = "#ff3b30"; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke(); ctx.restore();
        ctx.font = "700 13px system-ui"; ctx.textAlign = left ? "left" : "right"; ctx.fillStyle = "#fff";
        ctx.lineWidth = 4; ctx.strokeStyle = "rgba(0,0,0,.7)";
        const lbl = `Т${t.p.n} ${left ? "←" : "→"} ${Math.abs(Math.round(da))}°`;
        ctx.strokeText(lbl, left ? 46 : W - 46, y); ctx.fillText(lbl, left ? 46 : W - 46, y);
      }
    }
  }

  function hint(ctx, W, H, t) {
    ctx.font = "600 15px system-ui"; ctx.textAlign = "center"; ctx.fillStyle = "#fff";
    ctx.lineWidth = 4; ctx.strokeStyle = "rgba(0,0,0,.7)";
    ctx.strokeText(t, W / 2, H * 0.4); ctx.fillText(t, W / 2, H * 0.4);
  }

  function drawArrow() {
    const rp = rel();
    if (!rp) { $("#arBig").textContent = "—"; $("#arSub").textContent = "Очікую GPS…"; return; }
    if (S.target == null) S.target = rp.reduce((a, b) => (b.d < a.d ? b : a)).p.n;
    const t = rp.find((r) => r.p.n === S.target);
    showTarget(rp);
    const h = heading();
    const rot = h == null ? t.b : wrap180(t.b - h);
    $("#arArrow svg").style.transform = `rotate(${rot}deg)`;
    $("#arBig").textContent = t.d < 3 ? `Т${t.p.n} · ви на місці ✓` : `Т${t.p.n} · ${fmtD(t.d)}`;
    $("#arSub").textContent = h == null ? `азимут ${Math.round(t.b)}° від півночі (компас недоступний)` : `азимут ${Math.round(t.b)}°`;
    const isNear = t.d < 3;
    if (isNear && !S.near) navigator.vibrate?.(250);
    S.near = isNear;
    $("#arArrow").classList.toggle("near", isNear);
  }

  function showTarget(rp) {
    const t = rp.find((r) => r.p.n === S.target);
    if (!t) return;
    const nearest = rp.reduce((a, b) => (b.d < a.d ? b : a));
    $("#arTarget").innerHTML = `Ціль: <b>Т${t.p.n}</b> — ${fmtD(t.d)}, азимут ${Math.round(t.b)}°`
      + (nearest.p.n !== t.p.n ? ` · найближча Т${nearest.p.n} (${fmtD(nearest.d)})` : "");
  }

  function setMode(m) {
    S.mode = m;
    $("#arArrow").hidden = m !== "arrow";
    $("#arCanvas").hidden = $("#arCross").hidden = m !== "cam";
    $("#arVideo").style.opacity = m === "cam" ? 1 : 0.25;
    $("#arMode").textContent = m === "cam" ? "🧭 Стрілка" : "📷 Камера";
  }

  function onTapCanvas(e) {
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const hit = (S.screen || []).filter((s) => Math.hypot(s.x - x, s.y - y) < s.r).sort((a, b) => Math.hypot(a.x - x, a.y - y) - Math.hypot(b.x - x, b.y - y))[0];
    if (hit) { S.target = hit.n; return; }
    const xh = (S.xscreen || []).filter((s) => Math.abs(s.x - x) < s.w / 2 + 8 && Math.abs(s.y - y) < 16)[0];
    if (xh) openParcel(xh.cad);
  }

  function openParcel(cad) {
    close();
    window.KDAR.onOpenParcel?.(cad);
  }

  // ------------------------------------------------------------ рентген: ділянки навколо
  const XZ = 16, XR = 300;                 // масштаб тайлів і радіус показу, м
  async function loadAround() {
    if (!S || !S.pos || S.loading || !window.KDMVT) return;
    if (S.loadedAt && Math.hypot(...localFrame(S.loadedAt.lat, S.loadedAt.lng).to(S.pos.lat, S.pos.lng)) < 120) return;
    S.loading = true;
    const at = { lat: S.pos.lat, lng: S.pos.lng };
    try {
      const n = 2 ** XZ;
      const tx = Math.floor((at.lng + 180) / 360 * n);
      const ty = Math.floor((1 - Math.log(Math.tan(at.lat * D2R) + 1 / Math.cos(at.lat * D2R)) / Math.PI) / 2 * n);
      const F0 = S.F0 || localFrame(at.lat, at.lng);
      const pieces = [];
      const jobs = [];
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) jobs.push([tx + dx, ty + dy]);
      await Promise.all(jobs.map(async ([x, y]) => {
        const r = await fetch(`/tiles/dzk2/${XZ}/${x}/${y}.pbf`, { credentials: "same-origin" });
        if (r.status !== 200) return;
        const lay = KDMVT.decode(new Uint8Array(await r.arrayBuffer()))["parcels_ua"];
        if (!lay) return;
        const ext = lay.extent;
        for (const f of lay.features) {
          if (f.type !== 3 || !f.props.cadnum) continue;
          const rings = [], edges = [];
          let cx = 0, cy = 0, cn = 0;
          for (const ring of f.rings) {
            const pts = ring.map(([px, py]) => {
              const lng = (x + px / ext) / n * 360 - 180;
              const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * (y + py / ext) / n))) / D2R;
              return F0.to(lat, lng);
            });
            rings.push(pts);
            for (let i = 0; i < ring.length - 1; i++) {
              const [ax, ay] = ring[i], [bx, by] = ring[i + 1];
              // ребра на межі тайла — штучні, їх не малюємо
              const border = (ax <= 0 && bx <= 0) || (ax >= ext && bx >= ext) || (ay <= 0 && by <= 0) || (ay >= ext && by >= ext);
              edges.push({ a: pts[i], b: pts[i + 1], border });
              cx += pts[i][0]; cy += pts[i][1]; cn++;
            }
          }
          if (!cn) continue;
          pieces.push({ cad: f.props.cadnum, props: f.props, rings, edges, c: [cx / cn, cy / cn] });
        }
      }));
      S.F0 = F0; S.around = pieces; S.loadedAt = at;
    } catch (e) { console.warn("рентген:", e); }
    finally { S.loading = false; }
  }

  function userXY() { return S.F0 ? (([x, y]) => [x - S.posOff[0], y - S.posOff[1]])(S.F0.to(S.pos.lat, S.pos.lng)) : null; }

  const fmtArea = (a) => (a == null ? "" : a < 1 ? `${(a * 100).toFixed(1).replace(".", ",")} сот.` : `${String(a).replace(".", ",")} га`);
  const shortCad = (c) => c.slice(-8);

  function drawXray(ctx, cam, W, H) {
    const U = userXY();
    if (!U) { if (!S.around && !S.loading) loadAround(); return; }
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    const labels = [];
    for (const p of S.around || []) {
      const d = Math.hypot(p.c[0] - U[0], p.c[1] - U[1]);
      if (d > XR) continue;
      if (S.parcel && p.cad === S.parcel.cadnum) continue;   // своя ділянка вже намальована жовтим
      for (const e of p.edges) {
        if (e.border) continue;
        const ax = e.a[0] - U[0], ay = e.a[1] - U[1], bx = e.b[0] - U[0], by = e.b[1] - U[1];
        if (Math.min(Math.hypot(ax, ay), Math.hypot(bx, by)) > XR) continue;
        const len = Math.hypot(bx - ax, by - ay);
        const n = Math.max(1, Math.min(40, Math.ceil(len / 3)));
        let path = [];
        const flush = () => {
          if (path.length > 1) {
            ctx.lineWidth = 3.5; ctx.strokeStyle = "rgba(0,0,0,.4)"; ctx.beginPath();
            path.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
            ctx.lineWidth = 1.5; ctx.strokeStyle = "#22d3ee"; ctx.stroke();
          }
          path = [];
        };
        for (let k = 0; k <= n; k++) {
          const t = k / n;
          const s = project(cam, W, H, ax + (bx - ax) * t, ay + (by - ay) * t);
          if (s && Math.abs(s[0]) < W * 4 && Math.abs(s[1]) < H * 4) path.push(s); else flush();
        }
        flush();
      }
      if (d < 80) {
        const s = project(cam, W, H, p.c[0] - U[0], p.c[1] - U[1]);
        if (s && s[0] > -60 && s[0] < W + 60 && s[1] > 0 && s[1] < H) labels.push({ p, s, d });
      }
    }
    // ближні першими; підпис, що перекриває вже поставлений, пропускаємо
    labels.sort((a, b) => a.d - b.d);
    const placed = [];
    for (const l of labels) {
      if (placed.some((r) => Math.abs(r[0] - l.s[0]) < 95 && Math.abs(r[1] - l.s[1]) < 34)) continue;
      placed.push(l.s);
      const t1 = "…" + shortCad(l.p.cad), t2 = fmtArea(l.p.props.area);
      ctx.font = "600 12px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const w = Math.max(ctx.measureText(t1).width, ctx.measureText(t2).width) + 14;
      ctx.fillStyle = "rgba(0,0,0,.55)";
      roundRect(ctx, l.s[0] - w / 2, l.s[1] - 16, w, 32, 8); ctx.fill();
      ctx.fillStyle = "#fff"; ctx.fillText(t1, l.s[0], l.s[1] - 7); ctx.font = "500 11px system-ui"; ctx.fillText(t2, l.s[0], l.s[1] + 8);
      S.xscreen.push({ cad: l.p.cad, x: l.s[0], y: l.s[1], w });
    }
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  // ------------------------------------------------------------ калібрування
  function openCal() {
    const sh = $("#arCalSheet");
    const t = S.target;
    sh.innerHTML = `
      <b>Калібрування за точкою Т${t ?? "?"}</b>
      <button class="ar-btn wide" data-c="pos">📍 Я стою на точці Т${t}</button>
      <button class="ar-btn wide" data-c="head" ${heading() == null ? "disabled" : ""}>🎯 Т${t} зараз у центрі прицілу</button>
      <label class="ar-fov">Кут огляду камери: <input type="range" min="50" max="85" value="${S.fovLong}" id="arFov"> <span id="arFovV">${S.fovLong}°</span></label>
      <div class="ar-row"><button class="ar-btn wide" data-c="reset">Скинути</button><button class="ar-btn wide" data-c="close">Готово</button></div>
      <p class="ar-note">«Я стою на точці» прибирає похибку GPS, «у центрі прицілу» — похибку компаса. Робіть, коли ціль видно на місцевості (кілочок, кут паркану).</p>`;
    sh.hidden = false;
    sh.querySelectorAll("[data-c]").forEach((b) => (b.onclick = () => {
      const c = b.dataset.c;
      const rp = rel();
      const tt = rp && rp.find((r) => r.p.n === S.target);
      if (c === "pos" && tt) { S.posOff = [S.posOff[0] + tt.dx, S.posOff[1] + tt.dy]; if (!S.frozen) toggleFreeze(); }
      if (c === "head" && tt && heading() != null) S.headOff = wrap180(S.headOff + wrap180(tt.b - heading()));
      if (c === "reset") { S.posOff = [0, 0]; S.headOff = 0; }
      sh.hidden = true;
    }));
    $("#arFov").oninput = (e) => {
      S.fovLong = +e.target.value; $("#arFovV").textContent = S.fovLong + "°";
      try { localStorage.setItem("kd_ar_fov", S.fovLong); } catch {}
    };
  }

  addEventListener("keydown", (e) => { if (e.key === "Escape" && S) close(); });
  window.KDAR = { open, close, corners, _state: () => S, onOpenParcel: null };
})();
