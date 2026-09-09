<#
    Check-CQscanLimits.ps1
    ScanMate 11000 project - host-side transfer limit inspector / patcher

    Reads the two patched constants in CQscan.exe and reports the widest scan they
    allow. With -Apply, raises both to 0x30000 (196,608) after taking a backup.

    Offsets (verified by disassembly, revision 3 findings section 7):
      0x339C0  chunk budget    - imm32 of 'mov ecx, <n>' at 0x339BF
      0x60FE8  transfer guard  - initialiser for [obj+0x4c], checked at VA 0x4556AA

    Usage:
      .\Check-CQscanLimits.ps1
      .\Check-CQscanLimits.ps1 -Path "D:\CQ\CQscan.exe"
      .\Check-CQscanLimits.ps1 -Apply
#>

[CmdletBinding()]
param(
    [string] $Path,
    [switch] $Apply,
    [uint32] $NewLimit = 0x30000
)

$OFF_BUDGET = 0x339C0
$OFF_GUARD  = 0x60FE8
$STOCK_MD5  = 'C7A77CCE07EF2559F36CAB4FC84F6409'

function Find-CQscan {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Esko-Graphics\ColorQuartet Pro 5.2\CQscan.exe",
        "$env:ProgramFiles\Esko-Graphics\ColorQuartet Pro 5.2\CQscan.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $hit = Get-ChildItem -Path 'C:\','D:\' -Filter CQscan.exe -Recurse -File -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return $null
}

if (-not $Path) { $Path = Find-CQscan }
if (-not $Path -or -not (Test-Path $Path)) {
    Write-Host "CQscan.exe not found. Pass it explicitly:" -ForegroundColor Red
    Write-Host '    .\Check-CQscanLimits.ps1 -Path "C:\path\to\CQscan.exe"'
    exit 1
}

Write-Host ""
Write-Host "File : $Path"
$bytes = [IO.File]::ReadAllBytes($Path)
Write-Host ("Size : {0:N0} bytes" -f $bytes.Length)
if ($bytes.Length -ne 2211840) {
    Write-Host "  WARNING: expected 2,211,840 bytes. Offsets may not apply to this build." -ForegroundColor Yellow
}

function Read-U32([byte[]]$b, [int]$o) {
    return [BitConverter]::ToUInt32($b, $o)
}
function Show-Field([string]$name, [int]$off, [uint32]$val) {
    $hex = ($bytes[$off..($off+3)] | ForEach-Object { '{0:X2}' -f $_ }) -join ' '
    "{0,-16} offset 0x{1:X5}  bytes {2}  = {3,9:N0}  (0x{3:X})" -f $name, $off, $hex, $val
}

$budget = Read-U32 $bytes $OFF_BUDGET
$guard  = Read-U32 $bytes $OFF_GUARD

Write-Host ""
Write-Host "Current values" -ForegroundColor Cyan
Write-Host (Show-Field 'chunk budget'   $OFF_BUDGET $budget)
Write-Host (Show-Field 'transfer guard' $OFF_GUARD  $guard)

$effective = [Math]::Min($budget, $guard)
$px16 = [int]($effective / 6)
$px8  = [int]($effective / 3)

Write-Host ""
Write-Host "Widest line the host will accept: {0:N0} bytes" -f $effective
Write-Host ("  16-bit RGB : {0,7:N0} px" -f $px16)
Write-Host ("   8-bit RGB : {0,7:N0} px" -f $px8)

# firmware-side ceiling with the expanded DRAM pool (2 buffers required)
$fw16 = 27300; $fw8 = 54607
Write-Host ""
Write-Host "Firmware ceiling with the expanded pool (needs 2 buffers):" -ForegroundColor Cyan
Write-Host ("  16-bit RGB : {0,7:N0} px" -f $fw16)
Write-Host ("   8-bit RGB : {0,7:N0} px" -f $fw8)

Write-Host ""
if ($px16 -lt $fw16) {
    Write-Host "VERDICT: the HOST is the narrower limit." -ForegroundColor Yellow
    Write-Host ("  You are capped at {0:N0} px in 16-bit instead of {1:N0}." -f $px16, $fw16)
    Write-Host "  Re-run with -Apply to raise both to 0x30000 (196,608)."
} else {
    Write-Host "VERDICT: the host is not limiting you. Firmware ceiling applies." -ForegroundColor Green
}

if (-not $Apply) { Write-Host ""; exit 0 }

# ---------------- apply ----------------
Write-Host ""
Write-Host "Applying..." -ForegroundColor Cyan

$proc = Get-Process -Name CQscan,cq -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "  ColorQuartet is running. Close it first." -ForegroundColor Red
    exit 1
}
if ($NewLimit -gt 266240) {
    Write-Host "  Refusing: $NewLimit exceeds the adapter's 266,240-byte capability." -ForegroundColor Red
    exit 1
}

$backup = "$Path.bak-{0:yyyyMMdd-HHmmss}" -f (Get-Date)
Copy-Item $Path $backup -Force
Write-Host "  Backup: $backup"

$new = [BitConverter]::GetBytes([uint32]$NewLimit)
[Array]::Copy($new, 0, $bytes, $OFF_BUDGET, 4)
[Array]::Copy($new, 0, $bytes, $OFF_GUARD,  4)

try {
    [IO.File]::WriteAllBytes($Path, $bytes)
} catch {
    Write-Host "  WRITE FAILED: $_" -ForegroundColor Red
    Write-Host "  Run PowerShell as Administrator, or check the file is not read-only."
    exit 1
}

# verify by reading back from disk
$check = [IO.File]::ReadAllBytes($Path)
$b2 = Read-U32 $check $OFF_BUDGET
$g2 = Read-U32 $check $OFF_GUARD
Write-Host ""
Write-Host "Verified on disk" -ForegroundColor Cyan
Write-Host (Show-Field 'chunk budget'   $OFF_BUDGET $b2)
Write-Host (Show-Field 'transfer guard' $OFF_GUARD  $g2)

if ($b2 -eq $NewLimit -and $g2 -eq $NewLimit) {
    Write-Host ""
    Write-Host ("DONE. Host now accepts {0:N0} bytes/line -> {1:N0} px at 16-bit." -f $NewLimit, [int]($NewLimit/6)) -ForegroundColor Green
    Write-Host "The firmware ceiling of 27,300 px is now the binding limit."
} else {
    Write-Host ""
    Write-Host "MISMATCH after write. Restore from the backup above." -ForegroundColor Red
    exit 1
}
