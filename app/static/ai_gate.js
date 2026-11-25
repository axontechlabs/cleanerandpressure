/* Conexión con Claude (IA) al abrir el programa.
 *
 * La ventana está escrita directamente en el HTML (visible desde el primer momento) y este script es
 * independiente del resto de la página: aunque falle cualquier otra parte (visor 3D, navegador
 * antiguo…), la ventana aparece y funciona. Se muestra SIEMPRE al abrir el programa:
 *  - sin clave o si la conexión falla: pide la clave (modelo por defecto Claude Sonnet 5.5);
 *  - con una clave guardada que funciona: lo indica y basta con pulsar «Conectar» (o cambiarla).
 * Al conectar se hace una llamada mínima a la API y la clave queda guardada en .env.
 * Escrito sin sintaxis moderna para que funcione en cualquier navegador.
 */
(function () {
  'use strict';
  var current = null;

  function el(id) { return document.getElementById(id); }

  function modelName(id) {
    var ms = (current && current.models) || [];
    for (var i = 0; i < ms.length; i++) if (ms[i].id === id) return ms[i].name.replace(/ \(.*\)$/, '');
    return id || 'Claude Sonnet 5.5';
  }

  function paint() {
    var b = el('ai-badge'), s = el('ai-state'), off = el('ai-off');
    var ok = !!(current && current.connected);
    if (b) {
      if (ok) { b.textContent = 'IA conectada · ' + modelName(current.model); b.className = 'ok'; }
      else if (off && off.checked) { b.textContent = 'IA desactivada (pruebas)'; b.className = 'warn'; }
      else { b.textContent = 'IA sin conectar · pulse aquí'; b.className = 'warn'; }
    }
    if (s) {
      s.textContent = ok
        ? 'Conectada con ' + modelName(current.model) + ' (clave ' + current.key_hint + (current.saved ? ', guardada en este ordenador' : '') + ')'
        : (current && current.error ? 'Sin conexión: ' + current.error : 'Sin conexión con la IA');
      s.className = 'hint ' + (ok ? 'ok' : 'warn');
    }
  }

  function fillForm(message) {
    var sel = el('ai-gate-model');
    var models = (current && current.models) || [{ id: 'claude-sonnet-5-5', name: 'Claude Sonnet 5.5 (recomendado)' }];
    var cur = (current && current.model) || 'claude-sonnet-5-5';
    sel.innerHTML = '';
    var found = false;
    for (var i = 0; i < models.length; i++) {
      var o = document.createElement('option');
      o.value = models[i].id; o.textContent = models[i].name;
      if (models[i].id === cur) found = true;
      sel.appendChild(o);
    }
    if (!found) { var o2 = document.createElement('option'); o2.value = cur; o2.textContent = cur; sel.appendChild(o2); }
    sel.value = cur;
    var key = el('ai-gate-key');
    key.value = '';
    var info = el('ai-gate-info');
    if (current && current.connected) {
      info.textContent = '✓ Conectada con la clave ' + current.key_hint + '. Pulse «Conectar» para seguir con ella, o pegue otra clave.';
      info.className = 'okmsg';
      key.placeholder = 'dejar vacío para usar ' + current.key_hint;
    } else if (current && current.configured) {
      info.textContent = 'Hay una clave guardada (' + current.key_hint + ') pero no conecta. Pegue una clave válida, o pulse «Conectar» para reintentar.';
      info.className = 'warnmsg';
      key.placeholder = 'sk-ant-…  (vacío = reintentar con ' + current.key_hint + ')';
    } else {
      info.textContent = 'No hay ninguna clave guardada. Pegue la clave de la API para continuar.';
      info.className = 'warnmsg';
      key.placeholder = 'sk-ant-…';
    }
    el('ai-gate-err').textContent = message || (current && !current.connected && current.error) || '';
  }

  function show(message) {
    fillForm(message);
    el('ai-gate').style.display = 'flex';
    setTimeout(function () { try { el('ai-gate-key').focus(); } catch (e) { /* nada */ } }, 60);
  }

  function hide() { el('ai-gate').style.display = 'none'; paint(); }

  function request(method, url, body, done) {
    var x = new XMLHttpRequest();
    x.open(method, url, true);
    x.setRequestHeader('Content-Type', 'application/json');
    x.timeout = 60000;
    x.onload = function () {
      var d = {};
      try { d = JSON.parse(x.responseText); } catch (e) { d = { error: 'Respuesta inesperada del programa (' + x.status + ').' }; }
      done(x.status >= 200 && x.status < 300, d);
    };
    x.onerror = function () { done(false, { error: 'No se puede hablar con el programa. ¿Sigue abierta la Terminal con ./run.sh?' }); };
    x.ontimeout = function () { done(false, { error: 'La comprobación ha tardado demasiado. Revise la conexión a Internet.' }); };
    x.send(body ? JSON.stringify(body) : null);
  }

  function connect() {
    var key = el('ai-gate-key').value.replace(/^\s+|\s+$/g, '');
    if (!key && !(current && current.configured)) { el('ai-gate-err').textContent = 'Pegue la clave de la API (empieza por sk-ant-).'; return; }
    var btn = el('ai-gate-ok');
    btn.disabled = true;
    el('ai-gate-err').textContent = 'Conectando con Claude…';
    request('POST', '/api/ai/key', { key: key, model: el('ai-gate-model').value, persist: el('ai-gate-persist').checked }, function (ok, d) {
      btn.disabled = false;
      if (d && d.models) current = d;
      if (ok && d.connected) {
        var off = el('ai-off'); if (off) off.checked = false;
        hide();
      } else {
        fillForm((d && d.error) || 'No se ha podido conectar.');
        if (key) el('ai-gate-key').value = key;
        paint();
      }
    });
  }

  function skip() {
    var off = el('ai-off'); if (off) off.checked = true;
    hide();
  }

  function init() {
    if (!el('ai-gate')) return;
    el('ai-gate-ok').onclick = connect;
    el('ai-gate-skip').onclick = skip;
    el('ai-gate-key').onkeydown = function (e) { if (e.key === 'Enter' || e.keyCode === 13) connect(); };
    var badge = el('ai-badge'); if (badge) badge.onclick = function () { show(); };
    var cfg = el('ai-config'); if (cfg) cfg.onclick = function () { show(); };
    var off = el('ai-off'); if (off) off.onchange = paint;
    show();                                 // siempre al abrir el programa
    el('ai-gate-info').textContent = 'Comprobando la conexión…';
    request('GET', '/api/ai/status', null, function (ok, d) {
      if (!ok) { fillForm(d.error); return; }
      current = d;
      if (!d.configured) { fillForm(); paint(); return; }
      el('ai-gate-info').textContent = 'Comprobando la clave guardada (' + d.key_hint + ')…';
      request('POST', '/api/ai/test', {}, function (ok2, d2) {
        if (d2 && d2.models) current = d2;
        fillForm();
        paint();
      });
    });
  }

  window.aiGate = { open: show, refresh: paint, status: function () { return current; } };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
