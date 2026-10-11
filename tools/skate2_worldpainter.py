"""Skate 2 WorldPainter data and its Skate 3 equivalents (read-only helpers).

WorldPainter paints values over a world on a quadtree per layer. A world cell
(cSim_*.xsf) carries, per painted layer, three RW4 objects:

  EB000F layer       u32 quadtree index, u32 dictionary index, u64 layer key
  EB0010 quadtree    leaves index the dictionary
  EB0011 dictionary  u32 value words, u32 entries, u32 0, u32 entry table
                     (0x14), u32 values; entries {u32 value offset, u32 words}

The two games disagree on both keys:

  Skate 2   layer key = asset ID (eLayerID << 32) | 0x03E387C2; a value is one
            word, the index of a paint value (asset ID index << 32 |
            0x03E387C0) in the table compiled into default.xex (.rdata
            0x820882AC..: name pointer, 0, u32 index, 0x03E387C0).
  Skate 3   layer key = h64(layer name) (the names and their eLayerID order
            are in default.xex at 0x820C483C, hashed at startup by
            sub_82F8B9D0); a value is two words read as (word1 << 32) | word0
            (sub_82C0EAC0) = h64(name of the record painted), e.g. audio_ambience
            -> aud_wp_ambiences/<name>, audio_emitters -> aud_wp_emitters/<name>,
            audio_reverb -> aud_reverb/<name>, livingworld_vehicle_census ->
            livingworld_census/<name>.

Skate 3 therefore ignores every layer New San Van paints. The runtime
(src/skate3_world_loading.cpp) translates the layers named in worldpainter.txt
while the cells load; tools/build_skate2_audio.py writes that file.

Evidence: reports/log123_traffic_lights_ambience.md.
"""
from __future__ import annotations

import struct
from collections import Counter, defaultdict
from pathlib import Path

import inspect_sfil

LAYER_TYPE = 0x03E387C2
VALUE_TYPE = 0x03E387C0

# Skate 2 eLayerID -> Skate 3 layer name, from what each BAM layer paints
# (Skate 3 has no ped/vehicle type layers; its load groups come from the
# world record instead).
LAYERS = {
    0x00: 'test_layer0',
    0x01: 'test_layer1',
    0x02: 'livingworld_npc_census',
    0x03: None,                        # district ped types (district_*_peds)
    0x04: None,                        # district vehicle types and routes
    0x05: 'livingworld_vehicle_census',
    0x06: 'livingworld_security_guard_areas',
    0x0D: 'districts',
    0x0E: 'district_locations',
    0x0F: 'audio_ambience',
    0x10: 'audio_emitters',
    0x11: 'challenge_zone_races',
    0x12: 'audio_reverb',
    0x13: 'rendering_colorcube',
    0x14: 'cameras_race',
    0x16: 'services',
    0x17: 'livingworld_dmo_safety',    # AI cost areas (ForbiddenArea, ...)
    0x19: 'rendering_fog',
    0x1A: 'rendering_sky',
    0x1B: 'rendering_exposure',
    0x1C: 'rendering_bloom',
}

