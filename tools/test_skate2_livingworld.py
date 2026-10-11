"""Validate the original-actor preparation and DLC relocation path offline."""
import json
import struct
import mmap
import hashlib
import re
from pathlib import Path

from attrib_vault import Vault
from build_nsv_dlc import build_vault, audio_records, livingworld_records, load_spawns, Vault as Writer
from vlt64 import h64
from world_archive import entries

ROOT = Path(__file__).resolve().parent.parent/'out/build/relwithdebinfo'
OUT = ROOT/'mods/skate2/livingworld-v1'
RESEARCH = ROOT.parents[1]/'research/skate2_livingworld/validation'


def eager_inheritance(vault):
    """Model 82B71D60's one-time parent lookup, then 82B722C0's traversal."""
    loaded, parents = {}, {}
    for record in vault.records():
        key = (record.cls, record.collection)
        parents[key] = loaded.get((record.cls, record.parent)) if record.parent else None
        loaded[key] = record

    def inherited(record, attribute):
        while record:
            entry = next((e for e in record.entries if e.key == attribute), None)
            if entry:
                return entry
            record = parents[(record.cls, record.collection)]
        return None
    return loaded, parents, inherited


def check_export_order():
    # Exercise the actual serializer with deliberately reversed input. Also
    # retain external base-vault parents, and reject cycles/duplicate keys.
    writer = Writer('parent_order')
    writer.record('test', 'child', h64('parent'), [], [])
    writer.record('test', 'external', h64('base'), [], [])
    writer.record('test', 'parent', 0, [h64('int32')], [(h64('value'), 0, 0x4000, 3)])
    records, parents, inherited = eager_inheritance(Vault(*writer.build()))
    child = records[(h64('test'), h64('child'))]
    assert inherited(child, h64('value')).value == 3
    assert records[(h64('test'), h64('external'))].parent == h64('base')
    for kind in ('cycle', 'duplicate'):
        writer = Writer(kind)
        writer.record('test', 'one', h64('two') if kind == 'cycle' else 0, [], [])
        writer.record('test', 'two' if kind == 'cycle' else 'one', h64('one'), [], [])
        try:
            writer.build()
        except ValueError:
            pass
        else:
            raise AssertionError(f'{kind} was accepted')


def andale_name_id(name):
    """Mirror 82382B50 / 823C3BF8 / 823C3C78: six base38 chars per word."""
    words = []
    for i in range(0, 36, 6):
        word, power = 0, 38**5
        for char in name[i:i+6]:
            c = ord(char)
            value = c - 86 if c >= 97 else c - 47 if c <= 57 else c - 54 if c <= 90 else c - 58
            word = (word + value * power) & 0xffffffff
            power //= 38
        words.append(word)
    return struct.pack('>6I', *words)


def check_vehicle_poses(prepared, actual, records, inherited):
    from world_archive import decode_archive_payload
    def vehicle_db(path):
        with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            entry = next(e for e in entries(data) if e.name.lower().endswith('/vehiclesdb.abin'))
            return decode_archive_payload(bytes(data[entry.offset:entry.offset+entry.size]))
    native_db = vehicle_db(ROOT/'game/data/big/miscload.big')
    source_db = vehicle_db(Path(__file__).resolve().parents[2]/'Skate 2/data/big/anim.big')
    assert native_db == source_db, 'native vehicle poses must match original source'
    checked = 0
    group = records[(h64('livingworld_load_groups'), h64('nsv_s2_vehicles'))]
    entry = group.entries[0]
    count, _, stride, _ = struct.unpack_from('>4H', actual.bin, entry.target)
    for i in range(count):
        cls, coll, _ = struct.unpack_from('>QQQ', actual.bin, entry.target+8+i*stride)
        model = records[(cls,coll)]
        name = actual.string(actual.pointers[('bin',model.layout)])
        assert inherited(model, 0xA75AF7F3CFABE6E1).value == 0x01000000, 'animated vehicle needs its pose template'
        assert native_db.find(andale_name_id(name)) < 0, 'namespaced name unexpectedly resolves'
        assert native_db.find(andale_name_id(name.removeprefix('nsv_s2_'))) >= 0, name
        # 828DA3E0 tests linked ancestors. Our isolated ancestry must be
        # enumerated through its own root, not the canonical vehicles root.
        ancestor = model
        isolated, canonical = False, False
        while ancestor.parent:
            isolated |= ancestor.parent == h64('nsv_s2_vehicles')
            canonical |= ancestor.parent == h64('vehicles')
            ancestor = records[(ancestor.cls,ancestor.parent)]
        assert isolated and not canonical
        checked += 1
    assert checked == 16
    print(f'PASS: {checked} original animation IDs resolve only after namespace adaptation; canonical query excludes isolated records; VehiclesDB is byte-identical')


