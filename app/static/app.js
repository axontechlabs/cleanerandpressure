import { Viewer, V3 } from './viewer.js';
import * as aiGate from './ai_gate.js';

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const viewer = new Viewer($('#gl'));
window.viewer = viewer;   // accesible desde la consola y las pruebas

const STAGES = [
  { id: 'original', name: 'Original (limpio)', color: [0.68, 0.69, 0.72] },
  { id: 'segmentado', name: 'Segmentado', color: [0.36, 0.70, 0.45] },
  { id: 'recortado', name: 'Recortado / contorno', color: [0.36, 0.60, 0.86] },
  { id: 'suavizado', name: 'Suavizado', color: [0.93, 0.64, 0.32] },
  { id: 'solido', name: 'Sólido final', color: [0.88, 0.88, 0.90] },
];
const LABEL_COLORS = [[150, 104, 100], [78, 172, 100], [236, 196, 60]];

const PARAMS = {
  segment: { title: 'Segmentación y corte de plantilla', items: [
    ['mode', 'Tipo de corte', 'plantilla', [['plantilla', 'plantilla: planta + copa de talón + pestañas'], ['planta', 'solo planta (por pendiente)']], '', ''],
    ['heel_cup', 'Copa del talón', 15, 0, 35, 0.5, 'mm', 'Altura de la pared del talón sobre el suelo (≤ 10 mm se considera baja)'],
    ['medial_flange', 'Pestaña medial (arco)', 0, 0, 45, 0.5, 'mm', '0 = automática: altura del arco medida + 6 mm'],
    ['lateral_flange', 'Pared lateral', 10, 0, 30, 0.5, 'mm', 'Altura de la pared lateral del mediopié sobre el suelo'],
    ['forefoot_wall', 'Borde del antepié', 6, 0, 20, 0.5, 'mm', 'Altura del borde en el antepié'],
    ['toe_wall', 'Borde de los dedos', 4, 0, 15, 0.5, 'mm', 'Altura del borde en los dedos'],
    ['wall_max_angle', 'Pendiente máx. de pared', 80, 60, 87, 1, '°', 'Paredes más verticales no se pueden representar como campo de alturas'],
    ['seed_angle', 'Ángulo de semilla', 35, 5, 80, 1, '°', 'Pendiente máxima de las caras claramente plantares'],
    ['max_angle', 'Ángulo máximo', 62, 10, 88, 1, '°', 'Pendiente hasta la que crece la planta (más alto = sube más por los laterales)'],
    ['crease_angle', 'Ángulo de pliegue', 80, 20, 180, 1, '°', 'Ángulo entre caras vecinas que actúa como barrera'],
    ['layer_tol', 'Separación de capas', 1.5, 0.1, 10, 0.1, 'mm', 'Distancia a partir de la cual dos superficies son capas distintas'],
    ['gap_tol', 'Distancia de islas', 6, 0, 40, 0.5, 'mm', 'Distancia máxima a la planta para conservar una isla como dudosa'],
    ['join_dz', 'Salto de altura de islas', 4, 0, 40, 0.5, 'mm', 'Salto máximo de altura entre la isla y la planta'],
    ['bridge_gap', 'Puente de huecos', 5, 0, 20, 0.5, 'mm', 'Anchura de huecos entre regiones que se rellenan con caras reales'],
    ['min_island_area', 'Área mínima de isla', 25, 0, 500, 1, 'mm²', 'Las islas menores se descartan'],
    ['normal_radius', 'Radio de normales', 3, 0.5, 15, 0.5, 'mm', 'Escala de suavizado de normales (mayor = más robusto al ruido)'],
  ] },
  cut: { title: 'Contorno y remallado', items: [
    ['resolution', 'Resolución de la malla', 0, 0, 5, 0.05, 'mm', '0 = automática según la densidad del escaneo'],
    ['contour_close', 'Cierre del contorno', 2, 0, 15, 0.25, 'mm', 'Une grietas y muescas más estrechas que el doble de este radio'],
    ['contour_open', 'Apertura del contorno', 1.5, 0, 15, 0.25, 'mm', 'Elimina picos y lenguas finas'],
    ['contour_smooth', 'Regularización', 2, 0, 15, 0.25, 'mm', 'Suaviza el trazado del contorno'],
    ['contour_inset', 'Retranqueo', 0, 0, 20, 0.25, 'mm', 'Desplaza el borde hacia el interior'],
    ['max_hole_area', 'Agujero máximo', 0, 0, 5000, 10, 'mm²', '0 = rellenar todos los agujeros interiores'],
  ] },
  smooth: { title: 'Suavizado adaptativo', items: [
    ['method', 'Método', 'multiescala', [['multiescala', 'multiescala (superficie lisa)'], ['local', 'local (conservador)']], '', ''],
    ['intensity', 'Nivel de alisado', 0.7, 0, 1, 0.05, '', '0 = no tocar · 1 = máximo. Se ajusta solo a la textura medida en cada escaneo (con o sin calcetín)'],
    ['texture_scale', 'Tamaño máx. de irregularidad', 24, 6, 60, 1, 'mm', 'Las irregularidades de pequeña amplitud hasta este tamaño se eliminan; lo mayor se considera forma del pie'],
    ['detail_protection', 'Protección de detalle', 0.5, 0, 1, 0.05, '', 'Más alto = se conserva más relieve localizado (dedos, pliegues); más bajo = más liso'],
    ['min_scale', 'Grano mínimo', 4, 1, 12, 0.5, 'mm', 'Todo detalle menor que esto se elimina siempre (grano del sensor, puntos aislados)'],
    ['max_deviation', 'Tolerancia de desviación', 2, 0, 10, 0.1, 'mm', 'Límite de cambio respecto a la superficie sin grano. 0 = sin límite'],
    ['radius', 'Radio (método local)', 4, 0.5, 15, 0.25, 'mm', 'Solo método local: escala por debajo de la cual las irregularidades se consideran ruido'],
    ['iterations', 'Iteraciones (método local)', 3, 0, 12, 1, '', 'Solo método local'],
    ['curvature_preservation', 'Preservación de curvatura (local)', 0.5, 0, 1, 0.05, '', 'Solo método local: protege pliegues y relieves marcados'],
    ['arch_protection', 'Protección del arco (local)', 0.7, 0, 1, 0.05, '', 'Solo método local: protege las zonas cóncavas'],
    ['noise_floor', 'Umbral de ruido (local)', 0.03, 0.001, 1, 0.005, 'mm', 'Solo método local: por debajo de esta rugosidad casi no se interviene'],
  ] },
  solid: { title: 'Sólido cerrado', items: [
    ['thickness', 'Espesor', 5, 0.2, 100, 0.5, 'mm', 'Espesor mínimo bajo la superficie'],
    ['mode', 'Base', 'flat', [['flat', 'plana'], ['offset', 'espesor uniforme']], '', ''],
    ['side', 'Material', 'interior', [['interior', 'hacia el interior del pie (positivo)'], ['exterior', 'bajo la planta (plantilla)']], '', ''],
  ] },
};

