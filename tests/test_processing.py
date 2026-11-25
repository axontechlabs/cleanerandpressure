"""Pruebas del procesamiento con modelos sintéticos de verdad-terreno conocida.

Ejecutar:  python -m pytest -q
"""
from __future__ import annotations

import functools

import numpy as np
import pytest

from core import synthetic as S
from core.mesh import Mesh, boundary_loops, build_topology
from core.mesh_analysis import analyze, detect_unit_scale
from core.mesh_cleaning import clean
from core.mesh_cutting import plane_clip
from core.mesh_io import export_bytes, load_stl_bytes, stl_bytes
from core.mesh_reconstruction import SolidParams, build_solid
from core.mesh_validation import self_intersections, validate_solid
from core.pipeline import Pipeline, PipelineError
from core.surface_remesh import inpaint_biharmonic

NS, NT = 220, 110      # resolución de los modelos de prueba (~48 000 caras)


def rms(x):
    x = np.asarray(x)[np.isfinite(x)]
    return float(np.sqrt((x ** 2).mean()))


@functools.lru_cache(maxsize=None)
def processed(name: str):
    sc = S.make_scenario(name, ns=NS, nt=NT)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh), f"{name}.stl")
    # Fidelidad frente a la planta analítica: se mide en modo «planta» (sin las paredes de la
    # plantilla, casi verticales, donde un error lateral mínimo es un error grande en altura). El
    # corte de plantilla se prueba en tests/test_insole_ai.py.
    out = pipe.auto({"segment": {"mode": "planta"}})
    return sc, pipe, out


def cube() -> Mesh:
    v = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], float)
    f = [[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
         [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]]
    return Mesh(v, f)


# ------------------------------------------------------------------ unidades básicas
def test_stl_roundtrip_binary_and_ascii():
    m = cube()
    back = load_stl_bytes(stl_bytes(m))
    assert back.n_faces == 12
    welded, rep = clean(back)
    assert welded.n_vertices == 8 and abs(welded.volume - 1.0) < 1e-6
    lines = ["solid t"]
    for t in m.triangles:
        lines += ["facet normal 0 0 0", "outer loop"] + [f"vertex {p[0]} {p[1]} {p[2]}" for p in t] + ["endloop", "endfacet"]
    ascii_mesh = load_stl_bytes(("\n".join(lines) + "\nendsolid t\n").encode())
    assert ascii_mesh.n_faces == 12
    assert export_bytes(m, "obj").count(b"\nf ") == 12
    assert export_bytes(m, "ply").startswith(b"ply")


def test_validation_accepts_cube_and_rejects_defects():
    assert validate_solid(cube())["valid"]
    opened = Mesh(cube().vertices, cube().faces[:-2])
    rep = validate_solid(opened)
    assert not rep["valid"] and any("bordes" in e.lower() for e in rep["errors"])
    inverted = validate_solid(cube().flipped())
    assert not inverted["valid"] and any("Volumen" in e for e in inverted["errors"])
    c = cube()
    two = Mesh(np.vstack([c.vertices, c.vertices + 5.0]), np.vstack([c.faces, c.faces + 8]))
    assert not validate_solid(two)["valid"]
    # dos cubos que se atraviesan: estancos pero con autointersección
    inter = Mesh(np.vstack([c.vertices, c.vertices + 0.5]), np.vstack([c.faces, c.faces + 8]))
    rep = validate_solid(inter)
    ids = {ch["id"]: ch["ok"] for ch in rep["checks"]}
    assert ids["watertight"] and not ids["self_intersections"] and not rep["valid"]
    # vértice no variedad: dos tetraedros unidos por un punto
    tet = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    tf = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])
    v2 = np.vstack([tet, -tet[1:]])
    f2 = np.vstack([tf, np.array([[0, 4, 5], [0, 6, 4], [4, 6, 5], [0, 5, 6]])])
    ids = {ch["id"]: ch["ok"] for ch in validate_solid(Mesh(v2, f2))["checks"]}
    assert not ids["manifold_vertices"]


