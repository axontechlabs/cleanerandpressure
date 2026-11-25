// Conexión con Claude (IA) al abrir el programa: pide la clave si no hay, comprueba la conexión con una
// llamada mínima y la deja conectada (guardada en .env del ordenador). La usan las dos páginas.
const $ = (s) => document.querySelector(s);
let current = null;
const listeners = [];

function css() {
  if ($('#ai-gate-css')) return;
  const st = document.createElement('style');
  st.id = 'ai-gate-css';
  st.textContent = `
  #ai-gate { position: fixed; inset: 0; background: rgba(5,8,12,.72); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  #ai-gate[hidden] { display: none; }
  #ai-gate .card { width: min(460px, 92vw); background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px; box-shadow: 0 10px 40px rgba(0,0,0,.5); }
  #ai-gate h3 { margin: 0 0 6px; font-size: 16px; }
  #ai-gate p { color: var(--muted); margin: 6px 0 12px; font-size: 12.5px; }
  #ai-gate label { display: block; margin: 10px 0 4px; font-size: 12px; color: var(--muted); }
  #ai-gate input[type=password], #ai-gate select { width: 100%; padding: 7px 8px; background: var(--panel2); color: var(--text); border: 1px solid var(--line); border-radius: 6px; font: inherit; }
  #ai-gate .chk { display: flex; gap: 6px; align-items: center; color: var(--text); }
  #ai-gate .err { color: var(--err); font-size: 12px; min-height: 1.2em; margin-top: 10px; white-space: pre-wrap; }
  #ai-gate .row2 { display: flex; gap: 8px; justify-content: space-between; margin-top: 14px; }
  #ai-badge { padding: 3px 10px; border-radius: 10px; background: var(--panel2); color: var(--muted); cursor: pointer; border: 1px solid var(--line); font: inherit; font-size: 12px; white-space: nowrap; }
  #ai-badge.ok { color: #08130c; background: var(--ok); border-color: var(--ok); }
  #ai-badge.warn { color: #1a1403; background: var(--warn); border-color: var(--warn); }
  #ai-badge.busy { color: #fff; background: var(--accent); border-color: var(--accent); }`;
  document.head.appendChild(st);
}

function build() {
  if ($('#ai-gate')) return;
  const d = document.createElement('div');
  d.id = 'ai-gate';
  d.hidden = true;
  d.innerHTML = `<div class="card" role="dialog" aria-modal="true" aria-labelledby="ai-gate-title">
    <h3 id="ai-gate-title">Conectar con Claude (IA)</h3>
    <p>La IA revisa y reajusta el corte de cada pie y el mapa de presión dentro del proceso. Pegue la clave de la API
      (Claude Console → API keys; empieza por <code>sk-ant-</code>). Se comprueba la conexión y queda guardada en este ordenador.</p>
    <label for="ai-gate-key">Clave de la API</label>
    <input type="password" id="ai-gate-key" placeholder="sk-ant-…" autocomplete="off">
    <label for="ai-gate-model">Modelo</label>
    <select id="ai-gate-model"></select>
    <label class="chk"><input type="checkbox" id="ai-gate-persist" checked> Guardar en este ordenador (archivo .env, no se sube a GitHub)</label>
    <div class="err" id="ai-gate-err"></div>
    <div class="row2">
      <button class="btn small" id="ai-gate-skip">Continuar sin IA (solo pruebas)</button>
      <button class="btn primary" id="ai-gate-ok">Conectar</button>
    </div></div>`;
  document.body.appendChild(d);
  $('#ai-gate-ok').onclick = connect;
  $('#ai-gate-key').addEventListener('keydown', (e) => { if (e.key === 'Enter') connect(); });
  $('#ai-gate-skip').onclick = () => {
    const off = $('#ai-off'); if (off) off.checked = true;
    d.hidden = true; paint();
  };
}

function badge() {
  let b = $('#ai-badge');
  if (!b) {
    b = document.createElement('button');
    b.id = 'ai-badge';
    b.title = 'Conexión con Claude: pulse para cambiar la clave o el modelo';
    b.onclick = () => open();
    const st = $('#status');
    if (st && st.parentNode) st.parentNode.insertBefore(b, st); else document.body.prepend(b);
  }
  return b;
}

