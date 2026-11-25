// Modo bilateral: dos escaneos, dos limpiezas independientes y una simulación conjunta.
import { Viewer } from './viewer.js';

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const SIDES = ['izquierdo', 'derecho'];
const REGIONS = [   // clave, nombre, espesor (mm), E0 (kPa), β
  ['talon_medial', 'Talón medial', 19, 35, 8], ['talon_lateral', 'Talón lateral', 19, 35, 8],
  ['mediopie', 'Mediopié', 12, 30, 6], ['met1', 'Antepié 1.er MTT', 10, 35, 6], ['met23', 'Antepié 2.º–3.er', 11, 35, 6],
  ['met45', 'Antepié 4.º–5.º', 9, 35, 6], ['hallux', 'Hallux', 8, 30, 6], ['dedos', 'Dedos 2.º–5.º', 7, 30, 4],
];
const THERMAL = [[0, 0, 255], [0, 255, 255], [0, 255, 0], [255, 255, 0], [255, 128, 0], [255, 0, 0]];

const S = { state: null, report: null, placement: null, mapView: 'bilateral', tab: 'map', viewer: null, meshes: {} };

// ------------------------------------------------------------------ utilidades
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? '—'
  : Number(v).toLocaleString('es-ES', { minimumFractionDigits: d, maximumFractionDigits: d });
// Dónde ha fallado (archivo:línea), para poder diagnosticar cualquier error que llegue al usuario.
function errWhere(e) {
  const m = String((e && e.stack) || '').match(/\/static\/([\w.-]+\.js)(?:\?[^:]*)?:(\d+)/);
  return m ? ` [${m[1]}:${m[2]}]` : '';
}
window.addEventListener('error', (ev) => { const s = document.getElementById('status'); if (s) { s.textContent = `Error: ${ev.message}${errWhere(ev.error)}`; s.className = 'err'; } });
window.addEventListener('unhandledrejection', (ev) => { const s = document.getElementById('status'); if (s) { s.textContent = `Error: ${(ev.reason && ev.reason.message) || ev.reason}${errWhere(ev.reason)}`; s.className = 'err'; } });

function status(text, cls = 'idle') { const s = $('#status'); s.textContent = text; s.className = cls; s.title = text; }

async function api(path, body, raw = false) {
  const opt = body === undefined ? {} : { method: 'POST', body: raw ? body : JSON.stringify(body),
    headers: raw ? { 'Content-Type': 'application/octet-stream' } : { 'Content-Type': 'application/json' } };
  if (body === null) opt.method = 'POST';
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function busy(label, fn) {
  status(label + '…', 'busy');
  $$('button').forEach((b) => { b.dataset.wasDisabled = b.disabled; b.disabled = true; });
  try { return await fn(); }
  catch (e) { status(e.message + errWhere(e), 'err'); console.error(e); return null; }
  finally { $$('button').forEach((b) => { b.disabled = b.dataset.wasDisabled === 'true'; }); refreshButtons(); }
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const e = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * e >= v) return m * e;
  return 10 * e;
}

