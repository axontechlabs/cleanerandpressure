"""Etapa 3 (a): recorte exacto de una malla por un plano.

Los triángulos atravesados por el plano se dividen, de modo que el corte produce un
borde recto y continuo en lugar de una línea dentada de triángulos enteros.
"""
from __future__ import annotations

import numpy as np

from .mesh import Mesh


def plane_clip(mesh: Mesh, point, normal) -> Mesh:
    """Conserva la parte de la malla situada en el lado hacia el que apunta ``normal``."""
    nrm = np.asarray(normal, float)
    nrm = nrm / np.linalg.norm(nrm)
    v = mesh.vertices
    f = mesh.faces
    s = (v - np.asarray(point, float)) @ nrm
    scale = max(float(np.abs(s).max()) if len(s) else 1.0, 1e-12)
    s = np.where(np.abs(s) < 1e-9 * scale, 1e-9 * scale, s)
    pos = s > 0
    fp = pos[f]
    npos = fp.sum(axis=1)

    keep_all = f[npos == 3]
    new_vertices = [v]
    next_id = [len(v)]
    edge_map: dict = {}

    def cut_points(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Índices de los vértices de corte en las aristas (a,b); se comparten entre caras."""
        lo = np.minimum(a, b)
        hi = np.maximum(a, b)
        key = lo * len(v) + hi
        uniq, inv = np.unique(key, return_inverse=True)
        ids = np.empty(len(uniq), dtype=np.int64)
        fresh = []
        for i, k in enumerate(uniq.tolist()):
            if k in edge_map:
                ids[i] = edge_map[k]
            else:
                edge_map[k] = ids[i] = next_id[0]
                next_id[0] += 1
                fresh.append(k)
        if fresh:
            fk = np.array(fresh, dtype=np.int64)
            l, h = fk // len(v), fk % len(v)
            t = s[l] / (s[l] - s[h])
            new_vertices.append(v[l] + (v[h] - v[l]) * t[:, None])
        return ids[inv]

    out = [keep_all]
    # un vértice positivo: queda un triángulo
    m1 = npos == 1
    if m1.any():
        f1 = f[m1]
        k = np.argmax(fp[m1], axis=1)
        r = np.arange(len(f1))
        a, b, c = f1[r, k], f1[r, (k + 1) % 3], f1[r, (k + 2) % 3]
        out.append(np.stack([a, cut_points(a, b), cut_points(a, c)], axis=1))
    # dos vértices positivos: queda un cuadrilátero (dos triángulos)
    m2 = npos == 2
    if m2.any():
        f2 = f[m2]
        k = np.argmin(fp[m2], axis=1)
        r = np.arange(len(f2))
        a, b, c = f2[r, k], f2[r, (k + 1) % 3], f2[r, (k + 2) % 3]
        pab, pac = cut_points(a, b), cut_points(a, c)
        out.append(np.stack([pab, b, c], axis=1))
        out.append(np.stack([pab, c, pac], axis=1))
    faces = np.vstack(out) if out else np.zeros((0, 3), dtype=np.int64)
    m = Mesh(np.vstack(new_vertices), faces)
    return m.submesh(np.ones(m.n_faces, dtype=bool))
