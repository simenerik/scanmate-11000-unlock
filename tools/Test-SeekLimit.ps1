<#
    Test-SeekLimit.ps1
    ScanMate 11000 project - proves why ColorQuartet fails to finalise large TIFFs

    Hypothesis: libtiff's seek proc in CQscan.exe (0x004E3C2E) calls

        SetFilePointer(handle, distance, NULL, FILE_BEGIN)

    With lpDistanceToMoveHigh = NULL, Windows treats `distance` as a SIGNED 32-bit
    value. Any position above 2,147,483,647 becomes negative and the call fails with
    ERROR_NEGATIVE_SEEK (131). The TIFF directory is therefore never written, and both
    TIFFClose and Dto_CloseFile discard the error - so ColorQuartet reports success.

    Seeking beyond end-of-file is legal in Win32, so this needs only a tiny file.
    Runs in about a second.
#>

$sig = @"
using System;
using System.Runtime.InteropServices;
public static class Seek {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Auto)]
    public static extern IntPtr CreateFile(string name, uint access, uint share,
        IntPtr sec, uint disp, uint flags, IntPtr templ);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint SetFilePointer(IntPtr h, int lo, IntPtr hi, uint method);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint SetFilePointerHigh(IntPtr h, int lo, ref int hi, uint method);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool CloseHandle(IntPtr h);
}
"@
Add-Type -TypeDefinition $sig -ErrorAction Stop

# SetFilePointerHigh is the same export; declare it twice so we can pass NULL or a ref
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class Seek2 {
    [DllImport("kernel32.dll", SetLastError=true, EntryPoint="SetFilePointer")]
    public static extern uint WithHigh(IntPtr h, int lo, ref int hi, uint method);
}
"@ -ErrorAction SilentlyContinue

$path = Join-Path $env:TEMP "seektest.bin"
Set-Content -Path $path -Value "x" -NoNewline

$h = [Seek]::CreateFile($path, 0x40000000, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero)
if ($h -eq [IntPtr](-1)) { throw "could not open $path" }

$FILE_BEGIN = 0
$INVALID    = [uint32]::MaxValue

function Try-Seek([long]$pos) {
    # reinterpret the low 32 bits as a signed int WITHOUT range checking -
    # [int] throws above 2,147,483,647, which is the very range we are testing
    $u  = [uint32]($pos -band 0xFFFFFFFF)
    $lo = [BitConverter]::ToInt32([BitConverter]::GetBytes($u), 0)
    $hi = [int]($pos -shr 32)

    # --- the way CQscan does it: NULL high pointer ---
    [void][Runtime.InteropServices.Marshal]::GetLastWin32Error()
    $r1 = [Seek]::SetFilePointer($h, $lo, [IntPtr]::Zero, $FILE_BEGIN)
    $e1 = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    $ok1 = -not ($r1 -eq $INVALID -and $e1 -ne 0)

    # --- the fix: pointer to a zeroed high dword ---
    $hi2 = $hi
    $r2 = [Seek2]::WithHigh($h, $lo, [ref]$hi2, $FILE_BEGIN)
    $e2 = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    $ok2 = -not ($r2 -eq $INVALID -and $e2 -ne 0)

    "{0,16:N0}  {1,-24} {2,-24}" -f $pos,
        $(if ($ok1) { "OK  (pos $r1)" } else { "FAIL  err $e1" }),
        $(if ($ok2) { "OK  (pos $r2)" } else { "FAIL  err $e2" })
}

""
"Seeking a 1-byte file to positions beyond EOF (legal in Win32)."
""
"{0,16}  {1,-24} {2,-24}" -f "position", "NULL high (CQscan)", "high pointer (fixed)"
"-" * 68
foreach ($p in @(
    1000,
    2147483646,      # 2 GiB - 2
    2147483647,      # 2 GiB - 1, largest positive signed 32-bit
    2147483648,      # 2 GiB exactly
    2724280040,      # <-- the IFD offset from Scan-52
    4294967295       # 4 GiB - 1, the classic TIFF ceiling
)) { Try-Seek $p }

[void][Seek]::CloseHandle($h)
Remove-Item $path -Force -ErrorAction SilentlyContinue

""
"Error 131 = ERROR_NEGATIVE_SEEK."
""
"If the left column fails at 2,147,483,648 and the right column succeeds,"
"the diagnosis is confirmed: ColorQuartet cannot seek past 2 GiB to write"
"the TIFF directory, and the fix is to pass a high dword instead of NULL."
""