const S = { state: null, labels: null, manual: null, adj: null, tool: 'nav', undo: [], redo: [], planes: [],
  visible: {}, loaded: {}, busy: false, stroke: null, planeFlip: false, devRange: 0 };

// ------------------------------------------------------------------ utilidades
function setStatus(text, cls = 'idle') { const el = $('#status'); el.textContent = text; el.className = cls; el.title = text; }
const fmt = (x, d = 2) => (x === null || x === undefined || Number.isNaN(x)) ? '–' : (typeof x === 'number' ? (Number.isInteger(x) ? x.toLocaleString('es') : x.toFixed(d)) : String(x));
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

async function api(path, { method = 'POST', json = undefined, body = undefined } = {}) {
  const opt = { method };
  if (json !== undefined) { opt.body = JSON.stringify(json); opt.headers = { 'Content-Type': 'application/json' }; }
  else if (body !== undefined) opt.body = body;
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({ error: `Respuesta no válida (${r.status})` }));
  if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
  return data;
}
async function bin(path) {
  const r = await fetch(path);
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || `Error ${r.status}`); }
  return r.arrayBuffer();
}
function b64(u8) { let s = ''; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); }

async function run(label, fn) {
  if (S.busy) return;
  S.busy = true; setStatus(label + '…', 'busy'); updateButtons();
  try {
    const msg = await fn();
    if (msg !== false) setStatus(typeof msg === 'string' ? msg : label + ': hecho', (msg && msg.cls) || 'ok');
    if (msg && msg.text) setStatus(msg.text, msg.cls);
  } catch (e) { console.error(e); setStatus(e.message, 'err'); }
  S.busy = false; updateButtons(); renderReport();
}

// ------------------------------------------------------------------ parámetros
function buildParams() {
  const root = $('#params'); root.innerHTML = '';
  for (const [group, def] of Object.entries(PARAMS)) {
    const d = document.createElement('details'); d.innerHTML = `<summary>${def.title}</summary>`;
    for (const [key, label, dflt, a, b, step, unit, tip] of def.items) {
      const row = document.createElement('label'); row.className = 'param'; row.title = tip || '';
      if (Array.isArray(a)) {
        row.innerHTML = `<span>${label}</span><select data-g="${group}" data-k="${key}">${a.map(([v, t]) => `<option value="${v}"${v === dflt ? ' selected' : ''}>${t}</option>`).join('')}</select>`;
      } else {
        row.innerHTML = `<span>${label} <small>${unit}</small></span><input type="number" data-g="${group}" data-k="${key}" value="${dflt}" min="${a}" max="${b}" step="${step}">`;
      }
      d.appendChild(row);
    }
    root.appendChild(d);
  }
}
function setParams(group, values) {
  for (const el of $$(`#params [data-g="${group}"]`)) {
    const v = (values || {})[el.dataset.k];
    if (v === undefined || v === null) continue;
    el.value = v; el.dispatchEvent(new Event('input'));
  }
}
function getParams(group) {
  const out = {};
  for (const el of $$(`#params [data-g="${group}"]`)) out[el.dataset.k] = el.tagName === 'SELECT' ? el.value : Number(el.value);
  return out;
}

// ------------------------------------------------------------------ mallas y etapas
async function loadMesh(stage) {
  const buf = await bin('/api/mesh/' + stage);
  const [nV, nF] = new Uint32Array(buf, 0, 2);
  const vertices = new Float32Array(buf, 8, nV * 3), indices = new Uint32Array(buf, 8 + nV * 12, nF * 3);
  viewer.setMesh(stage, vertices, indices, { color: STAGES.find((s) => s.id === stage).color });
  S.loaded[stage] = true;
}

async function syncStages(state, from, show) {
  S.state = state;
  const i0 = STAGES.findIndex((s) => s.id === from);
  for (let i = 0; i < STAGES.length; i++) {
    const id = STAGES[i].id, avail = state.stages.includes(id);
    if (avail && (i >= i0 || !S.loaded[id])) await loadMesh(id);
    if (!avail && S.loaded[id]) { viewer.removeMesh(id); S.loaded[id] = false; }
  }
  const newest = show || [...state.stages].pop();
  for (const s of STAGES) S.visible[s.id] = s.id === newest;
  if (!S.loaded.suavizado) { S.devField = null; }
  applyVisibility();
}

function applyVisibility() {
  const vis = STAGES.filter((s) => S.visible[s.id] && S.loaded[s.id]);
  const top = vis.length ? vis[vis.length - 1].id : null;
  const op = Number($('#opacity').value);
  for (const s of STAGES) {
    const m = viewer.meshes.get(s.id); if (!m) continue;
    m.visible = !!S.visible[s.id];
    m.alpha = (vis.length > 1 && s.id !== top) ? op : 1;
    m.clip = s.id === 'segmentado' && $('#plane-on').checked;
    if (s.id === 'original') m.useVC = !!S.labels && $('#t-labels').checked;
    if (s.id === 'suavizado') m.useVC = !!S.devField && $('#t-dev').checked;
  }
  S.top = top;
  renderStages(); updateOverlays(); updateLegend(); viewer.requestRender();
}

