#!/usr/bin/env python3
"""
ground.py - segment:offset <-> file offset arithmetic for the ScanMate 11000 ROM,
checked against known anchors so a mistyped address is caught rather than flashed.

Two hand-computed segment->file conversions ended a working session on this project.
Nothing in this toolchain types an address any more; it is all derived here.

    python ground.py [path/to/SCA04000.A6]      verify the anchors and print the table
    python ground.py rom.A6 C662:02A0           convert seg:off -> file offset
    python ground.py rom.A6 0x68C0 C662         convert file offset -> seg:off

As a module:
    import ground
    ground.load('SCA04000.A6')      # optional; enables the byte-signature checks
    ground.so2f(0xC662, 0x02A0)     # -> 0x068C0
    ground.f2so(0x068C0, 0xC662)    # -> 0x02A0
"""
import os
import sys

ROM_LINEAR_BASE = 0xC0000     # file offset 0 maps to CPU linear 0xC0000
ROM_SIZE = 0x38000            # the field-updatable main block

# Known sites, each confirmed by disassembling the stock image. The signature is the
# first few bytes at that file offset, so the arithmetic is checked against the ROM
# itself and not just against itself.
ANCHORS = [
    (0xC000, 0x0AE1, 0x00AE1, '558bec561eba', 'SCSI drain primitive'),
    (0xC662, 0x02A0, 0x068C0, 'c82800005756', 'DMA setup'),
    (0xCA26, 0x024F, 0x0A4AF, 'c80400005756', 'family A dispatch'),
    (0xD41B, 0x0000, 0x141B0, '240d00c083c4', 'segment D41B base'),
    (0xD807, 0x0000, 0x18070, '98ff5e5fc9cb', 'segment D807 base'),
]

ROM = None


def load(path):
    """Load a ROM image so the anchor signatures can be checked. Returns the bytes."""
    global ROM
    if not os.path.exists(path):
        raise SystemExit(f"ground.py: cannot find {path}\n"
                         "Pass the path to your own stock SCA04000.A6 (229,376 bytes).\n"
                         "No ROM image is distributed with these tools.")
    d = open(path, 'rb').read()
    if len(d) != ROM_SIZE:
        raise SystemExit(f"ground.py: {path} is {len(d):,} bytes, expected {ROM_SIZE:,}")
    ROM = d
    return d


def seg_base(seg):
    """File offset of <seg>:0000, or None if that segment starts outside the image."""
    off = seg * 16 - ROM_LINEAR_BASE
    return off if 0 <= off < ROM_SIZE else None


def f2so(fileoff, seg):
    """File offset -> offset within <seg>. None if not representable in that segment."""
    b = seg_base(seg)
    if b is None:
        return None
    o = fileoff - b
    return o if 0 <= o <= 0xFFFF else None


def so2f(seg, off):
    """<seg>:<off> -> file offset. None if the segment starts outside the image."""
    b = seg_base(seg)
    return None if b is None else b + off


def check(verbose=True):
    """Verify every anchor. Raises AssertionError on the first mismatch."""
    for seg, off, fo, sig, what in ANCHORS:
        got = so2f(seg, off)
        assert got == fo, f"{seg:04X}:{off:04X} -> 0x{got:05X}, expected 0x{fo:05X}"
        back = f2so(fo, seg)
        assert back == off, f"round trip for 0x{fo:05X} in {seg:04X} gave 0x{back:X}"
        status = 'arithmetic only'
        if ROM is not None:
            have = ROM[fo:fo + len(sig) // 2].hex()
            assert have == sig, (f"{what} at file 0x{fo:05X}: ROM has {have}, "
                                 f"expected {sig} - is this the right image?")
            status = f'bytes {sig} match'
        if verbose:
            print(f"  OK  {seg:04X}:{off:04X} = file 0x{fo:05X}  {what:22} {status}")
    if verbose:
        print(f"  {len(ANCHORS)} anchors verified"
              f"{' against the ROM' if ROM is not None else ' (arithmetic only - pass a ROM path to check bytes)'}")
    return True


def main(argv):
    args = [a for a in argv[1:]]
    rom = None
    if args and not (':' in args[0] or args[0].lower().startswith('0x')):
        rom = args.pop(0)
        load(rom)
    elif os.path.exists('SCA04000.A6'):
        load('SCA04000.A6')

    if not args:
        print(f"ROM linear base 0x{ROM_LINEAR_BASE:05X}, main block {ROM_SIZE:,} bytes"
              f"{'' if ROM is None else ' - image loaded'}")
        check()
        return 0

    if ':' in args[0]:
        seg, off = args[0].split(':')
        f = so2f(int(seg, 16), int(off, 16))
        print('outside the image' if f is None else f"file 0x{f:05X}")
    else:
        fo = int(args[0], 16)
        seg = int(args[1], 16)
        o = f2so(fo, seg)
        print('not representable in that segment' if o is None else f"{seg:04X}:{o:04X}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
