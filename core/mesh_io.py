"""Lectura de STL (binario/ASCII) y escritura de STL, OBJ y PLY sin dependencias externas."""
from __future__ import annotations

import io
import re
import struct
from pathlib import Path

import numpy as np

from .mesh import Mesh

_STL_DTYPE = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def _is_binary_stl(data: bytes) -> bool:
    if len(data) < 84:
        return False
    n = struct.unpack_from("<I", data, 80)[0]
    if len(data) == 84 + 50 * n:
        return True
    head = data[:512].lstrip().lower()
    return not head.startswith(b"solid")


def load_stl_bytes(data: bytes) -> Mesh:
    """Carga un STL. Devuelve una malla *sin soldar* (3 vértices por cara)."""
    if _is_binary_stl(data):
        n = struct.unpack_from("<I", data, 80)[0]
        n = min(n, (len(data) - 84) // 50)
        rec = np.frombuffer(data, dtype=_STL_DTYPE, count=n, offset=84)
        verts = rec["v"].reshape(-1, 3).astype(np.float64)
    else:
        text = data.decode("utf-8", errors="ignore")
        nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text, flags=re.IGNORECASE)
        verts = np.array(nums, dtype=np.float64).reshape(-1, 3)
        verts = verts[: (len(verts) // 3) * 3]
    if len(verts) == 0:
        raise ValueError("El archivo STL no contiene triángulos.")
    if not np.isfinite(verts).all():
        bad = ~np.isfinite(verts).all(axis=1)
        keep = ~bad.reshape(-1, 3).any(axis=1)
        verts = verts.reshape(-1, 3, 3)[keep].reshape(-1, 3)
    faces = np.arange(len(verts), dtype=np.int64).reshape(-1, 3)
    return Mesh(verts, faces)


def load_stl(path) -> Mesh:
    return load_stl_bytes(Path(path).read_bytes())


def stl_bytes(mesh: Mesh, header: str = "foot-mesh-processor") -> bytes:
    n, _ = mesh.face_normals()
    rec = np.zeros(mesh.n_faces, dtype=_STL_DTYPE)
    rec["n"] = n.astype("<f4")
    rec["v"] = mesh.triangles.astype("<f4")
    head = header.encode("ascii", errors="ignore")[:80].ljust(80, b" ")
    return head + struct.pack("<I", mesh.n_faces) + rec.tobytes()


def obj_bytes(mesh: Mesh) -> bytes:
    buf = io.StringIO()
    buf.write("# foot-mesh-processor\n")
    np.savetxt(buf, mesh.vertices, fmt="v %.6f %.6f %.6f")
    np.savetxt(buf, mesh.faces + 1, fmt="f %d %d %d")
    return buf.getvalue().encode("ascii")


def ply_bytes(mesh: Mesh) -> bytes:
    head = (
        "ply\nformat binary_little_endian 1.0\ncomment foot-mesh-processor\n"
        f"element vertex {mesh.n_vertices}\nproperty float x\nproperty float y\nproperty float z\n"
        f"element face {mesh.n_faces}\nproperty list uchar int vertex_indices\nend_header\n"
    ).encode("ascii")
    frec = np.zeros(mesh.n_faces, dtype=np.dtype([("k", "u1"), ("i", "<i4", 3)]))
    frec["k"] = 3
    frec["i"] = mesh.faces
    return head + mesh.vertices.astype("<f4").tobytes() + frec.tobytes()


def export_bytes(mesh: Mesh, fmt: str) -> bytes:
    fmt = fmt.lower().lstrip(".")
    if fmt == "stl":
        return stl_bytes(mesh)
    if fmt == "obj":
        return obj_bytes(mesh)
    if fmt == "ply":
        return ply_bytes(mesh)
    raise ValueError(f"Formato no soportado: {fmt}")


def save(mesh: Mesh, path) -> None:
    path = Path(path)
    path.write_bytes(export_bytes(mesh, path.suffix))