function renderStages() {
  const root = $('#stages'); root.innerHTML = '';
  const avail = S.state ? S.state.stages : [];
  STAGES.forEach((s, i) => {
    const on = avail.includes(s.id);
    const row = document.createElement('div'); row.className = 'stage' + (on ? '' : ' off');
    row.innerHTML = `<input type="checkbox" ${S.visible[s.id] && on ? 'checked' : ''} ${on ? '' : 'disabled'}>
      <i style="background:rgb(${s.color.map((c) => Math.round(c * 255)).join(',')})"></i><span>${s.name}</span>`;
    row.querySelector('input').onchange = (e) => { S.visible[s.id] = e.target.checked; applyVisibility(); };
    if (on && i < avail.length - 1) {
      const b = document.createElement('button'); b.className = 'btn small'; b.textContent = 'Volver';
      b.title = 'Descartar las etapas posteriores y continuar desde aquí';
      b.onclick = () => run('Volviendo a ' + s.name, async () => {
        const r = await api('/api/revert', { json: { stage: s.id } });
        await syncStages(r.state, s.id, s.id);
      });
      row.appendChild(b);
    }
    root.appendChild(row);
  });
}

function paintLabels(f0 = 0, f1 = null) {
  if (!S.labels || !S.loaded.original) return;
  const L = S.labels;
  viewer.paintFaces('original', (f) => LABEL_COLORS[L[f]], f0, f1);
}

function updateLegend() {
  const el = $('#legend'); let h = '';
  const o = viewer.meshes.get('original'), sm = viewer.meshes.get('suavizado');
  if (o && o.visible && o.useVC) h += '<i style="background:rgb(78,172,100)"></i>conservar<i style="background:rgb(236,196,60)"></i>dudosa<i style="background:rgb(150,104,100)"></i>eliminar';
  if (sm && sm.visible && sm.useVC) h += `<i style="background:#3b6fd6"></i>−${S.devRange.toFixed(2)} mm<i style="background:#eee"></i>0<i style="background:#d6493b"></i>+${S.devRange.toFixed(2)} mm (desplazamiento del suavizado)`;
  el.innerHTML = h;
}

async function updateOverlays() {
  const top = S.top;
  if ($('#t-edges').checked && top) {
    try { viewer.setLines('edges', new Float32Array(await bin('/api/edges/' + top)), [1, 0.25, 0.2]); } catch (e) { viewer.setLines('edges', null); }
  } else viewer.setLines('edges', null);
  if ($('#t-normals').checked && top) {
    const m = viewer.meshes.get(top), p = m.positions, step = Math.max(1, Math.floor(m.nF / 12000));
    const len = (viewer.sceneRadius || 100) * 0.02, seg = [];
    for (let f = 0; f < m.nF; f += step) {
      const a = [p[f * 9], p[f * 9 + 1], p[f * 9 + 2]], b = [p[f * 9 + 3], p[f * 9 + 4], p[f * 9 + 5]], c = [p[f * 9 + 6], p[f * 9 + 7], p[f * 9 + 8]];
      const n = V3.norm(V3.cross(V3.sub(b, a), V3.sub(c, a))), g = V3.scale(V3.add(V3.add(a, b), c), 1 / 3);
      seg.push(...g, ...V3.add(g, V3.scale(n, len)));
    }
    viewer.setLines('normals', new Float32Array(seg), [0.3, 0.85, 1.0]);
  } else viewer.setLines('normals', null);
}

async function loadDeviation() {
  const m = viewer.meshes.get('suavizado'); if (!m) return;
  const f = new Float32Array(await bin('/api/field/displacement'));
  const sorted = Float32Array.from(f, Math.abs).sort();
  const rng = Math.max(sorted[Math.floor(sorted.length * 0.99)] || 0, 0.02);
  S.devRange = rng; S.devField = f;
  const idx = m.indices;
  viewer.paintFaces('suavizado', (face) => {
    const v = (f[idx[face * 3]] + f[idx[face * 3 + 1]] + f[idx[face * 3 + 2]]) / 3;
    const t = Math.max(-1, Math.min(1, v / rng)), a = Math.abs(t);
    return t >= 0 ? [238 - 24 * a, 238 - 165 * a, 238 - 179 * a] : [238 - 179 * a, 238 - 127 * a, 238 - 24 * a];
  });
}

// ------------------------------------------------------------------ vistas
function setView(kind) {
  const fr = S.state && S.state.frame; if (!fr) return;
  const p = fr.plantar_axis, l = fr.long_axis, w = fr.width_axis, neg = (v) => V3.scale(v, -1);
  if (kind === 'fit') { fitAll(); return; }
  const table = { top: [p, l], bottom: [neg(p), l], front: [l, p], back: [neg(l), p], side: [w, p], side2: [neg(w), p] };
  viewer.setView(...table[kind]);
}
function fitAll() {
  let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity], any = false;
  for (const s of STAGES) {
    const m = viewer.meshes.get(s.id); if (!m || !m.visible) continue; any = true;
    for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], m.bounds[0][k]); hi[k] = Math.max(hi[k], m.bounds[1][k]); }
  }
  if (any) viewer.fit([lo, hi]);
}

