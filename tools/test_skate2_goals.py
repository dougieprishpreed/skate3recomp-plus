"""Offline checks for the New San Van Skate 2 goals package (tools/build_skate2_goals.py).

Run from the repository root after the builder installed into out/build/relwithdebinfo:
  python tools/test_skate2_goals.py [--rebuild]
--rebuild also rebuilds the package from the original data and requires identical files.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_nsv_dlc as dlc                      # noqa: E402
import build_skate2_goals as goals               # noqa: E402
from attrib_vault import Vault                   # noqa: E402
from vlt64 import h64                            # noqa: E402

ROOT = HERE.parent / 'out' / 'build' / 'relwithdebinfo'
CONTENT = ROOT / '0000000000000000' / f'{dlc.TITLE_ID:08X}'
PACKAGE = CONTENT / '00000002' / goals.PACKAGE_DIR
ARCHIVE = PACKAGE / f'{goals.PACK}_00000000.big'
HEADER = CONTENT / 'Headers' / '00000002' / f'{goals.PACKAGE_DIR}.header'
STAGE = ROOT / 'mods' / 'skate2' / goals.STAGE
MALOOF = CONTENT / '00000002' / '0165E5B906309F63ACA64BC2DF01667B9930C47D45' / 'maloof_money_cup_00000000.big'


def check_writers():
    pairs = {'ID_A': b'Alpha', 'ID_B': b'', 'ID_LONG_LABEL_NAME': b'x' * 37}
    assert goals.label_table(pairs, dlc.PACK) == dlc.label_table(pairs), 'label table writer drifted'
    assert goals.read_label_table(dlc.label_table(pairs)) == {dlc.label_id(k): v for k, v in pairs.items()}
    assert goals.histogram([b'plain ascii']) == dlc.EMPTY_HISTOGRAM
    files = [('./x.ver', b'1:sk8build'), ('data/db/a.vlt', b'\1' * 70), ('data/db/sub/b.bin', b''),
             ('data/content/missions/m/m_Sim.xsm', bytes(range(200)))]
    assert goals.read_dlc_archive(dlc.eb3_archive(files)) == dict(files), 'DLC archive does not round-trip'


def check_retail_locator_tables():
    """The .loc generator reproduces every retail Maloof .loc file from its arena."""
    if not MALOOF.is_file():
        print('skip: Maloof Money Cup package not installed')
        return 0
    files = goals.read_dlc_archive(MALOOF.read_bytes())
    count = 0
    for path, data in files.items():
        if not path.endswith('.loc') or '_Proc_' in path or not path.startswith(
                ('data/content/global_locators/', 'data/content/missions/')):
            continue
        if '/global_locators/' in path:
            groups = [x for x in goals.eb0009_locators(files[path[:-4] + '.rx2']) if x[0] == 'G']
            assert goals.loc_xml(groups, False) == data, path
        else:
            cell = files[path.rsplit('/', 1)[0] + '/cSim_Global.xsf']
            groups = [x for x in goals.cell_contents(cell)[0] if x[0] == 'G']
            assert goals.loc_xml(groups, True) == data, path
        count += 1
    assert count >= 15, count
    # Character histograms: rebuilt byte for byte from each retail label table.
    for path, data in files.items():
        if '_HISTOGRAM_' in path:
            table = next(p for p in files if p.startswith(path.rsplit('/', 1)[0]) and '_HISTOGRAM_' not in p
                         and p.endswith('.BIN'))
            assert goals.histogram(goals.read_label_table(files[table]).values()) == data, path
            count += 1
    return count


def check_install():
    for path in (ARCHIVE, HEADER, STAGE / goals.MARKER, STAGE / 'manifest.json', STAGE / 'goals.txt'):
        assert path.is_file(), f'missing {path}'
    header = HEADER.read_bytes()
    assert len(header) == 332 and header[0x108:0x108 + 42] == goals.PACKAGE_DIR.encode()
    assert struct.unpack_from('>I', header, 0x140)[0] == dlc.TITLE_ID
    assert goals.PACKAGE_DIR != goals.dlc.PACKAGE_DIR, 'goals package must stay separate from the menu package'
    archive = ARCHIVE.read_bytes()
    files = goals.read_dlc_archive(archive)
    manifest = json.loads((STAGE / 'manifest.json').read_text(encoding='ascii'))
    assert manifest['archive_sha256'] == hashlib.sha256(archive).hexdigest()
    assert {f['path']: f['sha256'] for f in manifest['files']} == \
        {p: hashlib.sha256(d).hexdigest() for p, d in files.items()}, 'archive and manifest disagree'
    for path, data in files.items():
        assert (STAGE / 'loose' / path.lstrip('./')).read_bytes() == data, f'loose copy differs: {path}'
    return files, manifest


def vault(files, stem):
    return Vault(files[stem + '.vlt'], files[stem + '.bin'])


def check_records(files, manifest):
    vaultlist = files[goals.VAULTLIST].decode()
    listed = [line.split('>')[1].split('<')[0] for line in vaultlist.splitlines() if '<VaultFile>' in line]
    assert listed == [f'{goals.FRAMEWORK_VAULT}.vlt', f'challengebanks\\{goals.BANK_VAULT}.vlt'], listed
    bank = vault(files, f'data/db/challengebanks/{goals.BANK_VAULT}')
    fw = vault(files, f'data/db/{goals.FRAMEWORK_VAULT}')
    records = {(r.cls, r.collection): r for r in list(bank.records()) + list(fw.records())}
    strings = {}

    def entry(rec, key, inherit=True):
        while rec is not None:
            for e in rec.entries:
                if e.key == key:
                    return rec, e
            rec = records.get((rec.cls, rec.parent)) if inherit else None
        return None, None

    def text(v, e):
        return v.string(e.target)

    # Naming contract: every collection added by the package starts with nsv_ ('default' is re-exported).
    added = [g['name'] for g in manifest['goals']]
    names = [goals.ROOT, goals.TYPE_GROUP] + [n + s for n in added for s in ('', '_0', '_1')]
    assert all(n.startswith('nsv_') for n in names)
    allowed = {h64(n) for n in names + [goals.AUDIO_COLLECTION, goals.TELEPORT_GROUP] +
               [t['name'] for t in goals.TELEPORTS]} | {h64('default')}
    local_keys = set()
    for path in files:
        if path.startswith('data/db/challenge_local_data/') and path.endswith('.vlt'):
            local_keys |= {r.collection for r in vault(files, path[:-4]).records()}
    stray = [f'{c:016X}/{k:016X}' for c, k in records if k not in allowed] + \
        [f'{k:016X}' for k in local_keys if k not in allowed]
    assert not stray, f'collections outside the nsv_ naming contract: {stray}'
    # goals.txt names every challenge the package adds (src/ keeps them off other maps), never the
    # re-exported Skate 3 'default'.
    listed_keys = set()
    intro_lines = {}
    for line in (STAGE / 'goals.txt').read_text(encoding='ascii').splitlines()[2:]:
        kind, key, name = line.split()[:3]
        if kind == 'intro':
            # src/ starts Reda's intro line from these (scene id -> speaker, line).
            fields = dict(f.split('=') for f in line.split()[3:])
            intro_lines[name] = (key, int(fields['speaker']), int(fields['line']))
            continue
        assert int(key, 16) == h64(name) and name.startswith('nsv_'), line
        if kind == 'teleport':
            # src/ skips the leave-NSV reload for these destinations (in-city teleport).
            exits = [f[5:] for f in line.split()[3:] if f.startswith('exit=')]
            assert exits == [t['exit_locator'] for t in goals.TELEPORTS if t['name'] == name], line
        listed_keys.add(int(key, 16))
    challenge_keys = {k for c, k in records if c == h64('challenges')} - {h64('default')}
    assert listed_keys == challenge_keys, (sorted(map(hex, listed_keys ^ challenge_keys)))
    expected_intros = {n: (i['id'], goals.REDA, next(a['line'] for a in i['audio'] if a['speaker'] == goals.REDA))
                       for n, i in manifest['intros']['goals'].items()}
    assert intro_lines == expected_intros, 'goals.txt intro lines differ from the intro manifest'
    assert all(n.startswith(goals.PREFIX) for n in added) and len(set(added)) == len(added)
    assert all(len(n) <= goals.HULL_NAME_MAX for n in added), 'mission hull names over 30 characters'
    assert len(added) == 22, f'expected the 22 Skate 2 single-player Own the Spot goals, got {len(added)}'
    tables = {p.split('/')[3]: goals.read_label_table(d) for p, d in files.items()
              if '/fe/languages/' in p and '_HISTOGRAM_' not in p}
    assert len(tables) == 9
    for p, d in files.items():
        if '_HISTOGRAM_' in p:
            assert d == goals.histogram(tables[p.split('/')[3]].values()), p
    for g in manifest['goals']:
        n = g['name']
        ch = records[(goals.CH, h64(n))]
        gd = records[(goals.GD, h64(n))]
        # Inherited from the type group: the Skate 3 OTS state graph and OTS challenge type.
        _, sg = entry(ch, h64('StateGraph'))
        assert struct.unpack_from('>QQ', bank.bin, sg.target) == (goals.SG, h64('ownthespot_shared_mlui'))
        assert entry(gd, h64('ChallengeType'))[1].value == 9
        assert entry(gd, h64('Global'))[1].value == 3
        _, cid = entry(ch, goals.A_CONTENT_ID)
        assert struct.unpack_from('>Q', bank.bin, cid.target)[0] == goals.DLC_CONTENT_ID
        assert text(bank, entry(gd, h64('Title'))[1]) == g['title_label']
        assert text(bank, entry(gd, h64('Description'))[1]) == g['description_label']
        assert text(bank, entry(gd, h64('Location'))[1]) == g['location']['name']
        _, trig = entry(gd, h64('OTSTriggerBoundary'))
        assert f"{struct.unpack_from('>Q', bank.bin, trig.target + 8)[0]:016X}" == g['trigger_boundary']['id']
        _, world = entry(gd, h64('World'))
        assert struct.unpack_from('>Q', bank.bin, world.target)[0] == goals.WORLD_KEY
        _, hull = entry(gd, goals.A_HULLS)
        hulls = [bank.string(bank.pointers[('bin', hull.target + 8 + 4 * i)])
                 for i in range(struct.unpack_from('>H', bank.bin, hull.target)[0])]
        assert hulls == [n] + [p['name'] for p in g['props_hulls']], hulls
        for key, points in ((goals.A_OWN_GROUP, g['own_it_points']), (goals.A_KILL_GROUP, g['killed_it_points'])):
            _, ref = entry(gd, key)
            group = records[(goals.OG, struct.unpack_from('>Q', bank.bin, ref.target + 16)[0])]
            assert entry(group, h64('PointRequirement'))[1].value == points
            _, objectives = entry(group, h64('Objectives'))
            assert struct.unpack_from('>QQ', bank.bin, objectives.target + 8) == (goals.OBJ, h64(n + '_0'))
            assert (goals.OBJ, h64(n + '_0')) in records
        local = vault(files, f'data/db/challenge_local_data/{n}')
        (lr,) = list(local.records())
        assert lr.parent == h64(goals.TYPE_GROUP) and (goals.LD, lr.parent) in records
        by_key = {e.key: e for e in lr.entries}
        assert f"{struct.unpack_from('>Q', local.bin, by_key[h64('OTSScoringBoundary')].target + 8)[0]:016X}" == \
            g['scoring_boundary']['id']
        assert local.string(by_key[h64('StartLocation')].target) == g['start_location']
        for lang, table in tables.items():
            title, desc = table[dlc.label_id(g['title_label'])], table[dlc.label_id(g['description_label'])]
            if lang == 'english':
                assert title.decode('latin1') == g['title'] and desc.decode('latin1') == g['description']
            elif lang == 'labels':
                assert title == g['title_label'].encode()
            elif lang == 'japanese':
                assert title == desc == b''
            else:
                assert title and desc
        assert f"data/content/missions/{n}/cSim_Global.xsf" in files
        assert f'data/stream/{n}.xml' in files and f'data/boundary/{n}.xml' in files
        locators, volumes = goals.cell_contents(files[f'data/content/missions/{n}/cSim_Global.xsf'])
        assert {f'{v:016X}' for v in volumes} >= {g['trigger_boundary']['id'], g['scoring_boundary']['id']}
        names = {x[1] for x in locators}
        assert all(v['name'] in names for v in g['visual_indicators'])
        arena = files[f"{goals.LOCATOR_DIR}/{g['location']['arena']}"]
        assert any(k == 'G' and nm == g['location']['name'] for k, nm, _, _ in goals.eb0009_locators(arena))
    return len(manifest['goals'])


def resident_in_data_order(data):
    """[(name, bytes)] of a retail resident archive in data order (the order its writer used)."""
    import mmap
    from world_archive import entries
    with mmap.mmap(-1, len(data)) as m:
        m.write(data)
        listing = sorted(entries(m), key=lambda e: e.offset)
        return [(e.name if e.name.startswith('./') else './' + e.name, bytes(m[e.offset:e.offset + e.size]))
                for e in listing]


def check_intros(files, manifest):
    """Intro cutscenes: retail-identical resident writer, superset scene/ICE files, records, audio, text."""
    intros = goals.intros
    reproduced = 0
    nis = ROOT / 'game' / 'data' / 'audio' / 'english' / 'nis.big'
    retail = []
    if MALOOF.is_file():
        retail.append(goals.read_dlc_archive(MALOOF.read_bytes(), lambda n: n.endswith('_resident.big'))
                      ['data/audio/english/dlc_mmc_resident.big'])
    if nis.is_file():
        retail.append(goals.read_archive(nis, lambda n: n.endswith('nisresident.big'))['./nisresident.big'])
        retail.append(nis.read_bytes())          # the bank itself: same EB3 0x00100400 family
    for data in retail:
        assert intros.resident_archive(resident_in_data_order(data)) == data, 'resident writer drifted'
        reproduced += 1
    native_scene, native_ice = goals.newest_scene_files(ROOT / 'game' / 'data', ROOT)
    scene, ice = files[goals.SCENE_FILE], files[goals.ICE_FILE]
    added = len(manifest['intros']['goals'])
    assert added == 22
    intros.verify_superset(native_scene[2], native_ice[2], scene, ice, added)
    assert manifest['intros']['native_scene']['number'] == native_scene[0] < goals.SCENE_NUMBER
    by_name = {intros.scene_name(r): r for r in intros.read_scene_table(scene)[0]}
    ids = {intros.object_id(o) for _, objs in intros.read_ice(ice) for o in objs}
    resident = intros.read_resident(files[f'{goals.AUDIO_DIR}/{goals.RESIDENT}'])
    tables = {p.split('/')[3]: goals.read_label_table(d) for p, d in files.items()
              if '/fe/languages/' in p and '_HISTOGRAM_' not in p}
    for g in manifest['goals']:
        intro = manifest['intros']['goals'][g['name']]
        r = by_name[intro['scene']]
        assert intros.scene_type(r) == intros.SPOT_INTRO and intros.scene_id(r) in ids
        local = vault(files, f"data/db/challenge_local_data/{g['name']}")
        (lr,) = list(local.records())
        e = next(e for e in lr.entries if e.key == h64('IntroPresentationEvents'))
        assert local.string(local.pointers[('bin', e.target + 12)]) == intro['scene']
        assert struct.unpack_from('>I', local.bin, e.target + 8)[0] == 5
        speakers = sorted(x['speaker'] for x in intro['audio'])
        assert speakers == [goals.REDA, 99], (g['name'], speakers)
        for x in intro['audio']:
            folder, name = x['file'].rsplit('/', 1)
            assert folder == goals.AUDIO_DIR and x['file'] in files
            assert name.endswith('.sns') and name[:-4] == name[:-4].upper()
            assert len(resident['./' + name[:-4] + '.snr']) == intros.SNR_SIZE
            if x['speaker'] == goals.REDA:
                label = intros.subtitle_label(name)
                assert x['subtitle_label'] == label
                assert tables['english'][dlc.label_id(label)] and tables['japanese'][dlc.label_id(label)] == b''
    # Maloof's layout: loose upper-case streams plus one resident archive of their headers, no
    # archive inside the package (log 348: base-style banks open from the disc and crashed).
    audio_files = [p for p in files if p.startswith(goals.AUDIO_DIR + '/')]
    streams = [p for p in audio_files if p.endswith('.sns')]
    assert len(streams) == len(resident) == 44 and len(audio_files) == 45
    assert {'./' + p.rsplit('/', 1)[1][:-4] + '.snr' for p in streams} == set(resident)
    assert [p for p in files if p.endswith('.big')] == [f'{goals.AUDIO_DIR}/{goals.RESIDENT}'], \
        'no bank archive inside the package, only the resident headers'
    fw = vault(files, f'data/db/{goals.FRAMEWORK_VAULT}')
    audio = next(r for r in fw.records() if r.cls == h64('aud_bigfiles'))
    assert audio.collection == h64(goals.AUDIO_COLLECTION) and audio.parent == h64('default')
    values = {e.key: (fw.string(e.target) if e.target is not None else e.value) for e in audio.entries}
    assert values[goals.A_AUD_FOLDER] == goals.AUDIO_FOLDER and goals.A_AUD_FRENCH not in values
    assert values[goals.A_AUD_RESIDENT] == values[goals.A_AUD_RESIDENT_B] == values[goals.A_AUD_RESIDENT_C] == \
        goals.RESIDENT
    assert values[goals.A_AUD_SLOT] == goals.AUDIO_SLOT and values[goals.A_AUD_DLC], \
        'DLC-flagged like Maloof: the boot loader opens base banks from the disc'
    for a in manifest['intros']['animations']:
        assert a in files
    return reproduced


def check_teleports(files, manifest):
    """Teleport markers: records on Skate 3's base 'teleports' group, trigger hulls, moved locators."""
    import skate2_teleports as tp
    from inspect_sfil import assets, dictionary
    from skate2_collision import clustered_mesh
    # The literal RefPack writer round-trips odd sizes (refpack_literal checks itself).
    for size in (0, 1, 3, 4, 5, 111, 112, 113, 1000):
        tp.refpack_literal(bytes((i * 7) & 255 for i in range(size)))
    bank = vault(files, f'data/db/challengebanks/{goals.BANK_VAULT}')
    records = {(r.cls, r.collection): r for r in bank.records()}
    group = records[(goals.CH, h64(goals.TELEPORT_GROUP))]
    assert group.parent == h64(goals.ROOT)
    sg = next(e for e in group.entries if e.key == h64('StateGraph'))
    assert struct.unpack_from('>QQ', bank.bin, sg.target) == (goals.SG, h64('teleport'))
    # The group's global data starts its teleports without the career prerequisite
    # check (log 351: the manager filtered the lighthouse teleport out).
    group_gd = records[(goals.GD, h64(goals.TELEPORT_GROUP))]
    prereq = [e for e in group_gd.entries if e.key == goals.A_IGNORE_PREREQ]
    assert len(prereq) == 1 and prereq[0].target is None and prereq[0].value == goals.BOOL_TRUE, prereq
    arena = files[f'{goals.LOCATOR_DIR}/{goals.TELEPORT_ARENA}']
    groups = {n: m for k, n, _, m in goals.eb0009_locators(arena) if k == 'G'}
    markers = manifest['teleports']['markers']
    assert [m['name'] for m in markers] == [t['name'] for t in goals.TELEPORTS]
    for t, m in zip(goals.TELEPORTS, markers):
        n = t['name']
        ch = records[(goals.CH, h64(n))]
        gd = records[(goals.GD, h64(n))]
        assert ch.parent == gd.parent == h64(goals.TELEPORT_GROUP)
        by_key = {e.key: e for e in gd.entries}
        assert bank.string(by_key[h64('Location')].target) == t['start_locator']
        # The shape of Skate 3's own tele_dwtn_to_dwtnskpk (log 348: its hull loads with Downtown);
        # A_FREESKATE_ACTIVE inherits false (true = never registered, round 270).
        assert set(by_key) == {h64('Location'), goals.A_HULLS, h64('Title'), h64('World')}
        assert bank.string(bank.pointers[('bin', by_key[goals.A_HULLS].target + 8)]) == n
        assert struct.unpack_from('>Q', bank.bin, by_key[h64('World')].target)[0] == goals.WORLD_KEY
        records = [(rn, m) for k, rn, _, m in goals.eb0009_locators(arena) if k == 'R']
        for name, pos in ((t['start_locator'], t['start']), (t['exit_locator'], t['exit'])):
            assert max(abs(a - b) for a, b in zip(groups[name][12:15], pos)) < 1e-3, name
            # Child records moved along (Skate 3's own teleport start has ::spawn_01; log 349).
            kids = [m for rn, m in records if rn.startswith(name + '::')]
            assert kids and all(max(abs(a - b) for a, b in zip(m[12:15], pos)) < 1e-3 for m in kids), name
        xsf = files[f'data/content/missions/{n}/cSim_Global.xsf']
        lo, hi = goals.teleport_box(t)
        (_, _, (a, _)), = list(assets(xsf))
        for e in dictionary(a):
            if e['kind'] == 0x80006:
                tris, bounds, _ = clustered_mesh(a[e['pointer']:e['pointer'] + e['size']], True)
                assert abs(tris.reshape(-1, 3).min(0) - lo).max() < 0.01 and abs(tris.reshape(-1, 3).max(0) - hi).max() < 0.01
        assert goals.cell_contents(xsf)[1] == [int(m['volume_id'], 16)]
        local = vault(files, f'data/db/challenge_local_data/{n}')
        (lr,) = list(local.records())
        assert lr.parent == h64(goals.TELEPORT_GROUP)
        ext = next(e for e in lr.entries if e.key == goals.A_TELEPORT_EXIT)
        assert local.string(local.pointers[('bin', ext.target + 32)]) == t['exit_locator']
        assert struct.unpack_from('>Q', local.bin, ext.target + 16)[0] == goals.WORLD_KEY
    return len(markers)


