"""Servidor web local (Starlette + Uvicorn): API de procesamiento y archivos estáticos del visor."""
from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import threading
import time
import traceback
from pathlib import Path

import numpy as np
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from core import synthetic
from core.mesh import build_topology
from core.mesh_io import stl_bytes
from core import ai_client
from core.ai_client import AIError
from core.pipeline import STAGES, Pipeline, PipelineError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
WORKSPACE = ROOT.parent / "workspace"

pipe = Pipeline(WORKSPACE)
lock = threading.Lock()


def _clean(o):
    """Convierte tipos NumPy y valores no finitos a JSON válido."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if math.isfinite(f) else None
    return o


def _json(data, status: int = 200) -> Response:
    return Response(json.dumps(_clean(data), ensure_ascii=False), status_code=status,
                    media_type="application/json")


async def _body_json(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise PipelineError(f"JSON no válido: {e}") from e


async def _run(fn, *args, **kwargs):
    """Ejecuta una operación del pipeline en un hilo, en exclusión mutua, y traduce errores."""
    def locked():
        with lock:
            return fn(*args, **kwargs)
    try:
        return await run_in_threadpool(locked), None
    except (PipelineError, AIError) as e:
        return None, _json({"error": str(e)}, 400)
    except Exception as e:  # noqa: BLE001 - se informa al usuario en la interfaz
        traceback.print_exc()
        return None, _json({"error": f"Error interno: {type(e).__name__}: {e}"}, 500)


def _with_state(report):
    return {"report": report, "state": pipe.state()}


# ------------------------------------------------------------------------- rutas
BUILD = str(int(time.time()))


def html_page(path: Path) -> Response:
    """Página con los /static/… versionados: el navegador nunca mezcla HTML nuevo con JavaScript viejo."""
    html = path.read_text(encoding="utf-8")
    for ext in (".js", ".css"):
        html = re.sub(r'(/static/[\w./-]+\%s)(["\'])' % ext, r"\1?v=" + BUILD + r"\2", html)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})


async def index(request: Request):
    return html_page(STATIC / "index.html")


async def upload(request: Request):
    data = await request.body()
    name = request.query_params.get("name", "modelo.stl")
    scale = request.query_params.get("unit_scale")
    res, err = await _run(pipe.load, data, name, float(scale) if scale else None)
    return err or _json(_with_state(res))


async def sample(request: Request):
    name = request.path_params["name"]
    if name not in synthetic.SCENARIOS:
        return _json({"error": "Ejemplo desconocido."}, 404)

    def make():
        sc = synthetic.make_scenario(name)
        return pipe.load(stl_bytes(sc.mesh), f"sintetico_{name}.stl")
    res, err = await _run(make)
    return err or _json(_with_state(res))


async def state(request: Request):
    return _json(pipe.state())


async def analyze(request: Request):
    res, err = await _run(pipe.analyze)
    return err or _json(_with_state(res))


async def orientation(request: Request):
    try:
        body = await _body_json(request)
    except PipelineError as e:
        return _json({"error": str(e)}, 400)
    res, err = await _run(pipe.set_orientation, body.get("flip"), body.get("axis"), bool(body.get("reset")))
    return err or _json(_with_state(res))


async def segment(request: Request):
    try:
        body = await _body_json(request)
    except PipelineError as e:
        return _json({"error": str(e)}, 400)
    manual = None
    if body.get("manual_b64"):
        manual = np.frombuffer(base64.b64decode(body["manual_b64"]), dtype=np.uint8)
    main_face = body.get("main_face")
    res, err = await _run(pipe.segment, body.get("params"), manual,
                          int(main_face) if main_face is not None else None)
    return err or _json(_with_state(res))


async def labels_get(request: Request):
    if pipe.labels is None:
        return _json({"error": "Todavía no hay segmentación."}, 400)
    return Response(pipe.labels.tobytes(), media_type="application/octet-stream")


async def extract(request: Request):
    data = await request.body()

    def do():
        if data:
            pipe.set_labels(np.frombuffer(data, dtype=np.uint8))
        return pipe.extract()
    res, err = await _run(do)
    return err or _json(_with_state(res))


def _stage_op(method_name: str, with_planes: bool = False):
    async def handler(request: Request):
        try:
            body = await _body_json(request)
        except PipelineError as e:
            return _json({"error": str(e)}, 400)
        fn = getattr(pipe, method_name)
        if with_planes:
            res, err = await _run(fn, body.get("planes"), body.get("params"))
        else:
            res, err = await _run(fn, body.get("params"))
        return err or _json(_with_state(res))
    return handler


async def revert(request: Request):
    try:
        body = await _body_json(request)
    except PipelineError as e:
        return _json({"error": str(e)}, 400)
    res, err = await _run(pipe.revert, str(body.get("stage", "")))
    return err or _json(_with_state(res))


async def validate(request: Request):
    res, err = await _run(pipe.validate)
    return err or _json(_with_state(res))


async def auto(request: Request):
    try:
        body = await _body_json(request)
    except PipelineError as e:
        return _json({"error": str(e)}, 400)
    manual = None
    if body.get("manual_b64"):
        manual = np.frombuffer(base64.b64decode(body["manual_b64"]), dtype=np.uint8)

    def do():
        if manual is not None and pipe.work is not None and len(manual) == pipe.work.n_faces:
            pipe.manual = manual.copy()
        if body.get("sin_ia") or (body.get("segment") or {}).get("mode") == "planta":
            return pipe.auto(body)
        # La IA forma parte del proceso: revisa el corte y reajusta hasta darlo por bueno.
        from core.ai_tuner import auto_with_ai, require_client
        client = require_client(body.get("model") or None)
        return auto_with_ai(pipe, client, body, rounds=int(body.get("rounds", 6)))
    res, err = await _run(do)
    return err or _json(_with_state(res))


def _mesh_payload(stage: str) -> bytes:
    m = pipe.mesh(stage)
    head = np.array([m.n_vertices, m.n_faces], dtype="<u4").tobytes()
    return head + m.vertices.astype("<f4").tobytes() + m.faces.astype("<u4").tobytes()


async def mesh_get(request: Request):
    stage = request.path_params["stage"]
    if stage not in STAGES:
        return _json({"error": "Etapa desconocida."}, 404)
    res, err = await _run(_mesh_payload, stage)
    return err or Response(res, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


async def adjacency_get(request: Request):
    def do():
        if pipe.work is None:
            raise PipelineError("Cargue primero un STL.")
        return build_topology(pipe.work).adj.astype("<i4").tobytes()
    res, err = await _run(do)
    return err or Response(res, media_type="application/octet-stream")


async def edges_get(request: Request):
    stage = request.path_params["stage"]

    def do():
        m = pipe.mesh(stage)
        topo = build_topology(m)
        e = topo.edges[topo.edge_count != 2]
        return m.vertices[e].astype("<f4").tobytes()
    res, err = await _run(do)
    return err or Response(res, media_type="application/octet-stream")


async def field_get(request: Request):
    name = request.path_params["name"]
    if name not in pipe.fields:
        return _json({"error": "Campo no disponible: ejecute el suavizado."}, 400)
    return Response(pipe.fields[name].astype("<f4").tobytes(), media_type="application/octet-stream")


async def export(request: Request):
    q = request.query_params
    stage, fmt, frame = q.get("stage", "solido"), q.get("format", "stl").lower(), q.get("frame", "original")
    if stage not in STAGES or fmt not in ("stl", "obj", "ply") or frame not in ("original", "canonical"):
        return _json({"error": "Parámetros de exportación no válidos."}, 400)
    res, err = await _run(pipe.export, stage, fmt, frame)
    if err:
        return err
    base = Path(pipe.name or "model").stem
    stage_en = {"original": "original", "segmentado": "segmented", "recortado": "trimmed", "suavizado": "smoothed",
                "solido": "solid"}.get(stage, stage)
    fname = f"{base}_{stage_en}.{fmt}"           # resultados en inglés
    return Response(res, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def original(request: Request):
    res, err = await _run(pipe.original_bytes)
    if err:
        return err
    return Response(res, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{pipe.name}"'})


# ------------------------------------------------------------------------- IA
async def ai_status(request: Request):
    return _json(ai_client.status())


async def ai_key(request: Request):
    """Fija la clave/modelo (por defecto se guarda en .env) y comprueba la conexión con una llamada mínima."""
    try:
        body = await _body_json(request)
        ai_client.set_api_key(body.get("key"), body.get("model"), False)
    except (PipelineError, AIError) as e:
        return _json({"error": str(e)}, 400)
    st = await asyncio.to_thread(ai_client.test_connection)
    if st["connected"]:
        if body.get("persist", True):       # solo se guarda una clave que funciona
            ai_client.set_api_key(body.get("key"), body.get("model"), True)
            st = ai_client.status()
        return _json(st)
    return _json({**st, "error": st.get("error") or "No se ha podido conectar."}, 400)


async def ai_test(request: Request):
    """Comprueba la conexión con la clave y el modelo actuales (al abrir el programa)."""
    return _json(await asyncio.to_thread(ai_client.test_connection))


async def ai_cut(request: Request):
    """Revisión del corte por IA: Claude mira el corte, reajusta parámetros y se vuelve a cortar."""
    try:
        body = await _body_json(request)
    except PipelineError as e:
        return _json({"error": str(e)}, 400)

    def do():
        from core.ai_tuner import tune_cut
        if pipe.work is None:
            raise PipelineError("Cargue primero un STL.")
        if "analisis" not in pipe.reports:
            pipe.analyze()
        if "recortado" not in pipe.stages:
            pipe.autotune_cut(body.get("segment"), body.get("cut"), body.get("planes"))
        client = ai_client.ClaudeClient(model=body.get("model") or None)
        return tune_cut(pipe, client, rounds=int(body.get("rounds", 3)), planes=body.get("planes"))
    res, err = await _run(do)
    return err or _json(_with_state(res))


async def cut_views(request: Request):
    def do():
        from core.ai_tuner import cut_views_png
        if "recortado" not in pipe.stages:
            raise PipelineError("Ejecute primero el recorte en modo plantilla.")
        return cut_views_png(pipe, title=pipe.name or "")
    res, err = await _run(do)
    return err or Response(res, media_type="image/png", headers={"Cache-Control": "no-store"})


routes = [
    Route("/", index),
    Route("/api/ai/status", ai_status),
    Route("/api/ai/key", ai_key, methods=["POST"]),
    Route("/api/ai/test", ai_test, methods=["POST"]),
    Route("/api/ai/cut", ai_cut, methods=["POST"]),
    Route("/api/cut_views.png", cut_views),
    Route("/api/upload", upload, methods=["POST"]),
    Route("/api/sample/{name}", sample, methods=["POST"]),
    Route("/api/state", state),
    Route("/api/analyze", analyze, methods=["POST"]),
    Route("/api/orientation", orientation, methods=["POST"]),
    Route("/api/segment", segment, methods=["POST"]),
    Route("/api/labels", labels_get),
    Route("/api/extract", extract, methods=["POST"]),
    Route("/api/cut", _stage_op("cut", with_planes=True), methods=["POST"]),
    Route("/api/smooth", _stage_op("smooth"), methods=["POST"]),
    Route("/api/solid", _stage_op("solid"), methods=["POST"]),
    Route("/api/validate", validate, methods=["POST"]),
    Route("/api/revert", revert, methods=["POST"]),
    Route("/api/auto", auto, methods=["POST"]),
    Route("/api/mesh/{stage}", mesh_get),
    Route("/api/adjacency", adjacency_get),
    Route("/api/edges/{stage}", edges_get),
    Route("/api/field/{name}", field_get),
    Route("/api/export", export),
    Route("/api/original", original),
]

# Modo bilateral (dos pies): rutas propias, sesión independiente de la de un pie.
from app.bilateral_api import make_routes  # noqa: E402

routes += make_routes(STATIC, WORKSPACE, _json, _body_json)
routes.append(Mount("/static", StaticFiles(directory=STATIC), name="static"))

class _NoCacheStatic:
    """Tras actualizar el código, el navegador no debe mezclar HTML nuevo con JavaScript viejo."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/static/"):
            return await self.app(scope, receive, send)

        async def send2(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)
        return await self.app(scope, receive, send2)


app = Starlette(routes=routes, middleware=[Middleware(_NoCacheStatic)])
