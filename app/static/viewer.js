// Visor 3D WebGL2 sin dependencias externas: órbita, vistas, capas con opacidad,
// colores por cara, líneas (bordes, normales), plano de corte y selección por rayo.

const VS = `#version 300 es
precision highp float;
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aColor;
uniform mat4 uProj, uView;
out vec3 vView; out vec3 vWorld; out vec3 vColor;
void main(){
  vec4 v = uView * vec4(aPos, 1.0);
  vView = v.xyz; vWorld = aPos; vColor = aColor;
  gl_Position = uProj * v;
}`;

const FS = `#version 300 es
precision highp float;
in vec3 vView; in vec3 vWorld; in vec3 vColor;
uniform vec3 uColor; uniform float uUseVC, uAlpha, uLit, uClipOn;
uniform vec4 uClip;
out vec4 frag;
void main(){
  if (uClipOn > 0.5 && dot(uClip.xyz, vWorld) - uClip.w < 0.0) discard;
  vec3 base = mix(uColor, vColor, uUseVC);
  if (uLit > 0.5) {
    vec3 n = normalize(cross(dFdx(vView), dFdy(vView)));
    float d = 0.28 + 0.72 * max(dot(n, normalize(vec3(0.25, 0.45, 1.0))), 0.0);
    vec3 c = base * d;
    if (!gl_FrontFacing) c = mix(c, vec3(0.62, 0.36, 0.66) * d, 0.5);   // cara interior: tono violeta
    frag = vec4(c, uAlpha);
  } else {
    frag = vec4(base, uAlpha);
  }
}`;

// ---------------------------------------------------------------- álgebra mínima
export const V3 = {
  sub: (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]],
  add: (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]],
  scale: (a, s) => [a[0] * s, a[1] * s, a[2] * s],
  dot: (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2],
  cross: (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
  len: (a) => Math.hypot(a[0], a[1], a[2]),
  norm: (a) => { const l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l]; },
  // rotación de v alrededor del eje unitario k (Rodrigues)
  rot: (v, k, ang) => {
    const c = Math.cos(ang), s = Math.sin(ang), d = V3.dot(k, v), x = V3.cross(k, v);
    return [v[0] * c + x[0] * s + k[0] * d * (1 - c), v[1] * c + x[1] * s + k[1] * d * (1 - c),
            v[2] * c + x[2] * s + k[2] * d * (1 - c)];
  },
};

function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
}

