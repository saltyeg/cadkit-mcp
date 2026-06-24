"""Stateful sketch-session builder for Onshape — the human design pattern.

One sketch accumulates entities + geometric constraints + driving dimensions,
then emits a single BTMSketch-151 feature.  Unlike coordinate-driven generators,
this produces *fully-defined* sketches: grounded to the origin and dimensioned.

All JSON shapes here were verified against the live Onshape API:
  - external refs use BTMParameterQueryList-148 / parameterId "externalFirst"
  - the part-studio origin vertex has deterministic id "IB"
  - default planes: Front=JCC, Top=JDC, Right=JEC
"""
import math
from typing import Any, Dict, List, Optional, Tuple

IN = 0.0254  # inches -> meters
PLANES = {"Front": "JCC", "Top": "JDC", "Right": "JEC"}
ORIGIN_VERTEX = "IB"


def _expr(value) -> str:
    """A dimension value: a number (inches), or a raw expression / #variable."""
    if isinstance(value, (int, float)):
        return f"{value} in"
    return str(value)  # e.g. "#base_len", "60 mm", "2.4*inch"


class SketchSession:
    def __init__(self, document_id, workspace_id, element_id,
                 plane: str = "Front", name: str = "Sketch"):
        # `plane` is a standard plane name (Front/Top/Right) OR a deterministic face id
        # from cad_find_faces — letting a sketch live on an existing face/offset plane.
        self.doc, self.ws, self.elem = document_id, workspace_id, element_id
        self.plane, self.name = plane, name
        self.plane_id = PLANES.get(plane, plane)
        self.entities: List[Dict[str, Any]] = []
        self.constraints: List[Dict[str, Any]] = []
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    # ---- entities ----------------------------------------------------------
    def add_line(self, start: Tuple[float, float], end: Tuple[float, float],
                 construction: bool = False) -> str:
        sx, sy = start; ex, ey = end
        length = math.hypot(ex - sx, ey - sy)
        if length == 0:
            raise ValueError("line endpoints must differ")
        eid = self._id("ln")
        self.entities.append({
            "btType": "BTMSketchCurveSegment-155", "entityId": eid,
            "startPointId": f"{eid}.start", "endPointId": f"{eid}.end",
            "startParam": 0.0, "endParam": length * IN,
            "geometry": {"btType": "BTCurveGeometryLine-117",
                         "pntX": sx * IN, "pntY": sy * IN,
                         "dirX": (ex - sx) / length, "dirY": (ey - sy) / length},
            "isConstruction": construction,
        })
        return eid

    def add_circle(self, center: Tuple[float, float], radius: float,
                   construction: bool = False) -> str:
        cx, cy = center
        cid = self._id("cir")
        # two semicircle segments form a closed, region-producing circle
        for tag, p0, p1 in (("a", 0.0, math.pi), ("b", math.pi, 2 * math.pi)):
            self.entities.append({
                "btType": "BTMSketchCurveSegment-155", "entityId": f"{cid}.{tag}",
                "startPointId": f"{cid}.{'s' if tag=='a' else 'm'}",
                "endPointId": f"{cid}.{'m' if tag=='a' else 's'}",
                "startParam": p0, "endParam": p1,
                "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": radius * IN,
                             "xCenter": cx * IN, "yCenter": cy * IN,
                             "xDir": 1.0, "yDir": 0.0, "clockwise": False},
                "centerId": f"{cid}.center", "isConstruction": construction,
            })
        for tag, a, b in (("close1", f"{cid}.a.end", f"{cid}.b.start"),
                          ("close2", f"{cid}.b.end", f"{cid}.a.start")):
            self.constraints.append(self._con("COINCIDENT", f"{cid}.{tag}",
                [self._str(a, "localFirst"), self._str(b, "localSecond")]))
        # The two arcs share a center but NOT a radius — without this, a single diameter/radius
        # dimension binds only the .a arc and leaves .b at the placeholder, yielding a lopsided
        # "teardrop" bore (looks like a chamfer, and an oversized loose arc can split a thin wall).
        # EQUAL ties the arcs so one dimension drives the whole circle.
        self.constraints.append(self._con("EQUAL", f"{cid}.req",
            [self._str(f"{cid}.a", "localFirst"), self._str(f"{cid}.b", "localSecond")]))
        return cid

    def add_arc(self, center: Tuple[float, float], start: Tuple[float, float],
                end: Tuple[float, float], construction: bool = False) -> str:
        """A center-point arc swept CCW from `start` to `end`.

        Radius is fixed by `start` (distance center→start); the end point snaps onto that
        radius — standard center-point-arc behavior. The sweep is fully determined by the CCW
        direction, so to get the *complementary* arc you swap `start` and `end` (no separate
        clockwise flag — that keeps us on the one parameterization proven by `add_circle`'s
        semicircles: `clockwise:False`, params increasing). One BTMSketchCurveSegment-155 on a
        circle geometry, spanning a partial angle instead of the full 2π."""
        cx, cy = center; sx, sy = start; ex, ey = end
        r = math.hypot(sx - cx, sy - cy)
        if r == 0:
            raise ValueError("arc start point cannot coincide with the center")
        a0 = math.atan2(sy - cy, sx - cx)
        a1 = math.atan2(ey - cy, ex - cx)
        sweep = (a1 - a0) % (2 * math.pi)
        if sweep == 0:
            raise ValueError("arc start and end span zero angle (use add_circle for a full circle)")
        aid = self._id("arc")
        self.entities.append({
            "btType": "BTMSketchCurveSegment-155", "entityId": aid,
            "startPointId": f"{aid}.start", "endPointId": f"{aid}.end",
            "startParam": a0, "endParam": a0 + sweep,
            "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": r * IN,
                         "xCenter": cx * IN, "yCenter": cy * IN,
                         "xDir": 1.0, "yDir": 0.0, "clockwise": False},
            "centerId": f"{aid}.center", "isConstruction": construction,
        })
        return aid

    def _line_points(self, eid: str):
        """Reconstruct a line's (start, end) in inches from the JSON we emitted."""
        e = next((x for x in self.entities if x.get("entityId") == eid), None)
        if e is None or not e["geometry"]["btType"].startswith("BTCurveGeometryLine"):
            raise ValueError(f"{eid} is not a line in this sketch")
        g = e["geometry"]
        sx, sy = g["pntX"] / IN, g["pntY"] / IN
        L = e["endParam"] / IN
        return e, (sx, sy), (sx + g["dirX"] * L, sy + g["dirY"] * L)

    def _retrim_line_to(self, eid: str, corner_is_start: bool,
                        new_corner: Tuple[float, float], far: Tuple[float, float]):
        """Shorten a line so its corner end lands on the fillet tangent point."""
        e = next(x for x in self.entities if x.get("entityId") == eid)
        g = e["geometry"]
        nx, ny = new_corner; fx, fy = far
        newlen = math.hypot(fx - nx, fy - ny)
        if corner_is_start:                       # start moves to the tangent point; far end stays
            g["pntX"], g["pntY"] = nx * IN, ny * IN
            g["dirX"], g["dirY"] = (fx - nx) / newlen, (fy - ny) / newlen
        # else: end moves inward; start + direction are unchanged, only the length shrinks
        e["endParam"] = newlen * IN

    def add_fillet(self, line1: str, line2: str, radius: float) -> Dict[str, Any]:
        """Round the corner where two lines meet with a tangent arc of `radius`.

        Trims both lines back to their tangent points, drops the old corner coincident (so the
        two ends aren't forced back together), inserts a center-point arc, and adds the
        coincident + tangent constraints that make the fillet parametric. Pure trig — the tangent
        points sit a distance r/tan(θ/2) along each line from the corner; the arc center sits on
        the angle bisector at r/sin(θ/2). Returns the arc id and the computed geometry."""
        _, s1, en1 = self._line_points(line1)
        _, s2, en2 = self._line_points(line2)
        tol = 1e-6
        def close(p, q): return math.hypot(p[0] - q[0], p[1] - q[1]) < tol
        match = next((m for m in (("start", "start", s1, s2), ("start", "end", s1, en2),
                                  ("end", "start", en1, s2), ("end", "end", en1, en2))
                      if close(m[2], m[3])), None)
        if match is None:
            raise ValueError(f"{line1} and {line2} do not share a corner to fillet")
        end1, end2, C, _ = match
        far1 = en1 if end1 == "start" else s1
        far2 = en2 if end2 == "start" else s2
        u1 = (far1[0] - C[0], far1[1] - C[1]); l1 = math.hypot(*u1); u1 = (u1[0] / l1, u1[1] / l1)
        u2 = (far2[0] - C[0], far2[1] - C[1]); l2 = math.hypot(*u2); u2 = (u2[0] / l2, u2[1] / l2)
        dot = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        theta = math.acos(dot)
        if theta < 1e-6 or abs(theta - math.pi) < 1e-6:
            raise ValueError("fillet needs two non-collinear lines meeting at a corner")
        d = radius / math.tan(theta / 2)
        if d >= l1 - 1e-9 or d >= l2 - 1e-9:
            raise ValueError(f"fillet radius {radius} too large to fit "
                             f"(needs {d:.4f} in of run along each line)")
        T1 = (C[0] + u1[0] * d, C[1] + u1[1] * d)
        T2 = (C[0] + u2[0] * d, C[1] + u2[1] * d)
        bis = (u1[0] + u2[0], u1[1] + u2[1]); bl = math.hypot(*bis); bis = (bis[0] / bl, bis[1] / bl)
        h = radius / math.sin(theta / 2)
        O = (C[0] + bis[0] * h, C[1] + bis[1] * h)
        # drop the corner coincident that pinned the two ends together (else it conflicts with the arc)
        corner_pts = {f"{line1}.{end1}", f"{line2}.{end2}"}
        self.constraints = [c for c in self.constraints if not (
            c.get("constraintType") == "COINCIDENT"
            and {p.get("value") for p in c["parameters"] if p["btType"].startswith("BTMParameterString")}
            == corner_pts)]
        self._retrim_line_to(line1, end1 == "start", T1, far1)
        self._retrim_line_to(line2, end2 == "start", T2, far2)
        # order the arc endpoints so the CCW sweep is the minor (fillet) arc
        def ang(p): return math.atan2(p[1] - O[1], p[0] - O[0])
        if (ang(T2) - ang(T1)) % (2 * math.pi) <= math.pi:
            arc = self.add_arc(O, T1, T2)
            self.coincident(f"{line1}.{end1}", f"{arc}.start")
            self.coincident(f"{line2}.{end2}", f"{arc}.end")
        else:
            arc = self.add_arc(O, T2, T1)
            self.coincident(f"{line2}.{end2}", f"{arc}.start")
            self.coincident(f"{line1}.{end1}", f"{arc}.end")
        self.tangent(line1, arc); self.tangent(line2, arc)
        return {"arc": arc, "center": [O[0], O[1]],
                "tangentPoints": [[T1[0], T1[1]], [T2[0], T2[1]]], "radius": radius}

    def add_mirror(self, entity_ids: List[str], axis_line: str) -> Dict[str, str]:
        """Mirror sketch LINES across an existing line entity (the axis).

        Reflects each line's endpoints and emits the copy at the correct mirrored coordinates, so
        the copy is geometrically right on its own; also adds a MIRROR constraint tying copy to
        original so an edit to one propagates. Returns {originalId: copyId}.

        Lines only for now (the common symmetric-profile case); arcs/circles are a follow-up. The
        MIRROR constraint (localFirst/localSecond entities + `local` axis) is LIVE-VERIFIED: it
        regenerates `OK` and produces the correct mirrored solid (scripts/smoke_sketch_mirror.py —
        a half-diamond mirrors to a 2.0 in^3 rhombus, X bbox symmetric [-1,1]). Edit-propagation
        isn't separately exercised, but the constraint is accepted and consistent (no over/under
        -constrained error)."""
        _, p1, p2 = self._line_points(axis_line)
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        dd = dx * dx + dy * dy
        if dd == 0:
            raise ValueError("mirror axis line is degenerate")

        def reflect(q):
            t = ((q[0] - p1[0]) * dx + (q[1] - p1[1]) * dy) / dd
            fx, fy = p1[0] + t * dx, p1[1] + t * dy
            return (2 * fx - q[0], 2 * fy - q[1])

        mapping: Dict[str, str] = {}
        for eid in entity_ids:
            e = next((x for x in self.entities if x.get("entityId") == eid), None)
            if e is None or not e["geometry"]["btType"].startswith("BTCurveGeometryLine"):
                raise ValueError(f"{eid} is not a mirrorable line (lines only for now)")
            _, s, en = self._line_points(eid)
            copy = self.add_line(reflect(s), reflect(en))
            mapping[eid] = copy
            self.constraints.append(self._con("MIRROR", self._id("mir"),
                [self._str(eid, "localFirst"), self._str(copy, "localSecond"),
                 self._str(axis_line, "local")]))
        return mapping

    def add_point(self, at: Tuple[float, float], construction: bool = False) -> str:
        """A standalone sketch point — used as a hole `locations` target (native Hole feature)."""
        px, py = at
        pid = self._id("pt")
        self.entities.append({"btType": "BTMSketchPoint-158", "entityId": pid,
                              "x": px * IN, "y": py * IN, "isConstruction": construction})
        return pid

    def add_slot(self, center1: Tuple[float, float], center2: Tuple[float, float], width: float):
        """An obround (rounded slot) between two centre points: a rotated rectangle plus a circle
        at each end. Extruding the combined regions unions them into a clean slot — robust, built
        from proven primitives (no fragile arc/tangency math). Returns the entity ids."""
        (x1, y1), (x2, y2) = center1, center2
        r = width / 2.0
        th = math.atan2(y2 - y1, x2 - x1)
        px, py = -math.sin(th) * r, math.cos(th) * r      # perpendicular offset, length r
        a1 = (x1 + px, y1 + py); a2 = (x2 + px, y2 + py)
        b2 = (x2 - px, y2 - py); b1 = (x1 - px, y1 - py)
        sides = [self.add_line(a1, a2), self.add_line(a2, b2),
                 self.add_line(b2, b1), self.add_line(b1, a1)]
        for i in range(4):
            self.coincident(f"{sides[i]}.end", f"{sides[(i + 1) % 4]}.start")
        caps = [self.add_circle(center1, r), self.add_circle(center2, r)]
        return {"sides": sides, "caps": caps}

    def add_rectangle(self, corner1, corner2):
        """Convenience: 4 lines + a full geometric-constraint set (origin NOT grounded)."""
        x1, y1 = corner1; x2, y2 = corner2
        b = self.add_line((x1, y1), (x2, y1))
        r = self.add_line((x2, y1), (x2, y2))
        t = self.add_line((x2, y2), (x1, y2))
        l = self.add_line((x1, y2), (x1, y1))
        self.coincident(f"{b}.end", f"{r}.start")
        self.coincident(f"{r}.end", f"{t}.start")
        self.coincident(f"{t}.end", f"{l}.start")
        self.coincident(f"{l}.end", f"{b}.start")
        self.horizontal(b); self.horizontal(t)
        self.vertical(l); self.vertical(r)
        return {"bottom": b, "right": r, "top": t, "left": l}

    # ---- constraint primitives --------------------------------------------
    @staticmethod
    def _str(v, pid): return {"btType": "BTMParameterString-149", "value": v, "parameterId": pid}

    @staticmethod
    def _extq(ids, pid):
        return {"btType": "BTMParameterQueryList-148", "parameterId": pid,
                "queries": [{"btType": "BTMIndividualQuery-138", "deterministicIds": ids}]}

    def _con(self, ctype, eid, params):
        return {"btType": "BTMSketchConstraint-2", "constraintType": ctype,
                "entityId": eid, "parameters": params}

    def coincident(self, point_a: str, point_b: str):
        self.constraints.append(self._con("COINCIDENT", self._id("co"),
            [self._str(point_a, "localFirst"), self._str(point_b, "localSecond")]))

    def horizontal(self, line: str):
        self.constraints.append(self._con("HORIZONTAL", self._id("h"), [self._str(line, "localFirst")]))

    def vertical(self, line: str):
        self.constraints.append(self._con("VERTICAL", self._id("v"), [self._str(line, "localFirst")]))

    def perpendicular(self, l1: str, l2: str):
        self.constraints.append(self._con("PERPENDICULAR", self._id("perp"),
            [self._str(l1, "localFirst"), self._str(l2, "localSecond")]))

    def parallel(self, l1: str, l2: str):
        self.constraints.append(self._con("PARALLEL", self._id("par"),
            [self._str(l1, "localFirst"), self._str(l2, "localSecond")]))

    def equal(self, e1: str, e2: str):
        self.constraints.append(self._con("EQUAL", self._id("eq"),
            [self._str(e1, "localFirst"), self._str(e2, "localSecond")]))

    def concentric(self, a: str, b: str):
        self.constraints.append(self._con("CONCENTRIC", self._id("conc"),
            [self._str(a, "localFirst"), self._str(b, "localSecond")]))

    def tangent(self, a: str, b: str):
        self.constraints.append(self._con("TANGENT", self._id("tan"),
            [self._str(a, "localFirst"), self._str(b, "localSecond")]))

    def midpoint(self, point: str, line: str):
        self.constraints.append(self._con("MIDPOINT", self._id("mid"),
            [self._str(point, "localFirst"), self._str(line, "localSecond")]))

    def symmetric(self, a: str, b: str, symmetry_line: str):
        self.constraints.append(self._con("SYMMETRIC", self._id("sym"),
            [self._str(a, "localFirst"), self._str(b, "localSecond"),
             self._str(symmetry_line, "local")]))

    # full documented binary set, routed generically
    _BINARY = {"coincident": "COINCIDENT", "parallel": "PARALLEL", "perpendicular": "PERPENDICULAR",
               "tangent": "TANGENT", "equal": "EQUAL", "concentric": "CONCENTRIC", "pierce": "PIERCE"}
    _UNARY = {"horizontal": "HORIZONTAL", "vertical": "VERTICAL", "fix": "FIX"}

    def constrain(self, kind: str, a: str, b: Optional[str] = None, c: Optional[str] = None):
        if kind in self._UNARY:
            self.constraints.append(self._con(self._UNARY[kind], self._id("u"), [self._str(a, "localFirst")]))
        elif kind in self._BINARY:
            self.constraints.append(self._con(self._BINARY[kind], self._id("b"),
                [self._str(a, "localFirst"), self._str(b, "localSecond")]))
        elif kind == "midpoint": self.midpoint(a, b)
        elif kind == "symmetric": self.symmetric(a, b, c)
        elif kind == "ground_origin": self.ground_origin(a)
        else: raise ValueError(f"unknown constraint kind {kind}")

    def ground_origin(self, point: str):
        """Coincident a sketch point to the part-studio origin — the idiomatic anchor."""
        self.constraints.append(self._con("COINCIDENT", self._id("ground"),
            [self._extq([ORIGIN_VERTEX], "externalFirst"), self._str(point, "localSecond")]))

    # ---- dimensions (driving) ---------------------------------------------
    def dim_length(self, line: str, value):
        self.constraints.append(self._con("LENGTH", self._id("dlen"), [
            self._str(line, "localFirst"),
            {"btType": "BTMParameterEnum-145", "value": "MINIMUM",
             "enumName": "DimensionDirection", "parameterId": "direction"},
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value),
             "parameterId": "length", "isInteger": False},
            {"btType": "BTMParameterEnum-145", "value": "ALIGNED",
             "enumName": "DimensionAlignment", "parameterId": "alignment"}]))

    def _radius_target(self, ref: str) -> str:
        """The entity a radius/diameter dimension should bind to. A circle is two semicircle
        arcs, so its dimension targets the `.a` sub-arc; a standalone `add_arc` IS the curve, so
        it targets the entity itself. Decided by what actually exists — not by an id prefix."""
        ids = {e["entityId"] for e in self.entities}
        return f"{ref}.a" if f"{ref}.a" in ids else ref

    def dim_radius(self, circle: str, value):
        self.constraints.append(self._con("RADIUS", self._id("drad"), [
            self._str(self._radius_target(circle), "localFirst"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value),
             "parameterId": "radius", "isInteger": False}]))

    def dim_diameter(self, circle: str, value):
        self.constraints.append(self._con("DIAMETER", self._id("ddia"), [
            self._str(self._radius_target(circle), "localFirst"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value),
             "parameterId": "length", "isInteger": False}]))

    def dim_distance(self, a: str, b: str, value):
        self.constraints.append(self._con("DISTANCE", self._id("ddist"), [
            self._str(a, "localFirst"), self._str(b, "localSecond"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value), "parameterId": "length"}]))

    def dim_angle(self, l1: str, l2: str, value):
        v = value if (isinstance(value, str)) else f"{value} deg"
        self.constraints.append(self._con("ANGLE", self._id("dang"), [
            self._str(l1, "localFirst"), self._str(l2, "localSecond"),
            {"btType": "BTMParameterQuantity-147", "expression": v, "parameterId": "angle"}]))

    def dim_distance_to_plane(self, plane: str, line: str, value):
        """DISTANCE from a default plane (external) to a sketch line — positions the sketch."""
        self.constraints.append(self._con("DISTANCE", self._id("ddist"), [
            self._extq([PLANES[plane]], "externalFirst"),
            self._str(line, "localSecond"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value), "parameterId": "length"}]))

    # ---- diagnostics -------------------------------------------------------
    _DIM_TYPES = {"LENGTH", "DISTANCE", "RADIUS", "DIAMETER", "ANGLE"}

    def diagnostics(self) -> Dict[str, Any]:
        """Cheap, local under-definition signals (no API call).

        True 0-DOF detection isn't exposed by the API, but the two failure modes that make a
        sketch wildly under-defined — not grounded, or carrying no driving dimensions — are
        detectable from the constraints we emit. `wellFormed` is False when either is missing.
        """
        dims = sum(1 for c in self.constraints if c.get("constraintType") in self._DIM_TYPES)
        grounded = False
        for c in self.constraints:
            if c.get("constraintType") == "FIX":
                grounded = True
            elif c.get("constraintType") == "COINCIDENT":
                for p in c.get("parameters", []):
                    if p.get("btType", "").startswith("BTMParameterQueryList"):
                        for qy in p.get("queries", []):
                            if ORIGIN_VERTEX in qy.get("deterministicIds", []):
                                grounded = True
        return {"entities": len(self.entities), "dimensions": dims,
                "grounded": grounded, "wellFormed": grounded and dims > 0}

    # ---- emit --------------------------------------------------------------
    def build(self) -> Dict[str, Any]:
        return {"feature": {
            "btType": "BTMSketch-151", "featureType": "newSketch",
            "name": self.name, "suppressed": False,
            "parameters": [{"btType": "BTMParameterQueryList-148",
                            "queries": [{"btType": "BTMIndividualQuery-138",
                                         "deterministicIds": [self.plane_id]}],
                            "parameterId": "sketchPlane"}],
            "entities": self.entities, "constraints": self.constraints,
        }}
