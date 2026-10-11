"""Build a separate BAM global overlay from selected original world-state assets.

Source archives and the original loose BAM files are never modified. A complete
NEW runtime directory holds copies of the spatial files and six updated files.
Directory-level mounting also works with parent-first VFS file opens.
"""
import argparse
import hashlib
import json
import mmap
import shutil
import struct
from pathlib import Path

from world_archive import entries
from inspect_sfil import assets
from build_bam_lighting_ids import exports

NULL_GUID = 0xE171E1ABFEDB69ED


def records(data, magic, header, stride):
    if data[:8] != magic + b'\0\0\0\3':
        raise ValueError(f'expected version-3 {magic!r}')
    count = struct.unpack_from('>I', data, 16)[0]
    if len(data) != header + count * stride:
        raise ValueError('catalog count/length mismatch')
    return [bytearray(data[p:p + stride]) for p in range(header, len(data), stride)]


def guid(row, offset=0):
    return struct.unpack_from('>Q', row, offset)[0]


def make_catalog(header, rows):
    result = bytearray(header)
    struct.pack_into('>I', result, 16, len(rows))
    return result + b''.join(rows)


def merge_group(bam, sources, group):
    cmap = (bam / f'BAM_{group}.xsm').read_bytes()
    atoc = (bam / f'BAM_{group}.xst').read_bytes()
    cells = records(cmap, b'CMAP', 32, 104)
    toc = records(atoc, b'ATOC', 24, 64)
    known = {guid(row) for row in toc}
    if len(known) != len(toc):
        raise ValueError('duplicate base asset IDs')
    filename = f'c{group}_Global'
    roots = [r for r in cells if r[28:92].split(b'\0')[0].decode() == filename]
    if len(roots) > 1:
        raise ValueError('multiple global cells')
    if roots:
        root = roots[0]
        combined = bytearray((bam / (filename + '.xsf')).read_bytes())
        original_payload = bytes(combined[128:])
    else:
        # BAM Sim has no global. The collection loader (82C98B08) discovers
        # its global from a spatial cell's non-null parent GUID. Add a root
        # and make it the parent of the existing otherwise-rootless cells.
        if any(guid(r, 8) != NULL_GUID for r in cells):
            raise ValueError('unexpected existing parent graph')
        source_cmap = sources[0][f'{sources[0]["name"]}_{group}.xsm']
        root = records(source_cmap, b'CMAP', 32, 104)[0]
        if root[28:92].split(b'\0')[0].decode() != filename:
            raise ValueError('source is not a global-only collection')
        if any(guid(r) == guid(root) for r in cells):
            raise ValueError('global cell ID collision')
        for row in cells:
            struct.pack_into('>Q', row, 8, guid(root))
        cells.append(root)
        combined = bytearray(sources[0][filename + '.xsf'][:128])
        original_payload = b''
    struct.pack_into('>I', combined, 12, struct.unpack_from('>I', cmap, 12)[0])
    # This preamble is outside the CMAP's requested payload range, but retain
    # the source format's little-endian collection count for offline readers.
    struct.pack_into('<I', combined, 28, len(cells))
    additions = []
    stored_blocks = {asset_id: bytes(combined[p:p + struct.unpack_from('>I', combined, p + 16)[0]])
                     for asset_id, p, _ in assets(combined, include_resource=False)}
    stored_toc = {guid(row): row for row in toc}
    for source in sources:
        source_name = source['name']
        source_cells = records(source[f'{source_name}_{group}.xsm'], b'CMAP', 32, 104)
        if len(source_cells) != 1 or guid(source_cells[0], 8) != NULL_GUID:
            raise ValueError('supplement must be a single independent global')
        source_toc = records(source[f'{source_name}_{group}.xst'], b'ATOC', 24, 64)
        sf = source[filename + '.xsf']
        decoded = list(assets(sf, include_resource=False))
        source_ids = {g for g, _, _ in decoded}
        if len(source_ids) != len(decoded) or source_ids != {guid(r) for r in source_toc}:
            raise ValueError('supplement SFIL/ATOC identity mismatch')
        if struct.unpack_from('>III', source_cells[0], 16) != (128, len(sf) - 128, len(decoded)):
            raise ValueError('supplement global payload range mismatch')
        if len(combined) % 128 or len(sf) % 128:
            raise ValueError('unaligned SFIL concatenation')
        source_rows = {guid(row): row for row in source_toc}
        added_bytes = added_assets = duplicates = 0
        for asset_id, p, _ in decoded:
            block = sf[p:p + struct.unpack_from('>I', sf, p + 16)[0]]
            if asset_id in known:
                # Only deduplicate exact copies already in this global. An
                # asset residing solely in a spatial cell is not equivalent
                # to a permanent global dependency and must not be skipped.
                # Three trailing bytes contain differing source padding even
                # for byte-identical assets. Guest 82C98B08 registers GUID,
                # type at +8 and ten words at +16..+55; it never consumes
                # these bytes. Still require every preceding byte to match.
                if (stored_blocks.get(asset_id) != block or
                        stored_toc[asset_id][:61] != source_rows[asset_id][:61]):
                    raise ValueError(f'conflicting/non-global shared asset {asset_id:016X}')
                duplicates += 1
                continue
            combined.extend(block)
            toc.append(source_rows[asset_id])
            stored_blocks[asset_id] = block
            stored_toc[asset_id] = source_rows[asset_id]
            known.add(asset_id)
            added_assets += 1
            added_bytes += len(block)
        additions.append(dict(collection=source_name, assets=added_assets,
                              bytes=added_bytes, identical_shared_assets=duplicates))
    combined_assets = list(assets(combined, include_resource=False))
    if combined[128:128 + len(original_payload)] != original_payload:
        raise ValueError('base global payload changed')
    struct.pack_into('>III', root, 16, 128, len(combined) - 128, len(combined_assets))
    if not {g for g, _, _ in combined_assets} <= known:
        raise ValueError('global asset missing from ATOC')
    if any(guid(row, 8) not in (NULL_GUID, guid(root)) for row in cells):
        raise ValueError('unexpected global dependency')
    return {f'BAM_{group}.xsm': make_catalog(cmap[:32], cells),
            f'BAM_{group}.xst': make_catalog(atoc[:24], toc),
            filename + '.xsf': combined}, additions


