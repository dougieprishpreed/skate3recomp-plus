"""Prepare population paint and native traffic for the three Classic DLC streams.

Original DLC has neither WorldPainter census nor RAIN. Pedestrians use the
shared Recast/NavPower bake. Traffic is derived separately from connected,
authored street surfaces; open map edges are never invented as destinations.
Only unpublished streams are modified. Source archives are read-only.
"""
import json
import mmap
import struct
from pathlib import Path

import numpy as np

from inspect_sfil import assets, dictionary, materials, resolve_reference
from world_archive import entries, decode_archive_payload
from build_skate2_world_support import records, make_catalog, guid
from skate2_pedestrian_areas import pedestrian_surface_triangles, rain_footprints
from skate2_navigation import collision, bake_cell
from navpower_resource import read_rw_object
from vlt64 import h64
import skate2_dlc_roads as roads

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


def rendered_triangles(stream):
    """Every drawn static triangle of the high cells and Global (sky and LOD proxies excluded)."""
    parts=[];seen=set()
    for path in sorted(p for p in stream.iterdir() if p.name.startswith('cPres_') and
                       (p.name.endswith('_high.xsf') or p.name=='cPres_Global.xsf')):
        for ident,_,(arena,res) in assets(path.read_bytes()):
            if ident in seen:continue
            seen.add(ident);objects=list(dictionary(arena));mats=dict(materials(arena))
            for obj in objects:
                if obj['kind']!=0xeb0023:continue
                at=obj['pointer']
                _,offset=resolve_reference(arena,struct.unpack_from('>I',arena,at+36)[0])
                shader=mats[(offset-20)//12].get('AttribulatorMaterialName',{}).get('text','').lower()
                if shader.startswith(('sky.','proxyworld')):continue
                declaration=objects[struct.unpack_from('>I',arena,at+40)[0]]['pointer']
                stride=arena[declaration+(struct.unpack_from('>H',arena,declaration+8)[0]+1)*16]
                if struct.unpack_from('>I',arena,declaration+20)[0]&63!=57 or stride<12:continue
                vb=objects[struct.unpack_from('>I',arena,at+52)[0]-1];ib=objects[struct.unpack_from('>I',arena,at+48)[0]-1]
                vertices=np.ndarray((vb['size']//stride,3),'>f4',buffer=res,offset=vb['pointer'],strides=(stride,4)).astype(np.float32)
                indices=np.frombuffer(res,'>u2',ib['size']//2,ib['pointer'])
                draws,relative=struct.unpack_from('>I',arena,at+56)[0],struct.unpack_from('>I',arena,at+68)[0]
                for i in range(draws):
                    # Xenos primitives: 4 list, 5 fan, 6 strip (winding is irrelevant here).
                    primitive,base,start,count=struct.unpack_from('>4I',arena,at+relative+i*16)
                    seq=indices[start:start+count].astype(np.int64)+base
                    if primitive==4:idx=seq[:len(seq)//3*3].reshape(-1,3)
                    elif primitive==6 and len(seq)>=3:idx=np.stack((seq[:-2],seq[1:-1],seq[2:]),1)
                    elif primitive==5 and len(seq)>=3:idx=np.stack((np.full(len(seq)-2,seq[0]),seq[1:-1],seq[2:]),1)
                    else:continue
                    if len(idx) and idx.max()<len(vertices):parts.append(vertices[idx])
    return np.concatenate(parts) if parts else np.empty((0,3,3),np.float32)


def visible_collision(mesh,tags,rendered,reach=.3):
    """Collision a driver can see: tagged surfaces, and tag-0 faces with a drawn surface on them.

    Tag 0 marks the invisible walls that hold the skater inside the play
    area, but the same tag is also used on the collision of drawn props such
    as construction fencing. A tag-0 face counts as solid when at least half
    of its sample points lie within `reach` of a drawn triangle.
    """
    visible=tags[:,1]!=0
    hidden=np.flatnonzero(~visible)
    if not len(hidden) or not len(rendered):return visible
    tris=mesh[hidden].astype(np.float64)
    span=np.maximum.reduce([np.linalg.norm(tris[:,1]-tris[:,0],axis=1),np.linalg.norm(tris[:,2]-tris[:,0],axis=1),
                            np.linalg.norm(tris[:,2]-tris[:,1],axis=1)])
    steps=np.clip(np.ceil(span/.5).astype(int),1,100);points,owner=[],[]
    for n in np.unique(steps):
        sel=np.flatnonzero(steps==n)
        u,v=np.meshgrid(np.linspace(0,1,n+1),np.linspace(0,1,n+1),indexing='ij');keep=u+v<=1+1e-9
        u,v=u[keep],v[keep];t=tris[sel]
        points.append((t[:,None,0]+u[None,:,None]*(t[:,None,1]-t[:,None,0])+v[None,:,None]*(t[:,None,2]-t[:,None,0])).reshape(-1,3))
        owner.append(np.repeat(sel,len(u)))
    points,owner=np.concatenate(points),np.concatenate(owner)
    face=np.cross(tris[:,1]-tris[:,0],tris[:,2]-tris[:,0])
    face/=np.maximum(np.linalg.norm(face,axis=1),1e-12)[:,None]
    size=2.;cells=np.floor(points[:,[0,2]]/size).astype(np.int64)
    order=np.lexsort((cells[:,1],cells[:,0]));cells,points,owner=cells[order],points[order],owner[order]
    pnormal=face[owner]
    wanted={tuple(c) for c in np.unique(cells,axis=0)}
    drawn=rendered.astype(np.float64)
    lo=np.floor((drawn.min(1)[:,[0,2]]-reach)/size).astype(np.int64);hi=np.floor((drawn.max(1)[:,[0,2]]+reach)/size).astype(np.int64)
    bucket={}
    for k in np.flatnonzero(((hi-lo)<4096).all(1)):
        for x in range(lo[k,0],hi[k,0]+1):
            for z in range(lo[k,1],hi[k,1]+1):
                if (x,z) in wanted:bucket.setdefault((x,z),[]).append(k)
    normal=np.cross(drawn[:,1]-drawn[:,0],drawn[:,2]-drawn[:,0])
    area=np.linalg.norm(normal,axis=1);good=area>1e-9
    normal=np.divide(normal,area[:,None],out=np.zeros_like(normal),where=good[:,None])
    hit=np.zeros(len(points),bool)
    bounds=np.flatnonzero(np.any(np.diff(cells,axis=0)!=0,axis=1))+1
    for s0,s1 in zip(np.r_[0,bounds],np.r_[bounds,len(points)]):
        ids=bucket.get(tuple(cells[s0]))
        if not ids:continue
        ids=np.array(ids);ids=ids[good[ids]];t,nn,p=drawn[ids],normal[ids],points[s0:s1]
        rel=p[:,None,:]-t[None,:,0]
        d=np.einsum('pti,ti->pt',rel,nn)
        q=rel-d[...,None]*nn[None]
        e0,e1=t[:,1]-t[:,0],t[:,2]-t[:,0]
        d00,d01,d11=(e0*e0).sum(1),(e0*e1).sum(1),(e1*e1).sum(1);den=d00*d11-d01*d01
        d20,d21=np.einsum('pti,ti->pt',q,e0),np.einsum('pti,ti->pt',q,e1)
        bv=(d11*d20-d01*d21)/den;bw=(d00*d21-d01*d20)/den;bu=1-bv-bw
        # Inside the drawn triangle, or within `reach` of it across an edge.
        slack=reach/np.sqrt(np.maximum(den/np.maximum(np.maximum(d00,d11),1e-12),1e-12))
        inside=(bu>=-slack)&(bv>=-slack)&(bw>=-slack)
        # The drawn surface must lie along the face (not the road it stands on).
        along=np.abs(pnormal[s0:s1]@nn.T)>.7
        hit[s0:s1]=(inside&along&(np.abs(d)<=reach)).any(1)
    share=np.bincount(owner,hit,len(hidden))/np.maximum(np.bincount(owner,minlength=len(hidden)),1)
    visible=visible.copy();visible[hidden[share>=.5]]=True
    return visible


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


def lane_support_failures(lane,raster,core):
    """Segments failing the existing native ribbon/floor qualification."""
    direction=np.roll(lane,-1,axis=0)-lane
    length=np.linalg.norm(direction,axis=1)
    bad=(length<.1)|(np.abs(direction[:,1])/np.maximum(length,1e-9)>.35)
    bad|=native_lane_normals(lane)[:,1]<np.cos(np.radians(20))
    for curve in curve_samples(lane,17):
        y=raster.height(curve.reshape(-1,3)).reshape(len(lane),17)
        bad|=(~np.isfinite(y)|(np.abs(y-curve[:,:,1])>.6)).any(1)
    normals=np.column_stack((-direction[:,2],np.zeros(len(lane)),direction[:,0]))
    normals/=np.maximum(np.linalg.norm(normals,axis=1)[:,None],1e-9)
    for t in np.linspace(0,1,17):
        center=lane+t*direction
        for side in (-1.75,0.,1.75):
            y=raster.height(center+side*normals)
            bad|=~np.isfinite(y)|(np.abs(y-center[:,1])>.6)
    bad|=~np.isfinite(core.height(lane))
    return bad


def lane_turn_metrics(lane):
    points=curve_samples(lane,17)[0][:,:-1].reshape(-1,3)
    direction=np.roll(points,-1,axis=0)-points
    heading=np.arctan2(direction[:,0],direction[:,2])
    change=(np.roll(heading,-1)-heading+np.pi)%(2*np.pi)-np.pi
    curvature=np.abs(change)/np.maximum(np.linalg.norm(direction[:,[0,2]],axis=1),.01)
    return float(np.abs(change).sum()),float(np.quantile(curvature,.95)),float(curvature.max())


def separate_circuits(routes):
    """Reject boundary alternatives whose 3.5m lanes cross or double back.

    Local neighbours on the same circuit are expected to touch. Compare
    portions at least 20m apart along the circuit, including its closing
    seam, and every pair of independent circuits. This is a bake check;
    vehicle following and collision responses remain native.
    """
    samples=[curve_samples(r)[0][:,:-1].reshape(-1,3) for r in routes]
    for i,s in enumerate(samples):
        arc=np.r_[0,np.cumsum(np.linalg.norm(np.diff(s,axis=0),axis=1))]
        total=arc[-1]+np.linalg.norm(s[-1]-s[0])
        for at in range(0,len(s),64):
            d=s[at:at+64,None]-s[None]
            gap=np.abs(arc[at:at+64,None]-arc[None]);gap=np.minimum(gap,total-gap)
            if ((np.einsum('ijk,ijk->ij',d,d)<3.5**2)&(gap>=20)).any():return False
            for t in samples[:i]:
                d=s[at:at+64,None]-t[None]
                if (np.einsum('ijk,ijk->ij',d,d)<3.5**2).any():return False
    return True


def driving_routes(areas,mesh,driveable=None,visible=None):
    """Two-way, keep-right traffic circuits on the authored street network.

    skate2_dlc_roads reduces the street floor to a road graph and joins one
    lane per direction with the right-hand face rule (see its docstring).
    Every circuit must then pass the native qualification: supported 3.5m
    ribbon on street, upright native normals, slope, visible wall/ceiling
    clearance and separation from every other circuit. `visible` selects the
    collision that blocks traffic (see visible_collision): drawn walls and
    barriers block, undrawn skater-only faces do not.
    """
    street=np.concatenate((areas['road'],areas['crosswalk']))
    if not len(street) or driveable is None or not len(driveable):
        return [],dict(selection='road-network-v9',roads=0,circuits=0)
    def pave(floor):return roads.FloorRaster(floor,np.where(floor.mask,floor.y,floor.street.y))
    def check(points,floor):return lane_support_failures(points,pave(floor),roads.FloorRaster(floor,floor.y))
    routes,survey,floor,_=roads.plan(street,driveable,mesh,visible,check)
    solid=mesh if visible is None else mesh[visible]
    core=roads.FloorRaster(floor,floor.y)
    for i,lane in enumerate(routes):
        where=f'traffic circuit {i} near {lane[0,[0,2]].round(1).tolist()}'
        if lane_support_failures(lane,pave(floor),core).any():raise ValueError(where+': unsupported lane ribbon')
        if (native_lane_normals(lane)[:,1]<np.cos(np.radians(20))).any():raise ValueError(where+': folded lane')
        direction=np.roll(lane,-1,axis=0)-lane
        if (np.abs(direction[:,1])/np.maximum(np.linalg.norm(direction,axis=1),1e-9)>.35).any():raise ValueError(where+': too steep')
        if not collision_clear(curve_samples(lane,17)[0][:,:-1].reshape(-1,3),solid):raise ValueError(where+': wall or ceiling')
    if routes and not separate_circuits(routes):raise ValueError('traffic circuits overlap')
    survey.update(selection='road-network-v9',
                  turn_degrees=[round(float(np.degrees(lane_turn_metrics(r)[0])),1) for r in routes])
    return routes,survey


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
    if any((stream/f'LIVINGWORLD_COMPLETE_V{v}').exists() for v in range(1,10)):
        raise ValueError('DLC actors require fresh unpublished stream')
    schema=templates(source)
    areas=pedestrian_surface_triangles(stream,ocean_model,True);mesh,tags=collision(stream,True)
    driveable=vehicle_geometry(stream,areas,mesh,helper,scratch)
    routes,survey=driving_routes(areas,mesh,driveable,visible_collision(mesh,tags,rendered_triangles(stream)))
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
    result=dict(version=9,stream=name,census_cells=paint_cells,npc_value=32,vehicle_value=247,
                census_tiles=sum(map(len,paint.values())),census_grid=128,
                source_census=False,source_traffic=False,street_survey=survey,
                roads=len(routes),superroads=len(routes),lane_segments=sum(len(r) for r in routes),
                routes=[r.tolist() for r in routes],traffic_audio='shared original 10 banks',
                pedestrian_speech='shared original bank4',traffic_distance='cumulative-segment-end',
                traffic_curves='native-left-up-and-distance-table-v3',
                traffic_routes='road-network-v9')
    (stream/'livingworld.json').write_bytes((json.dumps(result,indent=2)+'\n').encode())
    (stream/'LIVINGWORLD_COMPLETE_V9').write_bytes(f'NSV_DLC_LIVINGWORLD_V9\n{paint_cells} {len(routes)}\n'.encode())
    return result
