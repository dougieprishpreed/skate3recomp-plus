"""Summarize a perf recorder stack profile (logs/perf_<n>_<time>_profile.csv).

Symbolizes addresses offline and prints, per sampled thread, the functions
with the most samples late in overlong guest frames (over 20 ms after the
last guest swap, i.e. the excess work), during the slowdown overall, and in
the seconds before it:
  incl = share of samples with the function anywhere on the stack (counted
         per frame, so recursion can exceed 100%)
  leaf = share of samples executing in the function itself
Samples include waits (a blocked thread samples inside its wait call), so
the shares are wall-clock time, not CPU time.

Symbols: skate3.exe and the DLLs beside it through llvm-symbolizer and
their PDBs; Windows DLLs through their export tables (nearest export).

Usage (repository root):
  python tools/analyze_perf_profile.py out/build/relwithdebinfo/logs/perf_1_..._profile.csv
      [--exe out/build/relwithdebinfo/skate3.exe] [--top 30] [--filter TEXT]
"""
import argparse
import bisect
import collections
import csv
import os
import struct
import subprocess
import sys

SYMBOLIZER = r"C:/Program Files/LLVM/bin/llvm-symbolizer.exe"
SYSTEM_DIR = r"C:/Windows/System32"


def pe_image_base(path):
    with open(path, "rb") as f:
        d = f.read(4096)
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    return struct.unpack_from("<Q", d, pe + 24 + 24)[0]


def pe_exports(path):
    """Sorted (rva, name) of a PE's named exports."""
    with open(path, "rb") as f:
        d = f.read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    ddir = opt + (112 if struct.unpack_from("<H", d, opt)[0] == 0x20B else 96)
    erva = struct.unpack_from("<I", d, ddir)[0]
    secs = [struct.unpack_from("<8sIIII", d, opt + optsz + 40 * i) for i in range(nsec)]

    def off(rva):
        for _, vsz, va, rsz, ptr in secs:
            if va <= rva < va + max(vsz, rsz):
                return rva - va + ptr
        return None

    if erva == 0 or off(erva) is None:
        return []
    o = off(erva)
    nfun, nnames, afun, anames, aord = struct.unpack_from("<IIIII", d, o + 20)
    funcs = [struct.unpack_from("<I", d, off(afun) + 4 * i)[0] for i in range(nfun)]
    out = []
    for i in range(nnames):
        no = off(struct.unpack_from("<I", d, off(anames) + 4 * i)[0])
        ordinal = struct.unpack_from("<H", d, off(aord) + 2 * i)[0]
        out.append((funcs[ordinal], d[no:d.index(b"\0", no)].decode(errors="replace")))
    out.sort()
    return out


def symbolize_pdb(path, rvas):
    base = pe_image_base(path)
    query = "".join("0x%x\n" % (base + rva) for rva in rvas)
    out = subprocess.run([SYMBOLIZER, "--obj=" + path, "--functions=linkage", "--no-inlines"],
                         input=query, capture_output=True, text=True).stdout
    blocks = [b for b in out.split("\n\n") if b.strip()]
    return {rva: block.strip().splitlines()[0] for rva, block in zip(rvas, blocks)}


def symbolize_exports(path, rvas):
    ex = pe_exports(path)
    keys = [a for a, _ in ex]
    names = {}
    for rva in rvas:
        i = bisect.bisect_right(keys, rva) - 1
        names[rva] = ex[i][1] if i >= 0 else "+0x%x" % rva
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--exe", default="out/build/relwithdebinfo/skate3.exe")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--filter", default=None, help="only functions containing this text")
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.profile)))
    exe_dir = os.path.dirname(os.path.abspath(args.exe))
    by_module = collections.defaultdict(set)
    for r in rows:
        by_module[r["module"]].add(int(r["rva"], 16))
    names = {}
    for module, rvas in by_module.items():
        rvas = sorted(rvas)
        local = os.path.join(exe_dir, module)
        system = os.path.join(SYSTEM_DIR, module)
        try:
            if os.path.isfile(local):
                resolved = symbolize_pdb(local, rvas)
            elif os.path.isfile(system):
                resolved = symbolize_exports(system, rvas)
            else:
                resolved = {}
        except (OSError, struct.error, ValueError):
            resolved = {}
        for rva in rvas:
            fn = resolved.get(rva) or "??"
            if fn.startswith("??"):
                fn = "+0x%x" % rva
            names[(module, rva)] = fn if module.lower() == "skate3.exe" else "%s!%s" % (module, fn)
    agg = collections.defaultdict(collections.Counter)
    totals = {}
    for r in rows:
        fn = names[(r["module"], int(r["rva"], 16))]
        agg[(r["thread"], r["window"], r["kind"])][fn] += int(r["count"])
        totals[(r["thread"], r["window"])] = int(r["window_samples"])
    for thread in sorted({k[0] for k in agg}):
        for kind in ("incl", "leaf"):
            late = agg[(thread, "late", kind)]
            during = agg[(thread, "during", kind)]
            before = agg[(thread, "before", kind)]
            tl =max(1, totals.get((thread, "late"), 1))
            td = max(1, totals.get((thread, "during"), 1))
            tb = max(1, totals.get((thread, "before"), 1))
            keep = (lambda fn: args.filter.lower() in fn.lower()) if args.filter else (lambda fn: True)
            ranked = [fn for fn, _ in late.most_common() if keep(fn)][:args.top]
            ranked += [fn for fn, _ in during.most_common() if keep(fn) and fn not in late][:args.top // 3]
            print("== %s %s (samples: late %d, during %d, before %d)" % (thread, kind, tl if late else 0,
                                                                         td, tb))
            print("    late  during  before")
            for fn in ranked:
                print("  %5.1f%%  %5.1f%%  %5.1f%%  %s" % (100.0 * late[fn] / tl, 100.0 * during[fn] / td,
                                                       100.0 * before[fn] / tb, fn[:150]))
            print()


if __name__ == "__main__":
    sys.exit(main())