function thermal(t) {
  t = Math.min(1, Math.max(0, t)) * (THERMAL.length - 1);
  const i = Math.min(THERMAL.length - 2, Math.floor(t)), f = t - i, a = THERMAL[i], b = THERMAL[i + 1];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

// ------------------------------------------------------------------ parámetros
function buildTissueTable() {
  let h = '';
  for (const s of SIDES) {
    h += `<tr><th>Pie ${s}</th><th>t</th><th>E₀</th><th>β</th></tr>`;
    for (const [k, name, t, e, b] of REGIONS)
      h += `<tr><td>${name}</td>`
        + `<td><input type="number" data-side="${s}" data-reg="${k}" data-q="thickness" value="${t}" step="0.5" min="1"></td>`
        + `<td><input type="number" data-side="${s}" data-reg="${k}" data-q="e0" value="${e}" step="5" min="1"></td>`
        + `<td><input type="number" data-side="${s}" data-reg="${k}" data-q="beta" value="${b}" step="0.5" min="0.1"></td></tr>`;
  }
  $('#tissue').innerHTML = h;
}

// tolerantes: si falta un campo (p. ej. una página antigua en caché) no se rompe la simulación
const num = (id) => { const e = $(id); const v = e ? parseFloat(e.value) : NaN; return Number.isFinite(v) ? v : undefined; };
const opt = (id) => { const e = $(id); const v = e ? parseFloat(e.value) : NaN; return Number.isFinite(v) ? v : null; };
const val = (id, d) => { const e = $(id); return e ? e.value : d; };
const chk = (id) => { const e = $(id); return !!(e && e.checked); };
const aiBody = () => ({ sin_ia: chk('#ai-off'), rounds: num('#ai-rounds') || 6 });

function stanceParams() {
  return {
    heel_separation: opt('#heel_separation'), height_cm: opt('#height_cm'), toe_out_left: num('#toe_out_left'), toe_out_right: num('#toe_out_right'),
    advance_right: num('#advance_right'), load_point: num('#load_point') / 100,
  };
}

function simParams() {
  const tissue = { izquierdo: {}, derecho: {} };
  $$('#tissue input').forEach((i) => {
    const t = tissue[i.dataset.side], v = parseFloat(i.value);
    if (t && Number.isFinite(v)) (t[i.dataset.reg] ||= {})[i.dataset.q] = v;
  });
  return {
    mass: num('#mass'), mode: val('#mode', 'reparto'), split_left: num('#split'),
    com_x: num('#com_x'), com_y_frac: num('#com_y') / 100, load_point: num('#load_point') / 100,
    nonlinearity_scale: num('#nonlinearity_scale'), spread: num('#spread'), contact_threshold: num('#contact_threshold'),
    arch_drop: opt('#arch_drop'), height_cm: opt('#height_cm'), loaded_scan: chk('#loaded_scan'),
    toe_flexor: num('#toe_flexor'), toe_max_flexion: num('#toe_max_flexion'),
    toe_definition: num('#toe_definition'), toe_pulp_lift: num('#toe_pulp_lift'), toe_spread: num('#toe_spread'),
    stiffness_scale: { izquierdo: num('#stiff-izquierdo'), derecho: num('#stiff-derecho') }, tissue,
  };
}

function aiLog(groups) {
  let h = '';
  for (const [title, hist] of groups) {
    h += `<h3>${title}</h3>` + (hist || []).map((e) => `<div class="hint"><b>Ronda ${e.round}: ${e.verdict || ''}</b> — ${e.diagnosis || ''}`
      + `${JSON.stringify(e.changes || {}).length > 30 ? '<br>Cambios: ' + JSON.stringify(e.changes) : ''}${e.reverted ? '<br>(deshecho)' : ''}</div>`).join('');
  }
  $('#ai-log').innerHTML = h;
}
function applySim(p) {
  for (const k of ['toe_flexor', 'toe_definition', 'toe_pulp_lift', 'toe_spread', 'spread', 'nonlinearity_scale', 'toe_max_flexion', 'arch_drop']) {
    if (p && p[k] !== undefined && p[k] !== null && $('#' + k)) $('#' + k).value = p[k];
  }
  if (p && p.load_point && $('#load_point')) $('#load_point').value = Math.round(p.load_point * 100);
  for (const s of SIDES) for (const [reg, vals] of Object.entries(((p || {}).tissue || {})[s] || {}))
    for (const [q, v] of Object.entries(vals)) { const el = $(`#tissue input[data-side="${s}"][data-reg="${reg}"][data-q="${q}"]`); if (el) el.value = v; }
}

function updateSplit() {
  const l = num('#split');
  $('#split-l').textContent = fmt(l) + ' %';
  $('#split-r').textContent = fmt(100 - l) + ' %';
}

// ------------------------------------------------------------------ estado e interfaz
function refreshButtons() {
  const st = S.state || {};
  const files = st.files || {};
  const both = files.izquierdo && files.derecho;
  const processed = st.processed || {};
  const valid = SIDES.every((s) => processed[s] && processed[s].valid);
  $('#process').disabled = !both;
  $('#place').disabled = !valid;
  $('#simulate').disabled = !valid;
  $('#ai-cut').disabled = !both;
  $('#ai-pressure').disabled = !valid;
  for (const s of SIDES) {
    $(`#name-${s}`).textContent = files[s] || '—';
    const a = $(`#ex-${s}`);
    const ok = processed[s] && processed[s].valid;
    a.classList.toggle('off', !ok);
    a.href = ok ? `/api/bi/export/${s}?fmt=stl` : '#';
  }
  const sim = !!st.simulated;
  for (const w of ['bilateral', ...SIDES]) {
    const a = $(`#ex-png-${w}`); a.classList.toggle('off', !sim); a.href = sim ? `/api/bi/map/${w}.png?download=1` : '#';
  }
  for (const [id, path] of [['#ex-csv', '/api/bi/export/csv'], ['#ex-json', '/api/bi/export/json'],
    ['#ex-loaded-izquierdo', '/api/bi/export/loaded_izquierdo?fmt=stl'], ['#ex-loaded-derecho', '/api/bi/export/loaded_derecho?fmt=stl'],
    ['#ex-loaded-ambos', '/api/bi/export/loaded_ambos?fmt=stl']]) {
    if (!$(id)) continue;
    $(id).classList.toggle('off', !sim); $(id).href = sim ? path : '#';
  }
}

function renderProcessing() {
  const p = (S.state && S.state.processed) || {};
  let h = '';
  for (const s of SIDES) {
    const r = p[s];
    if (!r) continue;
    h += `<div class="foot-proc ${r.valid ? 'ok' : 'bad'}"><b>Pie ${s}</b>: ${r.valid ? 'sólido válido' : 'NO válido'}`
      + ` · ${fmt(r.volume_cm3, 0)} cm³ · ${r.seconds} s`;
    if (r.ia && r.ia.history && r.ia.history.length) {
      const last = r.ia.history[r.ia.history.length - 1];
      h += `<div class="hint">IA: ${r.ia.history.length} ronda(s) · ${last.verdict === 'correcto' ? 'corte dado por bueno' : 'última ronda: ' + (last.verdict || '')} — ${last.diagnosis || ''}</div>`;
    }
    if (r.failed_checks && r.failed_checks.length) h += `<div class="hint">Falla: ${r.failed_checks.join(', ')}</div>`;
    for (const w of (r.warnings || []).slice(0, 3)) h += `<div class="msg warn">${w}</div>`;
    h += `<div class="row"><a class="btn small" href="/api/bi/export/${s}?fmt=stl">STL</a>`
      + `<a class="btn small" href="/api/bi/export/${s}?fmt=obj">OBJ</a><a class="btn small" href="/api/bi/export/${s}?fmt=ply">PLY</a></div></div>`;
  }
  $('#proc').innerHTML = h;
}

function rows(list) {
  return '<table>' + list.filter(Boolean).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('') + '</table>';
}

function renderPlacement(pl) {
  if (!pl || !pl.feet) return '';
  let h = '<h3>Disposición bilateral</h3>';
  const st = pl.stance;
  h += rows([['Separación entre talones', fmt(st.heel_separation_mm, 0) + ' mm'
      + (st.heel_separation_source ? ` <small>(${st.heel_separation_source})</small>` : '')],
    ['Ángulo entre los ejes de los pies', fmt(st.angle_between_feet_deg, 1) + '°'],
    ...SIDES.map((s) => [`Pie ${s}: lado detectado`, `${pl.feet[s].detected_side} (confianza ${pl.feet[s].side_confidence})`]),
    ...SIDES.map((s) => [`Pie ${s}: longitud / anchura`, `${fmt(pl.feet[s].length_mm, 0)} / ${fmt(pl.feet[s].width_mm, 0)} mm`])]);
  const warns = [...(st.warnings || []), ...SIDES.flatMap((s) => pl.feet[s].warnings || [])];
  h += warns.map((w) => `<div class="msg warn">${w}</div>`).join('');
  return h;
}

function renderReport() {
  const r = S.report;
  let h = renderPlacement(S.placement || (r && r.placement));
  if (!r || !r.global) { $('#report').innerHTML = h || '<div class="hint">Sin resultados todavía.</div>'; return; }
  const g = r.global, F = r.feet;
  h = '<h3>A · Conjunto bilateral</h3>' + rows([
    ['Peso corporal', fmt(g.mass_kg, 1) + ' kg'],
    ['Fuerza gravitatoria (m·g)', fmt(g.weight_N, 1) + ' N'],
    ['Fuerza soportada por los pies', fmt(g.feet_force_N, 1) + ' N'],
    ['Reparto izquierdo / derecho', `${fmt(g.split_left_pct)} / ${fmt(g.split_right_pct)} %`],
    ['Origen del reparto', g.split_source],
    ['Superficie de contacto total', fmt(g.contact_area_cm2, 1) + ' cm²'],
    ['Presión media global', fmt(g.mean_kPa, 1) + ' kPa'],
    ['Presión máxima global', fmt(g.peak_kPa, 1) + ' kPa'],
    ['Centro de presiones global (x, y)', `${fmt(g.cop_world_mm[0], 1)}, ${fmt(g.cop_world_mm[1], 1)} mm`],
    g.com_projection_mm && ['Centro de masas indicado (x, y)', `${fmt(g.com_projection_mm[0], 1)}, ${fmt(g.com_projection_mm[1], 1)} mm`],
    ['Error de equilibrio de fuerzas', `${fmt(g.force_error_N, 4)} N (${fmt(g.force_error_pct, 4)} %)`],
    g.moment_error_Nmm !== null && ['Error de equilibrio de momentos', fmt(g.moment_error_Nmm / 1000, 4) + ' N·m'],
    ['Línea de carga de cada pie', fmt(g.load_line_pct, 1) + ' % desde el talón'],
  ]) + '<div class="hint">Coordenadas: origen en el punto medio entre los centros de los talones, X hacia la derecha, Y hacia delante.</div>';
  for (const s of SIDES) {
    const f = F[s];
    h += `<h3><span class="tag ${s === 'izquierdo' ? 'left' : 'right'}">${s[0].toUpperCase()}</span>${s === 'izquierdo' ? 'B' : 'C'} · Pie ${s}</h3>` + rows([
      ['Fuerza soportada', fmt(f.force_N, 1) + ' N'],
      ['Porcentaje del peso', fmt(f.pct_body, 1) + ' %'],
      ['Superficie de contacto', fmt(f.contact_area_cm2, 1) + ' cm²'],
      ['Presión media', fmt(f.mean_kPa, 1) + ' kPa'],
      ['Presión máxima', fmt(f.peak_kPa, 1) + ' kPa'],
      ['Centro de presiones (x, y)', `${fmt(f.cop_world_mm[0], 1)}, ${fmt(f.cop_world_mm[1], 1)} mm`],
      ['CoP desde el talón', `${fmt(f.cop_from_heel_edge_pct, 1)} % de la longitud desde el borde posterior (${fmt(f.cop_from_heel_pct, 1)} % desde el centro del talón)`],
      ['Talón / mediopié / antepié / dedos', `${fmt(f.rearfoot_pct, 1)} / ${fmt(f.midfoot_pct, 1)} / ${fmt(f.forefoot_pct, 1)} / ${fmt(f.toes_pct, 1)} %`],
      ['Apoyo inicial (asentamiento por gravedad)', (f.initial_support_regions || []).join(', ') || '—'],
      ['Dedos que apoyan', `${f.toes_in_contact ?? '—'} de ${(f.toes || []).length || 5} · ${fmt(f.toe_contact_area_cm2, 1)} cm²`],
      ['Dedos: giro medio de asentamiento', fmt(f.toe_rest_deg, 1) + '°'],
      f.arch && f.arch.measured && ['Arco interno sin carga (escaneo)', `${fmt(f.arch.height_mm, 1)} mm (${fmt(f.arch.height_pct, 1)} % de la longitud) · ápice al ${fmt(100 * f.arch.apex_frac, 0)} %`],
      f.arch && f.arch.measured && ['Holgura del borde lateral del mediopié', fmt(f.arch.lateral_clearance_mm, 1) + ' mm'],
      ['Descenso del arco interno aplicado', fmt(f.arch_drop_mm, 1) + ' mm' + (f.arch_drop_source ? ` <small>(${f.arch_drop_source})</small>` : '')],
      ['Descenso de la columna lateral', fmt(f.lateral_column_drop_mm, 1) + ' mm'],
      ['Tipo de huella simulada', f.footprint_type || '—'],
      ['Talón unido al antepié (banda lateral)', f.footprint_continuous ? `sí · banda de ${fmt(f.midfoot_band_min_width_mm, 0)} mm en su punto más estrecho` : 'no (huella cortada en el mediopié)'],
      ['Índice del arco (Cavanagh)', fmt(f.arch_index, 2)],
      ['Compresión máx. del tejido', `${fmt(f.max_compression_mm, 1)} mm (${fmt(f.max_tissue_strain_pct, 0)} %)`],
      ['Giro del pie para el equilibrio', `cabeceo ${fmt(f.pitch_deg, 1)}°, alabeo ${fmt(f.roll_deg, 1)}°`],
    ]);
    h += '<table class="cmp"><tr><th>Región</th><th>% pie</th><th>% peso</th><th>cm²</th><th>máx. kPa</th></tr>'
      + f.regions.map((q) => `<tr><td>${q.name}</td><td>${fmt(q.pct_foot, 1)}</td><td>${fmt(q.pct_body, 1)}</td>`
        + `<td>${fmt(q.contact_area_cm2, 1)}</td><td>${fmt(q.peak_kPa, 0)}</td></tr>`).join('') + '</table>';
    if ((f.toes || []).length)
      h += '<table class="cmp"><tr><th>Dedo</th><th>N</th><th>% pie</th><th>cm²</th><th>máx. kPa</th><th>giro</th></tr>'
        + f.toes.map((t) => `<tr><td>${t.name}${t.touches ? '' : ' <small>(sin apoyo)</small>'}</td><td>${fmt(t.force_N, 1)}</td>`
          + `<td>${fmt(t.pct_foot, 1)}</td><td>${fmt(t.contact_area_cm2, 1)}</td><td>${fmt(t.peak_kPa, 0)}</td>`
          + `<td>${fmt(t.rest_deg, 0)}°</td></tr>`).join('') + '</table>'
        + '<div class="hint">Giro: el que necesita cada dedo en su articulación MTF, desde la postura del escaneo, para apoyar en el suelo.</div>';
    const lf = f.loaded_foot;
    if (lf) {
      const c = lf.change_mm, pc = lf.change_pct, u = lf.unloaded, l = lf.loaded;
      h += '<table class="cmp"><tr><th>Pie en carga</th><th>sin carga</th><th>en carga</th><th>cambio</th></tr>'
        + [['Longitud', u.length_mm, l.length_mm, c.length, pc.length],
           ['Anchura del antepié', u.ball_width_mm, l.ball_width_mm, c.ball_width, pc.ball_width],
           ['Anchura del talón', u.heel_width_mm, l.heel_width_mm, c.heel_width, pc.heel_width]]
          .map(([n, a, b, d, p]) => `<tr><td>${n}</td><td>${fmt(a, 1)} mm</td><td>${fmt(b, 1)} mm</td><td>+${fmt(d, 1)} mm (${fmt(p, 1)} %)</td></tr>`).join('')
        + `<tr><td>Descenso del arco</td><td></td><td></td><td>${fmt(c.arch_drop, 1)} mm</td></tr></table>`
        + `<div class="hint">${lf.loaded_scan ? 'El escaneo ya estaba en carga: no se añade expansión.' : `Estimación adaptada a este pie y a su carga (${fmt(lf.drivers.foot_load_N, 0)} N): medias de la literatura (longitud +${fmt(lf.literature_mean_pct.length, 2)} %, antepié +${fmt(lf.literature_mean_pct.ball_width, 2)} %, talón +${fmt(lf.literature_mean_pct.heel_width, 2)} %) escaladas con el descenso del arco y la compresión del tejido simulados.`} STL del pie en carga en «Exportación».</div>`;
    }
  }
  h += '<h3>Comparación izquierdo / derecho</h3><table class="cmp"><tr><th>Indicador</th><th>Izq.</th><th>Der.</th><th>Dif.</th><th>Asim. %</th></tr>'
    + r.comparison.map((c) => `<tr><td>${c.name}${c.unit ? ` <small>(${c.unit})</small>` : ''}</td><td>${fmt(c.left, c.unit ? 1 : 2)}</td>`
      + `<td>${fmt(c.right, c.unit ? 1 : 2)}</td><td>${fmt(c.difference, c.unit ? 1 : 2)}</td>`
      + `<td class="${c.asymmetry_pct > 10 ? 'hi' : ''}">${fmt(c.asymmetry_pct, 1)}</td></tr>`).join('') + '</table>'
    + '<div class="hint">Asimetría = 2·|I − D| / (I + D) · 100. Se resaltan las superiores al 10 %.</div>';
  const warns = r.warnings || [];
  h += warns.map((w) => `<div class="msg warn">${w}</div>`).join('');
  h += renderPlacement(r.placement);
  $('#report').innerHTML = h;
}

function showMap() {
  const img = $('#map');
  if (!(S.state && S.state.simulated)) { img.classList.remove('on'); $('#map-empty').hidden = false; return; }
  img.src = `/api/bi/map/${S.mapView}.png?t=${Date.now()}`;
  img.classList.add('on'); $('#map-empty').hidden = true;
}

function setTab(tab) {
  S.tab = tab;
  $$('[data-tab]').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  $('#map-wrap').hidden = tab !== 'map';
  $('#gl').hidden = tab !== '3d';
  $('#map-tools').hidden = tab !== 'map';
  $('#view-tools').hidden = tab !== '3d';
  if (tab === '3d') { ensureViewer(); S.viewer.requestRender(); }
}

// ------------------------------------------------------------------ 3D
function ensureViewer() {
  if (S.viewer) return;
  const cv = $('#gl');
  S.viewer = new Viewer(cv);
  let drag = null;
  cv.addEventListener('contextmenu', (e) => e.preventDefault());
  cv.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey }; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag.x = e.clientX; drag.y = e.clientY;
    if (drag.pan) S.viewer.pan(dx, dy); else S.viewer.orbit(dx, dy);
  });
  cv.addEventListener('pointerup', () => { drag = null; });
  cv.addEventListener('wheel', (e) => { e.preventDefault(); S.viewer.zoom(e.deltaY); }, { passive: false });
}

