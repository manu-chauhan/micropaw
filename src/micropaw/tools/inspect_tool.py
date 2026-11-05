"""Inspect tool: static facts about a binary file. It reads bytes and never runs the file."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import struct
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from micropaw.tools.base import Tool, ToolContext
from micropaw.tools.path_security import resolve_path, validate_file_path

_PARSE_CAP = 16 * 1024 * 1024  # strings, entropy and headers come from the first 16 MB
_BLOCK = 64 * 1024
_MAGIC = [(b"\x7fELF", "ELF"), (b"%PDF", "PDF"), (b"PK\x03\x04", "ZIP (also APK, JAR, Office)"),
          (b"\x1f\x8b", "gzip"), (b"\x89PNG", "PNG"), (b"7z\xbc\xaf", "7-Zip"), (b"Rar!", "RAR"),
          (b"dex\n", "Android DEX"), (b"\x00asm", "WebAssembly"), (b"SQLite format 3\x00", "SQLite")]
_MACHO = {0xFEEDFACE: ">", 0xFEEDFACF: ">", 0xCEFAEDFE: "<", 0xCFFAEDFE: "<"}  # first 4 bytes read big-endian
_PE_MACHINE = {0x14C: "x86", 0x8664: "x86-64", 0x1C0: "ARM", 0xAA64: "ARM64"}
_PE_SUBSYSTEM = {1: "native", 2: "windows GUI", 3: "windows console", 9: "windows CE", 10: "EFI application"}
_ELF_MACHINE = {3: "x86", 8: "MIPS", 0x14: "PowerPC", 0x15: "PowerPC64", 0x28: "ARM", 0x3E: "x86-64",
                0xB7: "AArch64", 0xF3: "RISC-V"}
_ELF_TYPE = {1: "relocatable", 2: "executable", 3: "shared object or PIE", 4: "core"}
_MACHO_CPU = {7: "x86", 0x01000007: "x86-64", 12: "ARM", 0x0100000C: "ARM64"}
_MACHO_TYPE = {1: "object", 2: "executable", 6: "dylib", 8: "bundle", 10: "dSYM"}
_DYLIB_CMDS = {0xC, 0x80000018, 0x8000001F, 0x80000023}


def _entropy(counts: Counter | dict, total: int) -> float:
    return max(0.0, -sum(c / total * math.log2(c / total) for c in counts.values() if c)) if total else 0.0


def _cstr(data: bytes, off: int) -> str:
    return data[off:off + 256].split(b"\0")[0].decode("ascii", "replace") if 0 <= off < len(data) else ""


def _pe(d: bytes) -> list[str]:
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        return ["PE header: not found"]
    machine, nsec, stamp, _, _, optsize, _ = struct.unpack_from("<HHIIIHH", d, pe + 4)
    opt = pe + 24
    plus = struct.unpack_from("<H", d, opt)[0] == 0x20B
    sub = struct.unpack_from("<H", d, opt + 68)[0]
    out = [f"PE {'PE32+' if plus else 'PE32'}, machine {_PE_MACHINE.get(machine, hex(machine))}, "
           f"subsystem {_PE_SUBSYSTEM.get(sub, sub)}, built {datetime.fromtimestamp(stamp, UTC):%Y-%m-%d %H:%M} UTC "
           "(can be forged)", "Sections:"]
    secs = []
    for i in range(min(nsec, 96)):
        name, vsize, va, rsize, rptr = struct.unpack_from("<8sIIII", d, opt + optsize + i * 40)
        raw = d[rptr:rptr + rsize]
        secs.append((va, max(vsize, rsize), rptr))
        label = name.rstrip(b"\0").decode("ascii", "replace")
        out.append(f"  {label:8} size {rsize:>9} entropy {_entropy(Counter(raw), len(raw)):.2f}")

    def off(rva: int) -> int:
        return next((p + rva - v for v, n, p in secs if v <= rva < v + n), -1)

    imp = off(struct.unpack_from("<I", d, opt + (112 if plus else 96) + 8)[0])
    width, shown = (8, 0) if plus else (4, 0)
    out.append("Imports:")
    while imp >= 0 and imp + 20 <= len(d) and shown < 400:
        orig, _, _, name, first = struct.unpack_from("<IIIII", d, imp)
        if not name:
            break
        names, thunk = [], off(orig or first)
        while thunk >= 0 and thunk + width <= len(d) and len(names) < 60:
            val = int.from_bytes(d[thunk:thunk + width], "little")
            if not val:
                break
            names.append(f"#{val & 0xFFFF}" if val >> (width * 8 - 1) else _cstr(d, off(val & 0x7FFFFFFF) + 2))
            thunk += width
        out.append(f"  {_cstr(d, off(name))}: {', '.join(names)}")
        shown += len(names) + 1
        imp += 20
    return out


def _elf(d: bytes) -> list[str]:
    is64, e = d[4] == 2, "<" if d[5] == 1 else ">"
    typ, machine = struct.unpack_from(e + "HH", d, 16)
    if is64:
        shoff = struct.unpack_from(e + "Q", d, 40)[0]
        shentsize, shnum, shstr = struct.unpack_from(e + "HHH", d, 58)
    else:
        shoff = struct.unpack_from(e + "I", d, 32)[0]
        shentsize, shnum, shstr = struct.unpack_from(e + "HHH", d, 46)
    out = [f"ELF {'64' if is64 else '32'}-bit {'little' if e == '<' else 'big'} endian, "
           f"machine {_ELF_MACHINE.get(machine, hex(machine))}, type {_ELF_TYPE.get(typ, typ)}", "Sections:"]
    fmt = e + ("IIQQQQII" if is64 else "IIIIIIII")
    secs = [struct.unpack_from(fmt, d, shoff + i * shentsize) for i in range(min(shnum, 128))
            if shoff + (i + 1) * shentsize <= len(d)]
    names = secs[shstr][4] if shstr < len(secs) else 0
    needed: list[str] = []
    for name, kind, _, _, offset, size, link, _ in secs:
        out.append(f"  {_cstr(d, names + name):18} type {kind:<3} size {size}")
        if kind == 6 and link < len(secs):  # SHT_DYNAMIC: DT_NEEDED entries name shared libraries
            step = 16 if is64 else 8
            for p in range(offset, min(offset + size, len(d) - step), step):
                tag, val = struct.unpack_from(e + ("qQ" if is64 else "iI"), d, p)
                if tag == 1:
                    needed.append(_cstr(d, secs[link][4] + val))
    return out + ["Needed libraries: " + (", ".join(needed) or "none")]


def _macho(d: bytes) -> list[str]:
    out = []
    if struct.unpack(">I", d[:4])[0] == 0xCAFEBABE:  # fat: describe the slices, parse the first
        n = struct.unpack_from(">I", d, 4)[0]
        arches = [struct.unpack_from(">iiII", d, 8 + i * 20) for i in range(min(n, 8))]
        out.append("Universal binary: " + ", ".join(_MACHO_CPU.get(a[0], hex(a[0])) for a in arches))
        d = d[arches[0][2]:]
    e = _MACHO[struct.unpack(">I", d[:4])[0]]
    cpu, _, ftype, ncmds, _, _ = struct.unpack_from(e + "iiIIII", d, 4)
    out.append(f"Mach-O cpu {_MACHO_CPU.get(cpu, hex(cpu))}, type {_MACHO_TYPE.get(ftype, ftype)}, "
               f"{ncmds} load commands")
    pos, kinds, dylibs = (32 if d[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf") else 28), Counter(), []
    for _ in range(min(ncmds, 512)):
        if pos + 8 > len(d):
            break
        cmd, size = struct.unpack_from(e + "II", d, pos)
        kinds[hex(cmd)] += 1
        if cmd in _DYLIB_CMDS:
            dylibs.append(_cstr(d, pos + struct.unpack_from(e + "I", d, pos + 8)[0]))
        pos += max(size, 8)
    return out + ["Load commands: " + ", ".join(f"{k} x{v}" for k, v in kinds.items()),
                  "Linked dylibs: " + (", ".join(dylibs) or "none")]


def _kind(head: bytes) -> str:
    if head[:2] == b"MZ":
        return "Windows PE (MZ)"
    if len(head) >= 4 and (struct.unpack(">I", head[:4])[0] in _MACHO or head[:4] == b"\xca\xfe\xba\xbe"):
        return "Mach-O"
    return next((n for m, n in _MAGIC if head.startswith(m)), "unknown (no known magic)")


def inspect_file(path: str) -> str:
    hashes = {n: hashlib.new(n) for n in ("md5", "sha1", "sha256")}
    size, counts, blocks, head = 0, Counter(), [], b""
    with open(path, "rb") as f:
        while chunk := f.read(_BLOCK):
            for h in hashes.values():
                h.update(chunk)
            if size < _PARSE_CAP:
                head += chunk
                c = Counter(chunk)
                counts.update(c)
                blocks.append(_entropy(c, len(chunk)))
            size += len(chunk)
    kind = _kind(head)
    out = [f"Type: {kind}", f"Size: {size:,} bytes" + (f" (only the first {_PARSE_CAP // 2**20} MB parsed)"
                                                       if size > _PARSE_CAP else "")]
    out += [f"{n}: {h.hexdigest()}" for n, h in hashes.items()]
    if blocks:
        out.append(f"Entropy: {_entropy(counts, len(head)):.2f} overall; {len(blocks)} blocks of 64 KB: "
                   f"min {min(blocks):.2f}, max {max(blocks):.2f}, {sum(b > 7.2 for b in blocks)} above 7.2 "
                   "(packed or encrypted data)")
    parser = {"Windows PE (MZ)": _pe, "ELF": _elf, "Mach-O": _macho}.get(kind)
    if parser:
        try:
            out += parser(head)
        except (struct.error, KeyError, IndexError, ValueError) as e:
            out.append(f"Header parse stopped: {e} (file may be truncated or damaged)")
    ascii_s = [m.group().decode() for m, _ in zip(re.finditer(rb"[\x20-\x7e]{6,}", head), range(120), strict=False)]
    wide_s = [m.group().decode("utf-16le") for m, _ in
              zip(re.finditer(rb"(?:[\x20-\x7e]\x00){6,}", head), range(200 - len(ascii_s)), strict=False)]
    out.append(f"Strings ({len(ascii_s)} ASCII, {len(wide_s)} UTF-16LE, first found, up to 200):")
    return "\n".join(out + [s[:160] for s in ascii_s + wide_s])


class InspectTool(Tool):
    name = "inspect"
    marks = True  # strings and names come from the file, which may be hostile
    description = ("Static triage of a binary file: type, hashes, entropy, printable strings, and PE, ELF or "
                   "Mach-O headers (sections, imports, libraries). Reads bytes only and never runs the file.")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the file to inspect"}},
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        path = resolve_path(args["path"], context.workspace)
        if error := validate_file_path(str(path)):
            return f"Error: {error}"
        if not path.is_file():
            return f"Error: Not a file: {path}"
        try:
            return await asyncio.to_thread(inspect_file, str(path))
        except OSError as e:
            return f"Error reading file: {e}"
