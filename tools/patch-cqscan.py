#!/usr/bin/env python3
"""
patch-cqscan.py - apply the ScanMate 11000 host patches to your own CQscan.exe

Applies, in one pass:
  * LARGE_ADDRESS_AWARE                      0x00116
  * chunk budget / transfer guard / buffer   0x339C0, 0x60FE8, 0x61A11   (coupled)
  * .text marked writable                    PE section flag
  * BigTIFF writer                           1,110 bytes into .text slack, 4 hooks

Works on a stock CQscan.exe or one that already has the three constants applied.
Never distributes ColorQuartet itself - it patches the copy you already own.

    python patch-cqscan.py "C:\\Program Files (x86)\\Esko-Graphics\\ColorQuartet Pro 5.2\\CQscan.exe"

A .original backup is written alongside unless one already exists.
"""
import sys, os, struct, base64, hashlib, shutil

EXPECT_SIZE = 2211840
IMG = 0x400000
PAYLOAD = {
"SAVE": {
"va": 5204160,
"b64": "UFGLQXSjwG5PAItBeKPEbk8Ai0Fgo8huTwCLQVyjzG5PAFlYi0E8M9vD"
},
"WRITE": {
"va": 5204224,
"b64": "YIvsg+xAi34Ii4fAAAAAiUX4hcAPhI8CAACLh8gAAACJRfyFwA+EfgIAAKHAbk8AhcAPhHECAACLDchuTwCFyQ+EYwIAAA+vwYsNzG5PAIXJD4RSAgAAD6/BwegDhcAPhEQCAACJRfQzwDPSiUXwiVXsi038i134iwEBRfCDVewAg8EES3Xxi038iwEz0vd19IlF6ItF8ItV7Pd19IlF3ItF8ItV7IPAEIPSAIlF5IlV4IXSdQs9AAAAgA+C6AEAAKHAbk8AoyhtTwCLRdyjPG1PAItF6KO0bU8AochuTwCjSG1PAMcFTG1PAAAAAACj6G1PAMcF7G1PAAAAAABmo6BtTwDHBVBtTwAAAAAAxwVUbU8AAAAAAMcF8G1PAAAAAADHBfRtTwAAAAAAocxuTwBmo1BtTwC4AQAAAGaj8G1PAIM9yG5PAAF0KaHMbk8AZqNSbU8AZqNUbU8AuAEAAABmo/JtTwBmo/RtTwC4AgAAAOsFuAEAAABmo3htTwCLRfijhG1PAMcFiG1PAAAAAACjwG1PAMcFxG1PAAAAAACLReSLVeAFAAEAAIPSAKOMbU8AiRWQbU8Ai034weEDA8GD0gCjyG1PAIkVzG1PAGgAAQAAaABtTwD/t7QBAAD/l7wBAACDxAzHBdRuTwAQAAAAxwXYbk8AAAAAAItd+It1/GgIAAAAaNRuTwD/t7QBAAD/l7wBAACDxAyLBgEF1G5PAIMV2G5PAACDxgRLddKLXfiLdfyLBqPUbk8AxwXYbk8AAAAAAGgIAAAAaNRuTwD/t7QBAAD/l7wBAACDxAyDxgRLddCLReSjCG5PAItF4KMMbk8AagBqAP+3tAEAAP+XwAEAAIPEDGgQAAAAaABuTwD/t7QBAAD/l7wBAACDxAzHRwgAAAAAg8RAYYtOCFHo6nL3/4PEBMdGCAAAAAC4AQAAAF7D"
},
"TMPL": {
"va": 5205248,
"b64": "DAAAAAAAAAD+AAQAAQAAAAAAAAAAAAAAAAAAAAABBAABAAAAAAAAAAAAAAAAAAAAAQEEAAEAAAAAAAAAAAAAAAAAAAACAQMAAwAAAAAAAAAQABAAEAAAAAMBAwABAAAAAAAAAAEAAAAAAAAABgEDAAEAAAAAAAAAAgAAAAAAAAARARAAAAAAAAAAAAAAAAAAAAAAABUBAwABAAAAAAAAAAMAAAAAAAAAFgEEAAEAAAAAAAAAAAAAAAAAAAAXARAAAAAAAAAAAAAAAAAAAAAAABwBAwABAAAAAAAAAAEAAAAAAAAAUwEDAAMAAAAAAAAAAQABAAEAAAAAAAAAAAAAAA=="
},
"HDRB": {
"va": 5205504,
"b64": "SUkrAAgAAAAAAAAAAAAAAA=="
},
"PAD": {
"va": 5205568,
"b64": "UFFSVovyagJqAP+2tAEAAP+WwAEAAIPEDIP4EHMcuRAAAAAryFFooG5PAP+2tAEAAP+WvAEAAIPEDF5aWViB4///AADD"
},
"PADBUF": {
"va": 5205664,
"b64": "AAAAAAAAAAAAAAAAAAAAAA=="
}
}
CONSTANTS = [(0x339C0, 0x0000F100, 0x00038000, 'chunk budget'),
             (0x60FE8, 0x0000FFFF, 0x00038000, 'transfer guard'),
             (0x61A11, 0x0002004C, 0x0003804C, 'SPTD buffer')]
