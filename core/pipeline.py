"""Orquestación del procesamiento por etapas (usada por la interfaz web, la CLI y las pruebas).

Etapas y dependencias::

    original -> segmentado -> recortado -> suavizado -> solido

Volver a ejecutar una etapa invalida las posteriores; las anteriores se conservan,
de modo que se puede corregir solo lo necesario y comparar resultados.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import numpy as np

from .mesh import Mesh, build_topology, face_graph, merge_meshes
from .mesh_analysis import analyze, detect_unit_scale
from .mesh_cleaning import clean
from .mesh_cutting import plane_clip
from .mesh_io import export_bytes, load_stl_bytes
from .mesh_reconstruction import SolidParams, build_solid
from .mesh_validation import validate_solid
from .metrics import quality_report
from .orientation import Frame, estimate_frame
from .segmentation import SegmentationParams, extract, segment_plantar
from .smoothing import SmoothParams, smooth_surface
from .surface_remesh import ContourParams, remesh_heightfield
from .insole import coverage_report

# Reajuste automático del corte de plantilla: límites de cada parámetro.
TUNE_LIMITS = {
    "max_angle": (40.0, 78.0), "gap_tol": (2.0, 20.0), "join_dz": (2.0, 16.0), "bridge_gap": (0.0, 12.0),
    "min_island_area": (5.0, 200.0), "crease_angle": (45.0, 115.0), "layer_tol": (0.5, 3.0),
    "seed_angle": (15.0, 50.0), "normal_radius": (1.0, 8.0),
    "heel_cup": (0.0, 35.0), "medial_flange": (0.0, 45.0), "lateral_flange": (0.0, 30.0),
    "forefoot_wall": (0.0, 20.0), "toe_wall": (0.0, 15.0), "wall_min": (0.0, 8.0),
    "wall_max_angle": (60.0, 87.0), "wall_max_reach": (5.0, 50.0),
}


def clamp_segment_params(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if k == "mode":
            if v in ("plantilla", "planta"):
                out[k] = v
            continue
        if k in TUNE_LIMITS:
            try:
                out[k] = float(np.clip(float(v), *TUNE_LIMITS[k]))
            except (TypeError, ValueError):
                continue
    return out


def adjust_for_coverage(seg: dict, cov: dict) -> dict:
    """Regla determinista: qué tocar según lo que falta en el corte (sin IA)."""
    p = SegmentationParams.from_dict(seg)
    new = dict(seg)
    z = cov.get("zones", {})
    if z.get("dedos", {}).get("coverage", 1) < 0.9 or cov.get("toe_missing_mm", 0) > 6:
        new.update(gap_tol=p.gap_tol * 1.7, join_dz=p.join_dz + 4, max_angle=p.max_angle + 5,
                   min_island_area=p.min_island_area * 0.6, bridge_gap=p.bridge_gap + 3)
    if any(z.get(k, {}).get("coverage", 1) < 0.9 for k in ("talon", "mediopie", "antepie")) \
            or cov.get("heel_missing_mm", 0) > 6:
        new.update(max_angle=max(new.get("max_angle", p.max_angle), p.max_angle + 6),
                   crease_angle=p.crease_angle + 8, layer_tol=p.layer_tol * 1.3)
    if any(w.get("ratio", 1) < 0.7 for w in cov.get("walls", {}).values()):
        new.update(wall_max_angle=p.wall_max_angle + 4, wall_max_reach=p.wall_max_reach + 10)
    if cov.get("extra_fraction", 0) > 0.03:
        new.update(crease_angle=min(new.get("crease_angle", p.crease_angle), p.crease_angle - 12))
    return {**seg, **clamp_segment_params(new)}

STAGES = ("original", "segmentado", "recortado", "suavizado", "solido")


class PipelineError(RuntimeError):
    """Error de uso o de datos que debe mostrarse al usuario."""


class Pipeline:
    def __init__(self, workspace: str | Path | None = None):
        self.workspace = Path(workspace) if workspace else None
        self.reset()

    # ------------------------------------------------------------------ estado
    def reset(self):
        self.name = None
        self.sha256 = None
        self.original_path = None
        self.unit_scale = 1.0
        self.work: Mesh | None = None            # malla limpia en mm, marco del archivo
        self.work_c: Mesh | None = None          # malla limpia en el marco canónico
        self.frame: Frame | None = None
        self.labels: np.ndarray | None = None
        self.reason: np.ndarray | None = None
        self.tuning: list[dict] = []
        self.manual: np.ndarray | None = None
        self.stages: dict[str, Mesh] = {}        # mallas en el marco canónico
        self.reports: dict[str, dict] = {}
        self.fields: dict[str, np.ndarray] = {}
        self._support = None
        self._raw_scaled = None
        self._cut_reference = None
        self._rim = None
        self._flip = False
        self._axis = None

    def _need(self, cond, msg):
        if not cond:
            raise PipelineError(msg)

    def _invalidate_after(self, stage: str):
        i = STAGES.index(stage)
        for s in STAGES[i + 1:]:
            self.stages.pop(s, None)
            self.reports.pop(s, None)
        for k in ("validacion", "calidad"):
            self.reports.pop(k, None)
        if i < STAGES.index("suavizado"):
            self.fields.clear()

    # ------------------------------------------------------------------ etapa 1
    def load(self, data: bytes, name: str = "modelo.stl", unit_scale: float | None = None) -> dict:
        self.reset()
        raw = load_stl_bytes(data)
        self.name = Path(name).name
        self.sha256 = hashlib.sha256(data).hexdigest()
        if self.workspace:
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.original_path = self.workspace / f"original_{self.sha256[:12]}_{self.name}"
            self.original_path.write_bytes(data)      # copia intacta del archivo original
        guess, unit = detect_unit_scale(raw.extents)
        self.unit_scale = float(unit_scale) if unit_scale else guess
        t0 = time.time()
        scaled = Mesh(raw.vertices * self.unit_scale, raw.faces)
        self.work, crep = clean(scaled)
        self._need(self.work.n_faces > 0, "El STL no contiene triángulos válidos.")
        self._raw_scaled = scaled
        rep = {
            "file": self.name, "sha256": self.sha256, "bytes": len(data),
            "original_copy": str(self.original_path) if self.original_path else None,
            "unit_guess": unit, "unit_scale": self.unit_scale,
            "triangles_file": raw.n_faces, "vertices": self.work.n_vertices, "faces": self.work.n_faces,
            "dimensions_file": raw.extents.tolist(), "dimensions_mm": self.work.extents.tolist(),
            "cleaning": crep, "seconds": round(time.time() - t0, 3), "warnings": [],
        }
        if self.unit_scale != 1.0:
            rep["warnings"].append(f"Unidades interpretadas como {unit}: se ha escalado ×{self.unit_scale:g} "
                                   "para trabajar en milímetros (la exportación sale en mm).")
        self.reports["carga"] = rep
        self._update_frame()
        return rep

    def _update_frame(self):
        self.frame = estimate_frame(self.work, flip=self._flip, axis_override=self._axis)
        self.work_c = self.work.transformed(self.frame.rotation, self.frame.origin)

    def set_orientation(self, flip: bool | None = None, axis=None, reset: bool = False):
        """Corrección manual de la cara plantar: invertir o fijar el eje (marco del archivo)."""
        self._need(self.work is not None, "Cargue primero un STL.")
        if reset:
            self._flip, self._axis = False, None
        if axis is not None:
            self._axis = np.asarray(axis, float)
            self._flip = False
        if flip is not None:
            self._flip = bool(flip)
        self._update_frame()
        self.labels = None
        self.manual = None
        self._invalidate_after("original")
        self.reports.pop("segmentacion", None)
        return self.frame.info

    def analyze(self, check_intersections: bool = True) -> dict:
        self._need(self.work is not None, "Cargue primero un STL.")
        t0 = time.time()
        rep = analyze(self._raw_scaled, check_intersections=check_intersections)
        rep["unit_guess"] = self.reports["carga"]["unit_guess"]
        rep["unit_scale_to_mm"] = self.unit_scale
        rep["dimensions_file_units"] = self.reports["carga"]["dimensions_file"]
        rep["warnings"] = [w for w in rep["warnings"] if "unidades" not in w] + self.reports["carga"]["warnings"]
        rep["orientation"] = self.frame.info
        rep["warnings"] += self.frame.info.get("warnings", [])
        rep["seconds"] = round(time.time() - t0, 3)
        self.reports["analisis"] = rep
        return rep

    # ------------------------------------------------------------------ etapa 2
    def segment(self, params: dict | None = None, manual: np.ndarray | None = None,
                main_face: int | None = None) -> dict:
        self._need(self.work_c is not None, "Cargue primero un STL.")
        t0 = time.time()
        if manual is not None:
            manual = np.asarray(manual, dtype=np.uint8)
            self._need(len(manual) == self.work.n_faces, "Las correcciones manuales no corresponden a esta malla.")
            self.manual = manual
        try:
            res = segment_plantar(self.work_c, SegmentationParams.from_dict(params), self.manual, main_face)
        except ValueError as e:
            raise PipelineError(str(e)) from e
        self.labels = res["labels"]
        self.reason = res["reason"]
        self._seg_params = SegmentationParams.from_dict(params)
        info = res["info"]
        info["warnings"] = self.frame.info.get("warnings", []) + info["warnings"]
        info["seconds"] = round(time.time() - t0, 3)
        self.reports["segmentacion"] = info
        self._invalidate_after("original")
        return info

    def set_labels(self, labels: np.ndarray):
        self._need(self.work is not None, "Cargue primero un STL.")
        labels = np.asarray(labels, dtype=np.uint8)
        self._need(len(labels) == self.work.n_faces, "Las etiquetas no corresponden a esta malla.")
        self.labels = labels.copy()
        self._invalidate_after("original")

    def extract(self) -> dict:
        self._need(self.labels is not None, "Segmente primero la superficie plantar.")
        self._need((self.labels > 0).any(), "La segmentación no conserva ninguna cara.")
        seg = extract(self.work_c, self.labels)
        n, a = seg.face_normals()
        if (a * n[:, 2]).sum() < 0:      # normales del archivo hacia dentro: se orienta hacia +Z
            seg = seg.flipped()
        # Borde real contiguo a la selección (pared que continúa la planta): solo se usa para
        # que el contorno regularizado tome su altura de la superficie medida.
        kept = self.labels > 0
        topo = build_topology(self.work_c)
        g = face_graph(topo, self.work_c.n_faces)
        cur = kept.copy()
        for _ in range(8):
            cur = cur | (g @ cur.astype(np.int32) > 0)
        rim = cur & ~kept
        if self.manual is not None:
            rim &= self.manual != 2
        self._rim = self.work_c.submesh(rim) if rim.any() else None
        _, a_all = self.work_c.face_normals()
        info = {
            "faces": seg.n_faces, "vertices": seg.n_vertices, "area": float(a.sum()),
            "removed_faces_pct": float(100.0 * (1 - seg.n_faces / self.work_c.n_faces)),
            "removed_area_pct": float(100.0 * (1 - a.sum() / max(a_all.sum(), 1e-300))),
            "uncertain_faces": int((self.labels == 2).sum()),
            "warnings": [],
        }
        if info["uncertain_faces"]:
            info["warnings"].append(f"Se conservan {info['uncertain_faces']} caras dudosas pendientes de revisión.")
        self._invalidate_after("original")
        self.stages["segmentado"] = seg
        self.reports["segmentado"] = info
        return info

    # ------------------------------------------------------------------ etapa 3
    def cut(self, planes: list[dict] | None = None, params: dict | None = None) -> dict:
        """Recorte por planos (opcional) + contorno limpio y remallado.

        Cada plano es ``{"point": [x,y,z], "normal": [x,y,z]}`` en el marco del archivo (mm);
        se conserva el lado hacia el que apunta la normal.
        """
        self._need("segmentado" in self.stages, "Elimine primero la geometría no deseada (etapa 3 del panel).")
        t0 = time.time()
        m = self.stages["segmentado"]
        before = m.area
        for pl in planes or []:
            pt = self.frame.to_canonical(np.asarray(pl["point"], float)[None])[0]
            nr = np.asarray(pl["normal"], float) @ self.frame.rotation.T
            m = plane_clip(m, pt, nr)
            self._need(m.n_faces > 0, "El plano de corte elimina toda la superficie.")
        context = [self.stages["segmentado"]] + ([self._rim] if self._rim is not None else [])
        try:
            res = remesh_heightfield(m, ContourParams.from_dict(params), merge_meshes(context))
        except ValueError as e:
            raise PipelineError(str(e)) from e
        info = res["info"]
        ins = (self.reports.get("segmentacion") or {}).get("insole")
        if ins:
            try:
                info["cobertura"] = coverage_report(self.work_c, res["mesh"], ins["frame"], ins["targets"],
                                                    getattr(self, "_seg_params", None))
                for prob in info["cobertura"]["problems"]:
                    info.setdefault("warnings", []).append("Plantilla: " + prob)
            except Exception as e:          # la medida de cobertura nunca debe impedir el corte
                info["cobertura"] = {"error": str(e)}
        info["planes"] = len(planes or [])
        info["area_removed_by_planes"] = float(before - m.area)
        info["seconds"] = round(time.time() - t0, 3)
        self._invalidate_after("segmentado")
        self.stages["recortado"] = res["mesh"]
        self._support = res["support"]
        self._cut_reference = m
        self.reports["recortado"] = info
        return info

    # ------------------------------------------------------------------ etapa 4
    def smooth(self, params: dict | None = None) -> dict:
        self._need("recortado" in self.stages, "Ejecute primero el recorte.")
        t0 = time.time()
        res = smooth_surface(self.stages["recortado"], SmoothParams.from_dict(params))
        info = res["info"]
        info["seconds"] = round(time.time() - t0, 3)
        self._invalidate_after("recortado")
        self.stages["suavizado"] = res["mesh"]
        self.fields = {k: v.astype(np.float32) for k, v in res["fields"].items()}
        self.reports["suavizado"] = info
        return info

    # ------------------------------------------------------------------ etapa 5
    def solid(self, params: dict | None = None) -> dict:
        src = "suavizado" if "suavizado" in self.stages else "recortado"
        self._need(src in self.stages, "Ejecute primero el recorte (y, si lo desea, el suavizado).")
        t0 = time.time()
        try:
            res = build_solid(self.stages[src], SolidParams.from_dict(params))
        except ValueError as e:
            raise PipelineError(str(e)) from e
        info = res["info"]
        info["source_stage"] = src
        info["seconds"] = round(time.time() - t0, 3)
        self._invalidate_after("suavizado")
        self.stages["solido"] = res["mesh"]
        self.reports["solido"] = info
        return info

    def validate(self) -> dict:
        self._need("solido" in self.stages, "Cree primero el sólido cerrado.")
        t0 = time.time()
        rep = validate_solid(self.stages["solido"])
        n_top = self.reports["solido"]["top_vertices"]
        f_top = self.reports["solido"]["top_faces"]
        solid = self.stages["solido"]
        src = self.stages[self.reports["solido"]["source_stage"]]
        same = bool(np.array_equal(solid.vertices[:n_top], src.vertices))
        rep["checks"].append({"id": "top_preserved", "name": "Superficie anatómica intacta en el sólido",
                              "ok": same, "value": same, "detail": ""})
        if not same:
            rep["errors"].append("La superficie anatómica del sólido no coincide con la etapa anterior.")
            rep["valid"] = False
        top = Mesh(solid.vertices[:n_top], src.faces)
        q = quality_report(self._cut_reference, top, self._support)
        q["removed_faces_pct"] = self.reports.get("segmentado", {}).get("removed_faces_pct")
        q["removed_area_pct"] = self.reports.get("segmentado", {}).get("removed_area_pct")
        sm = self.reports.get("suavizado")
        if sm:
            q["curvature_relative_change"] = sm["curvature_relative_change"]
            q["arch_depth_before"] = sm["arch_depth_before"]
            q["arch_depth_after"] = sm["arch_depth_after"]
        q["top_faces"] = f_top
        rep["quality"] = {**rep.get("quality", {}), **q}
        if q["preservation_pct"] < 95.0:
            rep["warnings"].append(f"Solo el {q['preservation_pct']:.1f}% de la planta segmentada queda representada "
                                   f"a menos de {q['preservation_tolerance']} mm (contorno regularizado o suavizado fuerte).")
        for key in ("segmentacion", "recortado", "suavizado", "solido"):
            for w in self.reports.get(key, {}).get("warnings", []):
                if w not in rep["warnings"]:
                    rep["warnings"].append(w)
        rep["seconds"] = round(time.time() - t0, 3)
        self.reports["validacion"] = rep
        return rep

    def revert(self, stage: str) -> dict:
        """Vuelve a una etapa anterior descartando las posteriores."""
        self._need(stage in STAGES and self.has(stage), f"La etapa «{stage}» no está disponible.")
        self._invalidate_after(stage)
        return {"stage": stage}

    # ------------------------------------------------------------------ automático
    def auto(self, params: dict | None = None) -> dict:
        """Procesamiento completo. ``params`` = {"segment":{}, "cut":{}, "smooth":{}, "solid":{}}."""
        params = params or {}
        out = {}
        if "analisis" not in self.reports:
            out["analisis"] = self.analyze()
        seg = params.get("segment") or {}
        tune = params.get("autotune", True) and SegmentationParams.from_dict(seg).mode == "plantilla"
        if tune and (self.labels is None or params.get("resegment", True)):
            self.autotune_cut(seg, params.get("cut"), params.get("planes"))
            out["segmentacion"] = self.reports["segmentacion"]
            out["segmentado"] = self.reports["segmentado"]
            out["recortado"] = self.reports["recortado"]
        else:
            if self.labels is None or params.get("resegment", True):
                out["segmentacion"] = self.segment(seg)
            out["segmentado"] = self.extract()
            out["recortado"] = self.cut(params.get("planes"), params.get("cut"))
        out["suavizado"] = self.smooth(params.get("smooth"))
        out["solido"] = self.solid(params.get("solid"))
        out["validacion"] = self.validate()
        return out

    def run_cut(self, seg: dict | None = None, cut: dict | None = None, planes=None) -> dict:
        """Segmentar + extraer + recortar con unos parámetros; devuelve la cobertura."""
        self.segment(seg)
        self.extract()
        info = self.cut(planes, cut)
        return info.get("cobertura") or {}

    def autotune_cut(self, seg: dict | None = None, cut: dict | None = None, planes=None,
                     max_rounds: int = 3) -> list[dict]:
        """Corta, mide la cobertura de la plantilla y reajusta los parámetros hasta que esté completa.

        Se queda con el mejor intento. Las correcciones manuales se respetan en todos.
        """
        seg = dict(seg or {})
        hist: list[dict] = []
        best = None
        for r in range(max_rounds):
            cov = self.run_cut(seg, cut, planes)
            if not cov or "score" not in cov:
                break
            hist.append({"round": r + 1, "source": "reglas", "params": dict(seg), "score": cov["score"],
                         "problems": cov["problems"]})
            if best is None or cov["score"] > best[0] + 1e-4:
                best = (cov["score"], dict(seg), r)
            if cov["ok"]:
                break
            new = adjust_for_coverage(seg, cov)
            if new == seg:
                break
            seg = new
        if best is not None and best[2] != len(hist) - 1:
            self.run_cut(best[1], cut, planes)
            hist.append({"round": len(hist) + 1, "source": "reglas", "params": dict(best[1]),
                         "score": best[0], "problems": [], "note": "se recupera el mejor intento"})
        self.tuning = hist
        if hist:
            self.reports["recortado"]["autotune"] = hist
        return hist

    # ------------------------------------------------------------------ salida
    def has(self, stage: str) -> bool:
        return stage == "original" and self.work is not None or stage in self.stages

    def mesh(self, stage: str, frame: str = "original") -> Mesh:
        """Malla de una etapa en mm. ``frame``: "original" (marco del archivo) o "canonical"."""
        self._need(self.has(stage), f"La etapa «{stage}» todavía no está disponible.")
        if stage == "original":
            return self.work if frame == "original" else self.work_c
        m = self.stages[stage]
        if frame == "canonical":
            if stage == "solido" and self.reports["solido"]["params"]["side"] == "exterior":
                v = m.vertices * np.array([1.0, -1.0, -1.0])     # giro de 180° sobre X: base abajo
                v[:, 2] -= v[:, 2].min()
                return Mesh(v, m.faces)
            v = m.vertices.copy()
            if stage == "solido":
                v[:, 2] -= v[:, 2].min()
            return Mesh(v, m.faces)
        return Mesh(self.frame.to_world(m.vertices), m.faces)

    def export(self, stage: str, fmt: str = "stl", frame: str = "original") -> bytes:
        if stage == "solido":
            rep = self.reports.get("validacion") or self.validate()
            self._need(rep["valid"], "El sólido no ha superado la validación: " + "; ".join(rep["errors"]))
        return export_bytes(self.mesh(stage, frame), fmt)

    def original_bytes(self) -> bytes:
        self._need(self.original_path is not None and self.original_path.exists(),
                   "No hay copia del archivo original.")
        return self.original_path.read_bytes()

    def state(self) -> dict:
        return {"file": self.name, "stages": [s for s in STAGES if self.has(s)],
                "has_labels": self.labels is not None, "reports": self.reports,
                "frame": None if self.frame is None else {
                    "plantar_axis": self.frame.rotation[2].tolist(), "long_axis": self.frame.rotation[0].tolist(),
                    "width_axis": self.frame.rotation[1].tolist(), "origin": self.frame.origin.tolist()}}
