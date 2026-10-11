"""Read-only log124 living-world audit. Output is not mounted or installed.

Run from the repository root. Uses the existing re-tools vault extracts.
Never writes game data, DLC packages, settings, or the active paint manifest.
"""
from pathlib import Path
import collections
import json
import hashlib
import mmap
import re
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'out/research/re-tools'))
import rw_kind as rw
import skate2_worldpainter as wp
from attrib_vault import Vault
from vlt64 import h64
from world_archive import entries as archive_entries, decode_archive_payload
from inspect_sfil import unpack, assets, dictionary

OUT = ROOT / 'out/research/skate2_livingworld/integration-audit'
REV = json.loads((ROOT / 'out/research/re-tools/h64rev2.json').read_text())


def name(k):
    return REV.get(f'{k:016x}', f'{k:016X}')


def write(filename, value):
    OUT.mkdir(exist_ok=True)
    (OUT / filename).write_bytes((json.dumps(value, indent=2) + '\n').encode())


def paths(files):
    result = []
    for cell, guid, arena, _, obj, _ in rw.objects(files, 0xEB0014):
        b = arena[obj['pointer']:obj['pointer'] + obj['size']]
        count, start = struct.unpack_from('>II', b)
        assert start + count * 0x60 <= len(b)
        for i in range(count):
            at = start + i * 0x60
            nodes, n, aux, links, nl, flags = struct.unpack_from('>6I', b, at + 0x30)
            assert at + nodes + n * 0x2C <= len(b)
            assert at + links + nl * 0xC <= len(b)
            node_rows = []
            for j in range(n):
                q = at + nodes + j * 0x2C
                relative = struct.unpack_from('>I', b, q + 0x20)[0]
                assert not relative or q + relative < len(b)
                node_rows.append(struct.unpack_from('>11I', b, q))
            result.append(dict(cell=cell, index=i, id=b[at+0x20:at+0x30].hex(),
                               bounds=struct.unpack_from('>8f', b, at),
                               nodes=n, links=nl, flags=flags,
                               tail=b[at+0x48:at+0x60].hex(),
                               node_samples=node_rows[:3]))
    return result


def rain(files):
    """Validate the road-network structure walked by 82962890/826B1B28.

    Zero-count arrays can retain unused poison offsets. Never dereference them.
    Counts are streamed instances, not deduplicated world totals.
    """
    rows = []
    for cell, _, arena, _, obj, _ in rw.objects(files, 0xEB0013):
        b = arena[obj['pointer']:obj['pointer'] + obj['size']]
        ni, nr, ns, oi, ore, os = struct.unpack_from('>6I', b, 0x20)
        for n, at, stride in [(ni, oi, 0x260), (nr, ore, 0xA0), (ns, os, 0x40)]:
            assert not n or 0x40 <= at <= len(b) - n * stride
        roads = []
        for i in range(nr):
            at = ore + i * 0xA0
            n, rel = struct.unpack_from('>II', b, at + 0x78)
            assert not n or at + rel + n * 0xE0 <= len(b)
            roads.append(dict(id=b[at:at+8].hex(), superroad=b[at+0x98:at+0xA0].hex(),
                              subrecords=n))
        for i in range(ni):
            at = oi + i * 0x260
            rel, n = struct.unpack_from('>II', b, at + 0x250)
            assert not n or at + rel + n * 0x70 <= len(b)
        signal_flags = [struct.unpack_from('>I', b, oi+i*0x260+0x244)[0] for i in range(ni)]
        rows.append(dict(cell=cell, intersections=ni, roads=nr, superroads=ns,
                         intersection_signal_flags=signal_flags,
                         road_records=roads,
                         superroad_ids=[b[os+i*0x40:os+i*0x40+8].hex() for i in range(ns)]))
    return rows


def inventory(files):
    cells, total = [], collections.Counter()
    arenas = 0
    for cell, data in files:
        counts = collections.Counter()
        for _, _, (arena, _) in assets(data, include_resource=False):
            arenas += 1
            counts.update(f'{o["kind"]:06X}' for o in dictionary(arena))
        cells.append(dict(cell=cell, kinds=dict(counts)))
        total.update(counts)
    return dict(files=len(files), arenas=arenas, kinds=dict(sorted(total.items())), cells=cells)


