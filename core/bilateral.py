"""Simulación bilateral de bipedestación estática: dos pies, un suelo, un peso corporal.

Modelo físico
-------------
* Cada pie es un **sólido rígido** (esqueleto) recubierto de **tejido blando plantar** cuyo
  espesor y rigidez dependen de la región anatómica. La geometría de partida es la superficie
  plantar limpia y suavizada que produce el pipeline de un pie (sin carga).
* El suelo es un **plano rígido común** a los dos pies.
* El tejido se modela como una cimentación elástica no lineal (Winkler) con acoplamiento
  lateral: en cada punto, la compresión δ = w − h (aproximación del pie w menos la altura de la
  planta sobre el suelo) produce una presión
      p = (E0 / β) · (exp(β · δ / t) − 1)
  que se reparte lateralmente con un núcleo gaussiano (el tejido no trabaja por columnas
  aisladas). E0 es el módulo inicial, t el espesor del tejido y β la no linealidad
  (rigidización con la compresión, típica de la almohadilla del talón).
* **Equilibrio de fuerzas** (siempre, de forma exacta): F_izq + F_der = m · g, con el reparto
  izquierdo/derecho indicado por el usuario (50/50 por defecto, como hipótesis).
* **Equilibrio de momentos** (opcional): si se conoce la proyección horizontal del centro de
  masas, el centro de presiones global debe coincidir con ella. Entonces el reparto deja de ser
  una entrada: se resuelven conjuntamente la aproximación de cada pie y una inclinación sagital
  común del cuerpo (estrategia de tobillo), y el reparto resulta del equilibrio.

Unidades: mm, kPa, N.  kPa · mm² = 1e-3 N.
"""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy import ndimage
from scipy.optimize import brentq, least_squares

from .mesh import Mesh
from .raster import Grid, make_grid, rasterize
from .smoothing import upper_hull

G = 9.80665
SIDES = ("izquierdo", "derecho")

# Regiones anatómicas (máscara porcentual sobre el eje talón-dedos, como en baropodometría)
REGIONS = (
    ("talon_medial", "Talón medial"),
    ("talon_lateral", "Talón lateral"),
    ("mediopie", "Mediopié"),
    ("met1", "Antepié medial (1.er MTT)"),
    ("met23", "Antepié central (2.º–3.er MTT)"),
    ("met45", "Antepié lateral (4.º–5.º MTT)"),
    ("hallux", "Hallux"),
    ("dedos", "Dedos 2.º–5.º"),
)
REGION_KEYS = tuple(k for k, _ in REGIONS)
REGION_NAMES = dict(REGIONS)

# Propiedades del tejido plantar por región:
#  * thickness: espesor sin carga (mm). Ecografía: almohadilla del talón ~19–20 mm; bajo las
#    cabezas metatarsianas ~7–15 mm según el estudio y la cabeza; dedos más delgados.
#  * beta: no linealidad. La curva p(ε) se rigidiza hacia ε ≈ 1/β. En indentación in vivo el
#    tejido entra en régimen no lineal hacia el 12 % de su espesor en el talón y el 16 % bajo
#    la 2.ª cabeza metatarsiana: el talón se rigidiza antes (β ≈ 1/0,12) que el antepié (≈ 1/0,16).
#  * e0: módulo inicial (kPa). Valor de partida calibrado para reproducir el reparto clásico en
#    bipedestación (talón ~60 %, mediopié ~8 %, antepié ~28 %, dedos pocos %); no es una medida.
DEFAULT_TISSUE = {
    "talon_medial": {"thickness": 19.0, "e0": 35.0, "beta": 8.0},
    "talon_lateral": {"thickness": 19.0, "e0": 35.0, "beta": 8.0},
    "mediopie": {"thickness": 12.0, "e0": 30.0, "beta": 6.0},
    "met1": {"thickness": 10.0, "e0": 35.0, "beta": 6.0},
    "met23": {"thickness": 11.0, "e0": 35.0, "beta": 6.0},
    "met45": {"thickness": 9.0, "e0": 35.0, "beta": 6.0},
    "hallux": {"thickness": 8.0, "e0": 30.0, "beta": 6.0},
    "dedos": {"thickness": 7.0, "e0": 30.0, "beta": 6.0},
}


@dataclass
class StanceParams:
    """Colocación de los pies en el suelo (vista superior: X a la derecha, Y hacia delante)."""

    heel_separation: float = 170.0      # mm entre centros de talón (McIlroy y Maki, 1997)
    toe_out_left: float = 7.0           # grados de apertura del pie izquierdo (14° entre ejes)
    toe_out_right: float = 7.0
    advance_right: float = 0.0          # mm que el pie derecho está por delante del izquierdo
    load_point: float = 0.32            # posición AP de la carga para apoyar el pie (fracción desde el talón)
    min_gap: float = 8.0                # mm de separación mínima entre huellas
    swap: bool = False                  # intercambiar los escaneos izquierdo y derecho

    @classmethod
    def from_dict(cls, d: dict | None) -> "StanceParams":
        d = d or {}
        out = cls()
        for k in cls.__dataclass_fields__:
            if k in d and d[k] is not None:
                setattr(out, k, type(getattr(out, k))(d[k]))
        return out


