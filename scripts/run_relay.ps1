# Keeps the relay alive independent of the job runner.
param(
    [string]$Config,
    [string]$StateDir,
    [string]$Python = "python"
)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $here)
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:NO_COLOR = "1" # This supervisor redirects to a plain file; colorize in watch_relay.py.
$relayArgs = @("-u", "proxy.py")
if ($Config) { $relayArgs += @("--config", $Config) }
if ($StateDir) {
    $null = New-Item -ItemType Directory -Path $StateDir -Force
    $relayArgs += @("--state-dir", $StateDir)
}
$logPath = if ($StateDir) { Join-Path $StateDir "relay.log" } else { "relay.log" }
while ($true) {
    & $Python @relayArgs *>> $logPath
    "restart $(Get-Date -Format o)" | Out-File -FilePath $logPath -Append -Encoding utf8
    Start-Sleep -Seconds 2
}