def dmo_positions(xsf):
    """Translations of the movable objects (eb001d instance matrices) in a cSim cell."""
    from inspect_sfil import assets, dictionary
    out = []
    for _, _, (arena, _) in assets(xsf):
        for e in dictionary(arena):
            if e['kind'] != 0xEB001D:
                continue
            blk = arena[e['pointer']:e['pointer'] + e['size']]
            for o in range(64, len(blk) - 16, 4):
                x, y, z, w = struct.unpack_from('>4f', blk, o)
                r = struct.unpack_from('>12f', blk, o - 48)
                if w == 1.0 and r[3] == r[7] == r[11] == 0 and abs(r[5]) > 0.3 and \
                        max(abs(x), abs(z)) < 6000 and abs(y) < 500:
                    out.append((x, y, z))
    return out


def check_props(files, manifest):
    """Skate 2 online props hulls: packaged, uniquely named, clear of the world's own movable objects."""
    import math
    names = [p['name'] for g in manifest['goals'] for p in g['props_hulls']]
    sources = [p['skate2_hull'] for g in manifest['goals'] for p in g['props_hulls']]
    assert names and len(set(names)) == len(names) and all(len(n) <= goals.HULL_NAME_MAX for n in names)
    assert 'f_1a_11_ots_wf1_boatpool_props' in sources, 'the Poop Deck sun lounger (log 345)'
    assert not set(sources) & set(goals.PROPS_SKIP)
    world = ROOT / 'mods' / 'skate2' / 'world-support-gvr-v1'
    cells = [dmo_positions(p.read_bytes()) for p in sorted(world.glob('cSim_*.xsf'))]
    placed = [p for c in cells for p in c]
    nearest = math.inf
    for n in names:
        xsf = files[f'data/content/missions/{n}/cSim_Global.xsf']
        assert f'data/content/missions/{n}/cPres_Global.xsf' in files
        for p in dmo_positions(xsf):
            if placed:
                nearest = min(nearest, min(math.dist(p, q) for q in placed))
    if placed:
        assert nearest >= 2.0, f'a props object is {nearest:.2f} m from a world movable object'
    return len(names), (nearest if placed else None)


