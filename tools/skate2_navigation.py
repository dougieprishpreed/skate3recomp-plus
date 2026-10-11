"""Bake native NavPower surfaces from instantiated BAM collision.

This module only writes the caller's scratch directory. The map publisher owns
installation. No original game file, native map, or guest navigation code changes.
"""
from __future__ import annotations

import struct
import subprocess
import json
import re
from pathlib import Path

import numpy as np

from inspect_sfil import assets, dictionary
from navpower_resource import write_resource, wrap_resource
from skate2_collision import static_triangles
from skate2_pedestrian_areas import road_footprints


def collision(stream: Path, with_tags=False):
    parts, tags = [], []
    for path in sorted(p for p in stream.iterdir() if p.name.startswith('cSim_') and p.suffix == '.xsf'):
        for _, _, (arena, _) in assets(path.read_bytes(), include_resource=False):
            for points, ids in static_triangles(arena, list(dictionary(arena)), True):
                parts.append(points.astype(np.float32))
                tags.append(ids)
    if not parts:
        raise ValueError('map has no instantiated static collision')
    triangles = np.concatenate(parts)
    return (triangles, np.concatenate(tags)) if with_tags else triangles


def read_bake(data: bytes):
    if len(data) < 20 or data[:8] not in (b'NSVPOL1\0', b'NSVPOL2\0'):
        raise ValueError('invalid navigation baker output')
    nv, count, nvp = struct.unpack_from('<3I', data, 8)
    version2 = data[:8] == b'NSVPOL2\0'
    arrays_end = 20+nv*12+count*nvp*4
    if not 3 <= nvp <= 6 or len(data) != arrays_end + (count*8 if version2 else 0):
        raise ValueError('invalid navigation baker array sizes')
    vertices = np.frombuffer(data, '<f4', nv*3, 20).reshape(-1, 3)
    if not np.isfinite(vertices).all():
        raise ValueError('non-finite baked vertex')
    raw = np.frombuffer(data, '<u2', count*nvp*2, 20+nv*12).reshape(-1, nvp*2)
    polys, links = [], []
    for row in raw:
        ids = list(map(int, row[:nvp][row[:nvp] != 0xffff]))
        if len(ids) < 3 or any(int(v) != 0xffff for v in row[len(ids):nvp]):
            raise ValueError('invalid polygon sentinel layout')
        edge_links = list(map(int, row[nvp:nvp+len(ids)]))
        # Recast uses 0xffff for a wall and 0x8000..0x8003 for tile
        # portals. Do not silently turn a corrupt neighbor into a wall.
        if any((n >= count if n < 0x8000 else
                n not in (0xffff, 0x8000, 0x8001, 0x8002, 0x8003))
               for n in edge_links):
            raise ValueError('baked neighbor outside polygon table')
        polys.append(ids)
        links.append([n if n < 0x8000 else -1 for n in edge_links])
    # Recast can emit zero projected-area triangles along a vertical seam.
    # NavPower requires a floor plane: remove these and close their incident
    # edges, never turn a vertical wall into a pedestrian connection.
    exact_area = np.frombuffer(data, '<i8', count, arrays_end) if version2 else None
    keep = []
    for index, ids in enumerate(polys):
        if any(i >= len(vertices) for i in ids):
            raise ValueError('baked vertex index outside table')
        if exact_area is not None:
            # Invalid projected slivers cannot form a floor. Discard them
            # and close incident links rather than reversing their winding.
            if exact_area[index] <= 0:
                keep.append(False)
                continue
        v = vertices[ids].astype(np.float64)
        area = float(np.cross(v-v[0], np.roll(v, -1, axis=0)-v[0])[:, 1].sum())
        if area < -1e-8:
            raise ValueError('downward-wound baked polygon')
        keep.append(area > 1e-8)
    remap = {old: new for new, old in enumerate(i for i, yes in enumerate(keep) if yes)}
    return (vertices, [p for i, p in enumerate(polys) if keep[i]],
            [[remap.get(n, -1) for n in links[i]] for i in range(len(polys)) if keep[i]])


