"""Revisión por IA: Claude mira el resultado, lo juzga y reajusta los parámetros para repetirlo.

Dos bucles independientes:

* ``tune_cut``: corte de plantilla de un pie. En cada ronda se envían a Claude las vistas del
  corte (``ai_views.render_cut_views``), la cobertura medida (``insole.coverage_report``) y los
  parámetros actuales con sus límites. Claude responde en JSON si el corte es correcto o qué
  parámetros cambiar; se vuelve a cortar y se repite. Nunca se aceptan valores fuera de los
  límites, y si un cambio empeora claramente la cobertura medida se deshace.
* ``tune_pressure``: simulación bilateral. Se envía el mapa de presión y las cifras por región y
  por dedo, junto con los rangos de referencia de la baropodometría estática; Claude propone
  parámetros del modelo (dedos, tejido, reparto lateral…) y se vuelve a simular.

La IA solo elige parámetros dentro de rangos acotados: la geometría y la física las calcula
siempre el código. Todo queda registrado en el historial (qué vio, qué dijo, qué cambió).
"""
from __future__ import annotations

import copy
import json

import numpy as np

from .ai_client import AIError, ClaudeClient
from .ai_views import render_cut_views
from .pipeline import TUNE_LIMITS, Pipeline, clamp_segment_params

CUT_LIMITS = {"contour_close": (0.0, 8.0), "contour_open": (0.0, 5.0), "contour_smooth": (0.0, 6.0),
              "contour_inset": (0.0, 5.0)}

SEG_DOC = {
    "max_angle": "º de pendiente hasta la que crece la planta (subir = conserva más arco y bordes; bajar si se cuela pared ajena)",
    "seed_angle": "º de pendiente de las caras semilla (bajar si una superficie inclinada ajena se toma por planta)",
    "crease_angle": "º de ángulo diedro que corta la continuidad (bajar si se cuela una lámina unida por un pliegue; subir si surcos reales parten la planta)",
    "layer_tol": "mm de separación entre capas superpuestas",
    "gap_tol": "mm: distancia máxima isla–planta para conservar una isla (dedos separados por un surco)",
    "join_dz": "mm: salto de altura máximo isla–planta (dedos levantados)",
    "bridge_gap": "mm: huecos entre regiones conservadas que se rellenan con caras reales",
    "min_island_area": "mm²: islas menores se descartan",
    "normal_radius": "mm: suavizado de normales antes de medir pendientes (subir con mucho ruido)",
    "heel_cup": "mm sobre el suelo: altura de la copa del talón",
    "medial_flange": "mm sobre el suelo: pestaña medial bajo el arco (0 = automática: altura del arco + 6)",
    "lateral_flange": "mm sobre el suelo: pared lateral del mediopié",
    "forefoot_wall": "mm sobre el suelo: borde del antepié",
    "toe_wall": "mm sobre el suelo: borde de los dedos",
    "wall_min": "mm: pared mínima por encima del borde de la planta en todo el contorno",
    "wall_max_angle": "º: pendiente máxima de pared que se acepta (más vertical no es representable)",
    "wall_max_reach": "mm: distancia máxima en planta de la pared a la planta",
}
CUT_DOC = {
    "contour_close": "mm: radio de cierre del contorno (une grietas entre dedos)",
    "contour_open": "mm: radio de apertura (elimina picos y lenguas finas del borde)",
    "contour_smooth": "mm: suavidad del trazado del contorno",
    "contour_inset": "mm: retranqueo del borde hacia dentro",
}

