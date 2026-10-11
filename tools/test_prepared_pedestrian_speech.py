"""Verify prepared speech against both original archives, without running a game."""
import argparse
import hashlib
import json
import struct
from pathlib import Path
from attrib_vault import Vault
from build_skate2_livingworld import read_big
from skate2_speech import read_events, native_namespaces, PREFERRED_NAMESPACE
from vlt64 import h64
from world_archive import entries, eb_entries
from skate2_speech import SAMPLE_ID_BIAS, prepare_tables


def validate(folder, source, native_data):
    original_path = source/'audio/english/livingworldspeech.big'
    original = read_big(original_path, lambda n: n.endswith(('.evt','hdr.big','sth.big')))
    native = read_big(native_data/'audio/english/livingworldspeech.big', lambda n: n.endswith('.evt'))
    installed = folder/'loose/data/audio/nsv_s2_speech'
    evt = next(v for n,v in original.items() if n.endswith('.evt'))
    adapted = (installed/'livingworld_Events.evt').read_bytes()
    assert adapted[:8] == evt[:8] and adapted[10:] == evt[10:]
    occupied = native_namespaces(native_data)
    assert read_events(adapted).namespace >= PREFERRED_NAMESPACE
    assert read_events(adapted).namespace not in occupied
    known = {e.name:e.ident for e in read_events(next(iter(native.values()))).events}
    common = {e.ident for e in read_events(evt).events if known.get(e.name)==e.ident}
    rows = (folder/'speech_events.txt').read_text().splitlines()
    assert rows[:2] == ['NSV_SPEECH_EVENTS_V1','47']
    assert set(map(lambda x:int(x,16),rows[2:])) == common
    tables = [next(v for n,v in original.items() if n.endswith(suffix)) for suffix in ('hdr.big','sth.big')]
    expected_h, expected_s, ids = prepare_tables(*tables, native_data)
    adapted_tables = [(installed/('livingworld'+suffix)).read_bytes() for suffix in ('hdr.big','sth.big')]
    assert adapted_tables == [expected_h, expected_s]
    for number, (old, new) in enumerate(zip(tables, adapted_tables)):
        old_files = {e.name.removeprefix('./'):old[e.offset:e.offset+e.size] for e in entries(old)}
        for index, e in enumerate(eb_entries(new)):
            # Reproduce the exact offset shift and filename addressing used by
            # 82C5BD60, independently of the general archive entry reader.
            offset = struct.unpack_from('>I',new,48+index*16)[0] << new[10]
            assert offset == e.offset and offset+e.size <= len(new)
            name_at = struct.unpack_from('>I',new,12)[0] + new[20]*index + 2
            assert new[name_at:new.index(b'\0',name_at)].decode() == e.name.rsplit('/',1)[1]
            payload = new[offset:offset+e.size]
            source_payload = old_files[e.name.removeprefix('./')]
            if number == 0:
                assert int.from_bytes(payload[:2],'big') == int.from_bytes(source_payload[:2],'big')+SAMPLE_ID_BIAS
                assert payload[2:] == source_payload[2:]
            else:
                assert payload == source_payload
    rows = (folder/'speech_samples.txt').read_text().splitlines()
    assert rows[:2] == ['NSV_SPEECH_SAMPLES_V1',f'{len(evt)} {len(ids)}']
    assert {int(x,16) for x in rows[2:]} == ids
    original_samples = read_big(original_path, lambda n:n.endswith('.dat'))
    converted_samples = read_big(installed/'samples.big')
    assert {n.removeprefix('./'):v for n,v in converted_samples.items()} == original_samples
    assert (installed/'samples.big').read_bytes()[:4] == b'EB\0\3'
    # The game's EB3 lookup reads each entry's storage mode right after the
    # index; any nonzero byte makes that sample's reads return no data.
    for name in ('livingworldhdr.big','livingworldsth.big','samples.big'):
        data = (installed/name).read_bytes()
        count = struct.unpack_from('>I',data,4)[0]
        modes_at = (48+16*count+15) & ~15
        assert struct.unpack_from('>I',data,12)[0] >= modes_at+count, name
        assert not any(data[modes_at:modes_at+count]), name
    files = read_big(source/'db/db.big',lambda n:n in {'data/db/skater.vlt','data/db/skater.bin'})
    vault = Vault(files['data/db/skater.vlt'],files['data/db/skater.bin'])
    models = {r.collection:r for r in vault.records() if r.cls==h64('livingworld_models')}
    records = json.loads((folder/'vault_records.json').read_bytes())['records']
    prepared = {h64(r['collection']):r for r in records if r['class']=='livingworld_models'}
    rows = (folder/'pedestrian_voices.txt').read_text().splitlines()
    assert rows[:2] == ['NSV_PED_VOICES_V1','38']
    keys = set()
    for row in rows[2:]:
        key,voice=row.split(); key=int(key,16); voice=int(voice)
        assert key not in keys; keys.add(key)
        model=prepared[key]; name=model['source']
        source_key=int(name[3:],16) if name.startswith('id_') else h64(name)
        original_model=models[source_key]
        actual=struct.unpack_from('>I',vault.bin,original_model.layout+4)[0]
        assert voice==(104 if actual==100 else actual)
        assert struct.unpack_from('>I',bytes.fromhex(model['layout']['data']),4)[0]==104
        assert model['layout']['pointers'][0]['string'].startswith('nsv_s2_')
    assert len(keys)==38
    inventory={line.split()[0] for line in (folder/'runtime_files.txt').read_text().splitlines()[2:]}
    for row in json.loads((folder/'speech_audio.json').read_bytes()):
        data=(folder/'loose'/row['target']).read_bytes()
        assert row['target'] in inventory and len(data)==row['bytes']
        assert hashlib.sha256(data).hexdigest()==row['sha256']
    print('PASS: 38 source voice identities; safe silent Attrib defaults; 47 matching events; original programs/audio, compatible indexed tables and isolated sample IDs; complete startup inventory')

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--folder',type=Path,required=True)
    ap.add_argument('--source',type=Path,required=True)
    ap.add_argument('--native-data',type=Path,required=True)
    args=ap.parse_args()
    validate(args.folder,args.source,args.native_data)
