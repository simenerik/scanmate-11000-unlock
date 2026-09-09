#!/usr/bin/env python3
"""
bigtiff.py - streaming BigTIFF writer for ScanMate 11000 scans

ColorQuartet writes classic TIFF, whose file offsets are 32-bit. That caps any
output at 4 GB no matter how the executable is patched, and in practice it dies
earlier. BigTIFF uses 64-bit offsets and has no practical size limit.

This tool never holds a whole image in memory. It copies strip by strip, so a
60 GB output costs the same RAM as a 60 MB one.

WHAT IT HANDLES
---------------
* finalised ColorQuartet TIFFs (IFD present, samples little-endian)
* crashed / headless ColorQuartet output (IFD offset 0, samples big-endian --
  CQscan streams big-endian and byte-swaps during finalisation, so a scan that
  died before finalising is left unswapped)
* raw sample dumps, given --width

Sample byte order is detected empirically rather than trusted from the header,
because CQscan's header says "II" even while the samples are big-endian.

COMMANDS
--------
    bigtiff.py info    IN [--width N]
    bigtiff.py convert IN -o OUT.btf [--width N] [--dpi D]
    bigtiff.py stitch  IN1 IN2 ... -o OUT.btf [--width N] [--dpi D]
    bigtiff.py verify  OUT.btf
    bigtiff.py selftest

THE FULL-FRAME WORKFLOW
----------------------
Scan the whole frame in ONE pass. ColorQuartet writes every pixel to disk --
its WriteFile calls pass lpOverlapped=NULL, so the kernel appends at a 64-bit
file position and size is not a constraint on the pixel stream. What breaks
past 2 GB is only the bookkeeping: SetFilePointer(h, 0, NULL, FILE_CURRENT)
returns a 32-bit position, so the strip offsets and IFD offset it records are
wrong, and classic TIFF could not express them past 4 GB anyway.

So let ColorQuartet capture, then replace the metadata:

    bigtiff.py convert Scan-NN.tif -o Scan-NN.btf --width 27084

Tested end to end on a 4.39 GB single-frame capture: 27,084 x 27,000, eleven
strip offsets past the 4 GB mark, rows byte-identical to source, read back by
tifffile with no warnings.

`stitch` remains available for joining separate captures, but it is not
required for large frames.
"""

import argparse
import os
import struct
import sys

SPP = 3                 # ScanMate always delivers RGB on the wire
BPS = 16
BYTES_PER_PX = SPP * BPS // 8
STRIP_TARGET = 8 << 20  # aim for ~8 MB strips
HDR = 16                # BigTIFF header size

# TIFF field types
SHORT, LONG, RATIONAL, LONG8 = 3, 4, 5, 16
TYPESIZE = {SHORT: 2, LONG: 4, RATIONAL: 8, LONG8: 8}


# ----------------------------------------------------------------- input side