def test_self_intersection_detection():
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0.2, 0.2, -1], [0.2, 0.2, 1], [1, 1, 0.5]], float)
    assert self_intersections(Mesh(v, [[0, 1, 2], [3, 4, 5]]))["pairs"] == 1
    assert self_intersections(cube())["pairs"] == 0


def test_plane_clip_produces_straight_boundary():
    sc = S.make_scenario("limpio", ns=120, nt=60)
    m, _ = clean(sc.mesh)
    point = m.vertices.mean(axis=0)
    normal = sc.rotation[:, 0]
    cut = plane_clip(m, point, normal)
    s = (cut.vertices - point) @ normal
    assert s.min() > -1e-6
    assert 0.2 * m.area < cut.area < 0.8 * m.area
    topo = build_topology(cut)
    on_plane = np.abs(s[np.unique(topo.boundary_edges)]) < 1e-6
    assert on_plane.sum() > 20                      # el corte crea un borde nuevo exactamente en el plano
    assert (topo.edge_count > 2).sum() == 0


def test_biharmonic_inpaint_reproduces_smooth_surface():
    x, y = np.meshgrid(np.arange(40.0), np.arange(40.0), indexing="ij")
    z = 0.02 * x * x - 0.01 * x * y + 0.3 * y
    unknown = (x - 20) ** 2 + (y - 18) ** 2 < 36
    filled = inpaint_biharmonic(np.where(unknown, np.nan, z), ~unknown, unknown)
    assert np.abs(filled - z)[unknown].max() < 0.05


def test_analysis_reports_defects_and_units():
    sc = S.make_scenario("completo", ns=NS, nt=NT)
    scale, unit = detect_unit_scale(sc.mesh.extents)
    assert scale == 1000.0 and unit == "metros"
    rep = analyze(Mesh(sc.mesh.vertices * scale, sc.mesh.faces))
    assert rep["components"] > 5 and rep["boundary_edges"] > 0 and rep["nonmanifold_edges"] > 0
    assert rep["duplicate_vertices"] > 0 and rep["self_intersecting_pairs"] > 0
    assert rep["multi_layer_fraction"] > 0.02 and not rep["watertight"]


# ------------------------------------------------------------------ escenarios completos
@pytest.mark.parametrize("name", S.SCENARIOS)
def test_scenario_produces_valid_faithful_solid(name):
    sc, pipe, out = processed(name)
    val = out["validacion"]
    assert val["valid"], val["errors"]
    assert all(c["ok"] for c in val["checks"])
    assert val["volume"] > 0

    top = pipe.mesh("suavizado").vertices
    err = sc.height_error(top)
    sup = pipe._support
    sigma = sc.noise_sigma
    # Fidelidad: la superficie final coincide con la planta analítica (el alisado multiescala
    # cede como mucho unas décimas de forma de pequeña amplitud a cambio de una superficie lisa).
    assert rms(err[sup]) < 0.20 + 0.9 * sigma
    # Sin restos de capas duplicadas, solapas, alas ni fragmentos (darían errores de mm).
    assert np.nanpercentile(np.abs(err[sup]), 99.5) < 1.0 + 4.0 * sigma
    assert np.isfinite(err).all()                    # nada fuera de la huella del pie
    # Las zonas interpoladas (agujeros) siguen la forma real.
    if (~sup).sum() > 50:
        assert rms(err[~sup]) < 0.6 + 2 * sigma
    # Se ha eliminado la geometría no plantar y se conserva la planta.
    assert out["segmentado"]["removed_area_pct"] > 30
    expected = 11000 if name in ("incompleto", "completo") else 12500
    assert out["recortado"]["footprint_area"] > expected
    assert out["recortado"]["boundary_loops"] == 1
    assert val["quality"]["preservation_pct"] > 93 - 30 * sigma     # con ruido fuerte, el propio escaneo se aleja


