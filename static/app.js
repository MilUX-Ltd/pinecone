'use strict';
(async () => {
  const $ = (s) => document.querySelector(s);
  const pad = (n) => String(n).padStart(2, '0');
  const fmtUTC = (ms) => { const d = new Date(ms); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`; };
  const fmtDay = (ms) => new Date(ms).toISOString().slice(0, 10);
  const fmtLocal = (ms) => new Date(ms).toLocaleTimeString('en-GB', { timeZone: 'Europe/London', hour12: false }) + ' London';
  const fmtAge = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); if (s < 60) return s + ' s'; const m = Math.floor(s / 60); if (m < 60) return `${m} m ${pad(s % 60)} s`; return `${Math.floor(m / 60)} h ${pad(m % 60)} m`; };
  // minutes are "min", never "m": on the proposals panel they sit beside metres (review note N3)
  const fmtDur = (ms) => { const m = Math.round(ms / 60000); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${pad(m % 60)} min`; };

  // Leaflet renders its attribution control with innerHTML, and a map's name, attribution and URL
  // all come from files and services this box did not write. Escape before handing anything over.
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

  const PALETTE = ['#4363d8', '#e6194b', '#3cb44b', '#f58231', '#42d4f4', '#f032e6', '#bfef45', '#fabed4',
                   '#469990', '#dcbeff', '#9a6324', '#ffe119', '#aaffc3', '#800000', '#808000', '#000075'];
  const shapeFor = (platform) => {
    const p = (platform || '').toLowerCase();
    if (p.includes('meshtastic')) return 'triangle';
    if (p.includes('atak')) return 'circle';
    if (p.includes('wintak')) return 'square';
    if (p.includes('cloudtak') || p.includes('webtak')) return 'diamond';
    return 'hex';
  };
  const SPEEDS = [1, 2, 5, 10, 20, 50, 100, 300];

  // ---------- load ----------
  const qs = new URLSearchParams(location.search);
  const bundles = await fetch('/bundles').then(r => r.json());
  const sel = $('#bundle');
  for (const b of bundles) {
    const o = document.createElement('option');
    o.value = b.name;
    o.textContent = b.capped_at ? `${b.name} (first ${b.capped_at} of ${b.reports})` : b.name;
    sel.appendChild(o);
  }
  // A file bundle is measured in bytes and an archive window in reports, so compare them on
  // whichever each one carries. Subtracting a field that is not there yields NaN, and a NaN
  // comparator leaves the default selection resting on the sort's own stability.
  const size = (b) => (typeof b.bytes === 'number' ? b.bytes : (typeof b.reports === 'number' ? b.reports : 0));
  const biggest = bundles.slice().sort((a, b) => size(b) - size(a))[0];
  const name = qs.get('bundle') || (biggest && biggest.name);
  if (!name) { $('#winmsg').textContent = 'This box has nothing to replay yet. It has not recorded anything from the server.'; return; }
  sel.value = name;
  sel.onchange = () => { location.search = '?bundle=' + encodeURIComponent(sel.value); };

  // A debrief is run over a period somebody names, which is almost never one of the three fixed
  // windows. `from` and `to` in the address are the window, so a replay can be sent to someone.
  let qsFrom = Number(qs.get('from')), qsTo = Number(qs.get('to'));
  const qsAt = Number(qs.get('at'));
  // a moment's time is a millisecond timestamp between 2000 and 2100; anything else is a bad link
  const atGiven = Number.isFinite(qsAt) && qsAt >= 946684800000 && qsAt <= 4133980800000;
  if (qs.has('at') && !atGiven) $('#winmsg').textContent = 'That link does not carry a time this box can read. Showing the default window instead.';
  // A moment handed to somebody is a time; if the window in the address does not hold it, or there
  // is no window, put half an hour either side of it.
  if (atGiven && !(Number.isFinite(qsFrom) && Number.isFinite(qsTo) && qsFrom <= qsAt && qsAt < qsTo)) {
    qsFrom = qsAt - 30 * 60000; qsTo = qsAt + 30 * 60000;
  }
  const chosen = Number.isFinite(qsFrom) && Number.isFinite(qsTo) && qsFrom > 0 && qsTo > qsFrom;
  const url = chosen
    ? `/bundle.json?name=archive&start=${qsFrom}&end=${qsTo}`
    : '/bundle.json?name=' + encodeURIComponent(name);
  const res = await fetch(url);
  if (!res.ok) {
    $('#winmsg').textContent = 'That window could not be replayed: ' + (await res.text()) + ' Try one of the quick windows above.';
    return;
  }
  const bundle = await res.json();
  const W0 = bundle.window.start, W1 = bundle.window.end;
  const tmeta = await fetch('/tiles/meta').then(r => r.json());

  // ---------- map ----------
  const map = L.map('map', { zoomControl: true, zoomSnap: 0.25, wheelPxPerZoomLevel: 90 });
  // the basemap is whatever this box last chose; the settings menu swaps it live
  let tileLayer = null;
  const layerFor = (m) => m.url
    ? L.tileLayer(m.url, { maxNativeZoom: (m.meta && +m.meta.maxzoom) || 19, maxZoom: 20, attribution: esc(m.attribution || m.url) })
    : L.tileLayer('/tiles/{z}/{x}/{y}.png', {
        maxNativeZoom: +(m.meta && m.meta.maxzoom) || 16, minNativeZoom: +(m.meta && m.meta.minzoom) || 8,
        maxZoom: 20, attribution: m.available ? esc((m.meta && m.meta.attribution) || (m.meta && m.meta.name) || '') : 'no basemap loaded',
      });
  const useMeta = (m) => { const next = layerFor(m); next.addTo(map); if (tileLayer) map.removeLayer(tileLayer); tileLayer = next; };
  // Nothing is chosen for you (Spec 011). With no choice the page tries one OpenStreetMap tile:
  // if the browser has a network the online map is drawn, and the picker says it is in use
  // without a choice having been made; if not, the map is blank and asks for a pack. The box
  // itself never fetches a tile.
  const OSM = { url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', attribution: '\u00a9 OpenStreetMap contributors', meta: { maxzoom: 19 } };
  let usingOsmByDefault = false;
  const nomap = $('#nomap');
  const osmReachable = () => new Promise((resolve) => {
    const img = new Image(); let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    setTimeout(() => finish(false), 4000);
    img.onload = () => finish(true); img.onerror = () => finish(false);
    img.src = 'https://tile.openstreetmap.org/0/0/0.png?' + Date.now();
  });
  async function settleBasemap(m) {
    nomap.hidden = true; usingOsmByDefault = false;
    if (m.url || m.available) { useMeta(m); return; }
    if (await osmReachable()) { usingOsmByDefault = true; useMeta(OSM); return; }
    if (tileLayer) { map.removeLayer(tileLayer); tileLayer = null; }
    nomap.hidden = false;
  }
  const basemapLabel = tmeta.url ? tmeta.url : tmeta.available ? tmeta.meta.name : 'none chosen; OpenStreetMap when the browser has a network, else the page asks';
  const renderer = L.canvas({ padding: 0.5 });

  // ---------- the ground it happened on ----------
  //
  // A replay with no plan behind it is narration. Overlays draw beneath the tracks, in their own
  // pane, so the movement is read against the boundaries and phase lines it was supposed to happen
  // within. Leaflet vector layers rather than the track canvas: there are few of them, they do not
  // move, and they want their own hit-testing for labels.
  map.createPane('ground');
  map.getPane('ground').style.zIndex = 350;   // above tiles (200), below the track canvas (400)

  const REPORTED_COLOUR = '#e06c5a';
  const GROUND_COLOUR = '#9ad0ff';
  const overlayState = { packs: [], layers: [], off: new Set() };

  // Which overlays are switched off is the operator's, and survives a reload. Per box, per browser.
  try {
    const saved = JSON.parse(localStorage.getItem('pinecone.overlays.off') || '[]');
    if (Array.isArray(saved)) overlayState.off = new Set(saved.map(String));
  } catch { /* a browser with storage disabled still gets every overlay, which is the safe way round */ }
  const rememberOff = () => {
    try { localStorage.setItem('pinecone.overlays.off', JSON.stringify([...overlayState.off])); }
    catch { /* not being able to remember is not a reason to fail */ }
  };

  const shapeKey = (packUid, overlayPath, i) => `${packUid}|${overlayPath}|${i}`;

  function layerForShape(s, key) {
    const reported = Boolean(s.reported);
    const colour = reported ? REPORTED_COLOUR : GROUND_COLOUR;
    const common = { pane: 'ground', color: colour, weight: reported ? 2 : 2.5, interactive: true };
    let layer = null;
    if (s.kind === 'polygon') layer = L.polygon(s.coordinates, { ...common, fillColor: colour, fillOpacity: 0.06 });
    else if (s.kind === 'line') layer = L.polyline(s.coordinates, common);
    else if (s.kind === 'point') {
      const [lat, lon] = s.coordinates[0];
      layer = L.circleMarker([lat, lon], { ...common, radius: 6, fillColor: colour, fillOpacity: 0.35, dashArray: reported ? '3 3' : null });
    }
    if (!layer) return null;
    // Everything here is text from a file somebody else made. Leaflet puts a STRING through
    // innerHTML and only appends a node as a node, so this builds a node. The same repository
    // already paid for this once, when Leaflet rendered its attribution control the same way, and
    // a comment claiming a string was "bound as text" is exactly how it happens twice.
    const parts = [s.label || '(unnamed)'];
    if (reported) parts.push('reported, not observed');
    // CoT's sentinel for "accuracy unknown" is a very large number; printing it as an accuracy
    // would be the same unsupported claim this labelling exists to prevent.
    if (reported && s.ce && s.ce < 100000) parts.push(`accuracy given as ${Math.round(s.ce)} m`);
    else if (reported) parts.push('no accuracy given');
    if (s.remarks) parts.push(s.remarks);
    if (s.window_unreadable) parts.push('this overlay declares a time window that could not be read, so it is shown throughout');
    else if (s.undated) parts.push('applies throughout: this overlay carries no time window');
    const tip = document.createElement('span');
    tip.textContent = parts.join(' \u00b7 ');
    layer.bindTooltip(tip, { sticky: true });
    layer.pinecone = { key, shape: s };
    return layer;
  }

  async function loadGround() {
    let packs = [];
    try { packs = await fetch('/api/packs').then(r => r.json()); } catch { return; }
    overlayState.packs = packs || [];
    for (const pack of overlayState.packs) {
      let overlays = [];
      try { overlays = await fetch(`/api/packs/${encodeURIComponent(pack.uid)}/overlays`).then(r => r.json()); }
      catch { continue; }
      for (const o of overlays) {
        const group = L.layerGroup([], { pane: 'ground' });
        const shapes = [];
        (o.shapes || []).forEach((s, i) => {
          const key = shapeKey(pack.uid, o.path, i);
          const layer = layerForShape(s, key);
          if (layer) { group.addLayer(layer); shapes.push({ shape: s, layer }); }
        });
        overlayState.layers.push({ pack, overlay: o, group, shapes, key: `${pack.uid}|${o.path}` });
      }
    }
    if (overlayState.layers.length) { buildGroundPanel(); applyGround(T); }
  }

  // A static overlay lies: the picture changed during the exercise. An overlay with a window is
  // drawn only while the replay clock is inside it, and one without a window applies throughout
  // and says so, rather than having a window invented for it.
  const appliesAt = (s, when) => {
    if (s.undated) return true;
    if (s.begin_ms != null && when < s.begin_ms) return false;
    if (s.end_ms != null && when >= s.end_ms) return false;
    return true;
  };

  function applyGround(when) {
    for (const entry of overlayState.layers) {
      const wanted = !overlayState.off.has(entry.key);
      if (wanted && !map.hasLayer(entry.group)) entry.group.addTo(map);
      if (!wanted && map.hasLayer(entry.group)) map.removeLayer(entry.group);
      if (!wanted) continue;
      for (const { shape, layer } of entry.shapes) {
        const on = appliesAt(shape, when);
        if (on && !entry.group.hasLayer(layer)) entry.group.addLayer(layer);
        if (!on && entry.group.hasLayer(layer)) entry.group.removeLayer(layer);
      }
    }
  }

  function buildGroundPanel() {
    const host = $('#ground');
    if (!host) return;
    host.hidden = false;
    const list = $('#groundlist');
    list.textContent = '';
    for (const entry of overlayState.layers) {
      const row = document.createElement('label');
      row.className = 'inl';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = !overlayState.off.has(entry.key);
      box.onchange = () => {
        if (box.checked) overlayState.off.delete(entry.key); else overlayState.off.add(entry.key);
        rememberOff();
        applyGround(T);
      };
      const name = document.createElement('span');
      // Three states, not two: timed, undated, and declared-but-unreadable. The third used to be
      // labelled "timed" while the clock governed nothing, which implied the opposite of the truth.
      const unreadable = entry.shapes.some(s => s.shape.window_unreadable);
      const dated = entry.shapes.some(s => !s.shape.undated && !s.shape.window_unreadable);
      const reported = entry.shapes.some(s => s.shape.reported);
      name.textContent = ` ${entry.overlay.name} (${entry.shapes.length})`
        + (dated ? ' \u00b7 timed' : '')
        + (unreadable ? ' \u00b7 window unreadable, shown throughout' : '')
        + (reported ? ' \u00b7 reported' : '');
      row.append(box, name);
      list.appendChild(row);
    }
  }

  // ---------- tracks ----------
  const tracks = bundle.tracks.map((t, i) => ({
    meta: t, pts: t.points, times: Float64Array.from(t.points, p => p[0]),
    colour: PALETTE[i % PALETTE.length], shape: shapeFor(t.platform),
    visible: true, gapMs: 0, idx: -1, runs: [], runOf: null, faint: [], bold: null, boldRun: -1, boldIdx: -1, bm: null, row: null,
  }));
  // The bundle's threshold when it carries one; otherwise the rule the player always used, with
  // the clamp it always had. A bundle built before honest time has no `time` facts, and an
  // unclamped fallback gave a bursty ATAK handset a two-second threshold on those.
  const gapFor = (t) => {
    const v = $('#gap').value;
    if (v !== 'auto') return +v;
    const bundled = t.meta.time && t.meta.time.dropout_threshold_ms;
    if (bundled) return bundled;
    const mi = t.meta.median_interval_ms || 60000;
    return Math.min(Math.max(4 * mi, 90000), 3600000);
  };
  function computeRuns(t) {
    t.gapMs = gapFor(t); t.runs = []; t.runOf = new Int32Array(t.pts.length);
    let s = 0;
    for (let i = 1; i <= t.pts.length; i++) {
      if (i === t.pts.length || t.times[i] - t.times[i - 1] > t.gapMs) {
        t.runs.push([s, i - 1]);
        for (let k = s; k < i; k++) t.runOf[k] = t.runs.length - 1;
        s = i;
      }
    }
  }
  function buildLines(t) {
    t.faint.forEach(l => l.remove()); t.faint = [];
    if (t.bold) { t.bold.remove(); t.bold = null; }
    if (!t.visible) return;
    if ($('#full').checked) for (const [a, b] of t.runs) {
      if (b > a) t.faint.push(L.polyline(t.pts.slice(a, b + 1).map(p => [p[1], p[2]]),
        { renderer, color: t.colour, weight: 2, opacity: 0.3, interactive: false }).addTo(map));
    }
    t.bold = L.polyline([], { renderer, color: t.colour, weight: 3.5, opacity: 0.95, interactive: false }).addTo(map);
    t.boldRun = -1; t.boldIdx = -1;
  }
  function idxAt(t, T) {                     // largest i with times[i] <= T, else -1
    let lo = 0, hi = t.times.length - 1, ans = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (t.times[m] <= T) { ans = m; lo = m + 1; } else hi = m - 1; }
    return ans;
  }
  function updateBold(t) {
    if (!t.bold) return;
    if (t.idx < 0) { if (t.boldIdx !== -1) { t.bold.setLatLngs([]); t.boldIdx = -1; t.boldRun = -1; } return; }
    const run = t.runOf[t.idx], a = t.runs[run][0], lim = +$('#trail').value;
    if (lim) {                                  // bounded trail: rebuild from the first point inside the window
      let a0 = a; while (a0 < t.idx && t.times[a0] < T - lim) a0++;
      t.bold.setLatLngs(t.pts.slice(a0, t.idx + 1).map(p => [p[1], p[2]]));
      t.boldRun = run; t.boldIdx = t.idx; return;
    }
    if (run === t.boldRun && t.idx === t.boldIdx + 1) { const p = t.pts[t.idx]; t.bold.addLatLng([p[1], p[2]]); }
    else if (run !== t.boldRun || t.idx !== t.boldIdx) t.bold.setLatLngs(t.pts.slice(a, t.idx + 1).map(p => [p[1], p[2]]));
    t.boldRun = run; t.boldIdx = t.idx;
  }
  const allBounds = () => {                 // fit to the 3rd..97th percentile so one wild fix does not shrink the site to a dot
    const lats = [], lons = [];
    for (const t of tracks) if (t.visible) for (const p of t.pts) { lats.push(p[1]); lons.push(p[2]); }
    if (!lats.length) return L.latLngBounds([]);
    lats.sort((a, b) => a - b); lons.sort((a, b) => a - b);
    const q = (arr, f) => arr[Math.min(arr.length - 1, Math.max(0, Math.floor(f * arr.length)))];
    return L.latLngBounds([[q(lats, 0.03), q(lons, 0.03)], [q(lats, 0.97), q(lons, 0.97)]]);
  };

  // ---------- marker layer (one canvas, drawn every frame) ----------
  function drawShape(ctx, shape, x, y, r) {
    ctx.beginPath();
    if (shape === 'circle') ctx.arc(x, y, r, 0, Math.PI * 2);
    else if (shape === 'triangle') { ctx.moveTo(x, y - r * 1.15); ctx.lineTo(x + r * 1.1, y + r * 0.85); ctx.lineTo(x - r * 1.1, y + r * 0.85); ctx.closePath(); }
    else if (shape === 'square') ctx.rect(x - r * 0.9, y - r * 0.9, r * 1.8, r * 1.8);
    else if (shape === 'diamond') { ctx.moveTo(x, y - r * 1.2); ctx.lineTo(x + r * 1.2, y); ctx.lineTo(x, y + r * 1.2); ctx.lineTo(x - r * 1.2, y); ctx.closePath(); }
    else { for (let i = 0; i < 6; i++) { const a = Math.PI / 3 * i; const px = x + r * 1.1 * Math.cos(a), py = y + r * 1.1 * Math.sin(a); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); } ctx.closePath(); }
  }
  const MarkerLayer = L.Layer.extend({
    onAdd(m) {
      this._m = m;
      this._c = L.DomUtil.create('canvas', 'pc-markers leaflet-zoom-animated');
      m.getPanes().markerPane.appendChild(this._c);
      this._ctx = this._c.getContext('2d');
      m.on('moveend zoomend resize', this._reset, this);
      m.on('zoomanim', this._zoomAnim, this);
      this._reset();
    },
    onRemove(m) { m.off('moveend zoomend resize', this._reset, this); m.off('zoomanim', this._zoomAnim, this); L.DomUtil.remove(this._c); },
    _reset() {
      const size = this._m.getSize(), dpr = window.devicePixelRatio || 1;
      this._tl = this._m.containerPointToLayerPoint([0, 0]);
      L.DomUtil.setPosition(this._c, this._tl);
      this._c.width = size.x * dpr; this._c.height = size.y * dpr;
      this._c.style.width = size.x + 'px'; this._c.style.height = size.y + 'px';
      this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    },
    _zoomAnim(e) {
      const scale = this._m.getZoomScale(e.zoom);
      const off = this._m._latLngBoundsToNewLayerBounds(this._m.getBounds(), e.zoom, e.center).min;
      L.DomUtil.setTransform(this._c, off, scale);
    },
    draw() {
      if (!this._m || this._m._animatingZoom) return;
      const ctx = this._ctx, size = this._m.getSize();
      ctx.clearRect(0, 0, size.x, size.y);
      ctx.font = '600 12px -apple-system, "Segoe UI", Helvetica, Arial, sans-serif';
      ctx.lineJoin = 'round';
      for (let ti = 0; ti < tracks.length; ti++) {
        const t = tracks[ti];
        if (!t.visible || t.idx < 0) continue;
        const dy = ((ti % 3) - 1) * 12;                          // stagger labels so a huddle stays readable
        const p = t.pts[t.idx], age = T - p[0], stale = age > t.gapMs;
        const lp = this._m.latLngToLayerPoint([p[1], p[2]]);
        const x = lp.x - this._tl.x, y = lp.y - this._tl.y;
        if (x < -40 || y < -40 || x > size.x + 40 || y > size.y + 40) continue;
        const r = 7;
        if (!stale && p[4] > 0.5 && p[5] != null) {              // heading tick from the CoT track element
          const a = (p[5] - 90) * Math.PI / 180;
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(a) * r * 2.6, y + Math.sin(a) * r * 2.6);
          ctx.strokeStyle = t.colour; ctx.lineWidth = 2; ctx.setLineDash([]); ctx.stroke();
        }
        drawShape(ctx, t.shape, x, y, r);
        if (stale) {
          ctx.fillStyle = 'rgba(20,24,28,0.85)'; ctx.fill();
          ctx.strokeStyle = t.colour; ctx.lineWidth = 2; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
        } else {
          ctx.fillStyle = t.colour; ctx.fill();
          ctx.strokeStyle = '#0d1013'; ctx.lineWidth = 1.5; ctx.stroke();
        }
        // Ten seconds is the figure the research names: with that much latency added, people made
        // significantly more false alarms whether or not they knew about it.
        const lateMs = p[8] != null ? p[0] - p[8] : null;
        const late = lateMs != null && lateMs > 10000 ? ` · late ${fmtAge(lateMs)}` : '';
        const label = (stale ? `${t.meta.callsign} · stale ${fmtAge(age)}` : t.meta.callsign) + late;
        ctx.lineWidth = 3.5; ctx.strokeStyle = 'rgba(14,17,20,0.9)'; ctx.strokeText(label, x + r + 5, y + 4 + dy);
        ctx.fillStyle = stale ? '#c9c4b0' : '#f3f1ea'; ctx.fillText(label, x + r + 5, y + 4 + dy);
      }
    },
  });
  const markers = new MarkerLayer().addTo(map);

  // ---------- timeline ----------
  const tl = $('#timeline'), tctx = tl.getContext('2d');
  let TLW = 0, TLH = 0;
  const HEAD = 18;
  function tlLayout() {
    const r = tl.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    tl.width = Math.round(r.width * dpr); tl.height = Math.round(r.height * dpr);
    tctx.setTransform(dpr, 0, 0, dpr, 0, 0); TLW = r.width; TLH = r.height;
    for (const t of tracks) {
      const bm = new Uint8Array(Math.max(1, Math.floor(TLW)));
      for (const tm of t.times) { const x = Math.floor((tm - W0) / (W1 - W0) * TLW); if (x >= 0 && x < bm.length) bm[x] = 1; }
      t.bm = bm;
    }
  }
  const xOf = (ms) => (ms - W0) / (W1 - W0) * TLW;
  const tOf = (x) => W0 + Math.min(1, Math.max(0, x / TLW)) * (W1 - W0);
  function drawTimeline() {
    tctx.clearRect(0, 0, TLW, TLH);
    const span = W1 - W0;
    const step = span > 36e5 * 30 ? 6 * 36e5 : span > 36e5 * 8 ? 36e5 : span > 36e5 * 2 ? 15 * 6e4 : 5 * 6e4;
    tctx.fillStyle = '#8a949d'; tctx.font = '10px -apple-system, Helvetica, Arial, sans-serif'; tctx.textBaseline = 'top';
    for (let m = Math.ceil(W0 / step) * step; m < W1; m += step) {
      const x = xOf(m);
      tctx.fillStyle = '#2a333c'; tctx.fillRect(x, HEAD - 3, 1, TLH - HEAD + 3);
      tctx.fillStyle = '#8a949d'; tctx.fillText(fmtUTC(m).slice(0, 5), x + 3, 2);
    }
    const vis = tracks.filter(t => t.visible);
    const rowH = Math.max(3, Math.min(11, (TLH - HEAD - 2) / Math.max(1, vis.length)));
    vis.forEach((t, i) => {
      const y = HEAD + i * rowH;
      // A dropout is drawn as a dropout: a red hatch where the reports stopped, so a silent gap
      // is read as a gap in the comms and not as a person standing still.
      const drops = (t.meta.time && t.meta.time.dropouts) || [];
      tctx.fillStyle = '#c0392b'; tctx.globalAlpha = 0.55;
      for (const d of drops) {
        const x0 = Math.max(0, xOf(Math.max(d.from, W0))), x1 = Math.min(TLW, xOf(Math.min(d.to, W1)));
        if (x1 <= x0) continue;
        for (let x = Math.floor(x0); x < x1; x += 3) tctx.fillRect(x, y + 1, 1, rowH - 2);
      }
      tctx.fillStyle = t.colour; tctx.globalAlpha = 0.9;
      for (let x = 0; x < t.bm.length; x++) if (t.bm[x]) tctx.fillRect(x, y + 1, 1, rowH - 2);
      tctx.globalAlpha = 1;
    });
    // messages, as small ticks along the top rule, before the moments so the moments sit on top
    tctx.fillStyle = '#7fb3d5';
    for (const m of chat) {
      if (m.servertime < W0 || m.servertime >= W1) continue;
      const cx = xOf(m.servertime);
      tctx.fillRect(cx - 0.5, HEAD - 8, 1, 5);
    }
    for (const m of momentState.list) {
      if (m.at < W0 || m.at >= W1) continue;
      const mx = xOf(m.at);
      tctx.fillStyle = m.promoted ? '#B5B171' : '#8a949d';
      tctx.fillRect(mx - (m.promoted ? 1 : 0.5), HEAD - 3, m.promoted ? 2 : 1, TLH - HEAD + 3);
      tctx.beginPath(); tctx.arc(mx, HEAD - 5, m.promoted ? 3.5 : 2.5, 0, Math.PI * 2); tctx.fill();
    }
    const x = xOf(T);
    tctx.fillStyle = '#F7F6EB'; tctx.fillRect(x - 0.5, 0, 1.5, TLH);
    tctx.fillStyle = '#B5B171'; tctx.beginPath(); tctx.moveTo(x - 5, 0); tctx.lineTo(x + 5, 0); tctx.lineTo(x, 7); tctx.closePath(); tctx.fill();
  }
  let scrubbing = false;
  const scrub = $('#scrub');
  scrub.addEventListener('pointerdown', () => { scrubbing = true; });
  scrub.addEventListener('pointerup', () => { scrubbing = false; });
  scrub.addEventListener('input', () => { T = W0 + (scrub.value / 100000) * (W1 - W0); dirty = true; });
  const seekFromEvent = (e) => { const r = tl.getBoundingClientRect(); T = tOf(e.clientX - r.left); dirty = true; };
  tl.addEventListener('pointerdown', (e) => { scrubbing = true; tl.setPointerCapture(e.pointerId); seekFromEvent(e); });
  tl.addEventListener('pointermove', (e) => { if (scrubbing) seekFromEvent(e); });
  tl.addEventListener('pointerup', (e) => { scrubbing = false; tl.releasePointerCapture(e.pointerId); });

  // ---------- side panel ----------
  const list = $('#tracks');
  function buildPanel() {
    list.innerHTML = '';
    for (const t of tracks) {
      const row = document.createElement('div'); row.className = 'track' + (t.visible ? '' : ' off');
      const sw = document.createElement('canvas'); sw.className = 'sw'; sw.width = 32; sw.height = 32; sw.style.width = '16px'; sw.style.height = '16px';
      const c = sw.getContext('2d'); c.scale(2, 2); drawShape(c, t.shape, 8, 8, 6); c.fillStyle = t.colour; c.fill();
      const txt = document.createElement('div');
      const m = t.meta, mi = m.median_interval_ms ? fmtAge(m.median_interval_ms) : '?';
      txt.innerHTML = `<div class="name"></div><div class="sub"></div>`;
      txt.querySelector('.name').textContent = m.callsign;
      txt.querySelector('.sub').textContent = `${m.platform || 'unknown platform'}${m.device ? ' · ' + m.device : ''}${m.team ? ' · ' + m.team : ''}`;
      const st = document.createElement('div'); st.className = 'st none'; st.textContent = '';
      const wb = document.createElement('button'); wb.className = 'where'; wb.textContent = 'where?'; wb.title = `where ${m.callsign} was at the clock`;
      wb.onclick = (e) => { e.stopPropagation(); whereWas(m.callsign); };
      row.append(sw, txt, st, wb); t.row = row; t.st = st;
      row.onclick = () => { t.visible = !t.visible; row.classList.toggle('off', !t.visible); buildLines(t); t.boldIdx = -1; updateBold(t); dirty = true; };
      list.appendChild(row);
    }
  }
  function updatePanel() {
    for (const t of tracks) {
      let cls = 'none', txt = 'no report yet';
      if (t.idx >= 0) {
        const age = T - t.times[t.idx];
        if (age > t.gapMs) { cls = 'stale'; txt = `stale ${fmtAge(age)}`; }
        else { cls = 'fresh'; txt = `fresh · ${fmtAge(age)} ago`; }
        if (t.idx === t.times.length - 1 && age > t.gapMs) txt = `ended · ${fmtAge(age)}`;
      }
      if (t.st.textContent !== txt) t.st.textContent = txt;
      if (!t.st.classList.contains(cls)) { t.st.className = 'st ' + cls; }
    }
  }
  {
    const meta = $('#meta');
    meta.textContent = '';
    const b = document.createElement('b'); b.textContent = bundle.source.name;
    meta.append(
      b, document.createElement('br'),
      `${fmtDay(W0)} ${fmtUTC(W0)} to ${fmtDay(W1)} ${fmtUTC(W1)} (${fmtDur(W1 - W0)})`, document.createElement('br'),
      `${bundle.counts.rows_kept} reports, ${bundle.counts.tracks} callsigns, ${bundle.counts.rows_without_fix} without a fix dropped`,
      document.createElement('br'), `basemap: ${basemapLabel}`,
    );
    // A window that was cut has to say so on the screen, not only in the JSON. Everything above
    // this line describes what was loaded, and would otherwise read as a complete picture.
    if (bundle.truncated) {
      const w = document.createElement('b');
      w.className = 'warn';
      w.textContent = `showing the first ${bundle.truncated.returned} of ${bundle.truncated.held} reports in this window`;
      meta.append(document.createElement('br'), w);
    }
  }

  // ---------- the window: from, to, and three that are one press ----------
  {
    const wstart = $('#wstart'), wend = $('#wend'), msg = $('#winmsg');
    // datetime-local speaks local time, and everything else here is UTC. Convert at the edge and
    // nowhere else, because a debrief that argues about the clock has already lost the room.
    const toField = (ms) => {
      const d = new Date(ms - new Date(ms).getTimezoneOffset() * 60000);
      return d.toISOString().slice(0, 19);
    };
    const fromField = (v) => (v ? new Date(v).getTime() : NaN);
    wstart.value = toField(W0);
    wend.value = toField(W1);

    const replay = (from, to) => {
      if (!Number.isFinite(from) || !Number.isFinite(to)) {
        msg.textContent = 'Set both a start and an end.'; msg.className = 'warn'; return;
      }
      if (from >= to) {
        msg.textContent = 'The start has to come before the end.'; msg.className = 'warn'; return;
      }
      location.search = `?from=${Math.round(from)}&to=${Math.round(to)}`;
    };
    $('#wgo').onclick = () => replay(fromField(wstart.value), fromField(wend.value));
    for (const b of document.querySelectorAll('#window .quick button')) {
      b.onclick = () => { const now = Date.now(); replay(now - Number(b.dataset.hours) * 3600000, now); };
    }

    // "Nothing in that window" is the whole point of asking, so it is set first and it wins. The
    // archive summary is a convenience that arrives later over the network, and it used to
    // overwrite the warning: the operator was told an empty result held reports, which is exactly
    // the confusion between an empty window and a broken query that this is here to prevent.
    const emptyWindow = Boolean(bundle.empty);
    if (emptyWindow) {
      const holds = bundle.archive_holds || {};
      msg.textContent = holds.count
        ? `Nothing in that window. The record runs from ${holds.first} to ${holds.last}.`
        : 'Nothing in that window, and nothing in the record yet.';
      msg.className = 'warn';
    }

    // What the archive actually holds, so nobody asks for a window that cannot exist.
    fetch('/api/archive').then(r => r.json()).then(a => {
      if (!a || !a.count || emptyWindow) return;
      const held = `The record holds ${a.count.toLocaleString()} reports, ${a.first} to ${a.last}.`;
      msg.textContent = a.catching_up
        ? `${held} Still catching up on the history the server holds.`
        : held;
      msg.className = '';
    }).catch(() => {});
  }

  // ---------- importing a pack ----------
  {
    const path = $('#packpath'), msg = $('#packmsg');
    $('#packgo').onclick = async () => {
      const wanted = path.value.trim();
      if (!wanted) { msg.textContent = 'Give the path of a data package on this box.'; msg.className = 'warn'; return; }
      msg.textContent = 'Reading it...'; msg.className = '';
      let res;
      try {
        res = await fetch('/api/packs/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: 'path=' + encodeURIComponent(wanted),
        });
      } catch (e) {
        msg.textContent = 'That could not be sent to the box.'; msg.className = 'warn'; return;
      }
      const text = await res.text();
      if (!res.ok) {
        // The refusal names the rule that stopped it, which is the useful half of refusing.
        msg.textContent = text; msg.className = 'warn'; return;
      }
      let pack = null;
      try { pack = JSON.parse(text); } catch { pack = null; }
      const n = pack && pack.overlays ? pack.overlays.length : 0;
      const replaced = pack && pack.replaced ? ` This replaced "${pack.replaced}", which carried the same identity.` : '';
      msg.textContent = `Imported ${pack && pack.name ? pack.name : 'the pack'}: ${n} overlay${n === 1 ? '' : 's'}.${replaced} Reload to draw it.`;
      msg.className = replaced ? 'warn' : '';
    };
  }

  // ---------- chat on the timeline ----------
  //
  // "I told you at half past" is settled by looking. Messages are marked on the timeline, the one
  // nearest the clock is shown as the replay runs, and the list is an index that jumps. Every
  // string is a person's words on an unauthenticated page: textContent, never markup.
  const chat = (bundle.chat || []).slice().sort((a, b) => a.servertime - b.servertime);
  const chatNow = $('#chatnow'), chatList = $('#chatlist'), chatWhy = $('#chatwhy');
  if (!chat.length) {
    chatWhy.textContent = 'No messages in this window. Change the window above to when they were sent.';
    fetch('/api/archive').then(r => r.json()).then(a => {
      if (a && a.messages) chatWhy.textContent = `No messages in this window. This box holds ${a.messages.toLocaleString()} in all. Change the window above to reach them.`;
    }).catch(() => {});
  } else {
    chatWhy.textContent = `${chat.length} in this window, as ticks along the top of the timeline; the latest shows here as the replay runs; a click jumps.`;
    for (const m of chat) {
      const row = document.createElement('div');
      row.className = 'msg';
      const t = document.createElement('span'); t.className = 't'; t.textContent = fmtUTC(m.servertime);
      const who = document.createElement('b'); who.textContent = m.sender || '(no sender)';
      const room = document.createElement('span'); room.className = 'room'; room.textContent = m.room ? ` \u2192 ${m.room}` : '';
      const text = document.createElement('span'); text.className = 'x'; text.textContent = m.text || '(no text)';
      row.append(t, who, room, text);
      row.onclick = () => { T = m.servertime; dirty = true; };
      chatList.appendChild(row);
    }
  }
  function chatAt(when) {
    // the latest message at or before the clock, if it was within the last ten minutes
    let lo = 0, hi = chat.length - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (chat[mid].servertime <= when) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    if (best < 0 || when - chat[best].servertime > 600000) { chatNow.textContent = ''; return; }
    const m = chat[best];
    chatNow.textContent = `${fmtUTC(m.servertime)} ${m.sender || '(no sender)'}${m.room ? ' \u2192 ' + m.room : ''}: ${m.text || '(no text)'}`;
  }

  // ---------- honest time ----------
  {
    const host = $('#timefacts');
    const fmtMs = (ms) => ms == null ? 'unknown' : ms < 1000 ? `${ms} ms` : fmtAge(ms);
    const rows = document.createElement('div');
    for (const t of tracks) {
      const f = t.meta.time;
      if (!f) continue;
      const line = document.createElement('div');
      line.className = 'tf';
      const name = document.createElement('b'); name.textContent = t.meta.callsign;
      const facts = document.createElement('span');
      const bits = [];
      // "late" is server time minus device time, so a clock behind the server reads as late and
      // a clock ahead is counted separately; the wording says so rather than blaming the link.
      if (f.latency_known) bits.push(`late by ${fmtMs(f.latency_median_ms)} typically, ${fmtMs(f.latency_max_ms)} at worst (or its clock is behind the server)`);
      else if (f.clock_ahead_count) bits.push('latency unknown: every report arrived before its own clock said it was sent');
      else bits.push('latency unknown: no device time');
      if (f.clock_ahead_count) bits.push(`clock ahead of the server on ${f.clock_ahead_count} report${f.clock_ahead_count === 1 ? '' : 's'}, by up to ${fmtMs(f.clock_ahead_max_ms)}`);
      if (f.dropouts && f.dropouts.length) bits.push(`${f.dropouts.length} dropout${f.dropouts.length === 1 ? '' : 's'}, ${fmtAge(f.missing_ms)} missing`);
      else bits.push('no dropouts');
      facts.textContent = ' ' + bits.join('; ');
      line.append(name, facts);
      rows.appendChild(line);
    }
    const note = document.createElement('div');
    note.className = 'dim';
    note.textContent = (bundle.time && bundle.time.note) || '';
    host.append(rows, note);
  }

  // ---------- named moments ----------
  //
  // "This, not the map, is the product." One key marks the moment the clock is at, Enter keeps
  // it, a click jumps back, and the number keys jump to the promoted ones in order. Everything an
  // operator types is bound as text, never as markup: this repository has paid twice for a string
  // reaching innerHTML.
  const momentState = { list: [], cap: 6, promoted: 0, pending: null };
  // declared here, above the moments list that reads it: a const is not readable before its line
  const recState = { list: [], open: null, shape: 'odcr', itemsCap: 12, doctrine: { sustain: 3, improve: 3 } };
  const mname = $('#momentname'), mlist = $('#momentlist'), mmsg = $('#momentmsg'), budget = $('#budget');
  const say = (text, warn) => { mmsg.textContent = text || ''; mmsg.className = warn ? 'warn' : ''; };
  const form = (o) => Object.entries(o).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');
  const post = async (path, body) => {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: form(body) });
    const text = await r.text();
    if (!r.ok) throw new Error(text);
    return JSON.parse(text);
  };

  async function loadMoments() {
    try {
      const d = await fetch('/api/moments').then(r => r.json());
      momentState.list = d.moments || []; momentState.cap = d.promoted_cap; momentState.promoted = d.promoted;
    } catch { momentState.list = []; }
    renderMoments();
    dirty = true;
  }

  function renderMoments() {
    budget.textContent = `${momentState.promoted} of ${momentState.cap} kept for the debrief`;
    mlist.textContent = '';
    let key = 1;
    for (const m of momentState.list) {
      const row = document.createElement('div');
      row.className = 'm' + (m.promoted ? ' promoted' : '');
      const t = document.createElement('span'); t.className = 't'; t.textContent = fmtUTC(m.at);
      const n = document.createElement('span'); n.className = 'n'; n.textContent = m.name;   // text, never markup
      row.append(t, n);
      if (m.promoted && key <= 9) { const k = document.createElement('span'); k.className = 'k'; k.textContent = String(key++); row.append(k); }
      const promote = document.createElement('button');
      promote.textContent = m.promoted ? 'take out' : 'keep';
      promote.title = m.promoted ? 'take this out of the six kept for the debrief' : `keep for the debrief (${momentState.promoted} of ${momentState.cap} used)`;
      promote.onclick = async (e) => {
        e.stopPropagation();
        try { await post(`/api/moments/${m.id}`, { promoted: m.promoted ? 'no' : 'yes' }); say(''); }
        catch (err) { say(err.message, true); }
        loadMoments();
      };
      const rename = document.createElement('button'); rename.textContent = 'rename';
      rename.onclick = (e) => {
        e.stopPropagation();
        momentState.pending = { id: m.id };
        mname.value = m.name; mname.placeholder = 'new name, Enter to keep it'; mname.focus(); mname.select();
      };
      const del = document.createElement('button'); del.textContent = 'delete';
      del.onclick = async (e) => {
        e.stopPropagation();
        try { await post(`/api/moments/${m.id}/delete`, {}); say(''); } catch (err) { say(err.message, true); }
        loadMoments();
      };
      row.append(promote, rename, del);
      if (recState.open) {
        const already = recState.open.items.some(i => i.at === m.at && i.observation === m.name);
        if (already) { const k = document.createElement('span'); k.className = 'inrec'; k.textContent = 'in record'; row.append(k); }
        else {
          const toRec = document.createElement('button'); toRec.textContent = 'add to record'; toRec.title = 'an observation in the open record, in this moment\'s own words';
          toRec.onclick = (e) => { e.stopPropagation(); addItem({ moment: m.id }); };
          row.append(toRec);
        }
      }
      row.onclick = () => jumpTo(m);
      mlist.appendChild(row);
    }
  }

  function jumpTo(m) {
    if (m.at >= W0 && m.at < W1) { T = m.at; dirty = true; return; }
    // Outside this window: reopen the replay around it. The address is the handle a moment is
    // passed around by, so this is the same path somebody else's link takes.
    location.search = `?at=${m.at}`;
  }

  function markMoment() {
    momentState.pending = { at: T };
    mname.value = '';
    mname.placeholder = `${fmtDay(T)} ${fmtUTC(T)}: name it, or Enter to keep the time as the name`;
    mname.focus();
    say(`marked ${fmtUTC(T)}; name it and press Enter`);
  }

  async function keepMoment() {
    const pending = momentState.pending;
    if (!pending) { say('press m while the replay is at the moment you want', false); return; }
    const typed = mname.value.trim();
    try {
      if (pending.id) {
        if (!typed) { say('a moment needs a name', true); return; }
        await post(`/api/moments/${pending.id}`, { name: typed });
      } else {
        const name = typed || `${fmtDay(pending.at)} ${fmtUTC(pending.at)}`;
        await post('/api/moments', { at: Math.round(pending.at), name });
      }
      momentState.pending = null; mname.value = ''; mname.placeholder = 'press m to mark this moment, Enter to keep it';
      say('');
      mname.blur();
    } catch (err) { say(err.message, true); }
    loadMoments();
  }
  $('#momentkeep').onclick = keepMoment;
  mname.addEventListener('keydown', (e) => {
    // the field says "press m": pressing m in it, empty, marks the moment rather than typing an m
    if (e.key === 'm' && !mname.value && !momentState.pending && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); e.stopPropagation(); markMoment(); return; }
    if (e.key === 'Enter') { e.preventDefault(); keepMoment(); }
    if (e.key === 'Escape') { momentState.pending = null; mname.value = ''; mname.placeholder = 'press m to mark this moment, Enter to keep it'; mname.blur(); say(''); }
    e.stopPropagation();
  });

  window.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;   // the browser's own shortcuts stay the browser's
    if (e.target.tagName === 'SELECT' || (e.target.tagName === 'INPUT' && e.target.type !== 'range')) return;
    if (e.key === 'm') { e.preventDefault(); markMoment(); }
    else if (/^[1-9]$/.test(e.key)) {
      const promoted = momentState.list.filter(m => m.promoted);
      const m = promoted[Number(e.key) - 1];
      if (m) jumpTo(m);
    }
  });
  loadMoments();

  // ---------- the structured record ----------
  //
  // One debrief's record in the unit's own shape, kept on the box and taken away as a file. The
  // tool writes nothing into it but a moment's own name; every field is the room's, saved as it
  // is left. The owner of an item is a duty position, and the field says so.
  const recsel = $('#recsel'), recmsg = $('#recmsg'), recbudget = $('#recbudget'), recitems = $('#recitems');
  const rsay = (text, warn) => { recmsg.textContent = text || ''; recmsg.className = warn ? 'warn' : ''; };
  const FIELD_LABEL = { observation: 'Observation', discussion: 'Discussion', conclusion: 'Conclusion', recommendation: 'Recommendation' };

  async function loadRecords(openId) {
    try {
      const d = await fetch('/api/records').then(r => r.json());
      recState.list = d.records || []; recState.shape = d.shape || 'odcr'; recState.itemsCap = d.items_cap; recState.doctrine = d.doctrine || recState.doctrine;
    } catch { recState.list = []; }
    recsel.textContent = '';
    const none = document.createElement('option'); none.value = ''; none.textContent = recState.list.length ? 'choose a record' : 'no record yet'; recsel.appendChild(none);
    $('#recnew').textContent = recState.list.length ? 'Start another' : 'Start a record for this window';
    for (const r of recState.list) { const o = document.createElement('option'); o.value = r.id; o.textContent = `${r.title} (${r.items})`; recsel.appendChild(o); }
    const want = openId || (recState.open && recState.open.id) || '';
    recsel.value = recState.list.some(r => r.id === want) ? want : '';
    await openRecord(recsel.value);
  }

  async function openRecord(id) {
    recState.open = null;
    $('#recopen').hidden = true; $('#recexport').hidden = true; recbudget.textContent = recState.shape === 'odcr' ? 'ODCR' : 'sustain and improve';
    if (!id) { renderMoments(); return; }
    try {
      const r = await fetch(`/api/records/${encodeURIComponent(id)}`);
      if (!r.ok) { rsay(await r.text(), true); return; }
      recState.open = await r.json();
    } catch (err) { rsay(err.message, true); return; }
    const rec = recState.open;
    $('#recopentitle').textContent = rec.title;
    $('#recobjectives').value = rec.objectives || '';
    $('#recopen').hidden = false;
    const ex = $('#recexport'); ex.hidden = false; ex.href = `/record/${rec.id}.md`; ex.title = 'a Markdown file in this box\'s shape; it downloads';
    recbudget.textContent = `${rec.items.length} items · aim for ${recState.doctrine.sustain} sustains and ${recState.doctrine.improve} improves · ${recState.itemsCap} at most`;
    renderItems();
    renderMoments();   // the moments list grows a "to record" button while a record is open
  }

  function renderItems() {
    recitems.textContent = '';
    const rec = recState.open;
    if (!rec) return;
    rec.items.forEach((it, n) => {
      const box = document.createElement('div'); box.className = 'item';
      const top = document.createElement('div'); top.className = 'top';
      const num = document.createElement('span'); num.textContent = `${n + 1}.`;
      const kind = document.createElement('select');
      for (const [v, label] of [['', 'not yet sorted'], ['sustain', 'sustain'], ['improve', 'improve']]) { const o = document.createElement('option'); o.value = v; o.textContent = label; kind.appendChild(o); }
      kind.value = it.kind || '';
      kind.onchange = () => saveItem(it.id, { kind: kind.value });
      const ownerLabel = document.createElement('label'); ownerLabel.textContent = 'duty position';
      const owner = document.createElement('input'); owner.placeholder = 'not a name'; owner.value = it.owner || ''; owner.maxLength = 80; owner.title = 'the duty position that owns this, never a person';
      owner.onchange = () => saveItem(it.id, { owner: owner.value });
      const at = document.createElement('button'); at.textContent = it.at ? fmtUTC(it.at) : 'at the clock'; at.title = it.at ? 'jump the replay to this moment' : 'stamp this item with the clock\'s time';
      at.onclick = async () => {
        if (it.at) { jumpTo({ at: it.at }); return; }
        const saved = await saveItem(it.id, { at: String(Math.round(T)) });
        if (saved) { it.at = saved.at; at.textContent = fmtUTC(saved.at); at.title = 'jump the replay to this moment'; }
      };
      const del = document.createElement('button'); del.textContent = 'delete';
      del.onclick = async () => { try { await post(`/api/records/${rec.id}/items/${it.id}/delete`, {}); rsay(''); } catch (err) { rsay(err.message, true); } loadRecords(rec.id); };
      top.append(num, kind, ownerLabel, owner, at, del);
      box.appendChild(top);
      const fields = recState.shape === 'odcr' ? ['observation', 'discussion', 'conclusion', 'recommendation'] : ['observation', 'discussion', 'recommendation'];
      for (const f of fields) {
        const label = document.createElement('label'); label.textContent = FIELD_LABEL[f];
        const ta = document.createElement('textarea'); ta.value = it[f] || ''; ta.placeholder = f === 'observation' ? 'what the record shows' : 'the room\'s words';
        ta.onchange = () => saveItem(it.id, { [f]: ta.value });
        ta.addEventListener('keydown', (e) => e.stopPropagation());
        box.append(label, ta);
      }
      recitems.appendChild(box);
    });
  }

  async function saveItem(iid, fields) {
    // Nothing is redrawn on a save: a redraw would wipe what is being typed into the next field,
    // and on a refusal the typed text stays where it is beside the sentence that refused it.
    const rec = recState.open; if (!rec) return null;
    try {
      const it = await post(`/api/records/${rec.id}/items/${iid}`, fields);
      rec.items = rec.items.map(x => x.id === iid ? it : x);
      rsay('');
      return it;
    } catch (err) { rsay(err.message, true); return null; }
  }

  async function addItem(fields) {
    const rec = recState.open; if (!rec) { rsay('open or start a record first', true); return; }
    try { await post(`/api/records/${rec.id}/items`, fields); rsay(''); } catch (err) { rsay(err.message, true); }
    loadRecords(rec.id);
  }

  recsel.onchange = () => openRecord(recsel.value);
  $('#recnew').onclick = () => { $('#recform').hidden = false; $('#rectitle').value = ''; $('#recobj').value = ''; $('#rectitle').focus(); };
  $('#reccancel').onclick = () => { $('#recform').hidden = true; };
  $('#recstart').onclick = async () => {
    try {
      const made = await post('/api/records', { title: $('#rectitle').value, start: String(Math.round(W0)), end: String(Math.round(W1)), objectives: $('#recobj').value });
      $('#recform').hidden = true; rsay('');
      loadRecords(made.id);
    } catch (err) { rsay(err.message, true); }
  };
  $('#recobjectives').onchange = async () => {
    const rec = recState.open; if (!rec) return;
    try { await post(`/api/records/${rec.id}`, { objectives: $('#recobjectives').value }); rsay(''); } catch (err) { rsay(err.message, true); }
  };
  const newRow = $('#recnewrow'), newObs = $('#recnewobs');
  $('#recadd').onclick = () => { newRow.hidden = false; newObs.value = ''; newObs.focus(); };
  newObs.addEventListener('keydown', async (e) => {
    e.stopPropagation();
    if (e.key === 'Escape') { newRow.hidden = true; return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const typed = newObs.value.trim();
    if (!typed) { rsay('an item starts with an observation, in the room\'s words', true); return; }
    newRow.hidden = true;
    await addItem({ observation: typed, at: String(Math.round(T)) });
  });
  $('#recdel').onclick = async () => {
    const rec = recState.open; if (!rec) return;
    if (!window.confirm(`Delete the record "${rec.title}" from this box? The exported file, if you took one, is unaffected.`)) return;
    try { await post(`/api/records/${rec.id}/delete`, {}); rsay(''); } catch (err) { rsay(err.message, true); }
    loadRecords('');
  };
  for (const id of ['#rectitle', '#recobj', '#recobjectives']) $(id).addEventListener('keydown', (e) => e.stopPropagation());
  $('#rectitle').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('#recstart').click(); } });
  loadRecords('');

  // ---------- proposals, never narration ----------
  //
  // The moments the record can point at, with their evidence and without a reason: who, when,
  // what the record shows. Accept makes a named moment through the moments route and takes the
  // proposal off the list; dismiss keeps it off on this box. Every word here is text on the page,
  // including a callsign typed into the where-was box, because a callsign can be set to anything.
  const propState = { list: [], cap: 200 };
  const plist = $('#proplist'), pmsg = $('#propmsg'), pcount = $('#propcount');
  const wmsg = $('#wheremsg');
  const psay = (text, warn) => { pmsg.textContent = text || ''; pmsg.className = warn ? 'warn' : ''; };
  const wsay = (text, warn) => { wmsg.textContent = text || ''; wmsg.className = warn ? 'warn' : ''; };
  const bundleQuery = chosen ? `name=archive&start=${qsFrom}&end=${qsTo}` : 'name=' + encodeURIComponent(name);
  const KIND = { 'co-location': 'close', dropout: 'gap', silence: 'silence', boundary: 'boundary', contact: 'reported', message: 'message' };
  // a moment's name from a proposal, cut at a word if it must be cut at all
  const cutName = (text) => { if (text.length <= 200) return text; const cut = text.slice(0, 199); const at = cut.lastIndexOf(' '); return (at > 120 ? cut.slice(0, at) : cut) + '\u2026'; };
  // the record's own words, and nothing that says why
  const describe = (p) => {
    const e = p.evidence || {}, cs = p.callsigns || [];
    switch (p.kind) {
      case 'co-location': return `${cs.join(' and ')} reported within ${e.closest_m} m of each other for ${fmtDur(e.for_ms)}`;
      case 'dropout': return e.gaps > 1
        ? `${cs.join(', ')}: ${e.gaps} gaps with no report, ${fmtDur(e.longest_ms)} at most, ${fmtDur(e.for_ms)} in all`
        : `${cs.join(', ')}: no report for ${fmtDur(e.for_ms)}`;
      case 'silence': return `No report and no message from any callsign for ${fmtDur(e.for_ms)}`;
      case 'boundary': return e.direction === 'in' ? `${cs.join(', ')} reported outside ${e.overlay}, then inside` : `${cs.join(', ')} reported inside ${e.overlay}, then outside`;
      case 'contact': return `${e.label}, reported in the mission pack${e.ce != null ? `, accuracy given as ${Math.round(e.ce)} m` : ''}`;
      case 'message': return `${cs.length ? cs.join(', ') : 'someone'} wrote: "${e.text}"`;
      default: return `${p.kind} at ${fmtUTC(p.at)}`;
    }
  };

  async function loadProposals() {
    try {
      const r = await fetch('/api/proposals?' + bundleQuery);
      if (!r.ok) { psay(await r.text(), true); propState.list = []; renderProposals(); return; }
      const d = await r.json();
      propState.list = d.proposals || []; propState.cap = d.cap;
      psay(d.count >= d.cap ? `showing the first ${d.cap}; narrow the window to see the rest` : '');
    } catch { propState.list = []; }
    renderProposals();
  }

  // a dismissed row leaves the list here rather than by recomputing the whole window (review note N2)
  const dropLocally = (id) => { propState.list = propState.list.filter(x => x.id !== id); renderProposals(); };

  const PAGE = 30;
  const kindsOn = new Set();   // empty means every kind
  let shown = PAGE;
  function renderKinds() {
    const kinds = $('#propkinds'); kinds.textContent = '';
    const counts = {};
    for (const p of propState.list) counts[p.kind] = (counts[p.kind] || 0) + 1;
    for (const [k, n] of Object.entries(counts)) {
      const b = document.createElement('button'); b.textContent = `${KIND[k] || k} ${n}`;
      b.className = (kindsOn.size === 0 || kindsOn.has(k)) ? 'on' : '';
      b.title = 'show only this kind; press again for all';
      b.onclick = () => { if (kindsOn.has(k)) kindsOn.delete(k); else { kindsOn.clear(); kindsOn.add(k); } shown = PAGE; renderProposals(); };
      kinds.appendChild(b);
    }
  }

  function renderProposals() {
    renderKinds();
    const all = propState.list.filter(p => kindsOn.size === 0 || kindsOn.has(p.kind));
    pcount.textContent = propState.list.length ? `${all.length} of ${propState.list.length}` : 'nothing in this window';
    if (!propState.list.length) psay('Nothing stood out in this window. Try a wider window, or press m to mark a moment yourself.');
    plist.textContent = '';
    for (const p of all.slice(0, shown)) {
      const row = document.createElement('div'); row.className = 'p';
      const top = document.createElement('div'); top.className = 'top';
      const k = document.createElement('span'); k.className = 'kind'; k.textContent = KIND[p.kind] || p.kind;
      const t = document.createElement('span'); t.className = 't'; t.textContent = fmtUTC(p.at) + (p.until ? ' to ' + fmtUTC(p.until) : '');
      top.append(k, t);
      const e = document.createElement('div'); e.className = 'e'; e.textContent = describe(p);   // text, never markup
      if (p.kind === 'message' && p.evidence && p.evidence.word) e.title = `mentions "${p.evidence.word}"`;
      const acts = document.createElement('div'); acts.className = 'acts';
      const accept = document.createElement('button'); accept.textContent = 'keep'; accept.title = 'keep this as one of your moments, in these words';
      accept.onclick = async (ev) => {
        ev.stopPropagation();
        try { await post('/api/moments', { at: Math.round(p.at), name: cutName(describe(p)) }); psay(''); }
        catch (err) { psay(err.message, true); return; }
        loadMoments();
        try { await post(`/api/proposals/${p.id}/dismiss`, {}); } catch (err) { psay(err.message, true); }
        dropLocally(p.id);
      };
      const dismiss = document.createElement('button'); dismiss.textContent = 'dismiss'; dismiss.title = 'do not propose this again on this box';
      dismiss.onclick = async (ev) => {
        ev.stopPropagation();
        try { await post(`/api/proposals/${p.id}/dismiss`, {}); psay(''); } catch (err) { psay(err.message, true); }
        dropLocally(p.id);
      };
      acts.append(accept, dismiss);
      row.append(top, e, acts);
      row.onclick = () => jumpTo({ at: p.at });
      plist.appendChild(row);
    }
    const more = $('#propmore');
    more.hidden = all.length <= shown;
    more.textContent = `show ${Math.min(PAGE, all.length - shown)} more of ${all.length - shown}`;
    more.onclick = () => { shown += PAGE; renderProposals(); };
  }

  // Where was X at the clock: pressed on a callsign row, answered under the list in two lines,
  // the answer first and the qualification second.
  async function whereWas(cs) {
    if (!cs) return;
    try {
      const r = await fetch(`/api/where?${bundleQuery}&callsign=${encodeURIComponent(cs)}&at=${Math.round(T)}`);
      const text = await r.text();
      if (!r.ok) { wsay(text, true); return; }
      const a = JSON.parse(text);
      if (!a.known) { wsay(a.message, true); return; }
      wmsg.textContent = ''; wmsg.className = a.stale ? 'warn' : '';
      const main = document.createElement('span');
      main.textContent = `${a.callsign} at ${fmtUTC(T)}: last report ${fmtAge(a.age_ms)} earlier.` + (a.stale ? ` Stale, older than its ${fmtDur(a.threshold_ms)} threshold.` : '');
      const sub = document.createElement('span'); sub.className = 'sub';
      sub.textContent = `${a.lat.toFixed(5)}, ${a.lon.toFixed(5)}, at ${fmtUTC(a.at)}.` + (a.nodes > 1 ? ` ${a.nodes} nodes carry this callsign; this is the newest report across them.` : '');
      wmsg.append(main, sub);
      map.panTo([a.lat, a.lon]);
    } catch (err) { wsay(err.message, true); }
  }
  loadProposals();

  // ---------- marks and keys, shown on request ----------
  $('#keystoggle').onclick = () => { const h = $('#keyshelp'); h.hidden = !h.hidden; $('#keystoggle').textContent = h.hidden ? 'show' : 'hide'; };

  // ---------- the map icon: the online map, or a pack this box carries ----------
  const gear = $('#gear'), settings = $('#settings');
  gear.onclick = () => { settings.hidden = !settings.hidden; if (!settings.hidden) loadMaps(); };
  $('#nomapbtn').onclick = () => { settings.hidden = false; loadMaps(); };
  settleBasemap(tmeta);
  document.addEventListener('click', (e) => {
    if (!settings.hidden && !settings.contains(e.target) && e.target !== gear) settings.hidden = true;
  });
  async function loadMaps() {
    const list = $('#maplist');
    try {
      const d = await fetch('/api/maps').then(r => r.json());
      if (!d.sources.length) { list.innerHTML = '<p class="muted">No map pack on this box and no online map. Put an .mbtiles in its maps directory.</p>'; return; }
      list.innerHTML = '';
      for (const s of d.sources) {
        const b = document.createElement('button');
        const inUse = s.id === d.chosen || (!d.chosen && usingOsmByDefault && s.id === 'online:osm');
        b.className = 'mapopt' + (inUse ? ' on' : '');
        const n = document.createElement('span'); n.className = 'n'; n.textContent = s.name + (inUse && !d.chosen ? ' (in use, no choice made)' : '');
        const o = document.createElement('span'); o.className = 'o';
        const z = (s.minzoom != null && s.maxzoom != null) ? ` · zoom ${s.minzoom} to ${s.maxzoom}` : '';
        o.textContent = s.origin + z;
        if (s.url_template) {
          // Warn only when the tiles really leave this box: a tile service on 127.0.0.1 does not.
          let host = '', local = false;
          try {
            host = new URL(s.url_template.replace(/\{[^}]*\}/g, '0')).hostname;
            local = host === 'localhost' || /^127\./.test(host) || /^10\./.test(host)
              || /^192\.168\./.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host);
          } catch (e) { host = ''; }
          if (host && !local) {
            const w = document.createElement('span'); w.className = 'o warn';
            w.textContent = ' · tiles fetched by your browser from ' + host;
            o.append(w);
          }
        }
        b.append(n, o);
        b.onclick = async () => {
          b.disabled = true;
          try {
            const r = await fetch('/api/maps/choose', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'id=' + encodeURIComponent(s.id) });
            if (!r.ok) { o.textContent = 'could not choose this one'; b.disabled = false; return; }
            await settleBasemap(await fetch('/tiles/meta').then(x => x.json()));
            loadMaps();
          } catch (err) { b.disabled = false; }
        };
        list.appendChild(b);
      }
    } catch (e) { list.innerHTML = '<p class="muted">could not read the maps on this box</p>'; }
  }

  // ---------- version and updates (releases only, never a branch) ----------
  fetch('/version').then(r => r.json()).then(v => {
    $('#ver').textContent = v.version;
    if (!v.local) $('#updmsg').textContent = 'updates are applied on the box itself';
    else if (v.can_update === false) $('#updmsg').textContent = 'installed copy: update with sudo /opt/pinecone/update.sh on the box';
    window.pineconeCanUpdate = v.can_update !== false;
  }).catch(() => {});
  $('#updcheck').onclick = async () => {
    $('#updmsg').textContent = 'checking…'; $('#updnow').hidden = true;
    try {
      const c = await fetch('/update/check').then(r => r.json());
      if (c.error) { $('#updmsg').textContent = c.error; return; }
      $('#updmsg').textContent = c.available === 'yes' ? `${c.latest} is available (you have ${c.current})` : `latest release is ${c.latest}; you have ${c.current}`;
      $('#updnow').hidden = c.available !== 'yes' || window.pineconeCanUpdate === false;
    } catch (e) { $('#updmsg').textContent = 'check failed'; }
  };
  $('#updnow').onclick = async () => {
    $('#updnow').disabled = true; $('#updmsg').textContent = 'downloading, verifying, applying…';
    const was = $('#ver').textContent;
    try {
      const a = await fetch('/update/apply', { method: 'POST' }).then(r => r.json());
      if (!a.restarting) { $('#updmsg').textContent = a.error || a.result || `update did not apply (rc ${a.rc})`; $('#updnow').disabled = false; return; }
      $('#updmsg').textContent = `updated to ${a.updated}, restarting…`;
      for (let i = 0; i < 40; i++) {
        await new Promise(r => setTimeout(r, 1000));
        try { const v = await fetch('/version').then(r => r.json()); if (v.version !== was) { location.reload(); return; } } catch (e) {}
      }
      $('#updmsg').textContent = 'restarted? reload the page';
    } catch (e) { $('#updmsg').textContent = 'apply failed'; $('#updnow').disabled = false; }
  };

  // ---------- controls ----------
  let T = atGiven && qsAt >= W0 && qsAt < W1 ? qsAt : W0, playing = false, si = 3, dir = 1, last = null, dirty = true, lastSlow = 0;
  const playBtn = $('#play'), speedsEl = $('#speeds');
  SPEEDS.forEach((s, i) => { const b = document.createElement('button'); b.textContent = s + '×'; b.onclick = () => { si = i; renderSpeed(); }; speedsEl.appendChild(b); });
  function renderSpeed() {
    [...speedsEl.children].forEach((b, i) => b.classList.toggle('on', i === si));
    $('#rev').classList.toggle('on', dir < 0);
    $('#speedlbl').textContent = `${SPEEDS[si]}× ${dir < 0 ? 'reverse' : 'forward'}`;
  }
  function setPlaying(p) { playing = p; playBtn.textContent = p ? 'Pause' : 'Play'; last = null; }
  playBtn.onclick = () => setPlaying(!playing);
  $('#toStart').onclick = () => { T = dir < 0 ? W1 : W0; dirty = true; };
  $('#rev').onclick = () => { dir = -dir; renderSpeed(); };
  $('#fit').onclick = () => { const b = allBounds(); if (b.isValid()) map.fitBounds(b.pad(0.3)); };
  $('#full').onchange = () => { for (const t of tracks) { buildLines(t); t.boldIdx = -1; updateBold(t); } dirty = true; };
  $('#trail').onchange = () => { for (const t of tracks) { t.boldIdx = -1; updateBold(t); } dirty = true; };
  $('#gap').onchange = () => { for (const t of tracks) { computeRuns(t); buildLines(t); t.boldIdx = -1; updateBold(t); } dirty = true; };
  window.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.tagName === 'SELECT' || (e.target.tagName === 'INPUT' && e.target.type !== 'range')) return;
    if (e.code === 'Space' || e.key === ' ') { e.preventDefault(); setPlaying(!playing); }
    else if (e.key === 'ArrowLeft') { T = Math.max(W0, T - 10000); dirty = true; }
    else if (e.key === 'ArrowRight') { T = Math.min(W1, T + 10000); dirty = true; }
    else if (e.key === '[') { si = Math.max(0, si - 1); renderSpeed(); }
    else if (e.key === ']') { si = Math.min(SPEEDS.length - 1, si + 1); renderSpeed(); }
    else if (e.key === 'r') { dir = -dir; renderSpeed(); }
    else if (e.key === 'f') $('#fit').click();
  });

  // ---------- main loop ----------
  function update(now) {
    if (playing && !scrubbing) {
      if (last != null) {
        T += (now - last) * dir * SPEEDS[si];
        if (T >= W1) { T = W1; setPlaying(false); }
        if (T <= W0) { T = W0; setPlaying(false); }
      }
      last = now; dirty = true;
    }
    if (dirty) {
      const bounded = +$('#trail').value > 0;
      for (const t of tracks) { const i = idxAt(t, T); if (i !== t.idx || bounded) { t.idx = i; updateBold(t); } }
      markers.draw();
      if (overlayState.layers.length) applyGround(T);
      if (chat.length) chatAt(T);
      $('#utc').textContent = fmtUTC(T); $('#day').textContent = fmtDay(T); $('#local').textContent = fmtLocal(T);
      $('#pos').textContent = `${fmtDur(T - W0)} of ${fmtDur(W1 - W0)}`;
      if (!scrubbing) scrub.value = Math.round((T - W0) / (W1 - W0) * 100000);
      if (now - lastSlow > 120) { drawTimeline(); updatePanel(); lastSlow = now; }
      dirty = false;
    }
    requestAnimationFrame(update);
  }

  // ---------- go ----------
  loadGround();   // the ground arrives over the network; the tracks do not wait for it
  for (const t of tracks) { computeRuns(t); buildLines(t); }
  buildPanel(); renderSpeed(); tlLayout();
  window.addEventListener('resize', () => { tlLayout(); dirty = true; });
  const b0 = allBounds();
  if (b0.isValid()) map.fitBounds(b0.pad(0.3)); else map.setView([51.2, -1.5], 12);
  requestAnimationFrame(update);
  window.pinecone = { tracks, map, seek: (ms) => { T = ms; dirty = true; }, get T() { return T; }, setPlaying, speed: (i) => { si = i; renderSpeed(); }, // so the ground can be checked at a given moment without waiting for an animation frame
                     ground: overlayState, applyGround, moments: momentState, loadMoments, proposals: propState, loadProposals, whereWas, record: recState, loadRecords };
})();
