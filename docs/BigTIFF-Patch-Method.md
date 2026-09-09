# Making a libtiff 3.x application write BigTIFF, by binary patch

**Method note · 9 September 2026**

How ColorQuartet Pro 5.2 (`CQscan.exe`, 2002, statically linked libtiff 3.x) was patched
to emit BigTIFF, without source and without converting libtiff to 64-bit offsets.

Written up for reuse on **Newcolor 7000** and other Heidelberg/ScanView-era prepress
applications, most of which link the same vintage of libtiff. The offsets are
ColorQuartet's; the method and the failure modes are portable.

It took **five failed builds**. Those failures are the most useful part of this document,
so they are in §6 rather than omitted.

---

## 1. The insight

Classic TIFF stores every file offset in 32 bits. 4 GiB is an architectural ceiling.

Converting a statically linked libtiff 3.x to BigTIFF is infeasible by patching: `toff_t`
is `uint32` across thousands of sites, and BigTIFF needs 64-bit offsets, 20-byte IFD
entries instead of 12, an 8-byte entry count, and new tag types. That is a library
replacement.

But a classic TIFF and a BigTIFF differ in only two regions:

```
classic:   8-byte header | pixel data | 12-byte IFD entries, 4-byte offsets
BigTIFF:  16-byte header | pixel data | 20-byte IFD entries, 8-byte offsets
```

**The pixel data is byte-identical.** So the question is never "can libtiff write
BigTIFF". It is: *can libtiff keep streaming pixels correctly while we write the structure
ourselves?*

---

## 2. First, establish the real failure

Do not design before you know what breaks. We misdiagnosed this twice.

**Symptom:** a 2.54 GB scan produced a file whose header was correct, whose pixel data was
complete to the byte, and which had no image file directory at all. The application
reported success.

**Cause 1 — the seek.** libtiff's Win32 seek proc:

```asm
push [esp+0xc]        ; dwMoveMethod
push 0                ; lpDistanceToMoveHigh = NULL   <-- the bug
push [esp+0x10]       ; lDistanceToMove
push [ecx+4]
call SetFilePointer
```

With `lpDistanceToMoveHigh = NULL`, Windows reads the distance as **signed**. Any position
above 2,147,483,647 is negative, and `FILE_BEGIN` with a negative distance fails with
`ERROR_NEGATIVE_SEEK` (131).

**Cause 2 — nobody checks.** `TIFFClose` discards the flush result, and the application's
`Dto_CloseFile` wrapper ends:

```asm
call TIFFClose
add  esp, 4
mov  dword [esi+8], 0
mov  eax, 1            ; <-- unconditional success
ret
```

### Prove it in one second, not one scan

Seeking past end-of-file is legal in Win32, so a 1-byte file suffices. Call
`SetFilePointer` twice per position — once with `NULL`, once with a pointer to a zeroed
high dword:

```
        position  NULL high (as shipped)   high pointer
   2 147 483 647  OK                       OK
   2 147 483 648  FAIL  err 131            OK
   2 724 280 040  FAIL  err 131            OK      <- the real IFD offset
   4 294 967 295  FAIL  err 131            OK
```

`Test-SeekLimit.ps1` does this. **Run it first on any new target.** It turned a diagnosis
that had already cost hours into a fact in one second.

---

## 3. The four feasibility checks

Answer all four before writing code. Any failure is a stop.

### Q1 — Does the pixel path seek?

If strip writes use absolute seeks they break at 2 GiB too, and the approach dies. Find
`TIFFAppendToStrip` (its error strings name it):

```asm
mov  eax, [eax + edi*4]   ; td_stripoffset[strip]
test eax, eax
je   new_strip
mov  ecx, [esi+0x128]     ; tif_curoff
test ecx, ecx
jne  do_write             ; <-- already positioned: NO SEEK AT ALL
...
new_strip:
push 2 ; push 0           ; SEEK_END, distance 0
call [esi+0x1c0]
```