@pytest.mark.parametrize("name", ["limpio", "ruido", "completo"])
def test_arch_is_preserved(name):
    sc, pipe, out = processed(name)
    sm = out["suavizado"]
    assert sm["arch_depth_before"] > 10                               # el arco existe en el modelo
    assert abs(sm["arch_depth_change"]) < 0.3 + sc.noise_sigma        # y no se aplana
    v = pipe.mesh("suavizado").vertices
    p = sc.to_model_frame(v)
    arch = (p[:, 0] > 70) & (p[:, 0] < 170) & (p[:, 1] > 10) & pipe._support
    err = sc.height_error(v)[arch]
    assert arch.sum() > 200
    assert abs(np.nanmean(err)) < 0.08 + 0.3 * sc.noise_sigma         # sin sesgo: el arco no se rellena


def test_smoothing_denoises_without_damaging_clean_data():
    sc, pipe, out = processed("ruido")
    sup = pipe._support
    before = rms(sc.height_error(pipe.mesh("recortado").vertices)[sup])
    after = rms(sc.height_error(pipe.mesh("suavizado").vertices)[sup])
    noisy = out["suavizado"]
    assert after < 0.8 * before
    assert noisy["noise_after_median"] < 0.15 * noisy["noise_before_median"]      # superficie lisa
    sc, pipe, out = processed("limpio")
    sup = pipe._support
    before = rms(sc.height_error(pipe.mesh("recortado").vertices)[sup])
    after = rms(sc.height_error(pipe.mesh("suavizado").vertices)[sup])
    clean = out["suavizado"]
    assert after < before + 0.08                              # la forma se conserva
    # Adaptación al escaneo: se mide más textura en el ruidoso y se interviene más en él.
    assert noisy["texture_slope"] > 3 * clean["texture_slope"]
    assert noisy["displacement_rms"] > 2 * clean["displacement_rms"]
    assert noisy["intervention_mean"] > clean["intervention_mean"]


def test_local_method_is_conservative_on_clean_data():
    sc, pipe, _ = processed("limpio")
    sup = pipe._support
    before = rms(sc.height_error(pipe.mesh("recortado").vertices)[sup])
    info = pipe.smooth({"method": "local", "intensity": 0.8, "max_deviation": 0.8})
    after = rms(sc.height_error(pipe.mesh("suavizado").vertices)[sup])
    assert info.get("method") != "multiescala"
    assert after < before + 0.01
    assert info["intervention_mean"] < 0.5                    # zona limpia: intervención reducida
    assert info["displacement_max"] <= 0.8 + 1e-9
    pipe.smooth()                                             # se restaura el estado compartido
    pipe.solid()
    pipe.validate()


def test_multiscale_removes_grain_and_isolated_points_keeping_relief():
    """Grano de sensor de profundidad + puntos aislados sobre una planta conocida."""
    from scipy import ndimage
    from core.raster import make_grid
    from core.smoothing import SmoothParams, multiscale_smooth

    sc, pipe, _ = processed("limpio")
    base = pipe.stages["recortado"]
    v = base.vertices
    rng = np.random.default_rng(11)
    g = make_grid(v[:, :2].min(axis=0), v[:, :2].max(axis=0), 0.5)

    def field(sigma_mm, amp):
        f = ndimage.gaussian_filter(rng.normal(size=g.shape), sigma_mm / 0.5)
        return g.sample_bilinear(f * (amp / f.std()), v[:, :2])

    z = v[:, 2] + field(0.7, 0.18) + field(1.4, 0.15) + rng.normal(0, 0.05, len(v))
    spikes = rng.choice(len(v), 40, replace=False)
    z[spikes] += rng.choice([-1.0, 1.0], 40) * 3.0
    res = multiscale_smooth(Mesh(np.column_stack([v[:, :2], z]), base.faces), SmoothParams())
    out, info = res["mesh"].vertices, res["info"]
    world = lambda zz: pipe.frame.to_world(np.column_stack([v[:, :2], zz]))        # noqa: E731
    sup = pipe._support
    e_in = sc.height_error(world(z))
    e_out = sc.height_error(world(out[:, 2]))
    assert info["noise_after_median"] < 0.1 * info["noise_before_median"]   # el grano desaparece
    assert np.nanmax(np.abs(e_out[spikes])) < 0.8                     # los puntos aislados también
    assert rms(e_out[sup]) < min(0.25, 0.8 * rms(e_in[sup]))          # y la forma se mantiene
    assert abs(info["arch_depth_change"]) < 0.3                       # el arco no se aplana
    toes = (sc.to_model_frame(world(out[:, 2]))[:, 0] > 0.88 * S.L_FOOT) & sup
    assert rms(e_out[toes]) < 0.45                                    # el relieve de los dedos sigue ahí
    assert np.array_equal(out[:, :2], v[:, :2])


