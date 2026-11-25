"""Resultados exportados en inglés (la interfaz sigue en castellano).

El modelo y la interfaz trabajan en castellano; al exportar (JSON, CSV, mapas PNG, nombres de
archivo) se traduce aquí:

* claves: ``izquierdo``/``derecho`` → ``left``/``right``; regiones (``talon_medial`` →
  ``heel_medial``…);
* textos conocidos (regiones, dedos, etiquetas, fuentes) con una tabla fija;
* avisos del modelo con patrones;
* lo que quede en castellano (avisos del procesamiento, etc.) se traduce con Claude si la IA está
  conectada; si no, se deja tal cual.
"""
from __future__ import annotations

import csv
import io
import json
import re

SIDE = {"izquierdo": "left", "derecho": "right", "ambos": "both"}
KEY = {"talon_medial": "heel_medial", "talon_lateral": "heel_lateral", "mediopie": "midfoot",
       "dedos": "lesser_toes", "izquierdo": "left", "derecho": "right"}
PHRASES = {
    "Talón medial": "Medial heel", "Talón lateral": "Lateral heel", "Mediopié": "Midfoot",
    "Antepié medial (1.er MTT)": "Medial forefoot (MT 1)", "Antepié central (2.º–3.er MTT)": "Central forefoot (MT 2–3)",
    "Antepié lateral (4.º–5.º MTT)": "Lateral forefoot (MT 4–5)", "Hallux": "Hallux",
    "Dedos 2.º–5.º": "Lesser toes (2nd–5th)", "Hallux (1.º)": "Hallux (1st)", "2.º dedo": "2nd toe",
    "3.º dedo": "3rd toe", "4.º dedo": "4th toe", "5.º dedo": "5th toe",
    "Talón": "Heel", "Antepié": "Forefoot", "Dedos": "Toes", "Mediopié / arco": "Midfoot / arch",
    "Fuerza": "Force", "Peso soportado": "Body weight supported", "Superficie de contacto": "Contact area",
    "Presión media": "Mean pressure", "Presión máxima": "Peak pressure", "Carga en retropié": "Rearfoot load",
    "Carga en mediopié": "Midfoot load", "Carga en antepié (metatarsos)": "Forefoot load (metatarsals)",
    "Carga en dedos": "Toe load", "Índice del arco": "Arch index", "CoP desde el borde del talón": "CoP from heel edge",
    "% del pie": "% of foot", "% longitud": "% of length", "% peso": "% body weight",
    "arco alto (índice ≤ 0,21)": "high arch (index ≤ 0.21)", "arco normal (0,21–0,26)": "normal arch (0.21–0.26)",
    "arco bajo (índice ≥ 0,26)": "low arch (index ≥ 0.26)",
    "hipótesis introducida por el usuario (no es una medición)": "user-supplied assumption (not a measurement)",
    "resultado del equilibrio de momentos con el centro de masas indicado":
        "result of moment equilibrium with the given centre of mass",
    "media de la población (McIlroy y Maki, 1997)": "population mean (McIlroy & Maki, 1997)",
    "según la longitud del pie (Nielsen et al., 2009)": "from foot length (Nielsen et al., 2009)",
    "escaneo en carga": "weight-bearing scan", "indicado por el usuario": "user-specified",
    "alta": "high", "media": "medium", "baja": "low", "reparto": "split", "centro_masas": "centre_of_mass",
    "plantilla": "insole", "planta": "sole",
    "No se pudo determinar el plano de apoyo; se usa la orientación del escaneo.":
        "The support plane could not be determined; the scan orientation is used.",
}
TOE_LOWER = {k.lower(): v for k, v in PHRASES.items() if "dedo" in k or "Hallux" in k}


def _side(s: str) -> str:
    return SIDE.get(s, s)


def _toe(s: str) -> str:
    return TOE_LOWER.get(s.lower(), s)


