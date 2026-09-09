# ScanMate 11000 — full unlock

Firmware and host patches for the **ScanView ScanMate 11000** drum scanner. Raises the
16-bit line ceiling from the factory **10,922 px** to **35,492 px**, and removes the file
size limit entirely by making ColorQuartet write BigTIFF.

All on completely unmodified hardware. The limits were never optical or mechanical — they
were 16-bit variables in firmware written in 1995, a DRAM pool using a third of the RAM
already fitted, and matching bottlenecks in the host software.

| | Factory | This release |
|---|---|---|
| Max line, 16-bit RGB | 10,922 px | **35,492 px** |
| Max line, 8-bit RGB | 21,845 px | **70,991 px** |
| DRAM pool in use | 31 % | **81 %** |
| Max file size | 4 GiB (2 GiB in practice) | **BigTIFF, no practical limit** |
| 35 mm / 645 / 6×6 / 6×7 / 6×9, 16-bit | 4,953–11,000 dpi | **11,000 dpi — the optical limit** |
| 4×5, 16-bit | 2,920 dpi | **9,489 dpi** |
| 5×7, 16-bit | 2,311 dpi | **7,512 dpi** |
| 8×10, 16-bit | 1,429 dpi | **4,646 dpi** |
| 8×10, 8-bit | 1,429 dpi | **9,294 dpi** |

Every figure verified on hardware. The final wide test ran **29,763 px × 333 lines** to
completion — a scan that on the previous firmware would have allocated a single buffer and
hung the SCSI bus.

---

## ⚠ Before you flash anything

**Dump your own EPROMs first.** Two flash chips, P6310 (IC10) and P6314 (IC14), Intel
N28F001BX-T120, PLCC32, socketed. Read each three times with a programmer such as an XGecu
T48 and confirm the reads match. This is your recovery path. Do not skip it.

**This firmware is built on version 10.04** (`SCA04000.A6`, 229,376 bytes). The service
console prints `Prom version is 10.04` at boot. A different revision needs a different
build.

**Every flash resets NVRAM calibration** to factory defaults. Capture the console's `*`
output before and after, and plan a white calibration afterwards.

**For hardware you own.** No ColorQuartet binary, installer or licence material is
redistributed here. `tools/patch-cqscan.py` modifies the copy you already have.

---

## Contents

```
firmware/   SCA04000_pool6800.A6    the release firmware      marker A8FF
            SCA04000_pool.A6        previous, 27,300 px       marker A5FF   (rollback)
            SCA04000_owntest.A6     RAM ownership test        marker A7FF
tools/      patch-cqscan.py         applies every host patch to YOUR CQscan.exe
tools/      bigtiff.py              BigTIFF converter, verifier and 1:1 crop tool
            Check-CQscanLimits.ps1  reads the host constants
            Get-ScsiPortCaps.ps1    does your SCSI adapter need tuning?
            Test-SeekLimit.ps1      demonstrates the 2 GiB seek fault in one second
            fwpatch.py, ground.py   ROM checksum and segment arithmetic
docs/       BigTIFF-Patch-Method.md how the BigTIFF work was done, for reuse elsewhere
```

MD5 sums are in `CHECKSUMS.md5` in each folder.

---

## Installing

### 1. Firmware — over SCSI, no chips removed

ColorQuartet contains an undocumented firmware downloader. It takes a file of exactly
229,376 bytes and sends it as seven 32 KB blocks. **The boot block is hardware-locked and
never written, so a bad image cannot brick the scanner.** About 30 seconds; the flash is
rated for 100,000 cycles.

CQscan → firmware update → select `firmware/SCA04000_pool6800.A6`.

The console prints **`A8FF`** at boot when the release firmware is running (stock prints
`00FF`).

### 2. SCSI adapter

The Adaptec `aic78xx` miniport defaults to a 17-entry scatter/gather list, capping every
transfer at 64 KB:

```
HKLM\SYSTEM\CurrentControlSet\Services\aic78xx\Parameters\Device
    MaximumSGList = 65    (DWORD)
```

**65 is the value to use, and it is sufficient with margin.** The host constants make
ColorQuartet allocate a 229,452-byte SPTD buffer (`0x3804C`), which in the worst case of
page misalignment spans **58** scatter/gather entries. 65 entries covers that with 7 to
spare, and 266,240 bytes against a 212,952-byte maximum line at 35,492 px.

If you raise the CQscan constants beyond `0x38000`, recompute: entries needed is
`ceil(buffer / 4096) + 1`.

**A different card? That key does nothing** — it belongs to that driver. Most later cards
already allow far more. Run `tools/Get-ScsiPortCaps.ps1` elevated to find out.

### 3. ColorQuartet

Run the patcher against your own installation. It backs up to `CQscan.exe.original`,
applies everything in one pass, verifies each step, and refuses to run twice:

```
python tools/patch-cqscan.py "C:\Program Files (x86)\Esko-Graphics\ColorQuartet Pro 5.2\CQscan.exe"
```

No ColorQuartet binary is distributed here. The changes it makes are:

| Offset | Field | From | To |
|---|---|---|---|
| `0x00116` | PE Characteristics | `0x010F` | `0x012F` (LARGE_ADDRESS_AWARE) |
| `0x339C0` | chunk budget | `0x0000F100` | `0x00038000` |
| `0x60FE8` | transfer guard | `0x0000FFFF` | `0x00038000` |
| `0x61A11` | SPTD buffer size | `0x0002004C` | `0x0003804C` |

