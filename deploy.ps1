#requires -Version 5.1
<#
.SYNOPSIS
  SyncBot root deploy launcher for Windows (PowerShell).

  Verifies a bash environment (Git Bash or WSL), then runs the root deploy.sh
  in bash — same contract as ./deploy.sh on macOS/Linux.

  All arguments are passed through to deploy.sh, including --env, --bootstrap, and --setup-github.
  CLOUD_PROVIDER in .env.deploy.<stage> selects AWS vs GCP.

  Provider-specific prerequisite checks live in infra/<provider>/scripts/deploy.sh
  (sourcing repo-root deploy.sh for shared helpers). There are no deploy.ps1 files under infra/.

.EXAMPLE
  .\deploy.ps1
  .\deploy.ps1 --env test
  .\deploy.ps1 --env prod --setup-github
  .\deploy.ps1 --env test --bootstrap
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $AllArgs
)

$ErrorActionPreference = "Stop"

function Find-GitBash {
    $cmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "${env:ProgramFiles}\Git\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
        "${env:LocalAppData}\Programs\Git\bin\bash.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Test-WslBashWorks {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return $false }
    try {
        $null = & wsl.exe -e bash -c "echo wsl_ok" 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Convert-WindowsPathToWsl {
    param([string] $WindowsPath)
    $full = (Resolve-Path -LiteralPath $WindowsPath).Path
    if ($full -match '^([A-Za-z]):[\\/](.*)$') {
        $drive = $Matches[1].ToLowerInvariant()
        $tail = $Matches[2] -replace '\\', '/'
        return "/mnt/$drive/$tail"
    }
    throw "Cannot map path to WSL (expected C:\...): $WindowsPath"
}

function Find-DeployBash {
    $gitBash = Find-GitBash
    if ($gitBash) {
        return [pscustomobject]@{ Kind = 'GitBash'; Executable = $gitBash }
    }
    if (Test-WslBashWorks) {
        return [pscustomobject]@{ Kind = 'Wsl'; Executable = 'wsl.exe' }
    }
    $bashCmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($bashCmd) {
        return [pscustomobject]@{ Kind = 'Path'; Executable = $bashCmd.Source }
    }
    return $null
}

function Show-WindowsPrereqStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string] $RepoRoot,
        [Parameter(Mandatory = $true)]
        $BashInfo
    )
    Write-Host ""
    Write-Host "=== SyncBot Deploy (Windows) ==="
    Write-Host "Repository: $RepoRoot"
    Write-Host ""
    Write-Host "Bash environment:"
    switch ($BashInfo.Kind) {
        'GitBash' {
            Write-Host "  Git Bash: $($BashInfo.Executable)" -ForegroundColor Green
            if (Test-WslBashWorks) {
                Write-Host "  WSL:      available (not used; Git Bash preferred)" -ForegroundColor DarkGray
            } else {
                Write-Host "  WSL:      not found or not ready" -ForegroundColor DarkGray
            }
        }
        'Wsl' {
            Write-Host "  Git Bash: not found" -ForegroundColor DarkGray
            Write-Host "  WSL:      bash (will run deploy.sh with Windows paths mapped to /mnt/...)" -ForegroundColor Green
        }
        'Path' {
            Write-Host "  bash:     $($BashInfo.Executable)" -ForegroundColor Green
        }
    }
    Write-Host ""
}

function Invoke-DeploySh {
    param(
        [Parameter(Mandatory = $true)]
        $BashInfo,
        [Parameter(Mandatory = $true)]
        [string] $ScriptPath,
        [string[]] $BashArgs
    )
    $extra = if ($null -ne $BashArgs -and $BashArgs.Count -gt 0) { @($BashArgs) } else { @() }
    if ($BashInfo.Kind -eq 'Wsl') {
        $wslPath = Convert-WindowsPathToWsl -WindowsPath $ScriptPath
        & wsl.exe -e bash $wslPath @extra
    } else {
        & $BashInfo.Executable $ScriptPath @extra
    }
}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($AllArgs -and ($AllArgs[0] -in @("-h", "--help", "help"))) {
    @"
Usage: .\deploy.ps1 [--env <stage>] [--bootstrap] [--setup-github] [--verbose] [--update-stack]

All arguments are passed through to ./deploy.sh.
Platform is CLOUD_PROVIDER in .env.deploy.<stage>.
Run .\deploy.ps1 --help for full usage from the bash script.
"@
    exit 0
}

$bashInfo = Find-DeployBash
if (-not $bashInfo) {
    Write-Host @"
Error: no bash found. Install one of:

  * Git for Windows (Git Bash): https://git-scm.com/download/win
  * WSL (Windows Subsystem for Linux): https://learn.microsoft.com/windows/wsl/install

Then re-run: .\deploy.ps1
"@ -ForegroundColor Red
    exit 1
}

Show-WindowsPrereqStatus -RepoRoot $RepoRoot -BashInfo $bashInfo

$deploySh = Join-Path $RepoRoot "deploy.sh"
if (-not (Test-Path -LiteralPath $deploySh)) {
    Write-Error "deploy.sh not found at $deploySh"
    exit 1
}

$extra = if ($null -ne $AllArgs -and $AllArgs.Count -gt 0) { @($AllArgs) } else { @() }
Write-Host "Running: deploy.sh $($extra -join ' ')"
Invoke-DeploySh -BashInfo $bashInfo -ScriptPath $deploySh -BashArgs $extra
exit $LASTEXITCODE
