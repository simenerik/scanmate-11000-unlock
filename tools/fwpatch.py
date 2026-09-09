#!/usr/bin/env python3
"""
fwpatch.py - ScanMate 11000 firmware patch framework

Operates on the 224 KB main block (229,376 bytes) - the format CQscan's
firmware Update dialog accepts and sends to the scanner in seven 32 KB blocks
(CDB 0A 82 00 00 01 00).

VERIFIED FACTS THIS TOOL DEPENDS ON
-----------------------------------
* Main block is exactly 0x38000 bytes and is byte-identical to file offsets
  0x00000-0x37FFF of the reconstructed 256 KB EPROM image.
* Boot block validates the main block with TWO independent 8-bit sums:
      AL = sum of bytes at EVEN offsets, 0x00000..0x376FF
      AH = sum of bytes at ODD  offsets, 0x00000..0x376FF
      requires (AH<<8)|AL == 0x0C0C          (ROM code at file 0x3C58B-0x3C5C6)
* The preboot area has its own identical-form checksum:
      region 0x37700..0x37EFF, same 0x0C0C requirement
      (ROM code at file 0x3C3DA-0x3C3EC)
* Model-specific magic confirmed: 0x0C0C for the 11000, 0x0A0A for the
  3000/4000/5000 (scod5000.bin) - two independent samples, same structure.
* Verified all-zero caves inside the main checksum region:
      0x1D973-0x1D986 (20 B, in-segment, near-jump reachable)
      0x28810-0x2FFFF (30,704 B)
      0x30111-0x375FF (29,935 B)

DESIGN RULES
------------
* Patches are located by CONTEXT-ANCHORED SIGNATURE, never by bare offset.
  Each patch declares bytes before and after the region it replaces; the
  signature must match exactly once in the image or the tool aborts.
* Replacement must be the same length as the original (no shifting).
* Checksums are rebalanced automatically using two dedicated spare bytes,
  one at an even offset and one at an odd offset, inside the checksummed
  region and inside a verified all-zero cave.
* Nothing is written unless every verification passes.

Usage
-----
    python3 fwpatch.py selftest
    python3 fwpatch.py checksum SCA04000.A6
    python3 fwpatch.py build SCA04000.A6 -o patched.A6            # no-op + rebalance
    python3 fwpatch.py build SCA04000.A6 -o patched.A6 -p NAME    # apply named patches
    python3 fwpatch.py verify patched.A6
"""

import argparse
import hashlib
import sys

MAIN_SIZE      = 0x38000
CKSUM_MAIN     = (0x00000, 0x37700)    # [start, end)  -> 0x00000..0x376FF
CKSUM_PREBOOT  = (0x37700, 0x37F00)    # [start, end)  -> 0x37700..0x37EFF
MAGIC_11000    = 0x0C0C

# Dedicated rebalance bytes: tail of the verified all-zero cave 0x30111-0x375FF.
# One even offset, one odd, both inside the main checksum region.
REBAL_EVEN = 0x375FE
REBAL_ODD  = 0x375FF

# Preboot spares - the offsets ScanView's own build tool used (stock 0x07/0xC8).
PB_REBAL_EVEN = 0x37EFE
PB_REBAL_ODD  = 0x37EFF

PRISTINE_MD5 = '893c53ac5bb21968618af5478d7f320e'

CAVES = [(0x1D973, 0x1D987), (0x28810, 0x30000), (0x30111, 0x37600)]


# ------------------------------------------------------------------- checksum

def sums(data, region):
    """Return (even_sum, odd_sum) over [start,end) as the ROM computes them."""
    start, end = region
    seg = data[start:end]
    # region starts at an even file offset, so seg[0::2] are the even offsets
    assert start % 2 == 0, "checksum region must start on an even offset"
    return sum(seg[0::2]) & 0xFF, sum(seg[1::2]) & 0xFF


def ax_of(data, region):
    lo, hi = sums(data, region)
    return (hi << 8) | lo


def check(data):
    """Return dict of both checksums and whether each passes."""
    m = ax_of(data, CKSUM_MAIN)
    p = ax_of(data, CKSUM_PREBOOT)
    return {
        'main': m, 'main_ok': m == MAGIC_11000,
        'preboot': p, 'preboot_ok': p == MAGIC_11000,
        'ok': m == MAGIC_11000 and p == MAGIC_11000,
    }


