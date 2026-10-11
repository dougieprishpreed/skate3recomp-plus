"""Prepare the installed New San Van map from extracted original game data.

ISO extraction is an upstream installer step. This tool takes its Skate 2 data
directory, builds in disposable staging, and publishes one complete runtime
map. It does not require or retain an installed original/loose BAM copy.
"""
import argparse
import filecmp
import json
import tempfile
from pathlib import Path

from extract_world_loose import extract
from build_bam_lighting_ids import inventory
from build_skate2_world_support import build

FOLDER = 'world-support-gvr-v1'


def identical(left, right):
    """Compare contents, never file timestamps or sizes alone."""
    # iterdir raises on inaccessible roots; rglob can silently return no files.
    list(left.iterdir())
    list(right.iterdir())
    names = {p.relative_to(left) for p in left.rglob('*') if p.is_file()}
    other = {p.relative_to(right) for p in right.rglob('*') if p.is_file()}
    return names == other and all(filecmp.cmp(left/n, right/n, shallow=False) for n in names)


def prepare(source, root):
    source, root = source.resolve(), root.resolve()
    archive = source/'content/worldbam.big'
    missions = source/'content/missions.big'
    if not archive.is_file() or not missions.is_file():
        raise ValueError('provide the extracted Skate 2 data directory containing content/worldbam.big and missions.big')
    mods = root/'mods/skate2'
    destination = mods/FOLDER
    mods.mkdir(parents=True, exist_ok=True)
    # Staging is on the destination volume, allowing a directory rename on
    # publication. TemporaryDirectory removes intermediate extraction on both
    # success and failure. No original archive is modified.
    with tempfile.TemporaryDirectory(prefix='.prepare-world-', dir=mods) as temporary:
        work = Path(temporary).resolve()
        if work.parent != mods.resolve():
            raise ValueError('staging directory escaped the mod root')
        extracted = work/'extracted'
        extract(archive, extracted)
        bam = extracted/'data/content/world/stream/BAM'
        lighting, _ = inventory(bam)
        ids = work/'lighting_texture_ids.txt'
        ids.write_bytes(('BAM_LIGHTING_TEXTURE_IDS_V1\n' + str(len(lighting)) + '\n' +
                         ''.join(f'{k:016X}\n' for k in sorted(lighting))).encode('ascii'))
        prepared = work/'prepared'
        build(bam, missions, ids, prepared, 'TrainingFacilityExteriorNew_10', 'GVR')
        # A completed stage is required even when replacing an older install.
        if not (prepared/'BAM_COMPLETE_V2').is_file():
            raise ValueError('map preparation did not complete')
        if destination.exists():
            if identical(prepared, destination):
                return dict(path=str(destination), changed=False)
            # Preserve installed content before publication. Leave no hidden
            # fallback to an older map in the runtime search path.
            backup = mods/(FOLDER + '.previous')
            if backup.exists():
                raise ValueError(f'preserve or move the previous installation before updating: {backup}')
            destination.rename(backup)
            try:
                prepared.rename(destination)
            except Exception:
                backup.rename(destination)
                raise
        else:
            prepared.rename(destination)
    return dict(path=str(destination), changed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='extracted Skate 2 data directory')
    parser.add_argument('--root', type=Path, required=True, help='application/installation root')
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.root)))


if __name__ == '__main__':
    main()