# Paint values of the audio and living-world layers (Skate 2 default.xex
# value table; index -> name). Values painted but absent from the table were
# deleted from the project: Skate 2 resolves them to nothing, and so does the
# translation.
VALUES = {
    'ambience': {
        82: 'back_alley_pete', 83: 'soho', 85: 'dt_courtyard', 86: 'soho_alley',
        88: 'dt_interiors', 89: 'spillway', 92: 'svm_north', 94: 'dt_matrix',
        95: 'svm_south', 97: 'dt_parks', 98: 'urban_res', 100: 'old_town', 101: 'wf_main',
        104: 'wf_shore', 106: 'projects_main', 107: 'x_games', 222: 'soho_classic',
        223: 'wf_main2', 226: 'dt_main', 227: 'projects_below', 230: 'ot_courtyard'},
    'emitters': {
        50: 'e_back_alley_pete', 55: 'e_dt_interiors', 70: 'e_spillway', 91: 'e_dt_main',
        103: 'e_projects_below', 210: 'e_urban_res', 211: 'e_dt_courtyard',
        212: 'e_svm_north', 213: 'e_svm_south', 214: 'e_wf_main', 215: 'e_wf_shore',
        216: 'e_x_games', 217: 'e_soho', 218: 'e_soho_alley', 219: 'e_dt_parks',
        220: 'e_old_town', 221: 'e_ot_courtyard', 224: 'e_wf_main2', 225: 'e_soho_classic',
        228: 'e_dt_matrix', 229: 'e_projects_main', 231: 't_dt_soho', 232: 't_dt_parks',
        233: 't_dt_matrix', 234: 't_dt_wf', 235: 't_dt_projects', 236: 't_wf_ot',
        237: 't_ot_projects', 238: 't_projects_soho', 239: 't_ur_bap', 240: 't_ur_svm_n',
        241: 't_soho_svm_s', 242: 'e_wf_skatepark', 243: 'e_svm_vistas',
        244: 't_projects_ur', 289: 't_ur_soho'},
    'reverb': {111 + i: f'reverb{i + 1:02d}' for i in range(24)},
    'npc_census': {
        5: 'district_waterfront01_peds_census', 28: 'district_oldtown01_peds_census',
        32: 'district_urbanres01_peds_census', 35: 'test_high_density_peds_census',
        36: 'test_med_density_peds_census', 37: 'test_low_density_peds_census',
        38: 'test_zero_peds_census', 158: 'district_artdeco01_peds_census',
        159: 'district_soho01_peds_census', 160: 'district_projects01_peds_census',
        161: 'district_svmountain01_peds_census', 165: 'district_park01_peds_census',
        248: 'district_alleys01_peds_census', 353: 'district_school01_peds_census',
        354: 'district_svmountain02_peds_census'},
    'vehicle_census': {
        168: 'district_artdeco01_vehicles_census', 169: 'district_svmountain01_vehicles_census',
        205: 'district_soho01_vehicles_census', 206: 'district_projects01_vehicles_census',
        207: 'district_park01_vehicles_census', 208: 'district_waterfront01_vehicles_census',
        245: 'district_oldtown02_vehicles_census', 247: 'district_urbanres01_vehicles_census'},
    'ped_types': {
        7: 'district_financial01_peds', 10: 'district_outskirts01_peds',
        11: 'district_oldtown01_peds', 12: 'district_suburbs01_peds',
        13: 'district_urbanres01_peds'},
    'vehicle_types': {
        14: 'district_financial01_vehicles', 17: 'district_outskirts01_vehicles',
        18: 'district_oldtown01_vehicles', 19: 'district_suburbs01_vehicles',
        20: 'district_urbanres01_vehicles', 142: 'area_financial_mainroad_vehicles',
        149: 'area_suburbs_shingo_route'},
    'security': {39: 'NoSkateZone_Area', 48: 'NoSkateZone_Buffer', 147: 'HighAlert_Area',
                 148: 'HighAlert_Buffer'},
    'districts': {45: 'district_financial', 46: 'district_projects', 51: 'district_oldtown',
                  53: 'district_urbanres', 143: 'district_xgames', 144: 'district_mountain',
                  170: 'district_waterfront', 171: 'dist_soho'},
    'ai_cost': {292: 'ForbiddenArea', 293: 'HighCostArea', 294: 'MediumCostArea',
                295: 'LowCostArea'},
}


def value_names():
    names = {}
    for table in VALUES.values():
        names.update(table)
    return names


def stream_layers(folder: Path):
    """{Skate 2 layer id: Counter(value id)} over a stream's cSim cells."""
    painted = defaultdict(Counter)
    # Path.glob suppresses directory-access errors in Python 3.13. Preparation
    # must never turn an unreadable map into a successful empty manifest.
    cells = sorted(p for p in folder.iterdir()
                   if p.name.startswith('cSim_') and p.suffix.lower() == '.xsf')
    if not cells:
        raise ValueError(f'no collision cells in WorldPainter stream: {folder}')
    for cell in cells:
        for _, _, (arena, _) in inspect_sfil.assets(cell.read_bytes()):
            objects = list(inspect_sfil.dictionary(arena))
            for obj in objects:
                if obj['kind'] != 0xEB000F:
                    continue
                layer = arena[obj['pointer']:obj['pointer'] + obj['size']]
                _, dictionary, key = struct.unpack_from('>IIQ', layer)
                if key & 0xFFFFFFFF != LAYER_TYPE or dictionary >= len(objects):
                    continue
                entry = objects[dictionary]
                data = arena[entry['pointer']:entry['pointer'] + entry['size']]
                words, count = struct.unpack_from('>II', data)
                if words != count:
                    continue
                for i in range(count):
                    offset, size = struct.unpack_from('>II', data, 0x14 + 8 * i)
                    painted[key >> 32][struct.unpack_from('>I', data, offset)[0]] += 1
    return painted