// ------------------------------------------------------------------ carga
async function afterLoad(res) {
  Object.assign(S, { labels: null, manual: null, adj: null, undo: [], redo: [], planes: [], loaded: {}, devField: null, flipped: false });
  for (const s of STAGES) viewer.removeMesh(s.id);
  viewer.clip = null; viewer.plane = null; $('#plane-on').checked = false; $('#plane-count').textContent = '0';
  $('#filename').textContent = res.state.file;
  await syncStages(res.state, 'original', 'original');
  S.manual = new Uint8Array(viewer.meshes.get('original').nF);
  setView('top'); fitAll();
  const w = res.report.warnings || [];
  return w.length ? { text: 'STL cargado. ' + w[0], cls: 'warn' } : 'STL cargado: ' + res.report.faces.toLocaleString('es') + ' caras';
}
function uploadFile(file) {
  run('Cargando ' + file.name, async () => afterLoad(await api('/api/upload?name=' + encodeURIComponent(file.name), { body: await file.arrayBuffer() })));
}

// ------------------------------------------------------------------ operaciones
async function fetchLabels() {
  S.labels = new Uint8Array(await bin('/api/labels'));
  if (!S.adj) S.adj = new Int32Array(await bin('/api/adjacency'));
  S.undo = []; S.redo = [];
}
const manualB64 = () => (S.manual ? b64(S.manual) : null);

const OPS = {
  analyze: () => run('Analizando', async () => { const r = await api('/api/analyze'); S.state = r.state; const n = r.report.warnings.length; return n ? { text: `Análisis: ${n} aviso(s)`, cls: 'warn' } : 'Análisis completado'; }),
  segment: (mainFace = null) => run('Segmentando la planta', async () => {
    const r = await api('/api/segment', { json: { params: getParams('segment'), manual_b64: manualB64(), main_face: mainFace } });
    await fetchLabels(); $('#t-labels').checked = true;
    await syncStages(r.state, 'segmentado', 'original'); paintLabels();
    const u = r.report.uncertain_regions;
    return u ? { text: `Segmentación: ${u} región(es) dudosa(s) en amarillo; revíselas`, cls: 'warn' } : `Segmentación: se elimina el ${r.report.removed_area_pct.toFixed(1)}% del área`;
  }),
  extract: () => run('Eliminando geometría no deseada', async () => {
    const r = await api('/api/extract', { body: S.labels });
    await syncStages(r.state, 'segmentado');
    return `Planta aislada: ${r.report.faces.toLocaleString('es')} caras`;
  }),
  cut: () => run('Recortando y generando el contorno', async () => {
    const planes = [...S.planes];
    if ($('#plane-on').checked) { planes.push(currentPlane()); S.planes = planes; setPlane(false); }
    const r = await api('/api/cut', { json: { planes, params: getParams('cut') } });
    $('#plane-count').textContent = S.planes.length;
    await syncStages(r.state, 'recortado');
    const w = r.report.warnings; return w.length ? { text: w[0], cls: 'warn' } : `Contorno limpio: ${r.report.boundary_loops} contorno, resolución ${r.report.resolution.toFixed(2)} mm`;
  }),
  smooth: () => run('Suavizando', async () => {
    const r = await api('/api/smooth', { json: { params: getParams('smooth') } });
    await syncStages(r.state, 'suavizado');
    if ($('#t-dev').checked) { await loadDeviation(); applyVisibility(); }
    const w = r.report.warnings; return w.length ? { text: w[0], cls: 'warn' } : `Suavizado: desplazamiento medio ${r.report.displacement_mean.toFixed(3)} mm, máx. ${r.report.displacement_max.toFixed(2)} mm`;
  }),
  solid: () => run('Creando el sólido', async () => {
    const r = await api('/api/solid', { json: { params: getParams('solid') } });
    await syncStages(r.state, 'solido');
    return `Sólido creado (${r.report.faces.toLocaleString('es')} caras). Valídelo antes de exportar`;
  }),
  validate: () => run('Validando', async () => {
    const r = await api('/api/validate'); S.state = r.state;
    return r.report.valid ? { text: 'Sólido VÁLIDO: estanco, variedad y sin autointersecciones', cls: r.report.warnings.length ? 'warn' : 'ok' }
      : { text: 'Sólido NO válido: ' + r.report.errors[0], cls: 'err' };
  }),
  export: () => doExport(),
};

// ------------------------------------------------------------------ revisión por IA
function aiCut() {
  run('La IA está revisando el corte (puede tardar 1–3 min)', async () => {
    const r = await api('/api/ai/cut', { json: { rounds: Number($('#ai-rounds').value) || 6,
      segment: getParams('segment'), cut: getParams('cut'), planes: [...S.planes] } });
    if (r.report.final_params) { setParams('segment', r.report.final_params.segment); setParams('cut', r.report.final_params.cut); }
    await fetchLabels();
    await syncStages(r.state, 'segmentado'); paintLabels();
    $('#ai-views').href = '/api/cut_views.png?t=' + Date.now(); $('#ai-views').hidden = false;
    const h = r.report.history || [], last = h[h.length - 1] || {};
    const v = r.report.validacion;
    const msg = `IA (${h.length} ronda/s): ${last.diagnosis || ''}`;
    return { text: msg + (v && !v.valid ? ' · sólido NO válido' : ''), cls: last.verdict === 'correcto' && v && v.valid ? 'ok' : 'warn' };
  });
}

function autoAll() {
  const noAi = $('#ai-off').checked;
  run(noAi ? 'Procesamiento automático' : 'Procesamiento automático: la IA revisa y reajusta el corte (1–4 min)', async () => {
    const planes = [...S.planes];
    const r = await api('/api/auto', { json: { segment: getParams('segment'), cut: getParams('cut'), smooth: getParams('smooth'),
      solid: getParams('solid'), planes, manual_b64: manualB64(), resegment: true,
      sin_ia: noAi, rounds: Number($('#ai-rounds').value) || 6 } });
    if (r.report.ia && r.report.ia.final_params) { setParams('segment', r.report.ia.final_params.segment); setParams('cut', r.report.ia.final_params.cut); }
    await fetchLabels();
    await syncStages(r.state, 'segmentado'); paintLabels();
    if (r.report.ia) { $('#ai-views').href = '/api/cut_views.png?t=' + Date.now(); $('#ai-views').hidden = false; }
    const v = r.report.validacion;
    if (!v.valid) return { text: 'Resultado NO válido: ' + v.errors[0], cls: 'err' };
    return v.warnings.length ? { text: 'Sólido válido, con avisos: ' + v.warnings[0], cls: 'warn' } : { text: 'Sólido válido y listo para exportar', cls: 'ok' };
  });
}