def check_pose_hook_callsites():
    # Validate the real PPC instruction preceding each hook's LR guard. This
    # catches call-address vs return-address mistakes that asset tests cannot.
    root = Path(__file__).resolve().parent.parent
    image = (root/'out/research/xex_image_tu/default_82000000_011B0000.bin').read_bytes()
    source = (root/'src/skate3_world_loading.cpp').read_text()
    for target in (0x828DA2C8, 0x823C3B00, 0x82D196A8):
        body = source.split(f'REX_FUNC(sub_{target:08X}) {{', 1)[1].split('\n}', 1)[0]
        lr = int(re.search(r'uint32_t\(ctx.lr\) == (0x[0-9A-F]+)', body)[1], 16)
        address = lr - 4
        word = struct.unpack_from('>I', image, address - 0x82000000)[0]
        assert word >> 26 == 18 and word & 3 == 1, (hex(target), hex(lr), 'expected relative bl')
        displacement = word & 0x03FFFFFC
        if displacement & 0x02000000: displacement -= 0x04000000
        assert address + displacement == target, (hex(target), hex(lr), 'wrong return address')
    print('PASS: all three pose hook return addresses match the actual PPC calls')


def main():
    check_pose_hook_callsites()
    check_export_order()
    prepared = livingworld_records(ROOT)
    assert prepared, 'prepare the actor import first'
    audio = audio_records(ROOT)
    combined = build_vault(load_spawns(), audio + prepared)
    actual = Vault(*combined)
    records = {(r.cls, r.collection): r for r in actual.records()}
    baseline = Vault(*build_vault(load_spawns(), audio))
    base_records = {(r.cls, r.collection): r for r in baseline.records()}
    # Existing menu/audio records keep their keys, parents, schemas and raw
    # fixed data. Relocated pointers are intentionally stored in PtrN.
    for key, old in base_records.items():
        new = records[key]
        assert (old.parent, old.types) == (new.parent, new.types)
        assert baseline.bin[old.layout:old.layout+32] == actual.bin[new.layout:new.layout+32] if old.layout else new.layout is None
        assert [(e.key, e.type, e.flags, e.value) for e in old.entries] == [
            (e.key, e.type, e.flags, e.value) for e in new.entries]
    expected = {(h64(r['class']), h64(r['collection'])) for r in prepared}
    assert len(expected) == len(prepared)
    _, parents, inherited = eager_inheritance(actual)
    for key in expected:
        record = records[key]
        assert not record.parent or parents[key] is not None, f'parent loaded after child: {key}'
    recipes = 0
    for r in prepared:
        new = records[(h64(r['class']), h64(r['collection']))]
        if new.parent:
            assert (new.cls, new.parent) in expected
        block = r['layout']
        for entry in r['entries']:
            if 'data' not in entry:
                continue
            data = bytes.fromhex(entry['data'])
            if entry['flags'] & 0x200:
                count, capacity, stride, flags = struct.unpack_from('>4H', data)
                begin = 16 if flags & 0x8000 and stride == 16 else 8
                assert len(data) >= begin + capacity*stride, (r['collection'], entry['key'])
            elif entry['flags'] & 0x800:
                assert len(data) >= 24
        if block:
            assert new.layout is not None
            data = bytes.fromhex(block['data'])
            assert actual.bin[new.layout:new.layout+len(data)] == data
            for p in block.get('pointers', []):
                target = actual.pointers[('bin', new.layout+p['offset'])]
                assert (actual.string(target) or '') == p['string']
                if r['class'] == 'livingworld_models' and p['string'].startswith('nsv_s2_'):
                    recipes += 1
        for e in new.entries:
            if e.target is None or not e.flags & 0x200:
                continue
            count, capacity, stride, _ = struct.unpack_from('>4H', actual.bin, e.target)
            assert count <= capacity and stride > 0
            assert e.target + 8 + capacity*stride <= len(actual.bin)
            # Arrays of RefSpec / LW weighted references have no leading
            # vector alignment pad; their internal collection keys must
            # resolve to the isolated imported records.
            if e.flags & 0x800:
                for i in range(count):
                    cls, coll = struct.unpack_from('>QQ', actual.bin, e.target+8+i*stride)
                    if cls in {h64('livingworld_models'), h64('livingworld_entities'),
                               h64('livingworld_entitycategories'), h64('livingworld_categorygroups')}:
                        assert (cls, coll) in expected
    assert recipes >= 50
    vehicles = records[(h64('livingworld_load_groups'), h64('nsv_s2_vehicles'))]
    e = vehicles.entries[0]
    n, _, stride, _ = struct.unpack_from('>4H', actual.bin, e.target)
    assert n == 16 and stride == 24
    for i in range(n):
        cls, coll, cache = struct.unpack_from('>QQQ', actual.bin, e.target+8+i*24)
        model = records[(cls, coll)]
        assert inherited(model, 0xF6897CC7B637850C).value == 3, 'native recipe family must be vehicle'
        name = actual.string(actual.pointers[('bin', model.layout)])
        assert name.startswith('nsv_s2_') and cache == 0
        path = OUT/'loose/data/content/recipe/nsv_s2_vehicle'/f'{name}.recipe'
        data = path.read_bytes()
        length = struct.unpack_from('>I', data, 4)[0]
        assert data[8:8+length].rstrip(b'\0').decode() == name
        assert struct.unpack_from('>I', actual.bin, model.layout+4)[0] == 104
    members = {}
    for group_name, expected_count in [('vehicles', 16), ('pedestrians', 38), ('actors', 54)]:
        group = records[(h64('livingworld_load_groups'), h64('nsv_s2_' + group_name))]
        entry = group.entries[0]
        count, _, stride, _ = struct.unpack_from('>4H', actual.bin, entry.target)
        assert count == expected_count and stride == 24
        keys = set()
        for i in range(count):
            cls, coll, cache = struct.unpack_from('>QQQ', actual.bin, entry.target+8+i*stride)
            assert cache == 0 and (cls, coll) in records
            keys.add((cls, coll))
            if group_name == 'pedestrians':
                model = records[(cls, coll)]
                name = actual.string(actual.pointers[('bin', model.layout)])
                assert (OUT/'loose/data/content/recipe/nsv_s2_livingworld'/f'{name}.recipe').is_file()
                assert struct.unpack_from('>I', actual.bin, model.layout+4)[0] == 104
        assert len(keys) == expected_count
        members[group_name] = keys
    assert not members['vehicles'] & members['pedestrians']
    assert members['actors'] == members['vehicles'] | members['pedestrians']
    print('PASS: 38 pedestrian recipes, isolated silent voice IDs, combined group retains all 16 cars')
    paint = (OUT/'worldpainter.txt').read_text().splitlines()
    layers, census = {}, {}
    for line in paint:
        if line.startswith('#') or not line.strip():
            continue
        kind, ident, key, name = line.split()
        if kind == 'layer':
            assert int(ident) not in layers
            layers[int(ident)] = (int(key, 16), name)
            assert int(key, 16) == h64(name)
        else:
            assert kind == 'value' and int(ident) not in census
            census[int(ident)] = name
            assert int(key, 16) == h64(name)
            assert (h64('livingworld_census'), int(key, 16)) in records
    assert set(layers) == {2, 5}
    assert len(census) == 18
    assert sum(n.endswith('_peds_census') for n in census.values()) == 10
    assert sum(n.endswith('_vehicles_census') for n in census.values()) == 8
    print('PASS: 10 pedestrian and 8 vehicle census values resolve in the installed vault')
    marker = OUT/'LIVINGWORLD_COMPLETE_V1'
    assert marker.is_file()
    check_vehicle_poses(prepared, actual, records, inherited)
    original = {int(s, 16) for s in (OUT/'texture_ids.txt').read_text().splitlines()[2:]}
    with (ROOT/'game/data/content/livingworld.big').open('rb') as f, mmap.mmap(
            f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        native = {int(Path(e.name).stem, 16) for e in entries(data)
                  if '/texture/' in e.name and e.name.endswith('.rx2')}
    assert not original & native, 'original textures must not redirect native actor resources'
    inventory = (OUT/'runtime_files.txt').read_text().splitlines()
    assert inventory[0] == 'NSV_LW_FILES_V2' and int(inventory[1]) == len(inventory)-2
    paths = set()
    for row in inventory[2:]:
        name, size, header = row.split()
        assert name not in paths
        paths.add(name)
        data = (OUT/'loose'/name).read_bytes()
        assert len(data) == int(size) and data[:8].hex() == header
    assets = json.loads((OUT/'assets.json').read_bytes())
    assert {a['target'] for a in assets} <= paths, 'all actor dependencies must be checked at startup'
    banks = json.loads((OUT/'traffic_audio.json').read_bytes())
    assert len(banks) == 10
    for bank in banks:
        data = (OUT/'loose'/bank['target']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == bank['sha256']
    # Native 828E3250/828E3148 require the per-name ID even on the
    # second registry-search pass. Compare the exact externals, not name sets.
    from build_skate2_livingworld import read_big
    from skate2_traffic_audio import csi_bindings, abk_bindings, retarget_traffic_bank
    native_audio = read_big(ROOT/'game/data/audio/audiofiles.big')
    native_csi = next(d for n,d in native_audio.items() if n.endswith('/AEMS_TRAFFIC.csi'))
    source_audio = read_big(ROOT.parents[3]/'Skate 2/data/audio/vehicle.big')
    source_csi = source_audio['AEMS_TRAFFIC.csi']
    source_registry, native_registry = csi_bindings(source_csi), csi_bindings(native_csi)
    assert source_registry.keys() == native_registry.keys()
    cue_failures, binding_checks = 0, 0
    for bank in banks:
        source_bank = source_audio[bank['source']]
        installed_bank = (OUT/'loose'/bank['target']).read_bytes()
        adapted, bindings = retarget_traffic_bank(source_bank, source_csi, native_csi)
        assert adapted == installed_bank and len(adapted) == len(source_bank)
        assert hashlib.sha256(source_bank).hexdigest() == bank['source_sha256']
        changed = {b['offset'] + i for b in bindings for i in range(4)}
        assert all(a == b for i,(a,b) in enumerate(zip(source_bank, adapted)) if i not in changed), 'sound/graph payload changed'
        for b in abk_bindings(adapted):
            assert b['ids'] == native_registry[(b['kind'],b['name'])]
            binding_checks += 1
        for b in bindings:
            key = b['kind'], b['name']
            if b['kind'] == 1:
                # Reproduce the binder's mandatory ID + string comparison:
                # original IDs fail, translated IDs succeed in native CSI.
                assert b['ids'][1] != native_registry[key][1]
                cue_failures += 1
        broken = bytearray(source_bank)
        broken[bindings[0]['offset']] ^= 1
        try:
            retarget_traffic_bank(bytes(broken), source_csi, native_csi)
        except ValueError:
            pass
        else:
            raise AssertionError('mismatched source reference accepted')
    assert cue_failures == 10 and binding_checks >= 30
    native_banks = {Path(n).name:d for n,d in native_audio.items() if n.endswith('.abk')}
    for bank in banks:
        source_bank = (OUT/'loose'/bank['target']).read_bytes()
        native_bank = native_banks[Path(bank['source']).name]
        assert source_bank[:16] == native_bank[:16]
        assert source_bank != native_bank
    # Consumed RPM/engine selector fields and native default attributes are
    # already identical. The S2 default's padding/RefSpec layout differs, so
    # it must not be copied wholesale into S3.
    db = ROOT.parents[1]/'research/re-tools'
    source_vault = Vault((db/'s2db/skater.vlt').read_bytes(),(db/'s2db/skater.bin').read_bytes())
    native_vault = Vault((db/'s3db/skatercollections.vlt').read_bytes(),(db/'s3db/skatercollections.bin').read_bytes())
    source_records, _, source_inherited = eager_inheritance(source_vault)
    inherited_checks = 0
    for r in prepared:
        if 'source' not in r:
            continue
        source_key = int(r['source'][3:], 16) if r['source'].startswith('id_') else h64(r['source'])
        original_record = source_records[(h64(r['class']), source_key)]
        imported_record = records[(h64(r['class']), h64(r['collection']))]
        ancestor = original_record
        while ancestor:
            for original_entry in ancestor.entries:
                # Compare effective values, including child overrides. This
                # catches lost driver/entity/model properties beyond family.
                original_entry = source_inherited(original_record, original_entry.key)
                imported_entry = inherited(imported_record, original_entry.key)
                assert imported_entry is not None, (r['source'], original_entry.key)
                assert (original_entry.type, original_entry.flags) == (imported_entry.type, imported_entry.flags)
                if original_entry.target is None:
                    assert imported_entry.value == original_entry.value, (r['source'], original_entry.key)
                inherited_checks += 1
            ancestor = source_records.get((ancestor.cls, ancestor.parent)) if ancestor.parent else None
    source_engine = {r.collection:r for r in source_vault.records() if r.cls==h64('aud_traffic_engine')}
    native_engine = {r.collection:r for r in native_vault.records() if r.cls==h64('aud_traffic_engine')}
    for key, record in source_engine.items():
        native_record = native_engine[key]
        assert source_vault.bin[record.layout:record.layout+12] == native_vault.bin[native_record.layout:native_record.layout+12]
        original_values = {e.key:e.value for e in record.entries}
        for e in native_record.entries:
            if e.flags & 0x4000: assert e.value == original_values[e.key]
    print(f'PASS: {len(paths)} complete actor dependencies; 10 original traffic banks; {binding_checks} external bindings; {cue_failures} original cue failures corrected; consumed tuning matches')
    print(f'PASS: eager native inheritance; {inherited_checks} original effective properties; all 16 vehicle families; reversed input/cycles/duplicates')
    print(f'PASS: {len(prepared)} isolated records; {recipes} relocated original recipe names; '
          f'{n} original vehicle variants; menu/audio records preserved')
    # Save the package payload for inspection, without installing it while
    # the user runs the diagnostic executable and existing DLC package.
    RESEARCH.mkdir(parents=True, exist_ok=True)
    (RESEARCH/'validated.vlt').write_bytes(combined[0])
    (RESEARCH/'validated.bin').write_bytes(combined[1])


if __name__ == '__main__':
    main()
