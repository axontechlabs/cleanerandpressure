"""Comprobaciones de validez: variedad, estanqueidad, orientación, autointersecciones y volumen."""
from __future__ import annotations

from itertools import product

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph

from .mesh import Mesh, build_topology, face_components


# --------------------------------------------------------------------------- autointersecciones
def _seg_tri(a, b, q0, q1, q2, eps=1e-7):
    """Intersección segmento-triángulo (Möller-Trumbore), vectorizada.

    El segmento debe cruzar el plano en su interior (estricto en ``t``), pero el punto de
    cruce puede caer sobre una arista del triángulo: entre triángulos sin vértices comunes
    ese contacto también es una penetración (p. ej. al atravesar la diagonal de un quad).
    """
    d = b - a
    e1 = q1 - q0
    e2 = q2 - q0
    pvec = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, pvec)
    scale = np.linalg.norm(d, axis=1) * np.linalg.norm(e1, axis=1) * np.linalg.norm(e2, axis=1)
    ok = np.abs(det) > 1e-10 * np.maximum(scale, 1e-300)
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    tvec = a - q0
    u = np.einsum("ij,ij->i", tvec, pvec) * inv
    qvec = np.cross(tvec, e1)
    v = np.einsum("ij,ij->i", d, qvec) * inv
    t = np.einsum("ij,ij->i", e2, qvec) * inv
    return ok & (u > -eps) & (v > -eps) & (u + v < 1 + eps) & (t > eps) & (t < 1 - eps)


