"""Road-network traffic circuits for the Classic DLC streams.

The DLC maps ship no RAIN. Earlier generators traced the outline of each
driveable patch, which put cars against the curb, wrapped them around median
tips and turned them back at every crosswalk notch. Here the street surface is
reduced to a road network instead:

1. Rasterise the car-clearance floor (driveable) and the authored street
   surface at 0.5m. Fill thin enclosed islands (medians, bollards, planters):
   they divide carriageways but are not city blocks.
2. Thin the filled floor to centerlines and build a graph of junctions,
   road ends and road edges. Short skeleton spurs from corner bulges and
   too-narrow passages are removed.
3. Each road edge gets one lane per direction, offset to the right of its
   centerline as in the original Skate 2 RAIN (lanes 4m apart, traffic keeps
   right; a median pushes the lane onto its own carriageway).
4. Lanes are joined with the right-hand face rule: at each junction a car
   takes the right-most continuation. Every lane belongs to exactly one
   circuit, circuits stay inside their own city block's ring and never cross
   one another, so no signals or intersection admission are needed.
   Long dead-end roads turn around with the widest loop that fits.
5. Joins are smooth Hermite turns, chosen as the widest that stays on the
   street surface with the full lane ribbon.

Only geometry is produced here. skate2_dlc_livingworld qualifies circuits
(ribbon support, slope, native normals, walls/ceilings, separation) and
writes the native resource.
"""
from collections import defaultdict, deque

import numpy as np

RES = 0.5               # metres per cell
LANE_OFFSET = 2.0       # original RAIN: opposing lane centres 4m apart
RIBBON = 1.75           # half the native 3.5m lane ribbon
MEDIAN_WIDTH = 12.0     # enclosed islands thinner than this divide a road
MIN_DEAD_END = 100.0    # shorter dead ends are side alleys and parking strips
SPACING = 3.0           # circuit sample spacing (native Hermite controls)
JUNCTION_SETBACK = 10.0 # centerline metres inside a junction, made straight
PINCH_WIDTH = 4.0       # street narrower than this (a barrier gap) is closed to cars
MIN_RADIUS = 6.0        # turns tighter than this are widened where the street allows


class Grid:
    """Minimum floor height per cell for a triangle soup (inf = no floor)."""

    def __init__(self, triangles, origin, shape):
        self.origin, self.shape = origin, shape
        self.y = np.full(shape, np.inf, np.float32)
        for t in np.asarray(triangles, np.float64):
            a, b, c = t
            det = (b[2]-c[2])*(a[0]-c[0])+(c[0]-b[0])*(a[2]-c[2])
            if abs(det) < 1e-7:
                continue
            lo = np.maximum(0, np.ceil((t[:, [0, 2]].min(0)-origin)/RES).astype(int))
            hi = np.minimum(np.array(shape)-1, np.floor((t[:, [0, 2]].max(0)-origin)/RES).astype(int))
            if (hi < lo).any():
                continue
            xx, zz = np.meshgrid(np.arange(lo[0], hi[0]+1), np.arange(lo[1], hi[1]+1), indexing='ij')
            x, z = xx*RES+origin[0], zz*RES+origin[1]
            u = ((b[2]-c[2])*(x-c[0])+(c[0]-b[0])*(z-c[2]))/det
            v = ((c[2]-a[2])*(x-c[0])+(a[0]-c[0])*(z-c[2]))/det
            inside = (u >= -1e-6) & (v >= -1e-6) & (u+v <= 1+1e-6)
            yy = u*a[1]+v*b[1]+(1-u-v)*c[1]
            self.y[xx[inside], zz[inside]] = np.minimum(self.y[xx[inside], zz[inside]], yy[inside])

    @property
    def mask(self):
        return np.isfinite(self.y)

    def cell(self, xz):
        return np.rint((np.asarray(xz)-self.origin)/RES).astype(int)

    def world(self, ij):
        return np.asarray(ij)*RES+self.origin

    def sample(self, xz, mask=None):
        """Height at world xz (inf outside); optional boolean mask."""
        ij = self.cell(xz)
        ok = (ij >= 0).all(-1) & (ij < np.array(self.shape)).all(-1)
        out = np.full(ij.shape[:-1], np.inf)
        y = self.y if mask is None else np.where(mask, self.y, np.inf)
        out[ok] = y[ij[ok][:, 0], ij[ok][:, 1]]
        return out


def label(mask):
    """4-connected component labels via run-length union-find (no SciPy)."""
    parent = [0]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs_prev = []
    labels = np.zeros(mask.shape, np.int32)
    for i in range(mask.shape[0]):
        row = mask[i].astype(np.int8)
        edges = np.flatnonzero(np.diff(np.r_[0, row, 0]))
        runs = []
        for s, e in zip(edges[::2], edges[1::2]):
            lab = None
            for ps, pe, pl in runs_prev:
                if ps < e and pe > s:
                    if lab is None:
                        lab = find(pl)
                    else:
                        a, b = find(lab), find(pl)
                        if a != b:
                            parent[max(a, b)] = min(a, b)
                            lab = min(a, b)
            if lab is None:
                lab = len(parent)
                parent.append(lab)
            runs.append((s, e, lab))
            labels[i, s:e] = lab
        runs_prev = runs
    roots = np.array([find(a) for a in range(len(parent))], np.int32)
    return roots[labels]


def shift_and(mask, steps):
    out = mask.copy()
    for _ in range(steps):
        p = np.pad(out, 1)
        out = out & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return out


def shift_or(mask, steps):
    out = mask.copy()
    for _ in range(steps):
        p = np.pad(out, 1)
        out = out | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    return out


def fill_islands(floor):
    """Fill enclosed islands thinner than MEDIAN_WIDTH (medians, obstacles)."""
    holes = label(~floor)
    border = set(np.unique(np.r_[holes[0], holes[-1], holes[:, 0], holes[:, -1]]).tolist())
    # An island survives erosion by half the median width only if it is a block.
    thick = shift_and(~floor, int(MEDIAN_WIDTH/2/RES))
    keep = set(np.unique(holes[thick]).tolist()) | border
    ids = np.unique(holes[~floor])
    filled = floor.copy()
    for h in ids:
        if h not in keep:
            filled[holes == h] = True
    return filled