@dataclass
class SimParams:
    mass: float = 70.0                  # kg
    split_left: float = 50.0            # % del peso sobre el pie izquierdo (hipótesis)
    mode: str = "reparto"               # "reparto" | "centro_masas"
    load_point: float = 0.32            # línea de carga de cada pie (fracción de la longitud desde el talón)
    arch_drop: float = 6.0              # mm: descenso del arco medial con la carga de bipedestación (caída del navicular)
    loaded_scan: bool = False           # el escaneo se tomó en carga (el arco ya descendió)
    toe_stiffness: float = 4000.0       # N·mm/rad: rigidez pasiva de la articulación metatarsofalángica (MTF)
    toe_flexor: float = 400.0           # N·mm por pie (con medio peso): tono de los flexores de los dedos
    com_x: float = 0.0                  # mm a la derecha del punto medio entre talones
    com_y_frac: float = 0.32            # posición AP del centro de masas (fracción de la longitud media del pie)
    nonlinearity_scale: float = 1.0     # factor sobre la β de cada región
    spread: float = 4.0                 # mm: acoplamiento lateral del tejido (σ del núcleo)
    contact_threshold: float = 2.0      # kPa: umbral de contacto (como el de un sensor)
    resolution: float = 1.0             # mm por celda
    tissue: dict = field(default_factory=dict)   # {"izquierdo": {region: {...}}, "derecho": {...}}
    stiffness_scale: dict = field(default_factory=lambda: {"izquierdo": 1.0, "derecho": 1.0})

    @classmethod
    def from_dict(cls, d: dict | None) -> "SimParams":
        d = d or {}
        out = cls()
        for k in ("mass", "split_left", "load_point", "com_x", "com_y_frac", "nonlinearity_scale", "spread",
                  "contact_threshold", "resolution", "arch_drop", "toe_stiffness", "toe_flexor"):
            if d.get(k) is not None:
                setattr(out, k, float(d[k]))
        if d.get("loaded_scan") is not None:
            out.loaded_scan = bool(d["loaded_scan"])
        if d.get("mode"):
            out.mode = str(d["mode"])
        if isinstance(d.get("tissue"), dict):
            out.tissue = d["tissue"]
        if isinstance(d.get("stiffness_scale"), dict):
            out.stiffness_scale.update({k: float(v) for k, v in d["stiffness_scale"].items() if k in SIDES})
        return out

    def tissue_for(self, side: str) -> dict:
        base = {k: dict(v) for k, v in DEFAULT_TISSUE.items()}
        for k, v in (self.tissue.get(side) or {}).items():
            if k in base and isinstance(v, dict):
                for q in ("thickness", "e0", "beta"):
                    if v.get(q) is not None and float(v[q]) > 0:
                        base[k][q] = float(v[q])
        s = float(self.stiffness_scale.get(side, 1.0))
        for k in base:
            base[k]["e0"] *= s
            base[k]["beta"] *= self.nonlinearity_scale
        return base


class BilateralError(RuntimeError):
    """Error de uso o de datos que debe mostrarse al usuario."""


# ============================================================================ geometría
def _rot_to_z(n: np.ndarray) -> np.ndarray:
    """Rotación que lleva el vector unitario ``n`` a +Z (Rodrigues)."""
    n = n / np.linalg.norm(n)
    z = np.array([0.0, 0.0, 1.0])
    k = np.cross(n, z)
    s = np.linalg.norm(k)
    c = float(n @ z)
    if s < 1e-12:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    k = k / s
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + s * kx + (1 - c) * kx @ kx


def _axis_analysis(xy: np.ndarray):
    """Eje largo (talón→dedos) por PCA y anchura; el antepié es más ancho que el talón."""
    c = xy.mean(axis=0)
    cov = np.cov((xy - c).T)
    w, vecs = np.linalg.eigh(cov)
    e1 = vecs[:, np.argmax(w)]
    e2 = np.array([-e1[1], e1[0]])
    s = (xy - c) @ e1
    q = (xy - c) @ e2
    lo, hi = float(s.min()), float(s.max())
    L = hi - lo
    nb = 40
    b = np.clip(((s - lo) / max(L, 1e-9) * nb).astype(int), 0, nb - 1)
    qmin = np.full(nb, np.inf)
    qmax = np.full(nb, -np.inf)
    np.minimum.at(qmin, b, q)
    np.maximum.at(qmax, b, q)
    width = np.where(np.isfinite(qmin), qmax - qmin, 0.0)
    pos = (np.arange(nb) + 0.5) / nb
    front = width[(pos > 0.60) & (pos < 0.80)].mean()
    back = width[(pos > 0.08) & (pos < 0.25)].mean()
    if front < back:          # el antepié está en el extremo negativo: se invierte el eje
        e1, e2 = -e1, -e2
        s, q = -s, -q
        lo, hi = -hi, -lo
    ratio = float(max(front, back) / max(min(front, back), 1e-9))
    return {"center": c, "e1": e1, "e2": e2, "s": s, "q": q, "s0": lo, "length": L, "width_ratio": ratio}


@dataclass
class FootGeometry:
    """Pie preparado para la simulación (marco "persona": X derecha, Y delante, Z arriba)."""

    declared_side: str
    detected_side: str
    side_confidence: str
    medial_sign: float                  # +1: medial hacia +X local; -1: hacia −X local
    length: float
    width: float
    sole_local: Mesh                    # superficie plantar: (x, y, altura sobre el suelo)
    solid_local: Mesh                   # sólido cerrado en el mismo marco
    n_sole_faces: int
    canonical_to_local: np.ndarray      # (4,4)
    ground_tilt_deg: float
    info: dict