function setView(kind) {
  const v = S.viewer; if (!v) return;
  const t = { top: [[0, 0, 1], [0, 1, 0]], bottom: [[0, 0, -1], [0, 1, 0]], front: [[0, 1, 0.15], [0, 0, 1]],
    side: [[1, 0, 0.1], [0, 0, 1]], persp: [[0.9, -1.4, 1.1], [0, 0, 1]] }[kind];
  v.setView(...t);
  const ground = v.meshes.get('suelo');
  if (ground) ground.visible = kind !== 'bottom';      // desde abajo el suelo taparía las plantas
  fitAll();
}

function fitAll() {
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (const s of SIDES) {
    const m = S.viewer.meshes.get(s); if (!m) continue;
    for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], m.bounds[0][k]); hi[k] = Math.max(hi[k], m.bounds[1][k]); }
  }
  if (lo[0] < Infinity) S.viewer.fit([lo, hi]);
}

async function loadMeshes() {
  ensureViewer();
  for (const s of SIDES) {
    const r = await fetch(`/api/bi/mesh/${s}`);
    if (!r.ok) return;
    const buf = await r.arrayBuffer();
    const head = new Uint32Array(buf, 0, 3);
    const [nv, nf, nsole] = head;
    const v = new Float32Array(buf, 12, nv * 3);
    const f = new Uint32Array(buf, 12 + nv * 12, nf * 3);
    S.meshes[s] = { nsole };
    S.viewer.setMesh(s, v, f, { color: s === 'izquierdo' ? [0.55, 0.62, 0.82] : [0.82, 0.6, 0.55] });
  }
  // suelo común
  const g = 400;
  const gv = new Float32Array([-g, -g / 2, -0.3, g, -g / 2, -0.3, g, g * 1.5, -0.3, -g, g * 1.5, -0.3]);
  S.viewer.setMesh('suelo', gv, new Uint32Array([0, 1, 2, 0, 2, 3]), { color: [0.18, 0.2, 0.24], alpha: 0.85 });
  setView('persp');
}

