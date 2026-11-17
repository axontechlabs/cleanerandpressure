"""Etapa 1: análisis del STL de entrada."""
from __future__ import annotations

import numpy as np

from .mesh import Mesh, build_topology, face_components, vertex_components
from .mesh_cleaning import weld_vertices
from .mesh_validation import nonmanifold_vertices, self_intersections
from .raster import make_grid, rasterize


def detect_unit_scale(extents: np.ndarray) -> tuple[float, str]:
    """Deduce las unidades a partir del tamaño (un pie mide ~150-320 mm)."""
    d = float(np.max(extents))
    if d <= 0:
        return 1.0, "desconocidas"
    if d < 1.2:
        return 1000.0, "metros"
    if d < 12.0:
        return 100.0, "decímetros"
    if d < 60.0:
        return 10.0, "centímetros"
    return 1.0, "milímetros"


def _stats(x: np.ndarray) -> dict:
    if len(x) == 0:
        return {"min": 0, "p05": 0, "median": 0, "mean": 0, "p95": 0, "max": 0, "cv": 0}
    return {"min": float(x.min()), "p05": float(np.percentile(x, 5)), "median": float(np.median(x)),
            "mean": float(x.mean()), "p95": float(np.percentile(x, 95)), "max": float(x.max()),
            "cv": float(x.std() / max(x.mean(), 1e-300))}