def prepare_foot(sole_c: Mesh, solid_c: Mesh, declared_side: str, load_point: float = 0.32) -> FootGeometry:
    """Orienta un pie respecto al suelo y lo expresa en su marco local de bipedestación.

    ``sole_c`` y ``solid_c`` están en el marco canónico del pipeline (Z hacia el suelo).
    El suelo es el plano de la envolvente convexa de la planta sobre el que apoyaría un pie
    rígido cargado en ``load_point`` (fracción de la longitud desde el talón): normalmente el
    trípode talón – cabezas metatarsianas. No se asume que la planta sea plana.
    """
    v = sole_c.vertices
    if len(v) < 50:
        raise BilateralError("La superficie plantar es demasiado pequeña para simular.")
    ax = _axis_analysis(v[:, :2])
    # punto de carga: sobre el eje, en la línea media de la huella a esa altura
    s_load = ax["s0"] + load_point * ax["length"]
    band = np.abs(ax["s"] - s_load) < 0.03 * ax["length"]
    q_mid = 0.5 * (ax["q"][band].min() + ax["q"][band].max()) if band.any() else 0.0
    P = ax["center"] + ax["e1"] * s_load + ax["e2"] * q_mid
    planes = upper_hull(v[:, :2], v[:, 2])
    warnings = []
    if planes is None:
        n_g = np.array([0.0, 0.0, 1.0])
        warnings.append("No se pudo determinar el plano de apoyo; se usa la orientación del escaneo.")
    else:
        a, b, c = planes
        k = int(np.argmin(a * P[0] + b * P[1] + c))
        n_g = np.array([-a[k], -b[k], 1.0])
        n_g /= np.linalg.norm(n_g)
    Rg = _rot_to_z(n_g)
    tilt = float(np.degrees(np.arccos(np.clip(n_g[2], -1, 1))))
    vr = v @ Rg.T
    ground = float(vr[:, 2].max())

    ax = _axis_analysis(vr[:, :2])
    s = ax["s"] - ax["s0"]
    L = ax["length"]
    sn = s / max(L, 1e-9)
    h = ground - vr[:, 2]                         # altura sobre el suelo (≥ 0)
    # Lado medial: en el mediopié, el arco interno queda más alto que el borde lateral.
    nb = 30
    b = np.clip((sn * nb).astype(int), 0, nb - 1)
    qmin = np.full(nb, np.inf)
    qmax = np.full(nb, -np.inf)
    np.minimum.at(qmin, b, ax["q"])
    np.maximum.at(qmax, b, ax["q"])
    mid = 0.5 * (qmin + qmax)
    half = 0.5 * (qmax - qmin)
    rel = (ax["q"] - mid[b]) / np.maximum(half[b], 1e-9)
    midfoot = (sn > 0.35) & (sn < 0.60)
    hp = h[midfoot & (rel > 0.25) & (rel < 0.85)]
    hn = h[midfoot & (rel < -0.25) & (rel > -0.85)]
    diff = (np.median(hp) if len(hp) else 0.0) - (np.median(hn) if len(hn) else 0.0)
    m_sign_q = 1.0 if diff > 0 else -1.0        # medial hacia +e2 (en el marco canónico girado)
    conf = "alta" if abs(diff) > 2.0 and ax["width_ratio"] > 1.15 else ("media" if abs(diff) > 0.7 else "baja")

    # Marco persona: Y = talón→dedos, Z = arriba (= −Z canónico), X = Y × Z (derecha).
    u = np.array([ax["e1"][0], ax["e1"][1], 0.0])
    up = np.array([0.0, 0.0, -1.0])
    r = np.cross(u, up)
    e2_3 = np.array([ax["e2"][0], ax["e2"][1], 0.0])
    medial_dir = m_sign_q * e2_3
    detected = "izquierdo" if float(medial_dir @ r) > 0 else "derecho"
    heel = (sn < 0.15)
    H2 = vr[heel, :2].mean(axis=0) if heel.any() else vr[:, :2].mean(axis=0)
    H = np.array([H2[0], H2[1], ground])
    M = np.stack([r, u, up])                      # filas: ejes del marco persona en coords. giradas
    A = M @ Rg                                    # canónico -> persona (rotación propia)
    t = -M @ H
    T = np.eye(4)
    T[:3, :3] = A
    T[:3, 3] = t

    def tf(m: Mesh) -> Mesh:
        return Mesh(m.vertices @ A.T + t, m.faces.copy())

    sole_l = tf(sole_c)
    solid_l = tf(solid_c)
    sole_l.vertices[:, 2] = np.maximum(sole_l.vertices[:, 2], 0.0)
    medial_sign = float(np.sign(medial_dir @ r)) or 1.0
    width = float(np.ptp(sole_l.vertices[:, 0]))
    info = {
        "declared_side": declared_side, "detected_side": detected, "side_confidence": conf,
        "arch_height_difference_mm": float(diff), "forefoot_heel_width_ratio": ax["width_ratio"],
        "length_mm": float(L), "width_mm": width, "ground_tilt_deg": tilt,
        "load_point_fraction": load_point, "warnings": warnings,
    }
    if detected != declared_side:
        info["warnings"].append(
            f"La geometría del escaneo cargado como pie {declared_side} parece la de un pie {detected} "
            f"(confianza {conf}). Compruebe los archivos o use «Intercambiar pies».")
    return FootGeometry(declared_side, detected, conf, medial_sign, float(L), width, sole_l, solid_l,
                        sole_c.n_faces, T, tilt, info)


# ============================================================================ colocación
@dataclass
class Placement:
    side: str
    angle_deg: float                    # giro en planta (positivo = antihorario visto desde arriba)
    offset: np.ndarray                  # traslación (x, y) del centro del talón

    def matrix(self) -> np.ndarray:
        a = math.radians(self.angle_deg)
        R = np.array([[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]])
        T = np.eye(4)
        T[:3, :3] = R
        T[:2, 3] = self.offset
        return T

    def apply(self, m: Mesh) -> Mesh:
        T = self.matrix()
        return Mesh(m.vertices @ T[:3, :3].T + T[:3, 3], m.faces.copy())

    def inverse_xy(self, xy: np.ndarray) -> np.ndarray:
        T = self.matrix()
        return (xy - T[:2, 3]) @ T[:2, :2]


def _footprint(mesh: Mesh, grid: Grid) -> np.ndarray:
    return rasterize(mesh.triangles, grid)["count"] > 0


def place_feet(feet: dict[str, FootGeometry], p: StanceParams) -> tuple[dict[str, Placement], dict]:
    """Coloca los dos pies en el suelo común sin solaparse."""
    sep = float(p.heel_separation)
    warnings = []
    adjusted = 0.0
    for _ in range(60):
        pl = {
            "izquierdo": Placement("izquierdo", +p.toe_out_left, np.array([-sep / 2, 0.0])),
            "derecho": Placement("derecho", -p.toe_out_right, np.array([+sep / 2, p.advance_right])),
        }
        meshes = {s: pl[s].apply(feet[s].sole_local) for s in SIDES}
        lo = np.minimum(meshes["izquierdo"].bounds[0], meshes["derecho"].bounds[0])[:2]
        hi = np.maximum(meshes["izquierdo"].bounds[1], meshes["derecho"].bounds[1])[:2]
        g = make_grid(lo, hi, 1.0)
        fl, fr = _footprint(meshes["izquierdo"], g), _footprint(meshes["derecho"], g)
        gap_cells = max(int(math.ceil(p.min_gap)), 0)
        fl_d = ndimage.binary_dilation(fl, iterations=gap_cells) if gap_cells else fl
        if not (fl_d & fr).any():
            break
        sep += 5.0
        adjusted += 5.0
    if adjusted > 0:
        warnings.append(f"Con la separación indicada las huellas se solapaban o quedaban a menos de "
                        f"{p.min_gap:g} mm: se ha aumentado la separación entre talones a {sep:.0f} mm.")
    info = {"heel_separation_mm": sep, "requested_heel_separation_mm": p.heel_separation,
            "separation_adjusted_mm": adjusted, "toe_out_left_deg": p.toe_out_left,
            "toe_out_right_deg": p.toe_out_right, "angle_between_feet_deg": p.toe_out_left + p.toe_out_right,
            "advance_right_mm": p.advance_right, "warnings": warnings}
    return pl, info