CUT_SYSTEM = """Eres un técnico experto en ortopedia y CAD de plantillas personalizadas, y en procesado de \
escaneos 3D de pies (sensores de profundidad tipo iPhone TrueDepth: ruido, huecos, calcetín, capas duplicadas).

Revisas el corte automático de UN escaneo de pie. El objetivo es la superficie que necesita una plantilla \
personalizada: TODA la planta (talón completo, arco/puente, antepié con las cabezas metatarsianas y los cinco \
dedos) más algo de pared: copa del talón, pestaña medial bajo el arco y pared lateral, hasta la línea de recorte \
indicada (alturas sobre el suelo). No debe incluir geometría ajena (suelo, cristal, fragmentos, pierna, empeine, \
capas duplicadas) ni perder anatomía real. Es preferible conservar una zona dudosa a perder planta.

Recibes una imagen con 4 paneles (marco anatómico: talón abajo, dedos arriba; medial a la derecha):
1) el escaneo visto desde el suelo en gris y lo conservado en color (verde planta, azul pared, amarillo dudoso);
2) la superficie resultante coloreada por altura sobre el suelo, con el contorno y las zonas;
3) perfil de altura frente a longitud: gris = escaneo, rojo = borde medial del resultado, azul = borde lateral; \
las líneas discontinuas son la línea de recorte objetivo;
4) cortes transversales en talón, arco, antepié y dedos (gris = escaneo, color = resultado).
También recibes la cobertura medida por el programa (fracción de lo que el escaneo permite conservar en cada zona, \
milímetros que faltan por la punta o el talón, altura de pared alcanzada) y los parámetros actuales con sus límites.

Decide si el corte es correcto. Si no lo es, propone cambios CONCRETOS de parámetros (solo los de la lista, dentro \
de sus límites, pocos cada vez y razonados por lo que ves). No cambies lo que ya está bien.

Responde SOLO con un objeto JSON, sin texto adicional:
{"veredicto": "correcto" | "ajustar",
 "diagnostico": "frase breve en español de lo que ves",
 "problemas": ["…"],
 "cambios": {"segment": {"parametro": valor, …}, "cut": {"parametro": valor, …}},
 "confianza": 0.0-1.0}"""

PRESSURE_SYSTEM = """Eres un experto en biomecánica del pie y baropodometría (plataformas de presión en \
bipedestación estática). Revisas una SIMULACIÓN de presión plantar bilateral obtenida a partir de dos escaneos 3D \
sin carga y un modelo físico (tejido blando no lineal por regiones, arco que desciende, cinco dedos articulados \
que giran en su articulación metatarsofalángica hasta apoyar y se aprietan con el momento flexor).

Referencias de bipedestación estática en adultos sanos (orientativas):
- reparto del pie: retropié ~55–66 %, antepié (metatarsos + dedos) ~33–45 %, mediopié bajo (~5–15 %), unido el \
talón con el antepié por una banda lateral del mediopié; huella normal: índice del arco 0,21–0,26;
- los cinco dedos suelen tocar el suelo y verse como manchas separadas (pulpejos), no como una franja continua;
- presión en los dedos respecto a la máxima del pie: hallux ~20–26 %, 2.º dedo ~18–24 %, dedos 3.º–5.º ~13–16 %;
- presión máxima del pie en estática típicamente del orden de 100–200 kPa, en el talón o en las cabezas \
metatarsianas; reparto izquierdo/derecho ~50/50.
Los valores exactos dependen del sensor y de la persona: busca un mapa plausible, no forzar números.

Recibes la imagen del mapa bilateral (escala común en kPa), las cifras por región y por dedo, los avisos del \
modelo y los parámetros ajustables con sus límites. Decide si el mapa es realista; si no, propone cambios \
CONCRETOS (solo parámetros de la lista, dentro de sus límites, pocos cada vez, explicando la razón física).

Responde SOLO con un objeto JSON, sin texto adicional:
{"veredicto": "correcto" | "ajustar",
 "diagnostico": "frase breve en español",
 "problemas": ["…"],
 "cambios": {"simulacion": {"parametro": valor, …}, "tejido": {"region": {"e0": v, "thickness": v, "beta": v}}},
 "confianza": 0.0-1.0}"""

