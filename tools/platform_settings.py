"""Derive desktop defaults without changing authored Windows or existing user settings."""
import argparse
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def settings_for(platform, source=None):
    raw = (source or ROOT / 'config/release-defaults.toml').read_bytes()
    if platform == 'Windows':
        return raw
    if platform not in ('Linux', 'Darwin'):
        raise ValueError('Unsupported desktop platform')
    before = tomllib.loads(raw.decode())
    lines = []
    for line in raw.decode().splitlines(keepends=True):
        key = line.split('=', 1)[0].strip()
        if key.startswith('d3d12_'):
            continue
        if key == 'input_backend':
            line = "input_backend = 'sdl'" + ('\r\n' if line.endswith('\r\n') else '\n')
        lines.append(line)
    output = ''.join(lines).encode()
    expected = {k: v for k, v in before.items() if not k.startswith('d3d12_')}
    expected['input_backend'] = 'sdl'
    if tomllib.loads(output.decode()) != expected:
        raise ValueError('Unexpected platform settings change')
    return output


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--platform', choices=('Windows', 'Linux', 'Darwin'), required=True)
    ap.add_argument('--source', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--only-if-missing', action='store_true')
    a = ap.parse_args()
    if a.only_if_missing and a.output.exists():
        return
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_bytes(settings_for(a.platform, a.source))


if __name__ == '__main__':
    main()
