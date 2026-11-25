"""Pie en carga: expansión (alargamiento y ensanchamiento) y aplastamiento al pisar.

Al pasar de descarga a bipedestación el pie se alarga, se ensancha y el arco baja. Medias de tres
estudios con escáner 3D (Houston et al.; Tsung et al. 2003; Xiong et al. 2009, recopiladas en
Telfer y Woodburn, J Foot Ankle Res 2010, 3:19), en carga completa frente a descarga:

* longitud del pie: +1,95 % (estudios entre +1,4 % y +3,4 %);
* anchura del antepié (línea de las cabezas metatarsianas): +3,66 % (+2,6 % a +6 %);
* anchura del talón: +3,68 % (+1,6 % a +8,7 %).

La variación entre personas es grande (la almohadilla del talón, por ejemplo, se expande de media
unos 4,8 mm, con mucha dispersión). Por eso el modelo **no aplica las medias tal cual**: las escala con
las deformaciones que la simulación calcula para *este* pie y *este* peso:

* **Longitud**: la produce sobre todo el descenso del arco (la bóveda se aplana y se alarga). Se
  escala con el descenso del arco simulado frente al típico para esa longitud de pie, y con la carga
  absoluta del pie (la deformación crece con el peso de forma menos que proporcional, exponente 1/3:
  de media carga a carga completa los estudios dan ≈ 78 % de la expansión total).
* **Anchura del talón y del antepié**: la produce el tejido blando que se aplasta y se abomba hacia
  los lados (y, en el antepié, la apertura de los metatarsianos). Se escala con la compresión del
  tejido simulada en esa región (milímetros) frente a la de referencia (70 kg repartidos 50/50).
* **Aplastamiento**: la planta en carga es exactamente la de la simulación: donde toca el suelo queda
  plana sobre él y el resto baja con el arco, los dedos asentados y la compresión del tejido.

Con ello se genera el STL del pie en carga (expandido y aplastado), en el mismo marco que la
simulación (suelo z = 0). Es una estimación, no una medición.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .bilateral import DEFAULT_TISSUE, Mesh, _base_height, arch_drop_for_length

# Medias de la literatura (carga completa frente a descarga)
LIT_LENGTH = 0.0195
LIT_BALL_WIDTH = 0.0366
LIT_HEEL_WIDTH = 0.0368
F_REF = 0.5 * 70.0 * 9.80665          # N por pie en la referencia (70 kg, 50/50)
LOAD_EXP = 1.0 / 3.0
# Compresión de referencia del tejido (simulación a 70 kg, 50/50): deformación ≈ 46 % del espesor
STRAIN_REF = 0.46
WIDTH_EXP = 0.7


def _sigmoid_ramp(x, a, b):
    t = np.clip((x - a) / max(b - a, 1e-9), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def loaded_skin_height(ff, sol: dict, arch_drop: float) -> np.ndarray:
    """Altura de la piel sobre el suelo en carga (mm), en la rejilla de la simulación."""
    h = _base_height(ff, arch_drop)
    if ff.toes.active:
        h = ff.toes.field(h, sol["press"])
    tp, tr = sol["pitch"], sol["roll"]
    pv = sol["pivot"]
    if tp or tr:
        h = h - tp * (ff.local[..., 1] - pv[1]) - tr * (ff.local[..., 0] - pv[0])
    return np.where(ff.mask, np.maximum(h - sol["w"], 0.0), np.nan)


def _widths(ff, L: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Centro y semianchura de la huella sin carga por franjas a lo largo del pie (coords. locales)."""
    loc = ff.local[ff.mask]
    y, x = loc[:, 1], loc[:, 0]
    nb = 60
    edges = np.linspace(y.min(), y.max(), nb + 1)
    b = np.clip(np.digitize(y, edges) - 1, 0, nb - 1)
    lo = np.full(nb, np.inf)
    hi = np.full(nb, -np.inf)
    np.minimum.at(lo, b, x)
    np.maximum.at(hi, b, x)
    ok = np.isfinite(lo)
    yc = 0.5 * (edges[:-1] + edges[1:])
    mid = np.interp(yc, yc[ok], (0.5 * (lo + hi))[ok])
    half = np.interp(yc, yc[ok], (0.5 * (hi - lo))[ok])
    return yc, ndimage.gaussian_filter1d(mid, 2.0), np.maximum(ndimage.gaussian_filter1d(half, 2.0), 3.0)