SIM_LIMITS = {
    "toe_flexor": (100.0, 2000.0), "toe_definition": (0.0, 1.5), "toe_pulp_lift": (0.0, 5.0),
    "toe_spread": (0.5, 5.0), "spread": (1.5, 8.0), "nonlinearity_scale": (0.5, 2.0),
    "toe_max_flexion": (10.0, 80.0), "arch_drop": (0.0, 15.0), "load_point": (0.25, 0.45),
}
SIM_DOC = {
    "toe_flexor": "N·mm por pie (con medio peso): momento flexor de los dedos; más = más carga en los dedos",
    "toe_definition": "0–1,5: relieve redondeado de cada dedo; más = pulpejos más separados y concentrados",
    "toe_pulp_lift": "mm: elevación de la falange proximal; más = el dedo apoya solo con el pulpejo distal",
    "toe_spread": "mm: reparto lateral del tejido en los dedos; más = manchas de los dedos más difusas y menos picudas",
    "spread": "mm: reparto lateral del tejido en la planta",
    "nonlinearity_scale": "factor sobre la rigidización del tejido; más = picos más altos y zonas de contacto más pequeñas",
    "toe_max_flexion": "º: giro máximo de un dedo hasta apoyar",
    "arch_drop": "mm: descenso del arco con medio peso (null = según la longitud del pie)",
    "load_point": "fracción de la longitud desde el talón donde cae la línea de carga (más = más carga delante)",
}
TISSUE_LIMITS = {"e0": (10.0, 120.0), "thickness": (3.0, 30.0), "beta": (1.0, 15.0)}


def _clamp(d: dict, limits: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if k not in limits:
            continue
        if v is None:
            if k == "arch_drop":
                out[k] = None
            continue
        try:
            out[k] = float(np.clip(float(v), *limits[k]))
        except (TypeError, ValueError):
            continue
    return out


def _param_table(current: dict, limits: dict, doc: dict) -> str:
    rows = []
    for k, (lo, hi) in limits.items():
        rows.append(f"- {k} = {current.get(k)!r}  [{lo:g} … {hi:g}]  {doc.get(k, '')}")
    return "\n".join(rows)


def _jdump(o) -> str:
    def clean(x):
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(v) for v in x]
        if isinstance(x, np.ndarray):
            return clean(x.tolist())
        if isinstance(x, (np.floating, float)):
            return round(float(x), 3) if np.isfinite(float(x)) else None
        if isinstance(x, np.integer):
            return int(x)
        if isinstance(x, np.bool_):
            return bool(x)
        return x
    return json.dumps(clean(o), ensure_ascii=False)


# ============================================================================ corte
def cut_snapshot(pipe: Pipeline) -> dict:
    seg = pipe.reports.get("segmentacion") or {}
    rec = pipe.reports.get("recortado") or {}
    return {"segment": {k: v for k, v in (seg.get("params") or {}).items() if k in TUNE_LIMITS or k == "mode"},
            "cut": {k: v for k, v in (rec.get("params") or {}).items() if k in CUT_LIMITS},
            "coverage": rec.get("cobertura") or {}}


def cut_views_png(pipe: Pipeline, title: str = "") -> bytes:
    seg = pipe.reports["segmentacion"]
    ins = seg.get("insole")
    if not ins:
        raise AIError("La revisión por IA necesita el corte en modo «plantilla».")
    surf = pipe.stages.get("suavizado") or pipe.stages.get("recortado")
    return render_cut_views(pipe.work_c, pipe.labels, surf, ins["frame"], ins["targets"], title=title,
                            reason=pipe.reason)


