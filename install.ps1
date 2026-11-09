# One-time setup: virtualenv, pythonnet, Thermo RawFileReader DLLs,
# Explorer context menu, and a Send To shortcut.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# pythonnet supports Python 3.10 to 3.15.
$MinorVersions = @(12, 13, 11, 14, 10, 15)

# Runs a candidate and returns its real executable path if it is a supported Python.
# The Microsoft Store "python" stub fails to run, so it is rejected here.
function Test-Python([string]$Exe, [string[]]$Prefix = @()) {
    $check = "import sys; print(sys.executable if (3, 10) <= sys.version_info[:2] <= (3, 15) else '')"
    try {
        $out = & $Exe @Prefix -c $check 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ([string]$out).Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    } catch {}
    return $null
}

function Find-Python {
    $ErrorActionPreference = "Continue"
    $found = @()
    foreach ($minor in $MinorVersions) {
        foreach ($dir in @(
            "$env:LocalAppData\Programs\Python\Python3$minor",
            "$env:ProgramFiles\Python3$minor",
            "$env:LocalAppData\Python\pythoncore-3.$minor-64"
        )) {
            $exe = Join-Path $dir "python.exe"
            if (Test-Path -LiteralPath $exe) { $found += Test-Python $exe }
        }
        if (Get-Command py -ErrorAction SilentlyContinue) { $found += Test-Python "py" @("-3.$minor") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { $found += Test-Python "python" }

    $found = @($found | Where-Object { $_ })
    # Prefer a regular install; Microsoft Store / install-manager Pythons also work.
    $regular = @($found | Where-Object { $_ -notmatch "WindowsApps" })
    if ($regular.Count -gt 0) { return $regular[0] }
    if ($found.Count -gt 0) { return $found[0] }
    throw "Python 3.10 to 3.15 was not found. Install Python from https://www.python.org/ and tick 'Add python.exe to PATH'."
}

Write-Host "=== ReadRaw setup ==="
$python = Find-Python
Write-Host "Using $python"

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating virtual environment..."
    & $python -m venv (Join-Path $Root ".venv")
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $Root "requirements.txt")
& $venvPython (Join-Path $Root "read_raw_metadata.py") --setup

$launch = Join-Path $Root "launch.ps1"
$command = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launch`" -Clicked `"%1`""
$backgroundCommand = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launch`" -Clicked `"%V`""

function Install-Verb([string]$KeyPath, [string]$CommandLine) {
    New-Item -Path $KeyPath -Force | Out-Null
    New-ItemProperty -Path $KeyPath -Name "MUIVerb" -Value "Extract RAW metadata" -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $KeyPath -Name "Icon" -Value "imageres.dll,-102" -PropertyType String -Force | Out-Null
    $cmdKey = Join-Path $KeyPath "command"
    New-Item -Path $cmdKey -Force | Out-Null
    Set-ItemProperty -Path $cmdKey -Name "(default)" -Value $CommandLine
}

Write-Host "Registering Explorer context menu (current user)..."
Install-Verb "HKCU:\Software\Classes\SystemFileAssociations\.raw\shell\ReadRawMetadata" $command
Install-Verb "HKCU:\Software\Classes\.raw\shell\ReadRawMetadata" $command
Install-Verb "HKCU:\Software\Classes\raw_auto_file\shell\ReadRawMetadata" $command
Install-Verb "HKCU:\Software\Classes\Directory\shell\ReadRawMetadata" $command
Install-Verb "HKCU:\Software\Classes\Directory\Background\shell\ReadRawMetadata" $backgroundCommand

$sendTo = Join-Path ([Environment]::GetFolderPath("SendTo")) "Extract RAW metadata.lnk"
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($sendTo)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launch`""
$shortcut.WorkingDirectory = $Root
$shortcut.WindowStyle = 7
$shortcut.Description = "Extract Thermo RAW metadata"
$shortcut.Save()

Write-Host ""
Write-Host "Setup complete."
Write-Host "Right-click a .raw file, several .raw files, or a folder and choose 'Extract RAW metadata'."
Write-Host "You can also use Send to > Extract RAW metadata."
Write-Host ""
Write-Host "Press Enter to close."
[void][System.Console]::ReadLine()
