"""Offline navigation geometry tests. Never starts the game.

Run: python tools/test_nsv_navigation.py --baker path/to/nsv_nav_bake.exe
"""
import argparse
from collections import deque
from pathlib import Path
import tempfile
import struct
import subprocess
import unittest

import numpy as np

from navpower_resource import read_rw_object, read_resource, write_resource
from skate2_navigation import bake_cell, read_bake, exclude_water_polygons
from skate2_pedestrian_areas import pedestrian_floors, rain_footprints, area_material, sidewalk_shoulders
from skate2_collision import clustered_mesh

BAKER = None


def floor(x0, z0, x1, z1, y=0):
    a,b,c,d = (x0,y,z0),(x0,y,z1),(x1,y,z1),(x1,y,z0)
    return [(a,b,c),(a,c,d)]


def box(x0,z0,x1,z1,height):
    result = floor(x0,z0,x1,z1,height)
    corners = [(x0,z0),(x0,z1),(x1,z1),(x1,z0)]
    for i,(x,z) in enumerate(corners):
        xx,zz = corners[(i+1)%4]
        a,b,c,d = (x,0,z),(x,height,z),(xx,height,zz),(xx,0,zz)
        result.extend(((a,b,c),(a,c,d)))
    return result


def contains(poly, x, z):
    v = np.array(poly.vertices)
    return bool((np.cross(np.roll(v,-1,axis=0)-v, np.array([x,0,z])-v)[:,1] >= -1e-6).all())


