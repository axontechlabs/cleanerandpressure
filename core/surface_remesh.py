"""Etapa 3 (b): contorno limpio, relleno de agujeros y remallado uniforme.

La superficie plantar segmentada es, vista desde la dirección plantar, un campo de
alturas z = h(x, y). Se reconstruye sobre una rejilla regular:

* la huella (máscara) se regulariza morfológicamente -> contorno continuo sin dientes;
* las alturas se toman de los triángulos originales (interpolación baricéntrica exacta);
* los agujeros se rellenan con un carenado biarmónico (mínima variación de curvatura);
* el borde escalonado de la rejilla se convierte en una curva suave (Taubin en 2D);
* la densidad de triángulos queda uniforme y las capas duplicadas se fusionan.

El resultado es una superficie abierta, variedad, con un único contorno limpio y sin
solapes, lo que garantiza que la extrusión posterior no pueda autointersecarse.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
from scipy import ndimage, sparse
from scipy.sparse.linalg import splu

from .mesh import Mesh, boundary_loops, cross2
from .raster import Grid, make_grid, rasterize
from .segmentation import _disk


@dataclass
class ContourParams:
    resolution: float = 0.0       # mm; 0 = automática según la densidad del escaneo
    contour_close: float = 2.0    # mm: radio de cierre (une grietas y muescas estrechas)
    contour_open: float = 1.5     # mm: radio de apertura (elimina picos y fragmentos finos)
    contour_smooth: float = 2.0   # mm: regularización del contorno (0 = desactivada)
    contour_inset: float = 0.0    # mm: retranqueo del borde hacia el interior
    boundary_iters: int = 10      # iteraciones de suavizado + reproyección del borde
    max_hole_area: float = 0.0    # mm²: 0 = rellenar todos los agujeros interiores
    layer_tol: float = 1.5        # mm: capas más separadas no se promedian (se toma la exterior)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ContourParams":
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})


def auto_resolution(mesh: Mesh) -> float:
    t = mesh.triangles
    if len(t) == 0:
        return 1.0
    el = np.concatenate([np.linalg.norm(t[:, (k + 1) % 3] - t[:, k], axis=1) for k in range(3)])
    ext = mesh.extents
    length = float(max(ext[0], ext[1]))
    # No más gruesa que 1 mm: con facetas mayores la superficie se ve poligonal aunque el
    # suavizado sea perfecto (los escaneos de sensores de profundidad traen aristas de ~2 mm).
    res = min(0.8 * float(np.median(el)), 1.0)
    return float(np.clip(res, max(length / 700.0, 0.2), max(length / 150.0, 0.4)))


def inpaint_biharmonic(z: np.ndarray, known: np.ndarray, unknown: np.ndarray) -> np.ndarray:
    """Rellena ``unknown`` minimizando ||Laplaciano(z)||² con los valores conocidos fijos."""
    out = z.copy()
    if not unknown.any():
        return out
    domain = known | unknown
    sq = np.ones((5, 5), dtype=bool)
    local = ndimage.binary_dilation(unknown, structure=sq) & domain
    rows_mask = ndimage.binary_dilation(unknown, structure=np.ones((3, 3), dtype=bool)) & domain
    idx = -np.ones(z.shape, dtype=np.int64)
    idx[local] = np.arange(int(local.sum()))
    n = int(local.sum())
    ii, jj = np.nonzero(local)
    r_list, c_list, d_list = [], [], []
    deg = np.zeros(n)
    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        ni, nj = ii + di, jj + dj
        ok = (ni >= 0) & (ni < z.shape[0]) & (nj >= 0) & (nj < z.shape[1])
        nb = np.full(len(ii), -1, dtype=np.int64)
        nb[ok] = idx[ni[ok], nj[ok]]
        m = nb >= 0
        r_list.append(np.flatnonzero(m))
        c_list.append(nb[m])
        d_list.append(-np.ones(int(m.sum())))
        deg[m] += 1
    r_list.append(np.arange(n))
    c_list.append(np.arange(n))
    d_list.append(deg)
    lap = sparse.csr_matrix((np.concatenate(d_list), (np.concatenate(r_list), np.concatenate(c_list))), shape=(n, n))
    is_u = unknown[local]
    is_row = rows_mask[local]
    lap = lap[np.flatnonzero(is_row)]
    a = lap[:, np.flatnonzero(is_u)].tocsc()
    b = lap[:, np.flatnonzero(~is_u)]
    zk = z[local][~is_u]
    zk = np.where(np.isfinite(zk), zk, 0.0)
    rhs = -(a.T @ (b @ zk))
    ata = (a.T @ a).tocsc()
    ata = ata + 1e-9 * sparse.identity(ata.shape[0], format="csc")
    sol = splu(ata).solve(rhs)
    tmp = out[local]
    tmp[is_u] = sol
    out[local] = tmp
    return out


def extrapolate_planes(z: np.ndarray, valid: np.ndarray, target: np.ndarray, radius: float = 3.5) -> np.ndarray:
    """Extiende ``z`` a los nodos ``target`` con un plano local ajustado por mínimos cuadrados.

    Se usa en una banda estrecha por fuera del contorno: al llevar el borde a la curva
    suave, la altura continúa la pendiente real en lugar de quedarse "plana" (lo que
    levantaría el borde en zonas inclinadas). El ajuste promedia el ruido en vez de
    amplificarlo, a diferencia de una extrapolación armónica.
    """
    out = z.copy()
    ti, tj = np.nonzero(target)
    if len(ti) == 0 or not valid.any():
        return out
    r = int(np.ceil(radius))
    zp = np.pad(np.where(valid, z, 0.0), r)
    vp = np.pad(valid, r)
    acc = np.zeros((len(ti), 9))
    for di in range(-r, r + 1):
        for dj in range(-r, r + 1):
            d2 = di * di + dj * dj
            if d2 > radius * radius or d2 == 0:
                continue
            ok = vp[ti + r + di, tj + r + dj]
            if not ok.any():
                continue
            w = np.exp(-2.0 * d2 / (radius * radius)) * ok
            zz = zp[ti + r + di, tj + r + dj]
            for k, val in enumerate((1.0, di, dj, di * di, di * dj, dj * dj)):
                acc[:, k] += w * val
            acc[:, 6] += w * zz
            acc[:, 7] += w * zz * di
            acc[:, 8] += w * zz * dj
    s0, sx, sy, sxx, sxy, syy, sz, szx, szy = acc.T
    m = np.stack([np.stack([s0, sx, sy], 1), np.stack([sx, sxx, sxy], 1), np.stack([sy, sxy, syy], 1)], 1)
    m = m + 1e-6 * np.maximum(s0, 1e-12)[:, None, None] * np.eye(3)
    rhs = np.stack([sz, szx, szy], 1)
    has = s0 > 1e-9
    plane = np.zeros(len(ti))
    plane[has] = np.linalg.solve(m[has], rhs[has][:, :, None])[:, 0, 0]
    mean = np.where(has, sz / np.maximum(s0, 1e-12), 0.0)
    # plano mal condicionado (vecinos casi alineados): se usa la media ponderada
    det = np.linalg.det(m / np.maximum(s0, 1e-12)[:, None, None])
    val = np.where(det > 1e-3, plane, mean)
    out[ti[has], tj[has]] = val[has]
    return out


def _largest(mask: np.ndarray) -> tuple[np.ndarray, int]:
    lab, n = ndimage.label(mask)
    if n <= 1:
        return mask, 0
    sizes = np.bincount(lab.ravel())[1:]
    keep = lab == (1 + int(np.argmax(sizes)))
    return keep, int(sizes.sum() - sizes.max())


def _fill_holes(mask: np.ndarray, max_cells: float) -> tuple[np.ndarray, list[int]]:
    """Rellena agujeros interiores (todos si ``max_cells`` <= 0). Devuelve sus tamaños."""
    filled = ndimage.binary_fill_holes(mask)
    holes = filled & ~mask
    lab, n = ndimage.label(holes)
    if n == 0:
        return mask, []
    sizes = np.bincount(lab.ravel())[1:]
    if max_cells <= 0:
        return filled, sizes.tolist()
    ok = np.r_[False, sizes <= max_cells]
    return mask | ok[lab], sizes[sizes <= max_cells].tolist()


def _fix_pinches(cells: np.ndarray) -> np.ndarray:
    """Elimina configuraciones en diagonal que crearían vértices no variedad."""
    c = cells.copy()
    for _ in range(50):
        a, b = c[:-1, :-1], c[1:, 1:]
        d, e = c[1:, :-1], c[:-1, 1:]
        p1 = a & b & ~d & ~e
        p2 = d & e & ~a & ~b
        if not (p1.any() or p2.any()):
            break
        c[1:, 1:][p1] = False
        c[1:, :-1][p2] = False
    return c


def remesh_heightfield(mesh: Mesh, params: ContourParams | None = None, context: Mesh | None = None) -> dict:
    """Reconstruye la superficie (marco canónico) como malla de campo de alturas con contorno limpio.

    ``context`` (opcional) es geometría real del escaneo contigua a la selección (el borde que
    quedó fuera por pendiente o por un plano de corte). Solo se usa para dar altura a la
    estrecha banda exterior al contorno, de modo que el borde siga la superficie medida.
    """
    p = params or ContourParams()
    warnings: list[str] = []
    if mesh.n_faces == 0:
        raise ValueError("No hay superficie que remallar.")
    res = p.resolution if p.resolution > 0 else auto_resolution(mesh)
    v = mesh.vertices
    pad = int(np.ceil(max(p.contour_close, 2 * p.contour_smooth, p.contour_open) / res)) + 4
    grid = make_grid(v[:, :2].min(axis=0), v[:, :2].max(axis=0), res, pad=pad)
    res = grid.res
    cell_area = res * res

    tri = mesh.triangles
    cr = cross2(tri[:, 1, :2] - tri[:, 0, :2], tri[:, 2, :2] - tri[:, 0, :2])
    r = rasterize(tri, grid)
    covered = r["count"] > 0
    zmean = r["zsum"] / np.maximum(r["count"], 1)
    spread = np.where(covered, r["zmax"] - r["zmin"], 0.0)
    multi = spread > 2.0 * p.layer_tol
    z0 = np.where(multi, r["zmax"], zmean)
    z0[~covered] = np.nan

    # ---- máscara de la huella -> contorno regular
    m = covered.copy()
    if p.contour_close > 0:
        m = ndimage.binary_closing(m, structure=_disk(p.contour_close / res))
    closed_gap_area = float((m & ~covered).sum() * cell_area)
    max_cells = p.max_hole_area / cell_area if p.max_hole_area > 0 else 0.0
    m, hole_sizes = _fill_holes(m, max_cells)
    if p.contour_open > 0:
        m = ndimage.binary_opening(m, structure=_disk(p.contour_open / res))
    if p.contour_smooth > 0:
        # regularización del contorno: umbral de la máscara difuminada (flujo de curvatura aproximado)
        m = ndimage.gaussian_filter(m.astype(np.float32), p.contour_smooth / res) > 0.5
        m = ndimage.binary_opening(m, structure=np.ones((3, 3), dtype=bool))
    if p.contour_inset > 0:
        m = ndimage.binary_erosion(m, structure=_disk(p.contour_inset / res))
    m, dropped_nodes = _largest(m)
    if not m.any():
        raise ValueError("La región seleccionada es demasiado pequeña o estrecha para generar un contorno.")

    cells = m[:-1, :-1] & m[1:, :-1] & m[:-1, 1:] & m[1:, 1:]
    cells, hs2 = _fill_holes(cells, max_cells)
    cells = _fix_pinches(cells)
    cells, dropped_cells = _largest(cells)
    cells = _fix_pinches(cells)
    if not cells.any():
        raise ValueError("No se ha podido construir la rejilla de la superficie.")
    mv = np.zeros_like(m)
    mv[:-1, :-1] |= cells
    mv[1:, :-1] |= cells
    mv[:-1, 1:] |= cells
    mv[1:, 1:] |= cells

    # ---- alturas: datos reales + carenado biarmónico donde faltan
    unknown = mv & ~covered
    zf = inpaint_biharmonic(z0, covered, unknown)
    valid = covered | unknown
    band = ndimage.binary_dilation(mv, structure=np.ones((3, 3), dtype=bool), iterations=3) & ~valid
    zplane = extrapolate_planes(zf, valid, band)
    band_real = np.zeros_like(band)
    if context is not None and context.n_faces and band.any():
        rc = rasterize(context.triangles, grid)
        dist = ndimage.distance_transform_edt(~valid)
        band_real = band & (rc["count"] > 0)
        # solo si continúa la superficie (no otra capa): coherente con la extrapolación local
        band_real &= np.abs(np.where(band_real, rc["zmax"], 0.0) - zplane) <= 1.5 + 4.0 * res * dist
        zf[band_real] = rc["zmax"][band_real]
    rest = band & ~band_real
    zf[rest] = extrapolate_planes(zf, valid | band_real, rest)[rest]
    valid = valid | band
    _, (ni, nj) = ndimage.distance_transform_edt(~valid, return_indices=True)
    zext = zf[ni, nj]

    # ---- triangulación
    vid = -np.ones(mv.shape, dtype=np.int64)
    vid[mv] = np.arange(int(mv.sum()))
    gx, gy = grid.node_xy()
    ii, jj = np.nonzero(mv)
    verts = np.stack([gx[ii], gy[jj], zf[ii, jj]], axis=1)
    support = covered[ii, jj]
    ci, cj = np.nonzero(cells)
    p00, p10 = vid[ci, cj], vid[ci + 1, cj]
    p11, p01 = vid[ci + 1, cj + 1], vid[ci, cj + 1]
    d_a = np.abs(zf[ci, cj] - zf[ci + 1, cj + 1])
    d_b = np.abs(zf[ci + 1, cj] - zf[ci, cj + 1])
    use_a = d_a <= d_b
    # En las esquinas convexas del borde la diagonal debe salir del vértice de esquina;
    # si no, quedaría un triángulo "oreja" que degenera al alisar el contorno.
    cp = np.pad(cells, 1)
    left, right = ~cp[:-2, 1:-1][ci, cj], ~cp[2:, 1:-1][ci, cj]
    down, up = ~cp[1:-1, :-2][ci, cj], ~cp[1:-1, 2:][ci, cj]
    corner_a = (left & down) | (right & up)
    corner_b = (right & down) | (left & up)
    use_a = np.where(corner_a & ~corner_b, True, np.where(corner_b & ~corner_a, False, use_a))
    f1 = np.where(use_a[:, None], np.stack([p00, p10, p11], axis=1), np.stack([p00, p10, p01], axis=1))
    f2 = np.where(use_a[:, None], np.stack([p00, p11, p01], axis=1), np.stack([p10, p11, p01], axis=1))
    out = Mesh(verts, np.vstack([f1, f2]))

    # ---- contorno subcelda: el borde escalonado de la rejilla se lleva a la isolínea 0.5
    #      de la máscara difuminada (recta para cortes rectos, curva suave en el resto)
    loops = boundary_loops(out)
    n_loops = len(loops)
    contour_len = 0.0
    if loops:
        gfield = ndimage.gaussian_filter(mv.astype(np.float64), 2.0)
        gdx, gdy = np.gradient(gfield, res)

        def project(q, steps):
            for _ in range(steps):
                gq = grid.sample_bilinear(gfield, q)
                gr = np.stack([grid.sample_bilinear(gdx, q), grid.sample_bilinear(gdy, q)], axis=1)
                step = ((0.5 - gq) / np.maximum((gr ** 2).sum(axis=1), 1e-12))[:, None] * gr
                ns_ = np.linalg.norm(step, axis=1)
                q += step * np.minimum(1.0, 0.5 * res / np.maximum(ns_, 1e-12))[:, None]
            return q

        xy0 = out.vertices[:, :2].copy()
        disp = np.zeros_like(xy0)
        is_b = np.zeros(out.n_vertices, dtype=bool)
        for loop in loops:
            q = project(xy0[loop].copy(), 4)
            # suavizar a lo largo del contorno y reproyectar: iguala el espaciado sin encoger
            for _ in range(max(int(p.boundary_iters), 0)):
                q += 0.5 * (0.5 * (np.roll(q, 1, axis=0) + np.roll(q, -1, axis=0)) - q)
                q = project(q, 2)
            d = q - xy0[loop]
            nd = np.linalg.norm(d, axis=1)
            d *= np.minimum(1.0, 1.2 * res / np.maximum(nd, 1e-12))[:, None]
            disp[loop] = d
            is_b[loop] = True
        f = out.faces
        e_i = np.r_[f[:, 0], f[:, 1], f[:, 2], f[:, 1], f[:, 2], f[:, 0]]
        e_j = np.r_[f[:, 1], f[:, 2], f[:, 0], f[:, 0], f[:, 1], f[:, 2]]
        adj = sparse.csr_matrix((np.ones(len(e_i)), (e_i, e_j)), shape=(out.n_vertices,) * 2)
        adj.data[:] = 1.0
        deg = np.asarray(adj.sum(axis=1)).ravel()
        ring = np.where(is_b, 0, 99)
        cur = is_b.copy()
        for k in range(1, 5):
            nxt = (adj @ cur.astype(np.float64) > 0) & (ring == 99)
            ring[nxt] = k
            cur = nxt
        band = (ring >= 1) & (ring <= 4)
        for _ in range(20):
            avg = (adj @ disp) / np.maximum(deg, 1)[:, None]
            disp[band] = avg[band]
        min_area = 0.15 * cell_area
        for _ in range(6):
            xy = xy0 + disp
            t2 = xy[f]
            sa = cross2(t2[:, 1] - t2[:, 0], t2[:, 2] - t2[:, 0])
            bad = sa < min_area
            if not bad.any():
                break
            bv = np.unique(f[bad])
            disp[bv] *= 0.5
        else:
            xy = xy0 + disp
            t2 = xy[f]
            bad = cross2(t2[:, 1] - t2[:, 0], t2[:, 2] - t2[:, 0]) < min_area
            disp[np.unique(f[bad])] = 0.0
        xy = xy0 + disp
        moved = np.linalg.norm(disp, axis=1) > 1e-9
        out.vertices[:, :2] = xy
        out.vertices[moved, 2] = grid.sample_bilinear(zext, xy[moved])
        loops = boundary_loops(out)
    for loop in loops:
        q = out.vertices[loop]
        contour_len += float(np.linalg.norm(np.roll(q, -1, axis=0) - q, axis=1).sum())

    hole_areas = sorted([float(s * cell_area) for s in (hole_sizes + hs2)], reverse=True)
    filled_area = float(unknown.sum() * cell_area)
    if hole_areas and hole_areas[0] > 100.0:
        warnings.append(f"Se han rellenado {len(hole_areas)} agujero(s); el mayor mide {hole_areas[0]:.0f} mm². "
                        "Esa zona se ha interpolado y no procede del escaneo.")
    if dropped_nodes * cell_area > 50.0:
        warnings.append(f"Se han descartado regiones separadas de la principal ({dropped_nodes * cell_area:.0f} mm²).")
    if multi.sum() * cell_area > 20.0:
        warnings.append(f"Capas superpuestas no fusionables en {multi.sum() * cell_area:.0f} mm²: "
                        "se ha tomado la capa exterior. Revise la segmentación en esa zona.")
    if n_loops > 1:
        warnings.append(f"La superficie tiene {n_loops} contornos (quedan agujeros sin rellenar).")
    if (cr <= 0).mean() > 0.05:
        warnings.append("Parte de la selección mira en sentido contrario al eje plantar (voladizos); "
                        "se ha proyectado sobre el campo de alturas.")
    info = {
        "params": asdict(p),
        "resolution": float(res),
        "vertices": out.n_vertices,
        "faces": out.n_faces,
        "boundary_loops": n_loops,
        "contour_length": contour_len,
        "holes_filled": len(hole_areas),
        "hole_areas": hole_areas[:20],
        "interpolated_area": filled_area,
        "closed_gap_area": closed_gap_area,
        "multi_layer_area": float(multi.sum() * cell_area),
        "footprint_area": float(cells.sum() * cell_area),
        "warnings": warnings,
    }
    return {"mesh": out, "info": info, "support": support, "grid": grid, "zext": zext}