# ============================================================================ contacto
def _law(eps: np.ndarray, e0: np.ndarray, beta, eps_c: float = 0.7) -> np.ndarray:
    """Presión (kPa) del tejido para la deformación ε = δ / t (exponencial, extensión lineal)."""
    b = np.maximum(beta, 1e-6)
    e = np.minimum(eps, eps_c)
    p = e0 / b * np.expm1(b * e)
    over = eps > eps_c
    if over.any():
        slope = e0 * np.exp(b * eps_c)
        p = np.where(over, p + slope * (eps - eps_c), p)
    return p


@dataclass
class FootField:
    """Campos de un pie sobre la rejilla común."""

    side: str
    mask: np.ndarray            # huella (proyección de la planta)
    h: np.ndarray               # altura de la planta sin carga sobre el suelo (mm)
    thick: np.ndarray           # espesor del tejido (mm)
    e0: np.ndarray              # módulo inicial (kPa)
    region: np.ndarray          # índice de región (−1 fuera)
    local: np.ndarray           # coordenadas locales (x, y) de cada celda (mm)
    toe_line: float             # fracción de la longitud donde empiezan los dedos
    beta: np.ndarray            # no linealidad del tejido
    toe_lever: np.ndarray       # brazo (mm) respecto al eje MTF en el segmento de los dedos (0 fuera)
    toe_rest: float             # giro medio de reposo de los dedos (rad), para el informe
    toe_rest_map: np.ndarray    # giro de reposo (rad) de cada dedo (franja mediolateral) tras asentarse
    arch_shape: np.ndarray      # forma (0–1) del descenso del arco
    mtp_line: float             # fracción de la longitud del eje MTF


def _regions(local: np.ndarray, mask: np.ndarray, L: float, medial_sign: float, h: np.ndarray):
    """Máscara anatómica en coordenadas del pie y línea de los dedos (surco digital)."""
    sn = local[..., 1] / max(L, 1e-9)
    x = local[..., 0] * medial_sign              # positivo hacia medial
    nb = 50
    b = np.clip((sn * nb).astype(int), 0, nb - 1)
    xm = np.where(mask, x, np.nan)
    tml = np.zeros_like(sn)
    for k in range(nb):
        sel = mask & (b == k)
        if sel.sum() < 2:
            continue
        lo, hi = np.nanmin(xm[sel]), np.nanmax(xm[sel])
        tml[sel] = (hi - x[sel]) / max(hi - lo, 1e-9)       # 0 = borde medial, 1 = lateral
    # surco digital: banda del antepié más alejada del suelo entre el 70 % y el 92 %
    toe = 0.80
    cand = mask & (tml > 0.15) & (tml < 0.85) & (sn > 0.68) & (sn < 0.93)
    if cand.sum() > 50:
        edges = np.linspace(0.68, 0.93, 26)
        prof = np.array([np.median(h[cand & (sn >= edges[i]) & (sn < edges[i + 1])])
                         if (cand & (sn >= edges[i]) & (sn < edges[i + 1])).any() else np.nan
                         for i in range(25)])
        if np.isfinite(prof).sum() > 10:
            prof = np.where(np.isfinite(prof), prof, np.nanmin(prof))
            prof = ndimage.uniform_filter1d(prof, 3)
            i = int(np.argmax(prof))
            prom = prof[i] - min(prof[: i + 1].min(), prof[i:].min())
            if prom > 0.5 and 1 <= i <= 23:
                toe = float(0.5 * (edges[i] + edges[i + 1]))
    reg = np.full(sn.shape, -1, dtype=np.int64)
    idx = {k: i for i, k in enumerate(REGION_KEYS)}
    heel = sn < 0.30
    reg[heel & (tml < 0.5)] = idx["talon_medial"]
    reg[heel & (tml >= 0.5)] = idx["talon_lateral"]
    reg[(sn >= 0.30) & (sn < 0.60)] = idx["mediopie"]
    fore = (sn >= 0.60) & (sn < toe)
    reg[fore & (tml < 0.33)] = idx["met1"]
    reg[fore & (tml >= 0.33) & (tml < 0.67)] = idx["met23"]
    reg[fore & (tml >= 0.67)] = idx["met45"]
    toes = sn >= toe
    reg[toes & (tml < 0.38)] = idx["hallux"]
    reg[toes & (tml >= 0.38)] = idx["dedos"]
    reg[~mask] = -1
    return reg, toe, tml


