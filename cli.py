#!/usr/bin/env python3
"""Procesamiento por línea de comandos (sin interfaz).

Ejemplos:
    python cli.py escaneo.stl -o planta_solida.stl
    python cli.py escaneo.stl -o planta.obj --thickness 8 --mode offset --report informe.json
    python cli.py escaneo.stl -o suave.ply --stage suavizado --config parametros.json
    python cli.py escaneo.stl -o plantilla.stl --ia 3 --vistas vistas.png   # revisión del corte por IA
    python cli.py escaneo.stl -o planta.stl --corte planta                   # solo planta (sin paredes)

Código de salida: 0 si el sólido supera la validación, 2 si no, 1 en caso de error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.ai_client import AIError
from core.pipeline import STAGES, Pipeline, PipelineError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extrae la planta de un escaneo STL y genera un sólido cerrado.")
    ap.add_argument("input", help="archivo STL de entrada")
    ap.add_argument("-o", "--output", required=True, help="archivo de salida (.stl, .obj o .ply)")
    ap.add_argument("--stage", default="solido", choices=STAGES[1:], help="etapa que se exporta")
    ap.add_argument("--frame", default="original", choices=("original", "canonical"),
                    help="original: orientación del escaneo; canonical: base apoyada en Z=0")
    ap.add_argument("--thickness", type=float, help="espesor del sólido en mm")
    ap.add_argument("--mode", choices=("flat", "offset"), help="base plana o espesor uniforme")
    ap.add_argument("--side", choices=("interior", "exterior"), help="lado del material")
    ap.add_argument("--resolution", type=float, help="resolución del remallado en mm (0 = automática)")
    ap.add_argument("--flip", action="store_true", help="invertir la cara plantar detectada")
    ap.add_argument("--unit-scale", type=float, help="factor a milímetros (por defecto se deduce)")
    ap.add_argument("--config", help="JSON con parámetros: {segment:{}, cut:{}, smooth:{}, solid:{}}")
    ap.add_argument("--report", help="guardar el informe completo en JSON")
    ap.add_argument("--force", action="store_true", help="exportar aunque la validación falle")
    ap.add_argument("--corte", choices=("plantilla", "planta"),
                    help="plantilla (por defecto): planta + copa de talón + pestañas; planta: solo planta")
    ap.add_argument("--copa-talon", type=float, help="altura de la copa del talón sobre el suelo (mm)")
    ap.add_argument("--ia", type=int, default=0, metavar="RONDAS",
                    help="revisar el corte con Claude (necesita ANTHROPIC_API_KEY o .env); 0 = no")
    ap.add_argument("--modelo", help="modelo de Claude para la revisión (por defecto claude-sonnet-5-5)")
    ap.add_argument("--vistas", help="guardar en PNG las vistas del corte (las que analiza la IA)")
    args = ap.parse_args(argv)

    params = json.loads(Path(args.config).read_text()) if args.config else {}
    solid = params.setdefault("solid", {})
    for key in ("thickness", "mode", "side"):
        if getattr(args, key) is not None:
            solid[key] = getattr(args, key)
    if args.resolution is not None:
        params.setdefault("cut", {})["resolution"] = args.resolution
    if args.corte:
        params.setdefault("segment", {})["mode"] = args.corte
    if args.copa_talon is not None:
        params.setdefault("segment", {})["heel_cup"] = args.copa_talon

    pipe = Pipeline()
    try:
        load = pipe.load(Path(args.input).read_bytes(), Path(args.input).name, args.unit_scale)
        print(f"Cargado: {load['faces']} caras, {' x '.join(f'{d:.1f}' for d in load['dimensions_mm'])} mm")
        if args.flip:
            pipe.set_orientation(flip=True)
        out = pipe.auto(params)
        cov = out["recortado"].get("cobertura") or {}
        if cov.get("zones"):
            print("Plantilla: cobertura " + ", ".join(f"{z['name']} {100 * z['coverage']:.0f}%" for z in cov["zones"].values())
                  + f" · {len(pipe.tuning)} intento(s) de corte")
        if args.ia > 0:
            from core.ai_client import ClaudeClient
            from core.ai_tuner import tune_cut
            res = tune_cut(pipe, ClaudeClient(model=args.modelo), rounds=args.ia, planes=params.get("planes"),
                           log=lambda e: print(f"IA ronda {e['round']}: {e['verdict']} — {e['diagnosis']} "
                                               f"{json.dumps(e['changes'], ensure_ascii=False)}"))
            out["validacion"] = res["validacion"]
            print(f"IA: {res['usage']['calls']} llamada(s), {res['usage']['input_tokens']} tokens de entrada, "
                  f"{res['usage']['output_tokens']} de salida ({res['model']})")
        if args.vistas and (pipe.reports.get("segmentacion") or {}).get("insole"):
            from core.ai_tuner import cut_views_png
            Path(args.vistas).write_bytes(cut_views_png(pipe, title=Path(args.input).name))
    except (PipelineError, AIError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    seg, val = out["segmentacion"], out["validacion"]
    q = val["quality"]
    print(f"Segmentación: se elimina el {seg['removed_area_pct']:.1f}% del área; "
          f"{seg['uncertain_regions']} región(es) dudosa(s)")
    print(f"Contorno: resolución {out['recortado']['resolution']:.2f} mm, "
          f"{out['recortado']['holes_filled']} agujero(s) rellenado(s)")
    print(f"Desviación respecto al escaneo: media {q['deviation']['mean']:.3f} mm, "
          f"P95 {q['deviation']['p95']:.3f} mm, máx. {q['deviation']['max']:.2f} mm")
    print(f"Sólido: {val['faces']} caras, volumen {val['volume'] / 1000:.1f} cm³")
    for c in val["checks"]:
        print(f"  [{'OK' if c['ok'] else 'FALLO'}] {c['name']}: {c['value']} {c['detail']}".rstrip())
    for w in val["warnings"]:
        print(f"  AVISO: {w}")
    if args.report:
        from app.server import _clean  # reutiliza la conversión a JSON
        Path(args.report).write_text(json.dumps(_clean(pipe.reports), ensure_ascii=False, indent=2))
    ok = val["valid"]
    if args.stage == "solido" and not ok and not args.force:
        print("El sólido NO es válido: no se exporta (use --force para exportarlo igualmente).", file=sys.stderr)
        return 2
    from core.mesh_io import export_bytes
    Path(args.output).write_bytes(export_bytes(pipe.mesh(args.stage, args.frame), Path(args.output).suffix))
    print(f"{'VÁLIDO' if ok else 'NO VÁLIDO'} -> {args.output}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
