#!/usr/bin/env python3
"""Simulación bilateral sin interfaz.

Uso:
  python cli_bilateral.py izquierdo.stl derecho.stl --peso 70 -o resultados/
  python cli_bilateral.py izq.stl der.stl --peso 82 --reparto-izquierdo 45 -o salida/
  python cli_bilateral.py izq.stl der.stl --peso 70 --centro-masas 15 38 -o salida/

Escribe en la carpeta de salida: los dos STL limpios (independientes, en el marco de su
archivo), el mapa bilateral y los individuales en PNG, el CSV de presiones y el JSON de
resultados. Código de salida 0 si todo es válido, 2 si algún sólido no es válido, 1 si hay error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.bilateral import BilateralError
from core.bilateral_session import BilateralSession
from core.pipeline import PipelineError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("izquierdo")
    ap.add_argument("derecho")
    ap.add_argument("--peso", type=float, required=True, help="masa corporal en kg")
    ap.add_argument("-o", "--salida", default="salida_bilateral")
    ap.add_argument("--reparto-izquierdo", type=float, default=50.0, help="%% del peso en el pie izquierdo (hipótesis)")
    ap.add_argument("--centro-masas", type=float, nargs=2, metavar=("X_MM", "Y_PCT"),
                    help="proyección del centro de masas: mm a la derecha del punto medio y %% de la longitud del pie")
    ap.add_argument("--separacion", type=float, default=170.0, help="mm entre centros de talón")
    ap.add_argument("--apertura", type=float, nargs=2, default=(7.0, 7.0), metavar=("IZQ", "DER"))
    ap.add_argument("--linea-carga", type=float, default=38.0, help="%% de la longitud desde el talón")
    ap.add_argument("--config", help="JSON con {'limpieza':{}, 'postura':{}, 'simulacion':{}}")
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text()) if a.config else {}
    out = Path(a.salida)
    out.mkdir(parents=True, exist_ok=True)
    s = BilateralSession()
    try:
        for side, path in (("izquierdo", a.izquierdo), ("derecho", a.derecho)):
            s.load(side, Path(path).read_bytes(), Path(path).name)
        proc = s.process(cfg.get("limpieza"))
        for side, r in proc.items():
            print(f"Pie {side}: {'válido' if r['valid'] else 'NO VÁLIDO'} ({r['seconds']} s)")
        if not all(r["valid"] for r in proc.values()):
            return 2
        for side in ("izquierdo", "derecho"):
            (out / f"pie_{side}_limpio.stl").write_bytes(s.export_stl(side))
        stance = {"heel_separation": a.separacion, "toe_out_left": a.apertura[0], "toe_out_right": a.apertura[1],
                  "load_point": a.linea_carga / 100.0, **cfg.get("postura", {})}
        sim = {"mass": a.peso, "split_left": a.reparto_izquierdo, "load_point": a.linea_carga / 100.0,
               **cfg.get("simulacion", {})}
        if a.centro_masas:
            sim.update(mode="centro_masas", com_x=a.centro_masas[0], com_y_frac=a.centro_masas[1] / 100.0)
        rep = s.simulate(sim, stance)
        for w in ("bilateral", "izquierdo", "derecho"):
            (out / f"mapa_presion_{w}.png").write_bytes(s.map_png(w))
        (out / "presiones_bilateral.csv").write_bytes(s.export_csv())
        (out / "resultados_bilateral.json").write_bytes(s.export_json())
        g = rep["global"]
        print(f"Peso {g['weight_N']:.1f} N · pies {g['feet_force_N']:.1f} N · reparto I/D "
              f"{g['split_left_pct']:.1f}/{g['split_right_pct']:.1f} % · máx. {g['peak_kPa']:.0f} kPa · "
              f"contacto {g['contact_area_cm2']:.0f} cm²")
        for w in rep["warnings"] + sum((f.get("warnings", []) for f in rep["placement"]["feet"].values()), []):
            print("Aviso:", w)
        print(f"Resultados en {out.resolve()}")
        return 0
    except (BilateralError, PipelineError) as e:
        print("Error:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