> **⚠ Those last three must change together.** Raising the transfer guard without
> enlarging the SPTD buffer causes heap corruption that surfaces much later as an
> unrelated crash. The buffer must always equal the guard plus `0x4C`.

Plus the BigTIFF patch: `.text` marked writable, four hooks, and about 1,110 bytes of code
and data in section slack. See `docs/BigTIFF-Patch-Method.md`.

`tools/Check-CQscanLimits.ps1` reads the current values back.

---

## How BigTIFF behaves

Classic TIFF stores offsets in 32 bits, and libtiff 3.x cannot seek past 2 GiB to write
its directory. So:

- **Files under 2 GiB** are written as ordinary classic TIFF. Previews and normal work
  stay fully compatible, and ColorQuartet can read its own output.
- **Files at or above 2 GiB** are written as BigTIFF by the patch, which never seeks past
  the header. No practical size limit.

ColorQuartet cannot *read* BigTIFF, so it may report "Cannot open the TIFF file" after a
very large scan. The file is fine; open it in Photoshop or GIMP.

`tools/bigtiff.py` (Python 3, no dependencies) converts, verifies, and pulls 1:1 crops:

```
python bigtiff.py info    scan.tif --width <px>
python bigtiff.py convert scan.tif -o out.tif --width <px> --dpi <dpi>
python bigtiff.py verify  out.tif
python bigtiff.py crop    scan.tif --width <px> -o check.png --size 900x900 --at X,Y
```

It also recovers any scan ColorQuartet fails to finalise — the pixels always reach the
disk even when the directory does not. Use `crop` to judge focus; downscaling hides
softness.

---

## What was wrong

Nine independent obstacles, found and cleared one at a time:

| # | Layer | Was | Fix |
|---|---|---|---|
| 1 | `aic78xx` scatter/gather list | 65,536 B | registry |
| 2 | CQscan chunk budget | 61,696 B | `0x339C0` |
| 3 | CQscan transfer guard | 65,535 B | `0x60FE8` |
| 4 | CQscan SPTD buffer | 131,148 B | `0x61A11` |
| 5 | DMA byte count written 16-bit | 65,536 B | firmware |
| 6 | SCSI line count `es:[0x8e2]` 16-bit | 65,535 B | firmware |
| 7 | DRAM allocator fed 16-bit byte counts | 65,535 B | firmware |
| 8 | SCSI drain capped per call | 65,535 B | firmware, chunked |
| 9 | DRAM pool sized at 160 KB | — | firmware constant |

Plus the file-size work: a seek with a NULL high dword that fails above 2 GiB, and two
layers of discarded error returns above it.

### The one that nearly ended it

The image DMA looked like it might belong to the CPLDs, whose security fuses prevent
readout — which would have meant hardware modification or nothing.

It turned out to be the **i386EX's own integrated DMA controller**. Its byte count
register `DMA1BYC2` (expanded address `F099H`) is **24 bits**, and ScanView had already
enabled full 24-bit decrementing via `DMAOVFE = 0Fh` — then wrote zero to the top byte.
The hardware had been configured for it since 1995.

---

## Limits that remain

**35,492 px is the ceiling for this configuration.** Beyond it, the pool would need the
256 KB at 0x80000–0xBFFFF, which has never been probed and may not be decoded. The
absolute real-mode maximum is 57,338 px, since the i386EX cannot address past 0xC0000
where ROM begins.

**Single buffering hangs the SCSI bus.** Any width where only one buffer fits will stall
with no timeout; recovery is a power cycle. This release keeps two buffers to 35,492 px at
16-bit. Do not exceed it.

**Service-mode console output roughly doubles wide-scan time.** The firmware spins on the
UART's `TEMT` bit for every byte at 9600 baud, and at wide widths the per-chunk messages
are not amortised. Measured on a 19,036 × 23,852 scan: **553 ms/line with the console, 293
without** — about 100 minutes on that scan. Take the thumbwheel out of service position
for production work.

**Mount film with the short side around the drum.** Only that axis is limited; carriage
travel is free.

---

## Method

Every address derived in code from a verified anchor, never typed. Every edit guarded
against its expected original bytes. Every result disassembled back out before flashing.
One stage per flash, with a regression scan at a width where the change should be
invisible.

Sixteen confident conclusions turned out to be wrong across this work. Every one was
caught by a test designed to fail visibly, not by more analysis — including a BigTIFF
writer that stored variables in a read-only section, and another whose writes all failed
silently because a file handle was derived from a guessed structure offset.

---

## Credits

**Bjarne**, the original ScanView developer who wrote this firmware, for answering
questions about decisions made thirty years ago. His recollection of "a smart FIFO in
memory with a write pointer and a read pointer" pointed straight at the buffer pool.

**Karl Hudson** (Hudson Grafik) and **Philipp Wagner** (Zoom and Enhance) for keeping
these machines alive.

---

## Licence

Tools and documentation: MIT. The firmware images are derived from ScanView /
Purup-Eskofot code and are provided for use with hardware you own.

Provided as-is, no warranty. You are modifying firmware in a thirty-year-old machine.
Dump your EPROMs first.