def skeleton(mask):
    """Zhang-Suen thinning."""
    m = np.pad(mask, 1).astype(bool)
    while True:
        changed = False
        for step in (0, 1):
            p = [m[:-2, 1:-1], m[:-2, 2:], m[1:-1, 2:], m[2:, 2:],
                 m[2:, 1:-1], m[2:, :-2], m[1:-1, :-2], m[:-2, :-2]]
            n = sum(a.astype(np.uint8) for a in p)
            t = sum((~p[i] & p[(i+1) % 8]).astype(np.uint8) for i in range(8))
            a, b, c, d = p[0], p[2], p[4], p[6]
            cond = (~(a & b & c) & ~(b & c & d)) if step == 0 else (~(a & b & d) & ~(a & c & d))
            remove = m[1:-1, 1:-1] & (n >= 2) & (n <= 6) & (t == 1) & cond
            if remove.any():
                changed = True
                m[1:-1, 1:-1][remove] = False
        if not changed:
            return m[1:-1, 1:-1]


NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))


def pixel_graph(skel):
    """m-adjacency: a diagonal link only where no 4-connected path exists."""
    pts = set(map(tuple, np.argwhere(skel)))
    adj = {}
    for p in pts:
        n = []
        for dx, dz in NEIGHBORS:
            q = (p[0]+dx, p[1]+dz)
            if q not in pts:
                continue
            if dx and dz and ((p[0]+dx, p[1]) in pts or (p[0], p[1]+dz) in pts):
                continue
            n.append(q)
        adj[p] = n
    return adj


def trace_edges(adj):
    """Chains between junction/end pixels; adjacent junction pixels merge."""
    special = {p for p, n in adj.items() if len(n) != 2}
    cluster = {}
    for p in sorted(special):
        if p in cluster:
            continue
        cluster[p] = p
        if len(adj[p]) < 3:
            continue
        stack = [p]
        while stack:
            q = stack.pop()
            for r in adj[q]:
                if r in special and len(adj[r]) >= 3 and r not in cluster:
                    cluster[r] = p
                    stack.append(r)
    visited, edges, pairs = set(), [], set()
    for p in sorted(special):
        for q in adj[p]:
            if q in special:
                key = tuple(sorted((cluster[p], cluster[q])))
                if cluster[p] != cluster[q] and key not in pairs:
                    pairs.add(key)
                    edges.append((cluster[p], cluster[q], [p, q]))
                continue
            if q in visited:
                continue
            chain, prev, cur = [p, q], p, q
            visited.add(q)
            end = None
            while True:
                nxt = [r for r in adj[cur] if r != prev]
                if not nxt:
                    break
                r = nxt[0]
                chain.append(r)
                if r in special:
                    end = cluster[r]
                    break
                if r in visited:
                    break
                visited.add(r)
                prev, cur = cur, r
            if end is not None:
                edges.append((cluster[p], end, chain))
    # A road that closes on itself without any junction or end.
    rest = {p for p in adj if p not in special and p not in visited}
    while rest:
        start = min(rest)
        rest.discard(start)
        chain, prev, cur = [start], None, start
        while True:
            nxt = [r for r in adj[cur] if r != prev]
            if not nxt or nxt[0] == start or nxt[0] not in rest:
                break
            prev, cur = cur, nxt[0]
            rest.discard(cur)
            chain.append(cur)
        if len(chain)*RES >= 40.:
            edges.append((start, start, chain+[start]))
    return edges


def prune_spurs(skel, length):
    """Remove skeleton branches that end in free space within length."""
    skel = skel.copy()
    limit = int(length/RES)
    while True:
        adj = pixel_graph(skel)
        removed = False
        for p, n in adj.items():
            if len(n) != 1 or not skel[p]:
                continue
            chain, prev, cur = [p], None, p
            while len(chain) <= limit:
                nxt = [r for r in adj[cur] if r != prev]
                if len(nxt) != 1:
                    break
                prev, cur = cur, nxt[0]
                if len(adj[cur]) != 2:
                    break
                chain.append(cur)
            if len(chain) <= limit and len(adj[cur]) >= 3:
                for q in chain:
                    skel[q] = False
                removed = True
        if not removed:
            return skel


# ---------------------------------------------------------------- floor ----

def obstacle_cells(mesh, floor_y, origin, shape):
    """Cells holding a wall/post face between 0.15m and 2.5m above the floor."""
    normal = np.cross(mesh[:, 1]-mesh[:, 0], mesh[:, 2]-mesh[:, 0])
    length = np.linalg.norm(normal, axis=1)
    steep = mesh[(length > 1e-9) & (np.abs(normal[:, 1]) < .7*length)]
    hi = origin+np.array(shape)*RES
    keep = ((steep[:, :, 0].max(1) >= origin[0]) & (steep[:, :, 0].min(1) <= hi[0]) &
            (steep[:, :, 2].max(1) >= origin[1]) & (steep[:, :, 2].min(1) <= hi[1]))
    out = np.zeros(shape, bool)
    for t in steep[keep].astype(np.float64):
        span = max(np.linalg.norm(t[1]-t[0]), np.linalg.norm(t[2]-t[0]), np.linalg.norm(t[2]-t[1]))
        n = int(np.ceil(span/(RES/2)))+1
        u, v = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n), indexing='ij')
        inside = u+v <= 1
        p = t[0]+u[inside, None]*(t[1]-t[0])+v[inside, None]*(t[2]-t[0])
        ij = np.rint((p[:, [0, 2]]-origin)/RES).astype(int)
        ok = (ij >= 0).all(1) & (ij < np.array(shape)).all(1)
        ij, p = ij[ok], p[ok]
        if not len(ij):
            continue
        y = floor_y[ij[:, 0], ij[:, 1]]
        hit = np.isfinite(y) & (p[:, 1] > y+.15) & (p[:, 1] < y+2.5)
        out[ij[hit, 0], ij[hit, 1]] = True
    return out


