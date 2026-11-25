"""Pruebas del corte de plantilla, de los dedos en el mapa de presión y de la revisión por IA.

La IA se prueba sin red: el cliente recibe un ``transport`` falso que devuelve respuestas
como las de la API de mensajes.
"""
from __future__ import annotations

import json

import numpy as np
import pytest
from scipy import ndimage

from core import synthetic as S
from core.ai_client import AIError, ClaudeClient, parse_json
from core.bilateral import SIDES
from core.bilateral_session import BilateralSession
from core.insole import frame_view
from core.mesh_io import stl_bytes
from core.pipeline import Pipeline, adjust_for_coverage, clamp_segment_params


@pytest.fixture(scope="module")
def insole():
    sc = S.make_scenario("limpio", ns=180, nt=90)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh), "limpio.stl")
    out = pipe.auto()
    return sc, pipe, out


def test_insole_cut_has_heel_arch_toes_and_walls(insole):
    sc, pipe, out = insole
    assert out["validacion"]["valid"], out["validacion"]["errors"]
    ins = out["segmentacion"]["insole"]
    assert ins and ins["wall_faces"] > 100                       # hay pared además de la planta
    cov = out["recortado"]["cobertura"]
    for key in ("talon", "mediopie", "antepie", "dedos"):
        assert cov["zones"][key]["coverage"] > 0.93, (key, cov["zones"][key])
    assert cov["toe_missing_mm"] < 5 and cov["heel_missing_mm"] < 5
    w = cov["walls"]
    assert w["talon"]["reached_mm"] > 12                         # copa del talón (objetivo 15 mm)
    assert w["mediopie_medial"]["reached_mm"] > 18               # pestaña medial hasta el arco
    assert w["mediopie_lateral"]["reached_mm"] > 8
    # la pared no sube por encima de la línea de recorte (el empeine no entra)
    frame = frame_view(ins["frame"])
    h = frame.height(pipe.stages["suavizado"].vertices)
    assert h.max() < 40


def test_planta_mode_keeps_only_the_sole(insole):
    sc, _, out = insole
    p = Pipeline()
    p.load(stl_bytes(sc.mesh), "limpio.stl")
    o = p.auto({"segment": {"mode": "planta"}})
    assert o["segmentacion"]["insole"] is None
    assert o["recortado"]["footprint_area"] < out["recortado"]["footprint_area"]


def test_insole_walls_do_not_grab_connected_brim():
    sc = S.make_scenario("conectado", ns=180, nt=90)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh), "c.stl")
    out = pipe.auto()
    assert out["validacion"]["valid"]
    cov = out["recortado"]["cobertura"]
    assert cov["ok"], cov["problems"]
    assert all(w["reached_mm"] < 32 for w in cov["walls"].values())   # el ala (a 40 mm) queda fuera


def test_coverage_rules_raise_parameters_when_toes_are_missing():
    cov = {"zones": {"dedos": {"coverage": 0.5}}, "toe_missing_mm": 20, "walls": {}, "extra_fraction": 0}
    new = adjust_for_coverage({"mode": "plantilla"}, cov)
    assert new["gap_tol"] > 6 and new["join_dz"] > 4 and new["max_angle"] > 62
    assert clamp_segment_params({"max_angle": 500, "heel_cup": -3, "foo": 1, "mode": "x"}) == \
        {"max_angle": 78.0, "heel_cup": 0.0}


# ------------------------------------------------------------------ dedos en el mapa de presión
@pytest.fixture(scope="module")
def flat_toes_session():
    left, right = S.make_foot_pair(seed=2, ns=200, nt=100, toe_lift=10.0, toe_pads=False)
    s = BilateralSession()
    s.load("izquierdo", stl_bytes(left), "izq.stl")
    s.load("derecho", stl_bytes(right), "der.stl")
    assert all(r["valid"] for r in s.process().values())
    return s


def _toe_pads(s, side):
    """Número de pulpejos: máximos locales de presión en la zona de los dedos (como los ve el ojo)."""
    arr = s.result["_arrays"]
    ff = arr["fields"][side]
    p = arr["pressure"][side]
    loc = (p == ndimage.maximum_filter(p, size=7)) & ff.toe_mask & (p > 0.08 * p.max())
    return int(loc.sum())