def nav_tiles(files):
    rows = []
    for cell, _, arena, _, obj, _ in rw.objects(files, 0xEB0027):
        b = arena[obj['pointer']:obj['pointer'] + obj['size']]
        count, table = struct.unpack_from('>II', b, 4)
        payload = struct.unpack_from('>I', b, 16)[0]
        assert table + count * 4 <= len(b) and payload < len(b)
        pointers = struct.unpack_from('>' + 'I' * count, b, table)
        assert all(payload <= x < len(b) for x in pointers)
        rows.append(dict(cell=cell, bytes=len(b), count=count, table=table, payload=payload,
                         tile_offsets=pointers, payload_prefix=b[payload:payload+48].hex()))
    return rows


def actor_audit():
    games = [('s2', ROOT.parent/'Skate 2/data'), ('s3', ROOT/'out/build/relwithdebinfo/game/data')]
    result, graphs, audio, audio_bytes = {}, {}, {}, {}
    for label, folder in games:
        graph_data, skeletons, recipes = {}, {}, []
        with (folder/'big/miscload.big').open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for e in archive_entries(data):
                if e.name.startswith('data/state/livingworldentities/'):
                    graph_data[e.name.lower()] = decode_archive_payload(bytes(data[e.offset:e.offset+e.size]))
                if 'pedestrianskeleton' in e.name.lower():
                    b = decode_archive_payload(bytes(data[e.offset:e.offset+e.size]))
                    skeletons[e.name] = dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest(), header=b[:32].hex())
        graphs[label] = graph_data
        # Skate 2 keeps the pedestrian animation banks in anim.big rather than
        # miscload.big. Do not infer missing rigs from one archive's index.
        for path in [folder/'big/anim.big', folder/'big/miscboot.big']:
            if not path.exists():
                continue
            with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                for e in archive_entries(data):
                    if 'pedestrianskeleton' not in e.name.lower():
                        continue
                    b = decode_archive_payload(bytes(data[e.offset:e.offset+e.size]))
                    skeletons[e.name] = dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest(), header=b[:32].hex())
        with (folder/'content/livingworld.big').open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            archive = archive_entries(data)
            for e in archive:
                if '/recipe/' not in e.name or not e.name.endswith('.xml'):
                    continue
                b = decode_archive_payload(bytes(data[e.offset:e.offset+e.size]))
                text = b.decode('utf-8')
                recipes.append(dict(path=e.name, bytes=len(b), schemas=re.findall(r'schemaversion="([^"]+)"', text),
                                    material_types=sorted(set(re.findall(r'<mat[^>]+type="([^"]+)"', text))),
                                    arena_ids=sorted(set(re.findall(r'arenaid="([^"]+)"', text)))))
            result[label] = dict(archive_entries=len(archive), rx2=sum(e.name.endswith('.rx2') for e in archive),
                                 xml_recipes=recipes, compiled_recipes=sum(e.name.endswith('.recipe') for e in archive),
                                 skeletons=skeletons,
                                 graphs={p:dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest()) for p,b in graph_data.items()})
        names, bank_data = [], {}
        audio_paths = [folder/'audio/vehicle.big', folder/'audio/pedestrian.big'] if label == 's2' else [folder/'audio/audiofiles.big']
        for path in audio_paths:
            with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                for e in archive_entries(data):
                    names.append(e.name)
                    # Compare only the traffic/foley banks, avoiding unrelated
                    # music and other large resident audio files.
                    if label == 's2' or Path(e.name).name.lower() in {Path(n).name.lower() for n in audio['s2']}:
                        bank_data[Path(e.name).name.lower()] = decode_archive_payload(bytes(data[e.offset:e.offset+e.size]))
        audio[label] = names
        audio_bytes[label] = bank_data
    common = set(graphs['s2']) & set(graphs['s3'])
    result['comparison'] = dict(common=len(common), identical=sum(graphs['s2'][p]==graphs['s3'][p] for p in common),
                                missing_in_s3=sorted(set(graphs['s2'])-set(graphs['s3'])),
                                changed=sorted(p for p in common if graphs['s2'][p]!=graphs['s3'][p]))
    # Names prove presence, not sound tuning or residency. Compare basenames because
    # the two titles place their audio banks in different archive directory roots.
    s3_bases = {Path(n).name.lower() for n in audio['s3']}
    result['audio'] = dict(s2=audio['s2'], s3_entries=len(audio['s3']),
                          s2_basename_present_in_s3=[n for n in audio['s2'] if Path(n).name.lower() in s3_bases],
                          s2_basename_missing_in_s3=[n for n in audio['s2'] if Path(n).name.lower() not in s3_bases],
                          decoded_identical=[n for n,b in audio_bytes['s2'].items() if b==audio_bytes['s3'].get(n)])
    result['audio']['bank_samples'] = {
        label:{n:dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest(), header=b[:32].hex())
               for n,b in banks.items()} for label,banks in audio_bytes.items()}
    write('actor-audit.json', result)
    print('actor graphs:', {k:len(v) for k,v in graphs.items()}, result['comparison']['common'], 'common;',
          result['comparison']['identical'], 'decoded byte-identical; missing', len(result['comparison']['missing_in_s3']))
    print('recipes:', {k:len(result[k]['xml_recipes']) for k,_ in games},
          'audio basename matches', len(result['audio']['s2_basename_present_in_s3']), '/', len(audio['s2']))