class NavigationTests(unittest.TestCase):
    def test_water_export_rejects_seam_shortcut_partial_overlap_and_closes_links(self):
        # Actual skinny seam-shortcut shape from the full map audit. All its
        # vertices are outside the water, but its interior crosses below it.
        vertices=np.array([(198.8,-1.35,84.6),(200,-1.4,84.6),(200,0,0),
                           (197,4,40),(200,4,40),(200,4,70),
                           (190,-2,90),(194,-2,90),(194,-2,96)],np.float32)
        polys=[[0,1,2],[3,4,5],[6,7,8]]
        links=[[1,-1,-1],[0,-1,-1],[-1,-1,-1]]
        water=np.array([[(163.45,-.4,31.685),(200,-.4,51.787),(200,-.4,76.554)]])
        p,n,rejected=exclude_water_polygons(vertices,polys,links,water)
        self.assertEqual(rejected,1)
        self.assertEqual(p,polys[1:])
        self.assertEqual(n,[[-1,-1,-1],[-1,-1,-1]])
        # A small corner overlap must be rejected even with a dry centroid.
        vertex=np.array([(0,0,0),(0,0,10),(10,0,0)],np.float32)
        corner=np.array(floor(0,0,1,1,1),np.float32)
        self.assertEqual(exclude_water_polygons(vertex,[[0,1,2]],[[-1]*3],corner)[2],1)

    def test_pavement_closes_unused_lane_preserving_sidewalk_bridge_and_crosswalk(self):
        geometry = floor(80,80,120,120)+floor(80,110,120,120,.15)+floor(90,90,110,106,4)
        road = [[(80,0,95),(120,0,95),(120,0,102),(80,0,102)]]
        pavement = floor(80,95,120,110)
        crossing = floor(98,90,102,115)
        layer = self.bake(geometry,floors=[1]*6,roads=road,crossings=crossing,water=[],pavement=pavement)
        ground = [p for p in layer.polygons if max(v[1] for v in p.vertices)<1]
        self.assertFalse(any(contains(p,85,106) for p in ground),'unused parking/gutter lane is road pavement')
        self.assertTrue(any(contains(p,85,115) for p in ground),'raised sidewalk remains usable')
        self.assertTrue(any(contains(p,100,106) for p in ground),'crosswalk reopens full pavement width')
        self.assertTrue(any(contains(p,100,100) and min(v[1] for v in p.vertices)>3 for p in layer.polygons),'bridge height stays separate')
        wet = self.bake(geometry,floors=[1]*6,roads=road,crossings=crossing,water=floor(98,95,102,110,1),pavement=pavement)
        self.assertFalse(any(contains(p,100,106) and max(v[1] for v in p.vertices)<1 for p in wet.polygons),'crosswalk cannot revive water')

    def test_sidewalk_shoulder_join_preserves_rural_and_separate_height_shoulders(self):
        shoulders=np.array(floor(80,80,120,84)+floor(80,100,120,104)+floor(80,80,120,84,5),np.float32)
        sidewalks=np.array(floor(80,84.2,120,90,.15),np.float32)
        self.assertEqual(sidewalk_shoulders(shoulders,sidewalks).tolist(),[True,True,False,False,False,False])
        layer=self.bake(floor(80,80,120,120),floors=[1,1],water=[],pavement=shoulders[sidewalk_shoulders(shoulders,sidewalks)])
        self.assertFalse(any(contains(p,100,82) for p in layer.polygons))
        self.assertTrue(any(contains(p,100,102) for p in layer.polygons),'roadside without sidewalk stays walkable')

    def test_extra_pavement_requires_nearby_sidewalk_at_matching_height(self):
        city=floor(80,80,120,84)
        rural=floor(80,105,120,110)
        bridge=floor(80,80,120,84,5)
        pavement=np.array(city+rural+bridge,np.float32)
        sidewalks=np.array(floor(80,89,120,92,.15),np.float32)
        selected=sidewalk_shoulders(pavement,sidewalks,8.)
        self.assertEqual(selected.tolist(),[True,True,False,False,False,False])
        layer=self.bake(floor(80,80,120,120),floors=[1,1],water=[],pavement=pavement[selected])
        self.assertFalse(any(contains(p,100,82) for p in layer.polygons))
        self.assertTrue(any(contains(p,100,107) for p in layer.polygons),'no-sidewalk road keeps its earlier roadside availability')

    def bake(self, triangles, center=(100,100), floors=None, roads=None, crossings=None, water=None, pavement=None):
        triangles = np.asarray(triangles,dtype=np.float32)
        with tempfile.TemporaryDirectory(prefix='nsv-nav-test-新-') as tmp:
            data,_ = bake_cell(triangles,triangles.min(1),triangles.max(1),center,BAKER,Path(tmp),floors,roads,crossings,water,pavement)
        self.assertIsNotNone(data)
        return read_rw_object(data)[0][0]

    def test_forbidden_ocean_keeps_raised_deck(self):
        geometry = floor(80,80,120,120,-12)+floor(90,90,110,110,0)
        layer = self.bake(geometry,floors=[0,0,1,1])
        self.assertTrue(any(contains(p,100,100) for p in layer.polygons))
        self.assertTrue(all(min(v[1] for v in p.vertices)>-.1 for p in layer.polygons))
        self.assertFalse(any(contains(p,85,85) for p in layer.polygons))

    def test_forbidden_shoulder_cannot_connect_to_sidewalk(self):
        geometry = floor(80,80,100,120)+floor(100,80,120,120,.1)
        layer = self.bake(geometry,floors=[0,0,1,1])
        self.assertFalse(any(contains(p,99.9,100) for p in layer.polygons))
        self.assertTrue(any(contains(p,110,100) for p in layer.polygons))

    def test_forbidden_surface_wins_when_coplanar_collision_overlaps(self):
        geometry = floor(80,80,120,120)+floor(90,90,110,110)
        layer = self.bake(geometry,floors=[1,1,0,0])
        self.assertTrue(any(contains(p,85,100) for p in layer.polygons))
        self.assertFalse(any(contains(p,100,100) for p in layer.polygons))

    def test_road_blocks_lane_and_preserves_sidewalks_and_bridge(self):
        geometry = floor(80,80,120,120)+floor(90,96,110,104,4)
        road = [[(80,0,97),(120,0,97),(120,0,103),(80,0,103)]]
        layer = self.bake(geometry,floors=[1]*4,roads=road)
        ground = [p for p in layer.polygons if max(v[1] for v in p.vertices)<1]
        bridge = [p for p in layer.polygons if min(v[1] for v in p.vertices)>3]
        self.assertFalse(any(contains(p,85,100) for p in ground))
        self.assertFalse(any(contains(p,85,96.9) for p in ground),'agent radius must clear traffic')
        self.assertTrue(any(contains(p,85,90) for p in ground))
        self.assertTrue(any(contains(p,85,110) for p in ground))
        self.assertTrue(any(contains(p,100,100) for p in bridge))
        offsets = {p.offset:p for p in ground}
        start = next(p.offset for p in ground if contains(p,85,90))
        goal = next(p.offset for p in ground if contains(p,85,110))
        seen,queue = {start},deque([start])
        while queue:
            for n in offsets[queue.popleft()].neighbors:
                if n in offsets and n not in seen:
                    seen.add(n);queue.append(n)
        self.assertNotIn(goal,seen,'ordinary wandering must not cross the blocked road')

    def test_surface_classification_ignores_audio_and_grind_bits(self):
        tags = [(0,(physical<<7)|audio|(grind<<12))
                for physical in range(32) for audio in (0,55,127) for grind in (0,9,15)]
        expected = [1]*len(tags)
        np.testing.assert_array_equal(pedestrian_floors(tags),expected)

    def test_collision_ids_are_little_endian_inside_big_endian_mesh(self):
        data = bytearray(0x70+75)
        struct.pack_into('>3f',data,16,1,0,1)
        for at,value in ((0x28,1),(0x34,0x60),(0x40,1),(0x48,1),(0x60,0x70)):
            struct.pack_into('>I',data,at,value)
        struct.pack_into('>f',data,0x38,.001)
        data[0x3e:0x40] = b'\x02\x02'
        struct.pack_into('>5H3B',data,0x70,1,11,3,3,75,3,0,0)
        for i,vertex in enumerate(((0,0,0),(0,0,1),(1,0,0))):
            struct.pack_into('>4f',data,0x80+i*16,*vertex,0)
        data[0xb0:] = b'\xe1\x00\x01\x02\x00\x00\x00\x34\x12\x37\xc3'
        triangles,bounds,tags = clustered_mesh(data,True)
        self.assertEqual(tags.tolist(),[[0x1234,0xc337]])
        self.assertEqual(pedestrian_floors(tags).tolist(),[1])
        np.testing.assert_array_equal(clustered_mesh(data)[0],triangles)

    def test_authored_water_preserves_beach_deck_and_dry_low_ground(self):
        geometry = floor(80,80,100,120,-12)+floor(100,80,120,120,-9)+floor(90,90,96,110,0)
        water = floor(80,80,100,120,-10)
        layer = self.bake(geometry,floors=[1]*6,water=water)
        low = [p for p in layer.polygons if max(v[1] for v in p.vertices)<-11]
        self.assertFalse(any(contains(p,85,100) for p in low))
        self.assertTrue(any(contains(p,110,100) for p in layer.polygons),'beach above water stays walkable')
        self.assertTrue(any(contains(p,93,100) for p in layer.polygons),'ship deck stays walkable')
        layer = self.bake(geometry,floors=[1]*6,water=floor(80,80,88,120,-10))
        self.assertTrue(any(contains(p,98,100) for p in layer.polygons),'low dry ground outside water stays walkable')

    def test_crosswalk_connects_sidewalks_without_reopening_other_road_or_water(self):
        geometry = floor(80,80,120,120)
        road = [[(80,0,97),(120,0,97),(120,0,103),(80,0,103)]]
        crossing = floor(98,90,102,110)
        layer = self.bake(geometry,floors=[1,1],roads=road,crossings=crossing)
        self.assertTrue(any(contains(p,100,100) for p in layer.polygons))
        self.assertFalse(any(contains(p,85,100) for p in layer.polygons))
        offsets = {p.offset:p for p in layer.polygons}
        start = next(p.offset for p in layer.polygons if contains(p,85,90))
        goal = next(p.offset for p in layer.polygons if contains(p,85,110))
        seen,queue = {start},deque([start])
        while queue:
            for n in offsets[queue.popleft()].neighbors:
                if n in offsets and n not in seen:
                    seen.add(n);queue.append(n)
        self.assertIn(goal,seen,'ordinary wandering can use the crosswalk')
        wet = self.bake(geometry,floors=[1,1],roads=road,crossings=crossing,water=floor(98,97,102,103,1))
        self.assertFalse(any(contains(p,100,100) for p in wet.polygons),'crosswalk must not reopen water')
        elevated = self.bake(geometry,floors=[1,1],roads=road,crossings=floor(98,90,102,110,4))
        self.assertFalse(any(contains(p,100,100) for p in elevated.polygons),'crosswalk on bridge must not reopen road below')

    def test_area_materials_use_authored_water_and_crosswalk_semantics(self):
        def channels(family,diffuse):
            return {'AttribulatorMaterialName':{'text':family},'diffuse':{'text':diffuse}}
        self.assertEqual(area_material(channels('ocean.default','')), 'water')
        self.assertEqual(area_material(channels('water.default','')), 'water')
        self.assertEqual(area_material(channels('environment.default','obj_road_crosswalk_01_dt_d_0x123')), 'crosswalk')
        self.assertEqual(area_material(channels('environment.default','obj_sidewalk_curbpanelcrossing_01_dt_d')), 'sidewalk')
        self.assertEqual(area_material(channels('environment.default','ot_roadb_crosswalk_d_0x123')), 'crosswalk')
        self.assertEqual(area_material(channels('environment.default','obj_road_crosswalkyellow_01_ur_d')), 'crosswalk')
        self.assertEqual(area_material(channels('environment.default','ot_roadb_clean_d')), 'road')
        self.assertEqual(area_material(channels('environment.default','road_curb_base02')), 'road')
        self.assertEqual(area_material(channels('environment.default','obj_road_roadsidefresh_03_sm_d')), 'shoulder')
        self.assertIsNone(area_material(channels('environment.default','ot_roadb_alley_d')))
        self.assertIsNone(area_material(channels('environment.default','obj_road_concreteramp_01_sm_d')))

    def test_authored_lane_width_and_curvature_and_bad_ranges(self):
        data = bytearray(64+160+224)
        struct.pack_into('>6I',data,32,0,1,0,0xdeadbeef,64,0xdeadbeef)
        struct.pack_into('>II',data,64+120,1,160)
        values = [(80,0,96),(80,0,104),(120,0,96),(120,0,104),
                  (40,0,8),(40,0,8),(40,0,-8),(40,0,-8)]
        for at,v in zip(range(0x50,0xd0,16),values):
            struct.pack_into('>3f',data,224+at,*v)
        quads = rain_footprints(data)
        self.assertEqual(quads.shape,(4,4,3))
        self.assertAlmostEqual(float(quads[:,:,0].min()),80)
        self.assertAlmostEqual(float(quads[:,:,0].max()),120)
        self.assertGreater(float(quads[:,:,2].max()),104)
        with self.assertRaisesRegex(ValueError,'outside resource'):
            rain_footprints(data[:-1])
        struct.pack_into('>f',data,224+0x50,float('nan'))
        with self.assertRaisesRegex(ValueError,'non-finite'):
            rain_footprints(data)

    def test_classified_input_fails_closed_on_bad_mask_or_truncation(self):
        geometry = np.asarray(floor(80,80,120,120),dtype='<f4')
        header = b'NSVCOL2\0'+struct.pack('<I6f',2,79,-1,79,121,2,121)+geometry.tobytes()
        with tempfile.TemporaryDirectory(prefix='nsv-nav-invalid-') as tmp:
            source,target = Path(tmp)/'in.bin',Path(tmp)/'out.bin'
            for tail,message in ((b'\x01\x02'+struct.pack('<I',0),'classification'),
                                 (b'\x01','Truncated'),
                                 (b'\x01\x01'+struct.pack('<I',1),'Truncated')):
                source.write_bytes(header+tail)
                result = subprocess.run([str(BAKER),str(source),str(target)],capture_output=True)
                self.assertNotEqual(result.returncode,0)
                self.assertIn(message,result.stderr.decode())

    def test_authored_input_rejects_truncated_and_nonfinite_area_arrays(self):
        geometry = np.asarray(floor(80,80,120,120),dtype='<f4')
        header = (b'NSVCOL3\0'+struct.pack('<I6f',2,79,-1,79,121,2,121)+
                  geometry.tobytes()+b'\x01\x01'+struct.pack('<I',0))
        with tempfile.TemporaryDirectory(prefix='nsv-nav-authored-invalid-') as tmp:
            source,target = Path(tmp)/'in.bin',Path(tmp)/'out.bin'
            for tail,message in ((struct.pack('<I',1),'Truncated'),
                                 (struct.pack('<I9f',1,*([float('nan')]*9)),'Non-finite'),
                                 (struct.pack('<II',0,1),'Truncated')):
                source.write_bytes(header+tail)
                result = subprocess.run([str(BAKER),str(source),str(target)],capture_output=True)
                self.assertNotEqual(result.returncode,0)
                self.assertIn(message,result.stderr.decode())

    def test_walkable_floor_and_obstacle_clearance(self):
        layer = self.bake(floor(80,80,120,120)+box(98,90,102,110,1))
        ground = {p.offset:p for p in layer.polygons if max(v[1] for v in p.vertices)<.2}
        self.assertTrue(any(contains(p,90,100) for p in ground.values()))
        self.assertFalse(any(contains(p,100,100) for p in ground.values()))
        self.assertFalse(any(contains(p,97.9,100) for p in ground.values()))
        start = next(p.offset for p in ground.values() if contains(p,90,100))
        goal = next(p.offset for p in ground.values() if contains(p,110,100))
        seen, queue = {start}, deque([start])
        while queue:
            for n in ground[queue.popleft()].neighbors:
                if n in ground and n not in seen:
                    seen.add(n);queue.append(n)
        self.assertIn(goal,seen,'route must walk around the obstacle')

    def test_cell_boundary_is_not_eroded(self):
        geometry = floor(180,90,220,110)
        left = self.bake(geometry,(100,100))
        right = self.bake(geometry,(300,100))
        def edges(layer):
            result=[]
            for p in layer.polygons:
                for i,a in enumerate(p.vertices):
                    b=p.vertices[(i+1)%len(p.vertices)]
                    if abs(a[0]-200)<1e-4 and abs(b[0]-200)<1e-4:
                        result.append(tuple(sorted((tuple(round(v,3) for v in a),tuple(round(v,3) for v in b)))))
            return sorted(result)
        self.assertTrue(edges(left))
        self.assertEqual(edges(left),edges(right))

    def test_baker_output_rejects_invalid_neighbors_and_vertices(self):
        header = b'NSVPOL1\0' + struct.pack('<3I', 3, 1, 3)
        vertices = struct.pack('<9f', 0,0,0, 0,0,1, 1,0,0)
        def encoded(link):
            return header + vertices + struct.pack('<6H', 0,1,2, link,0xffff,0xffff)
        for link in (1, 0x7fff, 0x8004, 0xfffe):
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, 'neighbor'):
                read_bake(encoded(link))
        for link in (0xffff, 0x8000, 0x8001, 0x8002, 0x8003):
            self.assertEqual(read_bake(encoded(link))[2], [[-1,-1,-1]])
        bad = bytearray(encoded(0xffff))
        struct.pack_into('<f', bad, 20, float('nan'))
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            read_bake(bad)

    def test_grid_degeneracy_survives_world_float_rounding(self):
        # Collinear integer-grid vertices from the fresh city bake. World
        # float32 conversion gives a spurious negative area (~9e-5).
        points = [(349.2,232.111,-1357.4), (347.6,232.661,-1359), (344.6,233.811,-1362)]
        data = (b'NSVPOL2\0' + struct.pack('<3I',3,1,3) +
                np.asarray(points,dtype='<f4').tobytes() +
                struct.pack('<6Hq',0,1,2,65535,65535,65535,0))
        self.assertEqual(read_bake(data)[1:], ([], []))
        self.assertEqual(read_bake(data[:-8]+struct.pack('<q',-1))[1:], ([], []))
        with self.assertRaisesRegex(ValueError,'array sizes'):
            read_bake(data[:-1])

    def test_corruption_and_wrong_adjacency_rejected(self):
        v=[(0,0,0),(0,0,10),(10,0,10),(10,0,0)]
        good=write_resource(v,[[0,1,2,3]],[[-1]*4])
        bad=bytearray(good);bad[-1]^=1
        with self.assertRaisesRegex(ValueError,'checksum'):
            read_resource(bad)
        with self.assertRaisesRegex(ValueError,'reciprocal'):
            write_resource(v,[[0,1,2],[0,2,3]],[[1,-1,-1],[-1,-1,-1]])
        with self.assertRaisesRegex(ValueError,'wound'):
            write_resource(v,[[3,2,1,0]],[[-1]*4])


if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--baker',type=Path,required=True)
    args,rest=ap.parse_known_args()
    BAKER=args.baker.resolve()
    unittest.main(argv=[__file__,*rest])
