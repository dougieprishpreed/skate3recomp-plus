"""Read an actor-history BIN/JSON pair (build219, 233 or 234 format) without running the game.

Prints a JSON summary with times in seconds before the save (the Ctrl+Insert press):
long frames, camera smoothing events (build233), streaming and render pauses (build233),
camera steps that break from their neighbours, parts whose history reset or that were
drawn unsmoothed (build233), and the largest changes in part positions relative to the
camera. These are candidates for review, not automatic classifications of visible glitches.
--csv preserves every recorded part/frame for raw/final/owner comparisons.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import struct

FORMATS = {b'S3ACT219': 219, b'S3ACT233': 233, b'S3ACT234': 234}
CAMERA_EVENTS = {'smoothed': 1, 'few_samples': 2, 'stale': 4, 'ring_gap': 8, 'ring_jump': 16,
                 'ring_turn': 32, 'clock_snap': 64, 'disabled': 128, 'built_sample': 256,
                 'outlier': 512}
PART_EVENTS = {'reset': 1, 'raw_short': 2, 'raw_size': 4, 'caster_hold': 8, 'interpolated': 16, 'held': 32}


def names(bits, table):
    return [name for name, bit in table.items() if bits & bit]


def load(path):
    """(schema, frames): each frame a dict with its parts as a list of dicts."""
    schema = json.loads(path.with_suffix('.json').read_text())
    data = path.read_bytes()
    version = FORMATS.get(data[:8])
    if version is None or schema['version'] != version:
        raise ValueError('unsupported actor history format')
    count, frame_size, actor_size, _ = struct.unpack_from('<4I', data, 8)
    if count > 2048 or len(data) < 24 + frame_size * count:
        raise ValueError('truncated or unbounded history')
    f, a, p = (schema[name] for name in ('frame', 'actor', 'pose'))

    def read(fmt, at):
        return struct.unpack_from('<' + fmt, data, at)

    def optional(fmt, table, at, name, default):
        return read(fmt, at + table[name])[0] if name in table else default

    frames = []
    for i in range(count):
        at = 24 + i * frame_size
        frame = {'time': read('d', at + f['time'])[0], 'generation': read('Q', at + f['generation'])[0],
                 'world': read('Q', at + f['world'])[0], 'retimed': read('I', at + f['retimed'])[0],
                 'raw_camera': read('3f', at + f['raw_camera']), 'final_camera': read('3f', at + f['final_camera']),
                 'camera_events': optional('I', f, at, 'camera_events', 0),
                 'camera_err_ms': optional('f', f, at, 'camera_err_ms', 0.0),
                 'camera_age_ms': optional('f', f, at, 'camera_age_ms', -1.0),
                 'render_paused': optional('I', f, at, 'render_paused', 0),
                 'stream': tuple(optional('I', f, at, name, 0) for name in
                                 ('stream_loads', 'stream_unloads', 'stream_loads_done', 'stream_unloads_done')),
                 'parts': []}
        parts = read('I', at + f['count'])[0]
        if parts > schema['max_actors']:
            raise ValueError('invalid part count')
        for j in range(parts):
            actor = at + f['actors'] + j * actor_size
            row = {name: read('I', actor + a[name])[0] for name in ('mesh', 'ctx', 'owner', 'instance', 'family', 'history_count')}
            row['history_key'] = read('Q', actor + a['history_key'])[0]
            row['period'] = read('d', actor + a['period'])[0]
            row['play'] = read('d', actor + a['play'])[0]
            row['events'] = optional('I', a, actor, 'events', 0)
            row['age_ms'] = optional('f', a, actor, 'age_ms', -1.0)
            for name in ('raw', 'final'):
                pose = actor + a[name]
                flags = read('I', pose + p['flags'])[0]
                row[name + '_present'] = read('I', pose + p['present'])[0]
                row[name + '_source'] = (flags >> 8) & 0xff
                # build 245+: where the paint/lighting rows came from (0 none, 1 capture,
                # 2 per-instance cache, 3 entity bridge, 4 mesh cache, 5 per-ctx LW cache)
                row[name + '_rows_src'] = (flags >> 16) & 0xff
                row[name + '_flags'] = flags & 0xff
                row[name + '_hash'] = read('I', pose + p['crc'])[0]
                bone = flags & 1 and read('I', pose + p['bone_count'])[0] > 0
                row[name + '_position'] = (tuple(read('f', pose + p['bones'] + k * 4)[0] for k in (3, 7, 11)) if bone
                                           else read('3f', pose + p['world'] + 48))
                row[name + '_paint'] = ','.join(f'{v:.6g}' for v in read('8f', pose + p['colors']))
            frame['parts'].append(row)
        frames.append(frame)
    return version, frames


def load_stream(path):
    """(camera samples, guest timing trace) saved after the frames (build233 runs 3+), else empty.
    Trace entries are (ticks, thread, serial low 32 bits, site, kind, arg); build233 entries are clock
    reads with site 0 (kind 0 = clock read; 234: 1/2 wait begin/end, 3 signal, 4 timer set, 5 title)."""
    schema = json.loads(path.with_suffix('.json').read_text())
    data = path.read_bytes()
    cams, reads = [], []
    section = schema.get('camera_samples')
    if section:
        fields = section['fields']
        for i in range(section['count']):
            at = section['offset'] + i * section['size']
            cams.append({'t': struct.unpack_from('<d', data, at + fields['t'])[0],
                         'guest_ticks': struct.unpack_from('<Q', data, at + fields['guest_ticks'])[0],
                         'trace_serial': struct.unpack_from('<Q', data, at + fields['trace_serial'])[0],
                         'source': struct.unpack_from('<I', data, at + fields['source'])[0],
                         'view': struct.unpack_from('<16f', data, at + fields['view']),
                         'proj': struct.unpack_from('<16f', data, at + fields['proj'])})
    section = schema.get('tick_reads')
    if section:
        wide = section['size'] >= 24
        for i in range(section['count']):
            at = section['offset'] + i * section['size']
            reads.append(struct.unpack_from('<QIIIHH', data, at) if wide else struct.unpack_from('<QII', data, at) + (0, 0, 0))
    return cams, reads


def distance(u, v):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(u, v)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--csv', type=Path)
    parser.add_argument('--top', type=int, default=15)
    args = parser.parse_args()
    version, frames = load(args.capture)
    if not frames:
        print('No frames recorded')
        return
    end = frames[-1]['time']

    def when(frame):
        return {'s_before_save': round(end - frame['time'], 3), 'generation': frame['generation']}

    intervals = [(b, (b['time'] - a['time']) * 1000) for a, b in zip(frames, frames[1:])]
    median_ms = statistics.median(ms for _, ms in intervals) if intervals else 0
    long_frames = [{**when(frame), 'ms': round(ms, 2)} for frame, ms in intervals if ms > max(2 * median_ms, 25)]

    camera_events = [{**when(frame), 'events': names(frame['camera_events'], CAMERA_EVENTS),
                      'err_ms': round(frame['camera_err_ms'], 2), 'newest_sample_age_ms': round(frame['camera_age_ms'], 2)}
                     for frame in frames if frame['camera_events'] & ~CAMERA_EVENTS['smoothed']]
    pauses, streaming = [], []
    for a, b in zip(frames, frames[1:]):
        if a['render_paused'] != b['render_paused']:
            pauses.append({**when(b), 'render_paused': b['render_paused']})
        delta = [y - x for x, y in zip(a['stream'], b['stream'])]
        if any(delta):
            streaming.append({**when(b), 'loads': delta[0], 'unloads': delta[1], 'loads_done': delta[2], 'unloads_done': delta[3]})

    # Camera steps against the median step of the surrounding frames (same world only).
    steps = []
    for i in range(1, len(frames)):
        a, b = frames[i - 1], frames[i]
        if a['world'] != b['world']:
            continue
        steps.append((b, distance(a['final_camera'], b['final_camera']), distance(a['raw_camera'], b['raw_camera'])))
    camera_steps = []
    for i, (frame, final, raw) in enumerate(steps):
        nearby = [s[1] for s in steps[max(0, i - 6):i] + steps[i + 1:i + 7]]
        if not nearby:
            continue
        usual = statistics.median(nearby)
        camera_steps.append({**when(frame), 'final_step_m': round(final, 4), 'raw_step_m': round(raw, 4),
                             'usual_final_step_m': round(usual, 4), 'excess_m': round(final - usual, 4),
                             'events': names(frame['camera_events'], CAMERA_EVENTS)})
    camera_steps.sort(key=lambda r: r['excess_m'], reverse=True)

    # Parts: reasons they left smoothing (build233), and camera-relative position changes.
    part_events, changes, previous, rows = [], [], {}, []
    for frame in frames:
        unsmoothed = []
        for part in frame['parts']:
            key = frame['world'], part['owner'], part['mesh'], part['ctx']
            old = previous.get(key)
            if part['events'] & ~PART_EVENTS['interpolated'] or (
                    version >= 233 and old and old[1]['events'] & PART_EVENTS['interpolated']
                    and not part['events'] & PART_EVENTS['interpolated'] and part['final_present']):
                unsmoothed.append({'ctx': f"{part['ctx']:08X}", 'owner': f"{part['owner']:08X}", 'family': part['family'],
                                   'events': names(part['events'], PART_EVENTS), 'history_age_ms': round(part['age_ms'], 2),
                                   'history_count': part['history_count']})
            if old and part['final_present'] and old[1]['final_present']:
                old_frame, old_part = old
                dt = frame['time'] - old_frame['time']
                if 0 < dt <= .1:
                    def relative(stage, f, p):
                        return [x - c for x, c in zip(p[stage + '_position'], f[stage + '_camera'])]
                    final = distance(relative('final', frame, part), relative('final', old_frame, old_part))
                    raw = (distance(relative('raw', frame, part), relative('raw', old_frame, old_part))
                           if part['raw_present'] and old_part['raw_present'] else None)
                    changes.append({**when(frame), 'ctx': f"{part['ctx']:08X}", 'owner': f"{part['owner']:08X}",
                                    'family': part['family'], 'dt_ms': round(dt * 1000, 3),
                                    'raw_camera_relative_m': round(raw, 4) if raw is not None else None,
                                    'final_camera_relative_m': round(final, 4),
                                    'events': names(part['events'], PART_EVENTS),
                                    'sources': [old_part['final_source'], part['final_source']]})
            previous[key] = frame, part
            if args.csv:
                rows.append({'time': frame['time'], 'generation': frame['generation'], 'world': frame['world'],
                             **{k: v for k, v in part.items() if not k.endswith('_position')},
                             **{f'{s}_{axis}': part[s + '_position'][k] for s in ('raw', 'final') for k, axis in enumerate('xyz')},
                             **{f'{s}_camera_{axis}': frame[s + '_camera'][k] for s in ('raw', 'final') for k, axis in enumerate('xyz')}})
        if unsmoothed:
            part_events.append({**when(frame), 'parts': unsmoothed})
    if args.csv and rows:
        with args.csv.open('w', newline='', encoding='utf8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    summary = {'format': version, 'frames': len(frames), 'parts': sum(len(f['parts']) for f in frames),
               'seconds': round(end - frames[0]['time'], 3), 'median_frame_ms': round(median_ms, 3),
               'long_frames': long_frames, 'camera_events': camera_events, 'render_pause_changes': pauses,
               'streaming': streaming, 'largest_camera_steps': camera_steps[:args.top],
               'part_events': part_events[-args.top * 4:],
               'largest_camera_relative_changes': sorted(changes, key=lambda r: r['final_camera_relative_m'], reverse=True)[:args.top]}
    cams, reads = load_stream(args.capture)
    if cams:
        gaps = sorted((b['t'] - a['t']) * 1000 for a, b in zip(cams, cams[1:]))
        pick = lambda q: round(gaps[min(len(gaps) - 1, int(q * len(gaps)))], 3) if gaps else None
        threads = {}
        for entry in reads:
            if entry[4] == 0:  # clock reads
                threads[entry[1]] = threads.get(entry[1], 0) + 1
        summary['camera_stream'] = {
            'samples': len(cams),
            'by_source': {name: sum(1 for c in cams if c['source'] == code)
                          for name, code in (('set_view_matrix', 1), ('sampler', 2))},
            'interval_ms': {'median': pick(.5), 'p95': pick(.95), 'max': pick(1.0)},
            'under_2ms': sum(1 for g in gaps if g < 2),
            'clock_reads': sum(1 for e in reads if e[4] == 0),
            'timing_events': sum(1 for e in reads if e[4] != 0),
            'clock_reads_by_thread': dict(sorted(threads.items(), key=lambda kv: -kv[1])[:8])}
    if version < 233:
        summary['note'] = 'build219 format: raw camera is zero and parts were picked by distance to the origin'
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