async function doExport(original = false) {
  run('Exportando', async () => {
    const url = original ? '/api/original' : `/api/export?stage=${$('#exp-stage').value}&format=${$('#exp-format').value}&frame=${$('#exp-frame').value}`;
    const r = await fetch(url);
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || 'No se pudo exportar'); }
    const name = (r.headers.get('Content-Disposition') || '').match(/filename="(.+)"/);
    const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob()); a.download = name ? name[1] : 'malla.stl';
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    S.state = await api('/api/state', { method: 'GET' });
    return 'Archivo exportado: ' + a.download;
  });
}

function updateButtons() {
  const st = S.state, has = (s) => !!st && st.stages.includes(s), rep = (k) => !!st && !!st.reports[k], b = S.busy;
  const en = { analyze: has('original'), segment: has('original'), extract: !!S.labels, cut: has('segmentado'), smooth: has('recortado'),
    solid: has('recortado'), validate: has('solido'), export: has('original') };
  const done = { analyze: rep('analisis'), segment: !!S.labels, extract: has('segmentado'), cut: has('recortado'), smooth: has('suavizado'),
    solid: has('solido'), validate: rep('validacion'), export: false };
  for (const el of $$('[data-op]')) { el.disabled = b || !en[el.dataset.op]; el.classList.toggle('done', done[el.dataset.op]); }
  $('#op-auto').disabled = b || !has('original');
  $('#ai-cut').disabled = b || !has('original');
  $('#undo').disabled = !S.undo.length; $('#redo').disabled = !S.redo.length;
  $('#clear-manual').disabled = b || !(S.manual && S.manual.some((v) => v));
  $('#flip').disabled = $('#axis-view').disabled = b || !has('original');
  $('#plane-on').disabled = !has('segmentado');
  $('#plane-apply').disabled = $('#plane-cancel').disabled = b || !$('#plane-on').checked;
  $('#plane-clear').disabled = b || !S.planes.length;
  $('#exp-go').disabled = $('#exp-original').disabled = b || !has('original');
}

