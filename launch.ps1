# Launcher used by the Explorer context menu and Send To shortcut.
# Explorer starts one process per selected file; only the first instance
# for a given selection actually runs the extraction.

param(
    [string]$Clicked = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Remaining = @()
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $Root "read_raw_metadata.py"

function Get-RelatedSelection([string]$ClickedPath) {
    if ([string]::IsNullOrWhiteSpace($ClickedPath)) {
        return @()
    }
    $normalized = $ClickedPath.TrimEnd('\')
    try {
        $shell = New-Object -ComObject Shell.Application
        foreach ($window in $shell.Windows()) {
            try {
                if ($null -eq $window.Document) { continue }
                $items = $window.Document.SelectedItems()
                $paths = @()
                $hasClicked = $false
                for ($i = 0; $i -lt $items.Count; $i++) {
                    $itemPath = [string]$items.Item($i).Path
                    if ([string]::IsNullOrWhiteSpace($itemPath)) { continue }
                    $paths += $itemPath
                    if ($itemPath.TrimEnd('\') -ieq $normalized) {
                        $hasClicked = $true
                    }
                }
                if ($hasClicked -and $paths.Count -gt 0) {
                    return $paths
                }
            } catch {
                continue
            }
        }
    } catch {
        # Fall back to the clicked path only.
    }
    return @($ClickedPath)
}

function Get-SelectionKey([string[]]$Paths) {
    $normalized = $Paths | ForEach-Object { $_.TrimEnd('\').ToLowerInvariant() } | Sort-Object -Unique
    $joined = [string]::Join("|", @($normalized))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
        return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "")
    } finally {
        $sha.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $Python)) { exit 1 }
if (-not (Test-Path -LiteralPath $Script)) { exit 1 }

$targets = @()
if ($Remaining -and $Remaining.Count -gt 0) {
    $targets += $Remaining
}
if ($Clicked) {
    $related = Get-RelatedSelection $Clicked
    if ($related.Count -gt 0) {
        $targets += $related
    } else {
        $targets += $Clicked
    }
}

$targets = @(
    $targets |
        Where-Object { $_ } |
        ForEach-Object { $_.Trim('"') } |
        Select-Object -Unique
)
if ($targets.Count -eq 0) { exit 1 }

$mutexName = "Local\ReadRaw_" + (Get-SelectionKey $targets)
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $owned = $true
}
if (-not $owned) {
    exit 0
}

try {
    $arguments = @($Script) + $targets
    & $Python @arguments
    exit $LASTEXITCODE
} finally {
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