def tri_tri_intersect(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """True si los triángulos p[i] y q[i] se atraviesan (no cuenta contactos tangentes)."""
    hit = np.zeros(len(p), dtype=bool)
    for k in range(3):
        hit |= _seg_tri(p[:, k], p[:, (k + 1) % 3], q[:, 0], q[:, 1], q[:, 2])
        hit |= _seg_tri(q[:, k], q[:, (k + 1) % 3], p[:, 0], p[:, 1], p[:, 2])
    return hit


def self_intersections(mesh: Mesh, pair_budget: int = 80_000_000, chunk: int = 4_000_000) -> dict:
    """Detecta pares de triángulos no adyacentes que se atraviesan.

    Fase ancha: rejilla espacial multinivel (cada triángulo se inserta en el nivel
    cuyo tamaño de celda es >= su caja envolvente y se cruza con los de niveles
    inferiores). Fase fina: test exacto arista-triángulo.
    """
    tri = mesh.triangles
    nf = len(tri)
    if nf < 2:
        return {"pairs": 0, "faces": np.zeros(0, dtype=np.int64), "complete": True}
    bmin = tri.min(axis=1)
    bmax = tri.max(axis=1)
    size = (bmax - bmin).max(axis=1)
    h0 = max(float(np.median(size)), 1e-9)
    level = np.clip(np.ceil(np.log2(np.maximum(size, 1e-300) / h0)), -4, 40).astype(np.int64)
    origin = bmin.min(axis=0) - h0
    faces = mesh.faces
    found = []
    tested = 0
    complete = True
    for lv in np.unique(level):
        h = h0 * (2.0 ** lv)
        sel = np.flatnonzero(level <= lv)
        lo = np.floor((bmin[sel] - origin) / h).astype(np.int64)
        hi = np.floor((bmax[sel] - origin) / h).astype(np.int64)
        dims = hi.max(axis=0) + 2
        keys, tris = [], []
        for dx, dy, dz in product((0, 1), repeat=3):
            c = lo + np.array([dx, dy, dz])
            m = (c <= hi).all(axis=1)
            if m.any():
                cm = c[m]
                keys.append((cm[:, 0] * dims[1] + cm[:, 1]) * dims[2] + cm[:, 2])
                tris.append(sel[m])
        key = np.concatenate(keys)
        tid = np.concatenate(tris)
        order = np.argsort(key, kind="stable")
        key, tid = key[order], tid[order]
        is_l = level[tid] == lv
        a_key, a_tid = key[is_l], tid[is_l]
        left = np.searchsorted(key, a_key, side="left")
        right = np.searchsorted(key, a_key, side="right")
        cnt = right - left
        csum = np.cumsum(cnt)
        start = 0
        while start < len(a_key):
            base = csum[start - 1] if start else 0
            stop = int(np.searchsorted(csum, base + chunk, side="right"))
            stop = max(stop, start + 1)
            c = cnt[start:stop]
            tot = int(c.sum())
            if tot:
                rep = np.repeat(np.arange(start, stop), c)
                offs = np.arange(tot) - np.repeat(np.cumsum(c) - c, c)
                i = a_tid[rep]
                j = tid[left[rep] + offs]
                m = (level[j] < lv) | (j > i)
                i, j = i[m], j[m]
                ov = ((bmin[i] <= bmax[j]) & (bmin[j] <= bmax[i])).all(axis=1)
                i, j = i[ov], j[ov]
                if len(i):
                    fi, fj = faces[i], faces[j]
                    share = (fi[:, :, None] == fj[:, None, :]).any(axis=(1, 2))
                    i, j = i[~share], j[~share]
                if len(i):
                    lo_i, hi_j = np.minimum(i, j), np.maximum(i, j)
                    pk = np.unique(lo_i * nf + hi_j)
                    a, b = pk // nf, pk % nf
                    hit = tri_tri_intersect(tri[a], tri[b])
                    if hit.any():
                        found.append(pk[hit])
                tested += tot
            start = stop
            if tested > pair_budget:
                complete = False
                break
        if not complete:
            break
    if found:
        pk = np.unique(np.concatenate(found))
        fs = np.unique(np.r_[pk // nf, pk % nf])
        return {"pairs": int(len(pk)), "faces": fs, "complete": complete}
    return {"pairs": 0, "faces": np.zeros(0, dtype=np.int64), "complete": complete}


# --------------------------------------------------------------------------- vértices no variedad
def nonmanifold_vertices(mesh: Mesh, topo=None) -> np.ndarray:
    """Vértices cuyo abanico de caras no es único (p. ej. dos conos unidos por un punto)."""
    topo = topo or build_topology(mesh)
    nf = mesh.n_faces
    if nf == 0:
        return np.zeros(0, dtype=np.int64)
    h = np.arange(3 * nf)
    opp = topo.opp.ravel()
    m = opp >= 0
    h, o = h[m], opp[m]
    f, k = h // 3, h % 3
    g, mm = o // 3, o % 3
    cons = topo.consistent.ravel()[h]
    # esquina de v0 en f = k ; esquina de v1 en f = (k+1)%3
    c_f_v0 = f * 3 + k
    c_f_v1 = f * 3 + (k + 1) % 3
    c_g_v0 = np.where(cons, g * 3 + (mm + 1) % 3, g * 3 + mm)
    c_g_v1 = np.where(cons, g * 3 + mm, g * 3 + (mm + 1) % 3)
    i = np.r_[c_f_v0, c_f_v1]
    j = np.r_[c_g_v0, c_g_v1]
    gph = sparse.csr_matrix((np.ones(len(i), dtype=np.int8), (i, j)), shape=(3 * nf, 3 * nf))
    _, lab = csgraph.connected_components(gph, directed=False)
    v = mesh.faces.ravel()
    pair = np.unique(np.stack([v, lab], axis=1), axis=0)
    cnt = np.bincount(pair[:, 0], minlength=mesh.n_vertices)
    return np.flatnonzero(cnt > 1)


# --------------------------------------------------------------------------- validación del sólido
def _check(cid, name, ok, value, detail=""):
    return {"id": cid, "name": name, "ok": bool(ok), "value": value, "detail": detail}


def validate_solid(mesh: Mesh, check_intersections: bool = True) -> dict:
    """Ejecuta todas las comprobaciones exigidas a un sólido cerrado.

    El campo ``valid`` solo es True si TODAS las comprobaciones se superan.
    """
    checks = []
    nf, nv = mesh.n_faces, mesh.n_vertices
    finite = bool(np.isfinite(mesh.vertices).all()) and nf > 0
    checks.append(_check("finite", "Coordenadas finitas y malla no vacía", finite, nf))
    if not finite:
        return {"valid": False, "checks": checks, "errors": ["Malla vacía o con coordenadas no finitas."],
                "warnings": [], "volume": 0.0, "area": 0.0}
    topo = build_topology(mesh)
    _, area = mesh.face_normals()
    f = mesh.faces
    rep = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 0] == f[:, 2])
    scale2 = float(np.linalg.norm(mesh.extents)) ** 2 or 1.0
    ndeg = int((rep | (area <= 1e-12 * scale2)).sum())
    checks.append(_check("degenerate", "Sin caras degeneradas", ndeg == 0, ndeg))
    nb = int((topo.edge_count == 1).sum())
    checks.append(_check("boundary", "Sin bordes abiertos", nb == 0, nb, "aristas de borde"))
    nnm = int((topo.edge_count > 2).sum())
    checks.append(_check("manifold_edges", "Aristas variedad (2 caras por arista)", nnm == 0, nnm))
    nmv = nonmanifold_vertices(mesh, topo)
    checks.append(_check("manifold_vertices", "Vértices variedad", len(nmv) == 0, int(len(nmv))))
    watertight = nb == 0 and nnm == 0
    checks.append(_check("watertight", "Estanca (watertight)", watertight, watertight))
    man = topo.adj >= 0
    ninc = int((man & ~topo.consistent).sum() // 2)
    checks.append(_check("winding", "Normales coherentes entre caras vecinas", ninc == 0, ninc))
    ncomp, _ = face_components(topo, nf)
    checks.append(_check("components", "Un único componente", ncomp == 1, ncomp))
    vol = mesh.volume
    checks.append(_check("volume", "Volumen positivo (normales hacia fuera)", vol > 0, round(vol, 3), "mm³"))
    euler = nv - topo.n_edges + nf
    checks.append(_check("euler", "Característica de Euler par (superficie cerrada orientable)",
                         (not watertight) or euler % 2 == 0, int(euler),
                         f"género {(2 * ncomp - euler) // 2}" if watertight else ""))
    if check_intersections:
        si = self_intersections(mesh)
        checks.append(_check("self_intersections", "Sin autointersecciones",
                             si["pairs"] == 0 and si["complete"], si["pairs"],
                             "" if si["complete"] else "búsqueda incompleta (malla demasiado densa)"))
    errors = [f"{c['name']}: {c['value']} {c['detail']}".strip() for c in checks if not c["ok"]]
    warnings = []
    q = triangle_quality(mesh)
    if q["min_angle_deg"] < 1.0:
        warnings.append(f"Hay triángulos muy afilados (ángulo mínimo {q['min_angle_deg']:.2f}°).")
    return {"valid": len(errors) == 0, "checks": checks, "errors": errors, "warnings": warnings,
            "volume": float(vol), "area": float(area.sum()), "vertices": nv, "faces": nf,
            "quality": q}


def triangle_quality(mesh: Mesh) -> dict:
    t = mesh.triangles
    if len(t) == 0:
        return {"min_angle_deg": 0.0, "mean_aspect": 0.0}
    e = [np.linalg.norm(t[:, (k + 1) % 3] - t[:, k], axis=1) for k in range(3)]
    a, b, c = e
    cos_a = np.clip((b * b + c * c - a * a) / np.maximum(2 * b * c, 1e-300), -1, 1)
    cos_b = np.clip((a * a + c * c - b * b) / np.maximum(2 * a * c, 1e-300), -1, 1)
    cos_c = np.clip((a * a + b * b - c * c) / np.maximum(2 * a * b, 1e-300), -1, 1)
    ang = np.degrees(np.arccos(np.stack([cos_a, cos_b, cos_c])))
    lmax = np.maximum(np.maximum(a, b), c)
    lmin = np.minimum(np.minimum(a, b), c)
    return {"min_angle_deg": float(ang.min()),
            "p01_min_angle_deg": float(np.percentile(ang.min(axis=0), 1)),
            "mean_aspect": float(np.mean(lmax / np.maximum(lmin, 1e-300)))}
