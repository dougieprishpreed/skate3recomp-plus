"""Offline DLC actor format/clearance regressions. Never runs the game."""
import argparse
import struct
import tempfile
import unittest
from pathlib import Path
import numpy as np
from skate2_dlc_livingworld import rain_resource,StreetRaster,prepare_livingworld,driving_routes,curve_samples,census_tiles,native_lane_normals,refine_boundary_lane
from skate2_pedestrian_areas import area_material,rain_footprints
from skate2_navigation import bake_cell
from navpower_resource import read_rw_object
from test_nsv_navigation import contains

BAKER=None

def floor(x0,z0,x1,z1,y=0.):
    a,b,c,d=(x0,y,z0),(x0,y,z1),(x1,y,z1),(x1,y,z0)
    return [((a,b,c)),((a,c,d))]


class DlcTests(unittest.TestCase):
    def test_native_census_grid_covers_positive_negative_and_stream_seams(self):
        mesh=np.array(floor(-220,-40,250,210),np.float32)
        paint=census_tiles(mesh,['cSim_-100_100_high','cSim_100_100_high','cSim_300_100_high'])
        centers=[c for cells in paint.values() for c in cells]
        keys={(int((x-64)/128),int((z-64)/128)) for x,z in centers}
        self.assertEqual(len(centers),len(keys))
        for x in (-219.,-200.,-128.,-.01,0.,127.99,128.,200.,249.):
            for z in (-39.,0.,128.,200.,209.):
                key=tuple(np.floor(np.array([x,z])/128).astype(int))
                self.assertIn(key,keys)
                center=np.array(key)*128+64
                self.assertTrue((abs(np.array([x,z])-center)<=64).all())

    def test_classic_roles_do_not_change_city(self):
        for diffuse,kind in [('suburb_roadway_roadcenter_d','road'),
                             ('suburb_roadway_roadedge_d','road'),
                             ('suburb_roadway_intersection_center_d','road'),
                             ('suburb_road_crosswalk2_d','crosswalk'),
                             ('road_center_crosswalk_full_d','crosswalk'),
                             ('road_base_intersectionx02_d','road'),
                             ('suburb_sidewalk_tile_d','sidewalk')]:
            channels={'diffuse':{'text':diffuse}}
            self.assertEqual(area_material(channels,True),kind)
            if not diffuse.startswith('road_center_'):self.assertIsNone(area_material(channels))
        for diffuse in ('asphalt_green','wood_whitegaragedoor01_d','suburb_roadway_90degreecurbcut_d'):
            self.assertIsNone(area_material({'diffuse':{'text':diffuse}},True))

    def test_native_traffic_offsets_continuations_and_identity(self):
        points=np.array([(20*np.cos(a),0.,20*np.sin(a)) for a in np.linspace(0,2*np.pi,80,endpoint=False)])
        first=rain_resource([points,points[::-1]],'one')
        second=rain_resource([points],'two')
        ni,nr,ns,oi,ore,os=struct.unpack_from('>6I',first,32)
        self.assertEqual((ni,nr,ns,oi),(0,2,2,0))
        self.assertNotEqual(first[ore:ore+8],second[64:72])
        for i in range(nr):
            road=ore+i*160;sup=os+i*64
            key=struct.unpack_from('>Q',first,road)[0]
            for offset,side in ((8,0),(64,1)):
                self.assertEqual(struct.unpack_from('>Q4I',first,road+offset),(key,1,side,1,0))
                self.assertEqual(first[road+offset+24:road+offset+56],bytes(32))
            count,rel=struct.unpack_from('>2I',first,road+120)
            self.assertEqual(count,80)
            self.assertEqual(first[road+152:road+160],first[sup:sup+8])
            self.assertEqual(struct.unpack_from('>I',first,sup+52)[0],1)
            self.assertEqual(struct.unpack_from('>I',first,sup+56)[0]&1,0)
            self.assertAlmostEqual(struct.unpack_from('>f',first,sup+48)[0],8.333333,places=5)
            lane=road+rel
            cumulative=0.;spawnable=0
            total=struct.unpack_from('>f',first,sup+36)[0]
            for j in range(count):
                at=lane+j*224;next_at=lane+((j+1)%count)*224
                length=struct.unpack_from('>f',first,at+64)[0]
                end=struct.unpack_from('>f',first,at+208)[0]
                cumulative+=length
                self.assertAlmostEqual(end,cumulative,delta=.00005)
                # Actual native factory predicate (82C36528..82C3656C).
                start=end-length+struct.unpack_from('>f',first,road+144)[0]-struct.unpack_from('>f',first,road+140)[0]
                spawnable+=15<=start<=total-15
                self.assertEqual(first[at+16:at+28],first[next_at:next_at+12])
                for end,start in ((48,32),(112,80),(128,96),(176,144),(192,160)):
                    self.assertEqual(first[at+end:at+end+12],first[next_at+start:next_at+start+12])
                table_count,table_offset=struct.unpack_from('>2I',first,at+68)
                self.assertEqual(table_count,16)
                self.assertGreater(table_offset,0)
                table=np.array(struct.unpack_from('>16f',first,at+table_offset))
                self.assertEqual(table[0],0.)
                self.assertAlmostEqual(table[-1],length,places=6)
                self.assertTrue((np.diff(table)>0).all())
                # Uniform parameter samples store cumulative metres, not fractions.
                center=curve_samples(points,16)[0][j]
                expected=np.r_[0.,np.cumsum(np.linalg.norm(np.diff(center,axis=0),axis=1))]
                np.testing.assert_allclose(table,expected,rtol=1e-6,atol=1e-6)
                self.assertGreater(struct.unpack_from('>f',first,at+64)[0],0.)
            self.assertGreater(spawnable,count//2)
            self.assertAlmostEqual(cumulative,total,delta=.00005)
            self.assertTrue((native_lane_normals(points)[:,1]>.99).all())
        self.assertEqual(len(rain_footprints(first)),640)
        for curve in curve_samples(points):
            np.testing.assert_allclose(curve[:,-1],np.roll(curve[:,0],-1,axis=0),atol=1e-7)

    def test_native_left_right_winding_and_folded_lane_rejection(self):
        for direction in (1,-1):
            points=np.array([(20*np.cos(a),-60.,20*np.sin(a)) for a in np.linspace(0,2*np.pi,80,endpoint=False)])[::direction]
            self.assertTrue((native_lane_normals(points)[:,1]>.99).all())
        # A tight closing spike folds a 3.5m ribbon despite finite center points.
        points=np.array([[0.,0.,0.],[0.,0.,10.],[10.,0.,10.],[10.,0.,0.],[-1.,0.,2.]])
        with self.assertRaisesRegex(ValueError,'native lane orientation'):
            rain_resource([points],'folded')

    def test_missing_vehicle_clearance_never_falls_back_to_art(self):
        roads=np.array(floor(0,0,20,20),np.float32)
        routes,_=driving_routes({'road':roads,'crosswalk':np.empty((0,3,3))},roads,np.empty((0,3,3)))
        self.assertEqual(routes,[])
        with self.assertRaises(ValueError):prepare_livingworld(Path('DLC_MaloofMoneyCup'),None,None,None,None)

    def test_turnaround_corner_refinement_retains_supported_ribbon(self):
        raster=StreetRaster(np.array(floor(-40,-10,40,10),np.float32))
        a=np.linspace(0,2*np.pi,100,endpoint=False)
        lane=np.column_stack((30*np.cos(a),np.zeros(len(a)),7*np.sin(a)+2*np.maximum(np.sin(a),0)**8))
        old=curve_samples(lane)
        self.assertTrue(any(np.isinf(raster.height(c.reshape(-1,3))).any() for c in old))
        refined=refine_boundary_lane(lane,raster,raster)
        self.assertLessEqual(np.linalg.norm((refined-lane)[:,[0,2]],axis=1).max(),1.5)
        self.assertFalse(np.array_equal(lane,refined))
        for c in curve_samples(refined):
            self.assertTrue(np.isfinite(raster.height(c.reshape(-1,3))).all())
        self.assertTrue((native_lane_normals(refined)[:,1]>.99).all())
        rain_resource([refined],'turnaround')
        # Already qualified routes are returned without changing a vertex.
        np.testing.assert_array_equal(refine_boundary_lane(refined,raster,raster),refined)

    def test_turnaround_refinement_does_not_invent_missing_street(self):
        raster=StreetRaster(np.array(floor(-40,-1,40,1),np.float32))
        a=np.linspace(0,2*np.pi,100,endpoint=False)
        lane=np.column_stack((30*np.cos(a),np.zeros(len(a)),.5*np.sin(a)))
        np.testing.assert_array_equal(refine_boundary_lane(lane,raster,raster),lane)

    def test_stacked_streets_do_not_mix_floor_heights(self):
        raster=StreetRaster(np.array(floor(0,0,10,10,0.)+floor(0,0,10,10,10.),np.float32))
        self.assertEqual(raster.height(np.array([[5.,0.,5.]]))[0],0.)
        self.assertTrue(np.isinf(raster.height(np.array([[20.,0.,5.]]))[0]))

    def test_vehicle_bake_street_only_collision_and_headroom(self):
        street=np.array(floor(-20,-20,20,20),np.float32)
        mesh=np.array(floor(-40,-40,40,40)+floor(-8,-8,8,8,1.7),np.float32)
        with tempfile.TemporaryDirectory(prefix='nsv-dlc-') as tmp:
            data,_=bake_cell(mesh,mesh.min(1),mesh.max(1),(0,0),BAKER,Path(tmp),
                            np.ones(len(mesh),np.uint8),pavement=street,vehicle=True)
        self.assertIsNotNone(data)
        polygons=[p for layers in read_rw_object(data) for layer in layers for p in layer.polygons]
        for p in polygons:
            center=np.array(p.vertices).mean(0)
            self.assertLessEqual(abs(center[0]),20.)
            self.assertLessEqual(abs(center[2]),20.)
            self.assertFalse(abs(center[0])<8. and abs(center[2])<8.)

    def test_vehicle_bake_allows_crosswalk_and_pedestrian_bake_excludes_road(self):
        mesh=np.array(floor(-40,-40,40,40),np.float32)
        road=np.array(floor(-20,-20,20,20),np.float32)
        crossing=np.array(floor(-5,-20,5,20),np.float32)
        with tempfile.TemporaryDirectory(prefix='nsv-dlc-') as tmp:
            for vehicle in (False,True):
                data,_=bake_cell(mesh,mesh.min(1),mesh.max(1),(0,0),BAKER,Path(tmp),
                                np.ones(len(mesh),np.uint8),pavement=road,crossings=crossing,vehicle=vehicle)
                polygons=[p for layers in read_rw_object(data) for layer in layers for p in layer.polygons]
                self.assertTrue(any(contains(p,0,0) for p in polygons))
                self.assertEqual(any(contains(p,-10,0) for p in polygons),vehicle)
                self.assertEqual(any(contains(p,30,0) for p in polygons),not vehicle)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--baker',type=Path,required=True)
    args,extra=ap.parse_known_args();BAKER=args.baker.resolve()
    unittest.main(argv=[__file__]+extra)
