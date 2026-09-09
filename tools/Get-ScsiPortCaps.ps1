<#
    Get-ScsiPortCaps.ps1
    ScanMate 11000 project - does your SCSI adapter need tuning?

    The Adaptec aic78xx miniport defaults to a 17-entry scatter/gather list, which
    caps every transfer at 64 KB and was the first of nine walls in this project.
    Most later cards default far higher and need no change at all.

    This queries IOCTL_SCSI_GET_CAPABILITIES on each SCSI port and reports the real
    limits, so you can tell whether the registry tweak in the README applies to you.

    Run in an elevated PowerShell:
        .\Get-ScsiPortCaps.ps1
#>

$sig = @"
using System;
using System.Runtime.InteropServices;

public static class ScsiCaps {
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Auto)]
    public static extern IntPtr CreateFile(string name, uint access, uint share,
        IntPtr sec, uint disp, uint flags, IntPtr templ);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool DeviceIoControl(IntPtr h, uint code,
        IntPtr inBuf, uint inSize, byte[] outBuf, uint outSize,
        ref uint returned, IntPtr overlapped);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr h);

    public const uint GENERIC_READ  = 0x80000000;
    public const uint GENERIC_WRITE = 0x40000000;
    public const uint FILE_SHARE_READ  = 1;
    public const uint FILE_SHARE_WRITE = 2;
    public const uint OPEN_EXISTING = 3;
    public const uint IOCTL_SCSI_GET_CAPABILITIES = 0x41010;
}
"@
Add-Type -TypeDefinition $sig -ErrorAction Stop

$found = $false
foreach ($n in 0..15) {
    $path = "\\.\Scsi$n`:"
    $h = [ScsiCaps]::CreateFile($path,
            [ScsiCaps]::GENERIC_READ -bor [ScsiCaps]::GENERIC_WRITE,
            [ScsiCaps]::FILE_SHARE_READ -bor [ScsiCaps]::FILE_SHARE_WRITE,
            [IntPtr]::Zero, [ScsiCaps]::OPEN_EXISTING, 0, [IntPtr]::Zero)
    if ($h -eq [IntPtr]::Zero -or $h -eq [IntPtr](-1)) { continue }

    $buf = New-Object byte[] 32
    $ret = 0
    $ok = [ScsiCaps]::DeviceIoControl($h, [ScsiCaps]::IOCTL_SCSI_GET_CAPABILITIES,
              [IntPtr]::Zero, 0, $buf, 32, [ref]$ret, [IntPtr]::Zero)
    [void][ScsiCaps]::CloseHandle($h)
    if (-not $ok) { continue }

    $found = $true
    $maxLen   = [BitConverter]::ToUInt32($buf, 4)
    $maxPages = [BitConverter]::ToUInt32($buf, 8)
    $align    = [BitConverter]::ToUInt32($buf, 16)
    $usesPio  = $buf[22]

    ""
    "=== $path ==="
    "  MaximumTransferLength : {0,10:N0} bytes" -f $maxLen
    "  MaximumPhysicalPages  : {0,10:N0}  (= {1:N0} bytes at 4 KB pages)" -f $maxPages, ($maxPages*4096)
    "  AlignmentMask         : 0x{0:X}" -f $align
    "  AdapterUsesPio        : {0}" -f [bool]$usesPio

    # the effective cap is the smaller of the two
    $eff = [Math]::Min($maxLen, $maxPages * 4096)
    "  effective per-command : {0,10:N0} bytes" -f $eff
    "    -> {0,7:N0} px per line at 16-bit RGB" -f [int]($eff/6)
    "    -> {0,7:N0} px per line at  8-bit RGB" -f [int]($eff/3)

    # What must fit: the SPTD buffer CQscan allocates, = transfer guard + 0x4C.
    # With the release host constants that is 0x3804C = 229,452 bytes.
    $needBytes = 229452
    $needPages = [Math]::Ceiling($needBytes / 4096) + 1     # worst-case misalignment
    $fwMax16   = 35492                                       # firmware ceiling, pool 0x6800

    if ($eff -ge $needBytes -and $maxPages -ge $needPages) {
        "  VERDICT: OK. This adapter covers the full {0:N0} px firmware ceiling." -f $fwMax16
        "           needs {0:N0} bytes / {1} SG entries; you have {2:N0} / {3}." -f `
            $needBytes, $needPages, $eff, $maxPages
    } else {
        "  VERDICT: this adapter is the binding limit."
        "           needs {0:N0} bytes and {1} SG entries for the full {2:N0} px ceiling;" -f `
            $needBytes, $needPages, $fwMax16
        "           you have {0:N0} bytes and {1} entries -> {2:N0} px max at 16-bit." -f `
            $eff, $maxPages, [int]($eff/6)
        ""
        "           aic78xx card:  set MaximumSGList = 65 (see README) and reboot."
        "           other driver:  find its scatter/gather parameter, or lower the"
        "                          CQscan constants to match what your card allows."
    }
}

if (-not $found) {
    ""
    "No SCSI ports responded. Either none are present, or you are not running elevated."
    "Note: 'Scsi0' may be an NVMe/VMD controller rather than your parallel SCSI card -"
    "check each port that reports."
}
