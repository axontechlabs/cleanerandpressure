"""API del modo bilateral (dos pies). Independiente de la sesión de un pie de ``server.py``."""
from __future__ import annotations

import threading
import traceback

import numpy as np
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from core import synthetic
from core import ai_client
from core.ai_client import AIError
from core.bilateral import SIDES, BilateralError
from core.bilateral_session import BilateralSession
from core.mesh_io import stl_bytes
from core.pipeline import PipelineError


def make_routes(static_dir, workspace, json_response, body_json):
    session = BilateralSession(workspace)
    lock = threading.Lock()

    async def run(fn, *a, **k):
        def locked():
            with lock:
                return fn(*a, **k)
        try:
            return await run_in_threadpool(locked), None
        except (BilateralError, PipelineError, AIError) as e:
            return None, json_response({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return None, json_response({"error": f"Error interno: {type(e).__name__}: {e}"}, 500)

    def with_state(rep):
        return {"report": rep, "state": session.state()}

    async def page(request: Request):
        return FileResponse(static_dir / "bilateral.html", headers={"Cache-Control": "no-store"})

    async def upload(request: Request):
        side = request.path_params["side"]
        data = await request.body()
        name = request.query_params.get("name", f"pie_{side}.stl")
        res, err = await run(session.load, side, data, name)
        return err or json_response(with_state(res))

    async def sample(request: Request):
        def do():
            left, right = synthetic.make_foot_pair()
            session.load("izquierdo", stl_bytes(left), "sintetico_izquierdo.stl")
            session.load("derecho", stl_bytes(right), "sintetico_derecho_arco_alto.stl")
            return {}
        res, err = await run(do)
        return err or json_response(with_state(res))

    async def swap(request: Request):
        res, err = await run(session.swap)
        return err or json_response(with_state({}))

    async def process(request: Request):
        body = await body_json(request)
        res, err = await run(session.process, body.get("params"))
        return err or json_response(with_state(res))

    async def place(request: Request):
        body = await body_json(request)
        res, err = await run(session.place, body.get("stance"))
        return err or json_response(with_state(res))

    async def simulate(request: Request):
        body = await body_json(request)
        res, err = await run(session.simulate, body.get("sim"), body.get("stance"))
        return err or json_response(with_state(res))

    async def state(request: Request):
        return json_response({"state": session.state(), "report": session.report(),
                              "placement": session.placement_report()})

    async def mesh(request: Request):
        side = request.path_params["side"]
        if side not in SIDES:
            return json_response({"error": "Lado desconocido."}, 404)

        def do():
            m, n_sole = session.positioned(side)
            head = np.array([m.n_vertices, m.n_faces, n_sole], dtype="<u4").tobytes()
            return head + m.vertices.astype("<f4").tobytes() + m.faces.astype("<u4").tobytes()
        res, err = await run(do)
        return err or Response(res, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    async def face_pressure(request: Request):
        side = request.path_params["side"]
        res, err = await run(lambda: session.face_pressures(side).astype("<f4").tobytes())
        return err or Response(res, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    async def png(request: Request):
        which = request.path_params["which"]
        if which not in ("bilateral",) + SIDES:
            return json_response({"error": "Mapa desconocido."}, 404)
        res, err = await run(session.map_png, which)
        if err:
            return err
        headers = {"Cache-Control": "no-store"}
        if request.query_params.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="mapa_presion_{which}.png"'
        return Response(res, media_type="image/png", headers=headers)

    async def export(request: Request):
        what = request.path_params["what"]
        fmt = request.query_params.get("fmt", "stl")
        if what in SIDES:
            if fmt not in ("stl", "obj", "ply"):
                return json_response({"error": "Formato no admitido."}, 400)
            res, err = await run(session.export_stl, what, fmt)
            fname, mt = f"pie_{what}_limpio.{fmt}", "application/octet-stream"
        elif what == "csv":
            res, err = await run(session.export_csv)
            fname, mt = "presiones_bilateral.csv", "text/csv"
        elif what == "json":
            res, err = await run(session.export_json)
            fname, mt = "resultados_bilateral.json", "application/json"
        else:
            return json_response({"error": "Exportación desconocida."}, 404)
        return err or Response(res, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    async def ai_cut(request: Request):
        """Revisión por IA del corte de cada pie (y nueva validación)."""
        body = await body_json(request)

        def do():
            from core.ai_tuner import tune_cut
            client = ai_client.ClaudeClient(model=body.get("model") or None)
            out = {}
            for s in SIDES:
                p = session.pipes[s]
                if p.work is None:
                    raise BilateralError(f"Falta el escaneo del pie {s}.")
                if "recortado" not in p.stages:
                    p.analyze()
                    p.autotune_cut()
                out[s] = tune_cut(p, client, rounds=int(body.get("rounds", 3)))
                v = out[s]["validacion"]
                session.process_reports[s] = {**session.process_reports.get(s, {}), "file": p.name,
                                              "valid": bool(v["valid"]),
                                              "failed_checks": [c["name"] for c in v["checks"] if not c["ok"]]}
            session.reset_results()
            return out
        res, err = await run(do)
        return err or json_response(with_state(res))

    async def ai_pressure(request: Request):
        """Revisión por IA del mapa de presión: reajusta el modelo y vuelve a simular."""
        body = await body_json(request)

        def do():
            from core.ai_tuner import tune_pressure
            client = ai_client.ClaudeClient(model=body.get("model") or None)
            out = tune_pressure(session, client, rounds=int(body.get("rounds", 3)), sim=body.get("sim"),
                                stance=body.get("stance"))
            return {"ia": out, **session.report()}
        res, err = await run(do)
        return err or json_response(with_state(res))

    return [
        Route("/bilateral", page),
        Route("/api/bi/ai/cut", ai_cut, methods=["POST"]),
        Route("/api/bi/ai/pressure", ai_pressure, methods=["POST"]),
        Route("/api/bi/upload/{side}", upload, methods=["POST"]),
        Route("/api/bi/sample", sample, methods=["POST"]),
        Route("/api/bi/swap", swap, methods=["POST"]),
        Route("/api/bi/process", process, methods=["POST"]),
        Route("/api/bi/place", place, methods=["POST"]),
        Route("/api/bi/simulate", simulate, methods=["POST"]),
        Route("/api/bi/state", state),
        Route("/api/bi/mesh/{side}", mesh),
        Route("/api/bi/facepressure/{side}", face_pressure),
        Route("/api/bi/map/{which}.png", png),
        Route("/api/bi/export/{what}", export),
    ]
