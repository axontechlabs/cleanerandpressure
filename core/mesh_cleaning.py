"""Limpieza básica: soldado de vértices, caras degeneradas/duplicadas y orientación coherente."""
from __future__ import annotations

from collections import deque

import numpy as np

from .mesh import Mesh, build_topology


def weld_vertices(mesh: Mesh, tol: float | None = None) -> tuple[Mesh, int]:
    """Fusiona vértices coincidentes (tolerancia relativa a la diagonal por defecto)."""
    if mesh.n_vertices == 0:
        return mesh.copy(), 0
    if tol is None:
        tol = 1e-7 * float(np.linalg.norm(mesh.extents)) or 1e-12
    q = np.round((mesh.vertices - mesh.vertices.min(axis=0)) / tol).astype(np.int64)
    _, idx, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    merged = mesh.n_vertices - len(idx)
    return Mesh(mesh.vertices[idx], inv[mesh.faces]), int(merged)


def remove_degenerate_faces(mesh: Mesh, rel_eps: float = 1e-12) -> tuple[Mesh, int]:
    f = mesh.faces
    same = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 0] == f[:, 2])
    _, area = mesh.face_normals()
    scale = float(np.linalg.norm(mesh.extents)) ** 2 or 1.0
    bad = same | (area <= rel_eps * scale)
    return Mesh(mesh.vertices, f[~bad]), int(bad.sum())


def remove_duplicate_faces(mesh: Mesh) -> tuple[Mesh, int]:
    if mesh.n_faces == 0:
        return mesh, 0
    key = np.sort(mesh.faces, axis=1)
    _, idx = np.unique(key, axis=0, return_index=True)
    idx.sort()
    return Mesh(mesh.vertices, mesh.faces[idx]), int(mesh.n_faces - len(idx))


def remove_unreferenced(mesh: Mesh) -> Mesh:
    return mesh.submesh(np.ones(mesh.n_faces, dtype=bool))


def orient_consistently(mesh: Mesh) -> tuple[Mesh, dict]:
    """Propaga un sentido de giro coherente dentro de cada componente (aristas variedad).

    En cada componente se conserva la orientación mayoritaria (por área) del archivo.
    """
    nf = mesh.n_faces
    topo = build_topology(mesh)
    adj = topo.adj.tolist()
    cons = topo.consistent.tolist()
    flip = np.zeros(nf, dtype=np.int8)
    comp = -np.ones(nf, dtype=np.int64)
    conflicts = 0
    ncomp = 0
    flip_l = [0] * nf
    comp_l = [-1] * nf
    for s in range(nf):
        if comp_l[s] != -1:
            continue
        comp_l[s] = ncomp
        dq = deque([s])
        while dq:
            f = dq.popleft()
            ff = flip_l[f]
            af, cf = adj[f], cons[f]
            for k in range(3):
                g = af[k]
                if g < 0:
                    continue
                want = ff if cf[k] else 1 - ff
                if comp_l[g] == -1:
                    comp_l[g] = ncomp
                    flip_l[g] = want
                    dq.append(g)
                elif flip_l[g] != want:
                    conflicts += 1
        ncomp += 1
    flip[:] = flip_l
    comp[:] = comp_l
    _, area = mesh.face_normals()
    flipped_area = np.bincount(comp, weights=area * flip, minlength=ncomp)
    total_area = np.bincount(comp, weights=area, minlength=ncomp)
    invert = flipped_area > 0.5 * total_area
    final = flip.astype(bool) ^ invert[comp]
    faces = mesh.faces.copy()
    faces[final] = faces[final][:, ::-1]
    return Mesh(mesh.vertices, faces), {
        "flipped_faces": int(final.sum()),
        "orientation_conflicts": int(conflicts // 2),
        "edge_components": int(ncomp),
    }


def clean(mesh: Mesh) -> tuple[Mesh, dict]:
    """Limpieza completa no destructiva (devuelve una malla nueva y un informe)."""
    rep = {"input_vertices": mesh.n_vertices, "input_faces": mesh.n_faces}
    m, merged = weld_vertices(mesh)
    rep["merged_vertices"] = merged
    m, ndeg = remove_degenerate_faces(m)
    rep["degenerate_faces_removed"] = ndeg
    m, ndup = remove_duplicate_faces(m)
    rep["duplicate_faces_removed"] = ndup
    m = remove_unreferenced(m)
    m, orep = orient_consistently(m)
    rep.update(orep)
    rep["output_vertices"] = m.n_vertices
    rep["output_faces"] = m.n_faces
    return m, rep
