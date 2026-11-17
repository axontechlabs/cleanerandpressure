"""Etapa 2: extracción de la superficie plantar.

Estrategia (en el marco canónico, +Z = normal exterior de la planta):

1. Normales suavizadas y ángulo de pendiente respecto al eje plantar (sin signo:
   no se confía en la orientación almacenada en el archivo).
2. Barreras de continuidad: aristas no variedad y pliegues (ángulo diedro alto).
3. Semillas (caras claramente plantares) agrupadas en islas; la isla principal es
   la más adelantada hacia el escáner entre las de área relevante.
4. Crecimiento de región por ondas desde la isla principal con histéresis de
   pendiente y una rejilla de ocupación que impide aceptar una segunda capa en la
   misma posición (capas superpuestas conectadas).
5. Todo lo que no pertenece a la región principal y cae dentro de su huella es una
   capa superpuesta o geometría por debajo/encima: se elimina.
6. Las islas plantares cercanas y coherentes en altura (dedos, zonas separadas por
   un surco o un agujero) se conservan como DUDOSAS para revisión manual.
7. Los huecos estrechos entre regiones conservadas se puentean con las caras reales
   visibles (surcos), no con interpolación.
8. Las correcciones manuales (conservar/eliminar) siempre prevalecen.

Etiquetas: 0 = eliminar, 1 = conservar, 2 = dudosa (se conserva y se avisa).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
from scipy import ndimage, sparse
from scipy.spatial import cKDTree

from .mesh import Mesh, build_topology, face_components
from .raster import make_grid, rasterize

REMOVE, KEEP, UNCERTAIN = 0, 1, 2

REASONS = {
    0: "planta principal",
    1: "isla dudosa conservada",
    2: "puente entre regiones",
    10: "pared o pendiente excesiva",
    11: "capa superpuesta",
    12: "geometría bajo/sobre la planta",
    13: "fragmento o superficie lejana",
    14: "isla demasiado pequeña",
    15: "oculta tras otra superficie",
    20: "conservada manualmente",
    21: "eliminada manualmente",
}


@dataclass
class SegmentationParams:
    seed_angle: float = 35.0        # º: pendiente máxima de las caras semilla
    max_angle: float = 62.0         # º: pendiente máxima aceptada al crecer
    crease_angle: float = 80.0      # º: ángulo diedro que actúa como barrera
    layer_tol: float = 1.5          # mm: separación a partir de la cual dos capas son distintas
    gap_tol: float = 6.0            # mm: distancia máxima isla–planta para conservarla como dudosa
    join_dz: float = 4.0            # mm: salto de altura máximo isla–planta
    bridge_gap: float = 5.0         # mm: anchura máxima de huecos que se puentean con caras reales
    min_island_area: float = 25.0   # mm²: islas menores se descartan
    normal_radius: float = 3.0      # mm: radio aproximado de suavizado de normales
    grid: float = 1.0               # mm: resolución de la rejilla de ocupación

    @classmethod
    def from_dict(cls, d: dict | None) -> "SegmentationParams":
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})


def _disk(radius_cells: float) -> np.ndarray:
    r = max(int(np.ceil(radius_cells)), 1)
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= radius_cells * radius_cells + 1e-9


def _grow(start, cand, adj, edge_ok, cell, zc, tol, occ, selected):
    """Crecimiento por ondas con rejilla de ocupación (una sola capa por celda)."""
    start = start[~selected[start]]
    selected[start] = True
    empty = np.isnan(occ[cell[start]])
    occ[cell[start][empty]] = zc[start][empty]
    frontier = start
    while len(frontier):
        nb = adj[frontier]
        c = np.unique(nb[edge_ok[frontier]])
        c = c[cand[c] & ~selected[c]]
        if len(c) == 0:
            break
        oz = occ[cell[c]]
        acc = np.isnan(oz) | (np.abs(oz - zc[c]) <= tol[c])
        c = c[acc]
        selected[c] = True
        cc = cell[c]
        empty = np.isnan(occ[cc])
        occ[cc[empty]] = zc[c][empty]
        frontier = c
    return selected


def segment_plantar(mesh: Mesh, params: SegmentationParams | None = None,
                    manual: np.ndarray | None = None, main_face: int | None = None) -> dict:
    """Segmenta la planta. ``mesh`` debe estar limpia y en el marco canónico (mm)."""
    p = params or SegmentationParams()
    nf = mesh.n_faces
    warnings: list[str] = []
    if nf == 0:
        raise ValueError("La malla no tiene caras.")
    if manual is None:
        manual = np.zeros(nf, dtype=np.uint8)
    forced_keep = manual == 1
    forced_remove = manual == 2

    topo = build_topology(mesh)
    n, area = mesh.face_normals()
    cen = mesh.centroids
    zc = cen[:, 2]
    adj = topo.adj
    man = adj >= 0
    sgn = np.where(topo.consistent, 1.0, -1.0)
    nn = n[np.maximum(adj, 0)]
    cosd = np.einsum("fk,fjk->fj", n, nn) * sgn
    edge_ok = man & (cosd >= np.cos(np.radians(p.crease_angle)))

    # --- normales suavizadas (coherentes con el sentido de giro propagado)
    rows = np.repeat(np.arange(nf), 3)[man.ravel()]
    cols = adj.ravel()[man.ravel()]
    w = sparse.csr_matrix((area[cols] * sgn.ravel()[man.ravel()], (rows, cols)), shape=(nf, nf))
    ns = n.copy()
    med_edge = float(np.median(np.linalg.norm(mesh.vertices[mesh.faces[:, 1]] - mesh.vertices[mesh.faces[:, 0]], axis=1)))
    n_iter = int(np.clip(np.ceil(p.normal_radius / max(med_edge, 1e-9)), 1, 25))
    for _ in range(n_iter):
        acc = area[:, None] * ns + w @ ns
        ns = acc / np.maximum(np.linalg.norm(acc, axis=1), 1e-300)[:, None]
    slope = np.abs(ns[:, 2])
    tan_s = np.minimum(np.sqrt(np.clip(1 - slope ** 2, 0, 1)) / np.maximum(slope, 1e-3), 3.0)

    cand = (slope >= np.cos(np.radians(p.max_angle))) & ~forced_remove
    seed = (slope >= np.cos(np.radians(p.seed_angle))) & cand

    # --- z-buffer superior (visibilidad desde la dirección plantar)
    r_close = max(p.bridge_gap / 2.0 / p.grid, 1.0)
    v = mesh.vertices
    grid = make_grid(v[:, :2].min(axis=0), v[:, :2].max(axis=0), p.grid, pad=int(np.ceil(r_close)) + 3)
    g = grid.res
    tri = mesh.triangles
    htop = rasterize(tri, grid)["zmax"]
    gi, gj = grid.index(cen[:, :2])
    cell = gi * grid.ny + gj
    tol = p.layer_tol + g * tan_s
    ht = htop[gi, gj]
    depth = np.where(np.isfinite(ht), ht - zc, 0.0)
    visible = depth <= tol

    # --- islas de semillas y elección de la principal
    nisl, lab = face_components(topo, nf, edge_ok=edge_ok, face_ok=seed)
    if nisl == 0:
        raise ValueError("No se han encontrado caras orientadas hacia la dirección plantar. "
                         "Revise la orientación (invertir lado plantar) o aumente el ángulo de semilla.")
    sl = lab[seed]
    isl_area = np.bincount(sl, weights=area[seed], minlength=nisl)
    isl_vis = np.bincount(sl, weights=area[seed] * visible[seed], minlength=nisl)
    isl_z = np.bincount(sl, weights=area[seed] * zc[seed], minlength=nisl) / np.maximum(isl_area, 1e-300)
    if main_face is not None and 0 <= main_face < nf:
        if lab[main_face] >= 0:
            main_isl = int(lab[main_face])
            start = np.flatnonzero(lab == main_isl)
        else:
            main_isl = -1
            start = np.array([main_face])
            cand[main_face] = True
    else:
        big = isl_vis >= 0.25 * isl_vis.max()
        main_isl = int(np.argmax(np.where(big, isl_z, -np.inf)))
        start = np.flatnonzero(lab == main_isl)
    if len(start) > 1:
        c0 = (cen[start, :2] * area[start, None]).sum(axis=0) / area[start].sum()
        start = start[[int(np.argmin(np.linalg.norm(cen[start, :2] - c0, axis=1)))]]

    occ = np.full(grid.nx * grid.ny, np.nan)
    main = _grow(start, cand, adj, edge_ok, cell, zc, tol, occ, np.zeros(nf, dtype=bool)).copy()
    main_sign = np.sign((area[main] * ns[main, 2]).sum()) or 1.0

    # --- huella de la planta principal: lo que cae dentro y no es principal se elimina
    rm = rasterize(tri[main], grid)
    foot = rm["count"] > 0
    zmain = np.where(foot, rm["zsum"] / np.maximum(rm["count"], 1), np.nan)
    in_foot = foot[gi, gj] & ~main
    dz_main = np.abs(zc - zmain[gi, gj])

    reason = np.full(nf, 13, dtype=np.uint8)
    reason[~cand] = 10
    reason[~visible & ~main] = 15
    reason[in_foot] = np.where(dz_main[in_foot] <= 3.0 * p.layer_tol + g * tan_s[in_foot], 11, 12)
    reason[main] = 0

    # --- islas de extensión (dudosas)
    _, comp_all = face_components(topo, nf)
    main_comp = np.zeros(comp_all.max() + 1, dtype=bool)
    main_comp[np.unique(comp_all[main])] = True
    same_side = np.where(main_comp[comp_all], np.sign(ns[:, 2]) == main_sign, True)
    ext_ok = seed & ~main & ~in_foot & visible & same_side
    labels = np.zeros(nf, dtype=np.uint8)
    labels[main] = KEEP
    n_uncertain = 0
    selected = main.copy()
    if ext_ok.any() and main.any():
        ne, elab = face_components(topo, nf, edge_ok=edge_ok, face_ok=ext_ok)
        ef = np.flatnonzero(ext_ok)
        e_area = np.bincount(elab[ef], weights=area[ef], minlength=ne)
        mv = np.unique(mesh.faces[main])
        tree = cKDTree(v[mv])
        fv = mesh.faces[ef]
        dist, idx = tree.query(v[fv.ravel()])
        dist = dist.reshape(-1, 3)
        dzv = np.abs(v[fv.ravel(), 2] - v[mv[idx], 2]).reshape(-1, 3)
        k = np.argmin(dist, axis=1)
        fd = dist[np.arange(len(ef)), k]
        fdz = dzv[np.arange(len(ef)), k]
        order = np.lexsort((fd, elab[ef]))
        first = order[np.r_[True, np.diff(elab[ef][order]) != 0]]
        isl_d = np.full(ne, np.inf)
        isl_dz = np.full(ne, np.inf)
        isl_d[elab[ef][first]] = fd[first]
        isl_dz[elab[ef][first]] = fdz[first]
        good = (e_area >= p.min_island_area) & (isl_d <= p.gap_tol) & (isl_dz <= p.join_dz)
        n_uncertain = int(good.sum())
        if n_uncertain:
            start2 = ef[good[elab[ef]]]
            cand2 = cand & ~in_foot & visible & ~main & same_side
            sel2 = _grow(start2, cand2, adj, edge_ok, cell, zc, tol, occ, selected.copy())
            newf = sel2 & ~selected
            labels[newf] = UNCERTAIN
            reason[newf] = 1
            selected = sel2

    # --- puentes: huecos estrechos entre regiones conservadas, rellenados con caras reales
    if p.bridge_gap > 0:
        cover = rasterize(tri[selected], grid)["count"] > 0
        closed = ndimage.binary_closing(cover, structure=_disk(r_close))
        gap = closed & ~cover
        bridge = (~selected & ~forced_remove & visible & (slope >= 0.08) & same_side & gap[gi, gj])
        labels[bridge] = KEEP
        reason[bridge] = 2
        selected = selected | bridge

    # --- islas residuales demasiado pequeñas
    nk, klab = face_components(topo, nf, face_ok=selected)
    if nk > 1:
        kf = np.flatnonzero(selected)
        k_area = np.bincount(klab[kf], weights=area[kf], minlength=nk)
        small = k_area < p.min_island_area
        drop = kf[small[klab[kf]]]
        labels[drop] = REMOVE
        reason[drop] = 14

    # --- correcciones manuales: siempre prevalecen
    labels[forced_keep] = KEEP
    reason[forced_keep] = 20
    labels[forced_remove] = REMOVE
    reason[forced_remove] = 21

    kept = labels > 0
    total_area = float(area.sum())
    kept_area = float(area[kept].sum())
    unc_area = float(area[labels == UNCERTAIN].sum())
    if n_uncertain:
        warnings.append(f"{n_uncertain} región(es) dudosa(s) conservada(s) ({unc_area:.0f} mm²): "
                        "revíselas (en amarillo) y elimínelas con el pincel si no son anatómicas.")
    cand_vis_area = float(area[cand & visible].sum())
    if kept_area < 0.3 * cand_vis_area:
        warnings.append("La región conservada es pequeña frente a la superficie orientada hacia el escáner: "
                        "puede faltar parte de la planta. Use «Elegir planta» o ajuste las tolerancias.")
    counts = {REASONS[int(k)]: int(c) for k, c in zip(*np.unique(reason, return_counts=True))}
    info = {
        "params": asdict(p),
        "faces_total": int(nf),
        "faces_kept": int(kept.sum()),
        "faces_removed": int(nf - kept.sum()),
        "faces_uncertain": int((labels == UNCERTAIN).sum()),
        "uncertain_regions": n_uncertain,
        "area_total": total_area,
        "area_kept": kept_area,
        "area_uncertain": unc_area,
        "removed_faces_pct": float(100.0 * (nf - kept.sum()) / nf),
        "removed_area_pct": float(100.0 * (total_area - kept_area) / max(total_area, 1e-300)),
        "seed_islands": int(nisl),
        "reason_counts": counts,
        "warnings": warnings,
    }
    return {"labels": labels, "reason": reason, "info": info}


def extract(mesh: Mesh, labels: np.ndarray) -> Mesh:
    """Etapa "eliminar geometría no deseada": submalla con las caras conservadas."""
    return mesh.submesh(np.asarray(labels) > 0)
