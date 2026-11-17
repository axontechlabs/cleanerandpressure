"""Mapas de presión plantar en PNG (estilo baropodometría estática).

Fondo negro, gradiente térmico azul → cian → verde → amarillo → naranja → rojo y una única
escala absoluta en kPa compartida por los dos pies y por los mapas individuales, para que
las diferencias sean comparables. Vista superior: pie izquierdo a la izquierda, dedos arriba.
"""
from __future__ import annotations

import io

import numpy as np

THERMAL = ["#0000ff", "#00ffff", "#00ff00", "#ffff00", "#ff8000", "#ff0000"]


def thermal_cmap():
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list("termico", THERMAL, N=256)


def nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    e = 10 ** np.floor(np.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= v:
            return float(m * e)
    return float(10 * e)


def render_map(result: dict, which: str = "bilateral", dpi: int = 150) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    arr = result["_arrays"]
    grid = arr["grid"]
    xs, ys = grid.node_xy()
    thr = result["params"]["contact_threshold"]
    vmax = nice_max(result["global"]["peak_kPa"])
    sides = ("izquierdo", "derecho") if which == "bilateral" else (which,)
    masks = {s: arr["fields"][s].mask for s in sides}
    union = np.zeros(grid.shape, dtype=bool)
    for m in masks.values():
        union |= m
    ii, jj = np.nonzero(union)
    pad = 12.0
    x0, x1 = xs[ii.min()] - pad, xs[ii.max()] + pad
    y0, y1 = ys[jj.min()] - pad, ys[jj.max()] + pad
    p = np.zeros(grid.shape)
    for s in sides:
        p = p + arr["pressure"][s]
    shown = np.ma.masked_where(p <= thr, p)

    w_mm, h_mm = x1 - x0, y1 - y0
    fig_h = 7.5
    fig_w = max(4.6, fig_h * w_mm / h_mm * 0.82 + 1.9)
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor="black")
    ax = fig.add_axes([0.03, 0.10, 0.80, 0.80])
    ax.set_facecolor("black")
    cmap = thermal_cmap().with_extremes(bad="black")
    im = ax.imshow(shown.T, origin="lower", extent=(xs[0] - grid.res / 2, xs[-1] + grid.res / 2,
                                                    ys[0] - grid.res / 2, ys[-1] + grid.res / 2),
                   cmap=cmap, vmin=0.0, vmax=vmax, interpolation="bilinear")
    for s in sides:
        ax.contour(xs, ys, masks[s].T.astype(float), levels=[0.5], colors="#5a5a5a", linewidths=0.8)
    feet = result["feet"]
    for s in sides:
        f = feet[s]
        cx, cy = f["cop_world_mm"]
        ax.plot(cx, cy, marker="+", color="white", markersize=14, mew=2)
        m = masks[s]
        mi, mj = np.nonzero(m)
        tx = xs[mi].mean()
        ax.text(tx, ys[mj].min() - 7, f"{s.capitalize()}\n{f['pct_body']:.1f} % · {f['force_N']:.0f} N\n"
                f"máx. {f['peak_kPa']:.0f} kPa · {f['contact_area_cm2']:.0f} cm²",
                color="white", ha="center", va="top", fontsize=9)
    if which == "bilateral":
        gx, gy = result["global"]["cop_world_mm"]
        ax.plot(gx, gy, marker="o", mfc="none", mec="white", markersize=11, mew=1.8)
        ax.plot(gx, gy, marker=".", color="white", markersize=4)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0 - 34, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    g = result["global"]
    title = ("Presión plantar · bipedestación estática bilateral" if which == "bilateral"
             else f"Presión plantar · pie {which}")
    sub = (f"{g['mass_kg']:.1f} kg · {g['weight_N']:.0f} N · reparto I/D {g['split_left_pct']:.1f} / "
           f"{g['split_right_pct']:.1f} %" if which == "bilateral" else
           f"{feet[which]['force_N']:.0f} N ({feet[which]['pct_body']:.1f} % del peso) · escala común con el mapa bilateral")
    fig.text(0.03, 0.965, title, color="white", fontsize=12, weight="bold", va="top")
    fig.text(0.03, 0.93, sub, color="#cfcfcf", fontsize=9, va="top")
    cax = fig.add_axes([0.86, 0.14, 0.035, 0.72])
    cb = fig.colorbar(im, cax=cax)
    cb.set_label("Presión (kPa)", color="white")
    cb.ax.yaxis.set_tick_params(color="white")
    for t in cb.ax.get_yticklabels():
        t.set_color("white")
    cb.outline.set_edgecolor("#888888")
    legend = "+  centro de presiones del pie" + ("\n○  centro de presiones global" if which == "bilateral" else "")
    fig.text(0.03, 0.03, legend, color="#cfcfcf", fontsize=8, va="bottom")
    fig.text(0.97, 0.03, "Simulación (no es una medición)", color="#8a8a8a", fontsize=7, ha="right", va="bottom")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="black")
    plt.close(fig)
    return buf.getvalue()
