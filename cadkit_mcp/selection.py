"""Semantic geometry selection — find edges/faces by *meaning*, return deterministic IDs.

Built on read-only FeatureScript so downstream features (fillet, chamfer, extrude-on-face)
reference topology robustly instead of leaking raw transient IDs to the caller.
"""
from typing import Any, Dict, List, Optional


def _strings(node, out: List[str]):
    if isinstance(node, dict):
        if node.get("btType", "").endswith("BTFSValueString") and node.get("typeTag") != "EntityType":
            out.append(node["value"])
        for v in node.values():
            _strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _strings(v, out)


def parse_ids(fs_result: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    _strings(fs_result.get("result"), ids)
    # de-dup, preserve order
    seen = set(); out = []
    for i in ids:
        if i not in seen:
            seen.add(i); out.append(i)
    return out


# ---- edge finders ---------------------------------------------------------
# All finders restrict to solid-body topology — sketch curves are also EDGE/LINE
# geometry and would otherwise crash evEdgeConvexity / surface evals.
SOLID = "qBodyType(qEverything(EntityType.BODY), BodyType.SOLID)"
SOLID_EDGES = f"qOwnedByBody({SOLID}, EntityType.EDGE)"
SOLID_FACES = f"qOwnedByBody({SOLID}, EntityType.FACE)"


def fs_circular_edges(radius_in: Optional[float], tol_in: float = 0.001) -> str:
    cond = "true" if radius_in is None else f"abs(s.radius/inch - {radius_in}) < {tol_in}"
    return f"""function(context is Context, queries){{
  var out = [];
  for (var e in evaluateQuery(context, qGeometry({SOLID_EDGES}, GeometryType.CIRCLE))){{
    var s = evCurveDefinition(context, {{"edge": e}});
    if ({cond}) {{ out = append(out, transientQueriesToStrings(e)); }}
  }}
  return out;
}}"""


def fs_concave_edges(which: str = "CONCAVE") -> str:
    # which in {CONCAVE, CONVEX, SMOOTH} per EdgeConvexityType
    return f"""function(context is Context, queries){{
  var out = [];
  for (var e in evaluateQuery(context, {SOLID_EDGES})){{
    if (evEdgeConvexity(context, {{"edge": e}}) == EdgeConvexityType.{which}){{
      out = append(out, transientQueriesToStrings(e));
    }}
  }}
  return out;
}}"""


def fs_linear_edges(axis: Optional[str], through_in: Optional[list], tol_in: float = 0.005) -> str:
    # axis in {"X","Y","Z"} ; through is [x,y,z] inches (any None coord ignored)
    axis_vec = {"X": "[1,0,0]", "Y": "[0,1,0]", "Z": "[0,0,1]"}.get(axis or "", None)
    checks = ["true"]
    if axis_vec:
        checks.append(f"abs(abs(dot(d, vector({axis_vec}))) - 1) < 1e-3")
    if through_in:
        for i, c in enumerate(through_in):
            if c is not None:
                checks.append(f"abs(o[{i}]/inch - ({c})) < {tol_in}")
    cond = " && ".join(checks)
    return f"""function(context is Context, queries){{
  var out = [];
  for (var e in evaluateQuery(context, qGeometry({SOLID_EDGES}, GeometryType.LINE))){{
    var ln = evLine(context, {{"edge": e}});
    var o = ln.origin; var d = ln.direction;
    if ({cond}) {{ out = append(out, transientQueriesToStrings(e)); }}
  }}
  return out;
}}"""


# ---- face finders ---------------------------------------------------------
def fs_planar_faces_by_normal(normal: list, tol: float = 1e-3) -> str:
    nx, ny, nz = normal
    return f"""function(context is Context, queries){{
  var out = [];
  for (var f in evaluateQuery(context, qGeometry({SOLID_FACES}, GeometryType.PLANE))){{
    var pl = evPlane(context, {{"face": f}});
    var n = pl.normal;
    if (abs(n[0]-({nx}))<{tol} && abs(n[1]-({ny}))<{tol} && abs(n[2]-({nz}))<{tol}){{
      out = append(out, transientQueriesToStrings(f));
    }}
  }}
  return out;
}}"""


def fs_cylindrical_faces(radius_in: Optional[float], tol_in: float = 0.001) -> str:
    cond = "true" if radius_in is None else f"abs(s.radius/inch - {radius_in}) < {tol_in}"
    return f"""function(context is Context, queries){{
  var out = [];
  for (var f in evaluateQuery(context, qGeometry({SOLID_FACES}, GeometryType.CYLINDER))){{
    var s = evSurfaceDefinition(context, {{"face": f}});
    if ({cond}) {{ out = append(out, transientQueriesToStrings(f)); }}
  }}
  return out;
}}"""


# ---- richer selection: by area / by position (reduce reliance on raw normals) ---------
_AXIS_IDX = {"X": 0, "Y": 1, "Z": 2}


def fs_faces_by_area(want_largest: bool = True) -> str:
    """The single largest (or smallest) face by area — e.g. 'the big flat face to sketch on'."""
    cmp = ">" if want_largest else "<"
    init = "-1.0" if want_largest else "1e18"
    # strip units before comparing — FeatureScript throws on length/area-vs-plain-number compares
    return f"""function(context is Context, queries){{
  var best; var bestA = {init};
  for (var f in evaluateQuery(context, {SOLID_FACES})){{
    var a = evArea(context, {{"entities": f}}) / (inch * inch);
    if (a {cmp} bestA){{ bestA = a; best = f; }}
  }}
  if (best == undefined){{ return []; }}
  return transientQueriesToStrings(best);
}}"""


def fs_extreme_faces(axis: str, want_max: bool = True) -> str:
    """The face whose centre sits furthest along +axis (max) or -axis (min) — e.g. the top face."""
    i = _AXIS_IDX[axis]
    cmp = ">" if want_max else "<"
    init = "-1e18" if want_max else "1e18"
    return f"""function(context is Context, queries){{
  var best; var bestV = {init};
  for (var f in evaluateQuery(context, {SOLID_FACES})){{
    var b = evBox3d(context, {{"topology": f}});
    var v = (b.minCorner[{i}] + b.maxCorner[{i}]) / 2 / inch;
    if (v {cmp} bestV){{ bestV = v; best = f; }}
  }}
  if (best == undefined){{ return []; }}
  return transientQueriesToStrings(best);
}}"""


def fs_extreme_edges(axis: str, want_max: bool = True, tol_in: float = 0.01) -> str:
    """ALL edges at the extreme position along an axis — e.g. every top edge, to fillet at once."""
    i = _AXIS_IDX[axis]
    cmp = ">" if want_max else "<"
    init = "-1e18" if want_max else "1e18"
    return f"""function(context is Context, queries){{
  var edges = evaluateQuery(context, {SOLID_EDGES});
  var bestV = {init};
  for (var e in edges){{
    var b = evBox3d(context, {{"topology": e}});
    var v = (b.minCorner[{i}] + b.maxCorner[{i}]) / 2 / inch;
    if (v {cmp} bestV){{ bestV = v; }}
  }}
  var out = [];
  for (var e in edges){{
    var b = evBox3d(context, {{"topology": e}});
    var v = (b.minCorner[{i}] + b.maxCorner[{i}]) / 2 / inch;
    if (abs(v - bestV) < {tol_in}){{ out = append(out, transientQueriesToStrings(e)); }}
  }}
  return out;
}}"""


def fs_faces_on_plane(axis: str, coord_in: float, tol_in: float = 0.01) -> str:
    """Planar faces lying in the plane {axis} = coord — e.g. 'the face on Z=0'. A face counts when
    it is thin along the axis (min ≈ max, so it's parallel to the plane) AND sits at the coord.
    Built on evBox3d, the same primitive `fs_extreme_faces` uses (already live-verified)."""
    i = _AXIS_IDX[axis]
    return f"""function(context is Context, queries){{
  var out = [];
  for (var f in evaluateQuery(context, {SOLID_FACES})){{
    var b = evBox3d(context, {{"topology": f}});
    var lo = b.minCorner[{i}]/inch; var hi = b.maxCorner[{i}]/inch;
    if (abs(hi - lo) < {tol_in} && abs((lo + hi)/2 - ({coord_in})) < {tol_in}){{
      out = append(out, transientQueriesToStrings(f));
    }}
  }}
  return out;
}}"""


def fs_edges_on_plane(axis: str, coord_in: float, tol_in: float = 0.01) -> str:
    """Edges lying in the plane {axis} = coord — e.g. every edge at Z=1. An edge counts when its
    box is thin along the axis (it lies in the plane) and sits at the coord."""
    i = _AXIS_IDX[axis]
    return f"""function(context is Context, queries){{
  var out = [];
  for (var e in evaluateQuery(context, {SOLID_EDGES})){{
    var b = evBox3d(context, {{"topology": e}});
    var lo = b.minCorner[{i}]/inch; var hi = b.maxCorner[{i}]/inch;
    if (abs(hi - lo) < {tol_in} && abs((lo + hi)/2 - ({coord_in})) < {tol_in}){{
      out = append(out, transientQueriesToStrings(e));
    }}
  }}
  return out;
}}"""


def fs_faces_adjacent_to_extreme(axis: str, want_max: bool = True) -> str:
    """Faces that border the extreme face along an axis — e.g. 'the side faces around the top'.

    Composes a seed (the extreme face, found exactly like `fs_extreme_faces`) with the faces
    sharing an edge with it, all in one eval so no transient id has to be round-tripped back into
    a query.

    The `qAdjacent(query, AdjacencyType.EDGE, EntityType.FACE)` signature is LIVE-VERIFIED
    (scripts/smoke_fillet_adjacency.py: on a box it returned the side faces bordering the top
    face). Onshape's qAdjacent already excludes the seed, so no manual subtraction is needed.
    """
    i = _AXIS_IDX[axis]
    cmp = ">" if want_max else "<"
    init = "-1e18" if want_max else "1e18"
    return f"""function(context is Context, queries){{
  var best; var bestV = {init};
  for (var f in evaluateQuery(context, {SOLID_FACES})){{
    var b = evBox3d(context, {{"topology": f}});
    var v = (b.minCorner[{i}] + b.maxCorner[{i}]) / 2 / inch;
    if (v {cmp} bestV){{ bestV = v; best = f; }}
  }}
  if (best == undefined){{ return []; }}
  var out = [];
  for (var nb in evaluateQuery(context, qAdjacent(best, AdjacencyType.EDGE, EntityType.FACE))){{
    out = append(out, transientQueriesToStrings(nb));
  }}
  return out;
}}"""


def fs_sketch_vertices(sketch_fid: str) -> str:
    """Deterministic ids of the point/vertex entities a sketch created — the native Hole
    feature's `locations` are sketch points."""
    return f"""function(context is Context, queries){{
  var out = [];
  for (var v in evaluateQuery(context, qCreatedBy(makeId("{sketch_fid}"), EntityType.VERTEX))){{
    out = append(out, transientQueriesToStrings(v));
  }}
  return out;
}}"""
