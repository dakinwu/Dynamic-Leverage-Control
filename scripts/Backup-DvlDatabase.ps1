param(
    [string]$Container = 'dynamic-leverage-control-postgres-1',
    [switch]$VerifyRestore,
    [switch]$RegisterDailyTask
)
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backupDir = Join-Path $workspace 'data\private\backups'
[IO.Directory]::CreateDirectory($backupDir) | Out-Null
Add-Type -AssemblyName System.Security

function Invoke-DockerBytes {
    param([string[]]$Arguments, [byte[]]$InputBytes)
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'docker.exe'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    # All arguments are local fixed names or a validated generated database name.
    $psi.Arguments = ($Arguments -join ' ')
    $process = [Diagnostics.Process]::Start($psi)
    $errors = $process.StandardError.ReadToEndAsync()
    $memory = [IO.MemoryStream]::new()
    $copy = $process.StandardOutput.BaseStream.CopyToAsync($memory)
    if ($InputBytes) { $process.StandardInput.BaseStream.Write($InputBytes, 0, $InputBytes.Length) }
    $process.StandardInput.Close()
    [void]$copy.GetAwaiter().GetResult()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Docker database operation failed: $($errors.GetAwaiter().GetResult())" }
    return ,$memory.ToArray()
}

if ($Container -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid container name' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$dump = Invoke-DockerBytes -Arguments @('exec', $Container, 'pg_dump', '-U', 'dynamic', '-d', 'dynamic_leverage', '-Fc')
if ($dump.Length -lt 100) { throw 'Database dump unexpectedly empty' }
$encrypted = [Security.Cryptography.ProtectedData]::Protect($dump, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
$backupPath = Join-Path $backupDir "database-$stamp.dpapi"
[IO.File]::WriteAllBytes($backupPath, $encrypted)
$plain = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($backupPath), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
$sha = [Security.Cryptography.SHA256]::Create()
$originalHash = [BitConverter]::ToString($sha.ComputeHash($dump)).Replace('-', '').ToLowerInvariant()
if ([BitConverter]::ToString($sha.ComputeHash($plain)).Replace('-', '').ToLowerInvariant() -ne $originalHash) { throw 'Encrypted round-trip mismatch' }
$lastRestore = Join-Path $backupDir 'last-restore.json'
$due = -not (Test-Path -LiteralPath $lastRestore)
if (-not $due) {
    $last = Get-Content -LiteralPath $lastRestore -Raw | ConvertFrom-Json
    $due = ((Get-Date).ToUniversalTime() - [datetime]$last.verified_at).TotalDays -ge 30
}
$report = [ordered]@{ backup_file = $backupPath; created_at = (Get-Date).ToUniversalTime().ToString('o'); encryption = 'Windows DPAPI CurrentUser'; bytes = $encrypted.Length; plaintext_sha256 = $originalHash; restore_verified = $false }
if ($VerifyRestore -or $due) {
    $restoreDb = 'dvl_restore_' + [guid]::NewGuid().ToString('N')
    if ($restoreDb -notmatch '^dvl_restore_[0-9a-f]{32}$') { throw 'Invalid isolated restore target' }
    Invoke-DockerBytes -Arguments @('exec', $Container, 'createdb', '-U', 'dynamic', $restoreDb) | Out-Null
    try {
        Invoke-DockerBytes -Arguments @('exec', '-i', $Container, 'pg_restore', '-U', 'dynamic', '--exit-on-error', '-d', $restoreDb) -InputBytes $plain | Out-Null
        $sql = [Text.Encoding]::UTF8.GetBytes('SELECT count(*) FROM raw_snapshots; SELECT version_num FROM alembic_version;')
        $result = Invoke-DockerBytes -Arguments @('exec', '-i', $Container, 'psql', '-U', 'dynamic', '-d', $restoreDb, '-At', '-v', 'ON_ERROR_STOP=1') -InputBytes $sql
        $report.restore_verified = $true
        $report.restore_evidence = [Text.Encoding]::UTF8.GetString($result).Trim()
        @{ verified_at = (Get-Date).ToUniversalTime().ToString('o'); backup_file = $backupPath } | ConvertTo-Json | Set-Content -LiteralPath $lastRestore -Encoding UTF8
    } finally {
        # Only this newly created, validated disposable database may be dropped.
        Invoke-DockerBytes -Arguments @('exec', $Container, 'dropdb', '-U', 'dynamic', $restoreDb) | Out-Null
    }
}
$report | ConvertTo-Json | Set-Content -LiteralPath "$backupPath.json" -Encoding UTF8
if ($RegisterDailyTask) {
    $scriptPath = Join-Path $PSScriptRoot 'Backup-DvlDatabase.ps1'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$scriptPath`""
    $trigger = New-ScheduledTaskTrigger -Daily -At '19:00'
    $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'DVL encrypted database backup' -Action $action -Trigger $trigger -Settings $taskSettings -Description 'Daily user-bound encrypted backup; isolated restore every 30 days. Docker and signed-in user required.' -Force | Out-Null
}
$report | ConvertTo-Json