def exclude_water_polygons(vertices, polys, links, water):
    """Reject exported polygon shortcuts through water, including tile seams.

    Recast's border/contour simplification can span a masked shoreline. Clip
    each floor triangle against the actual water triangles in XZ, interpolating
    both heights. A linear height difference reaches its minimum at an
    intersection vertex, so this also catches partial overlap, not just centers.
    Close incident links when removing a polygon; decks and dry low ground
    outside the water remain valid. This is an export invariant for every cell.
    """
    water=np.asarray(water if water is not None else [],dtype=np.float64).reshape(-1,3,3)
    if not len(water):
        return polys,links,0
    low,high=water.min(1),water.max(1)
    keep=[]
    for ids in polys:
        p=vertices[ids].astype(np.float64)
        lo,hi=p.min(0),p.max(0)
        nearby=water[(low[:,0]<=hi[0])&(high[:,0]>=lo[0])&(low[:,2]<=hi[2])&(high[:,2]>=lo[2])&
                     (high[:,1]+.05>=lo[1])]
        forbidden=False
        for wet in nearby:
            a,b,c=wet
            det=(b[2]-c[2])*(a[0]-c[0])+(c[0]-b[0])*(a[2]-c[2])
            if abs(det)<1e-6:
                continue
            orientation=1 if det>0 else -1
            for i in range(1,len(p)-1):
                clipped=[p[0],p[i],p[i+1]]
                for edge,next_edge in zip(wet,np.roll(wet,-1,axis=0)):
                    if not clipped:
                        break
                    delta=next_edge-edge
                    def distance(v):
                        return orientation*(delta[0]*(v[2]-edge[2])-delta[2]*(v[0]-edge[0]))
                    output=[]
                    for start,end in zip(clipped,clipped[1:]+clipped[:1]):
                        ds,de=distance(start),distance(end)
                        if ds>=-1e-8:
                            output.append(start)
                        if (ds>=0)!=(de>=0):
                            output.append(start+(end-start)*(ds/(ds-de)))
                    clipped=output
                if len(clipped)<3:
                    continue
                v=np.array(clipped)
                area=abs(np.cross(v-v[0],np.roll(v,-1,axis=0)-v[0])[:,1].sum())
                if area<1e-8:
                    continue  # Touching the water edge is not an overlap.
                u=((b[2]-c[2])*(v[:,0]-c[0])+(c[0]-b[0])*(v[:,2]-c[2]))/det
                vv=((c[2]-a[2])*(v[:,0]-c[0])+(a[0]-c[0])*(v[:,2]-c[2]))/det
                y=u*a[1]+vv*b[1]+(1-u-vv)*c[1]
                if np.any(v[:,1]<=y+.05):
                    forbidden=True
                    break
            if forbidden:
                break
        keep.append(not forbidden)
    remap={old:new for new,old in enumerate(i for i,yes in enumerate(keep) if yes)}
    return ([p for i,p in enumerate(polys) if keep[i]],
            [[remap.get(n,-1) for n in links[i]] for i in range(len(polys)) if keep[i]],
            len(polys)-len(remap))


def bake_cell(triangles, low, high, center, helper: Path, scratch: Path,
              floors=None, roads=None, crossings=None, water=None, pavement=None, vehicle=False):
    """200 m BAM cell, rasterized with a one-meter neighbor border.

    The helper removes the border after clearance erosion. This preserves open
    seams for the native resource loader's tile connection pass.
    """
    x, z = center
    extent = 102.2 if vehicle else 101
    selected = ((high[:, 0] >= x-extent) & (low[:, 0] <= x+extent) &
                (high[:, 2] >= z-extent) & (low[:, 2] <= z+extent))
    mesh = triangles[selected]
    if not len(mesh):
        return None, dict(triangles=0, polygons=0)
    bounds = (x-extent, float(mesh[:, :, 1].min())-1, z-extent,
              x+extent, float(mesh[:, :, 1].max())+2, z+extent)
    scratch.mkdir(parents=True, exist_ok=True)
    source, target = scratch/'collision.bin', scratch/'polygons.bin'
    encoded = b'NSVCOL1\0' + struct.pack('<I6f', len(mesh), *bounds) + mesh.astype('<f4', copy=False).tobytes()
    road_count = forbidden = 0
    if floors is not None:
        if len(floors) != len(triangles) or not np.isin(floors, (0,1)).all():
            raise ValueError('invalid pedestrian floor classification')
        area_mask = np.asarray(floors, dtype=np.uint8)[selected]
        quads = np.asarray(roads if roads is not None else [], dtype=np.float32).reshape(-1,4,3)
        if not np.isfinite(quads).all():
            raise ValueError('non-finite road footprint')
        qlow, qhigh = quads.min(1), quads.max(1)
        quads = quads[(qhigh[:,0]>=x-extent)&(qlow[:,0]<=x+extent)&
                      (qhigh[:,2]>=z-extent)&(qlow[:,2]<=z+extent)]
        encoded = b'NSVCOL2\0' + encoded[8:] + area_mask.tobytes() + struct.pack('<I',len(quads)) + quads.astype('<f4').tobytes()
        forbidden, road_count = int((area_mask == 0).sum()), len(quads)
    authored_counts = {}
    if crossings is not None or water is not None or pavement is not None:
        if floors is None:
            raise ValueError('authored areas require classified input')
        encoded = (b'NSVCOL4\0' if pavement is not None else b'NSVCOL3\0')+encoded[8:]
        areas = [('crosswalk_triangles',crossings),('water_triangles',water)]
        if pavement is not None:
            areas.append(('pavement_triangles',pavement))
        for kind, parts in areas:
            parts = np.asarray(parts if parts is not None else [],dtype=np.float32).reshape(-1,3,3)
            if not np.isfinite(parts).all():
                raise ValueError('non-finite authored area triangle')
            qlow,qhigh = parts.min(1),parts.max(1)
            parts = parts[(qhigh[:,0]>=x-extent)&(qlow[:,0]<=x+extent)&
                          (qhigh[:,2]>=z-extent)&(qlow[:,2]<=z+extent)]
            encoded += struct.pack('<I',len(parts))+parts.astype('<f4').tobytes()
            authored_counts[kind] = len(parts)
    source.write_bytes(encoded)
    target.unlink(missing_ok=True)
    process = subprocess.run([str(helper), str(source), str(target)]+(['--vehicle'] if vehicle else []), capture_output=True)
    if process.returncode:
        raise ValueError('navigation bake failed: ' + process.stderr.decode('utf-8', errors='replace').strip())
    vertices, polys, links = read_bake(target.read_bytes())
    polys,links,water_rejected=exclude_water_polygons(vertices,polys,links,water)
    authored_counts['water_export_rejected_polygons']=water_rejected
    if not polys:
        return None, dict(triangles=len(mesh), polygons=0, forbidden_triangles=forbidden, road_quads=road_count, **authored_counts)
    result = wrap_resource(write_resource(vertices, polys, links))
    return result, dict(triangles=len(mesh), polygons=len(polys), vertices=len(vertices),
                        bytes=len(result), forbidden_triangles=forbidden, road_quads=road_count, **authored_counts)