def build_field(foot: FootGeometry, pl: Placement, grid: Grid, tissue: dict) -> FootField:
    world = pl.apply(foot.sole_local)
    tri = world.triangles.copy()
    tri[..., 2] *= -1.0                       # el z-buffer guarda el máximo: se busca la mínima altura
    r = rasterize(tri, grid)
    mask = r["count"] > 0
    h = np.where(mask, -r["zmax"], np.inf)
    xs, ys = grid.node_xy()
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    local = pl.inverse_xy(np.stack([X.ravel(), Y.ravel()], axis=1)).reshape(X.shape + (2,))
    hh = np.where(mask, h, 0.0)
    reg, toe, tml = _regions(local, mask, foot.length, foot.medial_sign, hh)
    th = np.zeros(X.shape)
    e0 = np.zeros(X.shape)
    be = np.zeros(X.shape)
    for i, k in enumerate(REGION_KEYS):
        th[reg == i] = tissue[k]["thickness"]
        e0[reg == i] = tissue[k]["e0"]
        be[reg == i] = tissue[k]["beta"]
    # transiciones suaves entre regiones (el tejido no cambia de golpe)
    sig = 4.0 / grid.res
    wm = ndimage.gaussian_filter(mask.astype(float), sig)
    smooth = lambda f, out: np.where(mask, ndimage.gaussian_filter(f, sig) / np.maximum(wm, 1e-9), out)  # noqa: E731
    th, e0, be = smooth(th, 1.0), smooth(e0, 0.0), smooth(be, 1.0)
    sn = local[..., 1] / foot.length
    # Segmento de los dedos: gira alrededor del eje metatarsofalángico (MTF), situado algo por
    # detrás del surco digital. Asentamiento por gravedad: con el tono de los flexores, los dedos
    # relajados descienden hasta tocar el suelo (sin carga).
    mtp = toe - 0.07
    lever = np.where(mask & (sn > mtp), (sn - mtp) * foot.length, 0.0)
    # Cada dedo tiene su propia articulación MTF: el asentamiento se calcula por franjas
    # mediolaterales de ~9 mm (aprox. la anchura de un dedo). Cada franja gira hasta que su punto
    # más bajo toca el suelo, dentro del rango de flexión plantar (35°).
    toe_rest_map = np.zeros(X.shape)
    toes = lever > 0
    if toes.any():
        xl = local[..., 0]
        x0 = float(xl[toes].min())
        strip = np.floor((xl - x0) / 9.0).astype(int)
        rests = []
        for k in np.unique(strip[toes]):
            cells = toes & (strip == k)
            ok = cells & (lever > 8.0)
            if not ok.any():
                continue
            r = float(np.clip(np.min(hh[ok] / lever[ok]), -0.2, np.radians(35.0)))
            toe_rest_map[cells] = r
            rests.append(r)
        # transición suave entre franjas vecinas (piel y tejido continuos)
        wt = ndimage.gaussian_filter(toes.astype(float), 2.0 / grid.res)
        toe_rest_map = np.where(toes, ndimage.gaussian_filter(toe_rest_map, 2.0 / grid.res) / np.maximum(wt, 1e-9), 0.0)
        toe_rest = float(np.mean(rests)) if rests else 0.0
    else:
        toe_rest = 0.0
    # Descenso del arco longitudinal interno: máximo bajo el navicular (lado medial del mediopié);
    # la columna lateral (calcáneo-cuboides-4.º/5.º MTT) es mucho más rígida y no se hace descender.
    bx = np.where((sn > 0.20) & (sn < 0.70), np.sin(np.pi * (sn - 0.20) / 0.50) ** 2, 0.0)
    med = np.clip((0.6 - tml) / 0.5, 0.0, 1.0)
    arch = np.where(mask, bx * med * med * (3 - 2 * med), 0.0)
    return FootField(foot.declared_side, mask, h, np.maximum(th, 1.0), e0, reg, local, toe,
                     np.maximum(be, 0.1), lever, toe_rest, toe_rest_map, arch, mtp)


def _pressure(ff: FootField, w: float, tp: float, tr: float, pivot: np.ndarray, spread_px: float,
              toe_angle: float = 0.0, arch_drop: float = 0.0) -> np.ndarray:
    """Presión para una configuración del pie.

    * ``w``: aproximación vertical del pie; ``tp`` (cabeceo) baja la parte anterior y ``tr``
      (alabeo) el lado +X local, alrededor de ``pivot`` (línea de carga, coords. locales).
    * ``toe_angle``: giro adicional de los dedos (rad, + hacia el suelo) respecto a su reposo
      asentado, alrededor de los ejes MTF.
    * ``arch_drop``: descenso del arco (mm); el esqueleto no atraviesa el suelo: el descenso local
      se limita a la holgura que queda hasta tocarlo: el arco baja hasta apoyar, y la compresión
      posterior del tejido la produce la carga, no el descenso.
    """
    h = ff.h
    if arch_drop:
        drop = np.minimum(arch_drop * ff.arch_shape, np.maximum(np.where(ff.mask, h, 0.0), 0.0))
        h = h - drop
    if tp or tr:
        h = h - tp * (ff.local[..., 1] - pivot[1]) - tr * (ff.local[..., 0] - pivot[0])
    h = h - (ff.toe_rest_map + toe_angle) * ff.toe_lever
    delta = np.where(ff.mask, np.maximum(w - h, 0.0), 0.0)
    p = _law(delta / ff.thick, ff.e0, ff.beta)
    if spread_px > 0:
        p = ndimage.gaussian_filter(p, spread_px, mode="constant")
    return np.where(ff.mask, p, 0.0)


def _moments(p: np.ndarray, grid: Grid):
    xs, ys = grid.node_xy()
    a = grid.res ** 2 * 1e-3                   # N por kPa y celda
    F = float(p.sum() * a)
    mx = float((p.sum(axis=1) * xs).sum() * a)
    my = float((p.sum(axis=0) * ys).sum() * a)
    return F, mx, my


def _local_cop(ff: FootField, p: np.ndarray) -> tuple[float, np.ndarray]:
    w = p.sum()
    if w <= 0:
        return 0.0, np.array([np.nan, np.nan])
    return float(w), np.array([(p * ff.local[..., 0]).sum() / w, (p * ff.local[..., 1]).sum() / w])


def _midline_point(ff: FootField, L: float, frac: float) -> np.ndarray:
    """Punto de la línea media de la huella a la altura ``frac`` de la longitud (coords. locales)."""
    sn = ff.local[..., 1] / L
    band = ff.mask & (np.abs(sn - frac) < 0.02)
    if band.sum() < 3:
        band = ff.mask & (np.abs(sn - frac) < 0.06)
    if not band.any():
        raise BilateralError("La línea de carga cae fuera de la huella del pie.")
    xl = ff.local[..., 0][band]
    return np.array([0.5 * (xl.min() + xl.max()), frac * L])


