# Keeps the relay alive independent of the job runner.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $here)
$env:PYTHONUNBUFFERED = "1"
while ($true) {
    python -u proxy.py *>> relay.log
    "restart $(Get-Date -Format o)" | Out-File -Append -Encoding utf8 relay.log
    Start-Sleep -Seconds 2
}
