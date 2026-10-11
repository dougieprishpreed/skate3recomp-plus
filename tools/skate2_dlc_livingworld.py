"""Prepare population paint and native traffic for the three Classic DLC streams.

Original DLC has neither WorldPainter census nor RAIN. Pedestrians use the
shared Recast/NavPower bake. Traffic is derived separately from connected,
authored street surfaces; open map edges are never invented as destinations.
Only unpublished streams are modified. Source archives are read-only.
"""
from collections import deque
import json
import mmap
import struct
from pathlib import Path

import numpy as np

from inspect_sfil import assets, dictionary
from world_archive import entries, decode_archive_payload
from build_skate2_world_support import records, make_catalog, guid
from skate2_pedestrian_areas import pedestrian_surface_triangles, rain_footprints
from skate2_navigation import collision, bake_cell
from navpower_resource import read_rw_object
from vlt64 import h64

STREAMS = ('DLC_School', 'DLC_Community_Center', 'DLC_Parkade')


def templates(source):
    """Original compatible arena schemas, not installed/research assets."""
    found = {}
    path = source/'content/worldbam.big'
    with path.open('rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as raw:
        index = entries(raw)
        toc_entry = next(e for e in index if e.name.endswith('/BAM_Sim.xst'))
        toc = {guid(r):r for r in records(decode_archive_payload(
            bytes(raw[toc_entry.offset:toc_entry.offset+toc_entry.size])),b'ATOC',24,64)}
        for entry in index:
            if not Path(entry.name).name.startswith('cSim_') or not entry.name.endswith('.xsf'):
                continue
            sf = decode_archive_payload(bytes(raw[entry.offset:entry.offset+entry.size]))
            for ident,_,(arena,_) in assets(sf,include_resource=False):
                objs = list(dictionary(arena))
                kinds = [o['kind'] for o in objs]
                if 'road' not in found and kinds == [0xeb0008,0xeb0013,0xeb000b]:
                    found['road'] = arena,bytes(toc[ident])
                if 'paint' not in found:
                    layers = [o for o in objs if o['kind']==0xeb000f]
                    by_layer = {struct.unpack_from('>Q',arena,o['pointer']+8)[0]>>32:o for o in layers}
                    if 2 in by_layer and 5 in by_layer and all(
                        objs[struct.unpack_from('>I',arena,o['pointer'])[0]]['size']==58 and
                        objs[struct.unpack_from('>I',arena,o['pointer']+4)[0]]['size']==32
                        for o in layers):
                        found['paint'] = arena,bytes(toc[ident])
            if len(found)==2:
                return found
    raise ValueError('original native-compatible road/constant census schemas missing')


def pack_asset(arena, toc, identity):
    ident = h64(identity)
    asset = bytearray(208)
    stride = (208+len(arena)+127)&~127
    struct.pack_into('>QIII',asset,0,ident,80+len(arena),128,stride)
    struct.pack_into('>3I',asset,128,len(arena),len(arena),0)
    asset.extend(arena);asset.extend(bytes(stride-len(asset)))
    row = bytearray(toc)
    struct.pack_into('>Q',row,0,ident)
    struct.pack_into('>2I',row,12,len(arena),len(arena))
    decoded = list(assets(b'SFIL\0\0\0\3'+bytes(120)+asset))
    if len(decoded)!=1 or decoded[0][0]!=ident or decoded[0][2][0]!=arena:
        raise ValueError('generated actor stream asset failed round-trip')
    return bytes(asset),row


def census_asset(template, identity, center, npc=32, vehicle=247):
    arena,toc = template
    arena = bytearray(arena)
    objs = list(dictionary(arena))
    for obj in objs:
        at = obj['pointer']
        if obj['kind']==0xeb000f:
            tree,values,key = struct.unpack_from('>IIQ',arena,at)
            layer = key>>32
            if layer not in (2,5):
                # Preserve the schema/exports, but expose no unrelated city paint.
                struct.pack_into('>Q',arena,at+8,(0xff00+layer)<<32|0x03e387c2)
                continue
            t,d = objs[tree],objs[values]
            if t['kind']!=0xeb0010 or d['kind']!=0xeb0011:
                raise ValueError('census schema references wrong objects')
            # One ten-byte quadtree leaf: four absent children and value index0.
            if arena[t['pointer']+48:t['pointer']+58] != bytes.fromhex('ffffffffffffffff0000'):
                raise ValueError('unsupported constant census leaf')
            struct.pack_into('>I',arena,d['pointer']+28,npc if layer==2 else vehicle)
        elif obj['kind']==0xeb0010:
            # 82C0ED80 registers (center-halfsize)/(2*halfsize); 82C0EAC0
            # queries the fixed 128m grid. These are center/halfsize vectors,
            # not min/max bounds or the independent 200m streaming cell.
            struct.pack_into('>4f',arena,at,*center,0.,0.)
            struct.pack_into('>4f',arena,at+16,64.,64.,0.,0.)
        elif obj['kind']==0xeb000b:
            count,table = struct.unpack_from('>II',arena,at)
            for i in range(count):
                struct.pack_into('>Q',arena,at+table+i*24+8,h64(identity+'/export/'+str(i)))
    return pack_asset(bytes(arena),toc,identity)


def census_tiles(mesh, cells):
    """Unique native 128m paint tiles, owned by the nearest 200m stream cell."""
    names=sorted(cells)
    if not names:raise ValueError('DLC census has no streaming owners')
    owners=np.array([tuple(map(int,n.split('_')[1:3])) for n in names])
    low=np.floor(mesh.min(axis=(0,1))[[0,2]]/128).astype(int)
    high=np.floor(mesh.max(axis=(0,1))[[0,2]]/128).astype(int)
    result={n:[] for n in names}
    for x in range(low[0],high[0]+1):
        for z in range(low[1],high[1]+1):
            center=np.array([x*128+64,z*128+64])
            owner=names[int(np.argmin(((owners-center)**2).sum(1)))]
            result[owner].append((int(center[0]),int(center[1])))
    return result


class StreetRaster:
    """One-meter street support; overlapping floors never become one height."""
    def __init__(self, triangles):
        self.origin = np.floor(triangles.min(axis=(0,1))[[0,2]])-4
        high = np.ceil(triangles.max(axis=(0,1))[[0,2]])+4
        shape = (high-self.origin).astype(int)+1
        if np.prod(shape)>4000000:
            raise ValueError('DLC road survey exceeds bounded raster')
        self.y = np.full(tuple(shape),np.inf,np.float32)
        for t in triangles.astype(np.float64):
            a,b,c = t
            det = (b[2]-c[2])*(a[0]-c[0])+(c[0]-b[0])*(a[2]-c[2])
            if abs(det)<1e-5:continue
            lo=np.maximum(0,np.ceil(t[:,[0,2]].min(0)-self.origin).astype(int))
            hi=np.minimum(shape-1,np.floor(t[:,[0,2]].max(0)-self.origin).astype(int))
            xx,zz=np.meshgrid(np.arange(lo[0],hi[0]+1),np.arange(lo[1],hi[1]+1),indexing='ij')
            x,z=xx+self.origin[0],zz+self.origin[1]
            u=((b[2]-c[2])*(x-c[0])+(c[0]-b[0])*(z-c[2]))/det
            v=((c[2]-a[2])*(x-c[0])+(a[0]-c[0])*(z-c[2]))/det
            yy=u*a[1]+v*b[1]+(1-u-v)*c[1]
            inside=(u>=-1e-6)&(v>=-1e-6)&(u+v<=1+1e-6)
            self.y[xx[inside],zz[inside]]=np.minimum(self.y[xx[inside],zz[inside]],yy[inside])

    def height(self,points):
        ij=np.rint(points[:,[0,2]]-self.origin).astype(int)
        valid=(ij>=0).all(1)&(ij<np.array(self.y.shape)).all(1)
        result=np.full(len(points),np.inf)
        result[valid]=self.y[ij[valid,0],ij[valid,1]]
        return result

    def project(self,points):
        result=points.copy();height=self.height(points)
        for i in np.flatnonzero(~np.isfinite(height)):
            ij=np.rint(points[i,[0,2]]-self.origin).astype(int)
            choices=[]
            for dx in range(-2,3):
                for dz in range(-2,3):
                    x,z=ij+[dx,dz]
                    if x<0 or z<0 or x>=self.y.shape[0] or z>=self.y.shape[1]:continue
                    y=self.y[x,z]
                    if np.isfinite(y) and abs(y-points[i,1])<.6:
                        p=np.array([x+self.origin[0],y,z+self.origin[1]])
                        choices.append((float(np.linalg.norm(p-points[i])),p))
            if choices:
                distance,p=min(choices,key=lambda c:c[0])
                if distance<=2.:result[i]=p
        return result


def skeleton(mask):
    """Topology-preserving thinning; no machine-installed SciPy dependency."""
    mask=np.pad(mask,1).astype(bool)
    for iteration in range(512):
        changed=False
        for step in (0,1):
            p=[mask[:-2,1:-1],mask[:-2,2:],mask[1:-1,2:],mask[2:,2:],
               mask[2:,1:-1],mask[2:,:-2],mask[1:-1,:-2],mask[:-2,:-2]]
            neighbors=sum(a.astype(np.uint8) for a in p)
            transitions=sum((~p[i]&p[(i+1)%8]).astype(np.uint8) for i in range(8))
            a,b,c,d=(p[0],p[2],p[4],p[6])
            cond=(~(a&b&c)&~(b&c&d)) if step==0 else (~(a&b&d)&~(a&c&d))
            remove=mask[1:-1,1:-1]&(neighbors>=2)&(neighbors<=6)&(transitions==1)&cond
            if remove.any():changed=True;mask[1:-1,1:-1][remove]=False
        if not changed:return mask[1:-1,1:-1]
    raise ValueError('street thinning did not converge')


def street_cycles(raster):
    mask=np.isfinite(raster.y)
    # Preserve only a driveable street core, not one-pixel texture seams.
    for _ in range(2):
        padded=np.pad(mask,1)
        mask &= padded[:-2,1:-1]&padded[2:,1:-1]&padded[1:-1,:-2]&padded[1:-1,2:]
    points=set(map(tuple,np.argwhere(skeleton(mask))))
    graph={}
    for p in points:
        neighbors=[]
        for dx,dz in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
            q=(p[0]+dx,p[1]+dz)
            if q not in points:continue
            if dx and dz and ((p[0]+dx,p[1]) in points or (p[0],p[1]+dz) in points):continue
            if abs(float(raster.y[p])-float(raster.y[q]))<=.5:neighbors.append(q)
        graph[p]=neighbors
    # Remove dead ends: traffic must have a complete supported continuation.
    dead=deque(p for p,n in graph.items() if len(n)<2)
    while dead:
        p=dead.popleft()
        if p not in graph:continue
        for q in graph.pop(p):
            if q in graph:
                graph[q].remove(p)
                if len(graph[q])<2:dead.append(q)
    seen=set();cycles=[]
    for root in sorted(graph):
        if root in seen:continue
        parent={root:None};depth={root:0};seen.add(root)
        stack=[(root,iter(sorted(graph[root])))]
        component=[]
        while stack:
            p,it=stack[-1]
            q=next(it,None)
            if q is None:stack.pop();continue
            if q==parent[p]:continue
            if q not in seen:
                seen.add(q);parent[q]=p;depth[q]=depth[p]+1
                stack.append((q,iter(sorted(graph[q]))));continue
            if q not in depth or depth[q]>=depth[p]:continue
            chain=[p];cursor=p
            while cursor is not None and cursor!=q:cursor=parent[cursor];chain.append(cursor)
            if cursor==q and len(chain)>120:
                component.append(chain)
        # One coherent circuit per connected street component avoids placing
        # independent, conflicting traffic streams on the same lane.
        if component:cycles.append(max(component,key=len))
    return cycles


def collision_clear(points,mesh):
    """Swept center/side rays reject walls and low ceilings along a lane."""
    low,high=mesh.min(1),mesh.max(1)
    grid={};size=10.
    for i,(lo,hi) in enumerate(zip(low,high)):
        for x in range(int(np.floor(lo[0]/size)),int(np.floor(hi[0]/size))+1):
            for z in range(int(np.floor(lo[2]/size)),int(np.floor(hi[2]/size))+1):grid.setdefault((x,z),[]).append(i)
    nxt=np.roll(points,-1,axis=0);direction=nxt-points
    normals=np.column_stack((-direction[:,2],np.zeros(len(points)),direction[:,0]))
    normals/=np.maximum(np.linalg.norm(normals,axis=1)[:,None],1e-9)
    for i,(p,q) in enumerate(zip(points,nxt)):
        ids=set()
        for x in range(int(np.floor(min(p[0],q[0])/size))-1,int(np.floor(max(p[0],q[0])/size))+2):
            for z in range(int(np.floor(min(p[2],q[2])/size))-1,int(np.floor(max(p[2],q[2])/size))+2):ids.update(grid.get((x,z),()))
        ids=np.array(sorted(ids),dtype=int)
        if not len(ids):return False
        tris=mesh[ids]
        for side in (-1.4,0.,1.4):
            origin=p+normals[i]*side+[0.,1.,0.]
            delta=q-p
            edge1,edge2=tris[:,1]-tris[:,0],tris[:,2]-tris[:,0]
            cross=np.cross(delta,edge2);det=np.einsum('ij,ij->i',edge1,cross)
            good=np.abs(det)>1e-7
            inv=np.divide(1.,det,out=np.zeros_like(det),where=good)
            offset=origin-tris[:,0]
            u=np.einsum('ij,ij->i',offset,cross)*inv
            c=np.cross(offset,edge1)
            v=c@delta*inv;t=np.einsum('ij,ij->i',edge2,c)*inv
            hit=good&(u>=0)&(v>=0)&(u+v<=1)&(t>=0)&(t<=1)
            if hit.any():
                print('traffic survey collision',p.tolist(),q.tolist(),'side',side,'triangle',tris[np.flatnonzero(hit)[0]].tolist(),flush=True)
                return False
        ceiling=raster_hits(tris,p)
        hit=(ceiling>p[1]+.25)&(ceiling<p[1]+2.)
        if hit.any():
            print('traffic survey ceiling',p.tolist(),'triangle',tris[np.flatnonzero(hit)[0]].tolist(),flush=True)
            return False
    return True


def raster_hits(tris,p):
    a,b,c=tris[:,0],tris[:,1],tris[:,2]
    det=(b[:,2]-c[:,2])*(a[:,0]-c[:,0])+(c[:,0]-b[:,0])*(a[:,2]-c[:,2])
    good=np.abs(det)>1e-7;inv=np.divide(1.,det,out=np.zeros_like(det),where=good)
    u=((b[:,2]-c[:,2])*(p[0]-c[:,0])+(c[:,0]-b[:,0])*(p[2]-c[:,2]))*inv
    v=((c[:,2]-a[:,2])*(p[0]-c[:,0])+(a[:,0]-c[:,0])*(p[2]-c[:,2]))*inv
    return np.where(good&(u>=0)&(v>=0)&(u+v<=1),u*a[:,1]+v*b[:,1]+(1-u-v)*c[:,1],np.inf)


def boundary_cycles(raster):
    """Complete continuations around supported street islands, including caps.

    Used when barriers divide a street network into open strips. A circuit
    follows the two sides of a strip and rounds its supported end; it never
    creates a continuation beyond the geometry or through a collision barrier.
    """
    mask=np.isfinite(raster.y)
    for _ in range(2):
        padded=np.pad(mask,1)
        mask &= padded[:-2,1:-1]&padded[2:,1:-1]&padded[1:-1,:-2]&padded[1:-1,2:]
    edges={}
    for x,z in np.argwhere(mask):
        for dx,dz,a,b in ((0,-1,(x-.5,z-.5),(x+.5,z-.5)),
                          (1,0,(x+.5,z-.5),(x+.5,z+.5)),
                          (0,1,(x+.5,z+.5),(x-.5,z+.5)),
                          (-1,0,(x-.5,z+.5),(x-.5,z-.5))):
            xx,zz=x+dx,z+dz
            if xx<0 or zz<0 or xx>=mask.shape[0] or zz>=mask.shape[1] or not mask[xx,zz]:edges.setdefault(a,[]).append(b)
    cycles=[]
    while edges:
        start=min(edges);chain=[start];p=start
        while p in edges:
            q=edges[p].pop()
            if not edges[p]:del edges[p]
            if q==start:break
            chain.append(q);p=q
        if q==start and len(chain)>150:
            points=np.array(chain)
            # Raster corners lie between samples; recover their street floor
            # from the nearest supported corner rather than merging heights.
            points=np.floor(points+.5).astype(int)
            for i,ij in enumerate(points):
                x,z=ij
                if not np.isfinite(raster.y[x,z]):
                    candidates=[(xx,zz) for xx in (x-1,x) for zz in (z-1,z) if np.isfinite(raster.y[xx,zz])]
                    if candidates:points[i]=candidates[0]
            cycles.append(points.tolist())
    return cycles


def vehicle_geometry(stream,areas,mesh,helper,scratch):
    """The same Recast collision bake, with car clearance and street-only floors."""
    low,high=mesh.min(1),mesh.max(1);parts=[]
    pavement=np.concatenate((areas['road'],areas['crosswalk']))
    if not len(pavement):return np.empty((0,3,3),np.float32)
    plow,phigh=pavement.min(axis=(0,1)),pavement.max(axis=(0,1))
    for path in sorted(stream.iterdir()):
        if not path.name.startswith('cSim_') or not path.name.endswith('_high.xsf'):continue
        _,x,z,_=path.stem.split('_');x,z=int(x),int(z)
        if x+102.2<plow[0] or x-102.2>phigh[0] or z+102.2<plow[2] or z-102.2>phigh[2]:continue
        data,counts=bake_cell(mesh,low,high,(x,z),helper,scratch,np.ones(len(mesh),np.uint8),
                             water=areas['water'],pavement=pavement,vehicle=True)
        print(f"vehicle clearance: {stream.name}/{path.name} polygons={counts['polygons']}",flush=True)
        if data:
            for layers in read_rw_object(data):
                for layer in layers:
                    for p in layer.polygons:
                        for i in range(1,len(p.vertices)-1):parts.append([p.vertices[0],p.vertices[i],p.vertices[i+1]])
    return np.asarray(parts,np.float32).reshape(-1,3,3)


def route_curves(points):
    """Continuous center and boundary tangents for native Hermite lanes."""
    tangent=(np.roll(points,-1,axis=0)-np.roll(points,1,axis=0))/2
    # Native 82E13560 derives the surface normal from these boundaries.
    # Left is up cross forward. Reversing the sides creates a downward
    # road normal and flips the native vehicle placement frame.
    normal=np.column_stack((tangent[:,2],np.zeros(len(points)),-tangent[:,0]))
    normal/=np.maximum(np.linalg.norm(normal,axis=1)[:,None],1e-9)
    left,right=points+normal*1.75,points-normal*1.75
    return points,left,right,tangent,(np.roll(left,-1,axis=0)-np.roll(left,1,axis=0))/2,(np.roll(right,-1,axis=0)-np.roll(right,1,axis=0))/2


def curve_samples(points,count=9):
    positions=[]
    center,left,right,tangent,ld,rd=route_curves(points)
    for p,d in ((center,tangent),(left,ld),(right,rd)):
        q,e=np.roll(p,-1,axis=0),np.roll(d,-1,axis=0)
        t=np.linspace(0,1,count)
        positions.append((2*t**3-3*t**2+1)[None,:,None]*p[:,None]+(-2*t**3+3*t**2)[None,:,None]*q[:,None]+
                         (t**3-2*t**2+t)[None,:,None]*d[:,None]+(t**3-t**2)[None,:,None]*e[:,None])
    return positions


def native_lane_normals(points):
    """82E13560's diagonal cross product; detect folded native lane ribbons."""
    _,left,right,*_=route_curves(points)
    normals=np.cross(np.roll(right,-1,axis=0)-left,
                     np.roll(left,-1,axis=0)-right)
    lengths=np.linalg.norm(normals,axis=1)
    return normals/np.maximum(lengths[:,None],1e-12)


def refine_boundary_lane(lane,raster,core):
    """Keep turnaround ribbons on pavement at concave street corners.

    Smoothing a supported contour can cut a corner with its outer boundary,
    discarding an otherwise usable open-road circuit. Move adjacent centers
    away from unsupported boundary samples, taper the correction, then run
    the ordinary native-normal, slope, curve and collision qualification.
    No correction may leave the Recast floor or move more than 1.5 metres.
    Failed refinements retain the original candidate for normal rejection.
    """
    original=lane.copy()
    ts=np.linspace(0,1,9)
    for _ in range(20):
        sums=np.zeros_like(lane);weights=np.zeros(len(lane));bad_count=0
        def inspect(centers,sides):
            nonlocal bad_count
            for edge in sides:
                heights=raster.height(edge.reshape(-1,3)).reshape(len(lane),9)
                bad=~np.isfinite(heights)|(np.abs(heights-edge[:,:,1])>.6)
                bad_count+=int(bad.sum())
                for j,k in np.argwhere(bad):
                    delta=edge[j,k]-centers[j,k];delta[1]=0
                    length=np.linalg.norm(delta)
                    if length<.01:continue
                    push=-delta/length
                    for at,w in ((j,1-ts[k]),((j+1)%len(lane),ts[k])):
                        sums[at]+=push*w;weights[at]+=w
        curves=curve_samples(lane)
        inspect(curves[0],curves[1:])
        d=np.roll(lane,-1,axis=0)-lane
        n=np.column_stack((-d[:,2],np.zeros(len(lane)),d[:,0]))
        n/=np.maximum(np.linalg.norm(n,axis=1)[:,None],1e-9)
        line=lane[:,None]+ts[None,:,None]*d[:,None]
        inspect(line,[line-1.75*n[:,None],line+1.75*n[:,None]])
        if not bad_count:return lane
        delta=sums/np.maximum(weights[:,None],1)
        for _ in range(3):delta=(np.roll(delta,1,axis=0)+2*delta+np.roll(delta,-1,axis=0))/4
        lane=lane+.3*delta
        height=core.height(lane)
        if not np.isfinite(height).all():return original
        lane[:,1]=height
        if np.linalg.norm((lane-original)[:,[0,2]],axis=1).max()>1.5:return original
    return original


def driving_routes(areas,mesh,driveable=None,boundary=False):
    street=np.concatenate((areas['road'],areas['crosswalk']))
    if not len(street):return [],dict(candidates=0,rejected=0)
    if driveable is not None and not len(driveable):return [],dict(candidates=0,rejected=0)
    raster=StreetRaster(street);core=StreetRaster(driveable) if driveable is not None and len(driveable) else raster
    routes=[];cycles=boundary_cycles(core) if boundary else street_cycles(core);details=[]
    for cycle in cycles:
        ij=np.array(cycle);p=np.column_stack((ij[:,0]+core.origin[0],core.y[ij[:,0],ij[:,1]],ij[:,1]+core.origin[1])).astype(float)
        # Smooth the one-meter survey; evaluate its actual street support after
        # smoothing, rather than trusting a skeleton through a junction.
        for _ in range(36 if boundary else 12):p=(np.roll(p,1,axis=0)+2*p+np.roll(p,-1,axis=0))/4
        p=p[::4]
        d=np.roll(p,-1,axis=0)-np.roll(p,1,axis=0)
        normal=np.column_stack((-d[:,2],np.zeros(len(p)),d[:,0]))
        normal/=np.maximum(np.linalg.norm(normal,axis=1)[:,None],1e-9)
        before=len(routes)
        for sign in ((0,) if boundary else (1,-1,0)):
            if sign==0 and len(routes)>before:break
            lane=p+sign*2.1*normal
            if boundary:
                lane=core.project(lane)
                distinct=np.linalg.norm(lane-np.roll(lane,1,axis=0),axis=1)>.1
                lane=lane[distinct]
            height=core.height(lane)
            if not np.isfinite(height).all():
                details.append(dict(stage='center-support',point=lane[np.flatnonzero(~np.isfinite(height))[0]].tolist()));continue
            lane[:,1]=height
            if sign<0:lane=lane[::-1].copy()
            if boundary:lane=refine_boundary_lane(lane,raster,core)
            if (native_lane_normals(lane)[:,1]<np.cos(np.radians(20))).any():
                details.append(dict(stage='native-lane-normal',point=lane[0].tolist()));continue
            direction=np.roll(lane,-1,axis=0)-lane
            length=np.linalg.norm(direction,axis=1)
            if (length<.1).any() or (np.abs(direction[:,1])/length>.35).any():
                details.append(dict(stage='slope',max_slope=float((np.abs(direction[:,1])/length).max())));continue
            # Both boundaries, at intermediate samples, must stay on the same
            # street floor. This keeps lanes off sidewalks and stacked decks.
            normal=np.column_stack((-direction[:,2],np.zeros(len(lane)),direction[:,0]))
            normal/=np.maximum(np.linalg.norm(normal,axis=1)[:,None],1e-9)
            supported=True
            for t in np.linspace(0,1,9):
                sample=lane+t*direction
                for side in (-1.75,0.,1.75):
                    y=raster.height(sample+side*normal)
                    bad=~np.isfinite(y)|(np.abs(y-sample[:,1])>.6)
                    if bad.any():
                        details.append(dict(stage='edge-support',side=side,t=float(t),point=(sample+side*normal)[np.flatnonzero(bad)[0]].tolist()));supported=False;break
                if not supported:break
            if supported:
                curves=curve_samples(lane)
                for curve in curves:
                    samples=curve.reshape(-1,3);y=raster.height(samples)
                    if not np.isfinite(y).all() or (np.abs(y-samples[:,1])>.6).any():supported=False;break
                smooth=curves[0][:,:-1].reshape(-1,3)
                if supported and collision_clear(smooth,mesh):
                    routes.append(lane)
                    if sign==0:break
                else:details.append(dict(stage='curve-support-or-collision',point=lane[0].tolist()))
        # The center continuation is a fallback only when both offset lanes
        # cannot pass car clearance. Do not overlap it with a valid offset.
    if not routes and not boundary and driveable is not None:
        return driving_routes(areas,mesh,driveable,True)
    return routes,dict(candidates=len(cycles)*(1 if boundary else 3),
                       rejected=len(cycles)*(1 if boundary else 3)-len(routes),
                       boundary_continuations=boundary,details=details)


def rain_resource(routes,identity):
    """Native road/superroad records and 224-byte Hermite lane segments.

    Fixups 82962890/82962A40; constructor 82E13560; activation 826B1B28.
    Closed single-lane roads use native road-to-road continuations, including
    the closing segment. There are no synthetic traffic-light intersections.
    """
    nr=len(routes);ore=64;os=ore+nr*160
    raw=bytearray(os+nr*64)
    allpoints=np.concatenate(routes)
    struct.pack_into('>4f',raw,0,*allpoints.min(0),1.)
    struct.pack_into('>4f',raw,16,*allpoints.max(0),1.)
    struct.pack_into('>6I',raw,32,0,nr,nr,0,ore,os)
    for i,points in enumerate(routes):
        if (native_lane_normals(points)[:,1]<np.cos(np.radians(20))).any():
            raise ValueError('folded or unsupported native lane orientation')
        road=ore+i*160;sup=os+i*64;lane_at=len(raw)
        key=h64(identity+'/road/'+str(i));superkey=h64(identity+'/superroad/'+str(i))
        struct.pack_into('>Q',raw,road,key)
        for offset,side in ((8,0),(64,1)):
            struct.pack_into('>Q4I',raw,road+offset,key,1,side,1,0)
        center,left,right,tangent,ld,rd=route_curves(points)
        # Native 82E108E0 inverts distance along a segment through a table
        # of 16 cumulative metre distances at uniform curve parameters.
        # Count without a pointer makes it read guest address zero and
        # return the segment end instead of the requested position.
        samples=curve_samples(points,16)[0]
        distances=np.column_stack((np.zeros(len(points)),
            np.cumsum(np.linalg.norm(np.diff(samples,axis=1),axis=2),axis=1)))
        lengths=distances[:,-1];total=float(lengths.sum())
        struct.pack_into('>2I',raw,road+120,len(points),lane_at-road)
        struct.pack_into('>fI3fIQ',raw,road+128,8.333333,1,3.5,total,total,0,superkey)
        struct.pack_into('>QQIIQI2fIf3I',raw,sup,superkey,h64(identity+'/route/'+str(i)),
                         3,0,key,1,total,3.5,0,8.333333,1,2,0)
        cumulative=0.
        for j,length in enumerate(lengths):
            k=(j+1)%len(points);p,q=center[j],center[k]
            segment=bytearray(224)
            for at,vector in ((0,p),(16,q),(32,tangent[j]),(48,tangent[k]),(80,left[j]),(96,right[j]),
                              (112,left[k]),(128,right[k]),(144,ld[j]),(160,rd[j]),(176,ld[k]),(192,rd[k])):
                struct.pack_into('>4f',segment,at,*vector,0.)
            struct.pack_into('>fI',segment,64,float(length),16)
            # 82C36548 subtracts segment+64 from segment+208 to locate
            # the segment start along the superroad. Repeating the local
            # length here makes every spawn fail its 15m endpoint margin.
            cumulative+=float(length)
            struct.pack_into('>f',segment,208,cumulative)
            raw.extend(segment)
        for j,table in enumerate(distances):
            segment_at=lane_at+j*224
            struct.pack_into('>I',raw,segment_at+72,len(raw)-segment_at)
            raw.extend(struct.pack('>16f',*table))
    rain_footprints(raw)  # Independent strict geometry/offset reader.
    return bytes(raw)


def road_asset(template,routes,identity):
    arena,toc=template;objs=list(dictionary(arena));road,exports=objs[1:]
    raw=rain_resource(routes,identity)
    result=bytearray(arena[:road['pointer']]);result.extend(raw)
    result.extend(bytes((-len(result))%16));export_at=len(result)
    export=bytearray(arena[exports['pointer']:exports['pointer']+exports['size']])
    struct.pack_into('>Q',export,28,h64(identity+'/export'))
    result.extend(export);dictionary_at=len(result)
    dictionary_source=struct.unpack_from('>I',arena,48)[0]
    for i,obj in enumerate(objs):
        row=bytearray(arena[dictionary_source+i*24:dictionary_source+(i+1)*24])
        if i==1:struct.pack_into('>3I',row,0,road['pointer'],0,len(raw))
        if i==2:struct.pack_into('>I',row,0,export_at)
        result.extend(row)
    # Arena descriptor lengths in the compatible source header.
    replacements=0
    for at in range(0,road['pointer'],4):
        if struct.unpack_from('>I',arena,at)[0]==len(arena):
            struct.pack_into('>I',result,at,len(result));replacements+=1
    if replacements!=3:raise ValueError('unsupported RAIN arena size descriptors')
    struct.pack_into('>I',result,48,dictionary_at)
    if [o['kind'] for o in dictionary(result)]!=[0xeb0008,0xeb0013,0xeb000b]:
        raise ValueError('invalid generated road arena')
    return pack_asset(bytes(result),toc,identity)


def prepare_livingworld(stream,source,ocean_model,helper,scratch):
    name=stream.name
    if name not in STREAMS:raise ValueError('DLC actor preparation outside requested Classic streams')
    if any((stream/f'LIVINGWORLD_COMPLETE_V{v}').exists() for v in (1,2,3)):
        raise ValueError('DLC actors require fresh unpublished stream')
    schema=templates(source)
    areas=pedestrian_surface_triangles(stream,ocean_model,True);mesh=collision(stream)
    driveable=vehicle_geometry(stream,areas,mesh,helper,scratch)
    routes,survey=driving_routes(areas,mesh,driveable)
    if not routes:raise ValueError(f'{name}: no supported closed vehicle lanes; inspect street survey')
    cmap_path,toc_path=stream/(name+'_Sim.xsm'),stream/(name+'_Sim.xst')
    cmap,toc=cmap_path.read_bytes(),toc_path.read_bytes()
    cells,rows=records(cmap,b'CMAP',32,104),records(toc,b'ATOC',24,64)
    paint=census_tiles(mesh,[r[28:92].split(b'\0')[0].decode() for r in cells
                            if r[28:92].split(b'\0')[0].decode().endswith('_high')])
    known={guid(r) for r in rows};paint_cells=0
    for row in cells:
        cell=row[28:92].split(b'\0')[0].decode();path=stream/(cell+'.xsf')
        additions=[]
        if cell=='cSim_Global':
            additions.append(road_asset(schema['road'],routes,'nsv_dlc/'+name+'/traffic'))
        elif cell.endswith('_high'):
            for center in paint[cell]:
                identity='nsv_dlc/'+name+'/census/'+str(center[0])+'/'+str(center[1])
                additions.append(census_asset(schema['paint'],identity,center))
            if additions:paint_cells+=1
        if not additions:continue
        sf=bytearray(path.read_bytes());count=len(list(assets(sf,include_resource=False)))
        if struct.unpack_from('>3I',row,16)!=(128,len(sf)-128,count):raise ValueError('DLC cell range mismatch')
        for asset,entry in additions:
            if guid(entry) in known:raise ValueError('DLC actor asset identity collision')
            known.add(guid(entry));rows.append(entry);sf.extend(asset);count+=1
        struct.pack_into('>I',sf,24,count);struct.pack_into('>3I',row,16,128,len(sf)-128,count)
        path.write_bytes(sf)
    cmap_path.write_bytes(make_catalog(cmap[:32],cells));toc_path.write_bytes(make_catalog(toc[:24],rows))
    result=dict(version=4,stream=name,census_cells=paint_cells,npc_value=32,vehicle_value=247,
                census_tiles=sum(map(len,paint.values())),census_grid=128,
                source_census=False,source_traffic=False,street_survey=survey,
                roads=len(routes),superroads=len(routes),lane_segments=sum(len(r) for r in routes),
                routes=[r.tolist() for r in routes],traffic_audio='shared original 10 banks',
                pedestrian_speech='shared original bank4',traffic_distance='cumulative-segment-end',
                traffic_curves='native-left-up-and-distance-table-v3',
                traffic_routes='supported-boundary-refinement-v4')
    (stream/'livingworld.json').write_bytes((json.dumps(result,indent=2)+'\n').encode())
    (stream/'LIVINGWORLD_COMPLETE_V4').write_bytes(f'NSV_DLC_LIVINGWORLD_V4\n{paint_cells} {len(routes)}\n'.encode())
    return result