def _solve_foot(ff: FootField, grid: Grid, target_F: float, target_cop: np.ndarray, spread_px: float,
                side: str, toe_k: float, arch_drop: float, toe_flex: float = 0.0) -> dict:
    """Equilibrio de un pie: segmento principal rígido + dedos articulados + arco que desciende.

    Incógnitas: aproximación w, cabeceo, alabeo y giro de los dedos. Ecuaciones: fuerza total,
    los dos momentos que sitúan el centro de presiones en la línea de carga, y el equilibrio de
    momentos del segmento de los dedos alrededor del eje MTF: el momento de la reacción del
    suelo bajo los dedos = tono de los flexores + rigidez pasiva × giro respecto a su posición de
    reposo apoyada.
    """
    a_cell = grid.res ** 2 * 1e-3
    hmin = float(np.min(ff.h[ff.mask]))
    pivot = target_cop
    has_toes = bool((ff.toe_lever > 0).any()) and toe_k > 0

    def press(w, tp, tr, ta):
        return _pressure(ff, w, tp, tr, pivot, spread_px, ta, arch_drop)

    def F_of(w, tp, tr, ta):
        return press(w, tp, tr, ta).sum() * a_cell

    def w_for(tp, tr, ta, F):
        lo_w = hmin - 300.0 * (abs(tp) + abs(tr) + abs(ta)) - arch_drop - 1.0
        while F_of(lo_w, tp, tr, ta) > 0:
            lo_w -= 5.0
        hi_w = hmin + 1.0
        while F_of(hi_w, tp, tr, ta) < F:
            hi_w += max(1.0, hi_w - lo_w)
            if hi_w - hmin > 300:
                raise BilateralError(f"Pie {side}: no se alcanza el equilibrio de fuerzas.")
        return brentq(lambda w: F_of(w, tp, tr, ta) - F, lo_w, hi_w, xtol=1e-8, rtol=1e-12)

    def toe_moment(p):
        return float((p * ff.toe_lever).sum() * a_cell)            # N·mm

    def resid(x):
        tp, tr = x[0], x[1]
        ta = x[2] if has_toes else 0.0
        w = w_for(tp, tr, ta, target_F)
        p = press(w, tp, tr, ta)
        _, c = _local_cop(ff, p)
        r = list((c - target_cop) / 5.0)
        if has_toes:
            r.append((toe_moment(p) - toe_flex + toe_k * ta) / (0.02 * target_F * 50.0))
        return np.array(r)

    x0 = np.zeros(3) if has_toes else np.zeros(2)
    lo = [-0.35, -0.35] + ([-0.6] if has_toes else [])
    hi = [0.35, 0.35] + ([0.3] if has_toes else [])
    sol = least_squares(resid, x0, x_scale=np.full(len(x0), 0.02), bounds=(lo, hi),
                        xtol=1e-12, ftol=1e-12, gtol=1e-12, max_nfev=300, diff_step=1e-4)
    tp, tr = sol.x[0], sol.x[1]
    ta = float(sol.x[2]) if has_toes else 0.0
    w = w_for(tp, tr, ta, target_F)
    p = press(w, tp, tr, ta)
    F, c = _local_cop(ff, p)
    err = float(np.linalg.norm(c - target_cop))
    if err > 1.0:
        raise BilateralError(
            f"Pie {side}: no existe equilibrio de momentos con la línea de carga indicada (el centro de "
            f"presiones queda a {err:.1f} mm). Acérquela al centro de la huella.")
    toe_eq = (toe_moment(p) - toe_flex + toe_k * ta) if has_toes else 0.0
    return {"w": w, "pitch": float(tp), "roll": float(tr), "toe_angle": ta, "p": p, "pivot": pivot,
            "cop_error_mm": err, "toe_moment_Nmm": toe_moment(p), "toe_equilibrium_error_Nmm": float(toe_eq),
            "arch_drop": arch_drop}


def simulate(feet: dict[str, FootGeometry], placements: dict[str, Placement], sp: SimParams) -> dict:
    """Simulación conjunta de bipedestación estática sobre ambos pies."""
    if sp.mass <= 0:
        raise BilateralError("El peso corporal debe ser positivo.")
    W = sp.mass * G
    meshes = {s: placements[s].apply(feet[s].sole_local) for s in SIDES}
    lo = np.minimum(meshes["izquierdo"].bounds[0], meshes["derecho"].bounds[0])[:2] - 10
    hi = np.maximum(meshes["izquierdo"].bounds[1], meshes["derecho"].bounds[1])[:2] + 10
    grid = make_grid(lo, hi, sp.resolution)
    fields = {s: build_field(feet[s], placements[s], grid, sp.tissue_for(s)) for s in SIDES}
    spread_px = sp.spread / grid.res
    warnings: list[str] = []
    L = {sd: feet[sd].length for sd in SIDES}

    def world(sd, local_pt):
        T = placements[sd].matrix()
        return T[:2, :2] @ local_pt + T[:2, 3]

    if sp.mode == "centro_masas":
        # El reparto y la posición AP común de las líneas de carga se deducen del equilibrio
        # de momentos del cuerpo: el centro de presiones global = proyección del centro de masas.
        Lm = 0.5 * (L["izquierdo"] + L["derecho"])
        mid = 0.5 * (placements["izquierdo"].offset + placements["derecho"].offset)
        com = np.array([mid[0] + sp.com_x, mid[1] + sp.com_y_frac * Lm])

        def cop_targets(f):
            return {sd: _midline_point(fields[sd], L[sd], f) for sd in SIDES}

        def resid(x):
            lam, f = x
            t = cop_targets(f)
            g = lam * world("izquierdo", t["izquierdo"]) + (1 - lam) * world("derecho", t["derecho"])
            return (g - com) / 5.0

        sol = least_squares(resid, np.array([0.5, sp.load_point]), bounds=([0.01, 0.08], [0.99, 0.85]),
                            xtol=1e-12, ftol=1e-12, gtol=1e-12, diff_step=1e-4)
        if np.abs(resid(sol.x)).max() * 5.0 > 1.0:
            raise BilateralError(
                "No existe equilibrio con el centro de masas indicado: su proyección cae fuera de la "
                "base de apoyo de los dos pies. Acérquelo al espacio entre ambos pies.")
        lam, frac = sol.x
        targets = {"izquierdo": lam * W, "derecho": (1 - lam) * W}
        cops = cop_targets(frac)
        com_used = com
    else:
        if not (0.0 < sp.split_left < 100.0):
            raise BilateralError("El reparto del pie izquierdo debe estar entre 0 y 100 %.")
        targets = {"izquierdo": W * sp.split_left / 100.0, "derecho": W * (1 - sp.split_left / 100.0)}
        frac = sp.load_point
        cops = {sd: _midline_point(fields[sd], L[sd], frac) for sd in SIDES}
        com_used = None
    # descenso del arco proporcional a la carga del pie (la caída del navicular se mide con medio peso por pie)
    drops = {sd: (0.0 if sp.loaded_scan else sp.arch_drop * targets[sd] / (0.5 * W)) for sd in SIDES}
    flex = {sd: sp.toe_flexor * targets[sd] / (0.5 * W) for sd in SIDES}
    sols = {sd: _solve_foot(fields[sd], grid, targets[sd], cops[sd], spread_px, sd, sp.toe_stiffness, drops[sd],
                            flex[sd]) for sd in SIDES}
    P = {sd: sols[sd]["p"] for sd in SIDES}
    result = _report(feet, placements, fields, P, sols, grid, sp, W, frac, com_used, warnings)
    result["_arrays"] = {"grid": grid, "pressure": P, "fields": fields}
    return result