PATTERNS = [
    (r"^Pie (\w+): el (.+) no llega al suelo ni girando (\d+)° desde la postura del escaneo .*$",
     lambda m: f"{_side(m[1]).capitalize()} foot: the {_toe(m[2])} does not reach the ground even after rotating "
               f"{m[3]}° from the scanned posture (toe very raised or clawed): it is left unsupported."),
    (r"^Pie (\w+): el (.+) apenas carga \(por debajo del umbral de contacto\)\.$",
     lambda m: f"{_side(m[1]).capitalize()} foot: the {_toe(m[2])} barely carries load (below the contact threshold)."),
    (r"^Pie (\w+): (el hallux|los dedos 2\.º–5\.º) solo apoya con el (\d+) % del momento flexor previsto.*$",
     lambda m: f"{_side(m[1]).capitalize()} foot: {'the hallux' if 'hallux' in m[2] else 'the lesser toes'} only "
               f"provide {m[3]} % of the expected flexor moment: their contact surface in the scan is small "
               "(toe poorly captured or clawed?)."),
    (r"^Pie (\w+): algún dedo ha tenido que girar más de 30° para apoyar.*$",
     lambda m: f"{_side(m[1]).capitalize()} foot: some toe had to rotate more than 30° to touch the ground: the toes "
               "were very raised in the scan. Check that they are not clawed toes."),
    (r"^Pie (\w+): compresión máxima del tejido del (\d+) %.*$",
     lambda m: f"{_side(m[1]).capitalize()} foot: maximum tissue compression {m[2]} %, outside the range where the "
               "model is reliable (check body weight and tissue properties)."),
    (r"^Con la separación indicada las huellas se solapaban o quedaban a menos de ([\d.,]+) mm: se ha aumentado "
     r"la separación entre talones a (\d+) mm\.$",
     lambda m: f"With the requested separation the footprints overlapped or were closer than {m[1]} mm: the heel "
               f"separation was increased to {m[2]} mm."),
    (r"^El pie (\w+) mide (\d+) mm, el ([\d.,]+) % de la estatura \((\d+) cm\).*$",
     lambda m: f"The {_side(m[1])} foot is {m[2]} mm long, {m[3]} % of body height ({m[4]} cm); 14–16 % is usual. "
               "Check body height and units."),
    (r"^No se ha podido calcular el pie en carga: (.*)$",
     lambda m: f"The loaded (weight-bearing) foot could not be computed: {m[1]}"),
    (r"^(.+) \(carga\)$", lambda m: f"{PHRASES.get(m[1], m[1])} (load)"),
]
_SPANISH = re.compile(r"[áéíóúñ¿¡]|\b(el|la|los|las|de|del|pie|con|por|sin|que|una?|se|en|y)\b", re.I)
_cache: dict[str, str] = {}


def translate(s: str) -> str:
    if s in PHRASES:
        return PHRASES[s]
    if s in SIDE:
        return SIDE[s]
    if s in KEY:
        return KEY[s]
    if s.startswith("region_") and s[7:] in KEY:
        return "region_" + KEY[s[7:]]
    for pat, fn in PATTERNS:
        m = re.match(pat, s)
        if m:
            return fn(m)
    return _cache.get(s, s)


def _collect_spanish(o, out: set):
    if isinstance(o, dict):
        for v in o.values():
            _collect_spanish(v, out)
    elif isinstance(o, list):
        for v in o:
            _collect_spanish(v, out)
    elif isinstance(o, str) and len(o) > 3 and _SPANISH.search(o):
        out.add(o)


def _ai_translate(texts: list[str]) -> None:
    """Traduce con Claude lo que no cubren las tablas (solo si la IA está conectada)."""
    texts = [t for t in texts if t not in _cache]
    if not texts:
        return
    try:
        from .ai_client import ClaudeClient, get_api_key, status
        if not get_api_key() or not status().get("connected"):
            return
        c = ClaudeClient(timeout=60.0, max_retries=1)
        res = c.ask_json("You translate technical messages of a foot-scan processing and plantar-pressure program "
                         "from Spanish to English. Keep numbers, units and symbols. Answer only with JSON "
                         '{"translations": {"<spanish>": "<english>", ...}}.',
                         json.dumps({"texts": texts[:120]}, ensure_ascii=False), max_tokens=8000)
        for k, v in (res.get("translations") or {}).items():
            if isinstance(v, str):
                _cache[k] = v
    except Exception:  # noqa: BLE001 - si falla, se deja en castellano
        return


def to_english(o):
    if isinstance(o, dict):
        return {KEY.get(k, SIDE.get(k, k)) if isinstance(k, str) else k: to_english(v) for k, v in o.items()}
    if isinstance(o, list):
        return [to_english(v) for v in o]
    if isinstance(o, str):
        return translate(o)
    return o


def json_to_english(data: bytes) -> bytes:
    obj = json.loads(data)
    first = to_english(obj)
    left = set()
    _collect_spanish(first, left)
    if left:
        _ai_translate(sorted(left))
        first = to_english(first)
    return json.dumps(first, ensure_ascii=False, indent=1).encode("utf-8")


CSV_HEADER = {"pie": "side", "presion_kPa": "pressure_kPa", "contacto": "contact", "region": "region",
              "altura_sin_carga_mm": "unloaded_height_mm"}


def csv_to_english(data: bytes) -> bytes:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    if not rows:
        return data
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([CSV_HEADER.get(h, h) for h in rows[0]])
    for r in rows[1:]:
        w.writerow([SIDE.get(c, KEY.get(c, c)) for c in r])
    return out.getvalue().encode("utf-8")
