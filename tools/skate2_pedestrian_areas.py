"""Authored water, road and crosswalk geometry for BAM pedestrian navigation.

Every terrain surface remains eligible for normal Recast slope/clearance rules.
Water uses actual rendered triangles; RAIN supplies vehicle roadway ribbons.
Authored pavement beside sidewalks closes unused road lanes up to the curb. Roadside shoulders
are closed only where they border an authored sidewalk at the same height.
Crosswalk material triangles reopen only the roadway at the same height.
"""
import struct

import numpy as np

from inspect_sfil import assets, dictionary, materials, resolve_reference


def pedestrian_floors(tags):
    # Skateboard physical types do not describe pedestrian access or water.
    return np.ones(len(tags), dtype=np.uint8)


def area_material(channels, classic=False):
    family = channels.get('AttribulatorMaterialName', {}).get('text', '').lower()
    if family in ('ocean.default', 'water.default'):
        return 'water'
    # The authored road-crosswalk surface includes the stripe gaps, unlike
    # trying to infer a corridor from white pixels or a merged mesh's bounds.
    diffuse = channels.get('diffuse', {}).get('text', '').lower()
    if classic:
        if diffuse.startswith(('suburb_road_crosswalk', 'road_center_crosswalk')):
            return 'crosswalk'
        if diffuse.startswith('suburb_sidewalk_'):
            return 'sidewalk'
        if diffuse.startswith(('suburb_roadway_roadcenter_', 'suburb_roadway_roadedge_',
                               'suburb_roadway_intersection_', 'suburb_roadway_patchtile_',
                               'road_base_intersection')):
            return 'road'
    if diffuse.startswith(('obj_road_crosswalk_', 'obj_road_crosswalkyellow_',
                           'ot_roadb_crosswalk_')):
        return 'crosswalk'
    if diffuse.startswith(('sidewalk', 'obj_sidewalk_', 'ot_sidewalk')):
        return 'sidewalk'
    if diffuse.startswith('obj_road_roadside'):
        return 'shoulder'
    # Street pavement includes parking/gutter lanes outside RAIN's traffic
    # ribbons. Alleys, driveways and concrete ramps are separate walkable roles.
    if diffuse.startswith(('obj_road_roadfresh_', 'obj_road_roadworn_',
                           'obj_road_road_middlelane_', 'obj_road_road_gutterlane_',
                           'road_curb_', 'road_center_', 'road_clean', 'road_trashed_',
                           'ot_roadb_')) and not diffuse.startswith('ot_roadb_alley_'):
        return 'road'
    return None