class Floor:
    """Car floor: the clearance survey, regrown across the street it lost.

    The vehicle clearance survey drops strips at mesh seams and along curbs,
    so the floor grows back up to 4m over authored street surface: only at a
    continuous height (never onto an overpass or a lower road), and never onto
    or beside an obstacle face. Obstacles are the collision that `visible`
    selects: drawn walls and barriers (the jersey barriers that close the DLC
    streets carry the same surface tag 0 as undrawn skater-only faces).
    """

    def __init__(self, street_tris, driveable_tris, mesh, visible=None):
        lo = np.floor(street_tris.min(axis=(0, 1))[[0, 2]])-12
        hi = np.ceil(street_tris.max(axis=(0, 1))[[0, 2]])+12
        shape = tuple(int(v) for v in ((hi-lo)/RES).astype(int)+1)
        if np.prod(shape) > 16_000_000:
            raise ValueError('DLC road survey exceeds bounded raster')
        np.seterr(invalid='ignore')
        self.origin, self.shape = lo, shape
        self.street = Grid(street_tris, lo, shape)
        self.drive = Grid(driveable_tris, lo, shape)
        base = self.drive.mask
        solid = mesh if visible is None else mesh[visible]
        self.obstacle = obstacle_cells(solid, np.where(base, self.drive.y, self.street.y), lo, shape)
        allowed = self.street.mask & ~shift_or(self.obstacle, 1)
        mask, y = base.copy(), np.where(base, self.drive.y, np.inf).astype(np.float32)
        for _ in range(int(4./RES)):
            grown = np.zeros(shape, bool)
            for axis, step in ((0, 1), (0, -1), (1, 1), (1, -1)):
                ny = np.roll(y, step, axis=axis)
                edge = [slice(None)]*2
                edge[axis] = slice(0, 1) if step > 0 else slice(-1, None)
                ny[tuple(edge)] = np.inf
                cand = allowed & ~mask & ~grown & np.isfinite(ny) & (np.abs(self.street.y-ny) < .3)
                grown |= cand
            y = np.where(grown, self.street.y, y)
            mask |= grown
        # Survey polygons only approximate the floor (up to ~0.5m low); lanes
        # sit on the authored street surface wherever there is one.
        self.mask = mask
        self.y = np.where(mask & self.street.mask, self.street.y, y).astype(np.float32)
        self.seam_cells = int((mask & ~base).sum())

    def cell(self, xz):
        return np.rint((np.asarray(xz, float)-self.origin)/RES).astype(int)

    def world(self, ij):
        return np.asarray(ij, float)*RES+self.origin

    def lookup(self, grid, xz):
        ij = self.cell(xz)
        ok = (ij >= 0).all(-1) & (ij < np.array(self.shape)).all(-1)
        out = np.zeros(ij.shape[:-1], bool) if grid.dtype == bool else np.full(ij.shape[:-1], np.inf)
        out[ok] = grid[ij[ok][..., 0], ij[ok][..., 1]]
        return out


class FloorRaster:
    """The skate2_dlc_livingworld raster interface over a 0.5m grid."""

    def __init__(self, floor, grid):
        self.floor, self.grid = floor, grid

    def height(self, points):
        return self.floor.lookup(self.grid, np.asarray(points, float)[..., [0, 2]])


# ---------------------------------------------------------------- graph ----

def chain_length(points):
    return float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())


def resample(points, step, closed=False):
    pts = np.vstack((points, points[:1])) if closed else np.asarray(points)
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    total = arc[-1]
    if total < 1e-6:
        return pts[:1].copy()
    n = max(3, int(round(total/step)))
    at = np.linspace(0, total, n, endpoint=False) if closed else np.linspace(0, total, n+1)
    return np.column_stack([np.interp(at, arc, pts[:, k]) for k in range(pts.shape[1])])


def smooth_open(points, passes):
    p = points.copy()
    for _ in range(passes):
        p[1:-1] = (p[:-2]+2*p[1:-1]+p[2:])/4
    return p


def right_of(direction):
    """Right-hand side in the native frame (left = up x forward)."""
    return np.stack((-direction[..., 1], direction[..., 0]), -1)


def unit(v):
    return v/np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


