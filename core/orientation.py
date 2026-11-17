"""Estimación del marco plantar: eje Z = normal exterior de la planta, X = eje largo del pie.

No se asume ninguna orientación del archivo. La dirección plantar se obtiene de la
normal media ponderada por área (las paredes laterales se cancelan entre sí) y se
refina de forma robusta; el usuario puede invertirla o fijarla manualmente.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .mesh import Mesh, build_topology, vertex_components
from .raster import make_grid, rasterize


@dataclass
class Frame:
    rotation: np.ndarray          # filas = ejes X, Y, Z canónicos expresados en el marco del archivo
    origin: np.ndarray
    info: dict = field(default_factory=dict)

    def to_canonical(self, p: np.ndarray) -> np.ndarray:
        return (np.asarray(p, float) - self.origin) @ self.rotation.T

    def to_world(self, p: np.ndarray) -> np.ndarray:
        return np.asarray(p, float) @ self.rotation + self.origin

    @property
    def z_axis(self) -> np.ndarray:
        return self.rotation[2]


def _envelope_roughness(v: np.ndarray, faces: np.ndarray, d: np.ndarray) -> float:
    """Dispersión robusta de la envolvente superior vista desde +d (menor = más plana)."""
    x = np.cross(d, [1.0, 0, 0])
    if np.linalg.norm(x) < 0.3:
        x = np.cross(d, [0, 1.0, 0])
    x /= np.linalg.norm(x)
    y = np.cross(d, x)
    p = v @ np.stack([x, y, d]).T
    ext = p[:, :2].max(axis=0) - p[:, :2].min(axis=0)
    grid = make_grid(p[:, :2].min(axis=0), p[:, :2].max(axis=0), max(float(ext.max()) / 150.0, 1e-9))
    top = rasterize(p[faces], grid)["zmax"]
    h = top[np.isfinite(top)]
    if len(h) < 10:
        return np.inf
    return float(np.percentile(h, 90) - np.percentile(h, 10))


def estimate_frame(mesh: Mesh, flip: bool = False, axis_override=None) -> Frame:
    info: dict = {"warnings": []}
    n, a = mesh.face_normals()
    cen = mesh.centroids
    ncomp, lab = vertex_components(mesh)
    ca = np.bincount(lab, weights=a, minlength=max(ncomp, 1))
    main = lab == int(np.argmax(ca))
    s = (n[main] * a[main][:, None]).sum(axis=0)
    ratio = float(np.linalg.norm(s) / max(a[main].sum(), 1e-300))
    info["net_normal_ratio"] = ratio

    if axis_override is not None:
        d = np.asarray(axis_override, float)
        d = d / np.linalg.norm(d)
        info["method"] = "manual"
    elif ratio > 0.12:
        d = s / np.linalg.norm(s)
        for _ in range(5):
            sel = main & (n @ d > np.cos(np.radians(50.0)))
            if a[sel].sum() < 0.05 * a[main].sum():
                break
            s2 = (n[sel] * a[sel][:, None]).sum(axis=0)
            d = s2 / np.linalg.norm(s2)
        info["method"] = "normal media ponderada"
    else:
        # Malla cerrada o normales incoherentes: se prueba cada eje principal (±) y se elige
        # la dirección hacia la que mira más superficie "plana" (la planta frente al empeine).
        c = cen[main]
        w = a[main]
        mu = (c * w[:, None]).sum(axis=0) / w.sum()
        cov = ((c - mu) * w[:, None]).T @ (c - mu) / w.sum()
        axes = np.linalg.eigh(cov)[1].T
        sub = Mesh(mesh.vertices, mesh.faces[main])
        topo_m = build_topology(sub)
        closed = (topo_m.edge_count == 1).sum() < 0.01 * max(topo_m.n_edges, 1)
        cos_t = np.cos(np.radians(35.0))
        nm, am = n[main], a[main]
        if closed and abs(sub.volume) > 0:
            sgn = np.sign(sub.volume)          # normales hacia fuera = sgn * n
            cands = [s_ * ax for ax in axes for s_ in (1.0, -1.0)]
            d = max(cands, key=lambda q: am[(sgn * (nm @ q)) > cos_t].sum())
            for _ in range(4):
                sel = (sgn * (nm @ d)) > cos_t
                if not sel.any():
                    break
                s2 = sgn * (nm[sel] * am[sel][:, None]).sum(axis=0)
                d = s2 / np.linalg.norm(s2)
            info["method"] = "malla cerrada: lado con más superficie plana"
        else:
            d = max(list(axes), key=lambda q: am[np.abs(nm @ q) > cos_t].sum())
            r_pos = _envelope_roughness(mesh.vertices, mesh.faces[main], d)
            r_neg = _envelope_roughness(mesh.vertices, mesh.faces[main], -d)
            if r_neg < r_pos:
                d = -d
            info["method"] = "normales incoherentes: eje dominante y lado más plano"
        info["warnings"].append(
            "La malla es cerrada o sus normales no indican un lado de escaneo: la cara plantar se ha "
            "deducido como el lado más plano. Compruébela y use «Invertir lado plantar» si es incorrecta.")
    # Indicio geométrico: el borde de un escaneo abierto queda "detrás" de la planta (la
    # superficie se curva alejándose del escáner). Si contradice con fuerza a las normales
    # del archivo, estas apuntan hacia dentro y se corrige automáticamente.
    sub_main = mesh.submesh(main)
    bpts = sub_main.vertices[np.unique(build_topology(sub_main).boundary_edges)]
    hv = sub_main.vertices @ d
    hb = bpts @ d
    front = main & (n @ d > 0.7)
    if len(bpts) > 20 and hv.std() > 0 and axis_override is None and a[front].sum() > 0:
        h_front = float(((cen[front] @ d) * a[front]).sum() / a[front].sum())
        rim = float((h_front - hb.mean()) / hv.std())
        info["rim_cue"] = rim
        if rim < -1.2 and info["method"] == "normal media ponderada":
            d = -d
            info["auto_inverted"] = True
            info["warnings"].append(
                "Las normales del archivo apuntan hacia el interior del pie: la cara plantar se ha "
                "corregido automáticamente a partir de la forma del borde. Compruébela en la vista.")
        elif rim < -0.6:
            info["warnings"].append(
                "El borde del escaneo queda por delante de la superficie: la cara plantar podría estar "
                "invertida. Revise la vista y use «Invertir lado plantar» si procede.")
    if flip:
        d = -d
    info["flipped"] = bool(flip)

    facing = main & (n @ d > 0.5)
    if a[facing].sum() < 1e-12:
        facing = main
    w = a[facing]
    origin = (cen[facing] * w[:, None]).sum(axis=0) / w.sum()
    q = cen[facing] - origin
    q = q - np.outer(q @ d, d)
    cov = (q * w[:, None]).T @ q / w.sum()
    evals, evecs = np.linalg.eigh(cov)
    x = evecs[:, 2]
    x = x - (x @ d) * d
    x /= np.linalg.norm(x)
    y = np.cross(d, x)
    info["plantar_axis"] = d.tolist()
    info["long_axis"] = x.tolist()
    info["elongation"] = float(np.sqrt(max(evals[2], 0) / max(evals[1], 1e-300)))
    return Frame(np.stack([x, y, d]), origin, info)