**A zero distance is never negative, so `SEEK_END, 0` succeeds at any size.** And once
positioned it does not seek. Pixel streaming is size-agnostic.

libtiff's own `td_stripoffset[]` and `tif_curoff` wrap above 4 GiB. That does not matter —
we never read them.

### Q2 — Is the strip layout predictable?

For uncompressed data, yes: `header + i × stripBytes`, last strip short.

**Verify against real output rather than assuming.** We had five valid files from the same
application. Predicting `rowsPerStrip`, strip count, every offset and every byte count,
then diffing:

```
27,084 x 420    rps 3  nstrips 140   offsets ALL MATCH  counts ALL MATCH
23,810 x 420    rps 3  nstrips 140   offsets ALL MATCH  counts ALL MATCH
21,801 x 330    rps 4  nstrips  83   offsets ALL MATCH  counts ALL MATCH
11,910 x 331    rps 7  nstrips  48   offsets ALL MATCH  counts ALL MATCH
11,905 x 421    rps 7  nstrips  61   offsets ALL MATCH  counts ALL MATCH
```

Free, and the single highest-value check in the exercise. `rowsPerStrip` came from the
application's own open routine: `0x40000 / (width × samples)`, minimum 1.

**Caveat that later bit us:** predicting the layout is not the same as knowing the final
line count. See §6.5.

### Q3 — Is there code cave space, and is it WRITABLE?

PE sections are padded to file alignment:

```
name          VA        vsize      rawsize   slack   flags     R W X
.text     0x00401000  1,005,760  1,007,616   1,856  60000020  1 0 1
.rdata    0x004F7000    124,482    126,976   2,494  40000040  1 0 0
.data     0x00516000    137,832    106,496       0  C0000040  1 1 0
```

**Check the W column.** We did not, and it cost a build (§6.1). `.text` is read+execute.
Mutable data — variables, and any template patched at runtime — cannot live there unless
you set `IMAGE_SCN_MEM_WRITE` (`0x80000000`) in the section characteristics. That is a
one-dword change in the PE header and works fine.

### Q4 — Can the geometry be reached at close time?

Do not try to prove the parameter block is still live. **Capture the geometry at open,
where it certainly is, and stash it in cave variables.** That also gives a natural place to
neutralise the application's own 4 GiB check, which has to go anyway.

---

## 4. The design

```
at open    stash width/samples/bits into cave variables
           neutralise the 4 GiB check          (jle -> jmp, one byte)
           pad the file from 8 to 16 bytes     (BigTIFF header size)

at close   read the REAL layout from libtiff's directory
           we are ALREADY at the end of the pixel data
             -> write the BigTIFF IFD there, sequentially. NO SEEK.
           seek to 0 - small and safe - and write the 16-byte header
           set tif_mode = 0, then call TIFFClose
```

**No seek above 2 GiB is ever performed.** That is the whole trick: the only large offset
is where you already are.

### Reserving 16 bytes

libtiff writes an 8-byte classic header at open. BigTIFF needs 16. Writing 16 at offset 0
would clobber the first 8 bytes of pixel data.

Do not assume the position is 8 — **measure it**:

```asm
push 2 ; push 0 ; push [esi+0x1b4]   ; seek(clientdata, 0, SEEK_END)
call [esi+0x1c0]
add  esp, 0xc
cmp  eax, 16
jae  done                            ; already >= 16
mov  ecx, 16
sub  ecx, eax                        ; write exactly what is missing
```

### Suppressing libtiff's own directory write

No patch to `TIFFClose` needed. It begins:

```asm
mov  eax, [esi+8]      ; tif_mode
test eax, eax
je   skip_flush        ; O_RDONLY == 0
call TIFFFlush
```

Set `tif_mode = 0` before calling it. One `mov`.

### Use libtiff's own file procs, not Win32

**The most important implementation detail.** Do not extract the OS handle.
`TIFFAppendToStrip` shows the convention:

```asm
mov  edx, [esi+0x1b4]      ; tif_clientdata
push 2 ; push 0 ; push edx
call [esi+0x1c0]           ; seek(clientdata, offset, whence)
add  esp, 0xc              ; cdecl - caller cleans
```

```
tif_clientdata      [tif+0x1B4]
tif_writeproc       [tif+0x1BC]    (clientdata, buffer, length)
tif_seekproc        [tif+0x1C0]    (clientdata, offset, whence)
td_nstrips          [tif+0xC0]
td_stripoffset[]    [tif+0xC4]
td_stripbytecount[] [tif+0xC8]
tif_curoff          [tif+0x128]
```

Deriving the handle yourself is a guess that fails silently (§6.3).

### Read the layout from libtiff, not from your own arithmetic

At close, take `td_nstrips` and `td_stripbytecount[]` and compute:

```
offsets  = 16 + running 64-bit sum of counts
total    = sum of counts
height   = total / rowBytes
ifd_off  = 16 + total
```

Byte counts never overflow 32 bits — only offsets do — so libtiff's counts are always
correct, and the running sum is exact at any size. This is immune to the application
changing the line count after open (§6.5).

### Keep the x86 small

Do not build IFD entries instruction by instruction. Put a 256-byte template in the cave —
`[8-byte count][12 entries × 20][8-byte next]` — and patch only what varies: width,
height, bits, samples, rowsPerStrip, and the count/offset pairs for `StripOffsets` and
`StripByteCounts`.

The two arrays are written by loops, one call per 8-byte element. Roughly 12,000 calls on
a large image, about a second at close. Inelegant, but needs no buffer and cannot overflow
one.

### Only use BigTIFF when needed

Threshold at **2 GiB, not 4**. Below 2 GiB libtiff's own seek works, so let it write a
classic directory — previews stay readable by the application, and compatibility is
maximal. At or above 2 GiB, only the patch can succeed.

```asm
test edx, edx
jne  do_bigtiff
cmp  eax, 0x80000000
jb   bail                  ; classic
```

Getting this wrong at 4 GiB would send a 2.7 GB file back into the original bug.

### Final size

```
SAVE_GEOM      42 bytes    stash at open
PAD            69 bytes    16-byte reservation
WRITE_BIGTIFF 711 bytes    the writer
IFD template  256 bytes
header buffer  16 bytes
variables      32 bytes
```

About 1,110 bytes in 1,856 of `.text` slack.

---

## 5. Verification before it ever runs

**Model the output in a high-level language first.** Write the header and IFD generator in
Python, emit a small file, and validate with an independent reader — `tifffile` with
warnings as errors caught a spec violation a home-grown verifier had passed (strip count
not matching `ceil(height / rowsPerStrip)`).

**Emulate the emitted machine code.** Step the exact instruction sequence in Python — the
`div`, the `mul` producing `edx:eax`, the `add`/`adc` carry chain — and diff its output
against the model. Eight geometries, byte-identical, including grayscale and three past
4 GiB.

**Disassemble everything back out** of the patched binary, not the generator's idea of
what it wrote.

**Guard every edit against its expected original bytes.** This caught a mistyped offset
that would otherwise have corrupted an unrelated function.

None of that caught the bugs in §6. It is still worth doing — it caught others — but do
not mistake it for proof.

---

## 6. The five failures

### 6.1 — Variables in a read-only section

`0xC0000005` at the first `mov [cave_var], eax`. The read on the previous instruction
worked; the write faulted.

I verified the cave was zero and executable, then quietly started storing data in it.
`.text` is `R-X`.

**Fix:** set `IMAGE_SCN_MEM_WRITE` in the `.text` characteristics —
`0x60000020 → 0xE0000020`.

**Lesson:** every static proof I had done was about *values* — is this register the right
pointer. None asked whether the memory I was writing to could be written.

### 6.2 — Misdiagnosing the next failure

With `.text` writable, a preview completed and the application said "Cannot open the TIFF
file". I concluded BigTIFF was unreadable by its own libtiff and added a size threshold so
previews stayed classic.

