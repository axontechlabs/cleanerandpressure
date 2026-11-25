"""Corte "plantilla": la superficie que necesita una plantilla personalizada.

La segmentación plantar (``segmentation.py``) encuentra la planta por pendiente: se detiene
donde la piel se inclina más del *Ángulo máximo* (62°). Para diseñar una plantilla hace falta
además un poco de pared: la **copa del talón**, la **pestaña medial** bajo el arco, la
**pared lateral** y un borde mínimo en antepié y dedos. Es lo que en ortopedia se llama
*línea de recorte* (trim line) y se define por la **altura sobre el suelo**, no por la
pendiente: la copa del talón de una plantilla suele medir entre 10 y 20 mm (≤ 10 mm se
considera baja) y la pestaña medial llega a la altura del arco.

Este módulo, en el marco canónico del pipeline (+Z = normal exterior de la planta, hacia el
suelo):

1. Estima el **plano de apoyo** (suelo) como la cara de la envolvente convexa inferior de la
   planta bajo la línea de carga (trípode talón – cabezas metatarsianas) y la altura de cada
   punto sobre él.
2. Construye un **marco anatómico**: eje talón→dedos (el antepié es más ancho que el talón),
   lado medial (el arco interno queda más alto que el borde lateral) y la posición relativa
   de cada punto (fracción de la longitud y lado medial/lateral).
3. Fija una **línea de recorte objetivo** por zonas: talón, mediopié medial y lateral, antepié y
   dedos, con transiciones suaves.
4. Hace **crecer la selección** desde la planta por las paredes conectadas, visibles desde el
   suelo (sin voladizos ni capas ocultas) y por debajo de esa altura.
5. Mide la **cobertura** del resultado frente a lo que el escaneo permite (talón, mediopié,
   antepié, dedos, altura de pared alcanzada) para que el pipeline —o la IA— reajuste los
   parámetros y vuelva a cortar si falta algo.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .mesh import Mesh, build_topology, face_components
from .raster import make_grid, rasterize
from .smoothing import upper_hull

# Zonas a lo largo del pie (fracción de la longitud desde el borde del talón)
ZONES = (("talon", "Talón", 0.0, 0.25), ("mediopie", "Mediopié / arco", 0.25, 0.60),
         ("antepie", "Antepié", 0.60, 0.80), ("dedos", "Dedos", 0.80, 1.01))
ZONE_WEIGHTS = {"talon": 0.25, "mediopie": 0.20, "antepie": 0.25, "dedos": 0.30}
LOAD_POINT = 0.32
CLIP_MARGIN = 4.0      # mm: la pared se selecciona algo por encima de la línea y se recorta exacta


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


# ============================================================================ marco anatómico
def axis_analysis(xy: np.ndarray) -> dict:
    """Eje largo (talón→dedos) por PCA; el antepié es más ancho que el talón."""
    c = xy.mean(axis=0)
    cov = np.cov((xy - c).T)
    w, vecs = np.linalg.eigh(cov)
    e1 = vecs[:, int(np.argmax(w))]
    e2 = np.array([-e1[1], e1[0]])
    s = (xy - c) @ e1
    q = (xy - c) @ e2
    lo, hi = float(s.min()), float(s.max())
    L = max(hi - lo, 1e-9)
    nb = 40
    b = np.clip(((s - lo) / L * nb).astype(int), 0, nb - 1)
    qmin = np.full(nb, np.inf)
    qmax = np.full(nb, -np.inf)
    np.minimum.at(qmin, b, q)
    np.maximum.at(qmax, b, q)
    width = np.where(np.isfinite(qmin), qmax - qmin, 0.0)
    pos = (np.arange(nb) + 0.5) / nb
    front = width[(pos > 0.60) & (pos < 0.80)].mean()
    back = width[(pos > 0.08) & (pos < 0.25)].mean()
    if front < back:
        e1, e2 = -e1, -e2
        lo, hi = -hi, -lo
    return {"center": c, "e1": e1, "e2": e2, "s0": lo, "length": L,
            "width_ratio": float(max(front, back) / max(min(front, back), 1e-9))}


class AnatomyFrame:
    """Suelo + eje del pie + lado medial, a partir de la planta segmentada (marco canónico)."""

    def __init__(self, sole_vertices: np.ndarray, load_point: float = LOAD_POINT):
        v = np.asarray(sole_vertices, float)
        if len(v) < 30:
            raise ValueError("La planta segmentada es demasiado pequeña para el corte de plantilla.")
        ax = axis_analysis(v[:, :2])
        self.center, self.e1, self.e2 = ax["center"], ax["e1"], ax["e2"]
        self.s0, self.length = ax["s0"], ax["length"]
        # --- suelo: plano de la envolvente convexa inferior bajo la línea de carga
        s_load = self.s0 + load_point * self.length
        P = self.center + self.e1 * s_load
        planes = upper_hull(v[:, :2], v[:, 2])
        if planes is None:
            self.plane = np.array([0.0, 0.0, float(v[:, 2].max())])
        else:
            a, b, c = planes
            k = int(np.argmin(a * P[0] + b * P[1] + c))
            self.plane = np.array([a[k], b[k], c[k]])
        h = self.height(v)
        sn = self.sn(v[:, :2])
        q = self.q(v[:, :2])
        # --- perfil de anchura de la planta (centro y semianchura por franjas)
        nb = 50
        self._nb = nb
        b = np.clip((sn * nb).astype(int), 0, nb - 1)
        qmin = np.full(nb, np.inf)
        qmax = np.full(nb, -np.inf)
        np.minimum.at(qmin, b, q)
        np.maximum.at(qmax, b, q)
        ok = np.isfinite(qmin)
        centers = (np.arange(nb) + 0.5) / nb
        self._qmid = np.interp(centers, centers[ok], (0.5 * (qmin + qmax))[ok])
        self._half = np.maximum(np.interp(centers, centers[ok], (0.5 * (qmax - qmin))[ok]), 5.0)
        # --- lado medial: en el mediopié el arco interno queda más alto que el borde lateral
        r = (q - self._qmid[b]) / self._half[b]
        mid = (sn > 0.33) & (sn < 0.60)
        hp = h[mid & (r > 0.25) & (r < 0.9)]
        hn = h[mid & (r < -0.25) & (r > -0.9)]
        diff = (np.median(hp) if len(hp) else 0.0) - (np.median(hn) if len(hn) else 0.0)
        self.medial_sign = 1.0 if diff >= 0 else -1.0
        self.side_confidence = "alta" if abs(diff) > 2.0 else ("media" if abs(diff) > 0.7 else "baja")
        # --- altura del arco (borde medial del mediopié, sin carga)
        rm = r * self.medial_sign
        arch_sel = mid & (rm > 0.45) & (rm < 1.05)
        self.arch_height = float(np.percentile(h[arch_sel], 90)) if arch_sel.sum() > 10 else 0.0
        self.width = float(2 * self._half.max())

    # ---- coordenadas
    def height(self, p: np.ndarray) -> np.ndarray:
        """Altura sobre el suelo (mm, ≥ 0 sobre la planta)."""
        p = np.asarray(p, float)
        return self.plane[0] * p[:, 0] + self.plane[1] * p[:, 1] + self.plane[2] - p[:, 2]

    def sn(self, xy: np.ndarray) -> np.ndarray:
        """Fracción de la longitud desde el borde del talón."""
        return ((np.asarray(xy, float) - self.center) @ self.e1 - self.s0) / self.length

    def q(self, xy: np.ndarray) -> np.ndarray:
        return (np.asarray(xy, float) - self.center) @ self.e2

    def side(self, xy: np.ndarray) -> np.ndarray:
        """Posición mediolateral: +1 borde medial, −1 borde lateral (|·| > 1 fuera de la planta)."""
        sn = self.sn(xy)
        b = np.clip((sn * self._nb).astype(int), 0, self._nb - 1)
        return (self.q(xy) - self._qmid[b]) / self._half[b] * self.medial_sign

    def to_dict(self) -> dict:
        return {"plane": self.plane.tolist(), "center": self.center.tolist(), "e1": self.e1.tolist(),
                "e2": self.e2.tolist(), "s0": self.s0, "length_mm": self.length, "width_mm": self.width,
                "medial_sign": self.medial_sign, "side_confidence": self.side_confidence,
                "arch_height_mm": self.arch_height}


# ============================================================================ línea de recorte
def trim_targets(p, frame: AnatomyFrame) -> dict:
    """Altura objetivo (mm sobre el suelo) de la línea de recorte en cada zona."""
    med = float(p.medial_flange)
    auto = med <= 0
    if auto:
        med = float(np.clip(frame.arch_height + 6.0, max(p.lateral_flange, 10.0), 40.0))
    return {"talon": float(p.heel_cup), "mediopie_medial": med, "mediopie_lateral": float(p.lateral_flange),
            "antepie": float(p.forefoot_wall), "dedos": float(p.toe_wall), "medial_auto": auto}


def trim_height(xy: np.ndarray, frame: AnatomyFrame, t: dict) -> np.ndarray:
    """Altura de recorte en cada punto, con transiciones suaves entre zonas y lados."""
    sn = frame.sn(xy)
    sd = frame.side(xy)
    pts = [-1.0, 0.18, 0.32, 0.56, 0.68, 0.79, 0.86, 2.0]
    hm = np.interp(sn, pts, [t["talon"], t["talon"], t["mediopie_medial"], t["mediopie_medial"],
                             t["antepie"], t["antepie"], t["dedos"], t["dedos"]])
    hl = np.interp(sn, pts, [t["talon"], t["talon"], t["mediopie_lateral"], t["mediopie_lateral"],
                             t["antepie"], t["antepie"], t["dedos"], t["dedos"]])
    w = _smoothstep((sd + 0.35) / 0.7)
    return hl + w * (hm - hl)


def grow_walls(mesh: Mesh, keep: np.ndarray, forced_remove: np.ndarray, p, grid_res: float = 1.0,
               topo=None) -> dict:
    """Añade a ``keep`` las paredes conectadas por debajo de la línea de recorte.

    Devuelve la máscara de caras nuevas y la información del marco y de los objetivos.
    """
    nf = mesh.n_faces
    topo = topo or build_topology(mesh)
    v = mesh.vertices
    f = mesh.faces
    n, area = mesh.face_normals()
    cen = mesh.centroids
    sole_v = np.unique(f[keep])
    frame = AnatomyFrame(v[sole_v])
    targets = trim_targets(p, frame)
    # sentido exterior de la planta según la selección (las normales pueden venir invertidas)
    s0 = np.sign((area[keep] * n[keep, 2]).sum()) or 1.0
    nz = s0 * n[:, 2]
    hv = frame.height(v)
    h_face = hv[f].min(axis=1)              # la cara empieza por debajo de la línea (se recorta exacta después)
    h_cen = frame.height(cen)
    # altura local de la planta: la del vértice de la planta más cercano en planta (los dedos
    # levantados de un escaneo sin carga tienen su propia base, no la del suelo)
    from scipy.spatial import cKDTree
    tree = cKDTree(v[sole_v, :2])
    d_sole, idx = tree.query(cen[:, :2])
    h_ref = hv[sole_v][idx]
    tgt = trim_height(cen[:, :2], frame, targets)
    allowed = np.maximum(tgt, h_ref + p.wall_min)
    # visibilidad desde el suelo (z-buffer): sin capas ocultas ni la cara interior del pie
    grid = make_grid(v[:, :2].min(axis=0), v[:, :2].max(axis=0), grid_res, pad=3)
    ztop = rasterize(mesh.triangles, grid)["zmax"]
    gi, gj = grid.index(cen[:, :2])
    tan_f = np.minimum(np.sqrt(np.clip(1 - nz ** 2, 0, 1)) / np.maximum(np.abs(nz), 1e-3), 6.0)
    depth = ztop[gi, gj] - cen[:, 2]
    visible = np.isfinite(depth) & (depth <= p.layer_tol + grid.res * tan_f + 0.5)
    cand = (~keep & ~forced_remove & (nz >= np.cos(np.radians(p.wall_max_angle))) & visible
            & (h_face <= allowed + 0.5) & (hv[f].max(axis=1) <= allowed + CLIP_MARGIN)
            & (h_cen >= -2.0) & (d_sole <= p.wall_max_reach))
    # crecimiento por adyacencia desde la planta (solo aristas variedad, sin pliegues bruscos)
    adj = topo.adj
    man = adj >= 0
    nn = n[np.maximum(adj, 0)]
    sgn = np.where(topo.consistent, 1.0, -1.0)
    cosd = np.einsum("fk,fjk->fj", n, nn) * sgn
    edge_ok = man & (cosd >= np.cos(np.radians(p.crease_angle)))
    sel = keep.copy()
    frontier = np.flatnonzero(keep)
    added = np.zeros(nf, dtype=bool)
    while len(frontier):
        nb = adj[frontier]
        c = np.unique(nb[edge_ok[frontier]])
        c = c[(c >= 0)]
        c = c[cand[c] & ~sel[c]]
        if len(c) == 0:
            break
        sel[c] = True
        added[c] = True
        frontier = c
    # islas minúsculas de pared (dientes) se descartan
    if added.any():
        nc, lab = face_components(topo, nf, face_ok=added)
        if nc:
            a_c = np.bincount(lab[added], weights=area[added], minlength=nc)
            tiny = a_c < max(p.min_island_area * 0.2, 2.0)
            added[added] = ~tiny[lab[added]]
    info = {"frame": frame_dict(frame), "targets": targets, "wall_faces": int(added.sum()),
            "wall_area": float(area[added].sum())}
    return {"added": added, "frame": frame, "info": info}


# ============================================================================ cobertura
def _zone_of(sn: np.ndarray) -> np.ndarray:
    z = np.full(sn.shape, -1)
    for k, (_, _, a, b) in enumerate(ZONES):
        z[(sn >= a) & (sn < b)] = k
    return z


def coverage_report(full: Mesh, surface: Mesh, frame_d: dict, targets: dict, params=None,
                    res: float = 1.0) -> dict:
    """Compara la superficie resultante con lo que el escaneo permite conservar.

    * Huella posible: caras del escaneo conectadas a la planta, visibles desde el suelo, que
      miran hacia él y están por debajo de la línea de recorte (más un margen).
    * Cobertura por zona: fracción de esa huella posible que cubre el resultado.
    * Longitud: del resultado frente a la huella posible (si faltan dedos o talón, baja).
    * Pared: altura alcanzada en el contorno del resultado frente al objetivo y a lo disponible.
    """
    frame = _FrameView(frame_d)
    v = full.vertices
    n, area = full.face_normals()
    cen = full.centroids
    s_v = surface.vertices
    # sentido exterior: el de la mayor parte de la superficie resultante proyectada
    grid = make_grid(np.minimum(v[:, :2].min(axis=0), s_v[:, :2].min(axis=0)),
                     np.maximum(v[:, :2].max(axis=0), s_v[:, :2].max(axis=0)), res, pad=4)
    ztop = rasterize(full.triangles, grid)["zmax"]
    gi, gj = grid.index(cen[:, :2])
    rs = rasterize(surface.triangles, grid)
    R = rs["count"] > 0
    # orientación de las normales del escaneo respecto a la planta resultante
    in_r = R[gi, gj]
    s0 = np.sign((area[in_r] * n[in_r, 2]).sum()) or 1.0
    nz = s0 * n[:, 2]
    tan_f = np.minimum(np.sqrt(np.clip(1 - nz ** 2, 0, 1)) / np.maximum(np.abs(nz), 1e-3), 6.0)
    visible = np.isfinite(ztop[gi, gj]) & (ztop[gi, gj] - cen[:, 2] <= 1.5 + res * tan_f + 0.5)
    hf = frame.height(cen)
    tgt = trim_height(cen[:, :2], frame, targets)
    wall_max = getattr(params, "wall_max_angle", 80.0) if params is not None else 80.0
    core_max = getattr(params, "max_angle", 62.0) if params is not None else 62.0
    plantar_like = (nz >= np.cos(np.radians(max(core_max, 62.0)))) & (hf <= 35.0)
    wall_like = (nz >= np.cos(np.radians(wall_max))) & (hf <= tgt + 2.0)
    possible = visible & (hf >= -2.0) & (plantar_like | wall_like)
    # solo lo conectado (cara a cara, a través de caras posibles) con lo que el resultado ya
    # cubre: sin fragmentos sueltos ni alas unidas por una pared que supera la línea de recorte
    topo = build_topology(full)
    nc, comp = face_components(topo, full.n_faces, face_ok=possible)
    if nc:
        hit = np.unique(comp[in_r & possible & (comp >= 0)])
        possible &= np.isin(comp, hit)
    O = rasterize(full.triangles[possible], grid)["count"] > 0 if possible.any() else np.zeros_like(R)
    O = ndimage.binary_closing(O, structure=np.ones((3, 3), bool), iterations=2)
    xs, ys = grid.node_xy()
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    xy = np.stack([X.ravel(), Y.ravel()], axis=1)
    sn = frame.sn(xy).reshape(X.shape)
    zone = _zone_of(sn)
    zones = {}
    score = 0.0
    for k, (key, name, _, _) in enumerate(ZONES):
        oz = O & (zone == k)
        rz = R & (zone == k)
        cov = float((oz & R).sum() / oz.sum()) if oz.sum() > 20 else (1.0 if rz.sum() > 0 else 0.0)
        zones[key] = {"name": name, "coverage": round(cov, 3), "area_possible_cm2": float(oz.sum() * res * res / 100),
                      "area_result_cm2": float(rz.sum() * res * res / 100)}
        score += ZONE_WEIGHTS[key] * cov
    # longitud del resultado frente a la huella posible
    snR = sn[R]
    snO = sn[O] if O.any() else snR
    length_R = float((snR.max() - snR.min()) * frame.length) if R.any() else 0.0
    length_O = float((snO.max() - snO.min()) * frame.length) if O.any() else length_R
    heel_missing = float(max(snR.min() - snO.min(), 0.0) * frame.length) if R.any() else 0.0
    toe_missing = float(max(snO.max() - snR.max(), 0.0) * frame.length) if R.any() else 0.0
    extra = float((R & ~ndimage.binary_dilation(O, iterations=3)).sum() / max(R.sum(), 1))
    # altura de pared alcanzada en el contorno del resultado
    from .mesh import boundary_loops
    loops = boundary_loops(surface)
    walls = {}
    if loops:
        b = np.concatenate(loops)
        pb = s_v[b]
        hb = frame.height(pb)
        snb = frame.sn(pb[:, :2])
        sdb = frame.side(pb[:, :2])
        tb = trim_height(pb[:, :2], frame, targets)
        for key, lo, hi, side in (("talon", 0.0, 0.22, None), ("mediopie_medial", 0.32, 0.56, 1),
                                  ("mediopie_lateral", 0.32, 0.56, -1), ("antepie", 0.62, 0.78, None)):
            m = (snb >= lo) & (snb < hi)
            if side is not None:
                m &= (sdb * side) > 0
            if m.sum() < 3:
                continue
            got = float(np.median(hb[m]))
            want = float(np.median(tb[m]))
            walls[key] = {"reached_mm": round(got, 1), "target_mm": round(want, 1),
                          "ratio": round(min(got / max(want, 1e-6), 1.5), 2)}
    wall_score = float(np.mean([min(w["ratio"], 1.0) for w in walls.values()])) if walls else 0.0
    total = 0.85 * score + 0.15 * wall_score - 0.5 * extra - 0.004 * (heel_missing + toe_missing)
    problems = []
    for key, z in zones.items():
        if z["coverage"] < 0.9 and z["area_possible_cm2"] > 2:
            problems.append(f"{z['name']}: cubre solo el {100 * z['coverage']:.0f} % de lo que permite el escaneo")
    if toe_missing > 6:
        problems.append(f"Faltan unos {toe_missing:.0f} mm por la punta de los dedos")
    if heel_missing > 6:
        problems.append(f"Faltan unos {heel_missing:.0f} mm por el borde del talón")
    for key, w in walls.items():
        if w["ratio"] < 0.7:
            problems.append(f"Pared {key.replace('_', ' ')}: {w['reached_mm']:.0f} mm de {w['target_mm']:.0f} mm objetivo")
    if extra > 0.03:
        problems.append(f"{100 * extra:.0f} % de la superficie queda fuera de la huella esperada (posible geometría ajena)")
    return {"zones": zones, "walls": walls, "length_mm": round(length_R, 1), "length_possible_mm": round(length_O, 1),
            "toe_missing_mm": round(toe_missing, 1), "heel_missing_mm": round(heel_missing, 1),
            "extra_fraction": round(extra, 4), "coverage_score": round(score, 4), "wall_score": round(wall_score, 3),
            "score": round(total, 4), "problems": problems, "ok": not problems}


class _FrameView(AnatomyFrame):
    """Marco anatómico reconstruido desde su diccionario (sin recalcular)."""

    def __init__(self, d: dict):          # noqa: D401 - no llama al constructor padre
        self.plane = np.asarray(d["plane"], float)
        self.center = np.asarray(d["center"], float)
        self.e1 = np.asarray(d["e1"], float)
        self.e2 = np.asarray(d["e2"], float)
        self.s0 = float(d["s0"])
        self.length = float(d["length_mm"])
        self.medial_sign = float(d["medial_sign"])
        self.arch_height = float(d.get("arch_height_mm", 0.0))
        self.width = float(d.get("width_mm", 0.0))
        self.side_confidence = d.get("side_confidence", "")
        self._nb = len(d["qmid"])
        self._qmid = np.asarray(d["qmid"], float)
        self._half = np.asarray(d["half"], float)


def frame_dict(frame: AnatomyFrame) -> dict:
    d = frame.to_dict()
    d["qmid"] = frame._qmid.tolist()
    d["half"] = frame._half.tolist()
    return d


def frame_view(d: dict) -> AnatomyFrame:
    return _FrameView(d)


# ============================================================================ paredes
def _slice_points(mesh: Mesh, hv: np.ndarray, level: float) -> np.ndarray:
    """Puntos (xy) de la intersección de la malla con la superficie «altura sobre el suelo = level»."""
    F = mesh.faces
    fv = hv[F] - level
    s = np.sign(fv)
    s[s == 0] = 1
    cross = ~((s[:, 0] == s[:, 1]) & (s[:, 1] == s[:, 2]))
    if not cross.any():
        return np.zeros((0, 2))
    Fc, fc = F[cross], fv[cross]
    v = mesh.vertices
    pts = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        m = np.sign(fc[:, a]) != np.sign(fc[:, b])
        m &= (fc[:, a] != fc[:, b])
        t = fc[m, a] / (fc[m, a] - fc[m, b])
        pa, pb = v[Fc[m, a], :2], v[Fc[m, b], :2]
        pts.append(pa + t[:, None] * (pb - pa))
    return np.concatenate(pts) if pts else np.zeros((0, 2))


def build_walls(plantar: Mesh, scan: Mesh, frame_d: dict, targets: dict, wall_min: float = 2.0,
                n_rows: int = 10, reach: float = 25.0, level_step: float = 0.5, planes=None) -> dict:
    """Pared de la plantilla como una franja de columnas a lo largo del borde de la planta.

    Un campo de alturas en rejilla XY no puede representar paredes casi verticales (salen escalones y
    el borde en dientes de sierra). La pared se construye aparte:

    * Base = borde exterior de la planta remallada (los mismos vértices: la unión es exacta).
    * Arriba = la línea de recorte: ``max(altura objetivo de la zona, borde de la planta + wall_min)``,
      suavizada a lo largo del borde.
    * En cada columna (perpendicular al borde, hacia fuera) se sigue la pared del escaneo subiendo por
      cortes horizontales cada ``level_step`` mm; donde el escaneo no tiene pared se prolonga la última
      inclinación. Los desplazamientos se suavizan a lo largo del borde y en altura.

    En los tramos del borde que vienen de un plano de corte (``planes``: lista de ``(punto, normal)``)
    la pared sube recta, sin salirse del plano.

    Devuelve la malla de la pared (con los vértices de la base en el mismo orden que el borde) y la
    unión planta + pared.
    """
    from scipy.spatial import cKDTree
    from .mesh import boundary_loops
    frame = frame_view(frame_d)
    loops = boundary_loops(plantar)
    if not loops:
        raise ValueError("La planta no tiene borde.")
    lp = np.asarray(max(loops, key=len))
    F = plantar.faces
    # orientación del borde según el sentido de las caras (para coser la pared con el mismo sentido)
    a0, b0 = lp[0], lp[1]
    has_ab = ((F[:, 0] == a0) & (F[:, 1] == b0)) | ((F[:, 1] == a0) & (F[:, 2] == b0)) | ((F[:, 2] == a0) & (F[:, 0] == b0))
    if not has_ab.any():
        lp = lp[::-1]
    P = plantar.vertices[lp]
    n = len(lp)
    xy = P[:, :2]
    h0 = frame.height(P)
    # dirección hacia fuera en planta (perpendicular a la tangente suavizada)
    tang = np.roll(xy, -3, axis=0) - np.roll(xy, 3, axis=0)
    tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-9)
    area2 = np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1])
    out = np.stack([tang[:, 1], -tang[:, 0]], axis=1) * (1.0 if area2 > 0 else -1.0)
    out = ndimage.gaussian_filter1d(out, 3.0, axis=0, mode="wrap")
    out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
    # altura de la línea de recorte en cada columna
    H = np.maximum(trim_height(xy, frame, targets), h0 + wall_min)
    H = ndimage.gaussian_filter1d(H, 6.0, mode="wrap")
    H = np.maximum(H, h0 + max(wall_min, 0.8))
    # cortes horizontales del escaneo (solo lo cercano al borde)
    hv_scan = frame.height(scan.vertices)
    lo, hi = float(max(h0.min() - 1, -2)), float(H.max() + 1)
    levels = np.arange(lo, hi + level_step, level_step)
    trees = {}
    tree_all = cKDTree(xy)
    for j, L in enumerate(levels):
        pts = _slice_points(scan, hv_scan, L)
        if len(pts):
            d, _ = tree_all.query(pts, distance_upper_bound=reach + 2)
            pts = pts[np.isfinite(d)]
        trees[j] = cKDTree(pts) if len(pts) else None
    # seguir la pared columna a columna
    K = n_rows
    D = np.zeros((n, K + 1))
    Hk = h0[:, None] + (H - h0)[:, None] * (np.arange(K + 1) / K)[None, :]
    found = np.zeros((n, K + 1), bool)
    found[:, 0] = True
    for k in range(1, K + 1):
        dprev = D[:, k - 1]
        slope = D[:, k - 1] - D[:, k - 2] if k >= 2 else np.zeros(n)
        pred = xy + out * (dprev + slope)[:, None]
        jl = np.clip(np.rint((Hk[:, k] - lo) / level_step).astype(int), 0, len(levels) - 1)
        dk = dprev + slope                         # por defecto, se prolonga la inclinación
        for j in np.unique(jl):
            t = trees.get(int(j))
            if t is None:
                continue
            idx = np.flatnonzero(jl == j)
            dist, nn = t.query(pred[idx], distance_upper_bound=3.0)
            ok = np.isfinite(dist)
            if ok.any():
                q = t.data[nn[ok]]
                dk[idx[ok]] = np.einsum("ij,ij->i", q - xy[idx[ok]], out[idx[ok]])
                found[idx[ok], k] = True
        # la pared no se mete bajo la planta ni se aleja más de lo razonable
        D[:, k] = np.clip(dk, dprev - 1.0, np.minimum(dprev + 6.0, reach))
    # suavizado a lo largo del borde y en altura
    D[:, 1:] = ndimage.gaussian_filter1d(D[:, 1:], 6.0, axis=0, mode="wrap")
    D[:, 1:] = ndimage.gaussian_filter1d(D[:, 1:], 0.8, axis=1, mode="nearest")
    # sin voladizos: vista desde el suelo, la pared solo puede ir hacia fuera al subir (si no, la
    # superficie dejaría de ser un campo de alturas y el sólido se autointersecaría)
    D[:, 0] = 0.0
    for k in range(1, K + 1):
        D[:, k] = np.maximum(D[:, k], D[:, k - 1] + 0.02)
    # tramos del borde que vienen de un plano de corte: sin pared (se desvanece suavemente a los lados)
    wgt = np.ones(n)
    if planes:
        on_plane = np.zeros(n, bool)
        for pt, nr in planes:
            on_plane |= np.abs((P - pt) @ nr) < 1.5
        if on_plane.any():
            wgt = 1.0 - np.clip(ndimage.gaussian_filter1d(on_plane.astype(float), 3.0, mode="wrap") * 2.0, 0, 1)
            wgt[on_plane] = 0.0
            wgt[wgt < 0.02] = 0.0
            D *= wgt[:, None]
            Hk = h0[:, None] + (Hk - h0[:, None]) * wgt[:, None]
    # vértices de la pared
    a_, b_, c_ = frame.plane
    Wxy = xy[:, None, :] + out[:, None, :] * D[:, :, None]
    Wz = a_ * Wxy[..., 0] + b_ * Wxy[..., 1] + c_ - Hk
    W = np.concatenate([Wxy, Wz[..., None]], axis=2)            # (n, K+1, 3)
    nv0 = plantar.n_vertices
    vid = np.empty((n, K + 1), np.int64)
    vid[:, 0] = lp
    vid[:, 1:] = nv0 + np.arange(n * K).reshape(n, K)
    flat = wgt <= 0
    vid[flat, 1:] = lp[flat, None]                    # columna sin pared: todo en el vértice de la base
    new_v = W[:, 1:, :].reshape(-1, 3)
    i0 = np.arange(n)
    i1 = np.roll(i0, -1)
    faces = []
    for k in range(K):
        A, B = vid[i0, k], vid[i1, k]
        C, Dd = vid[i1, k + 1], vid[i0, k + 1]
        faces.append(np.stack([B, A, Dd], axis=1))
        faces.append(np.stack([B, Dd, C], axis=1))
    wf = np.concatenate(faces)
    wf = wf[(wf[:, 0] != wf[:, 1]) & (wf[:, 1] != wf[:, 2]) & (wf[:, 2] != wf[:, 0])]
    allv = np.concatenate([plantar.vertices, new_v])
    allf = np.concatenate([plantar.faces, wf])
    used = np.zeros(len(allv), bool)
    used[allf.ravel()] = True
    used[:nv0] = True                                  # la planta conserva su numeración
    remap = np.cumsum(used) - 1
    merged = Mesh(allv[used], remap[allf])
    new_v = allv[nv0:][used[nv0:]]
    cover = float(found[:, 1:].mean())
    info = {"columns": int(n), "rows": int(K), "scan_coverage": round(cover, 3),
            "height_mm": {"min": float((H - h0).min()), "max": float((H - h0).max())},
            "offset_mm_max": float(D.max())}
    return {"mesh": merged, "n_wall_vertices": int(len(new_v)), "info": info}