def rebalance_region(buf, region, even_off, odd_off):
    """Set two spare bytes so `region` sums to MAGIC. Spares must lie inside it."""
    assert even_off % 2 == 0 and odd_off % 2 == 1
    assert region[0] <= even_off < region[1] and region[0] <= odd_off < region[1]
    buf[even_off] = 0
    buf[odd_off] = 0
    lo, hi = sums(buf, region)
    buf[even_off] = ((MAGIC_11000 & 0xFF) - lo) & 0xFF
    buf[odd_off] = (((MAGIC_11000 >> 8) & 0xFF) - hi) & 0xFF
    if ax_of(buf, region) != MAGIC_11000:
        raise RuntimeError('rebalance failed to converge - internal error')
    return buf[even_off], buf[odd_off]


def rebalance_preboot(buf):
    """Preboot spares are the factory's own: 0x37EFE / 0x37EFF (stock 0x07/0xC8)."""
    return rebalance_region(buf, CKSUM_PREBOOT, PB_REBAL_EVEN, PB_REBAL_ODD)


def rebalance(buf):
    """Set the two spare bytes so the MAIN checksum comes to 0x0C0C.

    The spare bytes are inside the summed region, so we zero them first,
    measure, then solve. Does not touch the preboot region."""
    assert REBAL_EVEN % 2 == 0 and REBAL_ODD % 2 == 1
    assert CKSUM_MAIN[0] <= REBAL_EVEN < CKSUM_MAIN[1]
    assert CKSUM_MAIN[0] <= REBAL_ODD < CKSUM_MAIN[1]

    buf[REBAL_EVEN] = 0
    buf[REBAL_ODD] = 0
    lo, hi = sums(buf, CKSUM_MAIN)

    want_lo = MAGIC_11000 & 0xFF
    want_hi = (MAGIC_11000 >> 8) & 0xFF
    buf[REBAL_EVEN] = (want_lo - lo) & 0xFF
    buf[REBAL_ODD] = (want_hi - hi) & 0xFF

    lo2, hi2 = sums(buf, CKSUM_MAIN)
    if (hi2 << 8) | lo2 != MAGIC_11000:
        raise RuntimeError('rebalance failed to converge - internal error')
    return buf[REBAL_EVEN], buf[REBAL_ODD]


# --------------------------------------------------------------------- patches

class Patch:
    """A context-anchored replacement.

    pre   : bytes immediately before the replaced region (anchor)
    old   : the exact bytes to be replaced
    new   : replacement, MUST be len(old)
    post  : bytes immediately after the replaced region (anchor)
    """

    def __init__(self, name, pre, old, new, post, note=''):
        if len(new) != len(old):
            raise ValueError(f'{name}: new must be same length as old '
                             f'({len(new)} != {len(old)})')
        self.name, self.pre, self.old, self.new, self.post, self.note = \
            name, pre, old, new, post, note

    def find(self, data):
        """Return the single offset of `old`, or raise."""
        needle = self.pre + self.old + self.post
        hits, start = [], 0
        while True:
            i = data.find(needle, start)
            if i < 0:
                break
            hits.append(i + len(self.pre))
            start = i + 1
        if len(hits) != 1:
            raise RuntimeError(
                f'{self.name}: signature matched {len(hits)} times, need exactly 1')
        return hits[0]

    def apply(self, buf):
        off = self.find(bytes(buf))
        buf[off:off + len(self.old)] = self.new
        return off


# Registry. Empty for now by design: the framework is proven on a no-op first.
PATCHES = {
    # Harmless, visible end-to-end validation. The preboot stub at 0x37714 is
    #     b8 ff 00   mov ax, 0x00FF
    #     cb         retf
    # Boot calls it and prints AX, which is why the console shows "00FF".
    # Changing the immediate changes that printed value and nothing else.
    'preboot-marker': Patch(
        name='preboot-marker',
        pre=b'\x0d\x0a\x24\x00\x00',      # tail of "Preboot code.\r\n$" + pad
        old=b'\xb8\xff\x00',                # mov ax, 0x00FF
        new=b'\xb8\x34\x12',                # mov ax, 0x1234
        post=b'\xcb\xcb',                    # retf ; retf
        note='console prints 1234 instead of 00FF'),
}