HOOKS = [(0x4938AE, '8b413c33db', None, 'SAVE',  'stash geometry'),
         (0x4938F9, '7e42',       'eb42', None,  'neutralise the 4 GiB check'),
         (0x493966, '81e3ffff0000', None, 'PAD', 'reserve 16 bytes for the header'),
         (0x49469D, None,         None, 'WRITE', 'BigTIFF writer')]
CLOSE_ORIG = ('8b4e0851' + 'e8' + struct.pack('<i', 0x46DEA0-(0x4946A1+5)).hex()
              + '83c404' + 'c74608' + '00000000' + 'b801000000' + '5e' + 'c3')

def main():
    if len(sys.argv) != 2:
        print(__doc__); return 2
    path = sys.argv[1]
    b = bytearray(open(path,'rb').read())
    print(f"input  {path}")
    print(f"       {len(b):,} bytes  md5 {hashlib.md5(bytes(b)).hexdigest()}")
    if len(b) != EXPECT_SIZE:
        print(f"ERROR: expected {EXPECT_SIZE:,} bytes. This is not CQscan.exe 5.2.2.1."); return 1
    fo = lambda va: va - IMG

    # --- guard: already patched? ---
    if b[fo(0x49469D)] == 0xE9:
        print("ERROR: the BigTIFF hook is already present. Restore the original first."); return 1

    # --- 1. LARGE_ADDRESS_AWARE ---
    ch = struct.unpack('<H', b[0x116:0x118])[0]
    if ch & 0x20: print("  skip  LARGE_ADDRESS_AWARE already set")
    else:
        b[0x116] |= 0x20; print("  ok    LARGE_ADDRESS_AWARE set")

    # --- 2. the three coupled constants ---
    for off, old, new, name in CONSTANTS:
        cur = struct.unpack('<I', b[off:off+4])[0]
        if cur == new: print(f"  skip  {name} already 0x{new:X}")
        elif cur == old or cur in (0x30000, 0x3004C):
            b[off:off+4] = struct.pack('<I', new); print(f"  ok    {name} 0x{cur:X} -> 0x{new:X}")
        else:
            print(f"ERROR: {name} at 0x{off:05X} is 0x{cur:X}, unrecognised. Aborting."); return 1

    # --- 3. .text writable ---
    pe = struct.unpack('<I', b[0x3c:0x40])[0]
    opt = struct.unpack('<H', b[pe+0x14:pe+0x16])[0]
    sec = pe + 0x18 + opt
    if bytes(b[sec:sec+8]).rstrip(b'\x00') != b'.text':
        print("ERROR: first section is not .text"); return 1
    co = sec + 36
    cur = struct.unpack('<I', b[co:co+4])[0]
    b[co:co+4] = struct.pack('<I', cur | 0x80000000)
    print(f"  ok    .text characteristics 0x{cur:08X} -> 0x{cur|0x80000000:08X} (writable)")

    # --- 4. cave payload ---
    for name, d in PAYLOAD.items():
        blob = base64.b64decode(d['b64']); o = fo(d['va'])
        if any(x != 0 for x in b[o:o+len(blob)]):
            print(f"ERROR: cave region {name} at 0x{d['va']:08X} is not empty."); return 1
        b[o:o+len(blob)] = blob
        print(f"  ok    {name:6} {len(blob):4} bytes -> 0x{d['va']:08X}")

    # --- 5. hooks ---
    for va, orig, lit, target, desc in HOOKS:
        o = fo(va)
        if va == 0x49469D:
            exp = bytes.fromhex(CLOSE_ORIG)
            if bytes(b[o:o+len(exp)]) != exp:
                print(f"ERROR: 0x{va:08X} does not match the expected original."); return 1
            b[o:o+26] = b'\xe9' + struct.pack('<i', PAYLOAD['WRITE']['va']-(va+5)) + b'\x90'*21
        elif lit:
            if bytes(b[o:o+len(bytes.fromhex(orig))]) != bytes.fromhex(orig):
                print(f"ERROR: 0x{va:08X} does not match."); return 1
            b[o:o+len(bytes.fromhex(lit))] = bytes.fromhex(lit)
        else:
            ob = bytes.fromhex(orig)
            if bytes(b[o:o+len(ob)]) != ob:
                print(f"ERROR: 0x{va:08X} does not match."); return 1
            new = b'\xe8' + struct.pack('<i', PAYLOAD[target]['va']-(va+5))
            b[o:o+len(ob)] = new + b'\x90'*(len(ob)-len(new))
        print(f"  ok    hook 0x{va:08X}  {desc}")

    bak = path + '.original'
    if not os.path.exists(bak):
        shutil.copy2(path, bak); print(f"\nbackup {bak}")
    else:
        print(f"\nbackup already exists, left alone: {bak}")
    open(path,'wb').write(bytes(b))
    print(f"output {path}")
    print(f"       md5 {hashlib.md5(bytes(b)).hexdigest()}")
    print("\nDone. Restore the .original file to undo.")
    return 0

if __name__ == '__main__':
    sys.exit(main())
