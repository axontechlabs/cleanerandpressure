"""Métricas de control de calidad: desviación geométrica y preservación de la planta."""
from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from .mesh import Mesh


def closest_point_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Punto más cercano de cada triángulo (a,b,c) a cada punto p (Ericson, vectorizado)."""
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    den = np.where(np.abs(va + vb + vc) > 1e-300, va + vb + vc, 1.0)
    vv = vb / den
    ww = vc / den
    out = a + ab * vv[:, None] + ac * ww[:, None]
    # regiones de aristas y vértices (se aplican de menor a mayor prioridad)
    m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    t = (d4 - d3) / np.where(m, (d4 - d3) + (d5 - d6), 1.0)
    out[m] = (b + (c - b) * t[:, None])[m]
    m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    t = d2 / np.where(m, d2 - d6, 1.0)
    out[m] = (a + ac * t[:, None])[m]
    m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    t = d1 / np.where(m, d1 - d3, 1.0)
    out[m] = (a + ab * t[:, None])[m]
    m = (d6 >= 0) & (d5 <= d6)
    out[m] = c[m]
    m = (d3 >= 0) & (d4 <= d3)
    out[m] = b[m]
    m = (d1 <= 0) & (d2 <= 0)
    out[m] = a[m]
    return out


def point_to_mesh_distance(points: np.ndarray, mesh: Mesh, k: int = 10, chunk: int = 40000) -> np.ndarray:
    """Distancia de cada punto a la malla (exacta sobre los k triángulos de centroide más cercano)."""
    if mesh.n_faces == 0 or len(points) == 0:
        return np.full(len(points), np.inf)
    tri = mesh.triangles
    tree = cKDTree(tri.mean(axis=1))
    k = min(k, mesh.n_faces)
    out = np.empty(len(points))
    for s in range(0, len(points), chunk):
        q = points[s:s + chunk]
        _, idx = tree.query(q, k=k)
        idx = idx.reshape(len(q), -1)
        qq = np.repeat(q, idx.shape[1], axis=0)
        t = tri[idx.ravel()]
        cp = closest_point_on_triangles(qq, t[:, 0], t[:, 1], t[:, 2])
        d = np.linalg.norm(cp - qq, axis=1).reshape(len(q), -1)
        out[s:s + chunk] = d.min(axis=1)
    return out


def distance_stats(d: np.ndarray) -> dict:
    d = d[np.isfinite(d)]
    if len(d) == 0:
        return {"mean": None, "rms": None, "p95": None, "max": None, "n": 0}
    return {"mean": float(d.mean()), "rms": float(np.sqrt((d ** 2).mean())),
            "p95": float(np.percentile(d, 95)), "max": float(d.max()), "n": int(len(d))}


def quality_report(reference: Mesh, top_surface: Mesh, support: np.ndarray | None = None,
                   tolerance: float = 0.5) -> dict:
    """Compara la superficie final con el escaneo segmentado (ambos en el mismo marco).

    * ``deviation``: distancia superficie final -> escaneo, solo en vértices con soporte real.
    * ``preservation_pct``: % del área del escaneo conservado que queda representada en la
      superficie final dentro de ``tolerance`` mm.
    """
    pts = top_surface.vertices
    if support is not None and support.any():
        pts = pts[support]
    d_fwd = point_to_mesh_distance(pts, reference)
    _, area = reference.face_normals()
    d_bwd = point_to_mesh_distance(reference.centroids, top_surface)
    ok = d_bwd <= tolerance
    return {
        "deviation": distance_stats(d_fwd),
        "reverse_deviation": distance_stats(d_bwd),
        "preservation_pct": float(100.0 * area[ok].sum() / max(area.sum(), 1e-300)),
        "preservation_tolerance": tolerance,
        "interpolated_vertices_pct": float(100.0 * (1 - support.mean())) if support is not None else 0.0,
    }