async function paintPressure() {
  if (!S.report || !S.report.global || !S.viewer) return;
  const vmax = niceMax(S.report.global.peak_kPa), thr = S.report.params.contact_threshold;
  for (const s of SIDES) {
    const m = S.viewer.meshes.get(s); if (!m) continue;
    const r = await fetch(`/api/bi/facepressure/${s}`);
    if (!r.ok) continue;
    const p = new Float32Array(await r.arrayBuffer());
    const n = S.meshes[s].nsole;
    S.viewer.paintFaces(s, (f) => (f < n ? (p[f] > thr ? thermal(p[f] / vmax) : [20, 20, 24]) : [150, 155, 165]));
    m.useVC = true;
  }
  S.viewer.requestRender();
}

// ------------------------------------------------------------------ acciones
async function refreshState() {
  const d = await api('/api/bi/state');
  S.state = d.state; S.report = d.report && d.report.global ? d.report : null;
  S.placement = d.placement && d.placement.feet ? d.placement : null;
  renderProcessing(); renderReport(); refreshButtons(); showMap();
}

async function upload(side, file) {
  await busy(`Cargando pie ${side}`, async () => {
    const buf = await file.arrayBuffer();
    const d = await api(`/api/bi/upload/${side}?name=${encodeURIComponent(file.name)}`, buf, true);
    S.state = d.state; S.report = null; S.placement = null;
    renderProcessing(); renderReport(); showMap();
    status(`Pie ${side} cargado: ${file.name}`, 'ok');
  });
}