class Source:
    """A readable image: classic TIFF, headless CQscan output, or raw samples."""

    def __init__(self, path, width=None):
        self.path = path
        self.size = os.path.getsize(path)
        self._f = open(path, 'rb')
        head = self._f.read(16)
        if len(head) < 8:
            raise ValueError(f'{path}: too small to be an image')

        self.byte_order = head[:2]
        en = '<' if self.byte_order == b'II' else '>'
        magic = struct.unpack(en + 'H', head[2:4])[0]
        ifd_off = struct.unpack(en + 'I', head[4:8])[0] if magic == 42 else 0

        # An IFD offset that points at or past EOF means CQscan recorded where the
        # directory would go and then never wrote it. Treat that as headless.
        ifd_usable = (magic == 42 and 0 < ifd_off < self.size - 2)
        if ifd_usable:
            self._f.seek(ifd_off)
            cnt = struct.unpack(en + 'H', self._f.read(2))[0]
            if cnt == 0 or cnt > 512 or ifd_off + 2 + cnt * 12 > self.size:
                ifd_usable = False
        if ifd_usable:
            self._from_ifd(en, ifd_off)
            self.kind = 'classic TIFF'
        else:
            if width is None:
                raise ValueError(
                    f'{path}: no usable IFD (header says {ifd_off:,}, file is {self.size:,} '
                    f'bytes). CQscan never wrote the directory - pass --width.')
            self.width = width
            self.data_start = HDR // 2          # CQscan stub header is 8 bytes
            avail = self.size - self.data_start
            self.height = avail // (width * BYTES_PER_PX)
            self.strips = [(self.data_start + i * width * BYTES_PER_PX,
                            width * BYTES_PER_PX) for i in range(self.height)]
            self.rows_per_strip = 1
            self.kind = 'headless (unfinalised)'
        self.sample_order = self._detect_order()

    def _from_ifd(self, en, off):
        f = self._f
        f.seek(off)
        n = struct.unpack(en + 'H', f.read(2))[0]
        tags = {}
        for _ in range(n):
            tag, typ, cnt = struct.unpack(en + 'HHI', f.read(8))
            raw = f.read(4)
            tags[tag] = (typ, cnt, raw, en)
        self._tags = tags

        def scalar(tag, default=None):
            if tag not in tags:
                return default
            typ, cnt, raw, e = tags[tag]
            if typ == SHORT:
                return struct.unpack(e + 'H', raw[:2])[0]
            return struct.unpack(e + 'I', raw)[0]

        def array(tag):
            typ, cnt, raw, e = tags[tag]
            sz = 2 if typ == SHORT else 4
            fmt = 'H' if typ == SHORT else 'I'
            if cnt * sz <= 4:
                return list(struct.unpack(e + fmt * cnt, raw[:cnt * sz]))
            ptr = struct.unpack(e + 'I', raw)[0]
            here = f.tell()
            f.seek(ptr)
            vals = list(struct.unpack(e + fmt * cnt, f.read(cnt * sz)))
            f.seek(here)
            return vals

        self.width = scalar(256)
        self.height = scalar(257)
        spp = scalar(277, 3)
        if spp != SPP:
            raise ValueError(f'{self.path}: {spp} samples/pixel, expected {SPP}')
        if scalar(259, 1) != 1:
            raise ValueError(f'{self.path}: compressed input is not supported')
        self.rows_per_strip = scalar(278, self.height)
        offs = array(273)
        cnts = array(279)
        if len(offs) != len(cnts):
            raise ValueError(f'{self.path}: {len(offs)} strip offsets vs {len(cnts)} counts')
        self.strips = list(zip(offs, cnts))
        self.data_start = min(offs)

    def _detect_order(self):
        """CQscan's header lies about sample order, so decide from the data.

        Real image samples change slowly between neighbours; the byte-swapped
        reading of the same bytes jumps wildly. Mean absolute difference between
        consecutive samples separates the two cleanly."""
        off, cnt = self.strips[len(self.strips) // 2]
        self._f.seek(off)
        buf = self._f.read(min(cnt, 1 << 20))
        buf = buf[:len(buf) // 2 * 2]
        if len(buf) < 64:
            return 'little'
        n = len(buf) // 2
        le = struct.unpack(f'<{n}H', buf)
        be = struct.unpack(f'>{n}H', buf)

        def mad(v):
            return sum(abs(v[i + 1] - v[i]) for i in range(0, len(v) - 1, 3)) / (len(v) // 3)

        dl, db = mad(le), mad(be)
        self._order_evidence = (dl, db)
        return 'little' if dl <= db else 'big'

    def expected_bytes(self):
        return self.width * self.height * BYTES_PER_PX

    def actual_bytes(self):
        return sum(c for _, c in self.strips)

    def rows(self, chunk_rows):
        """Yield (nrows, bytes) in row-aligned chunks, streaming."""
        rb = self.width * BYTES_PER_PX
        buf = bytearray()
        have = 0
        for off, cnt in self.strips:
            self._f.seek(off)
            remaining = cnt
            while remaining:
                take = min(remaining, 1 << 22)
                data = self._f.read(take)
                if not data:
                    break
                remaining -= len(data)
                buf.extend(data)
                while len(buf) >= rb * chunk_rows:
                    out = bytes(buf[:rb * chunk_rows])
                    del buf[:rb * chunk_rows]
                    have += chunk_rows
                    yield chunk_rows, out
        whole = len(buf) // rb
        if whole:
            yield whole, bytes(buf[:whole * rb])

    def close(self):
        self._f.close()


# ---------------------------------------------------------------- output side

class BigTiffWriter:
    """Writes a single-image BigTIFF: header, then strips, then the IFD."""

    def __init__(self, path, width, dpi=None, byte_order='<'):
        self.path = path
        self.width = width
        self.dpi = dpi
        self.en = byte_order
        self.rows_written = 0
        self.strip_offsets = []
        self.strip_counts = []
        self.rows_per_strip = max(1, STRIP_TARGET // (width * BYTES_PER_PX))
        self._f = open(path, 'wb')
        self._f.write(b'\x00' * HDR)            # header patched at close()
        self._buf = bytearray()                 # rows not yet flushed to a strip
        self._rb = width * BYTES_PER_PX

    def write_rows(self, nrows, data):
        expect = nrows * self._rb
        if len(data) != expect:
            raise ValueError(f'write_rows: got {len(data)} bytes, expected {expect}')
        self._buf.extend(data)
        self.rows_written += nrows
        self._flush(full_only=True)

    def _flush(self, full_only):
        """Emit strips. TIFF requires every strip but the last to hold exactly
        RowsPerStrip rows, so partial strips are only written at the very end."""
        span = self.rows_per_strip * self._rb
        while len(self._buf) >= span:
            self.strip_offsets.append(self._f.tell())
            self.strip_counts.append(span)
            self._f.write(bytes(self._buf[:span]))
            del self._buf[:span]
        if not full_only and self._buf:
            self.strip_offsets.append(self._f.tell())
            self.strip_counts.append(len(self._buf))
            self._f.write(bytes(self._buf))
            self._buf.clear()

    def close(self):
        self._flush(full_only=False)
        f = self._f
        en = self.en
        H = self.rows_written
        n = len(self.strip_offsets)
        if n == 0:
            raise ValueError('no image data was written')

        # ---- IFD entries, tags MUST be ascending ----
        # each entry is (tag, type, count, inline_bytes_or_None, payload_or_None)
        entries = []

        def add(tag, typ, count, payload):
            if len(payload) <= 8:
                entries.append((tag, typ, count, payload.ljust(8, b'\x00'), None))
            else:
                entries.append((tag, typ, count, None, payload))

        add(254, LONG, 1, struct.pack(en + 'I', 0))
        add(256, LONG, 1, struct.pack(en + 'I', self.width))
        add(257, LONG, 1, struct.pack(en + 'I', H))
        add(258, SHORT, 3, struct.pack(en + 'HHH', BPS, BPS, BPS))
        add(259, SHORT, 1, struct.pack(en + 'H', 1))          # no compression
        add(262, SHORT, 1, struct.pack(en + 'H', 2))          # RGB
        add(273, LONG8, n, struct.pack(en + f'{n}Q', *self.strip_offsets))
        add(277, SHORT, 1, struct.pack(en + 'H', SPP))
        add(278, LONG, 1, struct.pack(en + 'I', self.rows_per_strip))
        add(279, LONG8, n, struct.pack(en + f'{n}Q', *self.strip_counts))
        if self.dpi:
            add(282, RATIONAL, 1, struct.pack(en + 'II', int(self.dpi), 1))
            add(283, RATIONAL, 1, struct.pack(en + 'II', int(self.dpi), 1))
        add(284, SHORT, 1, struct.pack(en + 'H', 1))          # chunky
        if self.dpi:
            add(296, SHORT, 1, struct.pack(en + 'H', 2))      # inch
        add(339, SHORT, 3, struct.pack(en + 'HHH', 1, 1, 1))  # unsigned int

        entries.sort(key=lambda e: e[0])
        tags_seen = [e[0] for e in entries]
        if tags_seen != sorted(set(tags_seen)):
            raise RuntimeError('IFD tags must be unique and ascending')

        ifd_off = f.tell()
        blob_off = ifd_off + 8 + 20 * len(entries) + 8
        resolved, blobs, cursor = [], [], blob_off
        for tag, typ, count, inline, payload in entries:
            if inline is not None:
                resolved.append((tag, typ, count, inline))
            else:
                if len(payload) != count * TYPESIZE[typ]:
                    raise RuntimeError(f'tag {tag}: payload {len(payload)} != '
                                       f'{count} x {TYPESIZE[typ]}')
                resolved.append((tag, typ, count, struct.pack(en + 'Q', cursor)))
                blobs.append(payload)
                cursor += len(payload)

        f.write(struct.pack(en + 'Q', len(resolved)))
        for tag, typ, count, val in resolved:
            f.write(struct.pack(en + 'HHQ', tag, typ, count) + val)
        f.write(struct.pack(en + 'Q', 0))       # no next IFD
        for b in blobs:
            f.write(b)

        f.seek(0)
        f.write((b'II' if en == '<' else b'MM')
                + struct.pack(en + 'HHH', 43, 8, 0)
                + struct.pack(en + 'Q', ifd_off))
        f.close()
        return H, n


# ------------------------------------------------------------------- reading

def read_bigtiff(path):
    f = open(path, 'rb')
    head = f.read(16)
    en = '<' if head[:2] == b'II' else '>'
    magic, offsize, pad = struct.unpack(en + 'HHH', head[2:8])
    if magic != 43:
        raise ValueError(f'{path}: magic {magic}, not BigTIFF (43)')
    if offsize != 8:
        raise ValueError(f'{path}: offset size {offsize}, expected 8')
    ifd = struct.unpack(en + 'Q', head[8:16])[0]
    f.seek(ifd)
    n = struct.unpack(en + 'Q', f.read(8))[0]
    tags = {}
    for _ in range(n):
        tag, typ, cnt = struct.unpack(en + 'HHQ', f.read(12))
        val = f.read(8)
        tags[tag] = (typ, cnt, val)

    def get(tag):
        typ, cnt, val = tags[tag]
        sz = TYPESIZE[typ]
        fmt = {SHORT: 'H', LONG: 'I', LONG8: 'Q', RATIONAL: 'II'}[typ]
        total = cnt * sz
        if total <= 8:
            return list(struct.unpack(en + fmt * cnt, val[:total]))
        ptr = struct.unpack(en + 'Q', val)[0]
        here = f.tell()
        f.seek(ptr)
        out = list(struct.unpack(en + fmt * cnt, f.read(total)))
        f.seek(here)
        return out

    info = dict(path=path, byte_order=head[:2].decode(), ifd_offset=ifd,
                entries=n, width=get(256)[0], height=get(257)[0],
                bits=get(258), spp=get(277)[0],
                rows_per_strip=get(278)[0],
                strip_offsets=get(273), strip_counts=get(279),
                size=os.path.getsize(path))
    f.close()
    return info



# ------------------------------------------------------------------- cropping

def _png(path, w, h, rgb8):
    """Minimal PNG writer - stdlib only."""
    import zlib
    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)                                  # filter: none
        raw.extend(rgb8[y * stride:(y + 1) * stride])
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 6)))
        f.write(chunk(b'IEND', b''))


