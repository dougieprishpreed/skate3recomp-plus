"""Rebuild selected SPIR-V blobs in src/native/shaders/spirv/skate3_native_shaders_spirv.h.

Each blob's Vulkan (set, binding) layout is recovered from the EXISTING blob
(OpName + DescriptorSet/Binding decorations), mapped back to the HLSL register
declarations of its source file, passed to DXC as -fvk-bind-register, and the
rebuilt blob must reproduce exactly the same resource layout or the tool
aborts without writing. Usage (repository root):
  python tools/rebuild_native_spirv.py DXC_EXE file.hlsl[:entry] ...
      [--add-binding=NAME=SET:BINDING ...]
--add-binding admits a resource the old blob did not use (it must match the
(set, binding) the resource has in the pipeline's other blobs, e.g. the root
constants C = 0:0 of vs_main/ps_main); the rebuilt blob must then use it.
"""
import pathlib, re, struct, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = ROOT / 'src/native/shaders/spirv/skate3_native_shaders_spirv.h'
SHADERS = ROOT / 'src/native/shaders'


def words(blob):
    return struct.unpack('<%dI' % (len(blob) // 4), blob)


def layout(ws):
    names, sets, binds = {}, {}, {}
    i = 5
    while i < len(ws):
        op, n = ws[i] & 0xFFFF, ws[i] >> 16
        if op == 5:  # OpName
            raw = struct.pack('<%dI' % (n - 2), *ws[i + 2:i + n])
            names[ws[i + 1]] = raw.split(b'\0', 1)[0].decode()
        elif op == 71 and ws[i + 2] in (33, 34):  # Decorate Binding / DescriptorSet
            (binds if ws[i + 2] == 33 else sets)[ws[i + 1]] = ws[i + 3]
        i += n
    return {names.get(k, f'%{k}'): (sets.get(k), v) for k, v in binds.items()}


def registers(source):
    regs = {}
    for m in re.finditer(r'(\w+)\s*(?:\[[^\]]*\])?\s*:\s*register\(\s*([tbsu]\d+)', source):
        regs[m[1]] = m[2]
    for m in re.finditer(r'cbuffer\s+(\w+)\s*:\s*register\(\s*(b\d+)', source):
        regs['type.' + m[1]] = m[2]
        regs[m[1]] = m[2]
    return regs


def main():
    dxc = sys.argv[1]
    targets, extra = [], {}
    for arg in sys.argv[2:]:
        if arg.startswith('--add-binding='):
            res, _, loc = arg[len('--add-binding='):].partition('=')
            dset, _, binding = loc.partition(':')
            extra[res] = (int(dset), int(binding))
        else:
            targets.append(arg)
    text = HEADER.read_text()
    pattern = re.compile(r'\{"([\w.]+)", "(\w+)", "([^"]*)", (k_\w+),')
    entries = pattern.findall(text)
    todo = []
    for f, entry, defs, name in entries:
        for t in targets:
            tf, _, te = t.partition(':')
            if f == tf and (not te or te == entry):
                todo.append((f, entry, defs, name))
    if not todo:
        sys.exit('no matching entries')
    blobs = {}
    for f, entry, defs, name in todo:
        m = re.search(r'static const uint32_t ' + name + r'\[\] = \{(.*?)\};', text, re.S)
        old = [int(x, 16) for x in re.findall(r'0x[0-9a-fA-F]+', m[1])]
        want = layout(old)
        for res, loc in extra.items():
            if want.setdefault(res, loc) != loc:
                sys.exit(f'{name}: {res} is already bound at {want[res]}, not {loc}')
        src = (SHADERS / f).read_text()
        seen = {f}
        for inc in re.findall(r'#include\s+"([^"]+)"', src):
            if inc not in seen and (SHADERS / inc).exists():
                seen.add(inc)
                src += '\n' + (SHADERS / inc).read_text()
        regs = registers(src)
        args = [dxc, '-spirv', '-fspv-target-env=vulkan1.1', '-O3',
                '-T', ('vs_6_0' if entry.startswith('vs') else 'ps_6_0'), '-E', entry]
        for res, (dset, binding) in want.items():
            key = res[len('type.'):] if res.startswith('type.') else res
            reg = regs.get(res) or regs.get(key)
            if reg is None:
                sys.exit(f'{name}: no register for {res}')
            args += ['-fvk-bind-register', reg, '0', str(binding), str(dset)]
        # DXC wants an explicit binding for every declared register, used or
        # not. Unused declarations are stripped from the module; the layout
        # check below proves that, so their placeholder slots never ship.
        used = {regs.get(r) or regs.get(r[len('type.'):]) for r in want}
        spare = 0
        for reg in sorted(set(regs.values()) - used):
            args += ['-fvk-bind-register', reg, '0', str(100 + spare), '15']
            spare += 1
        for d in filter(None, defs.split(';')):
            args += ['-D', d]
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / 'o.spv'
            subprocess.run(args + ['-Fo', str(out), str(SHADERS / f)], check=True)
            data = out.read_bytes()
        got = layout(words(data))
        if got != want:
            sys.exit(f'{name}: layout mismatch\n want {want}\n got  {got}')
        blobs[name] = (f, entry, data)
        print(f'{name}: {len(data)} bytes, {len(want)} bindings verified')
    for name, (f, entry, data) in blobs.items():
        ws = words(data)
        body = '\n' + ''.join('    ' + ', '.join('0x%08x' % w for w in ws[i:i + 8]) + ',\n'
                              for i in range(0, len(ws), 8))
        text, n = re.subn(r'(static const uint32_t ' + name + r'\[\] = \{).*?(\};)',
                          lambda m: m[1] + body + m[2], text, flags=re.S)
        assert n == 1
        text = re.sub(r'(// ' + re.escape(f) + ' : ' + entry + r' )\(\d+ bytes\)',
                      lambda m: m[1] + f'({len(data)} bytes)', text, count=1)
    HEADER.write_text(text)


if __name__ == '__main__':
    main()