function wire() {
  for (const s of SIDES) $(`#file-${s}`).addEventListener('change', (e) => { if (e.target.files[0]) upload(s, e.target.files[0]); e.target.value = ''; });
  $('#sample').onclick = () => busy('Generando pareja sintética', async () => {
    const d = await api('/api/bi/sample', null); S.state = d.state; S.report = null; S.placement = null;
    renderProcessing(); renderReport(); showMap(); status('Pareja sintética cargada (el derecho tiene el arco más alto)', 'ok');
  });
  $('#swap').onclick = () => busy('Intercambiando', async () => {
    const d = await api('/api/bi/swap', null); S.state = d.state; S.report = null; S.placement = null;
    renderProcessing(); renderReport(); showMap(); status('Pies intercambiados', 'ok');
  });
  $('#process').onclick = () => busy(chk('#ai-off') ? 'Procesando los dos escaneos' : 'Procesando los dos escaneos; la IA revisa y reajusta el corte de cada pie (varios minutos)', async () => {
    const d = await api('/api/bi/process', aiBody()); S.state = d.state; S.report = null; S.placement = null;
    renderProcessing(); renderReport(); showMap();
    if (!chk('#ai-off')) aiLog(SIDES.map((s) => [`Corte pie ${s}`, (d.report[s].ia || {}).history]));
    const ok = SIDES.every((s) => d.report[s].valid);
    status(ok ? 'Dos sólidos válidos e independientes' : 'Algún sólido no es válido: revíselo antes de simular', ok ? 'ok' : 'err');
  });
  $('#place').onclick = () => busy('Colocando los pies', async () => {
    const d = await api('/api/bi/place', { stance: stanceParams() }); S.state = d.state; S.placement = d.report; S.report = null;
    renderReport(); await loadMeshes(); setTab('3d');
    const w = [...(d.report.stance.warnings || []), ...SIDES.flatMap((s) => d.report.feet[s].warnings || [])];
    status(w.length ? 'Pies colocados, con avisos (ver informe)' : 'Pies colocados en el suelo común', w.length ? 'warn' : 'ok');
  });
  $('#simulate').onclick = () => busy(chk('#ai-off') ? 'Simulando la bipedestación bilateral' : 'Simulando; la IA revisa y reajusta el mapa de presión (varios minutos)', async () => {
    const d = await api('/api/bi/simulate', { sim: simParams(), stance: stanceParams(), ...aiBody() });
    S.state = d.state; S.report = d.report; S.placement = d.report.placement;
    if (d.report.ia) { applySim(d.report.ia.final_params); aiLog([['Mapa de presión', d.report.ia.history]]); }
    renderReport(); refreshButtons(); setTab('map'); showMap();
    await loadMeshes(); await paintPressure(); setTab('map');
    const g = d.report.global;
    status(`Equilibrio: ${fmt(g.feet_force_N, 1)} N = m·g · reparto ${fmt(g.split_left_pct)} / ${fmt(g.split_right_pct)} % · máx. ${fmt(g.peak_kPa, 0)} kPa`,
      (d.report.warnings || []).length ? 'warn' : 'ok');
  });
  $('#ai-cut').onclick = () => busy('La IA está revisando el corte de los dos pies (unos minutos)', async () => {
    const d = await api('/api/bi/ai/cut', { rounds: num('#ai-rounds') || 6 }); S.state = d.state; S.report = null; S.placement = null;
    renderProcessing(); renderReport(); showMap();
    aiLog(SIDES.map((s) => [`Corte pie ${s}`, d.report[s].history]));
    const ok = SIDES.every((s) => d.report[s].validacion.valid);
    status(ok ? 'Corte revisado por IA: dos sólidos válidos' : 'Corte revisado, pero algún sólido no es válido', ok ? 'ok' : 'err');
  });
  $('#ai-pressure').onclick = () => busy('La IA está revisando el mapa de presión (unos minutos)', async () => {
    const d = await api('/api/bi/ai/pressure', { rounds: num('#ai-rounds') || 6, sim: simParams(), stance: stanceParams() });
    S.state = d.state; S.report = d.report; S.placement = d.report.placement;
    applySim(d.report.ia.final_params);
    renderReport(); refreshButtons(); setTab('map'); showMap();
    await loadMeshes(); await paintPressure(); setTab('map');
    aiLog([['Mapa de presión', d.report.ia.history]]);
    const h = d.report.ia.history, last = h[h.length - 1] || {};
    status(`IA (${h.length} ronda/s): ${last.diagnosis || ''}`, last.verdict === 'correcto' ? 'ok' : 'warn');
  });
  // la conexión con la IA la gestiona /static/ai_gate.js (independiente de este archivo)
  $('#split').addEventListener('input', updateSplit);
  $('#mode').addEventListener('change', () => {
    const com = val('#mode', '') === 'centro_masas';
    $('#com-box').hidden = !com; $('#split-box').hidden = com;
  });
  $$('[data-tab]').forEach((b) => { b.onclick = () => setTab(b.dataset.tab); });
  $$('[data-map]').forEach((b) => {
    b.onclick = () => { S.mapView = b.dataset.map; $$('[data-map]').forEach((x) => x.classList.toggle('active', x === b)); showMap(); };
  });
  $$('[data-view]').forEach((b) => { b.onclick = () => setView(b.dataset.view); });
}

buildTissueTable();
updateSplit();
wire();
refreshState().catch((e) => status(e.message, 'err'));
