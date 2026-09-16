$ErrorActionPreference = "SilentlyContinue"
$keys = @(
    "HKCU:\Software\Classes\SystemFileAssociations\.raw\shell\ReadRawMetadata",
    "HKCU:\Software\Classes\.raw\shell\ReadRawMetadata",
    "HKCU:\Software\Classes\raw_auto_file\shell\ReadRawMetadata",
    "HKCU:\Software\Classes\Directory\shell\ReadRawMetadata",
    "HKCU:\Software\Classes\Directory\Background\shell\ReadRawMetadata"
)
foreach ($key in $keys) {
    if (Test-Path $key) {
        Remove-Item -Path $key -Recurse -Force
        Write-Host "Removed $key"
    }
}
$sendTo = Join-Path ([Environment]::GetFolderPath("SendTo")) "Extract RAW metadata.lnk"
if (Test-Path -LiteralPath $sendTo) {
    Remove-Item -LiteralPath $sendTo -Force
    Write-Host "Removed Send To shortcut"
}
Write-Host "Context menu entries removed. The project folder itself was left in place."
Write-Host ""
Write-Host "Press Enter to close."
[void][System.Console]::ReadLine()
