"""Package generated navigation using the game's existing stream ownership.

Only a three-object navigation arena's schema/header is reused from the user's
native data. All polygons, IDs and spatial trees are newly generated for BAM.
"""
import mmap
import struct
from pathlib import Path

from inspect_sfil import assets, dictionary
from world_archive import entries, decode_archive_payload
from build_skate2_world_support import records, make_catalog, guid
from vlt64 import h64
from navpower_resource import read_rw_object


def native_template(native_data: Path):
    archive = native_data/'content/worldDIST_DownTown.big'
    with archive.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        index = entries(raw)
        catalog = next(e for e in index if e.name.endswith('/DIST_DownTown_Sim.xst'))
        toc = records(decode_archive_payload(bytes(raw[catalog.offset:catalog.offset+catalog.size])), b'ATOC', 24, 64)
        by_id = {guid(row): row for row in toc}
        for entry in index:
            if not Path(entry.name).name.startswith('cSim_') or not entry.name.endswith('.xsf'):
                continue
            sf = decode_archive_payload(bytes(raw[entry.offset:entry.offset+entry.size]))
            for ident, _, (arena, resource) in assets(sf):
                objects = list(dictionary(arena))
                if [o['kind'] for o in objects] != [0xeb0008, 0xeb0027, 0xeb000b] or resource:
                    continue
                if [o['pointer'] for o in objects[:2]] != [544, 560]:
                    continue
                export = objects[2]
                data = arena[export['pointer']:export['pointer']+export['size']]
                # One public reference to dictionary index 1, the nav object.
                if (len(data) != 244 or struct.unpack_from('>5I', data) != (1,20,44,25,44)
                        or struct.unpack_from('>2I', data,36) != (0xeb0027,1)):
                    continue
                return arena[:560], data, bytes(by_id[ident])
    raise ValueError('native navigation arena schema missing or unsupported')


def make_asset(template, cell: str, nav: bytes):
    resources = read_rw_object(nav)
    if len(resources) != 1 or not resources[0] or not any(layer.polygons for layer in resources[0]):
        raise ValueError('generated navigation must contain one nonempty resource')
    header, exports, toc = template
    arena = bytearray(header)
    arena.extend(nav)
    arena.extend(bytes((-len(arena)) % 16))
    export_at = len(arena)
    export = bytearray(exports)
    struct.pack_into('>Q', export, 28, h64('nsv_navigation_export/'+cell))
    arena.extend(export)
    dictionary_at = len(arena)
    arena.extend(struct.pack('>18I', 544,0,16,16,16,0xeb0008,
                            560,0,len(nav),16,43,0xeb0027,
                            export_at,0,len(export),16,19,0xeb000b))
    # Main section size and buffer-descriptor sizes, from the native RW4 header.
    for offset in (0x44, 0x204, 0x208):
        struct.pack_into('>I', arena, offset, len(arena))
    struct.pack_into('>I', arena, 0x30, dictionary_at)
    ident = h64('nsv_navigation_asset/'+cell)
    asset = bytearray(208)
    length = 80+len(arena)
    stride = (128+length+127) & ~127
    struct.pack_into('>QIII', asset, 0, ident, length, 128, stride)
    struct.pack_into('>3I', asset, 128, len(arena),len(arena),0)
    asset.extend(arena)
    asset.extend(bytes(stride-len(asset)))
    row = bytearray(toc)
    struct.pack_into('>Q', row, 0, ident)
    struct.pack_into('>2I', row, 12, len(arena),len(arena))
    # Prove the emitted dictionary and SFIL block lengths with the independent reader.
    preamble = b'SFIL\0\0\0\3'+bytes(120)
    decoded = list(assets(preamble+asset))
    if len(decoded) != 1 or decoded[0][0] != ident or len(list(dictionary(decoded[0][2][0]))) != 3:
        raise ValueError('invalid generated navigation asset')
    return bytes(asset), row


def append_navigation(stream: Path, navigation: dict, template, stream_name='BAM'):
    """Mutate a scratch map, before publication; preserve every existing asset."""
    cmap_path, toc_path = stream/(stream_name+'_Sim.xsm'), stream/(stream_name+'_Sim.xst')
    cmap, toc = cmap_path.read_bytes(), toc_path.read_bytes()
    cells, rows = records(cmap,b'CMAP',32,104), records(toc,b'ATOC',24,64)
    known, consumed = {guid(row) for row in rows}, set()
    for cell in cells:
        name = cell[28:92].split(b'\0')[0].decode('ascii')
        nav = navigation.get(name)
        if nav is None:
            continue
        path = stream/(name+'.xsf')
        sf = path.read_bytes()
        decoded = list(assets(sf, include_resource=False))
        if struct.unpack_from('>3I', cell,16) != (128,len(sf)-128,len(decoded)):
            raise ValueError('unexpected source streaming cell range')
        asset, row = make_asset(template,name if stream_name == 'BAM' else stream_name+'/'+name,nav)
        if guid(row) in known:
            raise ValueError('generated navigation already exists or ID collides')
        known.add(guid(row))
        rows.append(row)
        result = bytearray(sf+asset)
        struct.pack_into('>I',result,24,len(decoded)+1)
        struct.pack_into('>3I',cell,16,128,len(result)-128,len(decoded)+1)
        path.write_bytes(result)
        consumed.add(name)
    if consumed != set(navigation):
        raise ValueError('navigation has no matching map streaming cell')
    cmap_path.write_bytes(make_catalog(cmap[:32],cells))
    toc_path.write_bytes(make_catalog(toc[:24],rows))
