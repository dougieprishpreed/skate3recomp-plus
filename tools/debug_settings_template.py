"""Seed documented host controls; the runtime registry adds SDK controls on startup."""
import ast
from pathlib import Path
import re


def defaults():
    result = {}
    source = Path(__file__).resolve().parent.parent / 'src'
    pattern = re.compile(r'REXCVAR_DEFINE_(BOOL|INT32|UINT32|INT64|UINT64|DOUBLE|STRING)\(\s*(\w+),\s*([^,\n]+),')
    for path in sorted(source.rglob('*.cpp')):
        for kind, name, value in pattern.findall(path.read_text(encoding='utf-8')):
            if not (name.startswith('skate3_') or any(t in name for t in ('debug', 'perf', 'log', 'trace', 'dump', 'capture', 'probe'))):
                continue
            value = value.strip()
            try:
                if kind == 'STRING':
                    # Decode a literal only; expressions/constants stay with the runtime registry.
                    value = repr(ast.literal_eval(value))
                elif kind == 'BOOL':
                    if value not in ('true', 'false'):
                        continue
                else:
                    suffix = r'(?:f)$' if kind == 'DOUBLE' else r'(?:ull|llu|ul|ll|u)$'
                    value = re.sub(suffix, '', value, flags=re.I)
                    float(value) if kind == 'DOUBLE' else int(value, 0)
                result[name] = value
            except (ValueError, SyntaxError):
                continue
    return result


def release_text():
    values = defaults()
    for name in ('skate3_perf_recorder', 'skate3_perf_profiler', 'skate3_native_render_scene_perf_log'):
        values[name] = 'false'
    return ('# Host controls, including diagnostics. Performance capture is off.\n'
            '# The runtime adds available SDK diagnostic controls on first startup.\n'
            '# Set skate3_perf_recorder=true for capture; native_render_scene_perf_log\n'
            '# controls periodic renderer timing logs. Restart after editing.\n\n' +
            ''.join(f'{name} = {value}\n' for name, value in sorted(values.items())))