def test_toes_appear_as_separate_pads(flat_toes_session):
    s = flat_toes_session
    rep = s.simulate({"mass": 70})
    new = [_toe_pads(s, x) for x in SIDES]
    for side, n in zip(SIDES, new):
        f = rep["feet"][side]
        assert f["toes_in_contact"] >= 4
        assert n >= 4                                           # pulpejos separados, no una franja
        assert f["rearfoot_pct"] > 45 and f["toes_pct"] > 2
        toes_peak = max(t["peak_kPa"] for t in f["toes"])
        assert toes_peak < 0.45 * f["peak_kPa"]                  # hallux ≈ 20–26 % del máximo en estática
    # sin el relieve de los dedos (modelo anterior) los dedos de este escaneo forman una franja
    s.simulate({"mass": 70, "toe_definition": 0, "toe_pulp_lift": 0, "toe_spread": 4})
    old = [_toe_pads(s, x) for x in SIDES]
    assert max(old) < min(new)


# ------------------------------------------------------------------ IA (sin red)
def _fake(responses):
    calls = []

    def transport(body):
        calls.append(body)
        txt = responses[min(len(calls) - 1, len(responses) - 1)]
        return {"content": [{"type": "text", "text": txt}], "usage": {"input_tokens": 1000, "output_tokens": 50}}
    return transport, calls


def test_parse_json_variants():
    assert parse_json('bla ```json\n{"a": 1}\n``` fin') == {"a": 1}
    assert parse_json('Respuesta: {"a": {"b": "}"}} y más') == {"a": {"b": "}"}}
    with pytest.raises(AIError):
        parse_json("sin json")


def test_ai_cut_loop_applies_clamped_changes_and_stops(insole):
    from core.ai_tuner import tune_cut
    sc, _, _ = insole
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh), "limpio.stl")
    pipe.auto()
    transport, calls = _fake([
        json.dumps({"veredicto": "ajustar", "diagnostico": "copa del talón baja", "problemas": ["talón"],
                    "cambios": {"segment": {"heel_cup": 22, "max_angle": 999, "inventado": 3},
                                "cut": {"contour_smooth": 3}}, "confianza": 0.8}),
        json.dumps({"veredicto": "correcto", "diagnostico": "bien", "problemas": [], "cambios": {}}),
    ])
    out = tune_cut(pipe, ClaudeClient(transport=transport, model="claude-sonnet-5-5"), rounds=4)
    assert len(calls) == 2
    first = calls[0]
    assert first["model"] == "claude-sonnet-5-5"
    content = first["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert "heel_cup" in content[1]["text"] and "Cobertura" in content[1]["text"]
    ch = out["history"][0]["changes"]["segment"]
    assert ch["heel_cup"] == 22 and ch["max_angle"] == 78.0 and "inventado" not in ch
    assert pipe.reports["segmentacion"]["insole"]["targets"]["talon"] == 22
    assert out["history"][-1]["verdict"] == "correcto"
    assert out["validacion"]["valid"]
    assert out["usage"]["calls"] == 2


def test_ai_pressure_loop_changes_model_parameters(flat_toes_session):
    from core.ai_tuner import tune_pressure
    s = flat_toes_session
    s.simulate({"mass": 70})
    transport, calls = _fake([
        json.dumps({"veredicto": "ajustar", "diagnostico": "hallux demasiado cargado",
                    "cambios": {"simulacion": {"toe_flexor": 400, "toe_spread": 99},
                                "tejido": {"hallux": {"e0": 25}, "inexistente": {"e0": 5}}}}),
        json.dumps({"veredicto": "correcto", "diagnostico": "plausible", "cambios": {}}),
    ])
    out = tune_pressure(s, ClaudeClient(transport=transport), rounds=3, sim={"mass": 70})
    assert len(calls) == 2
    assert calls[0]["messages"][0]["content"][0]["type"] == "image"
    ch = out["history"][0]["changes"]
    assert ch["simulacion"] == {"toe_flexor": 400.0, "toe_spread": 5.0}
    assert ch["tejido"] == {"hallux": {"e0": 25.0}}
    rep = s.report()
    assert rep["params"]["toe_flexor"] == 400.0
    assert rep["tissue"]["izquierdo"]["hallux"]["e0"] == 25.0
    assert abs(rep["global"]["feet_force_N"] - 70 * 9.80665) < 1e-3


def test_ai_client_requires_key(monkeypatch, tmp_path):
    from core import ai_client
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ai_client, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(ai_client, "_runtime_key", None)
    with pytest.raises(AIError):
        ClaudeClient()
    with pytest.raises(AIError):
        ai_client.set_api_key("no-es-una-clave")
    st = ai_client.set_api_key("sk-ant-prueba-1234", persist=True)
    assert st["configured"] and (tmp_path / ".env").read_text().startswith("ANTHROPIC_API_KEY=sk-ant-prueba-1234")
    monkeypatch.setattr(ai_client, "_runtime_key", None)
    assert ai_client.get_api_key() == "sk-ant-prueba-1234"        # se lee del .env