def cmd_crop(a):
    """Pull a 1:1 region out of a huge scan so focus can actually be judged."""
    s = Source(a.inputs[0], a.width)
    cw, ch = (int(v) for v in a.size.lower().split('x'))
    if a.at:
        cx, cy = (int(v) for v in a.at.split(','))
    else:
        cx, cy = (s.width - cw) // 2, (s.height - ch) // 2
    cx = max(0, min(cx, s.width - cw))
    cy = max(0, min(cy, s.height - ch))
    print(f'{s.path}: {s.width:,} x {s.height:,} ({s.sample_order}-endian)')
    print(f'  crop {cw} x {ch} at ({cx:,}, {cy:,})  -- 1:1, no resampling')

    rb = s.width * BYTES_PER_PX
    swap = (s.sample_order == 'big')
    out = bytearray()
    f = open(s.path, 'rb')
    for y in range(cy, cy + ch):
        # find the byte offset of this row within the source strips
        rows = 0
        for off, cnt in s.strips:
            n = cnt // rb
            if n and rows + n > y:
                f.seek(off + (y - rows) * rb + cx * BYTES_PER_PX)
                row = f.read(cw * BYTES_PER_PX)
                break
            rows += n
        else:
            raise SystemExit(f'ERROR: row {y} is not present in the file')
        fmt = ('>' if swap else '<') + f'{cw*SPP}H'
        vals = struct.unpack(fmt, row)
        out.extend(bytes(v >> 8 for v in vals))        # 16-bit -> 8-bit for viewing
    f.close()
    s.close()
    _png(a.out, cw, ch, out)
    print(f'  wrote {a.out}  ({os.path.getsize(a.out):,} bytes)')