def build(bam, archive, base_lighting, output, exterior, waterfront='OPEN'):
    if output.exists():
        raise ValueError('output must be a new directory')
    names = [waterfront, exterior]
    with archive.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        index = entries(data)
        sources = []
        for name in names:
            prefix = f'data/content/missions/{name}/'
            source = {Path(e.name).name: bytes(data[e.offset:e.offset + e.size])
                      for e in index if e.name.startswith(prefix)}
            source['name'] = name
            # Selected collections embed textures in Pres, not a spatial Tex group.
            if records(source[f'{name}_Tex.xst'], b'ATOC', 24, 64):
                raise ValueError('supplement has external Tex assets; not supported by this builder')
            sources.append(source)
    result, details = {}, {}
    for group in ('Pres', 'Sim'):
        files, additions = merge_group(bam, sources, group)
        result.update(files)
        details[group] = additions
    lines = base_lighting.read_text().splitlines()
    if lines[0] != 'BAM_LIGHTING_TEXTURE_IDS_V1' or int(lines[1]) != len(lines) - 2:
        raise ValueError('invalid base lighting inventory')
    lighting = {int(x, 16) for x in lines[2:]}
    before = len(lighting)
    extra = {}
    for source in sources:
        name = source['name']
        for texture_id, texture_name, _ in exports(source['cPres_Global.xsf']):
            if texture_name.startswith((f'chromo_{name}_', f'layerpage_{name}_')):
                extra[f'{texture_id:016X}'] = texture_name
                lighting.add(texture_id)
    if not extra:
        raise ValueError('no supplemental lighting exports found')
    result['lighting_texture_ids.txt'] = ('BAM_LIGHTING_TEXTURE_IDS_V1\n' + str(len(lighting)) + '\n' +
                                         ''.join(f'{x:016X}\n' for x in sorted(lighting))).encode()
    manifest = dict(version=2, collections=names, groups=details,
                    additional_lighting_ids=len(lighting) - before, lighting_exports=extra,
                    files={n: dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest())
                           for n, b in result.items()})
    output.mkdir(parents=True)
    # Copy, rather than hard-link, so even accidental later writes cannot alter
    # the preserved source files. Publish the completion marker last.
    for source_file in bam.iterdir():
        if source_file.is_file() and source_file.name not in result:
            shutil.copyfile(source_file, output / source_file.name)
    for name, payload in result.items():
        (output / name).write_bytes(payload)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'BAM_COMPLETE_V2').write_text('BAM_COMPLETE_V2\n')
    print(json.dumps(dict(collections=names, groups=details,
                         additional_lighting_ids=len(lighting) - before)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bam', type=Path)
    parser.add_argument('missions', type=Path)
    parser.add_argument('lighting_ids', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--exterior', choices=['TrainingFacilityExteriorNew_10',
                                             'TrainingFacilityExteriorAbando'], required=True)
    parser.add_argument('--waterfront', choices=['OPEN', 'GVR'], default='OPEN')
    a = parser.parse_args()
    build(a.bam, a.missions, a.lighting_ids, a.output, a.exterior, a.waterfront)
