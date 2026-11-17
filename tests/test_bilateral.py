"""Pruebas del modo bilateral: dos limpiezas independientes y una simulación conjunta."""
from __future__ import annotations

import csv
import io
import json

import numpy as np
import pytest

from core import synthetic as S
from core.bilateral import G, SIDES, BilateralError
from core.bilateral_session import BilateralSession
from core.mesh_cleaning import clean
from core.mesh_io import load_stl_bytes, stl_bytes
from core.mesh_validation import validate_solid


@pytest.fixture(scope="module")
def pair():
    left, right = S.make_foot_pair(ns=160, nt=80)
    return left, right


@pytest.fixture(scope="module")
def session(pair):
    s = BilateralSession()
    s.load("izquierdo", stl_bytes(pair[0]), "izq.stl")
    s.load("derecho", stl_bytes(pair[1]), "der.stl")
    rep = s.process()
    assert all(rep[x]["valid"] for x in SIDES)
    return s


def test_two_independent_valid_solids(session, pair):
    for side, scan in zip(SIDES, pair):
        stl = load_stl_bytes(session.export_stl(side))
        assert validate_solid(clean(stl)[0])["valid"]
    a = load_stl_bytes(session.export_stl("izquierdo"))
    b = load_stl_bytes(session.export_stl("derecho"))
    assert a.n_faces != b.n_faces or not np.allclose(a.vertices, b.vertices)   # no se fusionan


def test_side_detection_and_swap_warning(pair):
    s = BilateralSession()
    s.load("izquierdo", stl_bytes(pair[1]), "der.stl")      # cruzados a propósito
    s.load("derecho", stl_bytes(pair[0]), "izq.stl")
    s.process()
    rep = s.place()
    assert rep["feet"]["izquierdo"]["detected_side"] == "derecho"
    assert rep["feet"]["derecho"]["detected_side"] == "izquierdo"
    assert rep["feet"]["izquierdo"]["warnings"]
    s.swap()
    s.process()
    rep = s.place()
    assert all(rep["feet"][x]["detected_side"] == x for x in SIDES)
    assert all(rep["feet"][x]["side_confidence"] == "alta" for x in SIDES)


def test_force_equilibrium_and_split(session):
    for split in (50.0, 45.0, 62.0):
        r = session.simulate({"mass": 80.0, "split_left": split})
        g = r["global"]
        W = 80.0 * G
        assert g["weight_N"] == pytest.approx(W)
        assert abs(g["feet_force_N"] - W) < 1e-6 * W            # equilibrio exacto de fuerzas
        assert r["feet"]["izquierdo"]["force_N"] == pytest.approx(W * split / 100, rel=1e-6)
        assert r["feet"]["derecho"]["force_N"] == pytest.approx(W * (1 - split / 100), rel=1e-6)
        # nunca el peso completo en cada pie
        assert r["feet"]["izquierdo"]["force_N"] + r["feet"]["derecho"]["force_N"] == pytest.approx(W, rel=1e-9)


def test_toes_settle_and_carry_light_load(session):
    """Los dedos se asientan por gravedad y apoyan con poca carga (bipedestación descalza)."""
    r = session.simulate({"mass": 70.0})
    for x in SIDES:
        f = r["feet"][x]
        assert f["toe_contact_area_cm2"] > 2.0                  # los dedos aparecen en el mapa
        assert 0.0 < f["toes_pct"] < 10.0                        # carga ligera
        toes_peak = max(q["peak_kPa"] for q in f["regions"] if q["key"] in ("hallux", "dedos"))
        heel_peak = max(q["peak_kPa"] for q in f["regions"] if q["key"].startswith("talon"))
        assert toes_peak < 0.5 * heel_peak
        assert "Talón medial" in f["initial_support_regions"] or "Talón lateral" in f["initial_support_regions"]
    no_tone = session.simulate({"mass": 70.0, "toe_flexor": 0.0})
    assert no_tone["feet"]["izquierdo"]["toes_pct"] < r["feet"]["izquierdo"]["toes_pct"]


def test_reference_load_distribution(session):
    """Reparto clásico en bipedestación: el talón lleva la mayor parte y el mediopié poca."""
    r = session.simulate({"mass": 70.0})
    for x in SIDES:
        f = r["feet"][x]
        assert 50.0 < f["rearfoot_pct"] < 72.0
        assert f["midfoot_pct"] < 15.0
        assert 20.0 < f["forefoot_pct"] < 40.0
        heel_peak = max(q["peak_kPa"] for q in f["regions"] if q["key"].startswith("talon"))
        assert heel_peak == pytest.approx(f["peak_kPa"])          # pico en el talón


def test_arch_drop_and_loaded_scan(session):
    with_drop = session.simulate({"mass": 70.0})
    loaded = session.simulate({"mass": 70.0, "loaded_scan": True})
    for x in SIDES:
        assert with_drop["feet"][x]["arch_drop_mm"] == pytest.approx(6.0, rel=1e-6)
        assert loaded["feet"][x]["arch_drop_mm"] == 0.0
        assert with_drop["feet"][x]["midfoot_pct"] >= loaded["feet"][x]["midfoot_pct"] - 1e-6


def test_moment_equilibrium_of_each_foot(session):
    r = session.simulate({"mass": 70.0, "load_point": 0.40})
    for x in SIDES:
        f = r["feet"][x]
        assert f["cop_error_mm"] < 0.5
        assert f["cop_from_heel_pct"] == pytest.approx(40.0, abs=0.5)


