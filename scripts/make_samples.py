#!/usr/bin/env python3
"""Genera los escaneos sintéticos de ejemplo en ``samples/``.

Uso:  python scripts/make_samples.py [--all] [--ns 160 --nt 80]
Por defecto escribe solo ``sintetico_limpio.stl`` y ``sintetico_completo.stl``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import synthetic as S  # noqa: E402
from core.mesh_io import stl_bytes  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="generar los 8 escenarios")
    ap.add_argument("--ns", type=int, default=160)
    ap.add_argument("--nt", type=int, default=80)
    a = ap.parse_args()
    out = Path(__file__).resolve().parents[1] / "samples"
    out.mkdir(exist_ok=True)
    for name in (S.SCENARIOS if a.all else ("limpio", "completo")):
        sc = S.make_scenario(name, ns=a.ns, nt=a.nt)
        path = out / f"sintetico_{name}.stl"
        path.write_bytes(stl_bytes(sc.mesh))
        print(f"{path.name}: {sc.mesh.n_faces} caras - {sc.description}")


if __name__ == "__main__":
    main()