def prepare_navigation(stream: Path, native_data: Path, helper: Path, scratch: Path,
                       ocean_model: Path | None = None, stream_name='BAM', classic=False):
    """Prepare navigation in a new, unpublished map; scratch is disposable.

    The caller must publish the completed map transactionally. Baking all cells
    before appending ensures a bad collision/resource never enables census.
    """
    from navpower_stream import native_template, append_navigation
    from skate2_pedestrian_areas import pedestrian_surface_triangles
    if not helper.is_file():
        raise ValueError('navigation preparation helper is missing')
    marker = stream/'NAVIGATION_COMPLETE_V4'
    if any((stream/f'NAVIGATION_COMPLETE_V{i}').exists() for i in (1,2,3,4)):
        raise ValueError('navigation must be prepared from a fresh map stage')
    template = native_template(native_data)
    triangles = collision(stream)
    floors, roads = np.ones(len(triangles),np.uint8), road_footprints(stream,allow_empty=classic)
    areas = pedestrian_surface_triangles(stream,ocean_model or stream.parent/'sky-water-v1/S2_DIST_Water.rx2',classic)
    water,crossings,pavement = (areas[k] for k in ('water','crosswalk','pavement'))
    scratch.mkdir(parents=True,exist_ok=True)
    for k,v in areas.items():
        np.save(scratch/(k+'.npy'),v)
    low, high = triangles.min(axis=1), triangles.max(axis=1)
    navigation, stats = {}, {}
    for path in sorted(p for p in stream.iterdir() if p.name.startswith('cSim_') and p.name.endswith('_high.xsf')):
        match = re.fullmatch(r'cSim_(-?\d+)_(-?\d+)_high.xsf', path.name)
        if not match:
            raise ValueError('unrecognized streaming cell name')
        data, counts = bake_cell(triangles,low,high,tuple(map(int,match.groups())),helper,scratch,
                                 floors,roads,crossings,water,pavement)
        if data:
            navigation[path.stem] = data
        stats[path.name] = counts
        print(f"navigation: {path.name} polygons={counts['polygons']} forbidden={counts.get('forbidden_triangles',0)} roads={counts.get('road_quads',0)}", flush=True)
    if not navigation:
        raise ValueError('navigation bake produced no walkable cells')
    append_navigation(stream,navigation,template,stream_name)
    result = dict(version=4,policy='water-roadways-crosswalks-v4' if classic else 'water-pavement-crosswalks-v4',cells=stats,
                  polygons=sum(s['polygons'] for s in stats.values()),
                  collision_triangles=len(triangles), forbidden_triangles=int((floors == 0).sum()),
                  road_quads=len(roads), crosswalk_triangles=len(crossings),water_triangles=len(water),
                  pavement_triangles=len(pavement),sidewalk_shoulder_triangles=len(areas['sidewalk_shoulders']),
                  terrain_physical_type_filter=False,road_height_tolerance=.5,
                  sidewalk_join_tolerance=.75,pavement_sidewalk_distance=8.,
                  water_export_polygon_guard=True,
                  water_export_rejected_polygons=sum(s.get('water_export_rejected_polygons',0) for s in stats.values()))
    (stream/'navigation.json').write_bytes((json.dumps(result,indent=2)+'\n').encode('ascii'))
    marker.write_bytes(f"NSV_NAVIGATION_V4\n{len(navigation)} {result['polygons']}\n".encode('ascii'))
    return result