// ------------------------------------------------------------------ informe
function rows(pairs) { return '<table>' + pairs.filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('') + '</table>'; }
function msgs(list, cls) { return (list || []).map((w) => `<div class="msg ${cls}">${esc(w)}</div>`).join(''); }
function renderReport() {
  const st = S.state; if (!st) return;
  const R = st.reports; let h = '';
  const yes = (b) => (b ? 'sí' : 'no');
  if (R.validacion) {
    const v = R.validacion, q = v.quality || {}, d = q.deviation || {};
    h += `<h3>Validación del sólido</h3><div class="msg ${v.valid ? 'ok' : 'err'}">${v.valid ? 'VÁLIDO: supera todas las comprobaciones' : 'NO VÁLIDO: no lo use para fabricar'}</div>`;
    h += v.checks.map((c) => `<div class="check ${c.ok ? 'ok' : 'bad'}"><b>${c.ok ? '✓' : '✗'}</b><span>${esc(c.name)}</span><small>${esc(fmt(c.value))} ${esc(c.detail || '')}</small></div>`).join('');
    h += msgs(v.errors, 'err') + msgs(v.warnings, 'warn');
    h += '<h3>Control de calidad</h3>' + rows([
      ['Volumen', fmt(v.volume / 1000, 1) + ' cm³'], ['Geometría eliminada (caras / área)', `${fmt(q.removed_faces_pct, 1)}% / ${fmt(q.removed_area_pct, 1)}%`],
      ['Desviación respecto al escaneo (media)', fmt(d.mean, 3) + ' mm'], ['Desviación RMS / P95 / máx.', `${fmt(d.rms, 3)} / ${fmt(d.p95, 3)} / ${fmt(d.max, 2)} mm`],
      ['Planta preservada (±' + fmt(q.preservation_tolerance, 1) + ' mm)', fmt(q.preservation_pct, 1) + '%'],
      ['Vértices interpolados (agujeros)', fmt(q.interpolated_vertices_pct, 1) + '%'],
      q.arch_depth_before !== undefined && ['Profundidad del arco (antes → después)', `${fmt(q.arch_depth_before)} → ${fmt(q.arch_depth_after)} mm`],
      q.curvature_relative_change !== undefined && ['Cambio relativo de curvatura', fmt(100 * q.curvature_relative_change, 1) + '%'],
      ['Ángulo mínimo de triángulo', fmt(q.min_angle_deg, 1) + '°'],
    ]);
  }
  if (R.solido) { const r = R.solido; h += '<h3>Sólido</h3>' + rows([['Caras / vértices', `${fmt(r.faces)} / ${fmt(r.vertices)}`], ['Volumen', fmt(r.volume / 1000, 1) + ' cm³'], ['Espesor mín. / máx.', `${fmt(r.thickness_min)} / ${fmt(r.thickness_max)} mm`]]) + msgs(r.warnings, 'warn'); }
  if (R.suavizado) { const r = R.suavizado; const ms = r.method === 'multiescala';
    h += '<h3>Suavizado adaptativo</h3>' + rows([
    ms && ['Textura medida en este escaneo', `${fmt(r.texture_level, 3)} mm (pendiente ${fmt(1000 * r.texture_slope, 1)} µm/mm)`],
    ms && ['Grano fino eliminado (< ' + fmt(r.params.min_scale, 0) + ' mm)', fmt(r.fine_level, 3) + ' mm'],
    ['Rugosidad local (mediana)', `${fmt(r.noise_before_median, 3)} → ${fmt(r.noise_after_median, 3)} mm`],
    ['Rugosidad local (P95)', `${fmt(r.noise_before_p95, 3)} → ${fmt(r.noise_after_p95, 3)} mm`],
    ['Desplazamiento medio / máx.', `${fmt(r.displacement_mean, 3)} / ${fmt(r.displacement_max, 2)} mm`],
    ['Intervención media', fmt(100 * r.intervention_mean, 0) + '%'],
    ['Profundidad del arco', `${fmt(r.arch_depth_before)} → ${fmt(r.arch_depth_after)} mm`],
    !ms && ['Cambio relativo de curvatura', fmt(100 * r.curvature_relative_change, 1) + '%'],
    [ms ? 'Zona con relieve conservado' : 'Zona cóncava protegida', fmt(100 * r.protected_area_fraction, 0) + '%']]);
    if (ms && r.bands) h += '<div class="hint">Análisis por tamaño de detalle: nivel típico medido y parte de la superficie donde se conserva por ser relieve real.</div>' + rows(r.bands.map((b) =>
      [`· ${fmt(b.from, 0)}–${fmt(b.to, 0)} mm`, `${fmt(b.level, 3)} mm · conservado ${fmt(b.kept_pct, 0)}%`]));
    h += msgs(r.warnings, 'warn'); }
  if (R.recortado && R.recortado.ia) { const ia = R.recortado.ia; h += '<h3>Revisión por IA</h3>' + rows([
    ['Modelo', esc(ia.model)], ['Llamadas / tokens', `${fmt(ia.usage.calls)} / ${fmt(ia.usage.input_tokens + ia.usage.output_tokens)}`]])
    + ia.history.map((e) => `<div class="hint"><b>Ronda ${e.round}: ${esc(e.verdict || '')}</b> — ${esc(e.diagnosis || '')}`
      + (Object.keys(e.changes.segment).length || Object.keys(e.changes.cut).length ? `<br>Cambios: ${esc(JSON.stringify({ ...e.changes.segment, ...e.changes.cut }))}` : '')
      + (e.reverted ? '<br>(deshecho: empeoraba la cobertura)' : '') + '</div>').join(''); }
  if (R.recortado && R.recortado.cobertura && R.recortado.cobertura.zones) { const c = R.recortado.cobertura; h += '<h3>Plantilla: cobertura</h3>' + rows([
    ...Object.values(c.zones).map((z) => ['· ' + esc(z.name), fmt(100 * z.coverage, 0) + '%']),
    ...Object.entries(c.walls || {}).map(([k, w]) => ['· Pared ' + esc(k.replace('_', ' ')), `${fmt(w.reached_mm, 0)} / ${fmt(w.target_mm, 0)} mm`]),
    ['Longitud (posible)', `${fmt(c.length_mm, 0)} (${fmt(c.length_possible_mm, 0)}) mm`], ['Puntuación', fmt(c.score, 3)],
    ['Reajustes automáticos', fmt((R.recortado.autotune || []).length)]]) + msgs(c.problems || [], 'warn'); }
  if (R.recortado) { const r = R.recortado; h += '<h3>Recorte y contorno</h3>' + rows([
    ['Resolución', fmt(r.resolution) + ' mm'], ['Caras / vértices', `${fmt(r.faces)} / ${fmt(r.vertices)}`],
    ['Contornos / longitud', `${r.boundary_loops} / ${fmt(r.contour_length, 0)} mm`], ['Huella', fmt(r.footprint_area / 100, 1) + ' cm²'],
    ['Agujeros rellenados', fmt(r.holes_filled)], ['Área interpolada', fmt(r.interpolated_area, 0) + ' mm²'], ['Planos de corte', fmt(r.planes)]]) + msgs(r.warnings, 'warn'); }
  if (R.segmentacion) { const r = R.segmentacion; h += '<h3>Segmentación</h3>' + rows([
    ['Caras conservadas', `${fmt(r.faces_kept)} de ${fmt(r.faces_total)}`], ['Eliminado (caras / área)', `${fmt(r.removed_faces_pct, 1)}% / ${fmt(r.removed_area_pct, 1)}%`],
    ['Regiones dudosas', fmt(r.uncertain_regions)], ...Object.entries(r.reason_counts).map(([k, v]) => ['· ' + esc(k), fmt(v)])]) + msgs(r.warnings, 'warn'); }
  if (R.analisis) { const r = R.analisis, e = r.edge_length; h += '<h3>Análisis del STL</h3>' + rows([
    ['Vértices / caras', `${fmt(r.vertices)} / ${fmt(r.faces)}`], ['Dimensiones', r.dimensions.map((x) => fmt(x, 1)).join(' × ') + ' mm'],
    ['Unidades del archivo', esc(r.unit_guess)], ['Componentes', fmt(r.components)], ['Bordes abiertos (aristas)', fmt(r.boundary_edges)],
    ['Aristas / vértices no variedad', `${fmt(r.nonmanifold_edges)} / ${fmt(r.nonmanifold_vertices)}`], ['Caras degeneradas', fmt(r.degenerate_faces)],
    ['Caras / vértices duplicados', `${fmt(r.duplicate_faces)} / ${fmt(r.duplicate_vertices)}`], ['Aristas con normales incoherentes', fmt(r.inconsistent_normal_edges)],
    ['Pares de triángulos que se intersecan', r.self_intersecting_pairs === null ? 'no calculado' : fmt(r.self_intersecting_pairs)],
    ['Capas superpuestas (proyección)', fmt(100 * r.multi_layer_fraction, 1) + '%'], ['Estanca / variedad', `${yes(r.watertight)} / ${yes(r.manifold)}`],
    ['Arista: mediana (P5–P95)', `${fmt(e.median)} (${fmt(e.p05)}–${fmt(e.p95)}) mm`], ['Variación de densidad (P95/P5)', '×' + fmt(r.density_variation, 1)],
    ['Orientación plantar', esc(r.orientation.method || '')]]) + msgs(r.warnings, 'warn'); }
  if (R.carga) { const r = R.carga; h += '<h3>Archivo</h3>' + rows([['Nombre', esc(r.file)], ['Triángulos', fmt(r.triangles_file)],
    ['Dimensiones', r.dimensions_mm.map((x) => fmt(x, 1)).join(' × ') + ' mm'], ['SHA-256', r.sha256.slice(0, 16) + '…']]) + (R.analisis ? '' : msgs(r.warnings, 'warn')); }
  $('#report').innerHTML = h || '<p class="hint">Sin datos todavía.</p>';
}