# ------------------------------------------------------------------- pipeline

def load(path):
    d = open(path, 'rb').read()
    if len(d) != MAIN_SIZE:
        sys.exit(f'ERROR: {path} is {len(d)} bytes, expected {MAIN_SIZE} '
                 f'(224 KB main block)')
    return bytearray(d)


def build(src, out, names, quiet=False):
    buf = load(src)
    orig = bytes(buf)
    md5_in = hashlib.md5(orig).hexdigest()

    pre = check(orig)
    if not quiet:
        print(f'input : {src}')
        print(f'  size {len(orig)}  md5 {md5_in}'
              f'{"  (pristine)" if md5_in == PRISTINE_MD5 else ""}')
        print(f'  checksum main 0x{pre["main"]:04X} '
              f'{"OK" if pre["main_ok"] else "FAIL"}   '
              f'preboot 0x{pre["preboot"]:04X} '
              f'{"OK" if pre["preboot_ok"] else "FAIL"}')
    if not pre['ok']:
        sys.exit('ERROR: input image does not pass its own checksums. Refusing.')

    applied = []
    for n in names:
        if n not in PATCHES:
            sys.exit(f'ERROR: unknown patch "{n}". Known: {sorted(PATCHES) or "(none)"}')
        off = PATCHES[n].apply(buf)
        applied.append((n, off))
        if not quiet:
            print(f'  applied {n} at 0x{off:05X}')

    pev, pod = rebalance_preboot(buf)
    ev, od = rebalance(buf)
    if not quiet:
        print(f'  rebalance main    : [0x{REBAL_EVEN:05X}]=0x{ev:02X}  '
              f'[0x{REBAL_ODD:05X}]=0x{od:02X}')
        print(f'  rebalance preboot : [0x{PB_REBAL_EVEN:05X}]=0x{pev:02X}  '
              f'[0x{PB_REBAL_ODD:05X}]=0x{pod:02X}')

    post = check(bytes(buf))
    if not post['ok']:
        sys.exit('ERROR: post-patch checksum failed. Nothing written.')

    # independent re-verification of every intended change
    diffs = [i for i in range(MAIN_SIZE) if orig[i] != buf[i]]
    expected = set()
    for n, off in applied:
        p = PATCHES[n]
        for k in range(len(p.old)):
            if p.old[k] != p.new[k]:
                expected.add(off + k)
    expected |= {REBAL_EVEN, REBAL_ODD, PB_REBAL_EVEN, PB_REBAL_ODD}
    unexpected = [d for d in diffs if d not in expected]
    if unexpected:
        sys.exit(f'ERROR: {len(unexpected)} unexpected byte change(s), '
                 f'first at 0x{unexpected[0]:05X}. Nothing written.')

    if out:
        open(out, 'wb').write(bytes(buf))
        if not quiet:
            print(f'output: {out}')
            print(f'  md5 {hashlib.md5(bytes(buf)).hexdigest()}')
            print(f'  bytes changed: {len(diffs)}')
            print(f'  checksum main 0x{post["main"]:04X} OK   '
                  f'preboot 0x{post["preboot"]:04X} OK')
    return bytes(buf)


# -------------------------------------------------------------------- selftest

