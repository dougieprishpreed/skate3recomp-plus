"""Optimize host register aliasing inside NSV material activation only.

Keep the generated algorithm, read order, stack save/restore, jump-table read
and complete register result. Local variables remove PPCContext/guest-memory
aliasing within the arithmetic. Material variants declare the separate storage of host registers and guest
memory (global host-context aliases remain legal); every original instruction/call remains. No memoized hashes or
guest pointers survive.
"""
from pathlib import Path
import re
import sys


def patch(path):
    raw = path.read_bytes()
    text = raw.decode("utf8").replace("\r\n", "\n")
    start = text.index("DEFINE_REX_FUNC(sub_82B73AC0) {")
    end = text.index("\n}\n", start + 1) + 3
    function = text[start:end]
    if "NSV_LOCAL_HASH_BEGIN" in function:
        return
    anchor = "\t__savegprlr_21(ctx, base);\n"
    first = function.index(anchor) + len(anchor)
    last = function.index("\t// b 0x82f5214c\n", first)
    body = function[first:last]
    # Fail closed if code generation changes the region to call, write, trap
    # or use another architectural state. Only original byte loads are read.
    states = set(re.findall(r"ctx\.(\w+)", body))
    assert states <= {"ctr", "cr6", "xer"} | {f"r{i}" for i in range(32)}
    assert not re.search(r"REX_(?:STORE|CALL)|\bsub_|ctx\.xer\s*=", body)
    assert set(re.findall(r"REX_LOAD_(\w+)", body)) <= {"U8", "U32"}
    assert body.count("switch (ctx.r8.u32)") == 1
    assert body.count("__builtin_trap()") == 1
    used = sorted(states - {"xer"})
    declarations = "".join(f"\t\tauto nsv_{s} = ctx.{s};\n" for s in used)
    declarations += "\t\tconst auto nsv_xer = ctx.xer;\n"
    local = re.sub(r"ctx\.(\w+)", r"nsv_\1", body)
    local = re.sub(r"loc_([0-9A-F]+)", r"loc_\1_NSV", local)
    local = "\n".join("\t" + line if line else line for line in local.split("\n"))
    commits = "".join(f"\t\tctx.{s} = nsv_{s};\n" for s in used)
    block = ("\t// NSV_LOCAL_HASH_BEGIN: same algorithm with non-aliasing registers.\n"
             "\tif (skate3::world_loading::NativeSceneryRegistersEnabled()) {\n" +
             declarations + local + "\n" + commits +
             "\t\t__restgprlr_21(ctx, base);\n\t\treturn;\n\t}\n"
             "\t// NSV_LOCAL_HASH_END\n")
    function = function[:first] + block + function[first:]
    text = text[:start] + function + text[end:]
    if '#include "skate3_texture_lookup.h"' not in text:
        text = '#include "skate3_texture_lookup.h"\n' + text
    # Generated project sources use LF; never rewrite an already-patched file.
    path.write_bytes(text.encode("utf8"))
    print(f"Applied NSV local activation hash in {path.name}")


def specialize_materials(directory):
    # PPCContext lives on the host, outside the reserved guest address mapping.
    # State ONLY the separate allocations of context and guest memory, not
    # a restrict contract forbidding other host-context aliases (TLS helpers
    # can legitimately access the same context). All original calls remain.
    for address in ("827E5580", "827E51E8", "827E4F58", "82CA6150"):
        files = [p for p in directory.glob("skate3_recomp.*.cpp")
                 if f"DEFINE_REX_FUNC(sub_{address}) {{" in p.read_text(encoding="utf8")]
        assert len(files) == 1, address
        path = files[0]
        text = path.read_text(encoding="utf8")
        marker = f"// NSV_MATERIAL_REGISTERS_{address}"
        assumption = ("\n#if defined(__has_builtin)\n"
                      "#if __has_builtin(__builtin_assume_separate_storage)\n"
                      "\t__builtin_assume_separate_storage(&ctx, base);\n"
                      "#endif\n#endif\n")
        if marker in text:
            start = text.index(f"static void NsvActivation_{address}(")
            end = text.index("\n}\n", start + 1) + 3
            native = text[start:end]
            if "__builtin_assume_separate_storage" not in native:
                native = native.replace("PPCContext& __restrict ctx", "PPCContext& ctx")
                native = native.replace("{", "{" + assumption, 1)
                text = text[:start] + native + text[end:]
                path.write_bytes(text.encode("utf8"))
            # CMake's Windows file(WRITE) may have updated a probe argument
            # using CRLF. Generated material sources use LF; normalize only
            # when needed and retain idempotence on subsequent patch passes.
            encoded = text.encode("utf8")
            if path.read_bytes() != encoded:
                path.write_bytes(encoded)
            continue
        start = text.index(f"DEFINE_REX_FUNC(sub_{address}) {{")
        end = text.index("\n}\n", start + 1) + 3
        function = text[start:end]
        body = function[function.index("{") + 1:function.rfind("}")]
        assert body.count("REX_FUNC_PROLOGUE();") == 1
        native = (marker + "\nstatic void NsvActivation_" + address +
                  "(PPCContext& ctx, uint8_t* base) {" + assumption + body + "}\n\n")
        # Dispatch before the original phase scope and prologue: the selected
        # implementation executes exactly one original save/scope/restore.
        hook = (f"\n\tif (skate3::world_loading::NativeSceneryRegistersEnabled()) {{\n"
                f"\t\tNsvActivation_{address}(ctx, base);\n\t\treturn;\n\t}}")
        function = function.replace("{", "{" + hook, 1)
        text = text[:start] + native + function + text[end:]
        if '#include "skate3_texture_lookup.h"' not in text:
            text = '#include "skate3_texture_lookup.h"\n' + text
        path.write_bytes(text.encode("utf8"))
        print(f"Applied NSV non-aliasing material registers in {address}")


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    files = [p for p in directory.glob("skate3_recomp.*.cpp")
             if "DEFINE_REX_FUNC(sub_82B73AC0) {" in p.read_text(encoding="utf8")]
    assert len(files) == 1, "Expected exactly one generated 82B73AC0 definition"
    patch(files[0])
    specialize_materials(directory)