function modelName(id) {
  const m = ((current && current.models) || []).find((x) => x.id === id);
  return m ? m.name.replace(/ \(.*\)$/, '') : id;
}

function paint(text, cls) {
  const b = badge();
  const off = $('#ai-off') && $('#ai-off').checked;
  if (text) { b.textContent = text; b.className = cls || ''; }
  else if (current && current.connected) { b.textContent = `IA conectada · ${modelName(current.model)}`; b.className = 'ok'; }
  else if (off) { b.textContent = 'IA desactivada (pruebas)'; b.className = 'warn'; }
  else { b.textContent = 'IA sin conectar'; b.className = 'warn'; }
  const s = $('#ai-state');
  if (s) {
    s.textContent = current && current.connected
      ? `Conectada con ${modelName(current.model)} (clave ${current.key_hint}${current.saved ? ', guardada en este ordenador' : ''})`
      : (current && current.error ? `Sin conexión: ${current.error}` : 'Sin conexión con la IA');
    s.className = 'hint ' + (current && current.connected ? 'ok' : 'warn');
  }
  listeners.forEach((f) => f(current));
}

function fillModels() {
  const sel = $('#ai-gate-model');
  const models = (current && current.models) || [{ id: 'claude-sonnet-5-5', name: 'Claude Sonnet 5.5 (recomendado)' }];
  sel.innerHTML = models.map((m) => `<option value="${m.id}">${m.name}</option>`).join('');
  const cur = (current && current.model) || 'claude-sonnet-5-5';
  if (!models.some((m) => m.id === cur)) sel.insertAdjacentHTML('beforeend', `<option value="${cur}">${cur}</option>`);
  sel.value = cur;
}

export function open(error) {
  build();
  fillModels();
  $('#ai-gate-key').value = '';
  $('#ai-gate-key').placeholder = current && current.configured ? `${current.key_hint} (dejar vacío para usar la actual)` : 'sk-ant-…';
  $('#ai-gate-err').textContent = error || (current && current.error) || '';
  $('#ai-gate').hidden = false;
  setTimeout(() => $('#ai-gate-key').focus(), 50);
}

async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const d = await r.json().catch(() => ({}));
  return { ok: r.ok, d };
}

async function connect() {
  const key = $('#ai-gate-key').value.trim();
  if (!key && !(current && current.configured)) { $('#ai-gate-err').textContent = 'Pegue la clave de la API.'; return; }
  $('#ai-gate-ok').disabled = true;
  $('#ai-gate-err').textContent = 'Conectando…';
  try {
    const { ok, d } = await post('/api/ai/key', { key, model: $('#ai-gate-model').value, persist: $('#ai-gate-persist').checked });
    if (d && d.models) current = d;
    if (ok && d.connected) {
      const off = $('#ai-off'); if (off) off.checked = false;
      $('#ai-gate').hidden = true; paint();
    } else {
      $('#ai-gate-err').textContent = (d && d.error) || 'No se ha podido conectar.';
      paint();
    }
  } catch (e) {
    $('#ai-gate-err').textContent = 'No se puede hablar con el programa: ' + e.message;
  } finally {
    $('#ai-gate-ok').disabled = false;
  }
}

export function status() { return current; }
export function refresh() { paint(); }
export function onChange(f) { listeners.push(f); }

// Al abrir el programa: si hay clave, se comprueba la conexión; si no la hay o falla, se pide.
export async function initAiGate() {
  css(); build();
  try {
    current = await (await fetch('/api/ai/status')).json();
  } catch (e) { return; }
  if (current.connected) { paint(); return; }
  if (!current.configured) { paint(); open(); return; }
  paint('Conectando con la IA…', 'busy');
  try {
    const { d } = await post('/api/ai/test');
    current = d;
  } catch (e) { /* se muestra abajo */ }
  paint();
  if (!current || !current.connected) open((current && current.error) || 'No se ha podido conectar con la API.');
}
