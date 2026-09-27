Download via newest release! :)
# ScanMate 11000 unlock

Firmware and host patches for the ScanView ScanMate 11000 drum scanner.

The scanner could never scan a line wider than 10,922 pixels in 16-bit RGB. That limit was
not the optics or the mechanics. It was 16-bit variables in firmware from 1995, a buffer
pool using a third of the RAM already fitted, and the same kind of limits in the host
software. This fixes all of it. No hardware changes.

| | Factory | With this |
|---|---|---|
| Widest line, 16-bit RGB | 10,922 px | 35,492 px |
| Widest line, 8-bit | 21,845 px | 70,991 px |
| File size limit | 2 GB in practice | none, writes BigTIFF |

Every 120 format now reaches 11,000 dpi, which is the optical limit of the machine. 4x5
reaches 9,489 dpi, 8x10 reaches 4,646 dpi in 16-bit and 9,294 in 8-bit.

## What you need

- A ScanMate 11000 running firmware 10.04. The service console prints
  `Prom version is 10.04` at boot. Another version needs another build.
- ColorQuartet Pro 5.2 with CQscan.exe, 2,211,840 bytes.
- Python 3.7 or newer, for the patcher. It only edits the file, so you can run it on any
  PC and copy the patched CQscan.exe over afterwards. Windows 7 can run up to Python 3.8.
  Windows XP cannot run 3.7, so patch on another machine.

You do not need an EPROM programmer. The chips stay in the machine.

## Before you start

Copy CQscan.exe somewhere safe, and save a `*` dump from the service console so your
calibration is written down.

You cannot brick the scanner by flashing. The boot block is locked in hardware and never
written, it checks the image before programming it, and it has its own SCSI service and
download code, so even a main block that got corrupted halfway through should still accept
a new download. Nobody has had to find out. A flash takes about 30 seconds and the chips
are rated for 100,000 cycles.

If you do own a programmer, reading both chips first is cheap insurance, and it is the only
way to build an image for a scanner on a firmware revision other than 10.04.

## Install

Short version in `INSTALL.txt`.

1. Check the SCSI card. Run `tools\Get-ScsiPortCaps.ps1` in an elevated PowerShell. It
   prints what your card can actually do and says whether anything needs changing. Old
   Adaptecs on the aic78xx driver need one registry value, `MaximumSGList = 65`. Most
   later cards need nothing. Do not set the value blindly, it can also lower what you
   already have.
2. Patch CQscan with `tools\patch-cqscan.py`. It writes `CQscan.exe.original` first, so
   you can always undo it.
3. Flash `firmware\SCA04000_pool6800.A6` from CQscan's firmware update. The console prints
   `A8FF` at boot when it is running. Stock prints `00FF`.
4. Run a white calibration. See below.
5. Test narrow first, then wide.

## A flash wipes the gain tables

After every flash, all six R/G/B Gain and PMTGain tables read back as the factory
defaults stored in the flash itself. I checked this by reading the defaults out of the
EPROM image and comparing them against a console dump taken after a flash. All ten
apertures matched exactly.

Index offset, spindle offset, focus status and the barcode tables survive a flash. That is
why it can look like nothing happened if you only check those. Save a `*` dump before and
after, and plan a white calibration.

## Big files

Under 2 GB you get a normal TIFF, same as before, and ColorQuartet can still read it.
At 2 GB and above the patch writes BigTIFF instead. There is no size limit after that.

ColorQuartet cannot read BigTIFF, so after a very large scan it may say "Cannot open the
TIFF file". The file is fine. Open it in Photoshop or GIMP. Windows Photos cannot open it.

Save big scans to an NTFS or exFAT drive. FAT32 cannot hold a file over 4 GB.

`tools\bigtiff.py` checks and converts these files, and can pull a 1:1 crop so you can
judge focus without downscaling:

```
python bigtiff.py verify  scan.tif
python bigtiff.py info    scan.tif --width <px>
python bigtiff.py convert scan.tif -o out.tif --width <px> --dpi <dpi>
python bigtiff.py crop    scan.tif --width <px> -o check.png --size 900x900 --at X,Y
```

It also rescues a scan that ColorQuartet failed to finish writing. The pixels always reach
the disk even when the directory does not.