def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(('  PASS  ' if cond else '  FAIL  ') + msg)
        ok = ok and cond

    src = 'SCA04000.A6'
    try:
        data = bytes(load(src))
    except SystemExit:
        print('  SKIP  SCA04000.A6 not present in cwd')
        return 1

    print('selftest: baseline')
    md5 = hashlib.md5(data).hexdigest()
    chk(md5 == PRISTINE_MD5, f'input md5 is pristine ({md5})')
    st = check(data)
    chk(st['main_ok'], f'stock main checksum = 0x{st["main"]:04X} (0x0C0C)')
    chk(st['preboot_ok'], f'stock preboot checksum = 0x{st["preboot"]:04X} (0x0C0C)')

    print('selftest: rebalance bytes')
    chk(data[REBAL_EVEN] == 0, f'0x{REBAL_EVEN:05X} is 0x00 in stock image')
    chk(data[REBAL_ODD] == 0, f'0x{REBAL_ODD:05X} is 0x00 in stock image')
    chk(any(s <= REBAL_EVEN < e for s, e in CAVES), 'even byte lies in a verified cave')
    chk(any(s <= REBAL_ODD < e for s, e in CAVES), 'odd byte lies in a verified cave')
    chk(CKSUM_MAIN[0] <= REBAL_EVEN < CKSUM_MAIN[1] and
        CKSUM_MAIN[0] <= REBAL_ODD < CKSUM_MAIN[1], 'both inside checksummed region')

    print('selftest: no-op round trip')
    out = build(src, None, [], quiet=True)
    chk(out == data, 'no-op build reproduces the input BYTE-IDENTICALLY')
    chk(hashlib.md5(out).hexdigest() == PRISTINE_MD5, 'no-op build md5 unchanged')

    print('selftest: rebalance actually corrects a perturbation')
    buf = bytearray(data)
    buf[0x1000] = (buf[0x1000] + 0x37) & 0xFF     # even offset
    buf[0x1001] = (buf[0x1001] + 0x59) & 0xFF     # odd offset
    chk(not check(bytes(buf))['main_ok'], 'perturbed image fails checksum as expected')
    rebalance(buf)
    st2 = check(bytes(buf))
    chk(st2['main_ok'], f'after rebalance main = 0x{st2["main"]:04X} (0x0C0C)')
    chk(st2['preboot_ok'], 'preboot checksum untouched by rebalance')
    chk(buf[REBAL_EVEN] == (0x100 - 0x37) & 0xFF,
        f'even spare = 0x{buf[REBAL_EVEN]:02X} (compensates +0x37)')
    chk(buf[REBAL_ODD] == (0x100 - 0x59) & 0xFF,
        f'odd spare = 0x{buf[REBAL_ODD]:02X} (compensates +0x59)')

    print('selftest: patch class safety')
    try:
        Patch('bad', b'\x00', b'\x01\x02', b'\x03', b'\x00')
        chk(False, 'length mismatch rejected')
    except ValueError:
        chk(True, 'length mismatch rejected')
    p = Patch('nomatch', b'\xDE\xAD\xBE\xEF', b'\x00', b'\x01', b'\xCA\xFE')
    try:
        p.find(data)
        chk(False, 'absent signature rejected')
    except RuntimeError:
        chk(True, 'absent signature rejected')
    p2 = Patch('multi', b'\x00', b'\x00', b'\x01', b'\x00')
    try:
        p2.find(data)
        chk(False, 'ambiguous signature rejected')
    except RuntimeError:
        chk(True, 'ambiguous signature rejected')

    print()
    print('SELFTEST', 'PASSED' if ok else 'FAILED')
    return 0 if ok else 1


# ---------------------------------------------------------------------- driver

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('selftest')
    c = sub.add_parser('checksum'); c.add_argument('file')
    v = sub.add_parser('verify');   v.add_argument('file')
    b = sub.add_parser('build')
    b.add_argument('src'); b.add_argument('-o', '--out', required=True)
    b.add_argument('-p', '--patch', action='append', default=[])
    a = ap.parse_args()

    if a.cmd == 'selftest':
        return selftest()
    if a.cmd in ('checksum', 'verify'):
        d = bytes(load(a.file))
        st = check(d)
        print(f'{a.file}')
        print(f'  size {len(d)}  md5 {hashlib.md5(d).hexdigest()}')
        print(f'  main    0x{st["main"]:04X}  {"OK" if st["main_ok"] else "FAIL"}  '
              f'(region 0x00000-0x376FF, need 0x0C0C)')
        print(f'  preboot 0x{st["preboot"]:04X}  {"OK" if st["preboot_ok"] else "FAIL"}  '
              f'(region 0x37700-0x37EFF, need 0x0C0C)')
        return 0 if st['ok'] else 1
    if a.cmd == 'build':
        build(a.src, a.out, a.patch)
        return 0


if __name__ == '__main__':
    sys.exit(main())
