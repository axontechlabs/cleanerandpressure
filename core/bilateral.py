"""Simulación bilateral de bipedestación estática: dos pies, un suelo, un peso corporal.

Modelo físico
-------------
* Cada pie es un **segmento principal rígido** (retropié, mediopié y metatarsianos, con un arco
  interno que desciende con la carga) más **cinco dedos articulados**, recubiertos de **tejido
  blando plantar** cuyo espesor y rigidez dependen de la región anatómica. La geometría de partida
  es la superficie plantar limpia y suavizada que produce el pipeline de un pie (sin carga).
* **Dedos**: cada uno gira en su articulación metatarsofalángica hasta que su pulpejo toca el suelo
  (en un escaneo sin carga suelen quedar levantados) y se aprieta contra él con el momento flexor
  (flexores + fascia plantar) hasta el equilibrio de momentos alrededor de las MTF.
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

    heel_separation: float | None = None  # mm entre centros de talón; None = automática (estatura o media)
    toe_out_left: float = 7.0           # grados de apertura del pie izquierdo (14° entre ejes)
    toe_out_right: float = 7.0
    advance_right: float = 0.0          # mm que el pie derecho está por delante del izquierdo
    load_point: float = 0.32            # posición AP de la carga para apoyar el pie (fracción desde el talón)
    min_gap: float = 8.0                # mm de separación mínima entre huellas
    swap: bool = False                  # intercambiar los escaneos izquierdo y derecho
    height_cm: float = 0.0              # estatura (cm); 0 = desconocida

    @classmethod
    def from_dict(cls, d: dict | None) -> "StanceParams":
        d = d or {}
        out = cls()
        for k in cls.__dataclass_fields__:
            if k in d and d[k] is not None:
                if k == "swap":
                    out.swap = bool(d[k])
                else:
                    setattr(out, k, float(d[k]))
        if out.heel_separation is not None and not out.heel_separation > 0:
            out.heel_separation = None
        return out

    def resolved_separation(self) -> tuple[float, str]:
        """Separación entre centros de talón (mm) y de dónde sale."""
        if self.heel_separation is not None:
            return float(self.heel_separation), "indicada por el usuario"
        if self.height_cm and self.height_cm > 0:
            return stance_width_from_height(self.height_cm), "estimada por la estatura"
        return STANCE_WIDTH_MEAN, "media de la población (McIlroy y Maki, 1997)"


# Postura preferida: 17 cm entre centros de talón de media (McIlroy y Maki, 1997). La separación
# preferida se correlaciona con la estatura (r = 0,56; DE 4,7 cm de separación y 8 cm de estatura en
# adultos de 1,69 m de media): pendiente de regresión 0,56 · 4,7 / 8 ≈ 0,33 cm por cm de estatura.
STANCE_WIDTH_MEAN = 170.0           # mm
STANCE_WIDTH_SLOPE = 0.33           # mm por mm de estatura (= cm por cm)
STANCE_REF_HEIGHT = 169.0           # cm


def stance_width_from_height(height_cm: float) -> float:
    w = STANCE_WIDTH_MEAN + 10.0 * STANCE_WIDTH_SLOPE * (float(height_cm) - STANCE_REF_HEIGHT)
    return float(np.clip(w, 100.0, 280.0))


# Descenso del arco desde el escaneo sin carga hasta la bipedestación. Del pie sin carga (sentado) a
# de pie con medio peso, la altura dorsal a mitad del pie baja unos 10 mm de media (McPoil et al.,
# 2008); parte es compresión del tejido plantar, que el modelo calcula aparte, así que el descenso
# óseo de referencia es de 8 mm para un pie de 25,3 cm. (La caída del navicular clásica, 3,6–8,1 mm,
# se mide desde la posición neutra subastragalina, no desde el pie relajado sin carga.) Aumenta con
# la longitud del pie: 0,31–0,40 mm por cm (Nielsen et al., 2009).
ARCH_DROP_REF = 8.0                 # mm
ARCH_DROP_PER_CM = 0.35             # mm por cm de longitud del pie
ARCH_DROP_REF_LENGTH = 25.3         # cm


def arch_drop_for_length(length_mm: float) -> float:
    return float(np.clip(ARCH_DROP_REF + ARCH_DROP_PER_CM * (length_mm / 10.0 - ARCH_DROP_REF_LENGTH), 3.0, 10.0))


@dataclass
class SimParams:
    mass: float = 70.0                  # kg
    split_left: float = 50.0            # % del peso sobre el pie izquierdo (hipótesis)
    mode: str = "reparto"               # "reparto" | "centro_masas"
    load_point: float = 0.32            # línea de carga de cada pie (fracción de la longitud desde el talón)
    arch_drop: float | None = None      # mm con medio peso (caída del navicular); None = según la longitud del pie
    height_cm: float = 0.0              # estatura (cm); 0 = desconocida
    loaded_scan: bool = False           # el escaneo se tomó en carga (el arco ya descendió)
    toe_flexor: float = 700.0           # N·mm por pie (con medio peso): momento flexor de los dedos (flexores + fascia)
    toe_max_flexion: float = 50.0       # grados: giro máximo de un dedo desde la postura del escaneo hasta apoyar
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
                  "contact_threshold", "resolution", "arch_drop", "toe_flexor", "toe_max_flexion", "height_cm"):
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


# Longitud del pie / estatura: 14,6–15,1 % sin carga (Xiong et al., 2009); suele quedar entre el 14 y
# el 16 %. Fuera de 13–17,5 % lo más probable es que el escaneo tenga otra escala (unidades) o
# que la estatura sea errónea.
FOOT_STATURE_RANGE = (0.13, 0.175)


def scale_check(feet: dict[str, FootGeometry], height_cm: float) -> list[str]:
    """Comprueba que la longitud de cada pie es coherente con la estatura (escala del escaneo)."""
    if not height_cm or height_cm <= 0:
        return []
    out = []
    for s in SIDES:
        r = feet[s].length / (10.0 * height_cm)
        if not (FOOT_STATURE_RANGE[0] <= r <= FOOT_STATURE_RANGE[1]):
            out.append(f"El pie {s} mide {feet[s].length:.0f} mm, el {100 * r:.1f} % de la estatura "
                       f"({height_cm:.0f} cm); lo habitual es el 14–16 %. Compruebe la estatura y las unidades "
                       "del escaneo.")
    return out


def place_feet(feet: dict[str, FootGeometry], p: StanceParams) -> tuple[dict[str, Placement], dict]:
    """Coloca los dos pies en el suelo común sin solaparse."""
    sep, sep_source = p.resolved_separation()
    requested = sep
    warnings = scale_check(feet, p.height_cm)
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
    info = {"heel_separation_mm": sep, "requested_heel_separation_mm": requested,
            "heel_separation_source": sep_source, "height_cm": p.height_cm or None,
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
class ToeModel:
    """Los cinco dedos como segmentos rígidos, cada uno con su articulación MTF.

    Cada dedo j gira (en el plano sagital del pie) un ángulo θ_j alrededor de un eje transversal
    situado sobre la cabeza de su metatarsiano, a ``TOE_AXIS_HEIGHT`` mm sobre la piel. θ es
    positivo hacia el suelo (flexión plantar) y se mide desde la postura del escaneo.

    Cada celda de la zona de los dedos pertenece a uno o, cerca de un límite sin hueco
    interdigital visible, a dos dedos vecinos (pesos ``weight``, que suman 1): la piel es continua
    y la transición entre dedos es suave. Donde el escaneo muestra el hueco, el límite es nítido.
    """

    cells: tuple                # índices (i, j) de las celdas de la zona de los dedos
    weight: np.ndarray          # (n, 5) pertenencia de cada celda a cada dedo
    y: np.ndarray               # (n,) coordenada local a lo largo del pie (mm)
    h: np.ndarray               # (n,) altura sin carga (mm)
    axis_y: np.ndarray          # (5,) posición del eje MTF de cada dedo (mm)
    z_axis: np.ndarray          # (5,) altura del eje MTF sobre el suelo (mm)
    rest: np.ndarray            # (5,) giro de asentamiento por gravedad (rad): el dedo toca el suelo
    reach: np.ndarray           # (5,) True si el dedo llega al suelo dentro del rango articular
    present: np.ndarray         # (5,) el dedo tiene celdas
    cell_toe: np.ndarray        # (nx, ny) dedo principal de cada celda (−1 fuera de los dedos)
    length: float               # longitud del pie (mm)
    detected: int               # dedos separados por huecos visibles en la geometría
    theta_max: float            # rad: giro máximo desde la postura del escaneo

    @property
    def active(self) -> bool:
        return bool(self.present.any())

    def heights(self, theta: np.ndarray):
        """Altura (n,) de las celdas tras girar cada dedo θ_j (exacto) y el brazo (n, 5) de cada una."""
        dy = self.y[:, None] - self.axis_y[None, :]
        dz = self.h[:, None] - self.z_axis[None, :]
        s, c = np.sin(theta)[None, :], np.cos(theta)[None, :]
        hj = self.z_axis[None, :] - dy * s + dz * c
        return (self.weight * hj).sum(axis=1), dy * c + dz * s

    def settled(self):
        """Altura (n,) y brazos (n, 5) con cada dedo asentado (girado hasta tocar el suelo)."""
        if getattr(self, "_settled", None) is None:
            self._settled = self.heights(self.rest)
        return self._settled

    def press_cells(self, press: np.ndarray) -> np.ndarray:
        """Desplazamiento hacia el suelo (mm) de cada celda para el apoyo (hallux, 2.º–5.º)."""
        wh = self.weight[:, 0]
        return wh * press[0] + (1.0 - wh) * press[1]

    def field(self, base: np.ndarray, press: np.ndarray) -> np.ndarray:
        """Altura con los dedos asentados y apoyados ``press`` mm (por grupo) sobre el suelo."""
        out = base.copy()
        if self.active:
            out[self.cells] = self.settled()[0] - self.press_cells(press)
        return out


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
    toe_line: float             # fracción media de la longitud donde empiezan los dedos (surco)
    beta: np.ndarray            # no linealidad del tejido
    toes: ToeModel              # dedos articulados
    arch_shape: np.ndarray      # forma (0–1) del descenso del arco
    mtp_line: float             # fracción media de la longitud de los ejes MTF
    arch: dict = field(default_factory=dict)   # medidas del arco en el escaneo sin carga

    @property
    def toe_mask(self) -> np.ndarray:
        return self.toes.cell_toe >= 0


TOE_NAMES = ("Hallux (1.º)", "2.º dedo", "3.º dedo", "4.º dedo", "5.º dedo")
# Anchura relativa de cada dedo en la fila de los dedos (medial → lateral): el hallux ocupa
# aproximadamente un tercio. Se usa si la geometría no separa los cinco dedos.
TOE_WIDTH_SHARE = np.array([0.31, 0.18, 0.17, 0.17, 0.17])
# Reparto del momento flexor entre los dos grupos: el flexor largo del hallux actúa solo sobre el
# 1.º (40 %); los flexores largo y corto de los dedos y la fascia plantar, sobre el 2.º–5.º (60 %,
# que se reparte entre esos dedos según su geometría).
TOE_TORQUE_SHARE = np.array([0.40, 0.15, 0.15, 0.15, 0.15])
TOE_AXIS_HEIGHT = 20.0          # mm: eje MTF sobre la piel (≈ 10 mm de tejido + radio de la cabeza)
MTP_TO_SULCUS = 0.07            # fracción de la longitud entre el eje MTF y el surco digital (~17 mm)
TOE_THETA_MIN = np.radians(-30.0)


def _forefoot_lines(x: np.ndarray, y: np.ndarray, h: np.ndarray, mask: np.ndarray, L: float):
    """Eje MTF a lo largo de la fila de los dedos (parábola metatarsiana), por franjas de 4 mm.

    En cada franja mediolateral se busca, en el perfil de alturas a lo largo del pie, la
    almohadilla de las cabezas metatarsianas (lo más bajo del antepié) y delante el surco digital:
    un máximo local seguido del pulpejo del dedo, más bajo. Si los dedos están levantados en el
    escaneo no hay surco: el dedo empieza donde el perfil deja de estar apoyado. Las posiciones se
    ajustan con una parábola en x (robusta frente a franjas atípicas).
    """
    sn = y / L
    fore = mask & (sn > 0.55)
    if fore.sum() < 100:
        return None
    xs = x[fore]
    x_lo, x_hi = np.percentile(xs, 1), np.percentile(xs, 99)
    edges = np.arange(x_lo, x_hi + 4.0, 4.0)
    xc, ya, wt = [], [], []
    bin_mm = 2.0
    for k in range(len(edges) - 1):
        sel = fore & (x >= edges[k]) & (x < edges[k + 1])
        if sel.sum() < 15:
            continue
        yy, hh = y[sel], h[sel]
        b = np.floor((yy - 0.55 * L) / bin_mm).astype(int)
        nb = int(b.max()) + 1
        prof = np.full(nb, np.inf)
        np.minimum.at(prof, b, hh)
        yb = 0.55 * L + (np.arange(nb) + 0.5) * bin_mm
        ok = np.isfinite(prof)
        if ok.sum() < 8:
            continue
        prof = np.interp(yb, yb[ok], prof[ok])
        prof = ndimage.uniform_filter1d(prof, 3, mode="nearest")
        ball = (yb >= 0.58 * L) & (yb <= 0.84 * L)
        if not ball.any():
            continue
        ib = int(np.flatnonzero(ball)[np.argmin(prof[ball])])
        hb = prof[ib]
        axis = None
        # surco: máximo local por delante de la bola con un pulpejo más bajo detrás
        lim = min(nb - 1, ib + int(0.16 * L / bin_mm))
        for i in range(ib + 2, lim):
            if prof[i] >= prof[i - 1] and prof[i] >= prof[i + 1] and prof[i] - prof[i + 1:].min() > 0.8:
                axis = yb[i] - MTP_TO_SULCUS * L
                break
        if axis is None:
            rise = np.flatnonzero((np.arange(nb) > ib) & (prof > hb + 1.5))
            if not len(rise):
                continue
            axis = yb[rise[0]] - 0.02 * L
        xc.append(0.5 * (edges[k] + edges[k + 1]))
        ya.append(axis)
        wt.append(float(sel.sum()))
    if len(xc) < 3:
        return None
    xc, ya, wt = np.array(xc), np.array(ya), np.array(wt)
    keep = np.ones(len(xc), bool)
    for _ in range(3):
        deg = 2 if keep.sum() >= 6 else 1
        c = np.polyfit(xc[keep], ya[keep], deg, w=np.sqrt(wt[keep]))
        r = ya - np.polyval(c, xc)
        thr = max(6.0, 2.5 * np.median(np.abs(r[keep])))
        new = np.abs(r) <= thr
        if new.sum() < 3 or (new == keep).all():
            break
        keep = new
    # Límites anatómicos del eje MTF (desde el centro del talón): de la 5.ª articulación, hacia el
    # 57 % de la longitud, a la 1.ª, hacia el 66 %, con margen para pies y escaneos atípicos.
    return lambda xq: np.clip(np.polyval(c, xq), 0.55 * L, 0.78 * L)


def _settle_angle(lowest, theta_max: float) -> float:
    """Menor giro hacia el suelo con el que el punto más bajo del dedo toca el suelo (rad)."""
    if lowest(0.0) <= 0.0:
        lo = TOE_THETA_MIN
        if lowest(lo) <= 0.0:
            return lo
        return brentq(lowest, lo, 0.0, xtol=1e-6)
    if lowest(theta_max) > 0.0:
        return np.nan
    return brentq(lowest, 0.0, theta_max, xtol=1e-6)


def _build_toes(local: np.ndarray, mask: np.ndarray, h: np.ndarray, L: float, medial_sign: float,
                res: float, theta_max: float) -> ToeModel:
    """Localiza los cinco dedos y sus ejes MTF y calcula su asentamiento por gravedad."""
    x = local[..., 0] * medial_sign              # positivo hacia medial
    y = local[..., 1]
    shape = mask.shape
    none = np.zeros(0, dtype=np.int64)
    empty = ToeModel((none, none), np.zeros((0, 5)), np.zeros(0), np.zeros(0), np.zeros(5), np.zeros(5),
                     np.zeros(5), np.zeros(5, bool), np.zeros(5, bool), np.full(shape, -1), L, 0, theta_max)
    axis_of = _forefoot_lines(x, y, np.where(mask, h, np.inf), mask, L)
    if axis_of is None:
        return empty
    ya = axis_of(x)
    zone = mask & (y > ya + MTP_TO_SULCUS * L)
    if zone.sum() * res * res < 300.0:            # menos de 3 cm² de dedos: escaneo sin dedos
        return empty
    hh = np.where(mask, h, 0.0)
    # --- separación de los dedos: perfil mediolateral del giro necesario para tocar el suelo.
    # Los pulpejos (más prominentes) necesitan menos giro que los espacios interdigitales.
    xz = x[zone]
    ang = np.arctan2(hh[zone], np.maximum(y[zone] - ya[zone], 1.0))
    x0, x1 = float(xz.min()), float(xz.max())
    nb = max(int(np.ceil(x1 - x0)), 5)
    b = np.clip(((xz - x0) / (x1 - x0 + 1e-9) * nb).astype(int), 0, nb - 1)
    prof = np.full(nb, np.inf)
    np.minimum.at(prof, b, ang)
    xb = x0 + (np.arange(nb) + 0.5) * (x1 - x0) / nb
    have = np.isfinite(prof)
    prof_f = np.interp(xb, xb[have], prof[have])
    prof_f = np.where(have, prof_f, prof_f.max() + np.radians(10.0))      # huecos sin superficie
    prof_s = ndimage.gaussian_filter1d(prof_f, 1.5)
    peaks = [i for i in range(1, nb - 1) if prof_s[i] > prof_s[i - 1] and prof_s[i] >= prof_s[i + 1]
             and prof_s[i] - max(prof_s[:i].min(), prof_s[i + 1:].min()) > np.radians(0.75)]
    # límites por proporciones anatómicas (de medial a lateral: x decreciente) …
    bounds = x1 - np.cumsum(TOE_WIDTH_SHARE)[:-1] * (x1 - x0)
    sharp = np.zeros(4, bool)
    # … ajustados al hueco interdigital más cercano que muestre la geometría
    for k, bx in enumerate(bounds):
        cand = [xb[i] for i in peaks if abs(xb[i] - bx) <= 0.06 * (x1 - x0) + 2.0]
        if cand:
            bounds[k] = min(cand, key=lambda c: abs(c - bx))
            sharp[k] = True
    order = np.argsort(-bounds)
    bounds, sharp = bounds[order], sharp[order]
    # pertenencia suave (piel continua) salvo donde hay hueco visible
    width = np.where(sharp, 0.25, 2.5)                                    # mm
    u = 1.0 / (1.0 + np.exp(-np.clip((bounds[None, :] - xz[:, None]) / width[None, :], -60, 60)))
    W = np.empty((len(xz), 5))
    W[:, 0] = 1.0 - u[:, 0]
    W[:, 1:4] = u[:, :-1] - u[:, 1:]
    W[:, 4] = u[:, 3]
    W = np.maximum(W, 0.0)
    W /= W.sum(axis=1, keepdims=True)
    cells = np.nonzero(zone)
    yz, hz = y[zone], hh[zone]
    main = np.argmax(W, axis=1)
    present = np.bincount(main, minlength=5)[:5] >= 3
    axis_y = np.zeros(5)
    z_axis = np.zeros(5)
    for t in range(5):
        sel = main == t
        xm = float(np.median(xz[sel])) if sel.any() else float(np.mean(bounds))
        axis_y[t] = float(axis_of(np.array([xm]))[0])
        # altura del eje: sobre la almohadilla de la cabeza metatarsiana de este dedo
        ball = mask & (np.abs(x - xm) < 5.0) & (np.abs(y - axis_y[t]) < 6.0)
        z_axis[t] = (float(np.median(hh[ball])) if ball.any() else 0.0) + TOE_AXIS_HEIGHT
    cell_toe = np.full(shape, -1)
    cell_toe[cells] = main
    tm = ToeModel(cells, W, yz, hz, axis_y, z_axis, np.zeros(5), np.zeros(5, bool), present, cell_toe, L,
                  int(sharp.sum()), theta_max)
    # Asentamiento por gravedad: cada dedo gira hasta que su punto más bajo toca el suelo (los
    # vecinos quedan en su postura del escaneo mientras se busca).
    rest = np.zeros(5)
    for t in range(5):
        if not present[t]:
            continue
        own = W[:, t] > 0.5

        def lowest(a, t=t, own=own):
            hj = z_axis[t] - (yz[own] - axis_y[t]) * np.sin(a) + (hz[own] - z_axis[t]) * np.cos(a)
            return float(hj.min())
        r = _settle_angle(lowest, theta_max)
        tm.reach[t] = bool(np.isfinite(r))
        rest[t] = r if np.isfinite(r) else theta_max
    tm.rest = rest
    return tm


def _regions(local: np.ndarray, mask: np.ndarray, L: float, medial_sign: float, toes: ToeModel):
    """Máscara anatómica en coordenadas del pie (dedos según el modelo articulado)."""
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
    reg = np.full(sn.shape, -1, dtype=np.int64)
    idx = {k: i for i, k in enumerate(REGION_KEYS)}
    heel = sn < 0.30
    reg[heel & (tml < 0.5)] = idx["talon_medial"]
    reg[heel & (tml >= 0.5)] = idx["talon_lateral"]
    reg[(sn >= 0.30) & (sn < 0.60)] = idx["mediopie"]
    tm = toes.cell_toe >= 0
    if toes.active:
        fore = (sn >= 0.60) & ~tm
    else:
        fore = (sn >= 0.60) & (sn < 0.80)
    reg[fore & (tml < 0.33)] = idx["met1"]
    reg[fore & (tml >= 0.33) & (tml < 0.67)] = idx["met23"]
    reg[fore & (tml >= 0.67)] = idx["met45"]
    if toes.active:
        reg[toes.cell_toe == 0] = idx["hallux"]
        reg[toes.cell_toe > 0] = idx["dedos"]
    else:
        reg[(sn >= 0.80) & (tml < 0.38)] = idx["hallux"]
        reg[(sn >= 0.80) & (tml >= 0.38)] = idx["dedos"]
    reg[~mask] = -1
    return reg, tml


ARCH_HALF_SPAN = 0.25           # fracción de la longitud por detrás del ápice con descenso del arco
# Descenso de la columna lateral respecto al del arco medial: en TC en carga la distancia
# cuboides-suelo baja 5,3 mm cuando la navicular-suelo baja 15 mm (≈ 0,35).
LATERAL_COLUMN_RATIO = 0.35


def measure_arch(sn: np.ndarray, tml: np.ndarray, h: np.ndarray, sel: np.ndarray, L: float) -> dict:
    """Medidas del arco en el escaneo sin carga, con el pie apoyado en su plano de apoyo.

    * Altura del arco: la del borde medial de la planta (tercio medial de la anchura) en su punto
      más alto del mediopié; se da en mm y en % de la longitud del pie.
    * Ápice: posición de ese punto (fracción de la longitud desde el centro del talón); bajo él está
      el navicular.
    * Holgura lateral: altura del borde lateral en el mediopié (cuboides, base del 5.º MTT). Si es
      de pocos mm, el borde lateral apoya enseguida y el mediopié carga más.
    """
    edges = np.arange(0.15, 0.66, 0.025)
    med = sel & (tml > 0.03) & (tml < 0.33)
    lat = sel & (tml > 0.70) & (tml < 0.97)
    prof_m, prof_l, centers = [], [], []
    for a, b in zip(edges[:-1], edges[1:]):
        m = med & (sn >= a) & (sn < b)
        q = lat & (sn >= a) & (sn < b)
        if m.sum() < 3 or q.sum() < 3:
            continue
        centers.append(0.5 * (a + b))
        prof_m.append(float(np.median(h[m])))
        prof_l.append(float(np.min(h[q])))
    if len(centers) < 4:
        return {"apex_frac": 0.40, "height_mm": None, "height_pct": None, "lateral_clearance_mm": None,
                "lateral_apex_frac": 0.33, "lateral_height_mm": None, "measured": False}
    c, pm, pl = np.array(centers), np.array(prof_m), np.array(prof_l)
    win = (c >= 0.22) & (c <= 0.58)
    i = int(np.flatnonzero(win)[np.argmax(pm[win])]) if win.any() else int(np.argmax(pm))
    apex = float(np.clip(c[i], 0.28, 0.52))
    mid = (c >= 0.35) & (c <= 0.58)
    lat_clear = float(np.median(pl[mid])) if mid.any() else float(np.median(pl))
    wl = (c >= 0.18) & (c <= 0.50)
    j = int(np.flatnonzero(wl)[np.argmax(pl[wl])]) if wl.any() else int(np.argmax(pl))
    return {"apex_frac": apex, "height_mm": float(pm[i]), "height_pct": float(100.0 * pm[i] / L),
            "lateral_clearance_mm": lat_clear, "lateral_apex_frac": float(np.clip(c[j], 0.20, 0.50)),
            "lateral_height_mm": float(pl[j]), "measured": True,
            "profile": {"frac": c.round(3).tolist(), "medial_mm": pm.round(2).tolist(),
                        "lateral_min_mm": pl.round(2).tolist()}}


def build_field(foot: FootGeometry, pl: Placement, grid: Grid, tissue: dict,
                toe_max_flexion_deg: float = 50.0) -> FootField:
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
    # Dedos: cada uno es un segmento rígido que gira alrededor de su articulación MTF. Asentamiento
    # por gravedad (y tono de los flexores): cada dedo gira hasta que su pulpejo toca el suelo.
    toes = _build_toes(local, mask, h, foot.length, foot.medial_sign, grid.res,
                       np.radians(max(toe_max_flexion_deg, 0.0)))
    reg, tml = _regions(local, mask, foot.length, foot.medial_sign, toes)
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
    # Arco longitudinal interno medido en el escaneo (sin carga): perfil de altura del borde medial
    # a lo largo del pie. El descenso con la carga es máximo en su ápice (bajo el navicular) y nulo
    # en la columna lateral (calcáneo-cuboides-4.º/5.º MTT), mucho más rígida.
    arch = measure_arch(sn, tml, hh, mask & (toes.cell_toe < 0), foot.length)
    # El arco va del apoyo del talón a la 1.ª cabeza metatarsiana (eje MTF del hallux), con el
    # máximo en el ápice medido; el descenso sigue esa misma forma.
    a0 = arch["apex_frac"]
    back = max(0.08, a0 - ARCH_HALF_SPAN)
    front = (float(toes.axis_y[0]) / foot.length - 0.03) if toes.active and toes.present[0] else 0.66
    front = float(np.clip(front, a0 + 0.12, 0.75))
    d = np.where(sn < a0, (sn - a0) / (a0 - back), (sn - a0) / (front - a0))
    bx = np.where(np.abs(d) < 1.0, np.cos(0.5 * np.pi * d) ** 2, 0.0)
    arch["front_frac"], arch["back_frac"] = front, back
    med = np.clip((0.6 - tml) / 0.5, 0.0, 1.0)
    medial_shape = bx * med * med * (3 - 2 * med)
    # Columna lateral (calcáneo-cuboides-5.º MTT): también desciende con la carga, pero menos que la
    # medial (≈ 0,35 veces: distancia cuboides-suelo frente a navicular-suelo en TC en carga). Va
    # del apoyo del talón a la 5.ª cabeza metatarsiana, con el máximo bajo el punto más alto del
    # borde lateral medido en el escaneo. Es la que forma la banda lateral de apoyo del mediopié que,
    # en una huella estática normal, une el talón con el antepié.
    la = arch["lateral_apex_frac"]
    lback = max(0.06, la - 0.22)
    lfront = (float(toes.axis_y[4]) / foot.length - 0.03) if toes.active and toes.present[4] else 0.60
    lfront = float(np.clip(lfront, la + 0.12, 0.72))
    dl = np.where(sn < la, (sn - la) / (la - lback), (sn - la) / (lfront - la))
    bl = np.where(np.abs(dl) < 1.0, np.cos(0.5 * np.pi * dl) ** 2, 0.0)
    latw = np.clip((tml - 0.35) / 0.5, 0.0, 1.0)
    lateral_shape = LATERAL_COLUMN_RATIO * bl * latw * latw * (3 - 2 * latw)
    arch["lateral_front_frac"], arch["lateral_back_frac"] = lfront, lback
    arch_shape = np.where(mask & (toes.cell_toe < 0), np.maximum(medial_shape, lateral_shape), 0.0)
    mtp_line = float(np.mean(toes.axis_y[toes.present])) / foot.length if toes.active else 0.73
    toe_line = mtp_line + MTP_TO_SULCUS if toes.active else 0.80
    return FootField(foot.declared_side, mask, h, np.maximum(th, 1.0), e0, reg, local, toe_line,
                     np.maximum(be, 0.1), toes, arch_shape, mtp_line, arch)


def _base_height(ff: FootField, arch_drop: float) -> np.ndarray:
    """Altura sin carga tras el descenso del arco (sin el giro del pie ni el de los dedos)."""
    h = ff.h
    if arch_drop:
        drop = np.minimum(arch_drop * ff.arch_shape, np.maximum(np.where(ff.mask, h, 0.0), 0.0))
        h = h - drop
    return h


TOE_GROUPS = (np.array([True, False, False, False, False]), np.array([False, True, True, True, True]))
TOE_PRESS_RANGE = (-30.0, 15.0)        # mm
# Compresión máxima del pulpejo al apretar el dedo: más allá, el dedo se flexiona en las
# articulaciones interfalángicas en vez de aplastar el pulpejo contra el hueso.
TOE_MAX_STRAIN = 0.59


def _toe_base(ff: FootField, w: float, tp: float, tr: float, pivot: np.ndarray):
    """Aproximación (mm) de cada celda de los dedos asentados, antes del apoyo, y sus constantes."""
    T = ff.toes
    ci = T.cells
    lift = tp * (ff.local[..., 1][ci] - pivot[1]) + tr * (ff.local[..., 0][ci] - pivot[0])
    h0, arm = T.settled()
    return w - h0 + lift, arm, ff.thick[ci], ff.e0[ci], ff.beta[ci]


def _toe_group_moments(ff: FootField, w, tp, tr, pivot, press, a_cell, per_toe: bool = False):
    T = ff.toes
    base, arm, th_c, e0, be = _toe_base(ff, w, tp, tr, pivot)
    p = _law(np.maximum(base + T.press_cells(press), 0.0) / th_c, e0, be)
    m = ((p[:, None] * arm) * T.weight).sum(axis=0) * a_cell
    if per_toe:
        return m
    return np.array([m[TOE_GROUPS[0]].sum(), m[TOE_GROUPS[1]].sum()])


def _solve_toes(ff: FootField, w: float, tp: float, tr: float, pivot: np.ndarray, torque: np.ndarray,
                a_cell: float) -> np.ndarray:
    """Apoyo de los dedos para el equilibrio de momentos alrededor de sus ejes MTF.

    Cada dedo ya está asentado (girado en su MTF hasta tocar el suelo). Bajo carga, el momento
    flexor lo aprieta contra el suelo: las articulaciones interfalángicas y el pulpejo blando hacen
    que el dedo se aplaste sobre el suelo en vez de clavar la punta, así que el apoyo se modela como
    un desplazamiento hacia el suelo del dedo asentado. Dos grupos musculares: el hallux (flexor
    largo del hallux) y los dedos 2.º–5.º (flexores largo y corto de los dedos y fascia plantar). El
    desplazamiento de cada grupo hace que el momento de la reacción del suelo bajo el grupo iguale
    su momento flexor; el reparto entre los dedos del grupo resulta de su geometría.
    """
    T = ff.toes
    tq = np.array([torque[TOE_GROUPS[0]].sum(), torque[TOE_GROUPS[1]].sum()])
    has = np.array([(T.present & T.reach & g).any() for g in TOE_GROUPS]) & (tq > 0)
    lo, hi = TOE_PRESS_RANGE
    press = np.where(has, 0.0, lo)
    base, arm, th_c, e0, be = _toe_base(ff, w, tp, tr, pivot)
    wh = T.weight[:, 0]
    aw = arm * T.weight
    garm = (aw[:, TOE_GROUPS[0]].sum(axis=1) * a_cell, aw[:, TOE_GROUPS[1]].sum(axis=1) * a_cell)
    # solo las celdas con peso en el grupo cuentan para su momento
    for _ in range(4):                    # Gauss-Seidel: el hallux y el 2.º dedo comparten piel
        prev = press.copy()
        for g in (0, 1):
            if not has[g]:
                continue
            other = press[1 - g] * ((1.0 - wh) if g == 0 else wh)
            mine = wh if g == 0 else (1.0 - wh)
            sel = garm[g] != 0
            b0 = base[sel] + other[sel]
            mg, ag, tg, eg, bg = mine[sel], garm[g][sel], th_c[sel], e0[sel], be[sel]

            def f(x):
                return float((_law(np.maximum(b0 + mg * x, 0.0) / tg, eg, bg) * ag).sum()) - tq[g]
            # límite de compresión del pulpejo: sobre el 3 % de celdas más comprimidas del dedo, no
            # sobre una sola celda (sería sensible a la rejilla)
            own = (mg >= 0.5) & (b0 + mg * hi > 0)
            hi_g = min(hi, float(np.percentile((TOE_MAX_STRAIN * tg[own] - b0[own]) / mg[own], 3))) if own.any() else hi
            hi_g = max(hi_g, lo)
            fl, fh = f(lo), f(hi_g)
            if fh <= 0:
                press[g] = hi_g
            elif fl >= 0:
                press[g] = lo
            else:
                press[g] = brentq(f, lo, hi_g, xtol=1e-5)
        if np.abs(press - prev).max() < 1e-4:
            break
    return press


def _toe_moments(ff: FootField, w, tp, tr, pivot, press, a_cell) -> np.ndarray:
    """Momento (N·mm) de la reacción del suelo bajo cada dedo respecto a su eje MTF (sin reparto lateral)."""
    return _toe_group_moments(ff, w, tp, tr, pivot, press, a_cell, per_toe=True)


def _pressure(ff: FootField, w: float, tp: float, tr: float, pivot: np.ndarray, spread_px: float,
              toe_torque: np.ndarray | None = None, arch_drop: float = 0.0, a_cell: float = 1e-3,
              return_press: bool = False):
    """Presión para una configuración del pie.

    * ``w``: aproximación vertical del pie; ``tp`` (cabeceo) baja la parte anterior y ``tr``
      (alabeo) el lado +X local, alrededor de ``pivot`` (línea de carga, coords. locales).
    * ``toe_torque``: momento flexor (N·mm) de cada dedo; el apoyo de cada grupo de dedos se resuelve
      para el equilibrio de momentos alrededor de los ejes MTF. Sin él, los dedos quedan asentados.
    * ``arch_drop``: descenso del arco (mm); el esqueleto no atraviesa el suelo: el descenso local
      se limita a la holgura que queda hasta tocarlo: el arco baja hasta apoyar, y la compresión
      posterior del tejido la produce la carga, no el descenso.
    """
    h = _base_height(ff, arch_drop)
    if ff.toes.active:
        press = np.zeros(2) if toe_torque is None else _solve_toes(ff, w, tp, tr, pivot, toe_torque, a_cell)
        h = ff.toes.field(h, press)
    else:
        press = np.zeros(2)
    if tp or tr:
        h = h - tp * (ff.local[..., 1] - pivot[1]) - tr * (ff.local[..., 0] - pivot[0])
    delta = np.where(ff.mask, np.maximum(w - h, 0.0), 0.0)
    p = _law(delta / ff.thick, ff.e0, ff.beta)
    if spread_px > 0:
        p = ndimage.gaussian_filter(p, spread_px, mode="constant")
    p = np.where(ff.mask, p, 0.0)
    return (p, press) if return_press else p


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
                side: str, arch_drop: float, toe_flex: float = 0.0) -> dict:
    """Equilibrio de un pie: segmento principal rígido + cinco dedos articulados + arco que desciende.

    Incógnitas: aproximación w, cabeceo, alabeo y el giro de cada dedo. Ecuaciones: fuerza total,
    los dos momentos que sitúan el centro de presiones en la línea de carga y, para cada dedo, el
    equilibrio de momentos alrededor de su eje MTF (reacción del suelo bajo el dedo = momento
    flexor que le corresponde). Los giros de los dedos se resuelven dentro de cada evaluación.
    """
    a_cell = grid.res ** 2 * 1e-3
    hmin = float(np.min(ff.h[ff.mask]))
    pivot = target_cop
    # Sin momento flexor (toe_flex = 0) los dedos quedan libres: el suelo los levanta hasta que
    # dejan de cargar. Con él, cada grupo aprieta hasta equilibrarlo.
    torque = np.where(ff.toes.present, TOE_TORQUE_SHARE * max(toe_flex, 0.0), 0.0) if ff.toes.active else None

    def press(w, tp, tr, with_toes=False):
        return _pressure(ff, w, tp, tr, pivot, spread_px, torque, arch_drop, a_cell, with_toes)

    def F_of(w, tp, tr):
        return press(w, tp, tr).sum() * a_cell

    def w_for(tp, tr, F):
        lo_w = hmin - 300.0 * (abs(tp) + abs(tr)) - arch_drop - 1.0
        while F_of(lo_w, tp, tr) > 0:
            lo_w -= 5.0
            if hmin - lo_w > 400:
                raise BilateralError(f"Pie {side}: no se alcanza el equilibrio de fuerzas (dedos).")
        hi_w = hmin + 1.0
        while F_of(hi_w, tp, tr) < F:
            hi_w += max(1.0, hi_w - lo_w)
            if hi_w - hmin > 300:
                raise BilateralError(f"Pie {side}: no se alcanza el equilibrio de fuerzas.")
        return brentq(lambda w: F_of(w, tp, tr) - F, lo_w, hi_w, xtol=1e-8, rtol=1e-12)

    def resid(x):
        tp, tr = x
        w = w_for(tp, tr, target_F)
        _, c = _local_cop(ff, press(w, tp, tr))
        return (c - target_cop) / 5.0

    sol = least_squares(resid, np.zeros(2), x_scale=np.full(2, 0.02), bounds=([-0.35, -0.35], [0.35, 0.35]),
                        xtol=1e-12, ftol=1e-12, gtol=1e-12, max_nfev=300, diff_step=1e-4)
    tp, tr = float(sol.x[0]), float(sol.x[1])
    w = w_for(tp, tr, target_F)
    p, toe_press = press(w, tp, tr, True)
    F, c = _local_cop(ff, p)
    err = float(np.linalg.norm(c - target_cop))
    if err > 1.0:
        raise BilateralError(
            f"Pie {side}: no existe equilibrio de momentos con la línea de carga indicada (el centro de "
            f"presiones queda a {err:.1f} mm). Acérquela al centro de la huella.")
    toe_m = _toe_moments(ff, w, tp, tr, pivot, toe_press, a_cell) if ff.toes.active else np.zeros(5)
    req = torque if torque is not None else np.zeros(5)
    floating = ff.toes.present & ~ff.toes.reach
    toe_eq = np.zeros(2)
    capped = np.zeros(2, bool)
    if ff.toes.active:
        base, _, th_c, _, _ = _toe_base(ff, w, tp, tr, pivot)
        strain = (base + ff.toes.press_cells(toe_press)) / th_c
        wh = ff.toes.weight[:, 0]
        for k, own in enumerate((wh >= 0.5, wh < 0.5)):
            own = own & (strain > 0)
            capped[k] = bool(own.any() and np.percentile(strain[own], 97) >= TOE_MAX_STRAIN - 1e-6)
    for k, g in enumerate(TOE_GROUPS):
        if (ff.toes.present & ff.toes.reach & g).any() and not capped[k]:
            toe_eq[k] = toe_m[g].sum() - req[g].sum()
    return {"w": w, "pitch": tp, "roll": tr, "press": toe_press, "p": p, "pivot": pivot,
            "cop_error_mm": err, "toe_moments_Nmm": toe_m, "toe_torque_Nmm": req,
            "toe_equilibrium_error_Nmm": float(np.abs(toe_eq).max()), "toe_floating": floating,
            "toe_capped": capped,
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
    fields = {s: build_field(feet[s], placements[s], grid, sp.tissue_for(s), sp.toe_max_flexion) for s in SIDES}
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
    # descenso del arco con medio peso por pie: el indicado o, si no, el típico para la longitud de
    # cada pie (la caída del navicular crece con ella); proporcional a la carga del pie
    drop_half = {sd: (float(sp.arch_drop) if sp.arch_drop is not None and sp.arch_drop >= 0
                      else arch_drop_for_length(feet[sd].length)) for sd in SIDES}
    drops = {sd: (0.0 if sp.loaded_scan else drop_half[sd] * targets[sd] / (0.5 * W)) for sd in SIDES}
    warnings.extend(scale_check(feet, sp.height_cm))
    flex = {sd: sp.toe_flexor * targets[sd] / (0.5 * W) for sd in SIDES}
    sols = {sd: _solve_foot(fields[sd], grid, targets[sd], cops[sd], spread_px, sd, drops[sd], flex[sd])
            for sd in SIDES}
    for sd in SIDES:
        sols[sd]["arch_drop_source"] = ("escaneo en carga" if sp.loaded_scan else
                                        "indicado por el usuario" if sp.arch_drop is not None and sp.arch_drop >= 0
                                        else "según la longitud del pie (Nielsen et al., 2009)")
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
    toes = ff.toe_mask if ff.toes.active else (sn >= ff.toe_line)
    no_toes = contact & ~toes
    tl = float(np.max(sn[no_toes])) if no_toes.any() else ff.toe_line
    s0 = float(np.min(sn[no_toes])) if no_toes.any() else 0.0
    d3 = (tl - s0) / 3.0
    third = [(no_toes & (sn >= lo) & (sn < hi)).sum() for lo, hi in
             ((-1, s0 + d3), (s0 + d3, s0 + 2 * d3), (s0 + 2 * d3, 2))]
    arch_index = float(third[1] / max(sum(third), 1))
    # altura efectiva bajo carga (arco, dedos girados, cabeceo y alabeo) para la compresión del tejido
    h_eff = ff.toes.field(_base_height(ff, sol["arch_drop"]), sol["press"])
    h_eff = (h_eff - sol["pitch"] * (ff.local[..., 1] - sol["pivot"][1])
             - sol["roll"] * (ff.local[..., 0] - sol["pivot"][0]))
    eps = np.where(ff.mask, np.maximum(w - h_eff, 0.0) / ff.thick, 0.0)
    toe_contact = float((contact & toes).sum() * a_cell) / 100.0
    # Asentamiento por gravedad (antes de cargar): zonas que tocan el suelo con el pie apoyado
    # y cada dedo girado hasta apoyar su pulpejo, sin compresión del tejido.
    h_settle = np.where(ff.mask, ff.toes.field(np.where(ff.mask, ff.h, 0.0), np.zeros(2)), np.inf)
    touch = ff.mask & (h_settle <= 0.5)
    initial = [REGION_NAMES[k] for i, k in enumerate(REGION_KEYS) if (touch & (ff.region == i)).sum() * a_cell >= 4.0]
    T = ff.toes
    toe_list = []
    if T.active:
        for i in range(5):
            c = T.cell_toe == i
            if not c.any():
                continue
            Fi = float(p[c].sum() * a_cell * 1e-3)
            toe_list.append({
                "index": i + 1, "name": TOE_NAMES[i], "force_N": Fi, "pct_foot": 100.0 * Fi / F if F > 0 else 0.0,
                "contact_area_cm2": float((c & contact).sum() * a_cell) / 100.0,
                "peak_kPa": float(p[c].max()), "touches": bool((c & contact).any()),
                "reaches_ground": bool(T.reach[i]),
                "rest_deg": float(np.degrees(T.rest[i])),
                "press_mm": float(sol["press"][0 if i == 0 else 1]),
                "flexor_moment_Nmm": float(sol["toe_torque_Nmm"][i]),
                "ground_moment_Nmm": float(sol["toe_moments_Nmm"][i]),
                "mtp_axis_pct": float(100.0 * T.axis_y[i] / foot.length), "max_strain_pct": float(100.0 * eps[c].max()),
            })
    rests = [t["rest_deg"] for t in toe_list]
    return {
        "side": foot.declared_side, "force_N": F, "pct_body": 100.0 * F / W,
        "contact_area_cm2": area, "mean_kPa": float(F / (area * 1e-4) / 1e3) if area > 0 else 0.0,
        "peak_kPa": float(p.max()), "cop_world_mm": cop.tolist(),
        "cop_local_mm": cop_local.tolist(), "cop_from_heel_pct": float(100.0 * cop_local[1] / foot.length),
        # desde el punto más posterior del talón, como en la literatura de bipedestación (31–58 %)
        "cop_from_heel_edge_pct": float(100.0 * (cop_local[1] - ff.local[..., 1][ff.mask].min()) / foot.length),
        "max_compression_mm": float((eps * ff.thick).max()), "max_tissue_strain_pct": float(100.0 * eps.max()),
        "region_strain_pct": {k: float(100.0 * eps[ff.region == i].max()) if (ff.region == i).any() else 0.0
                              for i, k in enumerate(REGION_KEYS)},
        "pitch_deg": float(np.degrees(sol["pitch"])),
        "toe_rest_deg": float(np.mean(rests)) if rests else 0.0,
        "initial_support_regions": initial,
        "toe_press_mm": [float(v) for v in sol["press"]],
        "toe_contact_area_cm2": toe_contact,
        "toes_in_contact": int(sum(t["touches"] for t in toe_list)),
        "toes": toe_list, "toes_detected_by_geometry": int(T.detected) if T.active else 0,
        "toe_equilibrium_error_Nmm": float(sol["toe_equilibrium_error_Nmm"]),
        "toes_pct": float(sum(r["pct_foot"] for r in regions if r["key"] in ("hallux", "dedos"))),
        "midfoot_pct": float(sum(r["pct_foot"] for r in regions if r["key"] == "mediopie")),
        "arch_drop_mm": float(sol["arch_drop"]),
        "lateral_column_drop_mm": float(sol["arch_drop"] * LATERAL_COLUMN_RATIO),
        **_footprint_continuity(ff, contact, sn, grid.res, foot.length),
        "arch_drop_source": sol.get("arch_drop_source", ""),
        "arch": {k: v for k, v in ff.arch.items()},
        "footprint_type": _footprint_type(arch_index),
        "mtp_line_pct": 100.0 * ff.mtp_line,
        "roll_deg": float(np.degrees(sol["roll"]) * foot.medial_sign),
        "load_line_local_mm": np.asarray(sol["pivot"]).tolist(), "cop_error_mm": sol["cop_error_mm"],
        "regions": regions, "arch_index": arch_index, "toe_line_pct": 100.0 * ff.toe_line,
        "length_mm": foot.length, "width_mm": foot.width,
        "rearfoot_pct": float(sum(r["pct_foot"] for r in regions if r["key"].startswith("talon"))),
        "forefoot_pct": float(sum(r["pct_foot"] for r in regions if r["key"] in ("met1", "met23", "met45"))),
    }


def _footprint_continuity(ff: FootField, contact: np.ndarray, sn: np.ndarray, res: float, L: float) -> dict:
    """¿Une la huella (sin dedos) el talón con el antepié? Y anchura mínima de la banda del mediopié."""
    sole = contact & ~ff.toe_mask
    lab, _ = ndimage.label(sole, structure=np.ones((3, 3)))
    heel = sole & (sn < 0.12)
    fore = sole & (sn > 0.55) & (sn < ff.mtp_line)
    joined = bool((set(np.unique(lab[heel])) - {0}) & (set(np.unique(lab[fore])) - {0}))
    step = 0.025
    wmm = [float((sole & (sn >= a) & (sn < a + step)).sum() * res * res / (step * L))
           for a in np.arange(0.15, 0.55, step)]
    return {"footprint_continuous": joined, "midfoot_band_min_width_mm": min(wmm) if wmm else 0.0}


def _footprint_type(ai: float) -> str:
    """Tipo de huella según el índice del arco de Cavanagh y Rodgers (1987)."""
    if ai <= 0.21:
        return "arco alto (índice ≤ 0,21)"
    if ai < 0.26:
        return "arco normal (0,21–0,26)"
    return "arco bajo (índice ≥ 0,26)"


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
        for t in per[s]["toes"]:
            if not t["reaches_ground"]:
                warnings.append(f"Pie {s}: el {t['name'].lower()} no llega al suelo ni girando "
                                f"{sp.toe_max_flexion:.0f}° desde la postura del escaneo (dedo muy levantado o en "
                                "garra): queda sin apoyo.")
            elif not t["touches"]:
                warnings.append(f"Pie {s}: el {t['name'].lower()} apenas carga (por debajo del umbral de contacto).")
        for name, g in (("el hallux", 0), ("los dedos 2.º–5.º", 1)):
            sol = sols[s]
            req = float(sol["toe_torque_Nmm"][TOE_GROUPS[g]].sum())
            got = float(sol["toe_moments_Nmm"][TOE_GROUPS[g]].sum())
            if req > 0 and got < 0.9 * req and (fields[s].toes.present & fields[s].toes.reach & TOE_GROUPS[g]).any():
                warnings.append(f"Pie {s}: {name} solo apoya con el {100 * got / req:.0f} % del momento flexor previsto: "
                                "su superficie de apoyo en el escaneo es pequeña (¿dedo poco capturado o en garra?).")
        if per[s]["toes"] and max(t["rest_deg"] for t in per[s]["toes"]) > 30:
            warnings.append(f"Pie {s}: algún dedo ha tenido que girar más de 30° para apoyar: en el escaneo los "
                            "dedos estaban muy levantados. Compruebe que no se trata de dedos en garra.")
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
                            ("cop_from_heel_edge_pct", "CoP desde el borde del talón", "% longitud")):
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