# ============================================================================ informe
def _foot_report(foot: FootGeometry, ff: FootField, p: np.ndarray, sol: dict, grid: Grid, sp: SimParams,
                 W: float, pl: Placement) -> dict:
    w = sol["w"]
    a_cell = grid.res ** 2
    F, mx, my = _moments(p, grid)
    contact = p > sp.contact_threshold
    area = float(contact.sum() * a_cell) / 100.0      # cm²
    cop = np.array([mx / F, my / F]) if F > 0 else np.array([np.nan, np.nan])
    cop_local = pl.inverse_xy(cop[None, :])[0]
    regions = []
    for i, k in enumerate(REGION_KEYS):
        sel = ff.region == i
        pr = p[sel]
        Fr = float(pr.sum() * a_cell * 1e-3)
        ar = float((sel & contact).sum() * a_cell) / 100.0
        regions.append({
            "key": k, "name": REGION_NAMES[k], "force_N": Fr,
            "pct_foot": 100.0 * Fr / F if F > 0 else 0.0, "pct_body": 100.0 * Fr / W,
            "contact_area_cm2": ar, "peak_kPa": float(pr.max()) if pr.size else 0.0,
            "mean_kPa": float(Fr / (ar * 1e-4) / 1e3) if ar > 0 else 0.0,
        })
    # índice del arco (Cavanagh y Rodgers): área del mediopié / área de la huella sin dedos
    sn = ff.local[..., 1] / foot.length
    no_toes = contact & (sn < ff.toe_line)
    third = [(no_toes & (sn >= lo) & (sn < hi)).sum() for lo, hi in
             ((-1, ff.toe_line / 3), (ff.toe_line / 3, 2 * ff.toe_line / 3), (2 * ff.toe_line / 3, 2))]
    arch_index = float(third[1] / max(sum(third), 1))
    h_eff = ff.h
    if sol["arch_drop"]:
        h_eff = h_eff - np.minimum(sol["arch_drop"] * ff.arch_shape, np.maximum(np.where(ff.mask, ff.h, 0.0), 0.0))
    h_eff = (h_eff - sol["pitch"] * (ff.local[..., 1] - sol["pivot"][1])
             - sol["roll"] * (ff.local[..., 0] - sol["pivot"][0]) - (ff.toe_rest_map + sol["toe_angle"]) * ff.toe_lever)
    eps = np.where(ff.mask, np.maximum(w - h_eff, 0.0) / ff.thick, 0.0)
    toes = ff.toe_lever > 0
    toe_contact = float((contact & toes).sum() * a_cell) / 100.0
    # Asentamiento por gravedad (antes de cargar): zonas que tocan el suelo con el pie apoyado
    # y los dedos asentados, sin compresión del tejido.
    h_settle = ff.h - ff.toe_rest_map * ff.toe_lever
    touch = ff.mask & (h_settle <= 0.5)
    initial = [REGION_NAMES[k] for i, k in enumerate(REGION_KEYS) if (touch & (ff.region == i)).sum() * a_cell >= 4.0]
    return {
        "side": foot.declared_side, "force_N": F, "pct_body": 100.0 * F / W,
        "contact_area_cm2": area, "mean_kPa": float(F / (area * 1e-4) / 1e3) if area > 0 else 0.0,
        "peak_kPa": float(p.max()), "cop_world_mm": cop.tolist(),
        "cop_local_mm": cop_local.tolist(), "cop_from_heel_pct": float(100.0 * cop_local[1] / foot.length),
        "max_compression_mm": float((eps * ff.thick).max()), "max_tissue_strain_pct": float(100.0 * eps.max()),
        "pitch_deg": float(np.degrees(sol["pitch"])),
        "toe_rest_deg": float(np.degrees(ff.toe_rest)),
        "initial_support_regions": initial,
        "toe_angle_deg": float(np.degrees(sol["toe_angle"])),
        "toe_lift_under_load_deg": float(np.degrees(-sol["toe_angle"])),
        "toe_contact_area_cm2": toe_contact,
        "toe_equilibrium_error_Nmm": float(sol["toe_equilibrium_error_Nmm"]),
        "toes_pct": float(sum(r["pct_foot"] for r in regions if r["key"] in ("hallux", "dedos"))),
        "midfoot_pct": float(sum(r["pct_foot"] for r in regions if r["key"] == "mediopie")),
        "arch_drop_mm": float(sol["arch_drop"]),
        "mtp_line_pct": 100.0 * ff.mtp_line,
        "roll_deg": float(np.degrees(sol["roll"]) * foot.medial_sign),
        "load_line_local_mm": np.asarray(sol["pivot"]).tolist(), "cop_error_mm": sol["cop_error_mm"],
        "regions": regions, "arch_index": arch_index, "toe_line_pct": 100.0 * ff.toe_line,
        "length_mm": foot.length, "width_mm": foot.width,
        "rearfoot_pct": float(sum(r["pct_foot"] for r in regions if r["key"].startswith("talon"))),
        "forefoot_pct": float(sum(r["pct_foot"] for r in regions if r["key"] in ("met1", "met23", "met45"))),
    }


