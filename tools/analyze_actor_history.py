"""Read a build219 actor-history BIN/JSON pair without running the game.

Print the largest changes in actor positions relative to the camera. These
are candidates for review, not automatic classifications of visible glitches.
--csv preserves every recorded part/frame for raw/final/owner comparisons.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import struct


def records(path):
    schema = json.loads(path.with_suffix('.json').read_text())
    data = path.read_bytes()
    if data[:8] != b'S3ACT219' or schema['version'] != 219:
        raise ValueError('unsupported actor history format')
    count, frame_size, actor_size, _ = struct.unpack_from('<4I', data, 8)
    if count > 2048 or len(data) != 24 + frame_size * count:
        raise ValueError('truncated or unbounded history')
    f, a, p = (schema[name] for name in ('frame', 'actor', 'pose'))
    def read(fmt, at):
        return struct.unpack_from('<' + fmt, data, at)
    for i in range(count):
        at = 24 + i * frame_size
        t, = read('d', at + f['time'])
        generation, = read('Q', at + f['generation'])
        world, = read('Q', at + f['world'])
        parts, = read('I', at + f['count'])
        if parts > schema['max_actors']:
            raise ValueError('invalid part count')
        raw_cam = read('3f', at + f['raw_camera'])
        final_cam = read('3f', at + f['final_camera'])
        for j in range(parts):
            actor = at + f['actors'] + j * actor_size
            row = {'time': t, 'generation': generation, 'world': world}
            for name in ('mesh', 'ctx', 'owner', 'instance', 'family', 'history_count'):
                row[name], = read('I', actor + a[name])
            row['history_key'], = read('Q', actor + a['history_key'])
            row['period'], = read('d', actor + a['period'])
            row['play'], = read('d', actor + a['play'])
            for name, camera in (('raw', raw_cam), ('final', final_cam)):
                pose = actor + a[name]
                present, = read('I', pose + p['present'])
                flags, = read('I', pose + p['flags'])
                row[name + '_present'] = present
                row[name + '_source'] = flags >> 8
                row[name + '_hash'], = read('I', pose + p['crc'])
                if flags & 1:
                    position = tuple(read('f', pose + p['bones'] + k * 4)[0] for k in (3, 7, 11))
                else:
                    position = read('3f', pose + p['world'] + 48)
                for k, axis in enumerate('xyz'):
                    row[name + '_' + axis] = position[k]
                    row[name + '_camera_' + axis] = camera[k]
                row[name + '_paint'] = ','.join(f'{v:.6g}' for v in read('8f', pose + p['colors']))
            yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--csv', type=Path)
    parser.add_argument('--top', type=int, default=15)
    args = parser.parse_args()
    rows = list(records(args.capture))
    if not rows:
        print('No actor parts recorded')
        return
    previous = {}
    changes = []
    for row in rows:
        key = row['world'], row['owner'], row['mesh'], row['ctx']
        old = previous.get(key)
        if old and row['final_present'] and old['final_present']:
            dt = row['time'] - old['time']
            if 0 < dt <= .1:
                def delta(stage):
                    return math.sqrt(sum(((row[stage + '_' + axis] - row[stage + '_camera_' + axis]) -
                                          (old[stage + '_' + axis] - old[stage + '_camera_' + axis])) ** 2 for axis in 'xyz'))
                raw = delta('raw') if row['raw_present'] and old['raw_present'] else None
                final = delta('final')
                changes.append({'time': row['time'], 'generation': row['generation'],
                                'mesh': f"{row['mesh']:08X}", 'ctx': f"{row['ctx']:08X}",
                                'owner': f"{row['owner']:08X}", 'dt_ms': round(dt * 1000, 3),
                                'raw_camera_relative_m': round(raw, 4) if raw is not None else None, 'final_camera_relative_m': round(final, 4),
                                'sources': [old['final_source'], row['final_source']]})
        previous[key] = row
    if args.csv:
        with args.csv.open('w', newline='', encoding='utf8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    summary = {'parts': len(rows), 'seconds': rows[-1]['time'] - rows[0]['time'],
               'largest_camera_relative_changes': sorted(changes, key=lambda r: r['final_camera_relative_m'], reverse=True)[:args.top]}
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
