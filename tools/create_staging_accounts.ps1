# Creates the two STAGING test accounts (consumer + nurse) on a LOCAL development backend.
# Refuses to run against anything except http://localhost:<port> or http://127.0.0.1:<port>,
# and refuses unless /api/health reports env "development". Passwords are read from .env and never printed.
param([string]$Api = "http://localhost:8001")
$ErrorActionPreference = "Stop"

if ($Api -notmatch '^http://(localhost|127\.0\.0\.1):\d+$') { throw "Refusing: this script only talks to a local backend." }
$health = Invoke-RestMethod "$Api/api/health"
if ($health.env -ne "development") { throw "Refusing: backend reports env '$($health.env)', expected 'development'." }

$envFile = Join-Path $PSScriptRoot "..\.env"
function Get-EnvValue([string]$name) {
    $line = Get-Content $envFile | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -First 1
    if (-not $line) { throw "$name is missing in .env" }
    ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
}

function Post([string]$path, $body) {
    try {
        Invoke-RestMethod "$Api/api$path" -Method Post -ContentType "application/json" -Body ($body | ConvertTo-Json -Compress)
    } catch {
        $detail = ""
        try { $detail = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd() } catch {}
        throw "POST $path failed: $detail"
    }
}

function New-Account([string]$name, [string]$email, [string]$phone, [string]$password, [string]$role, [string]$workerType) {
    Write-Host "Creating $email ($role) ..."
    Post "/auth/otp/send" @{ phone_e164 = "+91$phone"; purpose = "signup"; role = $role; force_resend = $true } | Out-Null
    Post "/auth/otp/verify-signup" @{ phone_e164 = "+91$phone"; code = "123456"; role = $role } | Out-Null
    $body = @{ phone_e164 = "+91$phone"; full_name = $name; email = $email; password = $password; role = $role }
    if ($workerType) { $body.worker_type = $workerType }
    $r = Post "/auth/register" $body
    $code = "654321"
    if ($r.dev_verification_code) { $code = [string]$r.dev_verification_code }
    Post "/auth/verify-email" @{ email = $email; code = $code } | Out-Null
    Write-Host "  done"
}

New-Account "NC Test Consumer" (Get-EnvValue "NC_TEST_CONSUMER_EMAIL") "9999900001" (Get-EnvValue "NC_TEST_CONSUMER_PASSWORD") "consumer" ""
New-Account "NC Test Nurse"    (Get-EnvValue "NC_TEST_WORKER_EMAIL")   "9999900002" (Get-EnvValue "NC_TEST_WORKER_PASSWORD")   "worker"   "nurse"
Write-Host "Both staging accounts created on $Api."
