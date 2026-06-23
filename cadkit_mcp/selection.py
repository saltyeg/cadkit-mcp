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