// ------------------------------------------------------------------ edición manual
function setTool(t) {
  S.tool = t;
  $$('.tool').forEach((b) => b.classList.toggle('active', b.dataset.tool === t));
  $('#gl').classList.toggle('brush', t !== 'nav');
  if (t === 'nav') viewer.setLines('brush', null);
}
function canEdit() {
  if (!S.labels) { setStatus('Segmente primero la superficie plantar (operación 2)', 'warn'); return false; }
  if (!S.visible.original) { for (const s of STAGES) S.visible[s.id] = s.id === 'original'; $('#t-labels').checked = true; applyVisibility(); }
  return true;
}
function setFace(f, lab, man, rec) {
  if (S.labels[f] === lab && S.manual[f] === man) return false;
  if (!rec.has(f)) rec.set(f, [S.labels[f], S.manual[f]]);
  S.labels[f] = lab; S.manual[f] = man; return true;
}
function brushAt(x, y) {
  const hit = viewer.pick(x, y, 'original');
  const r = Number($('#brush').value);
  if (!hit) { viewer.setLines('brush', null); return; }
  const ax = viewer.axes, seg = [];
  for (let i = 0; i < 40; i++) {
    const a0 = i / 40 * 2 * Math.PI, a1 = (i + 1) / 40 * 2 * Math.PI;
    const p = (a) => V3.add(hit.point, V3.add(V3.scale(ax.x, r * Math.cos(a)), V3.scale(ax.y, r * Math.sin(a))));
    seg.push(...p(a0), ...p(a1));
  }
  viewer.setLines('brush', new Float32Array(seg), [1, 1, 1]);
  if (!S.stroke) return;
  const keep = S.tool === 'keep', lab = keep ? 1 : 0, man = keep ? 1 : 2;
  const c = viewer.centroids('original'), n = S.labels.length, r2 = r * r, [hx, hy, hz] = hit.point;
  let lo = n, hi = -1;
  for (let f = 0; f < n; f++) {
    const dx = c[f * 3] - hx, dy = c[f * 3 + 1] - hy, dz = c[f * 3 + 2] - hz;
    if (dx * dx + dy * dy + dz * dz <= r2 && setFace(f, lab, man, S.stroke)) { if (f < lo) lo = f; if (f > hi) hi = f; }
  }
  if (hi >= 0) paintLabels(lo, hi + 1);
}
function commit(rec) {
  if (!rec || !rec.size) return;
  const faces = Int32Array.from(rec.keys()), old = Uint8Array.from([...rec.values()].flat());
  const cur = new Uint8Array(faces.length * 2);
  faces.forEach((f, i) => { cur[i * 2] = S.labels[f]; cur[i * 2 + 1] = S.manual[f]; });
  S.undo.push({ faces, old, cur }); S.redo = []; if (S.undo.length > 200) S.undo.shift();
  updateButtons();
}
function applyEdit(e, which) {
  e.faces.forEach((f, i) => { S.labels[f] = e[which][i * 2]; S.manual[f] = e[which][i * 2 + 1]; });
  paintLabels(); updateButtons();
}
function groupAt(x, y, keep) {
  const hit = viewer.pick(x, y, 'original'); if (!hit) return;
  const start = hit.face, lab0 = S.labels[start], rec = new Map(), seen = new Uint8Array(S.labels.length), stack = [start];
  seen[start] = 1;
  while (stack.length) {
    const f = stack.pop();
    for (let k = 0; k < 3; k++) { const g = S.adj[f * 3 + k]; if (g >= 0 && !seen[g] && S.labels[g] === lab0) { seen[g] = 1; stack.push(g); } }
  }
  for (let f = 0; f < seen.length; f++) if (seen[f]) setFace(f, keep ? 1 : 0, keep ? 1 : 2, rec);
  paintLabels(); commit(rec);
  setStatus(`${rec.size.toLocaleString('es')} caras marcadas para ${keep ? 'conservar' : 'eliminar'}`, 'ok');
}

// ------------------------------------------------------------------ plano de corte
function currentPlane() {
  const fr = S.state.frame, ax = { long: fr.long_axis, width: fr.width_axis, plantar: fr.plantar_axis };
  const which = $('#plane-axis').value, base = ax[which];
  const others = ['long', 'width', 'plantar'].filter((k) => k !== which).map((k) => ax[k]);
  const a = Number($('#plane-a').value) * Math.PI / 180, b = Number($('#plane-b').value) * Math.PI / 180;
  let n = V3.norm(V3.rot(V3.rot(base, others[0], a), others[1], b));
  const m = viewer.meshes.get('segmentado'), c = [0, 1, 2].map((k) => (m.bounds[0][k] + m.bounds[1][k]) / 2);
  const point = V3.add(c, V3.scale(n, Number($('#plane-off').value)));
  if (S.planeFlip) n = V3.scale(n, -1);
  return { point, normal: n };
}
function updatePlane() {
  $('#plane-off-out').textContent = $('#plane-off').value + ' mm';
  $('#plane-a-out').textContent = $('#plane-a').value + '°'; $('#plane-b-out').textContent = $('#plane-b').value + '°';
  if (!$('#plane-on').checked || !S.loaded.segmentado) { viewer.clip = null; viewer.plane = null; viewer.requestRender(); return; }
  const pl = currentPlane(), m = viewer.meshes.get('segmentado');
  viewer.clip = { n: pl.normal, d: V3.dot(pl.normal, pl.point) };
  viewer.plane = { center: pl.point, n: pl.normal, size: 0.42 * V3.len(V3.sub(m.bounds[1], m.bounds[0])) };
  viewer.requestRender();
}
function setPlane(on) {
  $('#plane-on').checked = on;
  if (on) { for (const s of STAGES) S.visible[s.id] = s.id === 'segmentado'; }
  applyVisibility(); updatePlane(); updateButtons();
}