export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl2', { antialias: true, preserveDrawingBuffer: true });
    if (!gl) throw new Error('Este navegador no soporta WebGL2.');
    this.gl = gl;
    this.prog = this._program(VS, FS);
    this.u = {};
    for (const n of ['uProj', 'uView', 'uColor', 'uUseVC', 'uAlpha', 'uLit', 'uClipOn', 'uClip'])
      this.u[n] = gl.getUniformLocation(this.prog, n);
    this.meshes = new Map();
    this.lines = new Map();
    this.target = [0, 0, 0];
    this.dist = 300;
    this.axes = { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] };   // columnas: ejes de la cámara en el mundo
    this.fov = 30 * Math.PI / 180;
    this.clip = null;        // {n:[3], d:number}
    this.plane = null;       // {center, n, size}
    this.wire = false;
    this._dirty = true;
    this._quad = this._makeBuffers();
    const loop = () => { if (this._dirty) { this._dirty = false; this._draw(); } requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
    new ResizeObserver(() => this.requestRender()).observe(canvas);
  }

  requestRender() { this._dirty = true; }

  _program(vs, fs) {
    const gl = this.gl;
    const mk = (type, src) => {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, mk(gl.VERTEX_SHADER, vs)); gl.attachShader(p, mk(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }

  _makeBuffers() {
    const gl = this.gl;
    const vao = gl.createVertexArray(), pos = gl.createBuffer(), col = gl.createBuffer();
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, pos); gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, col); gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 3, gl.UNSIGNED_BYTE, true, 0, 0);
    gl.bindVertexArray(null);
    return { vao, pos, col, count: 0 };
  }

  // ------------------------------------------------------------- mallas
  setMesh(name, vertices, indices, opts = {}) {
    const gl = this.gl;
    const nF = indices.length / 3;
    const positions = new Float32Array(nF * 9);
    for (let i = 0; i < indices.length; i++) {
      const v = indices[i] * 3;
      positions[i * 3] = vertices[v]; positions[i * 3 + 1] = vertices[v + 1]; positions[i * 3 + 2] = vertices[v + 2];
    }
    let m = this.meshes.get(name);
    if (!m) { m = this._makeBuffers(); m.visible = true; m.alpha = 1; m.color = [0.7, 0.7, 0.72]; m.clip = false; this.meshes.set(name, m); }
    Object.assign(m, opts);
    m.nF = nF; m.positions = positions; m.vertices = vertices; m.indices = indices;
    m.colors = new Uint8Array(nF * 9); m.useVC = false; m.centroids = null; m.wireBuf = null;
    gl.bindBuffer(gl.ARRAY_BUFFER, m.pos); gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, m.col); gl.bufferData(gl.ARRAY_BUFFER, m.colors, gl.DYNAMIC_DRAW);
    m.count = nF * 3;
    let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < vertices.length; i += 3)
      for (let k = 0; k < 3; k++) { const x = vertices[i + k]; if (x < lo[k]) lo[k] = x; if (x > hi[k]) hi[k] = x; }
    m.bounds = [lo, hi];
    this.requestRender();
    return m;
  }

  removeMesh(name) {
    const m = this.meshes.get(name);
    if (!m) return;
    const gl = this.gl;
    gl.deleteBuffer(m.pos); gl.deleteBuffer(m.col); gl.deleteVertexArray(m.vao);
    if (m.wireBuf) gl.deleteBuffer(m.wireBuf.pos);
    this.meshes.delete(name); this.requestRender();
  }

  centroids(name) {
    const m = this.meshes.get(name);
    if (!m.centroids) {
      const c = new Float32Array(m.nF * 3), p = m.positions;
      for (let f = 0; f < m.nF; f++)
        for (let k = 0; k < 3; k++) c[f * 3 + k] = (p[f * 9 + k] + p[f * 9 + 3 + k] + p[f * 9 + 6 + k]) / 3;
      m.centroids = c;
    }
    return m.centroids;
  }

  // colores por cara: colorOf(f) -> [r,g,b] 0..255 ; rango opcional de caras [f0, f1)
  paintFaces(name, colorOf, f0 = 0, f1 = null) {
    const m = this.meshes.get(name); if (!m) return;
    if (f1 === null) f1 = m.nF;
    const c = m.colors;
    for (let f = f0; f < f1; f++) {
      const rgb = colorOf(f);
      for (let k = 0; k < 3; k++) { c[f * 9 + k * 3] = rgb[0]; c[f * 9 + k * 3 + 1] = rgb[1]; c[f * 9 + k * 3 + 2] = rgb[2]; }
    }
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, m.col);
    gl.bufferSubData(gl.ARRAY_BUFFER, f0 * 9, c.subarray(f0 * 9, f1 * 9));
    this.requestRender();
  }

  setLines(name, segments, color = [1, 0.2, 0.2], visible = true) {
    const gl = this.gl;
    let l = this.lines.get(name);
    if (!segments) { if (l) { l.count = 0; } this.requestRender(); return; }
    if (!l) { l = this._makeBuffers(); this.lines.set(name, l); }
    gl.bindBuffer(gl.ARRAY_BUFFER, l.pos); gl.bufferData(gl.ARRAY_BUFFER, segments, gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, l.col); gl.bufferData(gl.ARRAY_BUFFER, new Uint8Array(segments.length), gl.STATIC_DRAW);
    l.count = segments.length / 3; l.color = color; l.visible = visible;
    this.requestRender();
  }

  _wire(m) {
    if (m.wireBuf) return m.wireBuf;
    const gl = this.gl, p = m.positions, n = m.nF;
    const seg = new Float32Array(n * 18);
    for (let f = 0; f < n; f++)
      for (let e = 0; e < 3; e++) {
        const a = f * 9 + e * 3, b = f * 9 + ((e + 1) % 3) * 3, o = f * 18 + e * 6;
        seg[o] = p[a]; seg[o + 1] = p[a + 1]; seg[o + 2] = p[a + 2];
        seg[o + 3] = p[b]; seg[o + 4] = p[b + 1]; seg[o + 5] = p[b + 2];
      }
    const w = this._makeBuffers();
    gl.bindBuffer(gl.ARRAY_BUFFER, w.pos); gl.bufferData(gl.ARRAY_BUFFER, seg, gl.STATIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, w.col); gl.bufferData(gl.ARRAY_BUFFER, new Uint8Array(seg.length), gl.STATIC_DRAW);
    w.count = n * 6;
    m.wireBuf = w;
    return w;
  }

  // ------------------------------------------------------------- cámara
  eye() { return V3.add(this.target, V3.scale(this.axes.z, this.dist)); }

  setView(dirFrom, up) {
    const z = V3.norm(dirFrom);
    let x = V3.cross(up, z);
    if (V3.len(x) < 1e-6) x = V3.cross([0, 1, 0], z);
    if (V3.len(x) < 1e-6) x = V3.cross([1, 0, 0], z);
    x = V3.norm(x);
    this.axes = { x, y: V3.cross(z, x), z };
    this.requestRender();
  }

  fit(bounds) {
    const [lo, hi] = bounds;
    this.target = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
    const r = 0.5 * Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) || 1;
    this.dist = r / Math.sin(this.fov / 2) * 1.05;
    this.sceneRadius = r;
    this.requestRender();
  }

  orbit(dx, dy) {
    const a = this.axes, k = 0.006;
    let x = V3.rot(a.x, a.y, -dx * k), z = V3.rot(a.z, a.y, -dx * k);
    let y = V3.rot(a.y, x, -dy * k); z = V3.rot(z, x, -dy * k);
    z = V3.norm(z); x = V3.norm(V3.cross(y, z)); y = V3.cross(z, x);
    this.axes = { x, y, z }; this.requestRender();
  }

  pan(dx, dy) {
    const s = 2 * this.dist * Math.tan(this.fov / 2) / this.canvas.clientHeight;
    this.target = V3.add(this.target, V3.add(V3.scale(this.axes.x, -dx * s), V3.scale(this.axes.y, dy * s)));
    this.requestRender();
  }

  zoom(delta) { this.dist *= Math.exp(delta * 0.0012); this.requestRender(); }

  ray(px, py) {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight, t = Math.tan(this.fov / 2);
    const nx = (2 * px / w - 1) * t * (w / h), ny = (1 - 2 * py / h) * t, a = this.axes;
    const dir = V3.norm([a.x[0] * nx + a.y[0] * ny - a.z[0], a.x[1] * nx + a.y[1] * ny - a.z[1], a.x[2] * nx + a.y[2] * ny - a.z[2]]);
    return { origin: this.eye(), dir };
  }

  // Intersección rayo-malla (Möller-Trumbore). Devuelve {face, point} o null.
  pick(px, py, name) {
    const m = this.meshes.get(name);
    if (!m) return null;
    const { origin: o, dir: d } = this.ray(px, py), p = m.positions;
    let best = Infinity, face = -1;
    for (let f = 0, i = 0; f < m.nF; f++, i += 9) {
      const ax = p[i], ay = p[i + 1], az = p[i + 2];
      const e1x = p[i + 3] - ax, e1y = p[i + 4] - ay, e1z = p[i + 5] - az;
      const e2x = p[i + 6] - ax, e2y = p[i + 7] - ay, e2z = p[i + 8] - az;
      const px_ = d[1] * e2z - d[2] * e2y, py_ = d[2] * e2x - d[0] * e2z, pz_ = d[0] * e2y - d[1] * e2x;
      const det = e1x * px_ + e1y * py_ + e1z * pz_;
      if (det > -1e-12 && det < 1e-12) continue;
      const inv = 1 / det, tx = o[0] - ax, ty = o[1] - ay, tz = o[2] - az;
      const u = (tx * px_ + ty * py_ + tz * pz_) * inv;
      if (u < 0 || u > 1) continue;
      const qx = ty * e1z - tz * e1y, qy = tz * e1x - tx * e1z, qz = tx * e1y - ty * e1x;
      const v = (d[0] * qx + d[1] * qy + d[2] * qz) * inv;
      if (v < 0 || u + v > 1) continue;
      const t = (e2x * qx + e2y * qy + e2z * qz) * inv;
      if (t > 1e-6 && t < best) {
        if (m.clip && this.clip) {
          const hx = o[0] + d[0] * t, hy = o[1] + d[1] * t, hz = o[2] + d[2] * t, c = this.clip;
          if (c.n[0] * hx + c.n[1] * hy + c.n[2] * hz - c.d < 0) continue;
        }
        best = t; face = f;
      }
    }
    if (face < 0) return null;
    return { face, point: [o[0] + d[0] * best, o[1] + d[1] * best, o[2] + d[2] * best] };
  }

  // ------------------------------------------------------------- dibujo
  _draw() {
    const gl = this.gl, cv = this.canvas, dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(cv.clientWidth * dpr)), h = Math.max(1, Math.round(cv.clientHeight * dpr));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    gl.viewport(0, 0, w, h);
    gl.clearColor(0.105, 0.115, 0.135, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.useProgram(this.prog);
    const r = this.sceneRadius || 200;
    const proj = perspective(this.fov, w / h, Math.max(this.dist - 4 * r, this.dist * 0.01, 0.1), this.dist + 6 * r);
    const a = this.axes, e = this.eye();
    const view = new Float32Array([a.x[0], a.y[0], a.z[0], 0, a.x[1], a.y[1], a.z[1], 0, a.x[2], a.y[2], a.z[2], 0,
      -V3.dot(a.x, e), -V3.dot(a.y, e), -V3.dot(a.z, e), 1]);
    gl.uniformMatrix4fv(this.u.uProj, false, proj);
    gl.uniformMatrix4fv(this.u.uView, false, view);
    if (this.clip) gl.uniform4f(this.u.uClip, this.clip.n[0], this.clip.n[1], this.clip.n[2], this.clip.d);

    const drawMesh = (m) => {
      gl.uniform3fv(this.u.uColor, m.color);
      gl.uniform1f(this.u.uUseVC, m.useVC ? 1 : 0);
      gl.uniform1f(this.u.uAlpha, m.alpha);
      gl.uniform1f(this.u.uLit, 1);
      gl.uniform1f(this.u.uClipOn, m.clip && this.clip ? 1 : 0);
      gl.bindVertexArray(m.vao);
      gl.drawArrays(gl.TRIANGLES, 0, m.count);
    };
    const vis = [...this.meshes.values()].filter((m) => m.visible && m.count);
    gl.enable(gl.POLYGON_OFFSET_FILL); gl.polygonOffset(1, 1);
    gl.disable(gl.BLEND); gl.depthMask(true);
    vis.filter((m) => m.alpha >= 0.999).forEach(drawMesh);
    gl.disable(gl.POLYGON_OFFSET_FILL);

    // líneas
    gl.uniform1f(this.u.uLit, 0); gl.uniform1f(this.u.uUseVC, 0); gl.uniform1f(this.u.uAlpha, 1);
    if (this.wire) {
      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.uniform1f(this.u.uAlpha, 0.28);
      for (const m of vis) {
        const wb = this._wire(m);
        gl.uniform1f(this.u.uClipOn, m.clip && this.clip ? 1 : 0);
        gl.uniform3f(this.u.uColor, 0.05, 0.06, 0.08);
        gl.bindVertexArray(wb.vao); gl.drawArrays(gl.LINES, 0, wb.count);
      }
      gl.disable(gl.BLEND); gl.uniform1f(this.u.uAlpha, 1);
    }
    gl.uniform1f(this.u.uClipOn, 0);
    for (const l of this.lines.values()) {
      if (!l.visible || !l.count) continue;
      gl.uniform3fv(this.u.uColor, l.color);
      gl.bindVertexArray(l.vao); gl.drawArrays(gl.LINES, 0, l.count);
    }

    // transparentes
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
    vis.filter((m) => m.alpha < 0.999).forEach(drawMesh);
    if (this.plane) {
      const { center: c, n, size: s } = this.plane;
      let u = V3.cross(n, [0, 0, 1]); if (V3.len(u) < 0.2) u = V3.cross(n, [0, 1, 0]);
      u = V3.scale(V3.norm(u), s); const v = V3.scale(V3.norm(V3.cross(n, u)), s);
      const p = [V3.sub(V3.sub(c, u), v), V3.sub(V3.add(c, u), v), V3.add(V3.add(c, u), v), V3.add(V3.sub(c, u), v)];
      const tri = new Float32Array([...p[0], ...p[1], ...p[2], ...p[0], ...p[2], ...p[3]]);
      const q = this._quad;
      gl.bindBuffer(gl.ARRAY_BUFFER, q.pos); gl.bufferData(gl.ARRAY_BUFFER, tri, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, q.col); gl.bufferData(gl.ARRAY_BUFFER, new Uint8Array(18), gl.DYNAMIC_DRAW);
      gl.uniform1f(this.u.uLit, 0); gl.uniform1f(this.u.uUseVC, 0); gl.uniform1f(this.u.uClipOn, 0);
      gl.uniform3f(this.u.uColor, 0.35, 0.7, 1.0); gl.uniform1f(this.u.uAlpha, 0.22);
      gl.bindVertexArray(q.vao); gl.drawArrays(gl.TRIANGLES, 0, 6);
    }
    gl.depthMask(true); gl.disable(gl.BLEND);
    gl.bindVertexArray(null);
  }
}