def rendered_areas(arena, resource, classic=False):
    """Decode only authored area meshes, preserving individual triangles.

    Static BAM and DIST_Water positions are world-space float3 (format 57).
    Reject an unfamiliar selected mesh rather than silently omitting a mask.
    """
    objects, mats = list(dictionary(arena)), dict(materials(arena))
    parts = {k: [] for k in ('water', 'crosswalk', 'road', 'shoulder', 'sidewalk')}
    for obj in objects:
        if obj['kind'] != 0xeb0023:
            continue
        at = obj['pointer']
        _, offset = resolve_reference(arena, struct.unpack_from('>I', arena, at+36)[0])
        kind = area_material(mats[(offset-20)//12], classic)
        if kind is None:
            continue
        declaration = objects[struct.unpack_from('>I', arena, at+40)[0]]['pointer']
        elements = struct.unpack_from('>H', arena, declaration+8)[0]
        stride = arena[declaration+(elements+1)*16]
        fmt = struct.unpack_from('>I', arena, declaration+20)[0] & 63
        vb = objects[struct.unpack_from('>I', arena, at+52)[0]-1]
        ib = objects[struct.unpack_from('>I', arena, at+48)[0]-1]
        if fmt != 57 or stride < 12 or vb['size'] % stride or ib['size'] % 2:
            raise ValueError('unsupported authored area mesh layout')
        if vb['pointer']+vb['size'] > len(resource) or ib['pointer']+ib['size'] > len(resource):
            raise ValueError('authored area vertex/index buffer outside resource')
        vertices = np.ndarray((vb['size']//stride,3), '>f4', buffer=resource,
                              offset=vb['pointer'], strides=(stride,4)).astype(np.float32)
        indices = np.frombuffer(resource, '>u2', ib['size']//2, ib['pointer'])
        draws, relative = struct.unpack_from('>I',arena,at+56)[0], struct.unpack_from('>I',arena,at+68)[0]
        if relative < 72 or relative+draws*16 > obj['size']:
            raise ValueError('authored area draw array outside mesh')
        for i in range(draws):
            primitive, base, start, count = struct.unpack_from('>4I',arena,at+relative+i*16)
            if start+count > len(indices):
                raise ValueError('authored area draw outside index buffer')
            seq = indices[start:start+count].astype(np.int64)+base
            if len(seq) and seq.max() >= len(vertices):
                raise ValueError('authored area vertex outside buffer')
            if primitive == 4 and count % 3 == 0:
                triangles = vertices[seq.reshape(-1,3)]
            elif primitive == 5 and count >= 3:
                triangles = vertices[np.array([seq[j:j+3] for j in range(count-2)])]
            else:
                raise ValueError('unsupported authored area primitive')
            if not np.isfinite(triangles).all():
                raise ValueError('non-finite authored area geometry')
            edge1,edge2 = triangles[:,1]-triangles[:,0], triangles[:,2]-triangles[:,0]
            # Vertical sides/degenerate strips cannot define a floor mask.
            valid = np.abs(np.cross(edge1,edge2)[:,1]) > 1e-6
            parts[kind].append(triangles[valid])
    return {k: np.concatenate(v) if v else np.empty((0,3,3),np.float32)
            for k,v in parts.items()}


def sidewalk_shoulders(shoulders, sidewalks, tolerance=.75):
    """Select only shoulder triangles touching a real sidewalk, in 3D.

    Sample triangle edges at <=0.5m, then measure exact XZ distance to the
    sidewalk triangles and interpolated height. The default 0.75m join covers
    the curb/seam. Road pavement uses an 8m sidewalk search across its lane
    width. These distances select triangles; they do not expand the road mask.
    A spatial index avoids a whole-city all-pairs comparison. No district,
    terrain type, absolute elevation or merged-mesh bounding mask is used.
    """
    selected = np.zeros(len(shoulders),bool)
    if not len(shoulders) or not len(sidewalks):
        return selected
    grid = {}
    size = 20.
    low, high = sidewalks.min(1), sidewalks.max(1)
    for i,(lo,hi) in enumerate(zip(low,high)):
        for x in range(int(np.floor((lo[0]-tolerance)/size)),int(np.floor((hi[0]+tolerance)/size))+1):
            for z in range(int(np.floor((lo[2]-tolerance)/size)),int(np.floor((hi[2]+tolerance)/size))+1):
                grid.setdefault((x,z),[]).append(i)
    for i,triangle in enumerate(shoulders):
        lo,hi = triangle.min(0),triangle.max(0)
        candidates=set()
        for x in range(int(np.floor(lo[0]/size)),int(np.floor(hi[0]/size))+1):
            for z in range(int(np.floor(lo[2]/size)),int(np.floor(hi[2]/size))+1):
                candidates.update(grid.get((x,z),[]))
        if not any(low[j,1]<=hi[1]+.5 and high[j,1]>=lo[1]-.5 and
                   low[j,0]<=hi[0]+tolerance and high[j,0]>=lo[0]-tolerance and
                   low[j,2]<=hi[2]+tolerance and high[j,2]>=lo[2]-tolerance for j in candidates):
            continue
        samples = [triangle.mean(0)[None,:]]
        for a,b in zip(triangle,np.roll(triangle,-1,axis=0)):
            count = max(1,int(np.ceil(np.linalg.norm((b-a)[[0,2]])/.5)))
            samples.append(a+(b-a)*np.linspace(0,1,count+1)[:,None])
        points = np.concatenate(samples)
        for p in points:
            ids = grid.get((int(np.floor(p[0]/size)),int(np.floor(p[2]/size))),[])
            ids = [j for j in ids if low[j,1] <= p[1]+.5 and high[j,1] >= p[1]-.5 and
                   low[j,0]<=p[0]+tolerance and high[j,0]>=p[0]-tolerance and
                   low[j,2]<=p[2]+tolerance and high[j,2]>=p[2]-tolerance]
            if not ids:
                continue
            tris = sidewalks[ids].astype(np.float64)
            a,b,c = tris[:,0],tris[:,1],tris[:,2]
            det = (b[:,2]-c[:,2])*(a[:,0]-c[:,0])+(c[:,0]-b[:,0])*(a[:,2]-c[:,2])
            good = np.abs(det)>1e-6
            tris,a,b,c,det = tris[good],a[good],b[good],c[good],det[good]
            u = ((b[:,2]-c[:,2])*(p[0]-c[:,0])+(c[:,0]-b[:,0])*(p[2]-c[:,2]))/det
            v = ((c[:,2]-a[:,2])*(p[0]-c[:,0])+(a[:,0]-c[:,0])*(p[2]-c[:,2]))/det
            inside = (u>=0)&(v>=0)&(u+v<=1)&(np.abs(u*a[:,1]+v*b[:,1]+(1-u-v)*c[:,1]-p[1])<=.5)
            touches = bool(inside.any())
            for a,b in zip(np.moveaxis(tris,1,0),np.moveaxis(np.roll(tris,-1,axis=1),1,0)):
                delta = (b-a)[:,[0,2]]
                length = (delta*delta).sum(1)
                t = np.clip(((p[[0,2]]-a[:,[0,2]])*delta).sum(1)/np.maximum(length,1e-12),0,1)
                nearest = a+(b-a)*t[:,None]
                touches |= bool((((nearest[:,[0,2]]-p[[0,2]])**2).sum(1)<=tolerance**2)[np.abs(nearest[:,1]-p[1])<=.5].any())
            if touches:
                selected[i] = True
                break
    return selected


def pedestrian_surface_triangles(stream, ocean_model, classic=False):
    if not ocean_model.is_file():
        raise ValueError('prepared Skate 2 ocean model is required for navigation')
    raw = ocean_model.read_bytes()
    resource = struct.unpack_from('>I',raw,0x44)[0]
    if resource < 0x50 or resource >= len(raw):
        raise ValueError('invalid ocean model resource offset')
    areas = rendered_areas(raw,raw[resource:])
    parts = {k: [v] for k,v in areas.items()}
    if not len(areas['water']):
        raise ValueError('ocean model contains no authored water triangles')
    seen = set()
    for path in sorted(p for p in stream.iterdir() if p.name.startswith('cPres_') and
                       (p.name.endswith('_high.xsf') or p.name == 'cPres_Global.xsf')):
        for ident, _, (arena, buffers) in assets(path.read_bytes()):
            if ident in seen:
                continue
            seen.add(ident)
            areas = rendered_areas(arena,buffers,classic)
            for k,v in areas.items():
                parts[k].append(v)
    result = {k:np.concatenate(v) for k,v in parts.items()}
    selected = sidewalk_shoulders(result['shoulder'],result['sidewalk'])
    result['sidewalk_shoulders'] = result['shoulder'][selected]
    result['sidewalk_roads'] = result['road'][sidewalk_shoulders(result['road'],result['sidewalk'],8.)]
    result['pavement'] = np.concatenate((result['road'] if classic else result['sidewalk_roads'],
                                       result['sidewalk_shoulders']))
    return result


def water_crosswalk_triangles(stream, ocean_model):
    areas = pedestrian_surface_triangles(stream,ocean_model)
    return areas['water'],areas['crosswalk']


def _hull(points):
    """Convex XZ footprint, retaining each point's authored height."""
    points = sorted(set(map(tuple, points)), key=lambda p: (p[0], p[2], p[1]))
    def cross(a, b, c):
        return (b[0]-a[0])*(c[2]-a[2])-(b[2]-a[2])*(c[0]-a[0])
    halves = []
    for sequence in (points, list(reversed(points))):
        half = []
        for p in sequence:
            while len(half) >= 2 and cross(half[-2], half[-1], p) <= 1e-7:
                half.pop()
            half.append(p)
        halves.append(half[:-1])
    hull = halves[0] + halves[1]
    if len(hull) < 3:
        return None  # Authored zero-width/end-cap record, not a drivable area.
    if len(hull) > 4:
        raise ValueError('road footprint exceeds quad format')
    return hull + [hull[-1]] * (4-len(hull))


def rain_footprints(data):
    """Lane Hermite ribbons and outer intersection quads in world coordinates.

    Layout/fixups: 82962890, 82962A40, 82962C18. Lane endpoints/width
    boundaries are the same data used by ContinueFollowingLane (82C376E8).
    Split each short lane segment into four ribbons to follow curved boundaries.
    """
    if len(data) < 64:
        raise ValueError('truncated RAIN header')
    ni, nr, ns, oi, ore, os = struct.unpack_from('>6I', data, 32)
    for n, at, stride in ((ni, oi, 0x260), (nr, ore, 160), (ns, os, 64)):
        if n and (at < 64 or at+n*stride > len(data)):
            raise ValueError('RAIN array outside resource')
    quads = []
    def add(points):
        if not np.isfinite(points).all():
            raise ValueError('non-finite RAIN geometry')
        quad = _hull(points)
        if quad is not None:
            quads.append(quad)
    for i in range(ni):
        at = oi+i*0x260
        add([struct.unpack_from('>3f', data, at+p) for p in (0x40, 0x50, 0x60, 0x70)])
    for i in range(nr):
        at = ore+i*160
        count, rel = struct.unpack_from('>II', data, at+120)
        if not count:
            continue  # Zero-count arrays may retain poison offsets.
        if rel < 160 or at+rel+count*224 > len(data):
            raise ValueError('RAIN lane array outside resource')
        for j in range(count):
            lane = at+rel+j*224
            values = np.array([struct.unpack_from('>3f', data, lane+p)
                               for p in (0x50,0x60,0x70,0x80,0x90,0xa0,0xb0,0xc0)])
            if not np.isfinite(values).all():
                raise ValueError('non-finite RAIN lane')
            left0,right0,left1,right1,ld0,rd0,ld1,rd1 = values
            def hermite(a, b, da, db, t):
                return ((2*t**3-3*t**2+1)*a + (-2*t**3+3*t**2)*b +
                        (t**3-2*t**2+t)*da + (t**3-t**2)*db)
            left = [hermite(left0,left1,ld0,ld1,t) for t in np.linspace(0,1,5)]
            right = [hermite(right0,right1,rd0,rd1,t) for t in np.linspace(0,1,5)]
            for k in range(4):
                add([left[k], right[k], right[k+1], left[k+1]])
    return np.asarray(quads, dtype=np.float32).reshape(-1,4,3)


def road_footprints(stream, allow_empty=False):
    parts, seen = [], set()
    for path in sorted(p for p in stream.iterdir() if p.name.startswith('cSim_') and p.suffix == '.xsf'):
        for ident, _, (arena, _) in assets(path.read_bytes(), include_resource=False):
            if ident in seen:
                continue
            seen.add(ident)
            for obj in dictionary(arena):
                if obj['kind'] == 0xeb0013:
                    parts.append(rain_footprints(arena[obj['pointer']:obj['pointer']+obj['size']]))
    if not parts or not any(len(p) for p in parts):
        if allow_empty:
            return np.empty((0,4,3),dtype=np.float32)
        raise ValueError('BAM has no authored road footprints')
    return np.concatenate(parts)