## Limits

- 35,492 pixels is the ceiling. Do not go past it. Above that only one line buffer fits,
  and with one buffer the scanner stops mid scan and hangs the SCSI bus with no timeout.
  Power cycle to recover. Nothing is damaged.
- The console should always report 2 buffers or more. If it ever says 1, power cycle.
- Going higher would need the RAM at 0x80000 and up, which has never been tested and may
  not even be there. The real-mode ceiling is 57,338 pixels in any case, because ROM
  starts at 0xC0000.
- Mount film with the short side around the drum. Only that direction is limited.
- Take the thumbwheel out of service position for real scans. The console output doubles
  the scan time at wide widths, 553 ms per line against 293.

## Files

```
firmware/  SCA04000_pool6800.A6   the one to use, 35,492 px, marker A8FF
           SCA04000_pool.A6       older, 27,300 px, marker A5FF, for rollback
           SCA04000_owntest.A6    RAM test build, marker A7FF
tools/     patch-cqscan.py        patches your own CQscan.exe
           bigtiff.py             check, convert and crop big files
           Get-ScsiPortCaps.ps1   what your SCSI card can actually do
           Test-SeekLimit.ps1     shows the 2 GB seek bug, needs PowerShell 5
           fwpatch.py             ROM checksums
           ground.py              segment to file offset, with anchor checks
docs/      BigTIFF-Patch-Method.md  how the BigTIFF patch was done
```

MD5 sums for everything are in `CHECKSUMS.md5`. On Windows, check a file with
`certutil -hashfile <file> MD5`. Both ROM checksums in all three firmware
files are correct, and the three files are byte for byte what was flashed and tested.

No ColorQuartet files are included here. The patcher edits the copy you already own, and
writes `CQscan.exe.original` first so you can undo it.

## What is tested and what is not

Tested on hardware:

- 29,763 px at 16-bit, 333 lines, start to finish, 178,578 bytes per line, on 2 buffers.
  On the old firmware that width would have hung.
- BigTIFF output written by the patch and opened.
- Everything narrower, many times.

Not tested on hardware:

- Anything above 4 GB. The 64-bit part of the patch has only been run in emulation, where
  it is correct. Treat your first scan over 4 GB as a test and run `bigtiff.py verify`.
- Lines above 178,578 bytes, up to the 212,952 the ceiling allows. The SCSI card covers it
  on paper.
- `Get-ScsiPortCaps.ps1` has never been run on a real machine.
- One scanner, one firmware version, one SCSI card.

## What was actually wrong

Nine separate limits, each one hiding the next:

| | Where | Was |
|---|---|---|
| 1 | aic78xx scatter/gather list | 64 KB per command |
| 2 | CQscan chunk budget | 61,696 B |
| 3 | CQscan transfer guard | 65,535 B |
| 4 | CQscan pass-through buffer | 131,148 B |
| 5 | DMA byte count, written as 16-bit | 65,536 B |
| 6 | SCSI line count in firmware, 16-bit | 65,535 B |
| 7 | Buffer allocator fed 16-bit byte counts | 65,535 B |
| 8 | SCSI drain, per call | 65,535 B |
| 9 | Buffer pool | 160 KB |

Numbers 2 to 4 in CQscan must always be changed together, and the pass-through buffer must
be the transfer guard plus 0x4C. Raising one without the other corrupts the heap and
crashes later somewhere unrelated.

The DMA was the part I expected to be a dead end, because it looked like it belonged to the
CPLDs and those cannot be read out. It turned out to be the i386EX's own DMA controller.
Its byte counter is 24 bits wide and ScanView had already switched on all 24 bits. The
firmware then wrote zero into the top byte. The hardware had been ready for this since
1995.

## Thanks

Bjarne, who wrote this firmware at ScanView, for answering questions about decisions he
made thirty years ago. He remembered "a smart FIFO in memory with a write pointer and a
read pointer", which is exactly where the real limit was.

Karl Hudson at Hudson Grafik and Philipp Wagner at Zoom and Enhance, for keeping these
machines running.

## Licence

Tools and documentation are MIT. The firmware images come from ScanView / Purup-Eskofot
code and are for use with hardware you own.

No warranty. You are changing firmware in a thirty year old machine.
