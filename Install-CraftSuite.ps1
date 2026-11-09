<#
.SYNOPSIS
  Craft Suite Sync - one-time bootstrap / installer (Windows PowerShell 5.1 and PowerShell 7).

.DESCRIPTION
  Installs the central sync controller into the destination repository. It does NOT mirror the
  17 upstream repositories itself: mirroring is done by the GitHub Actions workflow it installs.

  Creates and pushes exactly two branches (never --force, never --mirror):
    craft-suite-control  manifest, engine, docs, tests, state/catalog (controller source of truth)
    craft                seed commit holding the workflow (the first Actions run replaces it
                         with the real Craft Anything mirror + the same workflow)

  The local folder (-WorkDir, default: current folder) must be EMPTY, or a CLEAN clone of the
  destination repository - the same convention as GitSync v4.7.

.PARAMETER Plan          Read-only: run every check, print what would happen, change nothing.
.PARAMETER AdoptExisting Allow a NON-EMPTY destination repository (see the warning it prints).
.PARAMETER UpdateControl Update the control branch of an already installed repository (non-force).
.PARAMETER SetSecret     Prompt (hidden) for the write PAT and store it as secret GH_SYNC_TOKEN via gh.
.PARAMETER FirstRun      dry-run (default) | sync | none : run the Craft Anything workflow once after installing (needs gh).
.EXAMPLE
  .\Install-CraftSuite.ps1 -Plan
  .\Install-CraftSuite.ps1 -WorkDir D:\Craft-Suite -SetSecret
#>
[CmdletBinding()]
param(
    [string]$Destination,
    [string]$WorkDir = (Get-Location).Path,
    [string]$GitUserName,
    [string]$GitUserEmail,
    [switch]$Plan,
    [switch]$AdoptExisting,
    [switch]$UpdateControl,
    [switch]$SetSecret,
    [switch]$NoDefaultBranch,
    [ValidateSet('dry-run', 'sync', 'none')][string]$FirstRun = 'dry-run',
    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# ----------------------------------------------------------------- output ---
function Write-Step([string]$m) { Write-Host ''; Write-Host "== $m" -ForegroundColor Cyan }
function Write-Info([string]$m) { Write-Host "   $m" }
function Write-Warn([string]$m) { Write-Host "   WARNING: $m" -ForegroundColor Yellow }
function Stop-Install([string]$m) { Write-Host ''; Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# -------------------------------------------------------------- git helpers ---
function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$GitArgs, [string]$Cwd = $script:WorkDirFull)
    Push-Location $Cwd
    try {
        & git @GitArgs
        if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
}

function Get-GitOutput {
    param([Parameter(Mandatory = $true)][string[]]$GitArgs, [string]$Cwd = $script:WorkDirFull)
    Push-Location $Cwd
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git @GitArgs 2>$null
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old; Pop-Location }
    return [pscustomobject]@{ Code = $code; Lines = @($out) }
}

function Get-RemoteRefs([string]$Url) {
    # Runs outside the work dir so it also works before `git init`.
    $r = Get-GitOutput -GitArgs @('ls-remote', '--heads', '--tags', $Url) -Cwd $env:TEMP
    if ($r.Code -ne 0) { Stop-Install "Cannot read $Url (git ls-remote exit $($r.Code)). Check the URL and your GitHub login." }
    return @($r.Lines | Where-Object { $_ })
}

function Normalize-Url([string]$u) { return ($u.Trim().TrimEnd('/') -replace '\.git$', '').ToLowerInvariant() }

function Write-FileLf([string]$Path, [string]$Text) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($Path, ($Text -replace "`r`n", "`n"), $script:Utf8NoBom)
}

function Clear-WorkTree {
    # Remove tracked + untracked content so an orphan branch starts empty.
    $null = Get-GitOutput -GitArgs @('rm', '-rf', '--quiet', '--ignore-unmatch', '.')
    $null = Get-GitOutput -GitArgs @('clean', '-fdxq')
}

