"""Sesión bilateral: dos escaneos, dos limpiezas independientes y una simulación conjunta.

Cada pie se limpia con el mismo ``Pipeline`` que la aplicación de un pie (mismos criterios de
segmentación, suavizado, reparación y sólido). Los STL limpios se exportan en el marco de su
archivo, sin las transformaciones de colocación, que solo se aplican a la copia simulada.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from .bilateral import (SIDES, BilateralError, Placement, SimParams, StanceParams, face_pressure,
                        place_feet, prepare_foot, pressure_csv, result_json, simulate)
from .mesh import Mesh
from .pipeline import Pipeline, PipelineError


class BilateralSession:
    def __init__(self, workspace: str | Path | None = None):
        ws = Path(workspace) if workspace else None
        self.pipes = {s: Pipeline(ws / f"bilateral_{s}" if ws else None) for s in SIDES}
        self.reset_results()

    def reset_results(self, keep_processing: bool = True):
        self.feet = {}
        self.placements = None
        self.place_info = None
        self.stance = StanceParams()
        self.result = None
        self.process_reports = getattr(self, "process_reports", {}) if keep_processing else {}

    # ------------------------------------------------------------------ carga y limpieza
    def _slot(self, side: str) -> str:
        if side not in SIDES:
            raise BilateralError("Lado desconocido: use «izquierdo» o «derecho».")
        return side

    def load(self, side: str, data: bytes, name: str) -> dict:
        side = self._slot(side)
        rep = self.pipes[side].load(data, name)
        self.process_reports.pop(side, None)
        self.reset_results()
        return rep

    def swap(self) -> dict:
        self.pipes["izquierdo"], self.pipes["derecho"] = self.pipes["derecho"], self.pipes["izquierdo"]
        a, b = self.process_reports.get("izquierdo"), self.process_reports.get("derecho")
        self.process_reports = {k: v for k, v in (("izquierdo", b), ("derecho", a)) if v is not None}
        self.reset_results()
        return self.state()

    def process(self, params: dict | None = None, ai_client=None, ai_rounds: int = 6) -> dict:
        """Limpia ambos escaneos por separado con el pipeline existente y valida los sólidos.

        Con ``ai_client``, el corte de cada pie lo revisa y reajusta la IA dentro del proceso.
        """
        for s in SIDES:
            if self.pipes[s].work is None:
                raise BilateralError(f"Falta el escaneo del pie {s}.")
        out = {}
        for s in SIDES:
            t0 = time.time()
            if ai_client is not None:
                from .ai_tuner import auto_with_ai
                r = auto_with_ai(self.pipes[s], ai_client, params, rounds=ai_rounds)
            else:
                r = self.pipes[s].auto(params)
            v = r["validacion"]
            out[s] = {
                "file": self.pipes[s].name, "valid": bool(v["valid"]), "seconds": round(time.time() - t0, 1),
                "faces": v.get("faces"), "volume_cm3": (v.get("volume") or 0) / 1000.0,
                "failed_checks": [c["name"] for c in v["checks"] if not c["ok"]],
                "warnings": sum((r.get(k, {}).get("warnings", []) for k in ("segmentacion", "recortado", "suavizado")), []),
                "deviation_mean_mm": v.get("quality", {}).get("deviation", {}).get("mean"),
                "cobertura": (r.get("recortado") or {}).get("cobertura"),
                "ia": ({"history": r["ia"]["history"], "model": r["ia"]["model"], "usage": r["ia"]["usage"]}
                       if r.get("ia") else None),
            }
        self.process_reports = out
        self.reset_results()
        return out

    # ------------------------------------------------------------------ colocación
    def _ready(self):
        for s in SIDES:
            p = self.pipes[s]
            if "solido" not in p.stages or "suavizado" not in p.stages:
                raise BilateralError(f"Procese primero los dos escaneos (falta el pie {s}).")
            if not self.process_reports.get(s, {}).get("valid"):
                raise BilateralError(f"El sólido del pie {s} no ha superado la validación geométrica: "
                                     "revíselo en la aplicación de un pie antes de simular.")

    def place(self, stance: dict | None = None) -> dict:
        self._ready()
        self.stance = StanceParams.from_dict(stance)
        self.feet = {s: prepare_foot(self.pipes[s].stages["suavizado"], self.pipes[s].stages["solido"], s,
                                     self.stance.load_point) for s in SIDES}
        self.placements, self.place_info = place_feet(self.feet, self.stance)
        self.result = None
        return self.placement_report()

    def placement_report(self) -> dict:
        if not self.feet:
            return {}
        return {"stance": self.place_info, "feet": {s: self.feet[s].info for s in SIDES}}

    def positioned(self, side: str) -> tuple[Mesh, int]:
        if not self.placements:
            raise BilateralError("Coloque primero los pies.")
        f = self.feet[side]
        return self.placements[side].apply(f.solid_local), f.n_sole_faces

    # ------------------------------------------------------------------ simulación
    def simulate(self, sim: dict | None = None, stance: dict | None = None) -> dict:
        if stance is not None or not self.placements:
            self.place(stance)
        sp = SimParams.from_dict(sim)
        self.result = simulate(self.feet, self.placements, sp)
        self._loaded = {}
        try:
            self._add_loaded_feet(sp)
        except Exception as e:  # noqa: BLE001 - la expansión nunca impide la simulación
            self.result.setdefault("warnings", []).append(f"No se ha podido calcular el pie en carga: {e}")
        return self.report()

    def _add_loaded_feet(self, sp) -> None:
        """Expansión del pie al pisar y sólido del pie en carga (expandido y aplastado)."""
        from .loaded_foot import expansion, loaded_mesh, public
        for s in SIDES:
            exp = expansion(self.result, s, self.feet[s], sp)
            solid, n = self.positioned(s)
            self._loaded[s] = loaded_mesh(self.result, s, self.feet[s], self.placements[s], solid, n, exp)
            self.result["feet"][s]["loaded_foot"] = public(exp)

    def export_loaded(self, side: str, fmt: str = "stl") -> bytes:
        """STL del pie en carga (un pie, o «ambos» en un solo archivo, en el marco de la simulación)."""
        from .mesh import merge_meshes
        from .mesh_io import export_bytes
        if self.result is None or not getattr(self, "_loaded", None):
            raise BilateralError("Ejecute primero la simulación.")
        if side in ("ambos", "both"):
            return export_bytes(merge_meshes([self._loaded[s] for s in SIDES]), fmt)
        if side not in self._loaded:
            raise BilateralError(f"Pie desconocido: {side}")
        return export_bytes(self._loaded[side], fmt)

    def report(self) -> dict:
        if self.result is None:
            return {}
        r = {k: v for k, v in self.result.items() if not k.startswith("_")}
        r["placement"] = self.placement_report()
        return r

    def face_pressures(self, side: str) -> np.ndarray:
        if self.result is None:
            raise BilateralError("Ejecute primero la simulación.")
        m, n = self.positioned(side)
        sole = Mesh(m.vertices, m.faces[:n])
        return face_pressure(self.result, sole, side)

    # ------------------------------------------------------------------ exportación
    def export_stl(self, side: str, fmt: str = "stl") -> bytes:
        p = self.pipes[self._slot(side)]
        if "solido" not in p.stages:
            raise BilateralError(f"No hay STL limpio del pie {side}: procese los escaneos.")
        if not self.process_reports.get(side, {}).get("valid"):
            raise BilateralError(f"El sólido del pie {side} no es válido: no se exporta.")
        return p.export("solido", fmt)

    def export_csv(self) -> bytes:
        if self.result is None:
            raise BilateralError("Ejecute primero la simulación.")
        from .report_en import csv_to_english
        return csv_to_english(pressure_csv(self.result))

    def export_json(self) -> bytes:
        if self.result is None:
            raise BilateralError("Ejecute primero la simulación.")
        extra = {"placement": self.placement_report(),
                 "processing": self.process_reports,
                 "files": {s: self.pipes[s].name for s in SIDES}}
        from .report_en import json_to_english
        return json_to_english(result_json(self.result, extra))

    def map_png(self, which: str = "bilateral") -> bytes:
        if self.result is None:
            raise BilateralError("Ejecute primero la simulación.")
        from .pressure_maps import render_map
        return render_map(self.result, which, lang="en")

    def state(self) -> dict:
        return {
            "files": {s: self.pipes[s].name for s in SIDES},
            "processed": {s: self.process_reports.get(s) for s in SIDES},
            "placed": bool(self.placements),
            "simulated": self.result is not None,
        }


__all__ = ["BilateralSession", "BilateralError", "PipelineError", "Placement"]
