#!/usr/bin/env python3
"""Build and install the New San Van Locations-menu package.

Adds "New San Vanelona" to the pause-menu Locations list, after the retail
districts, with one entry per spawn in tools/nsv_spawns.json. The data is a
DLC-style content package, laid out exactly like the retail map DLCs
(reports/log099_nsv_locations_menu.md has the evidence):

  map_category/dist_newsanvan  district name (ID_COMMON_NEW_SAN_VANELONA)
  world/dist_newsanvan         minimal world under default_skate3 (no streams)
  map_filter/newsanvan         Lua: IsLocationInWorld(item, "dist_newsanvan")
  map_listing/...              the filter under the Locations group
  fe_locations/nsv_*           one per spawn, parent skate3, Unlocked
  aud_wp_ambiences/nsv_*,      New San Van's painted ambience bed areas and
  aud_wp_emitters/nsv_*        sound zones, when tools/build_skate2_audio.py
                               has run (its vault_records.json; run it first)

Each fe_locations entry borrows a distinct retail locator; the game resolves it
normally and then calls the teleport routine sub_828646E0 with the entry's
world key. src/skate3_nsv_menu.cpp intercepts New San Van's world key there
and loads New San Van at the spawn stored for that locator in
<root>/mods/skate2/nsv_spawns.txt (written here). An entry's optional
"stream" picks the Skate 2 world stream to load (default BAM; the DLC maps
installed by tools/build_skate2_dlc_maps.py).

Formats (all checked against retail files): EB3 archive family 0x00500600
with its directory tree (rebuilds four retail DLC archives byte for byte);
label tables keyed by the game's label hash (h = 0xFFFFFFFF; h = h*33 + c;
36/36 retail DLC tables rebuild byte for byte); Attrib vault with export ids
h64("<class>/<collection>").

Usage (repository root):
  python tools/build_nsv_dlc.py [--root out/build/relwithdebinfo] [--uninstall]
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from vlt64 import h64  # noqa: E402

REPO = HERE.parent
PACK = 'newsanvan'
PACKAGE_DIR = b'NSVMOD-NEWSANVAN-SK2'.hex().upper() + '45'  # 42 chars, like retail
DISPLAY_NAME = 'New San Van (Skate 2)'
TITLE_ID = 0x454108E6
VAULT_STAMP = 0x22356EA43D6CA167      # Vers stamp of the retail DLC vaults
EXPORT_TYPE = 0xAD303B8F42B3307E      # Attrib CollectionLoadData export type
WORLD = 'dist_newsanvan'
FILTER = 'newsanvan'
LISTING = 'map_listing_newsanvan'
LISTING_ORDER = 50                    # retail districts 0..6, DLC maps 9+
CATEGORY_ORDER = 15000                # retail districts 10000..14000
DISTRICT_LABEL = 'ID_COMMON_NEW_SAN_VANELONA'   # retail: "New San Vanelona"
IMAGE = 'fe\\source\\images\\locations\\any_location'

# Retail district locators (skatercollections fe_locations). The borrowed
# locator only has to resolve; the runtime identifies the entry by it.
PLACEHOLDERS = [
    'Z_DT_Aletown', 'Z_DT_CanalPlaza', 'Z_DT_CarLot', 'Z_DT_CorePlaza', 'Z_DT_CubeTower',
    'Z_DT_HotelPlaza', 'Z_DT_Mall', 'Z_DT_Memorial', 'Z_DT_MidTownpromenade',
    'Z_DT_parkadeStart', 'Z_DT_resPlaza', 'Z_DT_SkatePark', 'Z_DT_SpillwayStart',
    'Z_DT_Tresoutta', 'Z_Ind_2nd_N_Navy', 'Z_Ind_dryDocks', 'Z_Ind_GhettoSpot',
    'Z_Ind_NewFactory', 'Z_Ind_NewFactoryRoof', 'Z_Ind_OldFactory', 'Z_Ind_Quarry',
    'Z_Ind_ReclaimedHastings', 'Z_Ind_Loading_Docks', 'Z_Ind_Spillway',
    'Z_Ind_Spillway_Bottom', 'Z_Ind_TankerShip', 'Z_UN_CampusEntrance', 'Z_UN_CampusStart',
    'Z_UN_ClockTower', 'Z_UN_CommunitySkatePark', 'Z_UN_TrainingExterior', 'Z_UN_ResRoads',
    'Z_UN_observatory', 'Z_UN_LowerCampus', 'Z_UniversityMegaPark', 'Z_UN_StadiumCampus',
]

# Keys not derivable from known names (hashes observed in retail records).
T_TEXT = 0x4657A686D9F1A95E
T_INT16 = 0x67EA4FB28BCCDD3B
T_INT32 = 0x20E3372771C4CC16
T_BOOL = 0x15C52CB4069556A3
T_REFSPEC = 0x97C5113DE9BD1AD6
T_LOCATIONID = 0x718619ED487050FC
T_ZONEREF = 0x05B4C0D186BF61DD
T_WORLDREF = 0x81E57FCE1C22AF7B
T_LUAFILTER = 0xEDC552351E2EA918
A_HALID_CATEGORY = 0x465698DD3CC73CA2
A_ORDER_CATEGORY = 0x1349BB6EC1D1E751
A_HALID_WORLD = 0x095E1D9778F0C6F4
A_HELPER = 0x5FBB2C3F59FC6F1A
A_ORDER_LISTING = 0x982BD1EAE2334504
A_ZONE = 0x50D9293B818507EC
PARENT_PORTCARVERTON = 0x1563E80F38121D85   # map_category of the retail districts
PARENT_LOCATIONS_GROUP = 0xC1A9DBAA19B10CA7  # map_listing: the Locations list


def align(n, a):
    return (n + a - 1) & ~(a - 1)


# ---- Attrib vault -------------------------------------------------------------
def best_shift(keys):
    """Table shift with the smallest probe length (any shift is valid: the
    game hashes (u32)rotl64(key, shift) % n with linear probing)."""
    n = len(keys)
    if n < 2:
        return 0
    mask = (1 << 64) - 1

    def probe(shift):
        slots, worst = [None] * n, 0
        for key in keys:
            rot = ((key << shift) | (key >> (64 - shift))) & mask if shift else key
            home, d = (rot & 0xFFFFFFFF) % n, 0
            while slots[(home + d) % n] is not None:
                d += 1
            slots[(home + d) % n] = key
            worst = max(worst, d)
        return worst
    return min(range(8), key=lambda s: (probe(s), s))


class Vault:
    def __init__(self, name):
        self.name = name
        self.strings = bytearray()
        self.string_at = {}
        self.data = bytearray()
        self.bin_ptrs = []       # (data offset holding a pointer, target ref)
        self.records = []

    def string(self, text):
        if text not in self.string_at:
            self.string_at[text] = len(self.strings)
            self.strings += text.encode('latin1') + b'\0'
        return ('str', self.string_at[text])

    def blob(self, raw, ptrs=()):
        while len(self.data) % 16:
            self.data.append(0)
        at = len(self.data)
        self.data += raw
        self.bin_ptrs += [(at + off, ref) for off, ref in ptrs]
        return ('dat', at)

    def record(self, cls, coll, parent, types, entries, layout=None):
        """entries: (attribute key, type index, flags, int value or ref)."""
        self.records.append((cls, coll, parent, types, entries, layout))

    def load_order(self):
        """The native collection constructor links a parent only once.

        Export parents before children, even if callers add them out of order.
        External parents already exist in the base vault; references to other
        collections are lazy RefSpecs and do not impose this ordering.
        """
        by_key = {(h64(r[0]), h64(r[1])): r for r in self.records}
        if len(by_key) != len(self.records):
            raise ValueError('duplicate vault collection')
        visiting, loaded, ordered = set(), set(), []

        def visit(key):
            if key in loaded:
                return
            if key in visiting:
                raise ValueError('cycle in vault collection inheritance')
            visiting.add(key)
            r = by_key[key]
            parent = (key[0], r[2])
            if r[2] and parent in by_key:
                visit(parent)
            visiting.remove(key)
            loaded.add(key)
            ordered.append(r)

        for r in self.records:
            visit((h64(r[0]), h64(r[1])))
        return ordered

    def build(self):
        stre_size = align(8 + len(self.strings), 16)
        data_base = stre_size + 16

        def where(ref):
            kind, off = ref
            return 8 + off if kind == 'str' else data_base + off

        binf = bytearray(b'StrE' + struct.pack('>I', stre_size) + self.strings)
        binf += bytes(stre_size - len(binf))
        binf += b'EndC' + struct.pack('>I', 16) + bytes(8) + self.data

        vlt = bytearray(b'Vers' + struct.pack('>IQ', 16, VAULT_STAMP))
        vlt += b'StrN' + struct.pack('>I', 16) + bytes(8)
        names = [self.name + '.vlt', self.name + '.bin']
        dep = struct.pack('>II', 0, 2) + b''.join(struct.pack('>Q', h64(n)) for n in names)
        dep += struct.pack('>II', 0, len(names[0]) + 1) + b''.join(n.encode() + b'\0' for n in names)
        dep_size = align(8 + len(dep), 16)
        vlt += b'DepN' + struct.pack('>I', dep_size) + dep + bytes(dep_size - 8 - len(dep))

        dat_at, recs, exports, vlt_ptrs = len(vlt), bytearray(), [], []
        for cls, coll, parent, types, entries, layout in self.load_order():
            at = dat_at + 8 + len(recs)
            n, slots = len(entries), align(len(types), 2)
            body = bytearray(struct.pack('>QQQIIIHHII', h64(coll), h64(cls), parent, n,
                                         best_shift([e[0] for e in entries]), n,
                                         len(types), slots, 0, 0))
            if layout is not None:
                vlt_ptrs.append((at + 40, where(layout)))
            for t in list(types) + [0] * (slots - len(types)):
                body += struct.pack('>Q', t)
            for key, tindex, flags, value in entries:
                if isinstance(value, tuple):
                    vlt_ptrs.append((at + len(body) + 8, where(value)))
                    value = 0
                body += struct.pack('>QIHH', key, value, tindex, flags)
            recs += body
            exports.append((h64(f'{cls}/{coll}'), len(body), at))
        dat_size = align(8 + len(recs), 16)
        vlt += b'DatN' + struct.pack('>I', dat_size) + recs + bytes(dat_size - 8 - len(recs))

        exp = struct.pack('>II', 0, len(exports)) + b''.join(
            struct.pack('>QQII', eid, EXPORT_TYPE, size, at) for eid, size, at in exports)
        exp_size = align(8 + len(exp), 16)
        vlt += b'ExpN' + struct.pack('>I', exp_size) + exp + bytes(exp_size - 8 - len(exp))

        ptr = struct.pack('>IHHII', 0, 2, 1, 0, 0)
        for off, ref in sorted(self.bin_ptrs):
            ptr += struct.pack('>IHHII', data_base + off, 3, 1, 0, where(ref))
        ptr += struct.pack('>IHHII', 0, 2, 0, 0, 0)
        for off, dest in sorted(vlt_ptrs):
            ptr += struct.pack('>IHHII', off, 3, 1, 0, dest)
        ptr += bytes(16)
        ptr_size = align(8 + len(ptr), 16)
        vlt += b'PtrN' + struct.pack('>I', ptr_size) + ptr + bytes(ptr_size - 8 - len(ptr))
        vlt += b'EndC' + struct.pack('>I', 16) + bytes(8)
        return bytes(vlt), bytes(binf)


def build_vault(spawns, extra=()):
    v = Vault('dlc_' + PACK)
    world_key = h64(WORLD)

    v.record('map_category', WORLD, PARENT_PORTCARVERTON, [T_TEXT, T_INT32], [
        (h64('Name'), 0, 0x4000, v.string(WORLD)),
        (A_HALID_CATEGORY, 0, 0x4000, v.string(DISTRICT_LABEL)),
        (A_ORDER_CATEGORY, 1, 0x4000, CATEGORY_ORDER)])

    # No stream/big fields: inherits default_skate3 -> default (IsSkatePark
    # false). The runtime never lets the game load this world natively.
    v.record('world', WORLD, h64('default_skate3'), [T_TEXT], [
        (h64('Name'), 0, 0x4000, v.string(WORLD)),
        (A_HALID_WORLD, 0, 0x4000, v.string(DISTRICT_LABEL))])

    lua = ('do\n\tif( item.ClassKey == Attrib.ClassName.fe_locations )then\n'
           '\t\t--\tprint( string.format( "Running filter %s on %s", self.Name, item.Name ) );\n'
           f'\t\tif( LocationFilterHelpers.IsLocationInWorld( item, "{WORLD}" ) ) then\n'
           '\t\t\treturn true;\n\t\tend\n\tend\n\t\n\treturn false;\nend;')
    filt = v.blob(bytes(12), [(0, v.string(lua)), (8, v.string(''))])
    v.record('map_filter', FILTER, h64('location'), [T_TEXT, T_LUAFILTER], [
        (h64('Name'), 0, 0x4000, v.string(FILTER)),
        (A_HELPER, 0, 0x4000, v.string('ID_NSV_MAP_HELPER')),
        (h64('Filter'), 1, 0x0000, filt),
        (h64('HalID'), 0, 0x4000, v.string(DISTRICT_LABEL))])

    ref = v.blob(struct.pack('>QQQ', h64('map_filter'), h64(FILTER), 0))
    v.record('map_listing', LISTING, PARENT_LOCATIONS_GROUP, [T_REFSPEC, T_INT16], [
        (h64('Listing'), 0, 0x0800, ref),
        (A_ORDER_LISTING, 1, 0x4000, LISTING_ORDER << 16)])

    image = v.string(IMAGE)
    for s in spawns:
        layout = v.blob(struct.pack('>IIIIQQQQ', 0, 0, 0, 0, world_key, 0, world_key, 0),
                        [(0, v.string(s['name'])), (4, v.string(s['placeholder'])),
                         (8, v.string(s['label']))])
        v.record('fe_locations', s['name'], h64('skate3'),
                 [T_TEXT, T_LOCATIONID, T_ZONEREF, T_BOOL, T_WORLDREF], [
                     (h64('Description'), 0, 0x4000, v.string(s['desc_label'])),
                     (h64('Image'), 0, 0x4000, image),
                     (A_ZONE, 2, 0x0800, ('dat', layout[1] + 16)),
                     (h64('Unlocked'), 3, 0x4000, 0x01000000),
                     (h64('World'), 4, 0x0800, ('dat', layout[1] + 32))],
                 layout=layout)

    # Isolated audio/actor records can carry strings inside fixed layouts.
    # Emit real PtrN relocations; embedding source vault addresses corrupts
    # recipe names after the DLC vault is loaded at a different address.
    def extra_blob(value):
        if isinstance(value, str):
            return v.blob(bytes.fromhex(value))
        raw = bytes.fromhex(value['data'])
        pointers = []
        for p in value.get('pointers', []):
            if not 0 <= p['offset'] <= len(raw) - 4:
                raise ValueError('extra vault pointer outside its blob')
            pointers.append((p['offset'], v.string(p['string'])))
        return v.blob(raw, pointers)

    for r in extra:
        layout = extra_blob(r['layout']) if r['layout'] else None
        entries = [(int(e['key'], 16), e['type'], e['flags'],
                    v.string(e['string']) if 'string' in e else
                    extra_blob(e) if 'data' in e else e['value'])
                   for e in r['entries']]
        v.record(r['class'], r['collection'], int(r['parent'], 16),
                 [int(t, 16) for t in r['types']], entries, layout=layout)
    return v.build()


def audio_records(root):
    """Records written by tools/build_skate2_audio.py, when its install is complete."""
    folder = root / 'mods' / 'skate2' / 'audio-v1'
    if not (folder / 'AUDIO_COMPLETE_V2').is_file():
        return []
    return json.loads((folder / 'vault_records.json').read_text(encoding='ascii'))['records']


def livingworld_records(root):
    folder = root / 'mods' / 'skate2' / 'livingworld-v1'
    if not (folder / 'LIVINGWORLD_COMPLETE_V1').is_file():
        return []
    records = json.loads((folder / 'vault_records.json').read_text(encoding='ascii'))['records']
    if any(not r['collection'].startswith('nsv_s2_') for r in records):
        raise ValueError('living-world records must use the isolated New San Van namespace')
    return records


# ---- label tables ---------------------------------------------------------------
def label_id(name):
    h = 0xFFFFFFFF
    for c in name.encode('latin1'):
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


def label_table(pairs):
    """pairs: {label: text bytes}. Strings in label order, index by id."""
    order = sorted(pairs)
    strings, offs = bytearray(), {}
    for name in order:
        offs[name] = len(strings)
        strings += pairs[name] + b'\0'
    table = b''.join(struct.pack('<II', label_id(n), offs[n])
                     for n in sorted(order, key=label_id))
    body = struct.pack('<III', len(order), 28, 28 + len(table)) + PACK.encode().ljust(16, b'\0')
    body += table + strings
    body += bytes(-(len(body) + 8) % 4)
    return struct.pack('<II', 0x00039000, len(body)) + body


EMPTY_HISTOGRAM = struct.pack('<III', 0x00039001, 6148, 256) + bytes(6144)
LANGUAGES = ['Spanish', 'Largest', 'Labels', 'Japanese', 'Italian', 'German', 'French',
             'English', 'Dutch']


# ---- EB3 archive ------------------------------------------------------------------
def path_hash(path):
    value = 5381
    for byte in path[2:] if path.startswith('./') else path:
        value = (value * 33 + ord(byte)) & 0xFFFFFFFF
    return value


def eb3_archive(files):
    """files: [(path, bytes)] in data order; './name' for root files."""
    nodes = [['', 0, 0, [], None]]           # name, type, parent, children, file index
    dir_node, dir_order = {'.': 0}, []
    for index, (path, _) in enumerate(files):
        folder, name = path.rsplit('/', 1)
        if folder not in dir_order:
            dir_order.append(folder)
        current, walked = 0, ''
        for part in ([] if folder == '.' else folder.split('/')):
            walked = f'{walked}/{part}' if walked else part
            if walked not in dir_node:
                nodes.append([part, 0, current, [], None])
                dir_node[walked] = len(nodes) - 1
                nodes[current][3].append(len(nodes) - 1)
            current = dir_node[walked]
        nodes.append([name, 1, current, [], index])
        nodes[current][3].append(len(nodes) - 1)
    strings, name_at = bytearray(), [0]
    for node in nodes[1:]:
        name_at.append(len(strings))
        strings += node[0].encode('ascii') + b'\0'
    kids, first = [], {}
    for i, node in enumerate(nodes):
        if node[1] == 0:
            first[i] = len(kids)
            kids += reversed(node[3])
    tree = bytearray(struct.pack('>I', 20 * len(nodes)))
    for i, (_, kind, parent, children, index) in enumerate(nodes):
        tree += struct.pack('>5i', name_at[i], kind, parent,
                            len(children) if kind == 0 else index, first[i] if kind == 0 else -1)
    tree += struct.pack('>I', 4 * len(kids)) + struct.pack(f'>{len(kids)}I', *kids)
    tree += struct.pack('>I', len(strings)) + strings
    tree += bytes(align(len(tree), 16) - len(tree))

    count = len(files)
    width = max(len(p.rsplit('/', 1)[1]) for p, _ in files) + 3
    dwidth = max(len(d) for d in dir_order) + 1
    names_at = align(48 + 17 * count, 16)
    dirs_at = align(names_at + count * width, 16)
    names_end = align(dirs_at + len(dir_order) * dwidth, 16)
    offsets, cursor = [], align(names_end + len(tree), 64)
    for _, data in files:
        offsets.append(cursor)
        cursor = align(cursor + len(data), 64)
    out = bytearray(cursor)
    struct.pack_into('>4s8I', out, 0, b'EB\0\3', count, 0x00500600, names_at,
                     names_end - names_at, (width << 24) | (dwidth << 16) | len(dir_order),
                     0, cursor, len(tree))
    for slot, i in enumerate(sorted(range(count), key=lambda i: path_hash(files[i][0]))):
        path, data = files[i]
        struct.pack_into('>4I', out, 48 + 16 * slot, offsets[i] >> 6, 0, len(data), path_hash(path))
        folder, name = path.rsplit('/', 1)
        entry = struct.pack('>H', dir_order.index(folder)) + name.encode('ascii') + b'\0'
        out[names_at + slot * width:names_at + slot * width + len(entry)] = entry
    for i, folder in enumerate(dir_order):
        entry = folder.encode('ascii') + b'\0'
        out[dirs_at + i * dwidth:dirs_at + i * dwidth + len(entry)] = entry
    out[names_end:names_end + len(tree)] = tree
    for (_, data), offset in zip(files, offsets):
        out[offset:offset + len(data)] = data
    return bytes(out)


# ---- spawns -------------------------------------------------------------------------
def load_spawns():
    config = json.loads((HERE / 'nsv_spawns.json').read_text(encoding='utf-8'))
    locators = json.loads((HERE / 'nsv_locators.json').read_text(encoding='utf-8'))
    spawns, used = [], set()
    if len(config['spawns']) > len(PLACEHOLDERS):
        raise SystemExit(f'at most {len(PLACEHOLDERS)} spawns (add placeholders)')
    for placeholder, item in zip(PLACEHOLDERS, config['spawns']):
        title = item['title']
        slug = ''.join(c if c.isalnum() else '_' for c in title.lower()).strip('_')
        while '__' in slug:
            slug = slug.replace('__', '_')
        name = 'nsv_' + slug
        if name in used:
            raise SystemExit(f'duplicate spawn title {title!r}')
        used.add(name)
        if 'transform' in item:
            m, source = [float(x) for x in item['transform']], 'transform'
        elif 'position' in item:
            h = math.radians(float(item.get('heading_degrees', 0)))
            x, y, z = (float(c) for c in item['position'])
            m = [math.cos(h), 0, -math.sin(h), 0, 0, 1, 0, 0, math.sin(h), 0, math.cos(h), 0,
                 x, y, z, 1]
            source = 'position'
        else:
            source = item['skate2_locator']
            if source not in locators:
                raise SystemExit(f'unknown Skate 2 locator {source!r}')
            m = locators[source]
        if len(m) != 16:
            raise SystemExit(f'{title}: transform needs 16 floats')
        description = item.get('description', '').strip()
        # The Skate 2 world stream to load (default BAM); DLC map streams come
        # from tools/build_skate2_dlc_maps.py.
        stream = item.get('stream', 'BAM')
        if not stream or not all(c.isalnum() or c == '_' for c in stream) or len(stream) > 63:
            raise SystemExit(f'{title}: invalid stream {stream!r}')
        spawns.append(dict(name=name, title=title, label='ID_' + name.upper(),
                           desc_label=('ID_' + name.upper() + '_DESC') if description
                           else 'ID_NSV_LOC_DESC',
                           description=description, stream=stream,
                           placeholder=placeholder, matrix=m, source=source))
    return spawns


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--root', default=str(REPO / 'out' / 'build' / 'relwithdebinfo'))
    ap.add_argument('--uninstall', action='store_true')
    args = ap.parse_args()
    root = Path(args.root)
    content = root / '0000000000000000' / f'{TITLE_ID:08X}'
    package = content / '00000002' / PACKAGE_DIR
    header = content / 'Headers' / '00000002' / f'{PACKAGE_DIR}.header'
    table = root / 'mods' / 'skate2' / 'nsv_spawns.txt'
    if args.uninstall:
        shutil.rmtree(package, ignore_errors=True)
        for path in (header, table):
            path.unlink(missing_ok=True)
        print(f'removed {package}, {header.name}, {table}')
        return

    spawns = load_spawns()
    extra = audio_records(root) + livingworld_records(root)
    vlt, binf = build_vault(spawns, extra)
    texts = {'ID_NSV_MAP_HELPER': 'The city that made you famous and put you in a cast. Twice.',
             'ID_NSV_LOC_DESC': 'Same city, same security guards, same stairs you slammed on.'}
    texts.update({s['label']: s['title'] for s in spawns})
    texts.update({s['desc_label']: s['description'] for s in spawns if s['description']})
    files = [('./newsanvn.ver', b'692631:sk8build'),
             (f'data/db/dlc_{PACK}.bin', binf),
             (f'data/db/dlc_{PACK}.opt', b'EndC' + struct.pack('>I', 16) + bytes(8)),
             (f'data/db/dlc_{PACK}.vaultlist.xml',
              ('<?xml version="1.0" encoding="utf-8"?>\r\n<VaultList>\r\n'
               f'  <VaultFile>dlc_{PACK}.vlt</VaultFile>\r\n</VaultList>').encode()),
             (f'data/db/dlc_{PACK}.vlt', vlt)]
    for lang in LANGUAGES:
        folder = f'data/fe/languages/{lang.lower()}'
        pairs = {k: (k if lang == 'Labels' else v).encode('latin1') for k, v in texts.items()}
        files.append((f'{folder}/LANGUAGE_{lang}_{PACK}_skate3ng-dlc_{PACK}.BIN',
                      label_table(pairs)))
        files.append((f'{folder}/LANGUAGE_{lang}_HISTOGRAM_skate3ng-dlc_{PACK}.BIN',
                      EMPTY_HISTOGRAM))
    archive = eb3_archive(files)

    # Additive install: a new content folder and header; retail files untouched.
    # A synthetic local content header must not depend on optional retail DLC.
    package.mkdir(parents=True, exist_ok=True)
    (package / f'{PACK}_00000000.big').write_bytes(archive)
    data = bytearray(332)  # XCONTENT_AGGREGATE_DATA + native-endian license mask
    struct.pack_into('>II', data, 0, 1, 2)  # HDD, marketplace content
    struct.pack_into('>I', data, 0x140, TITLE_ID)
    struct.pack_into('<I', data, 0x148, 0xFFFFFFFF)
    data[8:0x108] = DISPLAY_NAME.encode('utf-16-be').ljust(0x100, b'\0')
    data[0x108:0x134] = PACKAGE_DIR.encode('ascii').ljust(0x2C, b'\0')
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_bytes(bytes(data))

    lines = ['# New San Van Locations-menu spawns, written by tools/build_nsv_dlc.py from',
             '# tools/nsv_spawns.json. <borrowed menu locator> <16 floats, rows>',
             '# [stream=<Skate 2 world stream>, default BAM] # title']
    for s in spawns:
        values = ' '.join(f'{x:.6f}' for x in s['matrix'])
        stream = f" stream={s['stream']}" if s['stream'] != 'BAM' else ''
        lines.append(f"{s['placeholder']} {values}{stream} # {s['title']} ({s['source']})")
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'installed {package.name}/{PACK}_00000000.big ({len(archive)} bytes, {len(files)} files, '
          f'{len(spawns)} spawns, {len(extra)} extra records); header {header.name}; '
          f'spawn table {table}')


if __name__ == '__main__':
    main()