def overlap_estimate(mesh: Mesh, axis: np.ndarray | None = None, tol: float | None = None) -> dict:
    """Estima el área con varias capas superpuestas vistas a lo largo de ``axis``."""
    if mesh.n_faces == 0:
        return {"multi_layer_fraction": 0.0}
    if axis is None:
        n, a = mesh.face_normals()
        s = (n * a[:, None]).sum(axis=0)
        if np.linalg.norm(s) < 1e-9 * a.sum():
            cov = np.cov(mesh.vertices.T)
            axis = np.linalg.eigh(cov)[1][:, 0]
        else:
            axis = s / np.linalg.norm(s)
    z = axis / np.linalg.norm(axis)
    x = np.cross(z, [1.0, 0, 0])
    if np.linalg.norm(x) < 0.3:
        x = np.cross(z, [0, 1.0, 0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    v = mesh.vertices @ np.stack([x, y, z]).T
    ext = v.max(axis=0) - v.min(axis=0)
    res = max(float(max(ext[0], ext[1])) / 400.0, 1e-9)
    grid = make_grid(v[:, :2].min(axis=0), v[:, :2].max(axis=0), res)
    r = rasterize(v[mesh.faces], grid)
    cov = r["count"] > 0
    if tol is None:
        tol = 4.0 * res
    multi = cov & ((r["zmax"] - r["zmin"]) > tol)
    return {"multi_layer_fraction": float(multi.sum() / max(cov.sum(), 1)),
            "multi_layer_area": float(multi.sum() * res * res), "grid_res": res}


def analyze(raw: Mesh, check_intersections: bool = True, max_faces_intersections: int = 1_500_000) -> dict:
    """Informe completo del archivo de entrada (no modifica la malla)."""
    rep: dict = {"errors": [], "warnings": []}
    welded, merged = weld_vertices(raw)
    rep["vertices_file"] = raw.n_vertices
    rep["vertices"] = welded.n_vertices
    rep["faces"] = welded.n_faces
    rep["duplicate_vertices"] = int(merged)
    b = welded.bounds
    rep["bbox_min"] = b[0].tolist()
    rep["bbox_max"] = b[1].tolist()
    rep["dimensions"] = (b[1] - b[0]).tolist()
    scale, unit = detect_unit_scale(b[1] - b[0])
    rep["unit_guess"] = unit
    rep["unit_scale_to_mm"] = scale

    f = welded.faces
    n, area = welded.face_normals()
    rep["area"] = float(area.sum())
    same = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 0] == f[:, 2])
    scale2 = float(np.linalg.norm(b[1] - b[0])) ** 2 or 1.0
    deg = same | (area <= 1e-12 * scale2)
    rep["degenerate_faces"] = int(deg.sum())
    key = np.sort(f, axis=1)
    rep["duplicate_faces"] = int(len(key) - len(np.unique(key, axis=0)))

    good = Mesh(welded.vertices, f[~deg])
    topo = build_topology(good)
    rep["edges"] = int(topo.n_edges)
    rep["boundary_edges"] = int((topo.edge_count == 1).sum())
    rep["nonmanifold_edges"] = int((topo.edge_count > 2).sum())
    rep["nonmanifold_vertices"] = int(len(nonmanifold_vertices(good, topo)))
    ncomp_e, lab_e = face_components(topo, good.n_faces)
    ncomp_v, lab_v = vertex_components(good)
    rep["components_edge_connected"] = ncomp_e
    rep["components"] = ncomp_v
    _, a2 = good.face_normals()
    if ncomp_v:
        ca = np.bincount(lab_v, weights=a2, minlength=ncomp_v)
        order = np.argsort(-ca)
        rep["component_areas_top"] = [float(x) for x in ca[order][:10]]
        rep["largest_component_area_fraction"] = float(ca.max() / max(ca.sum(), 1e-300))
    man = topo.adj >= 0
    rep["inconsistent_normal_edges"] = int((man & ~topo.consistent).sum() // 2)
    rep["watertight"] = bool(rep["boundary_edges"] == 0 and rep["nonmanifold_edges"] == 0 and good.n_faces > 0)
    rep["manifold"] = bool(rep["nonmanifold_edges"] == 0 and rep["nonmanifold_vertices"] == 0)

    # Coherencia de las normales con una orientación global (superficie abierta vista de un lado)
    s = (n * area[:, None]).sum(axis=0)
    rep["net_normal_ratio"] = float(np.linalg.norm(s) / max(area.sum(), 1e-300))

    # Densidad de polígonos
    t = good.triangles
    el = np.concatenate([np.linalg.norm(t[:, (k + 1) % 3] - t[:, k], axis=1) for k in range(3)])
    rep["edge_length"] = _stats(el)
    rep["face_area"] = _stats(a2)
    rep["density_faces_per_area"] = float(good.n_faces / max(a2.sum(), 1e-300))
    rep["density_variation"] = float(np.percentile(el, 95) / max(np.percentile(el, 5), 1e-300)) if len(el) else 0.0

    rep.update(overlap_estimate(good))

    if check_intersections and good.n_faces <= max_faces_intersections:
        si = self_intersections(good)
        rep["self_intersecting_pairs"] = si["pairs"]
        rep["self_intersecting_faces"] = int(len(si["faces"]))
        rep["self_intersections_complete"] = si["complete"]
    else:
        rep["self_intersecting_pairs"] = None

    w = rep["warnings"]
    if scale != 1.0:
        w.append(f"Las dimensiones sugieren unidades en {unit}; se escalará ×{scale:g} a milímetros.")
    if rep["degenerate_faces"]:
        w.append(f"{rep['degenerate_faces']} caras degeneradas.")
    if rep["duplicate_faces"]:
        w.append(f"{rep['duplicate_faces']} caras duplicadas.")
    if rep["nonmanifold_edges"]:
        w.append(f"{rep['nonmanifold_edges']} aristas no variedad.")
    if rep["components"] > 1:
        w.append(f"{rep['components']} componentes desconectados.")
    if rep["multi_layer_fraction"] > 0.02:
        w.append(f"Capas superpuestas en ~{100 * rep['multi_layer_fraction']:.1f}% de la proyección.")
    if rep.get("self_intersecting_pairs"):
        w.append(f"{rep['self_intersecting_pairs']} pares de triángulos se intersecan.")
    if rep["density_variation"] > 6:
        w.append("Densidad de polígonos muy variable entre zonas.")
    return rep