def check_uninstall(files, manifest):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        goals.install(root, sorted(files.items()), dict(manifest))
        assert (root / 'mods' / 'skate2' / goals.STAGE / goals.MARKER).is_file()
        goals.uninstall(root)
        leftovers = [p for p in root.rglob('*') if p.is_file()]
        assert not leftovers, leftovers


def check_rebuild(files):
    native = ROOT / 'game' / 'data'
    rebuilt, _, _ = goals.build_package(HERE.parent.parent / 'Skate 2' / 'data', native)
    assert dict(rebuilt) == files, 'rebuild differs from the installed package'


def main():
    check_writers()
    loc = check_retail_locator_tables()
    files, manifest = check_install()
    count = check_records(files, manifest)
    residents = check_intros(files, manifest)
    markers = check_teleports(files, manifest)
    props, clearance = check_props(files, manifest)
    check_uninstall(files, manifest)
    if '--rebuild' in sys.argv:
        check_rebuild(files)
    print(f'skate2 goals ok: {count} goals, {len(files)} files, {loc} retail .loc/histogram files reproduced, '
          f'{count} intro cutscenes, {residents} retail resident archives reproduced, {markers} teleport markers, '
          f'{props} props hulls' + (f' ({clearance:.1f} m from the nearest world object)' if clearance else
                                    ' (no installed map to check clearance)')
          + (', rebuild identical' if '--rebuild' in sys.argv else ''))


if __name__ == '__main__':
    main()
