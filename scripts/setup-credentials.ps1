<#
.SYNOPSIS
  One-stop credential setup for Protocol Intelligence. Safe to re-run any time
  you need to add or rotate a credential.

.DESCRIPTION
  - Installs AWS CLI v2 if it is missing.
  - Creates/updates the AWS profile (access keys or IAM Identity Center/SSO).
  - Stores the Bedrock API key (and any other secret) in the gitignored .env.
  - Runs scripts/check_access.py to verify everything.
  Secrets are read with hidden input and are never echoed or written to the repo.

.EXAMPLE
  .\scripts\setup-credentials.ps1                      # full setup, browser console sign-in (aws login)
  .\scripts\setup-credentials.ps1 -Method Sso          # IAM Identity Center
  .\scripts\setup-credentials.ps1 -Method Keys         # long-lived access keys (least preferred)
  .\scripts\setup-credentials.ps1 -Only Bedrock        # just add/rotate the Bedrock API key
  .\scripts\setup-credentials.ps1 -SetVar S3_BUCKET    # store any other .env value
#>
param(
    [string]$ProfileName = "protocol-dev",
    [string]$Region = "us-east-1",
    [ValidateSet("Login", "Sso", "Keys")]
    [string]$Method = "Login",
    [ValidateSet("All", "Aws", "Bedrock", "Check")]
    [string]$Only = "All",
    [string]$SetVar
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Set-EnvValue([string]$Name, [string]$Value) {
    if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Root ".env.example") $EnvFile }
    $lines = [System.Collections.Generic.List[string]](Get-Content $EnvFile)
    $idx = $lines.FindIndex({ param($l) $l -match "^$([regex]::Escape($Name))=" })
    if ($idx -ge 0) { $lines[$idx] = "$Name=$Value" } else { $lines.Add("$Name=$Value") }
    [IO.File]::WriteAllLines($EnvFile, $lines, (New-Object Text.UTF8Encoding($false)))
    Write-Host "  saved $Name to .env" -ForegroundColor Green
}

function Ensure-AwsCli {
    if (Get-Command aws -ErrorAction SilentlyContinue) {
        Write-Host "AWS CLI: $(aws --version)" -ForegroundColor Green; return
    }
    Write-Host "Installing AWS CLI v2 (official MSI)..." -ForegroundColor Yellow
    Start-Process msiexec.exe -Wait -ArgumentList "/i https://awscli.amazonaws.com/AWSCLIV2.msi /qb"
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    aws --version
}

function Configure-Aws {
    Write-Host "`n== AWS profile '$ProfileName' ($Region) ==" -ForegroundColor Cyan
    aws configure set region $Region --profile $ProfileName
    if ($Method -eq "Login") {
        # Short-lived credentials from your AWS console session; auto-refreshed by the CLI/boto3.
        aws login --profile $ProfileName
    } elseif ($Method -eq "Sso") {
        aws configure sso --profile $ProfileName
        aws sso login --profile $ProfileName
    } else {
        Write-Host "Use the access key of the IAM user created for this project (not root)."
        $keyId = Read-Host "AWS Access Key ID (blank = keep current)"
        if ($keyId) {
            $secret = Read-Secret "AWS Secret Access Key"
            aws configure set aws_access_key_id $keyId --profile $ProfileName
            aws configure set aws_secret_access_key $secret --profile $ProfileName
            $secret = $null
        }
    }
    aws configure set region $Region --profile $ProfileName
    aws configure set output json --profile $ProfileName
    Set-EnvValue "AWS_PROFILE" $ProfileName
    Set-EnvValue "AWS_REGION" $Region
    aws sts get-caller-identity --profile $ProfileName
}

function Configure-Bedrock {
    Write-Host "`n== Bedrock API key (optional) ==" -ForegroundColor Cyan
    Write-Host "Create one in the Bedrock console > API keys. Leave blank to rely on IAM credentials."
    $key = Read-Secret "Bedrock API key (blank = skip)"
    if ($key) { Set-EnvValue "AWS_BEARER_TOKEN_BEDROCK" $key }
    $key = $null
}

if ($SetVar) {
    $val = Read-Secret "Value for $SetVar"
    Set-EnvValue $SetVar $val
    exit 0
}

if ($Only -in "All", "Aws") { Ensure-AwsCli; Configure-Aws }
if ($Only -in "All", "Bedrock") { Configure-Bedrock }

Write-Host "`n== Verifying access ==" -ForegroundColor Cyan
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py (Join-Path $Root "scripts\check_access.py")