def tune_cut(pipe: Pipeline, client: ClaudeClient, rounds: int = 3, planes=None, log=None,
             finish: bool = True) -> dict:
    """Bucle de revisión del corte por IA. Si ``finish``, termina con suavizado, sólido y validación."""
    if "recortado" not in pipe.stages:
        pipe.autotune_cut({}, None, planes)
    snap = cut_snapshot(pipe)
    seg, cut = dict(snap["segment"]), dict(snap["cut"])
    seg.setdefault("mode", "plantilla")
    best_score = snap["coverage"].get("score", 0.0)
    history = []
    for r in range(max(int(rounds), 1)):
        cov = pipe.reports["recortado"].get("cobertura") or {}
        png = cut_views_png(pipe, title=f"{pipe.name or 'escaneo'} · ronda {r + 1}")
        text = (f"Escaneo: {pipe.name}. Ronda {r + 1} de {rounds}.\n\n"
                f"Cobertura medida por el programa:\n{_jdump(cov)}\n\n"
                f"Marco anatómico y línea de recorte objetivo:\n"
                f"{_jdump({k: v for k, v in pipe.reports['segmentacion']['insole'].items() if k != 'frame'})}\n"
                f"Altura del arco medida: {pipe.reports['segmentacion']['insole']['frame'].get('arch_height_mm', 0):.1f} mm; "
                f"longitud del pie: {pipe.reports['segmentacion']['insole']['frame'].get('length_mm', 0):.0f} mm.\n\n"
                f"Parámetros de segmentación (segment):\n{_param_table(seg, TUNE_LIMITS, SEG_DOC)}\n\n"
                f"Parámetros de contorno (cut):\n{_param_table(cut, CUT_LIMITS, CUT_DOC)}\n\n"
                + (f"Rondas anteriores:\n{_jdump(history)}\n\n" if history else "")
                + "Devuelve el JSON.")
        resp = client.ask_json(CUT_SYSTEM, text, [png])
        ch = resp.get("cambios") or {}
        new_seg = {**seg, **clamp_segment_params(ch.get("segment") or {})}
        new_cut = {**cut, **_clamp(ch.get("cut") or {}, CUT_LIMITS)}
        entry = {"round": r + 1, "source": "ia", "verdict": resp.get("veredicto"),
                 "diagnosis": resp.get("diagnostico"), "problems": resp.get("problemas") or [],
                 "confidence": resp.get("confianza"), "score_before": cov.get("score"),
                 "changes": {"segment": {k: v for k, v in new_seg.items() if seg.get(k) != v},
                             "cut": {k: v for k, v in new_cut.items() if cut.get(k) != v}}}
        history.append(entry)
        if log:
            log(entry)
        if resp.get("veredicto") == "correcto" or (not entry["changes"]["segment"] and not entry["changes"]["cut"]):
            break
        prev = (dict(seg), dict(cut))
        seg, cut = new_seg, new_cut
        cov2 = pipe.run_cut(seg, cut, planes)
        entry["score_after"] = cov2.get("score")
        # salvaguarda: un cambio que empeora claramente la cobertura medida se deshace
        if cov2.get("score", 0) < best_score - 0.04:
            entry["reverted"] = True
            seg, cut = prev
            pipe.run_cut(seg, cut, planes)
        else:
            best_score = max(best_score, cov2.get("score", 0))
    out = {"history": history, "final_params": {"segment": seg, "cut": cut},
           "coverage": pipe.reports["recortado"].get("cobertura"), "usage": dict(client.usage),
           "model": client.model}
    pipe.tuning = list(pipe.tuning) + history
    pipe.reports["recortado"]["ia"] = out
    if finish:
        pipe.smooth()
        pipe.solid()
        out["validacion"] = pipe.validate()
    return out