# ------------------------------------------------------------------ commands

def cmd_info(a):
    for p in a.inputs:
        try:
            i = read_bigtiff(p)
            print(f'{p}: BigTIFF {i["width"]:,} x {i["height"]:,}, '
                  f'{len(i["strip_offsets"])} strips, {i["size"]:,} bytes')
            continue
        except Exception:
            pass
        s = Source(p, a.width)
        print(f'{p}')
        print(f'  kind          {s.kind}')
        print(f'  header        {s.byte_order.decode()}   samples {s.sample_order}-endian')
        print(f'  dimensions    {s.width:,} x {s.height:,}  ({SPP}x{BPS}-bit)')
        print(f'  strips        {len(s.strips)}, rows/strip {s.rows_per_strip}')
        print(f'  pixel bytes   {s.actual_bytes():,} of {s.expected_bytes():,} expected'
              f'  {"COMPLETE" if s.actual_bytes() >= s.expected_bytes() else "SHORT"}')
        print(f'  file size     {s.size:,}')
        if s.kind.startswith('headless'):
            tail = s.size - s.data_start - s.height * s.width * BYTES_PER_PX
            if tail:
                print(f'  trailing      {tail:,} bytes after the last whole row '
                      f'(partial row and/or broken metadata) - ignored')
        s.close()


