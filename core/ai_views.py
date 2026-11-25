"""Imágenes de diagnóstico del corte (PNG) para la revisión por IA y para el informe.

Una figura con cuatro paneles, en el marco anatómico del pie (talón abajo, dedos arriba,
medial a la derecha en la vista desde el suelo):

1. Escaneo completo visto desde el suelo, en gris por altura, con lo que se conserva en color:
   planta (verde), pared de plantilla (azul), dudoso (amarillo).
2. Superficie resultante coloreada por altura sobre el suelo, con las líneas de zona.
3. Perfil medial y lateral (altura frente a longitud): escaneo en gris, resultado en color.
4. Cortes transversales en talón, arco, antepié y dedos.
"""
from __future__ import annotations

import io

import numpy as np

from .insole import ZONES, frame_view, trim_height
from .mesh import Mesh, boundary_loops
from .raster import make_grid, rasterize


def _anat(frame, p: np.ndarray):
    """(longitud desde el talón, mediolateral con medial positivo, altura sobre el suelo)."""
    s = frame.sn(p[:, :2]) * frame.length
    q = frame.q(p[:, :2]) * frame.medial_sign
    return s, q, frame.height(p)


def render_cut_views(full: Mesh, labels: np.ndarray | None, surface: Mesh | None, frame_d: dict,
                     targets: dict | None = None, title: str = "", dpi: int = 90,
                     reason: np.ndarray | None = None) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frame = frame_view(frame_d)
    fig, axs = plt.subplots(1, 4, figsize=(17, 6.4), dpi=dpi,
                            gridspec_kw={"width_ratios": [1, 1, 1.25, 1.25]})
    fig.patch.set_facecolor("white")
    # ---------------- 1. escaneo + selección (vista desde el suelo)
    tri = full.triangles.reshape(-1, 3)
    s, q, h = _anat(frame, tri)
    T = np.stack([q, s, -h], axis=1).reshape(-1, 3, 3)       # z mayor = más cerca del suelo
    lo = T.reshape(-1, 3)[:, :2].min(axis=0)
    hi = T.reshape(-1, 3)[:, :2].max(axis=0)
    g = make_grid(lo, hi, max(float(np.max(hi - lo)) / 260.0, 0.8), pad=2)
    r = rasterize(T, g)
    hmap = -r["zmax"]
    ax = axs[0]
    ext = (g.x0, g.x0 + g.res * (g.nx - 1), g.y0, g.y0 + g.res * (g.ny - 1))
    ax.imshow(np.clip(hmap, 0, 60).T, origin="lower", extent=ext, cmap="Greys", vmin=-10, vmax=70)
    if labels is not None:
        lab = np.asarray(labels).astype(np.int64).copy()
        if reason is not None:
            lab[(lab == 1) & (np.asarray(reason) == 3)] = 3          # pared de plantilla
        colors = {1: (0.15, 0.7, 0.3), 2: (0.95, 0.8, 0.1), 3: (0.15, 0.4, 0.95)}
        for code, col in colors.items():
            sel = lab == code
            if not sel.any():
                continue
            rr = rasterize(T[sel], g)
            # solo lo visible desde el suelo
            vis = (rr["count"] > 0) & (np.abs(rr["zmax"] - r["zmax"]) < 2.0)
            img = np.zeros(vis.shape + (4,))
            img[vis] = (*col, 0.55)
            ax.imshow(np.transpose(img, (1, 0, 2)), origin="lower", extent=ext)
    ax.set_title("Escaneo desde el suelo\n(verde = planta, azul = pared, amarillo = dudoso)", fontsize=9)
    ax.set_xlabel("lateral ←   mm   → medial", fontsize=8)
    ax.set_ylabel("talón → dedos (mm)", fontsize=8)
    ax.set_aspect("equal")
    # ---------------- 2. resultado por altura
    ax = axs[1]
    if surface is not None and surface.n_faces:
        ts = surface.triangles.reshape(-1, 3)
        s2, q2, h2 = _anat(frame, ts)
        T2 = np.stack([q2, s2, -h2], axis=1).reshape(-1, 3, 3)
        r2 = rasterize(T2, g)
        hm2 = np.ma.masked_invalid(-r2["zmax"])
        im = ax.imshow(hm2.T, origin="lower", extent=ext, cmap="turbo", vmin=0,
                       vmax=max(25.0, float(np.nanpercentile(-r2["zmax"], 99)) if np.isfinite(r2["zmax"]).any() else 25))
        fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02).set_label("altura sobre el suelo (mm)", fontsize=8)
        # relieve sombreado encima: deja ver escalones, ondas o dientes en la superficie y en el borde
        hz = np.where(np.isfinite(r2["zmax"]), -r2["zmax"], np.nan)
        gy, gx = np.gradient(np.nan_to_num(hz, nan=np.nanmin(hz) if np.isfinite(hz).any() else 0.0), g.res)
        shade = np.clip((-gx * 0.6 - gy * 0.6 + 1.0) / np.sqrt(1 + gx ** 2 + gy ** 2), 0, 1)
        shade_img = np.zeros(shade.shape + (4,))
        shade_img[..., :3] = shade[..., None]
        shade_img[..., 3] = np.where(np.isfinite(hz), 0.45, 0.0)
        ax.imshow(np.transpose(shade_img, (1, 0, 2)), origin="lower", extent=ext)
        for loop in boundary_loops(surface):
            pb = surface.vertices[loop]
            sb, qb, _ = _anat(frame, pb)
            ax.plot(np.r_[qb, qb[:1]], np.r_[sb, sb[:1]], color="k", lw=0.8)
    for key, name, a, b in ZONES:
        y = a * frame.length
        ax.axhline(y, color="white", lw=0.6, ls="--")
        ax.text(ext[0] + 2, min(y + 3, ext[3] - 5), name, color="white", fontsize=7)
    ax.set_xlim(ext[0], ext[1])
    ax.set_ylim(ext[2], ext[3])
    ax.set_facecolor("#202020")
    ax.set_title("Superficie de plantilla resultante\n(color = altura sobre el suelo, con relieve sombreado)", fontsize=9)
    ax.set_aspect("equal")
    # ---------------- 3. perfiles medial y lateral
    ax = axs[2]
    v = full.vertices
    sv, qv, hv = _anat(frame, v)
    half = np.interp(sv / frame.length, (np.arange(len(frame._half)) + 0.5) / len(frame._half), frame._half)
    ok = (hv > -5) & (hv < 70)
    side = frame.side(v[:, :2])
    rng = np.random.default_rng(0)
    pick = rng.choice(np.flatnonzero(ok), size=min(int(ok.sum()), 25000), replace=False) if ok.any() else []
    ax.scatter(sv[pick], hv[pick], s=0.3, c="#bbbbbb", rasterized=True, label="escaneo")
    if surface is not None and surface.n_faces:
        vs = surface.vertices
        ss, qs, hs = _anat(frame, vs)
        sd = frame.side(vs[:, :2])
        for m, col, lab_ in ((sd > 0.55, "#d62728", "resultado: borde medial"),
                             (sd < -0.55, "#1f77b4", "resultado: borde lateral")):
            pk = np.flatnonzero(m)
            if len(pk) > 15000:
                pk = rng.choice(pk, 15000, replace=False)
            ax.scatter(ss[pk], hs[pk], s=0.5, c=col, rasterized=True, label=lab_)
    if targets:
        sl = np.linspace(0, frame.length, 120)
        for sgn, col in ((1.0, "#d62728"), (-1.0, "#1f77b4")):
            # punto sobre el borde medial/lateral a cada longitud
            xy = (frame.center[None, :] + frame.e1[None, :] * (frame.s0 + sl)[:, None]
                  + frame.e2[None, :] * (np.interp(sl / frame.length, (np.arange(len(frame._qmid)) + 0.5) / len(frame._qmid),
                                                   frame._qmid) + sgn * frame.medial_sign
                                         * np.interp(sl / frame.length, (np.arange(len(frame._half)) + 0.5) / len(frame._half),
                                                     frame._half))[:, None])
            ax.plot(sl, trim_height(xy, frame, targets), color=col, ls="--", lw=1)
    ax.set_xlim(-5, frame.length + 5)
    ax.set_ylim(-3, 50)
    ax.set_xlabel("talón → dedos (mm)", fontsize=8)
    ax.set_ylabel("altura sobre el suelo (mm)", fontsize=8)
    ax.set_title("Perfil: gris = escaneo; rojo/azul = resultado medial/lateral\n"
                 "discontinua = línea de recorte objetivo", fontsize=9)
    ax.legend(fontsize=7, loc="upper left", markerscale=12)
    del half, side, qv
    # ---------------- 4. cortes transversales
    ax = axs[3]
    cols = ("#8c564b", "#2ca02c", "#9467bd", "#ff7f0e")
    for (frac, name), col in zip(((0.12, "talón 12 %"), (0.45, "arco 45 %"), (0.70, "antepié 70 %"),
                                  (0.90, "dedos 90 %")), cols):
        band = 1.5
        y0 = frac * frame.length
        m = ok & (np.abs(sv - y0) < band)
        ax.scatter(_anat(frame, v[m])[1], hv[m] + frac * 0, s=0.4, c="#cccccc", rasterized=True)
        if surface is not None and surface.n_faces:
            ss, qs, hs = _anat(frame, surface.vertices)
            ms = np.abs(ss - y0) < band
            o = np.argsort(qs[ms])
            ax.plot(qs[ms][o], hs[ms][o], ".", ms=1.5, color=col, label=name)
    ax.set_ylim(-3, 50)
    ax.set_xlabel("lateral ←   mm   → medial", fontsize=8)
    ax.set_ylabel("altura sobre el suelo (mm)", fontsize=8)
    ax.set_title("Cortes transversales (gris = escaneo, color = resultado)", fontsize=9)
    ax.legend(fontsize=7, markerscale=6)
    if title:
        fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