def _asym(a: float, b: float) -> float:
    s = abs(a) + abs(b)
    return 200.0 * abs(a - b) / s if s > 0 else 0.0


def _report(feet, placements, fields, P, sols, grid, sp, W, frac, com, warnings) -> dict:
    per = {s: _foot_report(feet[s], fields[s], P[s], sols[s], grid, sp, W, placements[s]) for s in SIDES}
    Ft = per["izquierdo"]["force_N"] + per["derecho"]["force_N"]
    xs, ys = grid.node_xy()
    ptot = P["izquierdo"] + P["derecho"]
    _, mx, my = _moments(ptot, grid)
    cop = [mx / Ft, my / Ft]
    area = per["izquierdo"]["contact_area_cm2"] + per["derecho"]["contact_area_cm2"]
    for s in SIDES:
        if per[s]["max_tissue_strain_pct"] > 60:
            warnings.append(f"Pie {s}: compresión máxima del tejido del {per[s]['max_tissue_strain_pct']:.0f} %; "
                            "fuera del rango en que el modelo es fiable (revise peso y propiedades).")
    glob = {
        "mass_kg": sp.mass, "gravity": G, "weight_N": W, "feet_force_N": Ft,
        "split_left_pct": 100.0 * per["izquierdo"]["force_N"] / Ft,
        "split_right_pct": 100.0 * per["derecho"]["force_N"] / Ft,
        "split_source": ("resultado del equilibrio de momentos con el centro de masas indicado"
                         if sp.mode == "centro_masas" else "hipótesis introducida por el usuario (no es una medición)"),
        "contact_area_cm2": area, "mean_kPa": float(Ft / (area * 1e-4) / 1e3) if area > 0 else 0.0,
        "peak_kPa": float(max(per[s]["peak_kPa"] for s in SIDES)),
        "cop_world_mm": cop, "force_error_N": Ft - W, "force_error_pct": 100.0 * (Ft - W) / W,
        "load_line_pct": 100.0 * float(frac),
        "com_projection_mm": list(com) if com is not None else None,
        "moment_error_Nmm": (float(math.hypot(mx - W * com[0], my - W * com[1])) if com is not None else None),
        "cop_error_max_mm": float(max(sols[s]["cop_error_mm"] for s in SIDES)),
        "mode": sp.mode,
    }
    comp = []
    for key, name, unit in (("force_N", "Fuerza", "N"), ("pct_body", "Peso soportado", "%"),
                            ("contact_area_cm2", "Superficie de contacto", "cm²"), ("mean_kPa", "Presión media", "kPa"),
                            ("peak_kPa", "Presión máxima", "kPa"), ("rearfoot_pct", "Carga en retropié", "% del pie"),
                            ("midfoot_pct", "Carga en mediopié", "% del pie"),
                            ("forefoot_pct", "Carga en antepié (metatarsos)", "% del pie"),
                            ("toes_pct", "Carga en dedos", "% del pie"), ("arch_index", "Índice del arco", ""),
                            ("cop_from_heel_pct", "CoP desde el talón", "% longitud")):
        l, r = per["izquierdo"][key], per["derecho"][key]
        comp.append({"key": key, "name": name, "unit": unit, "left": l, "right": r,
                     "difference": l - r, "asymmetry_pct": _asym(l, r)})
    for i, k in enumerate(REGION_KEYS):
        l, r = per["izquierdo"]["regions"][i]["pct_body"], per["derecho"]["regions"][i]["pct_body"]
        comp.append({"key": f"region_{k}", "name": f"{REGION_NAMES[k]} (carga)", "unit": "% peso",
                     "left": l, "right": r, "difference": l - r, "asymmetry_pct": _asym(l, r)})
    return {"global": glob, "feet": per, "comparison": comp,
            "params": {k: v for k, v in asdict(sp).items()},
            "tissue": {s: sp.tissue_for(s) for s in SIDES},
            "grid": {"x0": grid.x0, "y0": grid.y0, "res": grid.res, "nx": grid.nx, "ny": grid.ny},
            "warnings": warnings}


# ============================================================================ salidas
def pressure_csv(result: dict) -> bytes:
    arr = result["_arrays"]
    grid: Grid = arr["grid"]
    xs, ys = grid.node_xy()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["pie", "x_mm", "y_mm", "x_local_mm", "y_local_mm", "presion_kPa", "contacto", "region",
                "altura_sin_carga_mm"])
    thr = result["params"]["contact_threshold"]
    for s in SIDES:
        ff = arr["fields"][s]
        p = arr["pressure"][s]
        ii, jj = np.nonzero(ff.mask)
        for i, j in zip(ii.tolist(), jj.tolist()):
            r = ff.region[i, j]
            w.writerow([s, f"{xs[i]:.2f}", f"{ys[j]:.2f}", f"{ff.local[i, j, 0]:.2f}", f"{ff.local[i, j, 1]:.2f}",
                        f"{p[i, j]:.3f}", int(p[i, j] > thr), REGION_KEYS[r] if r >= 0 else "",
                        f"{ff.h[i, j]:.3f}"])
    return out.getvalue().encode("utf-8")


def result_json(result: dict, extra: dict | None = None) -> bytes:
    data = {k: v for k, v in result.items() if not k.startswith("_")}
    if extra:
        data.update(extra)

    def clean(o):
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, np.ndarray):
            return clean(o.tolist())
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (float, np.floating)):
            return float(o) if math.isfinite(float(o)) else None
        if isinstance(o, np.bool_):
            return bool(o)
        return o
    return json.dumps(clean(data), ensure_ascii=False, indent=2).encode("utf-8")


def face_pressure(result: dict, foot_world: Mesh, side: str) -> np.ndarray:
    """Presión en el centroide de cada cara de la planta colocada (para colorear el 3D)."""
    arr = result["_arrays"]
    grid: Grid = arr["grid"]
    return grid.sample_bilinear(arr["pressure"][side], foot_world.centroids[:, :2])