Wrong. The preview had **no directory at all** — the same bug as 6.3. The threshold is
worth having anyway, but it was fixing a symptom of something else.

**Lesson:** an explanation that also predicts the observation is not the same as the cause.
I had two candidates and picked the tidier one without testing.

### 6.3 — A guessed structure offset, failing silently

Every write went through `WriteFile` with a handle derived as `[tif+0x1b4] → [+4]`. That
chain was a guess. It produced garbage, `SetFilePointer` returned `0xFFFFFFFF`, and my
"already ≥ 16, skip" check treated that as success. Every `WriteFile` failed silently too.
Meanwhile `tif_mode = 0` had already suppressed libtiff's directory.

Result: no pad bytes, no BigTIFF header, no classic IFD. A file with correct pixels and no
structure whatsoever.

**Fix:** call libtiff's own procs (§4), which removes the handle question entirely.

**Two lessons.** First, I found `mov esi, ecx` in what I thought was the write proc and
concluded thiscall — it was a C++ method behind a cdecl thunk. Second, **a failure path
that looks like success is worse than a crash.** `cmp eax,16 / jae skip` silently absorbed
`INVALID_SET_FILE_POINTER`.

### 6.4 — Writing the design and not the code

The 16-byte reservation was in my design document, described as part of the working
design. It was never implemented. Pixel data started at 8 while the IFD claimed 16 — every
strip offset wrong, and the header would have clobbered the first 8 bytes.

Caught only by comparing bytes 8–15 of the output against real files, which showed pixel
data where pad bytes should have been.

**Lesson:** documentation written ahead of implementation quietly becomes a claim that it
was implemented.

### 6.5 — Trusting the recorded height

The parameter block said 961 lines. The scanner's resolution correction changed it after
open. Our IFD offset overshot the file by 91,860 bytes.

**Fix:** read `td_nstrips` and `td_stripbytecount[]` from libtiff at close and sum them.
Those reflect what was actually written.

**Lesson:** the check in Q2 verified I could *predict the layout given the dimensions*. It
never verified the dimensions themselves stayed true. A check that validates part of a
chain is easy to mistake for validating the chain.

---

## 7. Applying this to another target

In order, each cheap:

1. **Does it link libtiff 3.x?** Search for `TIFFWriteScanline`, `TIFFSetField`,
   `Not a TIFF file, bad version number`. Absence of the string `BigTIFF` confirms 3.x.
2. **Run the seek test** (§2). One second.
3. **Find the close wrapper** by its error strings; check whether it discards the result.
4. **Read `TIFFAppendToStrip`**; confirm `SEEK_END, 0` and the skip-if-positioned branch.
5. **Validate the strip-layout prediction against existing output files.** Free, and the
   highest-value check.
6. **Measure PE section slack — and its writability.**
7. **Find the proc pointer offsets** by reading how libtiff itself calls them. Do not
   derive the OS handle.

If 1, 4 and 5 hold, the same design applies. The offsets will differ; the shape will not.

**One caution for Newcolor specifically:** a community TIFF file-size patch already exists
(Philipp Wagner's, lifting the limit to 4 GB on Windows). Check what it does before
patching the same area — it may already fix the seek, leaving only the BigTIFF structure
work, and coordination beats duplication.

---

## 8. What actually mattered

- **Build the falsifying test before the fix.** Every real bug here was found by a test
  designed to fail visibly, never by more analysis.
- **Verify against artefacts you already have.** Five existing files proved the geometry
  model at zero cost.
- **Call the application's own abstractions.** Every bug in §6.3 came from reaching past
  libtiff to the OS.
- **Silent failure is the enemy.** Two of five failures produced no crash, no error, and a
  plausible-looking file.
- **A 50 MB test exercises the same path as a 5 GB one.** Iterate in minutes.
- **Say what you have not verified.** The 64-bit carry path has still never executed —
  every scan so far keeps the IFD offset under 2³². Stated, not buried.
