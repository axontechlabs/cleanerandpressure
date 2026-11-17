"""Etapa 4: suavizado adaptativo que preserva la anatomía.

Método por defecto, **multiescala** (``multiscale_smooth``). La superficie se descompone en
bandas de tamaño de detalle con *splines* de placa delgada (penalización del Laplaciano, que
reproduce exactamente planos y curvaturas constantes y por tanto no encoge ni aplana):

* Todo lo menor que ``min_scale`` se elimina siempre, con pesos robustos frente a puntos
  aislados (el granulado típico de los sensores de profundidad).
* En cada banda se mide el **nivel típico de textura del propio escaneo** (mediana de la
  energía local). Donde la banda está a ese nivel es textura (ruido del sensor, tejido de un
  calcetín, arrugas) y se elimina por completo; donde lo supera con claridad y de forma
  extensa es relieve anatómico (dedos, surcos, bordes) y se conserva.
* El tamaño máximo de textura a eliminar se fija a mano o se elige automáticamente según
  cuánta forma típica contiene cada banda.

Los umbrales son relativos al nivel medido en cada escaneo, de modo que el mismo ajuste sirve
para un pie descalzo y para uno con calcetín.

Método alternativo, **local** (``adaptive_smooth``): regresión cuadrática local (MLS) con
intervención según el ruido; más conservador, útil para escaneos ya limpios.

Solo se modifica la coordenada Z (eje plantar): el contorno y la proyección no cambian, de
modo que la superficie sigue siendo un campo de alturas válido.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.sparse.linalg import splu
from scipy.spatial import ConvexHull, cKDTree

from .mesh import Mesh, boundary_loops, cross2


@dataclass
class SmoothParams:
    method: str = "multiescala"          # "multiescala" | "local"
    intensity: float = 0.7               # 0..1: nivel de alisado (0 = no tocar)
    texture_scale: float = 24.0          # mm: tamaño máximo de las irregularidades a eliminar
    detail_protection: float = 0.5       # 0..1: cuánto relieve localizado se conserva
    min_scale: float = 4.0               # mm: todo detalle menor se elimina siempre
    max_deviation: float = 2.0           # mm: desviación máxima respecto a la superficie de entrada (0 = sin límite)
    # --- solo método "local"
    radius: float = 4.0                  # mm: radio de influencia
    iterations: int = 3
    curvature_preservation: float = 0.5  # 0..1
    arch_protection: float = 0.7         # 0..1
    noise_floor: float = 0.03            # mm: rugosidad por debajo de la cual apenas se interviene

    @classmethod
    def from_dict(cls, d: dict | None) -> "SmoothParams":
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})


class _Neighborhood:
    """Vecindario fijo (en XY) y ajuste cuadrático ponderado por bloques.

    Si el radio pedido abarca más vértices de los que conviene usar por punto (mallas muy
    densas), los vértices se agrupan en celdas y el ajuste se hace sobre las medias de celda:
    el radio de influencia se respeta siempre y el coste no crece con la densidad.
    """

    def __init__(self, xy: np.ndarray, radius: float, k_max: int = 64):
        self.n = len(xy)
        self.xy = xy
        tree = cKDTree(xy)
        d2, _ = tree.query(xy, k=min(2, self.n))
        self.spacing = float(np.median(d2[:, -1])) if self.n > 1 else 1.0
        need = int(1.15 * np.pi * radius ** 2 / max(self.spacing, 1e-9) ** 2) + 2
        if need > k_max and self.n > k_max:
            cell = self.spacing * float(np.ceil(np.sqrt(need / (0.75 * k_max))))
            q = np.floor((xy - xy.min(axis=0)) / cell).astype(np.int64)
            _, self.cluster = np.unique(q[:, 0] * (int(q[:, 1].max()) + 1) + q[:, 1], return_inverse=True)
            self.cluster = self.cluster.ravel()
            self.count = np.bincount(self.cluster).astype(np.float64)
            self.src = np.stack([np.bincount(self.cluster, weights=xy[:, k]) / self.count for k in (0, 1)], axis=1)
            tree = cKDTree(self.src)
        else:
            self.cluster = None
            self.count = np.ones(self.n)
            self.src = xy
        k = int(min(max(need, 7), k_max, len(self.src)))
        dist, idx = tree.query(xy, k=k, distance_upper_bound=radius)
        self.valid = idx < len(self.src)
        self.idx = np.where(self.valid, idx, 0)
        dist = np.where(self.valid, dist, 0.0)
        self.radius_eff = float(np.percentile(dist.max(axis=1), 50)) if self.n else radius
        self.radius = radius
        self.w0 = np.exp(-3.0 * (dist / radius) ** 2) * self.valid * self.count[self.idx]
        self.wsum = np.maximum(self.w0.sum(axis=1), 1e-300)
        self.k = k
        self.clustered = self.cluster is not None

    def _source(self, f: np.ndarray) -> np.ndarray:
        """Valores del campo en los puntos de apoyo (medias de celda si hay agrupación)."""
        if self.cluster is None:
            return f
        if f.ndim == 1:
            return np.bincount(self.cluster, weights=f) / self.count
        return np.stack([np.bincount(self.cluster, weights=f[:, c]) / self.count for c in range(f.shape[1])], axis=1)

    def average(self, f: np.ndarray) -> np.ndarray:
        """Media ponderada (gaussiana) de un campo por vértice."""
        fs = self._source(f)
        if f.ndim == 1:
            return (self.w0 * fs[self.idx]).sum(axis=1) / self.wsum
        return np.einsum("nk,nkc->nc", self.w0, fs[self.idx]) / self.wsum[:, None]

    def fit(self, z: np.ndarray, range_sigma: np.ndarray | None = None, chunk: int = 12000):
        """Ajuste cuadrático ponderado centrado en cada vértice.

        Devuelve los 6 coeficientes (1, u, v, u², uv, v²) con u, v en unidades de radio.
        Si ``range_sigma`` no es None se hace una segunda pasada con pesos robustos.
        """
        coef = np.zeros((self.n, 6))
        ridge = np.diag([1e-9, 1e-6, 1e-6, 1e-3, 1e-3, 1e-3])
        zs = self._source(z)
        for s in range(0, self.n, chunk):
            e = min(s + chunk, self.n)
            ix = self.idx[s:e]
            u = (self.src[ix, 0] - self.xy[s:e, None, 0]) / self.radius
            v = (self.src[ix, 1] - self.xy[s:e, None, 1]) / self.radius
            phi = np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=2)
            zj = zs[ix]
            w = self.w0[s:e]
            for it in range(2 if range_sigma is not None else 1):
                wp = phi * w[:, :, None]
                m = np.matmul(wp.transpose(0, 2, 1), phi)
                m += ridge * w.sum(axis=1)[:, None, None]
                rhs = np.einsum("nka,nk->na", wp, zj)
                c = np.linalg.solve(m, rhs[:, :, None])[:, :, 0]
                if range_sigma is not None and it == 0:
                    resid = zj - np.einsum("nka,na->nk", phi, c)
                    sr = np.maximum(range_sigma[s:e], 1e-6)[:, None]
                    w = self.w0[s:e] * np.exp(-0.5 * (resid / sr) ** 2)
            coef[s:e] = c
        return coef


def upper_hull(xy: np.ndarray, z: np.ndarray, max_hull_points: int = 20000, seed: int = 0):
    """Planos (a, b, c) de la envolvente convexa superior: z_hull(x, y) = min(a·x + b·y + c)."""
    n = len(z)
    if n < 10:
        return None
    rng = np.random.default_rng(seed)
    sub = rng.choice(n, size=min(n, max_hull_points), replace=False)
    pts = np.column_stack([xy[sub], z[sub]])
    try:
        hull = ConvexHull(pts)
    except Exception:
        return None
    eq = hull.equations
    up = eq[eq[:, 2] > 1e-6]
    if len(up) == 0:
        return None
    return -up[:, 0] / up[:, 2], -up[:, 1] / up[:, 2], -up[:, 3] / up[:, 2]


def hull_height(planes, xy: np.ndarray) -> np.ndarray:
    a, b, c = planes
    out = np.empty(len(xy))
    step = max(1, int(4_000_000 / len(a)))
    for s in range(0, len(xy), step):
        e = min(s + step, len(xy))
        out[s:e] = (xy[s:e, 0, None] * a[None] + xy[s:e, 1, None] * b[None] + c[None]).min(axis=1)
    return out


def concavity_gap(xy: np.ndarray, z: np.ndarray, planes=None) -> np.ndarray:
    """Distancia vertical de cada punto a la envolvente convexa superior ("puente" del arco)."""
    planes = planes if planes is not None else upper_hull(xy, z)
    if planes is None:
        return np.zeros(len(z))
    return np.maximum(hull_height(planes, xy) - z, 0.0)


def arch_metrics(xy: np.ndarray, z: np.ndarray, spacing: float) -> dict:
    gap = concavity_gap(xy, z)
    deep = gap > 0.5
    return {"arch_depth": float(gap.max()) if len(gap) else 0.0,
            "concave_volume": float(gap[deep].sum() * spacing * spacing),
            "concave_area": float(deep.sum() * spacing * spacing)}


def _curvature(coef: np.ndarray, radius: float, nb: _Neighborhood):
    """Curvatura media y máxima (en valor absoluto) a partir del Hessiano promediado."""
    r2 = radius * radius
    hess = np.stack([2 * coef[:, 3] / r2, coef[:, 4] / r2, 2 * coef[:, 5] / r2], axis=1)
    hess = nb.average(hess)
    g2 = (coef[:, 1] ** 2 + coef[:, 2] ** 2) / r2
    den = (1.0 + g2) ** 1.5
    tr = hess[:, 0] + hess[:, 2]
    dif = np.sqrt((0.5 * (hess[:, 0] - hess[:, 2])) ** 2 + hess[:, 1] ** 2)
    k1 = (0.5 * tr + dif) / den
    k2 = (0.5 * tr - dif) / den
    return 0.5 * (k1 + k2), np.maximum(np.abs(k1), np.abs(k2))


def smooth_surface(mesh: Mesh, params: SmoothParams | None = None) -> dict:
    """Suaviza una superficie tipo campo de alturas con el método indicado en los parámetros."""
    p = params or SmoothParams()
    if p.method == "local":
        return adaptive_smooth(mesh, p)
    return multiscale_smooth(mesh, p)


def adaptive_smooth(mesh: Mesh, params: SmoothParams | None = None) -> dict:
    """Método "local": regresión cuadrática local con intervención según el ruido."""
    p = params or SmoothParams()
    v = mesh.vertices
    xy = np.ascontiguousarray(v[:, :2])
    z0 = v[:, 2].copy()
    n = len(z0)
    warnings: list[str] = []
    nb = _Neighborhood(xy, max(p.radius, 1e-6))

    # ---- análisis: ruido local, curvatura coherente y concavidad
    coef = nb.fit(z0)
    e0 = z0 - coef[:, 0]
    noise0 = np.sqrt(nb.average(e0 * e0))
    f_noise = noise0 ** 2 / (noise0 ** 2 + p.noise_floor ** 2)
    h_mean0, k_abs = _curvature(coef, nb.radius, nb)
    kc = 6.0 * float(np.clip(p.curvature_preservation, 0, 1))
    g_curv = 1.0 / (1.0 + (kc * k_abs * nb.radius) ** 2)
    hull0 = upper_hull(xy, coef[:, 0])
    gap0 = concavity_gap(xy, coef[:, 0], hull0)
    a_arch = np.clip((gap0 - 0.5) / 1.5, 0.0, 1.0)
    a_arch = a_arch * a_arch * (3 - 2 * a_arch)
    prot = float(np.clip(p.arch_protection, 0, 1))
    alpha = float(np.clip(p.intensity, 0, 1)) * f_noise * g_curv * (1.0 - 0.5 * prot * a_arch)
    tol = max(p.max_deviation, 0.0) * (1.0 - 0.7 * prot * a_arch)

    # ---- iteraciones de ajuste robusto
    z = z0.copy()
    range_sigma = np.maximum(2.0 * noise0, 0.5 * p.noise_floor)
    for _ in range(max(int(p.iterations), 0)):
        c = nb.fit(z, range_sigma=range_sigma)
        z = z + alpha * (c[:, 0] - z)
        z = np.clip(z, z0 - tol, z0 + tol)

    # ---- protección del arco: se anula el desplazamiento de baja frecuencia en zonas cóncavas
    if prot > 0 and a_arch.any():
        low = z - z0
        for _ in range(3):
            low = nb.average(low)
        z = z - prot * a_arch * low
        z = np.clip(z, z0 - tol, z0 + tol)

    # ---- métricas
    coef1 = nb.fit(z)
    e1 = z - coef1[:, 0]
    noise1 = np.sqrt(nb.average(e1 * e1))
    h_mean1, _ = _curvature(coef1, nb.radius, nb)
    dz = z - z0
    am0 = arch_metrics(xy, z0, nb.spacing)
    am1 = arch_metrics(xy, z, nb.spacing)
    # Profundidad del arco: separación máxima (percentil 99,5) entre la superficie y el "puente"
    # que apoya en talón y antepié. Antes y después se miden contra la MISMA envolvente de
    # referencia, para que la cifra refleje un aplanamiento real y no la retirada de ruido.
    gsm0 = float(np.percentile(gap0, 99.5)) if len(gap0) else 0.0
    gsm1 = float(np.percentile(concavity_gap(xy, coef1[:, 0], hull0), 99.5)) if hull0 is not None else 0.0
    curv_rms0 = float(np.sqrt(np.mean(h_mean0 ** 2)))
    curv_change = float(np.sqrt(np.mean((h_mean1 - h_mean0) ** 2)) / max(curv_rms0, 1e-12))
    info = {
        "params": asdict(p),
        "vertices": int(n),
        "neighbors": int(nb.k),
        "radius_effective": nb.radius_eff,
        "noise_before_median": float(np.median(noise0)),
        "noise_before_p95": float(np.percentile(noise0, 95)),
        "noise_after_median": float(np.median(noise1)),
        "noise_after_p95": float(np.percentile(noise1, 95)),
        "displacement_mean": float(np.abs(dz).mean()),
        "displacement_rms": float(np.sqrt((dz ** 2).mean())),
        "displacement_max": float(np.abs(dz).max()),
        "displacement_bias": float(dz.mean()),
        "intervention_mean": float(alpha.mean()),
        "protected_area_fraction": float((a_arch > 0.5).mean()),
        "arch_depth_before": gsm0,
        "arch_depth_after": gsm1,
        "arch_depth_change": gsm1 - gsm0,
        "concave_volume_before": am0["concave_volume"],
        "concave_volume_after": am1["concave_volume"],
        "mean_curvature_rms": curv_rms0,
        "curvature_relative_change": curv_change,
        "warnings": warnings,
    }
    if gsm0 > 1.0 and (gsm0 - gsm1) > max(0.15, 0.03 * gsm0):
        warnings.append(f"La profundidad del arco ha disminuido {gsm0 - gsm1:.2f} mm: reduzca la intensidad "
                        "o el radio, o aumente la protección del arco.")
    out = Mesh(np.column_stack([xy, z]), mesh.faces.copy())
    return {"mesh": out, "info": info,
            "fields": {"displacement": dz, "noise": noise0, "intervention": alpha, "concavity": a_arch}}


# ====================================================================== método multiescala
# Matrices simétricas definidas positivas: esta ordenación reduce el relleno de la factorización.
_LU = dict(permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options=dict(SymmetricMode=True))


class PlateSpline:
    """Suavizado por *spline* de placa delgada sobre la malla (en la proyección XY).

    Minimiza  Σ w·área·(f − z)²  +  λ·∫(Δf)²  con λ = (corte / 2π)⁴: los detalles de longitud
    de onda menor que ``corte`` se atenúan con una pendiente de cuarto orden y los mayores
    pasan casi intactos. El Laplaciano es el de cotangentes (exacto para funciones lineales
    en cualquier triangulación). Las filas del contorno no se penalizan (borde libre, sin
    aplanar la pendiente de salida) y se añade una penalización de curvatura a lo largo del
    propio contorno. Las factorizaciones se guardan por valor de corte.
    """

    def __init__(self, mesh: Mesh):
        v = mesh.vertices[:, :2]
        f = mesh.faces
        n = mesh.n_vertices
        self.n = n
        p = v[f]
        a2 = np.abs(cross2(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]))
        rows, cols, vals = [], [], []
        area = np.zeros(n)
        for k in range(3):
            i, j, o = f[:, (k + 1) % 3], f[:, (k + 2) % 3], f[:, k]
            w = 0.5 * ((v[i] - v[o]) * (v[j] - v[o])).sum(axis=1) / np.maximum(a2, 1e-12)
            rows += [i, j, i, j]
            cols += [j, i, i, j]
            vals += [-w, -w, w, w]
            np.add.at(area, o, a2 / 6.0)
        self.area = np.maximum(area, 1e-12)
        self.K = sparse.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n))
        loops = boundary_loops(mesh)
        self.loops = loops
        self.xy = v
        self.boundary = np.zeros(n, dtype=bool)
        r, br, bc, bv = 0, [], [], []
        seg = []
        for loop in loops:
            self.boundary[loop] = True
            m = len(loop)
            prev, nxt = np.roll(loop, 1), np.roll(loop, -1)
            idx = r + np.arange(m)
            br += [idx, idx, idx]
            bc += [prev, loop, nxt]
            bv += [np.ones(m), -2.0 * np.ones(m), np.ones(m)]
            seg.append(np.linalg.norm(v[nxt] - v[loop], axis=1))
            r += m
        interior = sparse.diags((~self.boundary) / self.area)
        self.P = (self.K.T @ interior @ self.K).tocsc()
        if r:
            d = sparse.csr_matrix((np.concatenate(bv), (np.concatenate(br), np.concatenate(bc))), shape=(r, n))
            self.Pb = (d.T @ d).tocsc()
            self.hb = float(np.median(np.concatenate(seg)))
        else:
            self.Pb = sparse.csc_matrix((n, n))
            self.hb = 1.0
        self._lu: dict = {}
        self.graph = (self.K != 0).tocsr()

    def _matrix(self, cutoff: float, w: np.ndarray):
        lam = (cutoff / (2.0 * np.pi)) ** 4
        return (sparse.diags(w) + lam * self.P + (lam / self.hb ** 2) * self.Pb).tocsc()

    def solve(self, z: np.ndarray, cutoff: float, weights: np.ndarray | None = None) -> np.ndarray:
        """Paso bajo de ``z`` con longitud de onda de corte ``cutoff`` (mm)."""
        if weights is not None:
            w = np.maximum(weights, 1e-6) * self.area
            return splu(self._matrix(cutoff, w), **_LU).solve(w * z)
        key = round(float(cutoff), 6)
        if key not in self._lu:
            self._lu[key] = splu(self._matrix(cutoff, self.area), **_LU)
        return self._lu[key].solve(self.area * z)

    def drop(self, cutoff: float) -> None:
        """Libera la factorización de un corte que ya no se va a usar."""
        self._lu.pop(round(float(cutoff), 6), None)

    def laplacian(self, z: np.ndarray) -> np.ndarray:
        return -(self.K @ z) / self.area


class _Rim:
    """Franja junto al contorno, en coordenadas (s = posición a lo largo del borde, d = distancia).

    Un spline de borde libre anula la curvatura en el contorno y por eso endereza la caída del
    canto del pie. Para no deformarlo sin dejar grano, en esta franja las bandas no se eliminan:
    se promedian *a lo largo* del contorno (misma distancia al borde, posiciones vecinas). El
    perfil del canto, que es casi constante a lo largo del borde, se conserva; los bultos, que
    varían a lo largo de él, desaparecen.
    """

    def __init__(self, sp: "PlateSpline", width: float):
        n = sp.n
        self.dist = np.full(n, np.inf)
        self.idx = np.zeros(0, dtype=np.int64)
        if not sp.loops:
            return
        pts, sval, offs = [], [], []
        off = 0.0
        self.period = []
        for loop in sp.loops:
            q = sp.xy[loop]
            seg = np.linalg.norm(np.roll(q, -1, axis=0) - q, axis=1)
            sv = np.concatenate([[0.0], np.cumsum(seg)[:-1]])
            pts.append(q)
            sval.append(sv + off)
            offs.append((off, float(seg.sum())))
            off += float(seg.sum()) + 1e4          # bucles distintos nunca son vecinos
        pts, sval = np.vstack(pts), np.concatenate(sval)
        d, j = cKDTree(pts).query(sp.xy)
        self.dist = d
        self.idx = np.flatnonzero(d <= width)
        self.s = sval[j[self.idx]]
        self.d = d[self.idx]
        # periodo del bucle de cada vértice (para cerrar el contorno)
        starts = np.array([o for o, _ in offs])
        lens = np.array([length for _, length in offs])
        which = np.searchsorted(starts, self.s, side="right") - 1
        self.per = lens[which]
        self.spacing = float(np.sqrt(np.median(sp.area)))

    def tangential(self, values: np.ndarray, length: float) -> np.ndarray:
        """Media de ``values`` (definidos en la franja) a lo largo del contorno, con ventana ``length``."""
        m = len(self.idx)
        if m == 0:
            return values
        delta = max(1.2 * self.spacing, 0.8)
        scale = length / delta
        base = np.column_stack([self.s, self.d * scale])
        # copias desplazadas un periodo para que la ventana cruce el inicio del bucle
        src = np.vstack([base, base + np.column_stack([self.per, np.zeros(m)]),
                         base - np.column_stack([self.per, np.zeros(m)])])
        vals = np.concatenate([values, values, values])
        k = int(min(len(src), max(16, 2.4 * length * 4.0 * delta / self.spacing ** 2), 160))
        dd, jj = cKDTree(src).query(base, k=k, distance_upper_bound=1.5 * length)
        ok = jj < len(src)
        w = np.exp(-2.0 * (np.where(ok, dd, 0.0) / length) ** 2) * ok
        return (w * vals[np.where(ok, jj, 0)]).sum(axis=1) / np.maximum(w.sum(axis=1), 1e-300)


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _ladder(p: SmoothParams) -> list[float]:
    lo = max(float(p.min_scale), 0.5)
    top = max(float(p.texture_scale), lo)
    out = [lo]
    while out[-1] * 1.45 < top * 0.92:
        out.append(out[-1] * 1.45)
    if top > lo * 1.05:
        out.append(top)
    return out


def _significant(sp: PlateSpline, ratio: np.ndarray, t_lo: float, t_hi: float, scale: float) -> np.ndarray:
    """Peso 0..1 de "relieve real" a partir de la energía local relativa de una banda.

    Umbral con histéresis: una zona solo se conserva si contiene un núcleo claramente
    significativo y extenso. Así los hoyuelos y granos aislados, por intensos que sean, no se
    confunden con relieve anatómico.
    """
    w = _smoothstep((ratio - t_lo) / (t_hi - t_lo))
    strong = w > 0.5
    if not strong.any():
        return np.zeros_like(w)
    g = sp.graph
    idx = np.flatnonzero(strong)
    _, lab = csgraph.connected_components(g[idx][:, idx], directed=False)
    area = np.bincount(lab, weights=sp.area[idx])
    good = area >= (1.5 * scale) ** 2
    seed = np.zeros(sp.n, dtype=bool)
    seed[idx[good[lab]]] = True
    if not seed.any():
        return np.zeros_like(w)
    support = np.flatnonzero(w > 0.02)
    _, lab2 = csgraph.connected_components(g[support][:, support], directed=False)
    has_seed = np.zeros(lab2.max() + 1, dtype=bool)
    has_seed[lab2[seed[support]]] = True
    out = np.zeros_like(w)
    keep = support[has_seed[lab2]]
    out[keep] = w[keep]
    return out


def multiscale_smooth(mesh: Mesh, params: SmoothParams | None = None) -> dict:
    """Método "multiescala": elimina la textura de pequeña amplitud y conserva el relieve real."""
    p = params or SmoothParams()
    v = mesh.vertices
    xy = np.ascontiguousarray(v[:, :2])
    z0 = v[:, 2].copy()
    n = len(z0)
    warnings: list[str] = []
    sp = PlateSpline(mesh)
    ladder = _ladder(p)
    prot = float(np.clip(p.detail_protection, 0.0, 1.0))
    t_lo = 2.5 - 2.0 * prot
    t_hi = 2.0 * t_lo
    strength = float(np.clip(p.intensity, 0.0, 1.0))
    gain = 3.0 ** (2.0 * strength - 1.0)

    # ---- nivel 0: se elimina todo lo menor que min_scale, con pesos robustos (puntos aislados)
    f0 = sp.solve(z0, ladder[0])
    for _ in range(2):
        r = z0 - f0
        s = 1.4826 * float(np.median(np.abs(r - np.median(r)))) + 1e-9
        f0 = sp.solve(z0, ladder[0], 1.0 / (1.0 + (r / (2.5 * s)) ** 2))
    fine_level = float(np.sqrt(np.median((z0 - f0) ** 2)))

    # ---- descomposición en bandas y nivel típico de cada una en ESTE escaneo
    # (cada factorización se usa para todo lo que la necesita y se libera, salvo las dos que
    # sirven después para medir la rugosidad: así la memoria no crece con el número de bandas)
    c_hp = ladder[1] if len(ladder) > 1 else 6.0
    c_en = ladder[3] if len(ladder) > 3 else max(12.0, 2.0 * c_hp)
    levels = [f0]
    bands = []
    for k in range(1, len(ladder)):
        c = ladder[k]
        levels.append(sp.solve(levels[-1], c))
        d = levels[k - 1] - levels[k]
        e = np.sqrt(np.maximum(sp.solve(d * d, c), 0.0))
        bands.append({"from": ladder[k - 1], "to": c, "d": d, "e": e, "level": float(np.median(e)) + 1e-12})
        if c not in (c_hp, c_en):
            sp.drop(c)

    # ---- modelo de textura del escaneo. La forma anatómica apenas tiene energía en las bandas
    # finas, así que lo que se mide ahí es la textura (ruido del sensor, tejido, arrugas). Se
    # caracteriza por su pendiente típica (amplitud / tamaño) y se supone que, a tamaños mayores,
    # la textura no supera esa pendiente: lo que la excede en una banda gruesa es forma. Un
    # escaneo rugoso (calcetín, mucho ruido) da una pendiente alta y se alisa más y a mayor
    # escala; uno limpio da una pendiente baja y casi no se toca.
    fine = [b for b in bands if b["to"] <= 9.5] or bands[:1]
    slope = max((b["level"] / b["to"] for b in fine), default=0.0)
    for b in bands:
        b["texture"] = min(b["level"], gain * slope * b["to"])
    top = len(bands)
    used = bands
    base = levels[top]

    # ---- síntesis: base + bandas solo donde hay relieve significativo
    rim = _Rim(sp, 0.5 * ladder[-1])
    z = base.copy()
    kept_any = np.zeros(n)
    removed = np.zeros(n)
    # De la banda más gruesa a la más fina. El relieve real (un pliegue, el borde de un dedo)
    # tiene energía en todas las escalas: donde la banda superior es relieve, la siguiente
    # necesita menos evidencia para conservarse. Los dedos quedan nítidos sin que el grano de
    # esas zonas, que sigue estando al nivel de la textura, se conserve con ellos.
    above = np.zeros(n)
    for b in used[::-1]:
        boost = above if b["to"] > 9.5 else 0.0      # las bandas más finas son sobre todo grano
        w = _significant(sp, (1.0 + boost) * b["e"] / b["texture"], t_lo, t_hi, b["to"])
        above = w
        b["w"] = w
        c = b["to"]
        contrib = w * b["d"]
        fac = float(np.clip((c - 6.0) / 6.0, 0.0, 1.0))
        if fac > 0 and len(rim.idx):
            r = rim.idx
            rw = fac * (1.0 - _smoothstep((rim.d - 0.15 * c) / (0.35 * c)))
            along = rim.tangential(b["d"][r], c)
            contrib[r] = contrib[r] + rw * (1.0 - w[r]) * along
        z += contrib
        kept_any = np.maximum(kept_any, w)
        removed += 1.0 - w
    removed = removed / max(len(used), 1)

    # ---- tolerancia de desviación. Se mide respecto a la superficie sin grano (nivel 0): quitar
    # un punto aislado de varios milímetros no es "desviarse" de la forma del pie.
    limited = 0
    if strength <= 0:
        z = z0.copy()
    elif p.max_deviation > 0:
        dv = z - f0
        x = np.abs(dv) / p.max_deviation
        limited = int((x > 1.0).sum())
        a = 0.6      # límite suave: intacto hasta el 60 % de la tolerancia, saturación progresiva después
        x = np.where(x <= a, x, a + (1.0 - a) * np.tanh((x - a) / (1.0 - a)))
        z = f0 + np.sign(dv) * x * p.max_deviation
    dz = z - z0

    # ---- métricas (misma estructura que el método local)
    def roughness(zz):
        hp = zz - sp.solve(zz, c_hp)
        return np.sqrt(np.maximum(sp.solve(hp * hp, c_en), 0.0))

    rough0, rough1 = roughness(z0), roughness(z)
    ref0, ref1 = sp.solve(z0, c_en), sp.solve(z, c_en)
    hull0 = upper_hull(xy, ref0)
    gap0 = concavity_gap(xy, ref0, hull0)
    gap1 = concavity_gap(xy, ref1, hull0) if hull0 is not None else gap0
    gsm0 = float(np.percentile(gap0, 99.5)) if n else 0.0
    gsm1 = float(np.percentile(gap1, 99.5)) if n else 0.0
    inner = ~sp.boundary
    h0, h1 = 0.5 * sp.laplacian(ref0)[inner], 0.5 * sp.laplacian(ref1)[inner]
    curv_rms0 = float(np.sqrt(np.mean(h0 ** 2))) if inner.any() else 0.0
    curv_change = float(np.sqrt(np.mean((h1 - h0) ** 2)) / max(curv_rms0, 1e-12)) if inner.any() else 0.0
    am0 = {"concave_volume": float((gap0 * sp.area)[gap0 > 0.5].sum())}
    am1 = {"concave_volume": float((gap1 * sp.area)[gap1 > 0.5].sum())}
    table = [{"from": b["from"], "to": b["to"], "level": b["level"], "texture": b["texture"],
              "kept_pct": float(100.0 * b["w"].mean())} for b in bands]
    texture = float(np.sqrt(sum(b["level"] ** 2 for b in fine)))
    if p.max_deviation > 0 and limited > 0.02 * n:
        warnings.append(f"La tolerancia de desviación ({p.max_deviation:g} mm) ha limitado el alisado en "
                        f"{100.0 * limited / n:.1f}% de los vértices: auméntela o póngala a 0 para un alisado completo.")
    if gsm0 > 1.0 and (gsm0 - gsm1) > max(0.3, 0.05 * gsm0):
        warnings.append(f"La profundidad del arco ha disminuido {gsm0 - gsm1:.2f} mm: reduzca el tamaño "
                        "máximo de textura.")
    info = {
        "params": asdict(p),
        "method": "multiescala",
        "vertices": int(n),
        "texture_scale": float(ladder[-1]),
        "texture_level": texture,
        "texture_slope": float(slope),
        "fine_level": fine_level,
        "bands": table,
        "detail_area_pct": float(100.0 * (kept_any > 0.5).mean()),
        "limited_vertices": limited,
        "noise_before_median": float(np.median(rough0)),
        "noise_before_p95": float(np.percentile(rough0, 95)),
        "noise_after_median": float(np.median(rough1)),
        "noise_after_p95": float(np.percentile(rough1, 95)),
        "displacement_mean": float(np.abs(dz).mean()),
        "displacement_rms": float(np.sqrt((dz ** 2).mean())),
        "displacement_max": float(np.abs(dz).max()),
        "displacement_bias": float(dz.mean()),
        "intervention_mean": float(removed.mean()),
        "protected_area_fraction": float((kept_any > 0.5).mean()),
        "arch_depth_before": gsm0,
        "arch_depth_after": gsm1,
        "arch_depth_change": gsm1 - gsm0,
        "concave_volume_before": am0["concave_volume"],
        "concave_volume_after": am1["concave_volume"],
        "mean_curvature_rms": curv_rms0,
        "curvature_relative_change": curv_change,
        "warnings": warnings,
    }
    out = Mesh(np.column_stack([xy, z]), mesh.faces.copy())
    return {"mesh": out, "info": info,
            "fields": {"displacement": dz, "noise": rough0, "intervention": removed, "concavity": kept_any}}