def test_smoothing_only_moves_along_plantar_axis():
    _, pipe, _ = processed("ruido")
    a, b = pipe.stages["recortado"].vertices, pipe.stages["suavizado"].vertices
    assert np.array_equal(a[:, :2], b[:, :2])


def test_orientation_independent_result():
    areas = []
    for seed in (1, 2, 3):
        sc = S.make_scenario("limpio", seed=seed, ns=160, nt=80)
        pipe = Pipeline()
        pipe.load(stl_bytes(sc.mesh))
        pipe.segment()
        pipe.extract()
        areas.append(pipe.cut()["footprint_area"])
        assert pipe.frame.z_axis @ sc.rotation[:, 2] > 0.98
    assert max(areas) - min(areas) < 0.03 * np.mean(areas)


def test_inverted_normals_are_detected():
    sc = S.make_scenario("limpio", ns=160, nt=80)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh.flipped()))
    assert pipe.frame.z_axis @ sc.rotation[:, 2] > 0.98
    assert pipe.frame.info.get("auto_inverted")


def test_manual_corrections_override_automatic_result():
    sc = S.make_scenario("limpio", ns=160, nt=80)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh))
    pipe.segment()
    auto = pipe.labels.copy()
    p = sc.to_model_frame(pipe.work.centroids)
    manual = np.zeros(pipe.work.n_faces, dtype=np.uint8)
    disk = (np.linalg.norm(p[:, :2] - [60, 0], axis=1) < 8) & (auto > 0)
    wall = (auto == 0) & (p[:, 0] > 100) & (p[:, 0] < 130) & (p[:, 1] < 0)
    manual[disk] = 2
    manual[wall] = 1
    assert disk.sum() > 10 and wall.sum() > 10
    pipe.segment(manual=manual)
    assert (pipe.labels[disk] == 0).all() and (pipe.labels[wall] == 1).all()
    untouched = manual == 0
    far = untouched & (np.abs(p[:, 0] - 60) > 30) & ~((p[:, 0] > 90) & (p[:, 0] < 140))
    assert (pipe.labels[far] == auto[far]).mean() > 0.995       # el resto no cambia


def test_main_region_can_be_chosen_manually():
    sc = S.make_scenario("conectado", ns=160, nt=80)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh))
    p = sc.to_model_frame(pipe.work.centroids)
    n, _ = pipe.work.face_normals()
    brim = np.flatnonzero((p[:, 2] < 1.0) & (np.abs(n @ sc.rotation[:, 2]) > 0.9))
    info = pipe.segment(main_face=int(brim[len(brim) // 2]))
    kept = p[pipe.labels > 0]
    assert np.median(kept[:, 2]) < 2.0 and info["faces_kept"] > 100     # ahora se conserva el ala


@pytest.mark.parametrize("mode,side", [("flat", "interior"), ("flat", "exterior"),
                                        ("offset", "interior"), ("offset", "exterior")])
def test_solid_modes(mode, side):
    _, pipe, _ = processed("limpio")
    surf = pipe.stages["suavizado"]
    res = build_solid(surf, SolidParams(thickness=4.0, mode=mode, side=side))
    rep = validate_solid(res["mesh"])
    assert rep["valid"], rep["errors"]
    assert res["info"]["thickness_min"] >= 4.0 - 1e-9
    assert np.array_equal(res["mesh"].vertices[:surf.n_vertices], surf.vertices)   # planta intacta
    if mode == "offset":
        assert abs(res["info"]["thickness_max"] - 4.0) < 1e-9


def test_plane_cut_in_pipeline_gives_clean_single_contour():
    sc = S.make_scenario("limpio", ns=160, nt=80)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh))
    pipe.segment()
    pipe.extract()
    normal = -sc.rotation[:, 0]                       # se conserva del corte hacia el talón
    point = np.array([200.0, 0, 30]) @ sc.rotation.T + sc.translation
    info = pipe.cut([{"point": point.tolist(), "normal": normal.tolist()}])
    v = pipe.mesh("recortado").vertices
    assert ((v - point) @ normal).min() > -1.5 * info["resolution"]
    assert sc.to_model_frame(v)[:, 0].max() < 200 + 1.5 * info["resolution"]
    assert info["boundary_loops"] == 1 and info["planes"] == 1
    # contorno sin dientes: los giros entre segmentos consecutivos son pequeños
    m = pipe.stages["recortado"]
    q = m.vertices[boundary_loops(m)[0], :2]
    d1, d2 = q - np.roll(q, 1, axis=0), np.roll(q, -1, axis=0) - q
    turn = np.degrees(np.abs(np.arctan2(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0], (d1 * d2).sum(axis=1))))
    assert np.percentile(turn, 95) < 15 and turn.mean() < 5
    pipe.solid()
    assert pipe.validate()["valid"]


