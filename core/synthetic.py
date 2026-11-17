"""Generador de escaneos plantares sintéticos con verdad-terreno analítica.

El modelo es una "suela" (planta + paredes laterales que bajan, como en un escaneo
real en el que el pie continúa hacia el empeine) con arco, almohadillas de los dedos
y surco metatarsal. Sobre él se añaden defectos controlados: ruido, fragmentos,
geometría conectada, capas superpuestas, agujeros y zonas incompletas.

Marco propio del modelo: X = longitud (talón→dedos), Y = anchura (medial +), Z = normal
exterior de la planta. Unidades: mm.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.interpolate import PchipInterpolator

from .mesh import Mesh, merge_meshes

L_FOOT = 250.0
H_WALL = 40.0
N_SUPER = 4.0

_XS = np.array([0.0, 0.05, 0.15, 0.30, 0.50, 0.70, 0.80, 0.90, 0.97, 1.0])
_W_MED = PchipInterpolator(_XS, [18, 26, 31, 31, 30, 44, 46, 41, 28, 14])
_W_LAT = PchipInterpolator(_XS, [18, 26, 32, 34, 38, 46, 44, 35, 22, 10])


def _taper(xh):
    return np.sqrt(np.clip(1.0 - np.abs(2.0 * xh - 1.0) ** 6, 0.0, 1.0))


def _relief(xh, u, arch_depth: float = 16.0, midfoot_lift: float = 0.0):
    """Relieve anatómico (mm) que se resta/suma a la altura base: arco, dedos y surco."""
    bx = np.where((xh > 0.22) & (xh < 0.72), np.sin(np.pi * (xh - 0.22) / 0.5) ** 2, 0.0)
    s = np.clip((u + 0.35) / 1.35, 0.0, 1.0)
    bu = (s * s * (3 - 2 * s)) ** 1.5
    arch = arch_depth * bx * bu
    toes = np.zeros_like(xh)
    for uc, amp in zip((-0.68, -0.36, -0.04, 0.28, 0.62), (1.8, 2.0, 2.2, 2.4, 3.0)):
        toes += amp * np.exp(-((xh - 0.93) / 0.028) ** 2 - ((u - uc) / 0.13) ** 2)
    sulcus = 2.2 * np.exp(-((xh - 0.855) / 0.018) ** 2)
    # perfil sagital: el mediopié (también el lateral) queda algo más alto que talón y antepié
    lift = midfoot_lift * np.where((xh > 0.12) & (xh < 0.80), np.sin(np.pi * (xh - 0.12) / 0.68) ** 2, 0.0)
    return -arch + toes - sulcus - lift


def _surface(xh, u_signed, zfac, arch_depth: float = 16.0, midfoot_lift: float = 0.0, toe_flatten: float = 0.0):
    w = np.where(u_signed >= 0, _W_MED(xh), _W_LAT(xh)) * _taper(xh)
    x = xh * L_FOOT
    y = 6.0 * np.sin(np.pi * xh) + u_signed * w
    tp = _taper(xh)
    if toe_flatten:          # pulpejos de los dedos casi a la altura del antepié (sin punta curvada)
        r = np.clip((xh - 0.72) / 0.18, 0.0, 1.0)
        tp = tp + toe_flatten * (r * r * (3 - 2 * r)) * (1.0 - tp)
    z = (H_WALL * tp + _relief(xh, u_signed, arch_depth, midfoot_lift)) * zfac
    return x, y, z


def true_height(x, y):
    """Altura analítica de la planta en (x, y) del marco del modelo (NaN fuera)."""
    xh = np.clip(np.asarray(x, float) / L_FOOT, 1e-6, 1 - 1e-6)
    yc = np.asarray(y, float) - 6.0 * np.sin(np.pi * xh)
    tp = _taper(xh)
    w = np.where(yc >= 0, _W_MED(xh), _W_LAT(xh)) * tp
    u = yc / np.maximum(w, 1e-9)
    inside = np.abs(u) < 1.0
    uc = np.clip(u, -1, 1)
    zfac = (1.0 - np.abs(uc) ** N_SUPER) ** (1.0 / N_SUPER)
    z = (H_WALL * tp + _relief(xh, uc)) * zfac
    return np.where(inside, z, np.nan)


def base_scan(ns: int = 300, nt: int = 150, closed: bool = False, arch_depth: float = 16.0,
              midfoot_lift: float = 0.0, toe_flatten: float = 0.0) -> Mesh:
    """Planta + paredes laterales (y empeine si ``closed``)."""
    s = np.linspace(0.004, 0.996, ns)
    xh = 0.5 * (1 - np.cos(np.pi * s)) * 0.992 + 0.004
    t_end = 2 * np.pi if closed else np.pi
    nt_eff = nt * 2 if closed else nt
    # Muestreo uniforme en longitud de arco de la sección (superelipse): con t uniforme los
    # triángulos del centro de la planta medirían varios milímetros de ancho.
    tf = np.linspace(0.0, t_end, 8001)
    cu = np.sign(np.cos(tf)) * np.abs(np.cos(tf)) ** (2.0 / N_SUPER) * 36.0
    cz = np.sign(np.sin(tf)) * np.abs(np.sin(tf)) ** (2.0 / N_SUPER) * H_WALL
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(cu), np.diff(cz)))])
    t = np.interp(np.linspace(0.0, arc[-1], nt_eff, endpoint=not closed), arc, tf)
    xx, tt = np.meshgrid(xh, t, indexing="ij")
    ct, st = np.cos(tt), np.sin(tt)
    u = np.sign(ct) * np.abs(ct) ** (2.0 / N_SUPER)
    zf = np.sign(st) * np.abs(st) ** (2.0 / N_SUPER)
    x, y, z = _surface(xx, u, np.maximum(zf, 0), arch_depth, midfoot_lift, toe_flatten)
    if closed:
        z = np.where(zf < 0, zf * 45.0 * _taper(xx), z)
    v = np.stack([x.ravel(), y.ravel(), z.ravel()], axis=1)
    idx = np.arange(ns * nt_eff).reshape(ns, nt_eff)
    if closed:
        a, b = idx[:-1, :], idx[1:, :]
        c, d = np.roll(idx, -1, axis=1)[1:, :], np.roll(idx, -1, axis=1)[:-1, :]
    else:
        a, b, c, d = idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]
    f = np.concatenate([np.stack([a.ravel(), c.ravel(), b.ravel()], axis=1),
                        np.stack([a.ravel(), d.ravel(), c.ravel()], axis=1)])
    m = Mesh(v, f)
    n, _ = m.face_normals()
    top = n[:, 2][np.argmax(m.centroids[:, 2])]
    return m if top > 0 else m.flipped()


# ------------------------------------------------------------------ defectos
def add_noise(mesh: Mesh, sigma: float, rng, forefoot_factor: float = 1.0) -> Mesh:
    vn = mesh.vertex_normals()
    amp = sigma * np.where(mesh.vertices[:, 0] > 0.55 * L_FOOT, forefoot_factor, 1.0)
    return Mesh(mesh.vertices + vn * (rng.standard_normal(mesh.n_vertices) * amp)[:, None], mesh.faces)


def _blob(center, radius, rng, n=10) -> Mesh:
    """Pequeño parche curvo aislado (fragmento flotante)."""
    u = np.linspace(-1, 1, n)
    uu, vv = np.meshgrid(u, u, indexing="ij")
    p = np.stack([uu.ravel(), vv.ravel(), 0.3 * (1 - uu.ravel() ** 2 - vv.ravel() ** 2)], axis=1) * radius
    q, _ = np.linalg.qr(rng.standard_normal((3, 3)))
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]
    f = np.concatenate([np.stack([a.ravel(), b.ravel(), c.ravel()], axis=1),
                        np.stack([a.ravel(), c.ravel(), d.ravel()], axis=1)])
    return Mesh(p @ q.T + center, f)


def add_fragments(mesh: Mesh, rng, count: int = 14) -> Mesh:
    parts = [mesh]
    for k in range(count):
        c = np.array([rng.uniform(-20, L_FOOT + 20), rng.uniform(-70, 70), rng.uniform(-25, 60)])
        if k < 3:   # algunos justo delante de la planta (ocluyen la vista)
            c = np.array([rng.uniform(60, 190), rng.uniform(-20, 20), H_WALL + rng.uniform(4, 12)])
        parts.append(_blob(c, rng.uniform(2.5, 9.0), rng))
    return merge_meshes(parts)


def add_brim(mesh: Mesh, width: float = 35.0, rings: int = 14) -> Mesh:
    """Ala horizontal conectada al borde inferior de las paredes (soporte/mesa conectada)."""
    from .mesh import boundary_loops
    loops = boundary_loops(mesh)
    if not loops:
        return mesh
    loop = loops[0]
    p = mesh.vertices[loop]
    c = p[:, :2].mean(axis=0)
    d = p[:, :2] - c
    d /= np.maximum(np.linalg.norm(d, axis=1), 1e-9)[:, None]
    verts = [mesh.vertices]
    faces = [mesh.faces]
    prev = loop
    base = mesh.n_vertices
    n = len(loop)
    for r in range(1, rings + 1):
        q = p.copy()
        q[:, :2] += d * width * r / rings
        q[:, 2] = p[:, 2].min()
        verts.append(q)
        cur = base + np.arange(n)
        a, b = prev, np.roll(prev, -1)
        c2, d2 = np.roll(cur, -1), cur
        # el bucle de borde sigue el sentido de la cara interior: se invierte para coser
        faces.append(np.stack([b, a, d2], axis=1))
        faces.append(np.stack([b, d2, c2], axis=1))
        prev = cur
        base += n
    return Mesh(np.vstack(verts), np.vstack(faces))


def _patch_faces(mesh: Mesh, center_xy, radius) -> np.ndarray:
    c = mesh.centroids
    n, _ = mesh.face_normals()
    return (np.linalg.norm(c[:, :2] - np.asarray(center_xy), axis=1) < radius) & (n[:, 2] > 0.6)


def add_ghost_layer(mesh: Mesh, center_xy, radius, offset, rng) -> Mesh:
    """Capa duplicada flotante (error de registro) desplazada ``offset`` mm."""
    sub = mesh.submesh(_patch_faces(mesh, center_xy, radius))
    v = sub.vertices + np.asarray(offset, float)
    v[:, 2] += 0.15 * rng.standard_normal(len(v))
    return merge_meshes([mesh, Mesh(v, sub.faces)])


def add_hinged_flap(mesh: Mesh, x_range, u_half: float, peel: float) -> Mesh:
    """Solapa duplicada CONECTADA a la planta por una línea de bisagra (aristas no variedad)."""
    c = mesh.centroids
    n, _ = mesh.face_normals()
    sel = (c[:, 0] > x_range[0]) & (c[:, 0] < x_range[1]) & (np.abs(c[:, 1]) < u_half) & (n[:, 2] > 0.6)
    pf = mesh.faces[sel]
    used = np.unique(pf)
    vx = mesh.vertices[used, 0]
    # Bisagra = las dos primeras columnas de vértices: así la solapa comparte ARISTAS con la
    # planta (aristas con 3 caras) y además deja algunas caras exactamente duplicadas.
    ux = np.unique(np.round(vx, 6))
    hinge = vx <= ux[min(1, len(ux) - 1)] + 1e-6
    new_id = np.full(mesh.n_vertices, -1, dtype=np.int64)
    dup = used[~hinge]
    new_id[dup] = mesh.n_vertices + np.arange(len(dup))
    new_id[used[hinge]] = used[hinge]
    nv = mesh.vertices[dup].copy()
    nv[:, 2] += peel * (nv[:, 0] - vx.min()) / max(x_range[1] - x_range[0], 1e-9)
    nv[:, 0] += 1.0
    return Mesh(np.vstack([mesh.vertices, nv]), np.vstack([mesh.faces, new_id[pf]]))


def punch_holes(mesh: Mesh, holes) -> Mesh:
    c = mesh.centroids
    keep = np.ones(mesh.n_faces, dtype=bool)
    for (x, y, r) in holes:
        keep &= np.linalg.norm(c[:, :2] - np.array([x, y]), axis=1) > r
    return mesh.submesh(keep)


def make_incomplete(mesh: Mesh, rng) -> Mesh:
    """Elimina una esquina del antepié y muerde el borde de forma irregular."""
    c = mesh.centroids
    keep = ~((c[:, 0] > 0.78 * L_FOOT) & (c[:, 1] < -18))
    keep &= ~((c[:, 0] < 0.10 * L_FOOT) & (c[:, 1] > 8))
    for _ in range(10):
        ang = rng.uniform(0, 2 * np.pi)
        p = np.array([125 + 115 * np.cos(ang), 45 * np.sin(ang)])
        keep &= np.linalg.norm(c[:, :2] - p, axis=1) > rng.uniform(4, 9)
    return mesh.submesh(keep)


def random_rigid(mesh: Mesh, rng, scale: float = 1.0):
    q, _ = np.linalg.qr(rng.standard_normal((3, 3)))
    if np.linalg.det(q) < 0:
        q[:, 0] *= -1
    t = rng.uniform(-200, 200, size=3)
    return Mesh((mesh.vertices @ q.T + t) * scale, mesh.faces), q, t


def unweld(mesh: Mesh) -> Mesh:
    """Convierte a "sopa de triángulos" como en un STL real."""
    return Mesh(mesh.vertices[mesh.faces].reshape(-1, 3), np.arange(3 * mesh.n_faces).reshape(-1, 3))


@dataclass
class Scenario:
    name: str
    description: str
    mesh: Mesh
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))
    translation: np.ndarray = field(default_factory=lambda: np.zeros(3))
    scale: float = 1.0
    noise_sigma: float = 0.0

    def to_model_frame(self, points_mm: np.ndarray) -> np.ndarray:
        """Convierte puntos (en mm, marco del archivo) al marco analítico del modelo."""
        return (points_mm - self.translation) @ self.rotation

    def height_error(self, points_mm: np.ndarray) -> np.ndarray:
        """Error en Z respecto a la planta analítica (NaN fuera de la huella)."""
        p = self.to_model_frame(points_mm)
        return p[:, 2] - true_height(p[:, 0], p[:, 1])


SCENARIOS = ("limpio", "fragmentos", "conectado", "superpuesto", "ruido", "agujeros", "incompleto", "completo")


def make_scenario(name: str, seed: int = 0, ns: int = 300, nt: int = 150) -> Scenario:
    rng = np.random.default_rng(seed)
    m = base_scan(ns, nt)
    sigma = 0.05
    desc = {
        "limpio": "Planta con paredes laterales conectadas y ruido mínimo.",
        "fragmentos": "Fragmentos flotantes, algunos delante de la planta.",
        "conectado": "Ala horizontal conectada a las paredes y solapa conectada por bisagra.",
        "superpuesto": "Capas duplicadas por encima y por debajo de la planta, y solapa conectada.",
        "ruido": "Ruido fuerte y no uniforme (más en el antepié).",
        "agujeros": "Agujeros en arco, talón y antepié.",
        "incompleto": "Antepié y talón incompletos con borde mordido.",
        "completo": "Todos los defectos a la vez, en metros y con orientación arbitraria.",
    }[name]
    scale = 1.0
    if name in ("conectado", "completo"):
        m = add_brim(m)
        m = add_hinged_flap(m, (40, 75), 14, -4.0)
    if name in ("superpuesto", "completo"):
        m = add_ghost_layer(m, (190, 5), 22, (1.5, 1.0, -1.6), rng)
        m = add_ghost_layer(m, (60, -4), 16, (-1.0, 0.8, 1.3), rng)
        m = add_hinged_flap(m, (120, 150), 12, 3.5)
    if name in ("agujeros", "completo"):
        m = punch_holes(m, [(120, 12, 7.0), (40, 0, 4.0), (195, -10, 3.0), (150, -20, 1.6)])
    if name in ("incompleto", "completo"):
        m = make_incomplete(m, rng)
    if name == "ruido":
        sigma = 0.30
        m = add_noise(m, sigma, rng, forefoot_factor=2.0)
    elif name == "completo":
        sigma = 0.20
        m = add_noise(m, sigma, rng, forefoot_factor=1.5)
        scale = 0.001
    else:
        m = add_noise(m, sigma, rng)
    if name in ("fragmentos", "completo"):
        m = add_fragments(m, rng)
    m, q, t = random_rigid(m, rng, scale)
    return Scenario(name, desc, unweld(m), q, t, scale, sigma)


def make_foot_pair(seed: int = 0, ns: int = 220, nt: int = 110, arch_left: float = 16.0,
                   arch_right: float = 22.0, sigma: float = 0.05, midfoot_lift: float = 9.0,
                   toe_flatten: float = 0.8):
    """Pareja sintética de escaneos (izquierdo, derecho) con arcos distintos.

    El modelo base es un pie izquierdo (medial hacia +Y con X hacia los dedos y Z hacia el
    escáner); el derecho se obtiene por simetría especular y tiene otro arco, de modo que
    los dos pies no son copias simétricas. Cada uno lleva su propia posición y orientación.
    """
    rng = np.random.default_rng(seed)
    kw = dict(midfoot_lift=midfoot_lift, toe_flatten=toe_flatten)
    left = add_noise(base_scan(ns, nt, arch_depth=arch_left, **kw), sigma, rng)
    right = add_noise(base_scan(ns, nt, arch_depth=arch_right, **kw), sigma, rng)
    right = Mesh(right.vertices * np.array([1.0, -1.0, 1.0]), right.faces[:, ::-1].copy())
    out = []
    for m in (left, right):
        m2, _, _ = random_rigid(m, rng)
        out.append(unweld(m2))
    return out[0], out[1]
