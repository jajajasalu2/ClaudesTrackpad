# Creates the "Claude's Trackpad" Start menu entry. Called by build.bat.
param([Parameter(Mandatory = $true)][string]$Target)

$programs = [Environment]::GetFolderPath('Programs')
$lnk = Join-Path $programs "Claude's Trackpad.lnk"
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$shortcut.TargetPath = $Target
$shortcut.WorkingDirectory = Split-Path $Target
$shortcut.IconLocation = "$Target,0"
$shortcut.Description = 'Use your phone as a trackpad and keyboard'
$shortcut.Save()
Write-Host "Start menu entry created: $lnk"
