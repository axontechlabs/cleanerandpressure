"""Rasterización de triángulos sobre una rejilla 2D (campo de alturas / z-buffer).

Se usa para: visibilidad desde la dirección plantar, detección de capas
superpuestas, huella de la región seleccionada y remallado por campo de alturas.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Grid:
    x0: float
    y0: float
    res: float
    nx: int
    ny: int

    @property
    def shape(self):
        return (self.nx, self.ny)

    def node_xy(self):
        x = self.x0 + self.res * np.arange(self.nx)
        y = self.y0 + self.res * np.arange(self.ny)
        return x, y

    def index(self, xy: np.ndarray):
        """Índices del nodo más cercano (recortados a la rejilla)."""
        i = np.clip(np.rint((xy[:, 0] - self.x0) / self.res).astype(np.int64), 0, self.nx - 1)
        j = np.clip(np.rint((xy[:, 1] - self.y0) / self.res).astype(np.int64), 0, self.ny - 1)
        return i, j

    def sample_nearest(self, field: np.ndarray, xy: np.ndarray) -> np.ndarray:
        i, j = self.index(xy)
        return field[i, j]

    def sample_bilinear(self, field: np.ndarray, xy: np.ndarray) -> np.ndarray:
        """Interpolación bilineal; ``field`` no debe contener NaN en la zona consultada."""
        gx = np.clip((xy[:, 0] - self.x0) / self.res, 0, self.nx - 1 - 1e-9)
        gy = np.clip((xy[:, 1] - self.y0) / self.res, 0, self.ny - 1 - 1e-9)
        i = np.floor(gx).astype(np.int64)
        j = np.floor(gy).astype(np.int64)
        fx = gx - i
        fy = gy - j
        return (field[i, j] * (1 - fx) * (1 - fy) + field[i + 1, j] * fx * (1 - fy)
                + field[i, j + 1] * (1 - fx) * fy + field[i + 1, j + 1] * fx * fy)


def make_grid(xy_min, xy_max, res: float, pad: int = 3, max_nodes: int = 6_000_000) -> Grid:
    ext = np.asarray(xy_max, float) - np.asarray(xy_min, float)
    n = np.ceil(ext / res).astype(int) + 1 + 2 * pad
    while n[0] * n[1] > max_nodes:
        res *= 1.25
        n = np.ceil(ext / res).astype(int) + 1 + 2 * pad
    return Grid(float(xy_min[0] - pad * res), float(xy_min[1] - pad * res), float(res), int(n[0]), int(n[1]))


def rasterize(tri: np.ndarray, grid: Grid) -> dict:
    """Rasteriza triángulos (T,3,3) sobre los nodos de la rejilla.

    Devuelve ``zmax``, ``zmin`` (NaN donde no hay cobertura), ``zsum`` y ``count``.
    """
    n_nodes = grid.nx * grid.ny
    zmax = np.full(n_nodes, -np.inf)
    zmin = np.full(n_nodes, np.inf)
    zsum = np.zeros(n_nodes)
    cnt = np.zeros(n_nodes)
    if len(tri):
        gx = (tri[:, :, 0] - grid.x0) / grid.res
        gy = (tri[:, :, 1] - grid.y0) / grid.res
        z = tri[:, :, 2]
        ax, bx, cx = gx[:, 0], gx[:, 1], gx[:, 2]
        ay, by, cy = gy[:, 0], gy[:, 1], gy[:, 2]
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        i0 = np.maximum(np.ceil(gx.min(axis=1) - 1e-9), 0).astype(np.int64)
        i1 = np.minimum(np.floor(gx.max(axis=1) + 1e-9), grid.nx - 1).astype(np.int64)
        j0 = np.maximum(np.ceil(gy.min(axis=1) - 1e-9), 0).astype(np.int64)
        j1 = np.minimum(np.floor(gy.max(axis=1) + 1e-9), grid.ny - 1).astype(np.int64)
        w = i1 - i0 + 1
        h = j1 - j0 + 1
        ok = (w > 0) & (h > 0) & (np.abs(det) > 1e-12)
        size = np.where(ok, np.maximum(w, h), 0)
        cap_prev = 0
        cap = 2
        smax = int(size.max()) if len(size) else 0
        while cap_prev < smax:
            sel = np.flatnonzero((size > cap_prev) & (size <= cap))
            if len(sel):
                _raster_class(sel, cap, i0, j0, w, h, ax, ay, bx, by, cx, cy, det, z,
                              grid, zmax, zmin, zsum, cnt)
            cap_prev = cap
            cap *= 2
    covered = cnt > 0
    zmax[~covered] = np.nan
    zmin[~covered] = np.nan
    sh = grid.shape
    return {"zmax": zmax.reshape(sh), "zmin": zmin.reshape(sh), "zsum": zsum.reshape(sh),
            "count": cnt.reshape(sh)}


def _raster_class(sel, cap, i0, j0, w, h, ax, ay, bx, by, cx, cy, det, z, grid, zmax, zmin, zsum, cnt):
    n_nodes = grid.nx * grid.ny
    i0s, j0s, ws, hs = i0[sel], j0[sel], w[sel], h[sel]
    axs, ays, bxs, bys, cxs, cys = ax[sel], ay[sel], bx[sel], by[sel], cx[sel], cy[sel]
    inv = 1.0 / det[sel]
    zs = z[sel]
    eps = 1e-9
    for di in range(cap):
        mi = ws > di
        if not mi.any():
            break
        for dj in range(cap):
            m = mi & (hs > dj)
            if not m.any():
                break
            px = (i0s[m] + di).astype(np.float64)
            py = (j0s[m] + dj).astype(np.float64)
            l1 = ((bys[m] - cys[m]) * (px - cxs[m]) + (cxs[m] - bxs[m]) * (py - cys[m])) * inv[m]
            l2 = ((cys[m] - ays[m]) * (px - cxs[m]) + (axs[m] - cxs[m]) * (py - cys[m])) * inv[m]
            l3 = 1.0 - l1 - l2
            inside = (l1 >= -eps) & (l2 >= -eps) & (l3 >= -eps)
            if not inside.any():
                continue
            zm = zs[m]
            zz = (l1 * zm[:, 0] + l2 * zm[:, 1] + l3 * zm[:, 2])[inside]
            node = ((i0s[m] + di) * grid.ny + (j0s[m] + dj))[inside]
            np.maximum.at(zmax, node, zz)
            np.minimum.at(zmin, node, zz)
            zsum += np.bincount(node, weights=zz, minlength=n_nodes)
            cnt += np.bincount(node, minlength=n_nodes)
