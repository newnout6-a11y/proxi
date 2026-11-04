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
$env:PYTHONIOENCODING = "utf-8"
$env:NO_COLOR = "1" # This supervisor redirects to a plain file; colorize in watch_relay.py.
# Windows PowerShell 5.1 decodes native output with this encoding and `*>>` would
# re-encode it as UTF-16LE with a BOM, so relay.log is written explicitly as UTF-8.
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$utf8 = [Text.UTF8Encoding]::new($false)
$relayArgs = @("-u", "proxy.py")
if ($Config) { $relayArgs += @("--config", $Config) }
if ($StateDir) {
    $null = New-Item -ItemType Directory -Path $StateDir -Force
    $relayArgs += @("--state-dir", $StateDir)
}
$logPath = if ($StateDir) { Join-Path $StateDir "relay.log" } else { "relay.log" }
# .NET resolves relative paths against the process directory, not Set-Location.
$logPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($logPath)
while ($true) {
    & $Python @relayArgs 2>&1 | ForEach-Object { [IO.File]::AppendAllText($logPath, "$_`r`n", $utf8) }
    [IO.File]::AppendAllText($logPath, "restart $(Get-Date -Format o)`r`n", $utf8)
    Start-Sleep -Seconds 2
}
