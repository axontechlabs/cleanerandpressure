#!/usr/bin/env python3
"""Procesa todos los escenarios sintéticos y resume las métricas (tabla Markdown).

Uso:  python scripts/report_scenarios.py [--ns 220 --nt 110]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import synthetic as S  # noqa: E402
from core.mesh_io import stl_bytes  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", type=int, default=220)
    ap.add_argument("--nt", type=int, default=110)
    a = ap.parse_args()
    print("| Escenario | Caras | Eliminado (área) | Error RMS vs. analítico | P99,5 | Desv. media / P95 vs. escaneo | "
          "Planta preservada | Δ arco | Sólido válido | Tiempo |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|:---:|---:|")
    for name in S.SCENARIOS:
        sc = S.make_scenario(name, ns=a.ns, nt=a.nt)
        pipe = Pipeline()
        t0 = time.time()
        pipe.load(stl_bytes(sc.mesh), f"{name}.stl")
        out = pipe.auto({"segment": {"mode": "planta"}})
        dt = time.time() - t0
        err = sc.height_error(pipe.mesh("suavizado").vertices)[pipe._support]
        q = out["validacion"]["quality"]
        sm = out["suavizado"]
        print(f"| {name} (σ={sc.noise_sigma:g} mm) | {sc.mesh.n_faces} | {out['segmentado']['removed_area_pct']:.1f} % | "
              f"{np.sqrt(np.nanmean(err ** 2)):.3f} mm | {np.nanpercentile(np.abs(err), 99.5):.2f} mm | "
              f"{q['deviation']['mean']:.3f} / {q['deviation']['p95']:.3f} mm | {q['preservation_pct']:.1f} % | "
              f"{sm['arch_depth_change']:+.2f} mm | {'sí' if out['validacion']['valid'] else 'NO'} | {dt:.1f} s |")


if __name__ == "__main__":
    main()