# ============================================================================ presión
def pressure_summary(rep: dict) -> dict:
    feet = {}
    for s, f in rep["feet"].items():
        feet[s] = {
            "peso_%": f["pct_body"], "pico_kPa": f["peak_kPa"], "area_contacto_cm2": f["contact_area_cm2"],
            "retropie_%": f["rearfoot_pct"], "mediopie_%": f["midfoot_pct"], "antepie_metatarsos_%": f["forefoot_pct"],
            "dedos_%": f["toes_pct"], "indice_arco": f["arch_index"], "huella_continua": f["footprint_continuous"],
            "banda_mediopie_min_mm": f["midfoot_band_min_width_mm"], "dedos_en_contacto": f["toes_in_contact"],
            "regiones": {r["key"]: {"%pie": r["pct_foot"], "pico_kPa": r["peak_kPa"], "area_cm2": r["contact_area_cm2"]}
                         for r in f["regions"]},
            "dedos": [{"dedo": t["name"], "pico_kPa": t["peak_kPa"], "pico_%_del_max": 100 * t["peak_kPa"] / max(f["peak_kPa"], 1e-9),
                       "fuerza_N": t["force_N"], "area_cm2": t["contact_area_cm2"], "toca": t["touches"],
                       "giro_asentamiento_deg": t["rest_deg"]} for t in f["toes"]],
            "deformacion_max_tejido_%": f["region_strain_pct"],
        }
    return {"global": {k: rep["global"][k] for k in ("mass_kg", "split_left_pct", "split_right_pct", "peak_kPa",
                                                     "contact_area_cm2")},
            "pies": feet, "avisos": rep.get("warnings", [])}


def tune_pressure(session, client: ClaudeClient, rounds: int = 3, sim: dict | None = None,
                  stance: dict | None = None, log=None) -> dict:
    """Bucle de revisión del mapa de presión por IA (sesión bilateral)."""
    sim = copy.deepcopy(sim or {})
    if session.result is None:
        session.simulate(sim, stance)
    history = []
    for r in range(max(int(rounds), 1)):
        rep = session.report()
        png = session.map_png("bilateral")
        cur = {**{k: rep["params"].get(k) for k in SIM_LIMITS}, **{k: v for k, v in sim.items() if k in SIM_LIMITS}}
        tissue = rep.get("tissue", {}).get("izquierdo", {})
        text = (f"Ronda {r + 1} de {rounds}.\n\nResultados de la simulación:\n{_jdump(pressure_summary(rep))}\n\n"
                f"Parámetros ajustables (simulacion):\n{_param_table(cur, SIM_LIMITS, SIM_DOC)}\n\n"
                f"Tejido por región (tejido: e0 kPa [{TISSUE_LIMITS['e0'][0]:g}…{TISSUE_LIMITS['e0'][1]:g}], "
                f"thickness mm [{TISSUE_LIMITS['thickness'][0]:g}…{TISSUE_LIMITS['thickness'][1]:g}], "
                f"beta [{TISSUE_LIMITS['beta'][0]:g}…{TISSUE_LIMITS['beta'][1]:g}]); se aplica a los dos pies:\n"
                f"{_jdump(tissue)}\n\n"
                + (f"Rondas anteriores:\n{_jdump(history)}\n\n" if history else "")
                + "Devuelve el JSON.")
        resp = client.ask_json(PRESSURE_SYSTEM, text, [png])
        ch = resp.get("cambios") or {}
        new_sim = _clamp(ch.get("simulacion") or {}, SIM_LIMITS)
        tch = {}
        for reg, vals in (ch.get("tejido") or {}).items():
            if reg in tissue and isinstance(vals, dict):
                c = _clamp(vals, TISSUE_LIMITS)
                if c:
                    tch[reg] = c
        entry = {"round": r + 1, "source": "ia", "verdict": resp.get("veredicto"),
                 "diagnosis": resp.get("diagnostico"), "problems": resp.get("problemas") or [],
                 "confidence": resp.get("confianza"),
                 "changes": {"simulacion": {k: v for k, v in new_sim.items() if cur.get(k) != v}, "tejido": tch}}
        history.append(entry)
        if log:
            log(entry)
        if resp.get("veredicto") == "correcto" or (not entry["changes"]["simulacion"] and not tch):
            break
        sim.update(new_sim)
        if tch:
            t = sim.setdefault("tissue", {})
            for side in ("izquierdo", "derecho"):
                ts = t.setdefault(side, {})
                for reg, vals in tch.items():
                    ts.setdefault(reg, {}).update(vals)
        session.simulate(sim, None)
    out = {"history": history, "final_params": sim, "usage": dict(client.usage), "model": client.model}
    if session.result is not None:
        session.result["ia"] = out
    return out