function New-Commit([string]$Message) {
    Invoke-Git -GitArgs @('add', '-A')
    Invoke-Git -GitArgs @('-c', "user.name=$($script:GitName)", '-c', "user.email=$($script:GitEmail)", 'commit', '-q', '-m', $Message)
}

# ----------------------------------------------------------------- preflight ---
Write-Step 'Preflight'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Stop-Install 'git is not installed or not on PATH.' }
foreach ($p in @('config\apps.json', 'engine\craft_sync.py', 'workflow\app-sync.yml', 'workflow\sync-all.yml', 'templates\CRAFT-SUITE.md')) {
    if (-not (Test-Path -LiteralPath (Join-Path $script:PackageRoot $p))) { Stop-Install "Package file missing: $p (run this script from the package folder)." }
}
$manifestPath = Join-Path $script:PackageRoot 'config\apps.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$apps = @($manifest.apps)
if ($apps.Count -ne 17) { Stop-Install "Manifest must define exactly 17 apps (found $($apps.Count))." }
$slugs = @($apps | ForEach-Object { $_.slug })
if (($slugs | Select-Object -Unique).Count -ne $slugs.Count) { Stop-Install 'Manifest has duplicate slugs.' }
foreach ($a in $apps) {
    if ($a.slug -notmatch '^[a-z0-9]+(-[a-z0-9]+)*$') { Stop-Install "Invalid slug: $($a.slug)" }
    if ($a.upstream_url -notmatch '^https://') { Stop-Install "upstream_url must be HTTPS: $($a.slug)" }
}
$destRepo = [string]$manifest.destination.repository
$controlBranch = [string]$manifest.destination.control_branch
$defaultBranch = [string]$manifest.destination.default_branch
if ($destRepo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { Stop-Install "Invalid destination.repository: $destRepo" }
if (-not $Destination) { $Destination = "https://github.com/$destRepo.git" }
if ($Destination -notmatch '^https://') { Stop-Install 'Destination must be an HTTPS GitHub URL.' }
$enabledApps = @($apps | Where-Object { $_.enabled -eq $true })

$script:GitName = $GitUserName
$script:GitEmail = $GitUserEmail
if (-not $script:GitName) { $script:GitName = (Get-GitOutput -GitArgs @('config', '--get', 'user.name') -Cwd $env:TEMP).Lines | Select-Object -First 1 }
if (-not $script:GitEmail) { $script:GitEmail = (Get-GitOutput -GitArgs @('config', '--get', 'user.email') -Cwd $env:TEMP).Lines | Select-Object -First 1 }
if (-not $script:GitName -or -not $script:GitEmail) { Stop-Install 'Git identity not set. Pass -GitUserName and -GitUserEmail (a GitHub noreply address is recommended).' }

$script:WorkDirFull = [System.IO.Path]::GetFullPath($WorkDir)
if ($script:WorkDirFull.TrimEnd('\') -eq $script:PackageRoot.TrimEnd('\')) { Stop-Install 'WorkDir must not be the package folder itself.' }
Write-Info "Destination : $Destination"
Write-Info "Work folder : $($script:WorkDirFull)"
Write-Info "Branches    : $defaultBranch (default) + $controlBranch (control)"
Write-Info "Workflows   : $($enabledApps.Count) per-app + 1 'Sync ALL' (manual run only, no schedule)"

# ------------------------------------------------------------ local folder ---
Write-Step 'Local folder'
$isGit = Test-Path -LiteralPath (Join-Path $script:WorkDirFull '.git')
$exists = Test-Path -LiteralPath $script:WorkDirFull
$items = @()
if ($exists) { $items = @(Get-ChildItem -LiteralPath $script:WorkDirFull -Force | Where-Object { $_.Name -ne '.git' }) }
if ($exists -and -not $isGit -and $items.Count -gt 0) {
    Stop-Install 'WorkDir is not empty and is not a git clone. Use an empty folder or a clean clone of the destination.'
}
if ($isGit) {
    $dirty = Get-GitOutput -GitArgs @('status', '--porcelain')
    if ($dirty.Lines.Count -gt 0 -and $dirty.Lines[0]) { Stop-Install 'The local clone has uncommitted changes. Commit/stash them first.' }
    $origin = (Get-GitOutput -GitArgs @('remote', 'get-url', 'origin')).Lines | Select-Object -First 1
    if ($origin -and (Normalize-Url $origin) -ne (Normalize-Url $Destination)) {
        Stop-Install "Local origin ($origin) does not match the destination ($Destination)."
    }
    Write-Info 'Using existing clean clone.'
} else { Write-Info 'Empty folder: a new repository will be initialised.' }

# ----------------------------------------------------------- remote checks ---
Write-Step 'Remote repository'
$refs = @(Get-RemoteRefs $Destination)
$hasControl = @($refs | Where-Object { $_ -match "refs/heads/$([regex]::Escape($controlBranch))$" }).Count -gt 0
$hasDefault = @($refs | Where-Object { $_ -match "refs/heads/$([regex]::Escape($defaultBranch))$" }).Count -gt 0
if ($UpdateControl) {
    if (-not $hasControl) { Stop-Install "-UpdateControl needs an installed repository, but branch $controlBranch does not exist." }
    Write-Info "Mode: UPDATE control branch ($controlBranch), non-force."
} elseif ($refs.Count -eq 0) {
    Write-Info 'Remote is empty. Mode: FRESH INSTALL.'
} else {
    if (-not $AdoptExisting) {
        Stop-Install ("The destination repository is NOT empty ($($refs.Count) refs). Refusing to touch it.`n" +
            "   Create the repository empty (no README/LICENSE/.gitignore), or re-run with -AdoptExisting.")
    }
    if ($hasControl -or $hasDefault) {
        Stop-Install "Branch '$defaultBranch' or '$controlBranch' already exists remotely. Adoption never overwrites them. Rename/delete them yourself first."
    }
    Write-Warn 'ADOPT MODE: the repository already has content.'
    Write-Warn "The sync workflow will later FORCE-UPDATE only these managed refs: the 17 app branches, '$defaultBranch', and tags named '<slug>--*'."
    Write-Warn 'Existing branches/tags with those names would be overwritten by the first real sync.'
    if (-not $Force -and -not $Plan) {
        $ans = Read-Host "Type the repository name ($destRepo) to continue"
        if ($ans -ne $destRepo) { Stop-Install 'Confirmation did not match. Nothing was changed.' }
    }
}

if ($Plan) {
    Write-Step 'PLAN (nothing was changed)'
    if ($UpdateControl) { Write-Info "Would update '$controlBranch' with the package files (state/ preserved) and push without force." }
    else {
        Write-Info "Would create orphan branch '$defaultBranch' (workflow + pointer README) and push it FIRST (so it becomes the default branch)."
        Write-Info "Would create orphan branch '$controlBranch' (config, engine, workflow, templates, docs, tests) and push it."
        Write-Info "Would set the default branch (gh), if gh is available."
    }
    Write-Info "Then: add secret GH_SYNC_TOKEN, and run one app workflow with dry_run=true."
    exit 0
}

# ------------------------------------------------------------ file staging ---
function Copy-PackageTo([string]$Target) {
    $include = @('config', 'engine', 'workflow', 'templates', 'docs', 'tests')
    foreach ($d in $include) {
        $src = Join-Path $script:PackageRoot $d
        if (-not (Test-Path -LiteralPath $src)) { continue }
        Get-ChildItem -LiteralPath $src -Recurse -File -Force |
            Where-Object { $_.FullName -notmatch '__pycache__|\.pyc$' } |
            ForEach-Object {
                $rel = $_.FullName.Substring($script:PackageRoot.TrimEnd('\').Length + 1)
                $dest = Join-Path $Target $rel
                $dir = Split-Path -Parent $dest
                if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                [System.IO.File]::WriteAllBytes($dest, [System.IO.File]::ReadAllBytes($_.FullName))
            }
    }
    foreach ($f in @('Install-CraftSuite.ps1', 'README.md', 'VALIDATION-RESULTS.md')) {
        $p = Join-Path $script:PackageRoot $f
        if (Test-Path -LiteralPath $p) { [System.IO.File]::WriteAllBytes((Join-Path $Target $f), [System.IO.File]::ReadAllBytes($p)) }
    }
    Write-FileLf (Join-Path $Target '.gitignore') "__pycache__/`n*.pyc`n"
}

function Get-RenderedWorkflow($app) {
    $t = [System.IO.File]::ReadAllText((Join-Path $script:PackageRoot 'workflow\app-sync.yml'), $script:Utf8NoBom)
    return $t.Replace('__SLUG__', [string]$app.slug).Replace('__APP_NAME__', [string]$app.name).Replace('__CONTROL_BRANCH__', $controlBranch)
}

function Get-RenderedPointer {
    $t = [System.IO.File]::ReadAllText((Join-Path $script:PackageRoot 'templates\CRAFT-SUITE.md'), $script:Utf8NoBom)
    $rows = ($apps | ForEach-Object { "| $($_.name) | ``$($_.destination_branch)`` | https://github.com/$($_.repository) |" }) -join "`n"
    return $t.Replace('__APP_TABLE__', $rows).Replace('__CONTROL_BRANCH__', $controlBranch).Replace('__REPOSITORY__', $destRepo)
}

# ----------------------------------------------------------------- install ---
if (-not $isGit) {
    New-Item -ItemType Directory -Path $script:WorkDirFull -Force | Out-Null
    Invoke-Git -GitArgs @('init', '-q')
    Invoke-Git -GitArgs @('remote', 'add', 'origin', $Destination)
}
Invoke-Git -GitArgs @('config', 'core.autocrlf', 'false')
Invoke-Git -GitArgs @('config', 'core.longpaths', 'true')

if ($UpdateControl) {
    Write-Step "Updating $controlBranch"
    Invoke-Git -GitArgs @('fetch', 'origin', $controlBranch)
    Invoke-Git -GitArgs @('checkout', '-q', '-B', $controlBranch, "origin/$controlBranch")
    $tracked = (Get-GitOutput -GitArgs @('ls-files')).Lines | Where-Object { $_ }
    foreach ($f in $tracked) {
        if ($f -like 'state/*' -or $f -eq 'CRAFT-SUITE-INDEX.md') { continue }   # generated by the workflow: preserve
        Remove-Item -LiteralPath (Join-Path $script:WorkDirFull $f) -Force -ErrorAction SilentlyContinue
    }
    Copy-PackageTo $script:WorkDirFull
    Invoke-Git -GitArgs @('add', '-A')
    $changed = Get-GitOutput -GitArgs @('status', '--porcelain')
    if ($changed.Lines.Count -eq 0 -or -not $changed.Lines[0]) { Write-Info 'Control branch already up to date.'; exit 0 }
    New-Commit 'Update Craft Suite Sync controller'
    Invoke-Git -GitArgs @('push', 'origin', $controlBranch)      # no --force
    Write-Info 'Updated. The next sync run re-renders the workflow on the default branch automatically.'
    exit 0
}

Write-Step "Creating $defaultBranch (seed)"
Invoke-Git -GitArgs @('checkout', '-q', '--orphan', $defaultBranch)
Clear-WorkTree
foreach ($a in $enabledApps) {
    Write-FileLf (Join-Path $script:WorkDirFull ".github\workflows\sync-$($a.slug).yml") (Get-RenderedWorkflow $a)
}
$allTpl = [System.IO.File]::ReadAllText((Join-Path $script:PackageRoot 'workflow\sync-all.yml'), $script:Utf8NoBom)
Write-FileLf (Join-Path $script:WorkDirFull '.github\workflows\sync-all.yml') ($allTpl.Replace('__CONTROL_BRANCH__', $controlBranch))
Write-FileLf (Join-Path $script:WorkDirFull 'CRAFT-SUITE.md') (Get-RenderedPointer)
New-Commit 'Seed Craft Suite default branch with the per-app sync workflows'
Write-Info 'Pushing the workflow file needs a credential with the "workflow" scope (Git Credential Manager normally has it).'
Invoke-Git -GitArgs @('push', '-u', 'origin', $defaultBranch)   # no --force

Write-Step "Creating $controlBranch"
Invoke-Git -GitArgs @('checkout', '-q', '--orphan', $controlBranch)
Clear-WorkTree
Copy-PackageTo $script:WorkDirFull
New-Commit 'Install Craft Suite Sync controller'
Invoke-Git -GitArgs @('push', 'origin', $controlBranch)          # no --force
Invoke-Git -GitArgs @('checkout', '-q', $defaultBranch)

# --------------------------------------------------------- optional gh steps ---
$gh = Get-Command gh -ErrorAction SilentlyContinue
$ghOk = $false
if ($gh) {
    & gh auth status *> $null
    $ghOk = ($LASTEXITCODE -eq 0)
}
Write-Step 'GitHub settings'
if ($ghOk) {
    if (-not $NoDefaultBranch) {
        & gh api -X PATCH "repos/$destRepo" -f "default_branch=$defaultBranch" *> $null
        if ($LASTEXITCODE -eq 0) { Write-Info "Default branch = $defaultBranch" } else { Write-Warn "Could not set the default branch; set it to '$defaultBranch' in Settings > Branches." }
    }
    if ($SetSecret) {
        $sec = Read-Host 'Paste the fine-grained PAT (Contents: write, Workflows: write; only this repository)' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try {
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            $plain | & gh secret set GH_SYNC_TOKEN --repo $destRepo
            if ($LASTEXITCODE -ne 0) { Write-Warn 'gh secret set failed; add GH_SYNC_TOKEN manually (docs/SETUP.md).' } else { Write-Info 'Secret GH_SYNC_TOKEN stored.' }
        } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr); $plain = $null }
    }
} else {
    Write-Warn 'GitHub CLI (gh) is not installed/logged in: do the manual steps in docs/SETUP.md (default branch, variable, secret).'
}

if ($ghOk -and $FirstRun -ne 'none') {
    Write-Step "First run ($FirstRun)"
    $dry = if ($FirstRun -eq 'dry-run') { 'true' } else { 'false' }
    if ($dry -eq 'false' -and -not $SetSecret) { Write-Warn 'A real sync needs the secret GH_SYNC_TOKEN. If it is missing, every app fails with AUTH_MISSING (nothing is pushed).' }
    $started = $false
    for ($i = 1; $i -le 6 -and -not $started; $i++) {
        Start-Sleep -Seconds 5
        & gh workflow run sync-craft.yml --repo $destRepo --ref $defaultBranch -f dry_run=$dry *> $null
        $started = ($LASTEXITCODE -eq 0)
    }
    if ($started) { Write-Info 'Workflow started. Watch it in the Actions tab.' } else { Write-Warn 'Could not start the workflow yet (GitHub may still be registering it). Start it from the Actions tab.' }
}

Write-Step 'Done'
Write-Info 'Next: (1) ensure secret GH_SYNC_TOKEN exists, (2) Actions > "Sync Craft Anything" > Run workflow (dry_run=true first), (3) then repeat for each app you want.'
Write-Info "Control branch for edits (toggles, cron): $controlBranch -> config/apps.json"
