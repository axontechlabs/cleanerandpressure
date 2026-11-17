"""Etapa 5: construcción del sólido cerrado a partir de la superficie plantar.

La superficie anatómica NO se modifica: sus vértices son los primeros del sólido.
Se añade una copia inferior (base plana o desplazada verticalmente) con la misma
triangulación proyectada, y paredes verticales (subdivididas en filas) que cosen
ambos contornos. Como la
superficie es un campo de alturas con proyección inyectiva y las paredes son
verticales, el sólido resultante es estanco, variedad y sin autointersecciones por
construcción (y aun así se comprueba en la validación).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from .mesh import Mesh, build_topology, cross2


@dataclass
class SolidParams:
    thickness: float = 5.0     # mm
    mode: str = "flat"         # "flat": base plana; "offset": espesor uniforme
    side: str = "interior"     # "interior": material hacia el interior del pie (positivo)
                               # "exterior": material bajo la planta (plantilla / negativo)
    wall_rows: int = 0         # filas de triángulos en las paredes (0 = automático)

    @classmethod
    def from_dict(cls, d: dict | None) -> "SolidParams":
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})


def build_solid(surface: Mesh, params: SolidParams | None = None) -> dict:
    p = params or SolidParams()
    if p.thickness <= 0:
        raise ValueError("El espesor debe ser mayor que cero.")
    if p.mode not in ("flat", "offset") or p.side not in ("interior", "exterior"):
        raise ValueError("Parámetros de sólido no válidos.")
    warnings: list[str] = []
    v = surface.vertices
    f = surface.faces
    n = len(v)
    if len(f) == 0:
        raise ValueError("No hay superficie para construir el sólido.")
    topo = build_topology(surface)
    if (topo.edge_count > 2).any():
        raise ValueError("La superficie tiene aristas no variedad; repita el recorte/remallado.")
    t2 = v[f][:, :, :2]
    sa = cross2(t2[:, 1] - t2[:, 0], t2[:, 2] - t2[:, 0])
    if (sa <= 0).any():
        warnings.append(f"{int((sa <= 0).sum())} triángulos no miran hacia el eje plantar; "
                        "el sólido podría autointersecarse (se comprobará en la validación).")
    sign = -1.0 if p.side == "interior" else 1.0
    z = v[:, 2]
    if p.mode == "flat":
        zb = np.full(n, (z.min() - p.thickness) if sign < 0 else (z.max() + p.thickness))
    else:
        zb = z + sign * p.thickness
    bottom = np.column_stack([v[:, :2], zb])

    bmask = topo.he_count == 1
    a = f[bmask]
    b = f[:, [1, 2, 0]][bmask]
    # Paredes verticales subdivididas en filas: evita triángulos muy afilados cuando la pared
    # es alta frente al tamaño de arista (mejor para CAD, laminadores y elementos finitos).
    thick = np.abs(zb - z)
    bv = np.unique(a)
    edge_len = float(np.linalg.norm(v[a] - v[b], axis=1).mean()) if len(a) else 1.0
    rows = int(p.wall_rows) if p.wall_rows > 0 else int(np.clip(round(float(thick[bv].max()) / (2.5 * max(edge_len, 1e-9)))
                                                             if len(bv) else 1, 1, 30))
    slot = np.full(n, -1, dtype=np.int64)
    slot[bv] = np.arange(len(bv))
    extra = []
    for j in range(1, rows):
        t = j / rows
        extra.append(v[bv] * (1 - t) + bottom[bv] * t)

    def level(x, j):
        if j == 0:
            return x
        if j == rows:
            return x + n
        return 2 * n + (j - 1) * len(bv) + slot[x]

    walls = []
    for j in range(rows):
        a0, b0, a1, b1 = level(a, j), level(b, j), level(a, j + 1), level(b, j + 1)
        walls.append(np.stack([b0, a0, a1], axis=1))
        walls.append(np.stack([b0, a1, b1], axis=1))
    faces = np.vstack([f, f[:, ::-1] + n] + walls)
    if sign > 0:
        faces = faces[:, ::-1]
    solid = Mesh(np.vstack([v, bottom] + extra), faces)
    info = {
        "params": asdict(p),
        "vertices": solid.n_vertices,
        "faces": solid.n_faces,
        "top_vertices": int(n),
        "top_faces": int(len(f)),
        "boundary_edges_stitched": int(bmask.sum()),
        "wall_rows": rows,
        "thickness_min": float(thick.min()),
        "thickness_max": float(thick.max()),
        "volume": float(solid.volume),
        "warnings": warnings,
    }
    return {"mesh": solid, "info": info}
