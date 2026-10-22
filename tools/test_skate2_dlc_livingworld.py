"""Offline DLC actor format/clearance regressions. Never runs the game."""
import argparse
import struct
import tempfile
import unittest
from pathlib import Path
import numpy as np
from skate2_dlc_livingworld import rain_resource,StreetRaster,prepare_livingworld,driving_routes,curve_samples,census_tiles,native_lane_normals,separate_circuits,visible_collision
import skate2_dlc_roads as roads
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

    def test_road_network_keeps_right_turns_at_junctions_and_skips_alleys(self):
        # A 12m-wide ring road round one block, a long dead-end road and a
        # short side alley. Wall faces stand along the far curbs only.
        street=np.array(floor(0,0,120,12)+floor(0,108,120,120)+floor(0,12,12,108)+
                        floor(108,12,120,108)+floor(120,54,260,66)+floor(54,120,66,160),np.float32)
        areas={'road':street,'crosswalk':np.empty((0,3,3),np.float32)}
        routes,survey=driving_routes(areas,street,street)
        self.assertEqual(survey['selection'],'road-network-v9')
        self.assertEqual((survey['circuits'],survey['dead_ends'],survey['alleys']),(2,1,1))
        self.assertTrue(separate_circuits(routes))
        points=np.concatenate(routes)
        # The alley is never entered; the long road is driven to its end.
        self.assertFalse(((points[:,2]>125)&(np.abs(points[:,0]-60)<8)).any())
        self.assertGreater(points[:,0].max(),245)
        # Keep right like the original RAIN: on the bottom road a lane heading
        # +x sits 2m right of the centerline (+z) with the opposing lane 4m on
        # its left (up x forward = -z).
        for lane in routes:
            d=np.roll(lane,-1,axis=0)-lane
            on=(np.abs(lane[:,0]-60)<20)&(lane[:,2]<12)
            for p,v in zip(lane[on],d[on]):
                v=v[[0,2]]/np.linalg.norm(v[[0,2]])
                if v[0]>.99:self.assertAlmostEqual(p[2],8.,delta=.6)
                if v[0]<-.99:self.assertAlmostEqual(p[2],4.,delta=.6)
            self.assertTrue((native_lane_normals(lane)[:,1]>.99).all())
            rain_resource([lane],'ring')

    def test_median_divides_carriageways_instead_of_making_a_loop(self):
        # The ring's bottom road is 20m wide with a 3m median down its middle.
        street=np.array(floor(0,0,120,8.5)+floor(0,11.5,120,20)+floor(0,108,120,120)+
                        floor(0,20,12,108)+floor(108,20,120,108)+floor(0,8.5,12,11.5)+
                        floor(108,8.5,120,11.5),np.float32)
        areas={'road':street,'crosswalk':np.empty((0,3,3),np.float32)}
        routes,survey=driving_routes(areas,street,street)
        self.assertEqual(survey['circuits'],2)
        for lane in routes:
            on=(np.abs(lane[:,0]-60)<30)&(lane[:,2]<20)
            self.assertTrue(on.any())
            # Each lane keeps its ribbon clear of the median (z 8.5..11.5).
            self.assertTrue((np.abs(lane[on,2]-10)>=1.5+1.75-.05).all())

    def test_drawn_barriers_block_and_undrawn_skater_walls_do_not(self):
        # Two tag-0 walls across a street: a jersey barrier with a drawn mesh
        # on it, and an undrawn skater-only wall.
        def wall(x):return np.array([[[x,0,0],[x,1.2,0],[x,0,12]],[[x,1.2,0],[x,1.2,12],[x,0,12]]],np.float32)
        street=np.array(floor(0,0,100,12),np.float32)
        mesh=np.concatenate((street,wall(30),wall(70)))
        tags=np.zeros((len(mesh),2),int);tags[:len(street),1]=7
        drawn=wall(30)+np.array([0.05,0,0],np.float32)
        visible=visible_collision(mesh,tags,np.concatenate((street,drawn)))
        self.assertTrue(visible[:len(street)].all())
        self.assertEqual(visible[len(street):].tolist(),[True,True,False,False])

    def test_barrier_pinch_closes_a_road(self):
        # A 12m street with a barrier leaving a 3m gap at x=300: each side is
        # its own dead-end road, never driven through the gap.
        street=np.array(floor(0,0,600,12),np.float32)
        survey=np.array(floor(0,0,299,12)+floor(299,0,301,3)+floor(301,0,600,12),np.float32)
        barrier=np.array([[[300,0,3],[300,1.2,3],[300,0,12]],[[300,1.2,3],[300,1.2,12],[300,0,12]]],np.float32)
        mesh=np.concatenate((street,barrier))
        routes,survey_report=driving_routes({'road':street,'crosswalk':np.empty((0,3,3),np.float32)},mesh,survey)
        self.assertEqual(survey_report['circuits'],2)
        for lane in routes:
            self.assertTrue((lane[:,0]<300).all() or (lane[:,0]>300).all())

    def test_invisible_boundary_walls_do_not_close_a_street(self):
        street=np.array(floor(0,0,100,12),np.float32)
        # The clearance survey stops 3m either side of a wall across x=50.
        survey=np.array(floor(0,0,47,12)+floor(53,0,100,12),np.float32)
        wall=np.array([[[50,0,0],[50,4,0],[50,0,12]],[[50,4,0],[50,4,12],[50,0,12]]],np.float32)
        mesh=np.concatenate((street,wall))
        probe=[50,6]
        for visible,expect in ((np.r_[np.ones(len(street),bool),False,False],True),
                               (np.ones(len(mesh),bool),False)):
            f=roads.Floor(street,survey,mesh,visible)
            self.assertEqual(bool(f.lookup(f.mask,np.array([probe]))[0]),expect)
            self.assertTrue(f.lookup(f.mask,np.array([[20.,6.]]))[0])

    def test_independent_circuits_reject_crossing_and_overlapping_lanes(self):
        angles=np.linspace(0,2*np.pi,100,endpoint=False)
        loop=np.column_stack((20*np.cos(angles),np.zeros(len(angles)),20*np.sin(angles)))
        self.assertTrue(separate_circuits([loop]))
        self.assertTrue(separate_circuits([loop,loop+[50.,0.,0.]]))
        self.assertFalse(separate_circuits([loop,loop+[10.,0.,0.]]))
        self.assertFalse(separate_circuits([loop,loop[::-1]]))

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