def _build(out, sources, dpi, quiet=False):
    width = sources[0].width
    for s in sources:
        if s.width != width:
            raise SystemExit(f'ERROR: width mismatch - {sources[0].path} is {width:,} px '
                             f'but {s.path} is {s.width:,} px')
    orders = {s.sample_order for s in sources}
    if len(orders) > 1:
        raise SystemExit(f'ERROR: inputs disagree on sample byte order: '
                         + ', '.join(f'{s.path}={s.sample_order}' for s in sources))
    en = '<' if orders.pop() == 'little' else '>'

    w = BigTiffWriter(out, width, dpi=dpi, byte_order=en)
    chunk = max(1, STRIP_TARGET // (width * BYTES_PER_PX))
    total = 0
    for s in sources:
        for nrows, data in s.rows(chunk):
            w.write_rows(nrows, data)
            total += nrows
            if not quiet and total % (chunk * 8) < chunk:
                print(f'\r  {total:,} rows', end='', flush=True)
    H, nstrips = w.close()
    if not quiet:
        print(f'\r  {H:,} rows, {nstrips} strips written')
    return H, nstrips, en


def cmd_convert(a):
    s = Source(a.inputs[0], a.width)
    print(f'input : {s.path}  ({s.kind}, samples {s.sample_order}-endian)')
    print(f'        {s.width:,} x {s.height:,}')
    H, n, en = _build(a.out, [s], a.dpi)
    s.close()
    _report(a.out, H, n)


def cmd_stitch(a):
    srcs = [Source(p, a.width) for p in a.inputs]
    print(f'stitching {len(srcs)} inputs, top to bottom:')
    for s in srcs:
        print(f'  {s.path:40} {s.width:,} x {s.height:,}  ({s.sample_order}-endian)')
    total_h = sum(s.height for s in srcs)
    print(f'output height will be {total_h:,} rows')
    H, n, en = _build(a.out, srcs, a.dpi)
    for s in srcs:
        s.close()
    if H != total_h:
        raise SystemExit(f'ERROR: wrote {H:,} rows, expected {total_h:,}')
    _report(a.out, H, n)


def _report(path, H, n):
    i = read_bigtiff(path)
    ok = (i['height'] == H and len(i['strip_offsets']) == n)
    declared = sum(i['strip_counts'])
    expect = i['width'] * i['height'] * BYTES_PER_PX
    print(f'\noutput: {path}')
    print(f'  {i["width"]:,} x {i["height"]:,}, {len(i["strip_offsets"])} strips')
    print(f'  pixel bytes {declared:,} of {expect:,} expected'
          f'  {"OK" if declared == expect else "MISMATCH"}')
    print(f'  file size {i["size"]:,} ({i["size"]/1e9:.2f} GB)')
    print(f'  readback  {"OK" if ok and declared == expect else "FAILED"}')
    if not (ok and declared == expect):
        raise SystemExit(1)


def cmd_verify(a):
    for p in a.inputs:
        i = read_bigtiff(p)
        declared = sum(i['strip_counts'])
        expect = i['width'] * i['height'] * BYTES_PER_PX
        last = max(o + c for o, c in zip(i['strip_offsets'], i['strip_counts']))
        checks = [
            ('magic 43 / offset size 8', True),
            ('IFD within file', i['ifd_offset'] < i['size']),
            ('strip data within file', last <= i['size']),
            ('pixel bytes match dimensions', declared == expect),
            ('16 bits x 3 samples', i['bits'] == [16, 16, 16] and i['spp'] == 3),
            ('strips non-overlapping and ascending',
             all(i['strip_offsets'][k] + i['strip_counts'][k] <= i['strip_offsets'][k + 1]
                 for k in range(len(i['strip_offsets']) - 1))),
            ('strip count matches ceil(height / rows_per_strip)',
             len(i['strip_offsets']) ==
             -(-i['height'] // i['rows_per_strip'])),
            ('all strips but the last are exactly rows_per_strip rows',
             all(c == i['rows_per_strip'] * i['width'] * BYTES_PER_PX
                 for c in i['strip_counts'][:-1])),
        ]
        print(f'{p}: {i["width"]:,} x {i["height"]:,}, {i["size"]:,} bytes')
        good = True
        for name, c in checks:
            good &= c
            print(f'   {"PASS" if c else "FAIL"}  {name}')
        print(f'   -> {"VALID" if good else "INVALID"}')
        if not good:
            raise SystemExit(1)


def cmd_selftest(a):
    import tempfile, hashlib, random
    ok = True

    def chk(c, m):
        nonlocal ok
        ok &= c
        print(f'  {"PASS" if c else "FAIL"}  {m}')

    tmp = tempfile.mkdtemp()
    W, H1, H2 = 1000, 37, 51
    random.seed(7)

    def make_classic(path, w, h, en, seed):
        """A minimal but valid classic TIFF, one strip per row."""
        rng = random.Random(seed)
        # image-like: smooth gradients plus a little noise, as a real scan looks
        rows = []
        for y in range(h):
            vals = []
            for x in range(w):
                base = (x * 37 + y * 91 + seed * 1000) % 40000 + 10000
                for c in range(SPP):
                    vals.append(min(65535, max(0, base + c * 700 + rng.randrange(-60, 60))))
            rows.append(struct.pack(en + f'{w*SPP}H', *vals))
        data = b''.join(rows)
        rb = w * BYTES_PER_PX
        f = open(path, 'wb')
        f.write(b'II' + struct.pack('<HI', 42, 0))
        f.write(data)
        ifd = f.tell()
        offs = [8 + k * rb for k in range(h)]
        cnts = [rb] * h
        ent = [(256, LONG, 1, struct.pack('<I', w)),
               (257, LONG, 1, struct.pack('<I', h)),
               (258, SHORT, 3, None), (259, SHORT, 1, struct.pack('<HH', 1, 0)),
               (262, SHORT, 1, struct.pack('<HH', 2, 0)),
               (273, LONG, h, None), (277, SHORT, 1, struct.pack('<HH', 3, 0)),
               (278, LONG, 1, struct.pack('<I', 1)), (279, LONG, h, None),
               (284, SHORT, 1, struct.pack('<HH', 1, 0))]
        blob_at = ifd + 2 + 12 * len(ent) + 4
        bps_off = blob_at
        off_off = bps_off + 6
        cnt_off = off_off + 4 * h
        f.seek(ifd)
        f.write(struct.pack('<H', len(ent)))
        for tag, typ, cnt, val in ent:
            if val is None:
                ptr = {258: bps_off, 273: off_off, 279: cnt_off}[tag]
                val = struct.pack('<I', ptr)
            f.write(struct.pack('<HHI', tag, typ, cnt) + val)
        f.write(struct.pack('<I', 0))
        f.write(struct.pack('<HHH', 16, 16, 16))
        f.write(struct.pack(f'<{h}I', *offs))
        f.write(struct.pack(f'<{h}I', *cnts))
        f.seek(4)
        f.write(struct.pack('<I', ifd))      # patch the real IFD offset
        f.close()
        return data

    print('selftest: single-file convert')
    a_path = os.path.join(tmp, 'a.tif')
    d1 = make_classic(a_path, W, H1, '<', 1)
    s = Source(a_path)
    chk(s.width == W and s.height == H1, f'source parsed {s.width}x{s.height}')
    chk(s.sample_order == 'little', f'sample order detected: {s.sample_order}')
    out = os.path.join(tmp, 'a.btf')
    Hh, n, en = _build(out, [s], None, quiet=True)
    s.close()
    i = read_bigtiff(out)
    chk(i['width'] == W and i['height'] == H1, 'BigTIFF dimensions match')
    body = b''.join(open(out, 'rb').read()[o:o + c]
                    for o, c in zip(i['strip_offsets'], i['strip_counts']))
    chk(hashlib.md5(body).hexdigest() == hashlib.md5(d1).hexdigest(),
        'pixel data byte-identical after conversion')

    print('selftest: stitch')
    b_path = os.path.join(tmp, 'b.tif')
    d2 = make_classic(b_path, W, H2, '<', 2)
    sa, sb = Source(a_path), Source(b_path)
    out2 = os.path.join(tmp, 'ab.btf')
    Hh, n, en = _build(out2, [sa, sb], None, quiet=True)
    sa.close(); sb.close()
    i2 = read_bigtiff(out2)
    chk(i2['height'] == H1 + H2, f'stitched height {i2["height"]} = {H1}+{H2}')
    body2 = b''.join(open(out2, 'rb').read()[o:o + c]
                     for o, c in zip(i2['strip_offsets'], i2['strip_counts']))
    chk(hashlib.md5(body2).hexdigest() == hashlib.md5(d1 + d2).hexdigest(),
        'stitched pixel data byte-identical and in order')

    print('selftest: headless input')
    hl = os.path.join(tmp, 'headless.raw')
    with open(hl, 'wb') as f:
        f.write(b'II' + struct.pack('<HI', 42, 0))
        f.write(d1)
    s = Source(hl, width=W)
    chk(s.height == H1, f'headless height inferred: {s.height}')
    chk(s.kind.startswith('headless'), 'recognised as unfinalised')
    s.close()

    print('selftest: big-endian samples')
    be = os.path.join(tmp, 'be.tif')
    dbe = make_classic(be, W, H1, '>', 3)
    s = Source(be)
    chk(s.sample_order == 'big', f'detected big-endian samples: {s.sample_order}')
    s.close()

    print('selftest: rejects mismatched widths')
    c_path = os.path.join(tmp, 'c.tif')
    make_classic(c_path, W + 10, 5, '<', 4)
    sa, sc = Source(a_path), Source(c_path)
    try:
        _build(os.path.join(tmp, 'bad.btf'), [sa, sc], None, quiet=True)
        chk(False, 'width mismatch rejected')
    except SystemExit:
        chk(True, 'width mismatch rejected')
    sa.close(); sc.close()

    print('selftest: verify catches a truncated file')
    trunc = os.path.join(tmp, 'trunc.btf')
    raw = open(out, 'rb').read()
    open(trunc, 'wb').write(raw[:len(raw) - 200] + raw[-200:][:100])
    try:
        cmd_verify(argparse.Namespace(inputs=[trunc]))
        chk(False, 'truncated file rejected')
    except SystemExit:
        chk(True, 'truncated file rejected')
    except Exception:
        chk(True, 'truncated file rejected')

    print()
    print('SELFTEST', 'PASSED' if ok else 'FAILED')
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('info', 'convert', 'stitch', 'verify', 'crop'):
        p = sub.add_parser(name)
        p.add_argument('inputs', nargs='+')
        p.add_argument('--width', type=int, help='pixel width, required for headless input')
        p.add_argument('--dpi', type=float, help='resolution to record in the output')
        if name in ('convert', 'stitch', 'crop'):
            p.add_argument('-o', '--out', required=True)
        if name == 'crop':
            p.add_argument('--at', help='top-left as X,Y (default: centre)')
            p.add_argument('--size', default='900x900', help='crop size, e.g. 900x900')
    sub.add_parser('selftest')
    a = ap.parse_args()
    return {'info': cmd_info, 'convert': cmd_convert, 'stitch': cmd_stitch,
            'verify': cmd_verify, 'crop': cmd_crop, 'selftest': cmd_selftest}[a.cmd](a) or 0


if __name__ == '__main__':
    sys.exit(main())
