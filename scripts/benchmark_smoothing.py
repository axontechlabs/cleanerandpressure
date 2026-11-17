#!/usr/bin/env python3
"""Compara métodos de suavizado sobre escaneos sintéticos con verdad-terreno analítica.

Uso:  python scripts/benchmark_smoothing.py

Para cada escenario se parte de la misma superficie recortada y se mide, frente a la
planta analítica: el error RMS, el sesgo en la zona del arco (positivo = el arco se
rellena), el error en el relieve de los dedos y la rugosidad residual (mediana del residuo
respecto a una media local: cuanto menor, más lisa). Sirve para justificar el método elegido.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy import sparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import synthetic as S  # noqa: E402
from core.mesh_io import stl_bytes  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.smoothing import SmoothParams, adaptive_smooth, multiscale_smooth  # noqa: E402


def umbrella(mesh):
    f = mesh.faces
    i = np.r_[f[:, 0], f[:, 1], f[:, 2], f[:, 1], f[:, 2], f[:, 0]]
    j = np.r_[f[:, 1], f[:, 2], f[:, 0], f[:, 0], f[:, 1], f[:, 2]]
    a = sparse.csr_matrix((np.ones(len(i)), (i, j)), shape=(mesh.n_vertices,) * 2)
    a.data[:] = 1.0
    deg = np.asarray(a.sum(axis=1)).ravel()
    return sparse.diags(1.0 / np.maximum(deg, 1)) @ a


def laplacian(mesh, iterations=10, lam=0.5):
    w = umbrella(mesh)
    z = mesh.vertices[:, 2].copy()
    for _ in range(iterations):
        z += lam * (w @ z - z)
    return z


def taubin(mesh, iterations=20, lam=0.5, mu=-0.53):
    w = umbrella(mesh)
    z = mesh.vertices[:, 2].copy()
    for _ in range(iterations):
        z += lam * (w @ z - z)
        z += mu * (w @ z - z)
    return z


def rms(x):
    x = np.asarray(x)[np.isfinite(x)]
    return float(np.sqrt((x ** 2).mean())) if len(x) else float("nan")


def main():
    print(f"{'escenario':10s} {'método':34s} {'RMS':>7s} {'sesgo arco':>11s} {'RMS dedos':>10s} {'rugosidad':>10s}")
    for name in ("limpio", "ruido", "completo"):
        sc = S.make_scenario(name, ns=220, nt=110)
        pipe = Pipeline()
        pipe.load(stl_bytes(sc.mesh), f"{name}.stl")
        pipe.segment()
        pipe.extract()
        pipe.cut()
        base = pipe.stages["recortado"]
        sup = pipe._support
        world = lambda z: pipe.frame.to_world(np.column_stack([base.vertices[:, :2], z]))  # noqa: E731
        p = sc.to_model_frame(world(base.vertices[:, 2]))
        arch = (p[:, 0] > 70) & (p[:, 0] < 170) & (p[:, 1] > 10) & sup
        toes = (p[:, 0] > 0.88 * S.L_FOOT) & sup
        methods = {
            "sin suavizar": base.vertices[:, 2],
            "Laplaciano (10 it, λ=0,5)": laplacian(base),
            "Laplaciano (40 it, λ=0,5)": laplacian(base, 40),
            "Taubin λ|μ (20 it)": taubin(base),
            "Taubin λ|μ (60 it)": taubin(base, 60),
            "MLS local (método alternativo)": adaptive_smooth(base, SmoothParams(
                method="local", intensity=0.8, max_deviation=0.8))["mesh"].vertices[:, 2],
            "Multiescala (por defecto)": multiscale_smooth(base, SmoothParams())["mesh"].vertices[:, 2],
            "Multiescala, tamaño máx. 16 mm": multiscale_smooth(base, SmoothParams(texture_scale=16))["mesh"].vertices[:, 2],
        }
        w = umbrella(base)
        w2 = w @ w
        for label, z in methods.items():
            e = sc.height_error(world(z))
            rough = float(np.median(np.abs(z - w2 @ (w2 @ z))))      # residuo respecto a una media local (2 anillos x2)
            print(f"{name:10s} {label:34s} {rms(e[sup]):7.3f} {np.nanmean(e[arch]):+11.3f} {rms(e[toes]):10.3f} {rough:10.4f}")
        print()


if __name__ == "__main__":
    main()