def expansion(result: dict, side: str, foot, sp) -> dict:
    """Expansión estimada del pie en carga (mm y %), adaptada a la simulación de este pie."""
    arr = result["_arrays"]
    ff = arr["fields"][side]
    f = result["feet"][side]
    L = float(foot.length)
    yc, mid, half = _widths(ff, L)
    s = (yc - yc.min()) / max(yc.max() - yc.min(), 1e-9)
    w_heel = float(2 * half[(s > 0.08) & (s < 0.22)].max())
    w_ball = float(2 * half[(s > 0.60) & (s < 0.80)].max())
    tissue = (result.get("tissue") or {}).get(side) or DEFAULT_TISSUE
    st = f["region_strain_pct"]
    d_heel = max(st["talon_medial"] / 100 * tissue["talon_medial"]["thickness"],
                 st["talon_lateral"] / 100 * tissue["talon_lateral"]["thickness"])
    d_ff = max(st[k] / 100 * tissue[k]["thickness"] for k in ("met1", "met23", "met45"))
    d_heel_ref = STRAIN_REF * DEFAULT_TISSUE["talon_medial"]["thickness"]
    d_ff_ref = STRAIN_REF * DEFAULT_TISSUE["met23"]["thickness"]
    F = float(f["force_N"])
    drop = float(arr["drops"][side])
    drop_typ = arch_drop_for_length(L)
    loaded_scan = bool(getattr(sp, "loaded_scan", False))
    s_load = (max(F, 1.0) / F_REF) ** LOAD_EXP
    f_arch = (max(drop, 0.0) / drop_typ) ** 0.5 if drop_typ > 0 else 0.0
    f_heel = (max(d_heel, 0.0) / d_heel_ref) ** WIDTH_EXP
    f_ball = (max(d_ff, 0.0) / d_ff_ref) ** WIDTH_EXP
    if loaded_scan:                 # el escaneo ya se tomó en carga: no hay nada que expandir
        f_arch = f_heel = f_ball = 0.0
    dL = LIT_LENGTH * L * f_arch * s_load
    dW_heel = LIT_HEEL_WIDTH * w_heel * f_heel
    dW_ball = LIT_BALL_WIDTH * w_ball * f_ball
    return {
        "loaded_scan": loaded_scan,
        "unloaded": {"length_mm": L, "heel_width_mm": w_heel, "ball_width_mm": w_ball},
        "loaded": {"length_mm": L + dL, "heel_width_mm": w_heel + dW_heel, "ball_width_mm": w_ball + dW_ball},
        "change_mm": {"length": dL, "heel_width": dW_heel, "ball_width": dW_ball, "arch_drop": drop},
        "change_pct": {"length": 100 * dL / L, "heel_width": 100 * dW_heel / max(w_heel, 1e-9),
                       "ball_width": 100 * dW_ball / max(w_ball, 1e-9)},
        "drivers": {"foot_load_N": F, "load_factor": s_load, "arch_drop_mm": drop, "typical_arch_drop_mm": drop_typ,
                    "heel_compression_mm": d_heel, "forefoot_compression_mm": d_ff,
                    "heel_compression_ref_mm": d_heel_ref, "forefoot_compression_ref_mm": d_ff_ref},
        "literature_mean_pct": {"length": 100 * LIT_LENGTH, "ball_width": 100 * LIT_BALL_WIDTH,
                                "heel_width": 100 * LIT_HEEL_WIDTH},
        "_profile": (yc, mid, half),
    }


def loaded_mesh(result: dict, side: str, foot, placement, solid_world: Mesh, n_sole_faces: int,
                exp: dict) -> Mesh:
    """Sólido del pie en carga: planta aplastada según la simulación y huella expandida."""
    arr = result["_arrays"]
    grid = arr["grid"]
    ff = arr["fields"][side]
    sol = arr["sols"][side]
    g = loaded_skin_height(ff, sol, float(arr["drops"][side]))
    disp = np.where(ff.mask, ff.h - g, np.nan)
    # se prolonga fuera de la huella (paredes de la plantilla, bordes) con el valor más cercano
    bad = ~np.isfinite(disp)
    if bad.all():
        return solid_world
    _, (ii, jj) = ndimage.distance_transform_edt(bad, return_indices=True)
    disp = disp[ii, jj]
    disp = ndimage.gaussian_filter(disp, 0.8)
    V = solid_world.vertices.copy()
    F = solid_world.faces
    sole_v = np.unique(F[:n_sole_faces])
    d = grid.sample_bilinear(disp, V[sole_v, :2])
    d = np.where(np.isfinite(d), d, 0.0)
    V[sole_v, 2] = np.maximum(V[sole_v, 2] - d, 0.0)
    # expansión en planta, en el marco local del pie (talón en el origen, eje Y hacia los dedos)
    yc, mid, half = exp["_profile"]
    loc = placement.inverse_xy(V[:, :2])
    y, x = loc[:, 1], loc[:, 0]
    L = exp["unloaded"]["length_mm"]
    y0 = float(yc.min())
    s = (y - y0) / max(L, 1e-9)
    ch = exp["change_mm"]
    # alargamiento: el talón queda fijo; el arco se estira y todo lo anterior avanza ΔL
    y_new = y + ch["length"] * _sigmoid_ramp(s, 0.20, 0.70)
    # ensanchamiento: del talón al antepié, alrededor de la línea media de la huella
    dW = np.interp(s, [-1.0, 0.18, 0.35, 0.55, 0.72, 0.85, 2.0],
                   [ch["heel_width"], ch["heel_width"], 0.6 * ch["heel_width"] + 0.4 * ch["ball_width"],
                    0.3 * ch["heel_width"] + 0.7 * ch["ball_width"], ch["ball_width"], 0.8 * ch["ball_width"],
                    0.8 * ch["ball_width"]])
    m = np.interp(y, yc, mid)
    hh = np.interp(y, yc, half)
    x_new = m + (x - m) * (1.0 + 0.5 * dW / np.maximum(hh, 3.0))
    T = placement.matrix()
    V[:, :2] = np.stack([x_new, y_new], axis=1) @ T[:2, :2].T + T[:2, 3]
    return Mesh(V, F.copy())


def public(exp: dict) -> dict:
    return {k: v for k, v in exp.items() if not k.startswith("_")}