class Network:
    """Junctions, road ends and centerline road edges of the street floor."""

    def __init__(self, floor):
        self.floor = floor
        self.filled = fill_islands(floor.mask)
        components = label(self.filled)
        skel = prune_spurs(skeleton(self.filled), 14.)
        adj = pixel_graph(skel)
        raw = trace_edges(adj)
        points = {}
        for p in adj:
            points[p] = floor.world(np.array(p))
        # Contract short junction-to-junction links into one junction.
        degree = defaultdict(int)
        for a, b, _ in raw:
            degree[a] += 1
            degree[b] += 1
        parent = {}

        def find(k):
            parent.setdefault(k, k)
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k
        for a, b, chain in raw:
            if a != b and degree[a] > 1 and degree[b] > 1 and len(chain)*RES < 12.:
                parent[find(a)] = find(b)
        members = defaultdict(list)
        for a, b, chain in raw:
            for k in (a, b):
                members[find(k)].append(points[k])
        self.nodes = {k: np.mean(v, axis=0) for k, v in members.items()}
        self.edges = []
        for a, b, chain in raw:
            a2, b2 = find(a), find(b)
            pts = floor.world(np.array(chain))
            if a2 == b2 and (a != b or len(chain)*RES < 40.):
                continue  # Contracted link or a tiny loop inside a junction.
            pts = resample(np.vstack((self.nodes[a2], pts, self.nodes[b2])), 1.)
            pts = smooth_open(pts, 40)
            mid = tuple(floor.cell(pts[len(pts)//2]))
            self.edges.append(dict(a=a2, b=b2, center=pts, length=chain_length(pts),
                                   component=int(components[mid])))
        total = defaultdict(float)
        for e in self.edges:
            total[e['component']] += e['length']
        # Unconnected lots and plazas are not part of the street network.
        self.dropped = {c: round(t, 1) for c, t in total.items() if t < 250.}
        self.edges = [e for e in self.edges if e['component'] not in self.dropped]
        self.narrow = []

    def degree(self):
        d = defaultdict(int)
        for e in self.edges:
            d[e['a']] += 1
            d[e['b']] += 1
        return d

    def split_pinches(self, floor, width=PINCH_WIDTH, ends=6.):
        """Close a road where its floor narrows below one car ribbon.

        A barrier that closes one arm of a junction can leave a gap of a few
        metres against the curb, so the centerline runs on through it. The
        road is cut there and each side becomes a dead end. A centerline off
        the floor (on a median) is never a pinch.
        """
        steps = np.arange(0, 8.01, .25)
        kept, serial = [], 0
        for e in self.edges:
            c = e['center']
            t = unit(np.gradient(c, axis=0))
            r = right_of(t)
            station = np.r_[0, np.cumsum(np.linalg.norm(np.diff(c, axis=0), axis=1))]
            on = floor.lookup(floor.mask, c)
            span = np.zeros(len(c))
            for side in (1, -1):
                probe = floor.lookup(floor.mask, c[:, None]+side*r[:, None]*steps[None, :, None])
                # A 0.25m speckle is not an edge: the floor ends at two misses in a row.
                end = ~probe[:, :-1] & ~probe[:, 1:]
                span += np.where(end.any(1), np.argmax(end, axis=1)*.25, steps[-1])
            pinch = on & (span < width) & (station > ends) & (station < station[-1]-ends)
            if not pinch.any():
                kept.append(e)
                continue
            idx = np.flatnonzero(pinch)
            runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1)+1)
            cuts = [(station[run[0]]-2., station[run[-1]]+2.) for run in runs]
            start, a = 0., e['a']
            for lo, hi in cuts+[(station[-1], None)]:
                if hi is None:
                    b = e['b']
                else:
                    b = ('pinch', id(self), serial)
                    serial += 1
                piece = c[(station >= start) & (station <= lo)]
                if len(piece) >= 2:
                    if b not in self.nodes:
                        self.nodes[b] = piece[-1]
                    if a not in self.nodes:
                        self.nodes[a] = piece[0]
                    kept.append(dict(e, a=a, b=b, center=piece, length=chain_length(piece)))
                if hi is not None:
                    a = ('pinch', id(self), serial)
                    serial += 1
                    start = hi
                    self.nodes[a] = c[np.searchsorted(station, hi).clip(0, len(c)-1)]
        self.edges = kept

    def merge_through(self):
        """Join the two roads at a former junction that lost its side road.

        Only where the road carries straight on (within 30 degrees). A sharp
        corner stays a node, so its turn is built and checked like any
        junction turn instead of a centerline cutting across the curb.
        """
        corners = set()
        while True:
            d = self.degree()
            node = next((k for k, n in d.items() if n == 2 and k not in corners and
                         not any(e['a'] == e['b'] == k for e in self.edges)), None)
            if node is None:
                return
            e1, e2 = [e for e in self.edges if node in (e['a'], e['b'])]
            c1 = e1['center'] if e1['b'] == node else e1['center'][::-1]
            a = e1['a'] if e1['b'] == node else e1['b']
            c2 = e2['center'] if e2['a'] == node else e2['center'][::-1]
            b = e2['b'] if e2['a'] == node else e2['a']

            def heading(c):
                st = np.r_[0, np.cumsum(np.linalg.norm(np.diff(c, axis=0), axis=1))]
                i0 = min(int(np.searchsorted(st, JUNCTION_SETBACK)), len(c)-1)
                i1 = min(int(np.searchsorted(st, JUNCTION_SETBACK+12.)), len(c)-1)
                return unit(c[i1]-c[i0]) if i1 > i0 else unit(c[-1]-c[0])
            arrive, leave = -heading(c1[::-1]), heading(c2)
            if arrive@leave < np.cos(np.radians(30)):
                corners.add(node)
                continue
            # The old junction pulled both ends toward its centre: drop those
            # metres and let the smoothing bridge a straight continuation.
            def trim(c, from_end):
                st = np.r_[0, np.cumsum(np.linalg.norm(np.diff(c, axis=0), axis=1))]
                if from_end:
                    keep = st <= st[-1]-JUNCTION_SETBACK
                else:
                    keep = st >= JUNCTION_SETBACK
                return c[keep] if keep.sum() >= 2 else c
            pts = resample(np.vstack((trim(c1, True), trim(c2, False))), 1.)
            pts = smooth_open(pts, 40)
            self.edges = [e for e in self.edges if e is not e1 and e is not e2]
            self.edges.append(dict(a=a, b=b, center=pts, length=chain_length(pts),
                                   component=e1['component']))

    def drop_short_dead_ends(self, minimum):
        """Side alleys are not traffic roads; long dead ends turn around."""
        self.alleys = []
        while True:
            d = self.degree()
            keep = []
            for e in self.edges:
                if (d[e['a']] == 1 or d[e['b']] == 1) and e['length'] < minimum:
                    self.alleys.append(e)
                else:
                    keep.append(e)
            if len(keep) == len(self.edges):
                self.merge_through()
                return
            self.edges = keep


# ---------------------------------------------------------------- lanes ----

def side_offsets(floor, center, skip_a, skip_b):
    """Right-lane offset along a centerline (positive = right of travel).

    Casts across the floor to the first car band on that side (beyond any
    median) and keeps the lane LANE_OFFSET..2.5m into it, like the original
    RAIN's 4m lane spacing; never closer than the ribbon fits. Junction ends
    are excluded from the estimate and filled from the road interior.
    """
    t = unit(np.gradient(center, axis=0))
    r = right_of(t)
    steps = np.arange(0, 16.01, .25)
    probe = center[:, None, :]+r[:, None]*steps[None, :, None]
    on = floor.lookup(floor.mask, probe)
    station = np.r_[0, np.cumsum(np.linalg.norm(np.diff(center, axis=0), axis=1))]
    d = np.full(len(center), np.nan)
    band = np.full(len(center), np.nan)
    far = np.full(len(center), np.nan)
    for i in range(len(center)):
        if station[i] < skip_a or station[-1]-station[i] < skip_b:
            continue
        row = on[i]
        start = np.argmax(row)
        if not row[start] or steps[start] > 8:
            continue
        run = row[start:]
        stop = len(run)
        gaps = np.flatnonzero(~run)
        for g in gaps:
            if g+1 < len(run) and run[g+1]:
                continue  # 0.25m speckle
            stop = g
            break
        a, b = steps[start], steps[start]+stop*.25
        band[i], far[i] = b-a, b
        d[i] = a+np.clip((b-a)/2, LANE_OFFSET, 2.5)
    return d, band, station, far


def fill_profile(values, station):
    ok = np.isfinite(values)
    if not ok.any():
        return None
    v = np.interp(station, station[ok], values[ok])
    # Robust: a 21m running median removes driveway mouths and bus bays.
    half = 10
    med = np.array([np.median(v[max(0, i-half):i+half+1]) for i in range(len(v))])
    # Shift lanes gradually (<=0.15m per metre) and early enough before a
    # median nose: the envelope rises ahead of every required offset.
    env = med.copy()
    for i in range(1, len(env)):
        env[i] = max(env[i], env[i-1]-.15*(station[i]-station[i-1]))
    for i in range(len(env)-2, -1, -1):
        env[i] = max(env[i], env[i+1]-.15*(station[i+1]-station[i]))
    for _ in range(6):
        env[1:-1] = np.maximum(env[1:-1], (env[:-2]+2*env[1:-1]+env[2:])/4)
    return env


def straight_approach(center, setback):
    """Replace the first setback metres of a centerline with a straight line.

    The thinned centerline bends toward the middle of every junction. Those
    metres lie inside the junction, where lanes are replaced by turns anyway;
    a straight approach along the road's own direction keeps through lanes
    from bulging toward a side street.
    """
    station = np.r_[0, np.cumsum(np.linalg.norm(np.diff(center, axis=0), axis=1))]
    if station[-1] < setback*2.5:
        return center
    k = int(np.searchsorted(station, setback))
    k2 = int(np.searchsorted(station, setback+8.))
    direction = unit(center[k2]-center[k])
    out = center.copy()
    out[:k] = center[k]-direction*(station[k]-station[:k])[:, None]
    return out


def fit_clearance(floor, xz, clearance=RIBBON+.3):
    """Keep a lane's ribbon off a curb or median nose next to it.

    Measures the free street on either side of every lane point. Where one
    side is closer than the ribbon needs and the other has room, the lane
    shifts across by the deficit; shifts taper in and out at 0.15m per metre.
    """
    t = unit(np.gradient(xz, axis=0))
    r = right_of(t)
    y = floor.lookup(floor.y, xz)
    y = np.where(np.isfinite(y), y, floor.lookup(floor.street.y, xz))
    steps = np.arange(.25, 6.01, .25)

    def free(sign):
        probe = xz[:, None]+sign*r[:, None]*steps[None, :, None]
        ys = np.where(floor.lookup(floor.mask, probe), floor.lookup(floor.y, probe),
                      floor.lookup(floor.street.y, probe))
        ok = np.isfinite(ys) & (np.abs(ys-y[:, None]) <= .6) & ~floor.lookup(floor.obstacle, probe)
        first = np.where(ok.all(1), len(steps), np.argmin(ok, axis=1))
        return np.where(first > 0, steps[np.maximum(first-1, 0)], 0.)
    left, right = free(-1.), free(1.)
    shift = np.where((left < clearance) & (right > clearance), np.minimum(clearance-left, right-clearance), 0.)
    shift -= np.where((right < clearance) & (left > clearance), np.minimum(clearance-right, left-clearance), 0.)
    station = np.r_[0, np.cumsum(np.linalg.norm(np.diff(xz, axis=0), axis=1))]
    env = shift.copy()
    for order in (range(1, len(env)), range(len(env)-2, -1, -1)):
        for i in order:
            j = i-1 if order.step == 1 else i+1
            slack = .15*abs(station[i]-station[j])
            if abs(env[j])-slack > abs(env[i]):
                env[i] = np.sign(env[j])*(abs(env[j])-slack)
    return xz+r*env[:, None]


class Lane:
    def __init__(self, edge, sign, offsets):
        c = edge['center'] if sign > 0 else edge['center'][::-1]
        o = offsets if sign > 0 else offsets[::-1]
        t = unit(np.gradient(c, axis=0))
        self.center = c
        self.xz = fit_clearance(edge['floor'], smooth_open(c+right_of(t)*o[:, None], 12))
        self.offset = o
        self.station = np.r_[0, np.cumsum(np.linalg.norm(np.diff(c, axis=0), axis=1))]
        self.length = self.station[-1]
        self.edge, self.sign = edge, sign
        self.start = edge['a'] if sign > 0 else edge['b']
        self.end = edge['b'] if sign > 0 else edge['a']

    def departure(self, distance=12.):
        """Direction leaving the start node."""
        k = min(np.searchsorted(self.station, min(distance, self.length*.5)), len(self.center)-1)
        return unit(self.center[k]-self.center[0])

    def arrival(self, distance=12.):
        """Heading entering the end node."""
        k = max(np.searchsorted(self.station, self.length-min(distance, self.length*.5))-1, 0)
        return unit(self.center[-1]-self.center[k])

    def at(self, s):
        """Position and heading at station s."""
        x = np.interp(s, self.station, self.xz[:, 0])
        z = np.interp(s, self.station, self.xz[:, 1])
        h = 1.
        p0 = np.array([np.interp(max(s-h, 0), self.station, self.xz[:, k]) for k in (0, 1)])
        p1 = np.array([np.interp(min(s+h, self.length), self.station, self.xz[:, k]) for k in (0, 1)])
        return np.array([x, z]), unit(p1-p0)

    def piece(self, s0, s1):
        keep = (self.station > s0) & (self.station < s1)
        a, _ = self.at(s0)
        b, _ = self.at(s1)
        return np.vstack((a, self.xz[keep], b))


def hermite(p0, t0, p1, t1, n):
    s = np.linspace(0, 1, n)[:, None]
    return ((2*s**3-3*s**2+1)*p0+(s**3-2*s**2+s)*t0+(-2*s**3+3*s**2)*p1+(s**3-s**2)*t1)


def ribbon_ok(floor, xz, clearance=RIBBON+.3):
    """Center on the car floor; both ribbon edges on street at its height."""
    if len(xz) < 2:
        return False
    t = unit(np.gradient(xz, axis=0))
    y = floor.lookup(floor.y, xz)
    if not np.isfinite(y).all():
        return False
    for side in (-clearance, clearance):
        e = xz+right_of(t)*side
        ys = np.where(floor.lookup(floor.mask, e), floor.lookup(floor.y, e), floor.lookup(floor.street.y, e))
        if not np.isfinite(ys).all() or (np.abs(ys-y) > .6).any():
            return False
        if floor.lookup(floor.obstacle, e).any():
            return False
    return not floor.lookup(floor.obstacle, xz).any()


def min_radius(xz):
    d = np.diff(xz, axis=0)
    h = np.arctan2(d[:, 0], d[:, 1])
    turn = np.abs((np.diff(h)+np.pi) % (2*np.pi)-np.pi)
    ds = np.linalg.norm(d, axis=1)[1:]
    return float(np.min(ds/np.maximum(turn, 1e-9))) if len(turn) else np.inf


def corner_arc(lin, lout, s, sign):
    """Lane arc round a two-road corner, offset from one shared centerline arc.

    sign +1 builds lin -> lout; sign -1 builds the opposite direction using
    the same centerline arc, so both lanes of a corner stay parallel.
    """
    def centre(lane, station):
        c = np.array([np.interp(station, lane.station, lane.center[:, k]) for k in (0, 1)])
        lo = np.array([np.interp(max(station-1, 0), lane.station, lane.center[:, k]) for k in (0, 1)])
        hi = np.array([np.interp(min(station+1, lane.length), lane.station, lane.center[:, k]) for k in (0, 1)])
        return c, unit(hi-lo), float(np.interp(station, lane.station, lane.offset))
    c0, t0, o0 = centre(lin, lin.length-s)
    c1, t1, o1 = centre(lout, s)
    chord = np.linalg.norm(c1-c0)
    angle = np.arccos(np.clip(t0@t1, -1, 1))
    k = chord*(1+.3*angle/(np.pi/2))
    n = max(8, int(chord/.5))
    u = np.linspace(0, 1, n)[:, None]
    arc = hermite(c0, t0*k, c1, t1*k, n)
    tangent = unit((6*u**2-6*u)*c0+(3*u**2-4*u+1)*t0*k+(-6*u**2+6*u)*c1+(3*u**2-2*u)*t1*k)
    h = u*u*(3-2*u)
    if sign > 0:
        lane = arc+right_of(tangent)*(o0*(1-h)+o1*h)
        a, b = lin.at(lin.length-s)[0], lout.at(s)[0]
    else:
        tw_in, tw_out = lout.twin, lin.twin
        p0 = float(np.interp(tw_in.length-s, tw_in.station, tw_in.offset))
        p1 = float(np.interp(s, tw_out.station, tw_out.offset))
        arc, tangent, h = arc[::-1], -tangent[::-1], h[::-1]
        lane = arc+right_of(tangent)*(p0*h+p1*(1-h))
        a, b = tw_in.at(tw_in.length-s)[0], tw_out.at(s)[0]
    # Blend the small residual to the smoothed lane polylines at both ends.
    w = np.linspace(0, 1, n)[:, None]
    w = w*w*(3-2*w)
    return lane+(a-lane[0])*(1-w)+(b-lane[-1])*w


def corner_turn(lin, lout, node_radius, cache):
    """Turn at a two-road corner: the widest setback where both lanes fit."""
    key = frozenset((id(lin), id(lout), id(lout.twin), id(lin.twin)))
    if key not in cache:
        floor = lin.edge['floor']
        chosen = None
        top = min(lin.length*.45, lout.length*.45, 32.)
        for s in np.arange(top, max(node_radius, 2.)-.01, -1.):
            a = corner_arc(lin, lout, s, +1)
            b = corner_arc(lin, lout, s, -1)
            if min_radius(a) < 3.5 or min_radius(b) < 3.5:
                continue
            if not (ribbon_ok(floor, a) and ribbon_ok(floor, b)):
                continue
            gap = np.linalg.norm(a[:, None]-b[None], axis=2).min()
            if gap < 3.6:
                continue
            chosen = s
            break
        cache[key] = (chosen, lin)
    s, first = cache[key]
    if s is None:
        # No shared arc fits (a bend left by a barrier across one arm of a
        # junction): each direction takes its own widest junction join.
        return junction_turn(lin, lout, node_radius)
    return s, s, corner_arc(lin, lout, s, +1 if first is lin else +1)


def junction_turn(lin, lout, node_radius):
    """Widest smooth Hermite join from lin's end to lout's start."""
    best = None
    top = min(lin.length*.45, lout.length*.45, 32.)
    for s in np.arange(top, node_radius-.01, -1.):
        p0, t0 = lin.at(lin.length-s)
        p1, t1 = lout.at(s)
        chord = np.linalg.norm(p1-p0)
        if chord < .5:
            continue
        angle = np.arccos(np.clip(t0@t1, -1, 1))
        if angle > np.radians(150):
            continue  # Never a U-turn inside a junction.
        k = chord*(1+.3*angle/(np.pi/2))
        curve = hermite(p0, t0*k, p1, t1*k, max(8, int(chord/.5)))
        if min_radius(curve) < 4.:
            continue
        if ribbon_ok(lin.edge['floor'], curve):
            return s, s, curve
        if best is None:
            best = (s, s, curve)
    if best is None:
        s = max(node_radius, 1.)
        p0, t0 = lin.at(max(lin.length-s, 0))
        p1, t1 = lout.at(min(s, lout.length))
        chord = max(np.linalg.norm(p1-p0), .5)
        best = (s, s, hermite(p0, t0*chord, p1, t1*chord, 8))
    return best[0], best[1], None if best is None else best[2]


def dead_end_turn(lin, lout):
    """Widest turnaround loop that fits at a dead end.

    Leaves the right lane, swings round a circle centred on the centerline
    as far into the dead end as the street allows, and returns on the
    opposite lane. Radius from 7m down to the lane offset.
    """
    floor = lin.edge['floor']
    c = lin.center
    st = lin.station
    L = lin.length
    for radius in np.arange(7., LANE_OFFSET-.01, -.5):
        for back in np.arange(-6., min(40., L*.45), 1.):
            s = L-back
            if s > L:
                ce = c[-1]+unit(c[-1]-c[-2])*(s-L)
                t = unit(c[-1]-c[-2])
            else:
                ce = np.array([np.interp(s, st, c[:, k]) for k in (0, 1)])
                k0 = max(np.searchsorted(st, s-1.)-1, 0)
                k1 = min(np.searchsorted(st, s+1.), len(c)-1)
                t = unit(c[k1]-c[k0])
            r = right_of(t)
            arc = np.linspace(0, np.pi, 24)
            loop = ce+radius*(np.cos(arc)[:, None]*r+np.sin(arc)[:, None]*t)
            approach = max(10., 3*abs(radius-lin.offset[-1]))
            sin_ = min(s, L)-approach
            if sin_ < lin.length*.3:
                continue
            p0, t0 = lin.at(sin_)
            q1, u1 = lout.at(lout.length-sin_)
            entry = hermite(p0, t0*approach, loop[0], t*approach, 12)
            exit_ = hermite(loop[-1], -t*approach, lout.at(L-sin_)[0], lout.at(L-sin_)[1]*approach, 12)
            curve = np.vstack((entry[:-1], loop, exit_[1:]))
            if ribbon_ok(floor, curve):
                return sin_, L-sin_, curve, radius
    return None


# --------------------------------------------------------------- circuits ---

def circuit_radius(xz, span=2):
    """Turn radius along a closed ~1m polyline (circumradius over +-span)."""
    a, c = np.roll(xz, span, axis=0), np.roll(xz, -span, axis=0)
    ab, bc, ca = xz-a, c-xz, a-c
    cross = np.abs(ab[:, 0]*bc[:, 1]-ab[:, 1]*bc[:, 0])
    la, lb, lc = (np.linalg.norm(v, axis=1) for v in (ab, bc, ca))
    return la*lb*lc/np.maximum(2*cross, 1e-9)


def ribbon_points(floor, xz, clearance=RIBBON+.3):
    """Per-point ribbon support on a closed polyline (see ribbon_ok).

    Checked at three intermediate positions per segment too: a square curb
    corner can reach between two controls. A point fails if either of its
    segments does.
    """
    nxt = np.roll(xz, -1, axis=0)
    dense = (xz[:, None]+np.linspace(0, 1, 4, endpoint=False)[None, :, None]*(nxt-xz)[:, None]).reshape(-1, 2)
    ok = _ribbon_dense(floor, dense, clearance).reshape(len(xz), 4).all(1)
    return ok & np.roll(ok, 1)


def side_points(floor, xz, side):
    """Per-point support of one ribbon edge (side metres to the right)."""
    nxt = np.roll(xz, -1, axis=0)
    dense = (xz[:, None]+np.linspace(0, 1, 4, endpoint=False)[None, :, None]*(nxt-xz)[:, None]).reshape(-1, 2)
    # Native ribbons use each segment's chord direction, not a smoothed one.
    t = unit(np.roll(dense, -1, axis=0)-dense)
    y = floor.lookup(floor.y, dense)
    y = np.where(np.isfinite(y), y, floor.lookup(floor.street.y, dense))
    e = dense+right_of(t)*side
    ys = np.where(floor.lookup(floor.mask, e), floor.lookup(floor.y, e), floor.lookup(floor.street.y, e))
    ok = (np.isfinite(ys) & (np.abs(ys-y) <= .6) & ~floor.lookup(floor.obstacle, e)).reshape(len(xz), 4).all(1)
    return ok & np.roll(ok, 1)


def _ribbon_dense(floor, xz, clearance):
    t = unit(np.roll(xz, -1, axis=0)-xz)
    y = floor.lookup(floor.y, xz)
    ok = np.isfinite(y) & ~floor.lookup(floor.obstacle, xz)
    for side in (-clearance, clearance):
        e = xz+right_of(t)*side
        ys = np.where(floor.lookup(floor.mask, e), floor.lookup(floor.y, e), floor.lookup(floor.street.y, e))
        ok &= np.isfinite(ys) & (np.abs(ys-y) <= .6) & ~floor.lookup(floor.obstacle, e)
    return ok


def fillet_tight(xz, floor):
    """Round every turn tighter than MIN_RADIUS with the widest fitting arc.

    Sharp street corners make the thinned centerline (and so the inner lane)
    turn almost on the spot. Each tight point is replaced by a smooth Hermite
    arc between points w metres either side (w from 14m down), keeping the
    arc only if its whole ribbon stays on street and it is wider than before.
    Turnarounds (over 150 degrees) keep the loop built for them.
    """
    xz = resample(xz, 1., closed=True)
    n = len(xz)
    frozen = np.zeros(n, bool)
    for _ in range(200):
        radius = circuit_radius(xz)
        radius[frozen] = np.inf
        i = int(np.argmin(radius))
        if radius[i] >= MIN_RADIUS:
            break
        done = False
        for w in range(14, 2, -1):
            if 2*w+3 > n:
                continue
            ia, ib = (i-w) % n, (i+w) % n
            p0, p1 = xz[ia], xz[ib]
            t0 = unit(xz[(ia+1) % n]-xz[(ia-1) % n])
            t1 = unit(xz[(ib+1) % n]-xz[(ib-1) % n])
            angle = np.arccos(np.clip(t0@t1, -1, 1))
            if angle > np.radians(150):
                break
            chord = np.linalg.norm(p1-p0)
            k = chord*(1+.3*angle/(np.pi/2))
            arc = hermite(p0, t0*k, p1, t1*k, 2*w+1)
            trial = xz.copy()
            idx = (np.arange(-w, w+1)+i) % n
            trial[idx] = arc
            window = (np.arange(-w-3, w+4)+i) % n
            if not ribbon_points(floor, trial)[window].all():
                continue
            if circuit_radius(trial)[window].min() <= radius[i]+.05:
                continue
            xz, done = trial, True
            break
        if not done:
            frozen[max(i-2, 0):i+3] = True
            if i < 2 or i > n-3:
                frozen[(np.arange(-2, 3)+i) % n] = True
    return xz


def nudge_supported(xz, floor, reach=12., limit=1.5):
    """Shift a closed lane sideways where its ribbon still leaves the street.

    A bend inside a road (for example where a barrier closes one arm of a
    junction) can put the inner ribbon edge over the curb even after the
    clearance fit. Each failing stretch moves by the smallest sideways
    offset (up to `limit`) that puts its ribbon back on the street, blended
    out over `reach` metres either side.
    """
    xz = resample(xz, 1., closed=True)
    n = len(xz)
    ok = ribbon_points(floor, xz)
    arc = np.arange(n, dtype=float)
    for _ in range(20):
        bad = np.flatnonzero(~ok)
        if not len(bad):
            break
        i = int(bad[0])
        run = [i]
        while (run[-1]+1) % n in set(bad.tolist()) and len(run) < n:
            run.append((run[-1]+1) % n)
        centre = run[len(run)//2]
        normal = right_of(unit(np.roll(xz, -1, axis=0)-np.roll(xz, 1, axis=0)))
        gap = np.abs((arc-arc[centre]+n/2) % n-n/2)
        bump = np.where(gap < reach+len(run)/2, .5+.5*np.cos(np.pi*np.clip((gap-len(run)/2)/reach, 0, 1)), 0.)
        window = gap < reach+len(run)/2+3
        moved = False
        for d in sorted(np.arange(-limit, limit+1e-6, .25), key=abs):
            if abs(d) < 1e-6:
                continue
            trial = xz+normal*(bump*d)[:, None]
            trial_ok = ribbon_points(floor, trial)
            if trial_ok[window].all():
                xz, ok, moved = trial, trial_ok, True
                break
        if not moved:
            ok[run] = True  # Leave it to the native check to report.
    return xz


def resample_adaptive(xz, focus=None):
    """Closed resample: SPACING on straights, half of it through turns.

    Native lanes run straight chords between controls; dense controls keep
    a turn's chords from cutting inside its supported ribbon. Positions in
    focus (where a native check failed) get 0.75m controls within 12m.
    """
    xz = resample(xz, .5, closed=True)
    radius = circuit_radius(xz, 4)
    if focus is not None and len(focus):
        near = np.linalg.norm(xz[:, None]-np.asarray(focus)[None], axis=2).min(1) < 12.
        radius = np.where(near, -1., radius)
    seg = np.linalg.norm(np.roll(xz, -1, axis=0)-xz, axis=1)
    arc = np.r_[0, np.cumsum(seg)]
    total = arc[-1]
    stations, at = [], 0.
    while at < total-1e-6:
        stations.append(at)
        i = min(int(np.searchsorted(arc, at, side='right'))-1, len(xz)-1)
        at += SPACING/4 if radius[i] < 0 else SPACING/2 if radius[i] < 15. else SPACING
    # Native tangents are central differences: spread the closing remainder
    # over the whole circuit instead of leaving one short segment.
    st = np.array(stations)*(total/at)
    closed = np.vstack((xz, xz[:1]))
    return np.column_stack([np.interp(st, arc, closed[:, k]) for k in (0, 1)])


def with_heights(floor, xz):
    """Lane controls on the street surface (lightly smoothed heights)."""
    y = floor.lookup(floor.y, xz)
    bad = ~np.isfinite(y)
    if bad.any():
        y[bad] = floor.lookup(floor.street.y, xz[bad])
    y = np.where(np.isfinite(y), y, np.nan)
    if np.isnan(y).any():
        idx = np.arange(len(y))
        ok = ~np.isnan(y)
        y = np.interp(idx, idx[ok], y[ok], period=len(y))
    y = (np.roll(y, 1)+2*y+np.roll(y, -1))/4
    return np.column_stack((xz[:, 0], y, xz[:, 1]))


def plan(street_tris, driveable_tris, mesh, visible=None, check=None):
    """Closed right-hand circuits covering every two-way road.

    check(points, floor) -> per-segment failure mask (the native ribbon qualification);
    failing places are resampled with denser controls before giving up.
    """
    floor = Floor(street_tris, driveable_tris, mesh, visible)
    net = Network(floor)
    net.split_pinches(floor)
    net.drop_short_dead_ends(MIN_DEAD_END)
    d = net.degree()
    # A junction's centerlines bend toward its middle over about the width
    # of the widest road meeting there; straighten that whole approach.
    width = defaultdict(float)
    for e in net.edges:
        cut = e['length']*.3
        _, _, _, far_r = side_offsets(floor, e['center'], cut, cut)
        _, _, _, far_l = side_offsets(floor, e['center'][::-1], cut, cut)
        if np.isfinite(far_r).any() and np.isfinite(far_l).any():
            w = float(np.nanmedian(far_r)+np.nanmedian(far_l))
            for k in (e['a'], e['b']):
                width[k] = max(width[k], w)
    for e in net.edges:
        if e['a'] == e['b']:
            continue  # A closed road has no junction to approach.
        if d[e['a']] > 1:
            e['center'] = straight_approach(e['center'], max(JUNCTION_SETBACK, .75*width[e['a']]))
        if d[e['b']] > 1:
            e['center'] = straight_approach(e['center'][::-1], max(JUNCTION_SETBACK, .75*width[e['b']]))[::-1]
    # Lane offsets; roads too narrow for two opposing lanes are removed.
    while True:
        d = net.degree()
        lanes, narrow = [], []
        for e in net.edges:
            e['floor'] = floor
            skip_a = 6. if d[e['a']] == 1 else 14.
            skip_b = 6. if d[e['b']] == 1 else 14.
            if e['length'] < skip_a+skip_b+4:
                skip_a = skip_b = e['length']*.3
            right, band_r, station, _ = side_offsets(floor, e['center'], skip_a, skip_b)
            left, band_l, _, _ = side_offsets(floor, e['center'][::-1], skip_b, skip_a)
            left = left[::-1]
            fits = []
            for off, band in ((right, band_r), (left, band_l[::-1])):
                valid = np.isfinite(band)
                fits.append(valid.sum() >= 3 and np.mean(band[valid] >= 2*RIBBON-.5) >= .8)
            if not all(fits):
                narrow.append(e)
                continue
            e['right'] = fill_profile(right, station)
            e['left'] = fill_profile(left, station)
        if not narrow:
            break
        net.narrow += narrow
        net.edges = [e for e in net.edges if e not in narrow]
        net.drop_short_dead_ends(MIN_DEAD_END)
    lanes = []
    for e in net.edges:
        lanes.append(Lane(e, +1, e['right']))
        lanes.append(Lane(e, -1, e['left']))
    starting = defaultdict(list)
    for ln in lanes:
        starting[ln.start].append(ln)
    twin = {}
    for i in range(0, len(lanes), 2):
        twin[id(lanes[i])], twin[id(lanes[i+1])] = lanes[i+1], lanes[i]
        lanes[i].twin, lanes[i+1].twin = lanes[i+1], lanes[i]
    degree, corner_cache = net.degree(), {}

    def successor(ln):
        options = [o for o in starting[ln.end] if o is not twin[id(ln)]]
        if not options:
            return twin[id(ln)]
        h = ln.arrival()
        best, score = None, -9.
        for o in options:
            dv = o.departure()
            angle = np.arctan2(dv@right_of(h), dv@h)
            if angle > score:
                best, score = o, angle
        return best

    # Node radius: setback where a turn may start (half the widest road).
    radius = defaultdict(lambda: 6.)
    for e in net.edges:
        w = np.nanmax(np.r_[e['right'], e['left']])+RIBBON
        for k in (e['a'], e['b']):
            radius[k] = max(radius[k], w)
    used, circuits, report = set(), [], dict(turns=0, failed_turns=[], dead_ends=0, failed_dead_ends=[])
    for first in lanes:
        if id(first) in used:
            continue
        seq = [first]
        used.add(id(first))
        while True:
            nxt = successor(seq[-1])
            if nxt is first:
                break
            if id(nxt) in used:
                raise ValueError('road network face walk is not a permutation')
            used.add(id(nxt))
            seq.append(nxt)
        joins = []
        for i, ln in enumerate(seq):
            nxt = seq[(i+1) % len(seq)]
            if nxt is twin[id(ln)]:
                result = dead_end_turn(ln, nxt)
                report['dead_ends'] += 1
                if result is None:
                    report['failed_dead_ends'].append(ln.center[-1].tolist())
                    s = min(4., ln.length*.3)
                    p0, t0 = ln.at(ln.length-s)
                    p1, t1 = nxt.at(s)
                    joins.append((ln.length-s, s, hermite(p0, t0*8, p1, t1*8, 12)))
                else:
                    joins.append(result[:3])
            else:
                if degree[ln.end] == 2:
                    s_in, s_out, curve = corner_turn(ln, nxt, radius[ln.end], corner_cache)
                else:
                    s_in, s_out, curve = junction_turn(ln, nxt, radius[ln.end])
                report['turns'] += 1
                if not ribbon_ok(floor, curve):
                    report['failed_turns'].append(ln.center[-1].tolist())
                joins.append((ln.length-s_in, s_out, curve))
        path = []
        for i, ln in enumerate(seq):
            begin = joins[i-1][1]
            end = joins[i][0]
            if end-begin < 1.:
                mid = (begin+end)/2
                begin, end = mid-.5, mid+.5
            path.append(ln.piece(begin, end))
            path.append(joins[i][2])
        xz = np.vstack(path)
        keep = np.r_[True, np.linalg.norm(np.diff(xz, axis=0), axis=1) > 1e-3]
        base = fillet_tight(xz[keep], floor)
        focus = []
        for _ in range(4):
            points = with_heights(floor, resample_adaptive(base, focus))
            if check is None:
                break
            bad = check(points, floor)
            if not bad.any():
                break
            focus += points[bad][:, [0, 2]].tolist()
            if len(focus) > len(points[bad]):
                base = nudge_supported(base, floor)
        report['densified'] = report.get('densified', 0)+len(focus)
        circuits.append(points)
    report.update(nodes=len(net.nodes), roads=len(net.edges), narrow=len(net.narrow),
                  alleys=len(getattr(net, 'alleys', [])), seam_cells=floor.seam_cells,
                  dropped_components=net.dropped, circuits=len(circuits),
                  road_length=round(sum(e['length'] for e in net.edges), 1))
    return circuits, report, floor, net
