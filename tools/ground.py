import capstone
ROM = open('/home/claude/fw/SCA04000.A6','rb').read()
FULL = open('/home/claude/fw/SCA04000_FULL_256K.bin','rb').read()
assert len(ROM)==0x38000 and FULL[:0x38000]==ROM
MD = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_16); MD.detail=True

ROM_LINEAR_BASE = 0xC0000          # file 0 maps to CPU linear 0xC0000

def seg_base(seg):
    """file offset of <seg>:0000, or None if outside the image"""
    lin = seg*16
    off = lin - ROM_LINEAR_BASE
    return off if 0 <= off < len(ROM) else None

def f2so(fileoff, seg):
    """file offset -> offset within segment; None if not representable"""
    b = seg_base(seg)
    if b is None: return None
    o = fileoff - b
    return o if 0 <= o <= 0xFFFF else None

def so2f(seg, off):
    b = seg_base(seg)
    return None if b is None else b + off

def dis(fileoff, seg, count=1):
    off = f2so(fileoff, seg)
    return list(MD.disasm(ROM[fileoff:fileoff+16*count], off, count=count))