def test_export_refuses_invalid_solid_and_keeps_original():
    sc = S.make_scenario("limpio", ns=120, nt=60)
    data = stl_bytes(sc.mesh)
    pipe = Pipeline()
    pipe.load(data)
    pipe.auto()
    good = load_stl_bytes(pipe.export("solido", "stl"))
    assert validate_solid(clean(good)[0])["valid"]             # el STL exportado es un sólido cerrado
    canon = clean(load_stl_bytes(pipe.export("solido", "stl", "canonical")))[0]
    assert abs(canon.vertices[:, 2].min()) < 1e-4
    solid = pipe.stages["solido"]
    pipe.stages["solido"] = Mesh(solid.vertices, solid.faces[:-10])     # se rompe a propósito
    pipe.reports.pop("validacion")
    with pytest.raises(PipelineError):
        pipe.export("solido", "stl")
    assert stl_bytes(sc.mesh) == data                                    # el original no se altera


def test_stage_invalidation_and_revert():
    _, pipe0, _ = processed("limpio")
    sc = S.make_scenario("limpio", ns=120, nt=60)
    pipe = Pipeline()
    pipe.load(stl_bytes(sc.mesh))
    pipe.auto()
    assert pipe.has("solido")
    pipe.smooth({"intensity": 0.3})
    assert not pipe.has("solido") and "validacion" not in pipe.reports
    pipe.revert("segmentado")
    assert pipe.has("segmentado") and not pipe.has("recortado")
    with pytest.raises(PipelineError):
        pipe.solid()


def test_web_api_end_to_end():
    pytest.importorskip("starlette")
    uvicorn = pytest.importorskip("uvicorn")
    import json
    import socket
    import threading
    import time
    import urllib.request

    from app.server import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, data=None, method=None):
        req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"))
        with opener.open(req, timeout=120) as r:
            return r.read()

    for _ in range(100):
        try:
            call("/api/state")
            break
        except OSError:
            time.sleep(0.1)
    try:
        assert b"<canvas" in call("/")
        sc = S.make_scenario("fragmentos", ns=120, nt=60)
        res = json.loads(call("/api/upload?name=prueba.stl", stl_bytes(sc.mesh)))
        assert res["state"]["stages"] == ["original"]
        res = json.loads(call("/api/auto", b"{}"))
        assert res["report"]["validacion"]["valid"]
        assert res["state"]["stages"][-1] == "solido"
        head = np.frombuffer(call("/api/mesh/solido")[:8], dtype="<u4")
        assert head[1] == res["report"]["solido"]["faces"]
        labels = np.frombuffer(call("/api/labels"), dtype=np.uint8)
        assert len(labels) == res["report"]["segmentacion"]["faces_total"]
        stl = load_stl_bytes(call("/api/export?stage=solido&format=stl"))
        assert validate_solid(clean(stl)[0])["valid"]
        assert call("/api/original") == stl_bytes(sc.mesh)          # copia original intacta
    finally:
        server.should_exit = True