def vault_audit():
    folder = ROOT / 'out/research/re-tools/s3db'
    v = Vault((folder/'skatercollections.vlt').read_bytes(),
              (folder/'skatercollections.bin').read_bytes())
    records = {(r.cls, r.collection): r for r in v.records()}

    def inherited(r):
        entries = {}
        if r.parent:
            p = records.get((r.cls, r.parent))
            if p:
                entries.update(inherited(p))
        entries.update({e.key: e for e in r.entries})
        return entries

    def arrays(r):
        out = []
        for e in inherited(r).values():
            if e.target is None or not e.flags & 0x200:
                continue
            n, capacity, stride, _ = struct.unpack_from('>4H', v.bin, e.target)
            if stride not in (24, 32):
                continue
            assert n <= capacity and e.target + 8 + n * stride <= len(v.bin)
            for i in range(n):
                at = e.target + 8 + i * stride
                cls, coll = struct.unpack_from('>QQ', v.bin, at)
                out.append(dict(attribute=name(e.key), cls=name(cls), record=name(coll),
                                resolved=(cls, coll) in records,
                                weight=struct.unpack_from('>f', v.bin, at+24)[0] if stride == 32 else None))
        return out

    group = h64('livingworld_categorygroups')
    paint = wp.stream_layers(ROOT / 'out/build/relwithdebinfo/mods/skate2/world-support-gvr-v1')
    census = []
    for layer, table in [(2, 'npc_census'), (5, 'vehicle_census'), (6, 'security')]:
        for index, occurrences in sorted(paint[layer].items()):
            nm = wp.VALUES[table].get(index)
            matches = [r for r in records.values() if nm and r.collection == h64(nm)]
            row = dict(layer=layer, value=index, name=nm, dictionary_entries=occurrences,
                       classes=[name(r.cls) for r in matches])
            for r in matches:
                if r.cls != h64('livingworld_census'):
                    continue
                cls, coll = struct.unpack_from('>QQ', v.bin, r.layout)
                row.update(category_class=name(cls), category=name(coll),
                           category_resolves=(cls, coll) in records,
                           layout_population=struct.unpack_from('>I', v.bin, r.layout+24)[0])
            census.append(row)
    groups = []
    for r in records.values():
        if r.cls in {group, h64('livingworld_entitycategories'), h64('livingworld_load_groups')}:
            if r.cls == h64('livingworld_load_groups') and name(r.collection) != 'dist_downtown':
                continue
            groups.append(dict(cls=name(r.cls), record=name(r.collection), parent=name(r.parent), refs=arrays(r)))
    write('census-audit.json', dict(paint=census, groups=groups))
    print('painted census/security:', len(census), 'values; missing:',
          [(r['layer'], r['value'], r['name']) for r in census if not r['classes']])
    print('unresolved array references:', [(r['record'], q['cls'], q['record'])
          for r in groups for q in r['refs'] if not q['resolved']])

    # Resolve models from each entity's fixed layout, then compare to load group.
    loaded = {q['record'] for g in groups if g['cls'] == 'livingworld_load_groups' for q in g['refs']}
    entities = []
    for r in records.values():
        if r.cls != h64('livingworld_entities'):
            continue
        refs = []
        if r.layout is not None:
            cls, coll = struct.unpack_from('>QQ', v.bin, r.layout)
            refs.append(dict(cls=name(cls), record=name(coll), resolved=(cls, coll) in records,
                             in_downtown_load_group=name(coll) in loaded))
        entities.append(dict(name=name(r.collection), layout_refs=refs))
    write('entity-models.json', entities)
    entity_models = {e['name']:e['layout_refs'] for e in entities}
    refs_by_record = {(g['cls'], g['record']):g['refs'] for g in groups}
    def model_closure(cls, coll, seen=None):
        seen = set() if seen is None else seen
        key = (cls, coll)
        if key in seen:
            return set()
        seen.add(key)
        if cls == 'livingworld_entities':
            return {q['record'] for q in entity_models[coll] if q['cls']=='livingworld_models'}
        return set().union(*(model_closure(q['cls'], q['record'], seen) for q in refs_by_record.get(key, [])))
    closure = []
    for row in census:
        if row['name']=='district_park01_vehicles_census':
            r = records[(h64('livingworld_census'), h64('district_parks01_vehicles_census'))]
            cls, coll = struct.unpack_from('>QQ', v.bin, r.layout)
            row = dict(row, category_class=name(cls), category=name(coll))
        if 'category' not in row:
            continue
        needed = model_closure(row['category_class'], row['category'])
        closure.append(dict(census=row['name'], models=sorted(needed), not_in_downtown=sorted(needed-loaded)))
    write('load-group-coverage.json', dict(downtown_models=sorted(loaded), painted_census=closure))
    with (ROOT/'out/build/relwithdebinfo/game/data/content/livingworld.big').open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        recipe_names = {Path(e.name).stem.lower() for e in archive_entries(data) if e.name.endswith('.recipe')}
    recipe_coverage = []
    for model in sorted({m for row in closure for m in row['models']}):
        r = records[(h64('livingworld_models'), h64(model))]
        target = v.pointers.get(('bin', r.layout)) if r.layout is not None else None
        recipe = v.string(target)
        recipe_coverage.append(dict(model=model, recipe=recipe,
                                    indexed_compiled_recipe=bool(recipe and recipe.lower() in recipe_names),
                                    in_downtown_load_group=model in loaded))
    write('model-recipe-coverage.json', recipe_coverage)
    print('painted models with no matching indexed native recipe:',
          [(r['model'], r['recipe']) for r in recipe_coverage if not r['indexed_compiled_recipe']])
    print('painted category models absent from Downtown load group:',
          sorted({m for r in closure for m in r['not_in_downtown']}))
    plan = []
    for row in census:
        if not row['name']:
            continue
        dest = row['name']
        if dest == 'district_park01_vehicles_census':
            dest = 'district_parks01_vehicles_census'
        security = row['layer']==6
        allowed = not security or dest in {'NoSkateZone_Area', 'HighAlert_Area'}
        plan.append(dict(source_layer=row['layer'], source_value=row['value'], source_name=row['name'],
                         proposed_layer={2:'livingworld_npc_census',5:'livingworld_vehicle_census',6:'livingworld_security_guard_areas'}[row['layer']],
                         target=dest if allowed else None, key=f'{h64(dest):016X}' if allowed else None,
                         record_resolves=any(r.collection==h64(dest) for r in records.values()),
                         note='sentinel; not a vault record' if security and allowed else 'buffer consumer not proven' if security else 'census record'))
    write('census-translation-plan.json', dict(installed=False, unknown_values='zero; never pass a Skate 2 index to a Skate 3 record lookup', mappings=plan))


def main():
    inventories = {}
    for label, files in [('downtown', rw.s3_sims()), ('bam', rw.bam_sims())]:
        inventories[label] = inventory(files)
        tiles = nav_tiles(files)
        write(label + '-navpower.json', tiles)
        print(label, 'NavPower objects', len(tiles), 'tiles', sum(r['count'] for r in tiles))
        rows = paths(files)
        write(label + '-paths.json', rows)
        print(label, 'paths', len(rows), 'nodes', sum(r['nodes'] for r in rows),
              'link records', sum(r['links'] for r in rows),
              'flags', dict(collections.Counter(r['flags'] for r in rows)))
        roads = rain(files)
        write(label + '-roads.json', roads)
        ids = {s for r in roads for s in r['superroad_ids']}
        missing = [x for r in roads for x in r['road_records'] if x['superroad'] not in ids]
        print(label, 'RAIN intersections/roads/superroads:',
              [sum(r[k] for r in roads) for k in ['intersections', 'roads', 'superroads']],
              'missing superroad references', len(missing))
    write('rw-inventory.json', inventories)
    vault_audit()
    actor_audit()


if __name__ == '__main__':
    main()