// ------------------------------------------------------------------ eventos
function bindUI() {
  $('#file').onchange = (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); e.target.value = ''; };
  $('#sample').onchange = (e) => { const v = e.target.value; e.target.value = ''; if (v) run('Generando ejemplo', async () => afterLoad(await api('/api/sample/' + v))); };
  $$('[data-op]').forEach((b) => { b.onclick = () => OPS[b.dataset.op](); });
  $('#op-auto').onclick = autoAll;
  $('#ai-cut').onclick = aiCut;
  $('#ai-config').onclick = () => aiGate.open();
  $('#ai-off').addEventListener('change', () => aiGate.refresh());
  aiGate.initAiGate();
  $$('.tool').forEach((b) => { b.onclick = () => setTool(b.dataset.tool); });
  $('#brush').oninput = (e) => { $('#brush-out').textContent = e.target.value + ' mm'; };
  $('#undo').onclick = () => { const e = S.undo.pop(); if (e) { S.redo.push(e); applyEdit(e, 'old'); } };
  $('#redo').onclick = () => { const e = S.redo.pop(); if (e) { S.undo.push(e); applyEdit(e, 'cur'); } };
  $('#clear-manual').onclick = () => { S.manual.fill(0); OPS.segment(); };
  $('#flip').onclick = () => run('Invirtiendo el lado plantar', async () => {
    S.flipped = !S.flipped;
    const r = await api('/api/orientation', { json: { flip: S.flipped } });
    S.labels = null; S.manual.fill(0); S.undo = []; S.redo = []; await syncStages(r.state, 'segmentado', 'original'); setView('top');
    return 'Lado plantar invertido: vuelva a segmentar';
  });
  $('#axis-view').onclick = () => run('Fijando la dirección plantar', async () => {
    S.flipped = false;
    const r = await api('/api/orientation', { json: { axis: viewer.axes.z } });
    S.labels = null; S.manual.fill(0); S.undo = []; S.redo = []; await syncStages(r.state, 'segmentado', 'original');
    return 'Dirección plantar fijada a la vista actual: vuelva a segmentar';
  });
  $$('[data-view]').forEach((b) => { b.onclick = () => setView(b.dataset.view); });
  $('#t-wire').onchange = (e) => { viewer.wire = e.target.checked; viewer.requestRender(); };
  $('#t-edges').onchange = $('#t-normals').onchange = $('#t-labels').onchange = () => applyVisibility();
  $('#t-dev').onchange = async (e) => {
    if (e.target.checked && S.loaded.suavizado) { try { await loadDeviation(); for (const s of STAGES) S.visible[s.id] = s.id === 'suavizado'; } catch (err) { setStatus(err.message, 'err'); } }
    else if (e.target.checked) setStatus('El mapa de desviación requiere ejecutar el suavizado', 'warn');
    applyVisibility();
  };
  $('#opacity').oninput = () => applyVisibility();
  $('#plane-on').onchange = (e) => setPlane(e.target.checked);
  ['#plane-off', '#plane-a', '#plane-b'].forEach((s) => { $(s).oninput = updatePlane; });
  $('#plane-axis').onchange = updatePlane;
  $('#plane-flip').onclick = () => { S.planeFlip = !S.planeFlip; updatePlane(); };
  $('#plane-apply').onclick = () => OPS.cut();
  $('#plane-cancel').onclick = () => setPlane(false);
  $('#plane-clear').onclick = () => { S.planes = []; $('#plane-count').textContent = '0'; if (S.state.stages.includes('recortado')) OPS.cut(); else updateButtons(); };
  $('#params-reset').onclick = buildParams;
  $('#exp-go').onclick = () => doExport(false);
  $('#exp-original').onclick = () => doExport(true);

  const cv = $('#gl'); let drag = null;
  cv.addEventListener('contextmenu', (e) => e.preventDefault());
  cv.addEventListener('pointerdown', (e) => {
    cv.setPointerCapture(e.pointerId);
    const tool = S.tool, left = e.button === 0;
    if (tool !== 'nav' && left) {
      if (!canEdit()) return;
      if (tool === 'keep' || tool === 'remove') { S.stroke = new Map(); brushAt(e.offsetX, e.offsetY); }
      else if (tool === 'group-keep' || tool === 'group-remove') groupAt(e.offsetX, e.offsetY, tool === 'group-keep');
      else if (tool === 'main') { const hit = viewer.pick(e.offsetX, e.offsetY, 'original'); if (hit) { setTool('nav'); OPS.segment(hit.face); } }
      return;
    }
    drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey || e.button === 1 || (tool === 'nav' && e.button === 2) };
  });
  cv.addEventListener('pointermove', (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag.x = e.clientX; drag.y = e.clientY;
      if (drag.pan) viewer.pan(dx, dy); else viewer.orbit(dx, dy);
    } else if ((S.tool === 'keep' || S.tool === 'remove') && S.labels && S.loaded.original && viewer.meshes.get('original').visible) brushAt(e.offsetX, e.offsetY);
  });
  const end = () => { drag = null; if (S.stroke) { commit(S.stroke); S.stroke = null; } };
  cv.addEventListener('pointerup', end); cv.addEventListener('pointercancel', end);
  cv.addEventListener('wheel', (e) => { e.preventDefault(); viewer.zoom(e.deltaY); }, { passive: false });
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); (e.shiftKey ? $('#redo') : $('#undo')).click(); }
  });

  const center = $('#center'), drop = $('#drop');
  center.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('on'); });
  center.addEventListener('dragleave', () => drop.classList.remove('on'));
  center.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('on'); const f = e.dataTransfer.files[0]; if (f) uploadFile(f); });
}

buildParams(); bindUI(); renderStages(); updateButtons();
