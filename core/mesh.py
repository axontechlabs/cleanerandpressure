"""Estructura de malla triangular indexada y conectividad básica.

El núcleo geométrico se apoya únicamente en NumPy y SciPy para que la
instalación sea trivial (sin compilación, licencias BSD) y reproducible en
macOS, Linux, Docker y Codespaces.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph


def cross2(a, b):
    """Producto vectorial 2D (componente z); np.cross ya no admite vectores 2D."""
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


class Mesh:
    """Malla triangular indexada: ``vertices`` (V,3) float64 y ``faces`` (F,3) int64."""

    __slots__ = ("vertices", "faces")

    def __init__(self, vertices, faces):
        self.vertices = np.ascontiguousarray(vertices, dtype=np.float64).reshape(-1, 3)
        self.faces = np.ascontiguousarray(faces, dtype=np.int64).reshape(-1, 3)

    @property
    def n_vertices(self) -> int:
        return len(self.vertices)

    @property
    def n_faces(self) -> int:
        return len(self.faces)

    def copy(self) -> "Mesh":
        return Mesh(self.vertices.copy(), self.faces.copy())

    @property
    def triangles(self) -> np.ndarray:
        return self.vertices[self.faces]

    @property
    def centroids(self) -> np.ndarray:
        return self.triangles.mean(axis=1)

    def face_normals(self):
        """Devuelve (normales unitarias (F,3), áreas (F,))."""
        t = self.triangles
        cr = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        dbl = np.linalg.norm(cr, axis=1)
        n = cr / np.maximum(dbl, 1e-300)[:, None]
        return n, 0.5 * dbl

    @property
    def area(self) -> float:
        return float(self.face_normals()[1].sum())

    @property
    def bounds(self) -> np.ndarray:
        if self.n_vertices == 0:
            return np.zeros((2, 3))
        return np.array([self.vertices.min(axis=0), self.vertices.max(axis=0)])

    @property
    def extents(self) -> np.ndarray:
        b = self.bounds
        return b[1] - b[0]

    @property
    def volume(self) -> float:
        """Volumen con signo (positivo si las normales apuntan hacia fuera)."""
        t = self.triangles
        return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)

    def vertex_normals(self) -> np.ndarray:
        n, a = self.face_normals()
        vn = np.zeros_like(self.vertices)
        for k in range(3):
            np.add.at(vn, self.faces[:, k], n * a[:, None])
        return vn / np.maximum(np.linalg.norm(vn, axis=1), 1e-300)[:, None]

    def submesh(self, face_mask) -> "Mesh":
        """Submalla con las caras indicadas (máscara o índices); reindexa vértices."""
        faces = self.faces[face_mask]
        used = np.zeros(self.n_vertices, dtype=bool)
        used[faces.ravel()] = True
        remap = np.cumsum(used) - 1
        return Mesh(self.vertices[used], remap[faces])

    def transformed(self, rotation: np.ndarray, origin: np.ndarray) -> "Mesh":
        """Coordenadas canónicas: ``(p - origin) @ rotation.T``."""
        return Mesh((self.vertices - origin) @ rotation.T, self.faces.copy())

    def flipped(self) -> "Mesh":
        return Mesh(self.vertices.copy(), self.faces[:, ::-1].copy())


@dataclass
class Topology:
    """Conectividad por aristas de una malla."""

    n_edges: int
    edges: np.ndarray        # (E,2) vértices ordenados de cada arista única
    edge_count: np.ndarray   # (E,) caras incidentes
    he_edge: np.ndarray      # (F,3) arista única de cada semiarista
    adj: np.ndarray          # (F,3) cara vecina por cada lado (-1 si borde o no variedad)
    opp: np.ndarray          # (F,3) semiarista opuesta (índice plano) o -1
    consistent: np.ndarray   # (F,3) la cara vecina recorre la arista en sentido contrario

    @property
    def he_count(self) -> np.ndarray:
        return self.edge_count[self.he_edge]

    @property
    def boundary_edges(self) -> np.ndarray:
        return self.edges[self.edge_count == 1]

    @property
    def nonmanifold_edges(self) -> np.ndarray:
        return self.edges[self.edge_count > 2]


def build_topology(mesh: Mesh) -> Topology:
    faces = mesh.faces
    nf = len(faces)
    nv = max(mesh.n_vertices, 1)
    if nf == 0:
        z = np.zeros((0, 3), dtype=np.int64)
        return Topology(0, np.zeros((0, 2), np.int64), np.zeros(0, np.int64), z, z, z, z.astype(bool))
    v0 = faces.ravel()
    v1 = faces[:, [1, 2, 0]].ravel()
    lo = np.minimum(v0, v1)
    hi = np.maximum(v0, v1)
    key = lo * nv + hi
    order = np.argsort(key, kind="stable")
    ks = key[order]
    start = np.r_[True, ks[1:] != ks[:-1]]
    eid_sorted = np.cumsum(start) - 1
    n_edges = int(eid_sorted[-1]) + 1
    he_edge = np.empty(3 * nf, dtype=np.int64)
    he_edge[order] = eid_sorted
    counts = np.bincount(eid_sorted, minlength=n_edges)
    first = np.flatnonzero(start)
    edges = np.stack([lo[order[first]], hi[order[first]]], axis=1)
    m = counts == 2
    a = order[first[m]]
    b = order[first[m] + 1]
    adj = -np.ones(3 * nf, dtype=np.int64)
    adj[a] = b // 3
    adj[b] = a // 3
    opp = -np.ones(3 * nf, dtype=np.int64)
    opp[a] = b
    opp[b] = a
    cons = np.zeros(3 * nf, dtype=bool)
    c = v0[a] != v0[b]
    cons[a] = c
    cons[b] = c
    return Topology(n_edges, edges, counts, he_edge.reshape(nf, 3), adj.reshape(nf, 3),
                    opp.reshape(nf, 3), cons.reshape(nf, 3))


def face_graph(topo: Topology, n_faces: int, edge_ok=None, face_ok=None) -> sparse.csr_matrix:
    """Grafo de adyacencia entre caras (aristas variedad), con filtros opcionales."""
    f = np.repeat(np.arange(n_faces), 3)
    g = topo.adj.ravel()
    ok = g >= 0
    if edge_ok is not None:
        ok &= edge_ok.ravel()
    if face_ok is not None:
        ok &= face_ok[f] & face_ok[np.maximum(g, 0)]
    f, g = f[ok], g[ok]
    return sparse.csr_matrix((np.ones(len(f), dtype=np.int8), (f, g)), shape=(n_faces, n_faces))


def face_components(topo: Topology, n_faces: int, edge_ok=None, face_ok=None):
    """Componentes conexas de caras. Las caras excluidas reciben la etiqueta -1."""
    g = face_graph(topo, n_faces, edge_ok, face_ok)
    n, lab = csgraph.connected_components(g, directed=False)
    if face_ok is not None:
        lab = lab.copy()
        lab[~face_ok] = -1
        valid = lab >= 0
        if valid.any():
            uniq, inv = np.unique(lab[valid], return_inverse=True)
            lab[valid] = inv
            n = len(uniq)
        else:
            n = 0
    return int(n), lab


def vertex_components(mesh: Mesh):
    """Componentes conexas por vértices compartidos; devuelve etiqueta por cara."""
    f = mesh.faces
    if len(f) == 0:
        return 0, np.zeros(0, dtype=np.int64)
    i = np.r_[f[:, 0], f[:, 1]]
    j = np.r_[f[:, 1], f[:, 2]]
    g = sparse.csr_matrix((np.ones(len(i), dtype=np.int8), (i, j)), shape=(mesh.n_vertices, mesh.n_vertices))
    _, lab = csgraph.connected_components(g, directed=False)
    fl = lab[f[:, 0]]
    uniq, inv = np.unique(fl, return_inverse=True)
    return len(uniq), inv


def boundary_loops(mesh: Mesh, topo: Topology | None = None) -> list[np.ndarray]:
    """Bucles de borde ordenados (siguiendo el sentido de las semiaristas de borde)."""
    topo = topo or build_topology(mesh)
    f = mesh.faces
    bmask = topo.he_count == 1
    v0 = f[bmask]
    v1 = f[:, [1, 2, 0]][bmask]
    if len(v0) == 0:
        return []
    nxt: dict[int, int] = {}
    for a, b in zip(v0.tolist(), v1.tolist()):
        nxt.setdefault(a, b)
    loops = []
    visited: set[int] = set()
    for a in v0.tolist():
        if a in visited:
            continue
        loop = []
        cur = a
        while cur not in visited and cur in nxt:
            visited.add(cur)
            loop.append(cur)
            cur = nxt[cur]
        if len(loop) >= 3:
            loops.append(np.array(loop, dtype=np.int64))
    loops.sort(key=len, reverse=True)
    return loops


def merge_meshes(meshes) -> Mesh:
    vs, fs, off = [], [], 0
    for m in meshes:
        vs.append(m.vertices)
        fs.append(m.faces + off)
        off += m.n_vertices
    if not vs:
        return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    return Mesh(np.vstack(vs), np.vstack(fs))