def test_physiological_ranges(session):
    r = session.simulate({"mass": 70.0})
    for x in SIDES:
        f = r["feet"][x]
        assert 50 < f["peak_kPa"] < 250
        assert 60 < f["contact_area_cm2"] < 180
        assert f["rearfoot_pct"] > 30                       # el retropié lleva una parte importante
        assert f["max_tissue_strain_pct"] < 70
        assert abs(f["toe_equilibrium_error_Nmm"]) < 0.01 * f["force_N"] * 50.0     # equilibrio de los dedos
    assert r["global"]["mean_kPa"] == pytest.approx(r["global"]["feet_force_N"] / (r["global"]["contact_area_cm2"] * 1e-4) / 1e3)


def test_feet_with_different_arches_give_different_maps(session):
    r = session.simulate({"mass": 70.0})
    left, right = r["feet"]["izquierdo"], r["feet"]["derecho"]
    differs = [abs(left[k] - right[k]) for k in ("arch_index", "contact_area_cm2", "peak_kPa")]
    assert max(differs) > 0.01
    mid_l = next(q for q in left["regions"] if q["key"] == "mediopie")
    mid_r = next(q for q in right["regions"] if q["key"] == "mediopie")
    assert mid_l["pct_foot"] != pytest.approx(mid_r["pct_foot"], abs=0.2)   # el arco alto (derecho) carga distinto
    # las regiones suman la fuerza del pie
    for f in (left, right):
        assert sum(q["force_N"] for q in f["regions"]) == pytest.approx(f["force_N"], rel=1e-6)


def test_centre_of_mass_mode(session):
    r0 = session.simulate({"mass": 70.0, "mode": "centro_masas", "com_x": 0.0, "com_y_frac": 0.32})
    r1 = session.simulate({"mass": 70.0, "mode": "centro_masas", "com_x": 25.0, "com_y_frac": 0.32})
    for r in (r0, r1):
        g = r["global"]
        assert np.allclose(g["cop_world_mm"], g["com_projection_mm"], atol=0.5)
        assert g["moment_error_Nmm"] < 0.5 * g["weight_N"]       # < 0,5 mm de brazo
        assert abs(g["force_error_N"]) < 1e-6 * g["weight_N"]
    assert r1["global"]["split_right_pct"] > r0["global"]["split_right_pct"] + 5
    with pytest.raises(BilateralError):
        session.simulate({"mass": 70.0, "mode": "centro_masas", "com_x": 600.0})


def test_feet_do_not_overlap_and_originals_untouched(session):
    before = {x: session.export_stl(x) for x in SIDES}
    rep = session.place({"heel_separation": 30.0, "toe_out_left": 0, "toe_out_right": 0})
    assert rep["stance"]["heel_separation_mm"] > 30.0
    assert rep["stance"]["warnings"]
    r = session.simulate({"mass": 70.0})
    arr = session.result["_arrays"]
    assert not (arr["fields"]["izquierdo"].mask & arr["fields"]["derecho"].mask).any()
    assert r["global"]["force_error_pct"] == pytest.approx(0.0, abs=1e-6)
    for x in SIDES:
        assert session.export_stl(x) == before[x]                # la colocación no toca los STL limpios
    session.place({})


def test_exports(session):
    session.simulate({"mass": 70.0})
    for w in ("bilateral", "izquierdo", "derecho"):
        png = session.map_png(w)
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20000
    rows = list(csv.reader(io.StringIO(session.export_csv().decode())))
    assert rows[0][:6] == ["pie", "x_mm", "y_mm", "x_local_mm", "y_local_mm", "presion_kPa"]
    assert {r[0] for r in rows[1:]} == set(SIDES)
    data = json.loads(session.export_json())
    assert {"global", "feet", "comparison", "tissue", "placement", "processing"} <= set(data)
    assert data["global"]["split_source"].startswith("hipótesis")


def test_regional_properties_change_only_their_foot(session):
    base = session.simulate({"mass": 70.0})
    soft = session.simulate({"mass": 70.0, "stiffness_scale": {"izquierdo": 0.5, "derecho": 1.0}})
    assert soft["feet"]["izquierdo"]["contact_area_cm2"] > base["feet"]["izquierdo"]["contact_area_cm2"]
    assert soft["feet"]["derecho"]["contact_area_cm2"] == pytest.approx(base["feet"]["derecho"]["contact_area_cm2"], rel=1e-9)


def test_bilateral_web_api(pair):
    pytest.importorskip("starlette")
    uvicorn = pytest.importorskip("uvicorn")
    import socket
    import threading
    import time
    import urllib.request

    from app.server import app

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, data=None):
        req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
        with opener.open(req, timeout=240) as r:
            return r.read()

    for _ in range(100):
        try:
            call("/api/bi/state")
            break
        except OSError:
            time.sleep(0.1)
    try:
        assert b"bilateral.js" in call("/bilateral")
        assert b'href="/bilateral"' in call("/")
        call("/api/bi/upload/izquierdo?name=i.stl", stl_bytes(pair[0]))
        call("/api/bi/upload/derecho?name=d.stl", stl_bytes(pair[1]))
        res = json.loads(call("/api/bi/process", b"{}"))
        assert all(res["report"][x]["valid"] for x in SIDES)
        body = json.dumps({"sim": {"mass": 70, "split_left": 47}, "stance": {}}).encode()
        res = json.loads(call("/api/bi/simulate", body))
        assert res["report"]["global"]["split_left_pct"] == pytest.approx(47.0, abs=1e-6)
        head = np.frombuffer(call("/api/bi/mesh/izquierdo")[:12], dtype="<u4")
        fp = np.frombuffer(call("/api/bi/facepressure/izquierdo"), dtype="<f4")
        assert len(fp) == head[2] and fp.max() > 0
        assert call("/api/bi/map/bilateral.png")[:4] == b"\x89PNG"
        assert call("/api/bi/export/csv").startswith(b"pie,")
    finally:
        server.should_exit = True
