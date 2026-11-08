<#
.SYNOPSIS
    Session-10 mutation-testing harness: mutmut over the four scoped modules.

.DESCRIPTION
    Runs `mutmut` against exactly four targets and exits non-zero when the kill
    rate is below -Threshold (default 90 percent) among the mutants a test can
    reach:

        app/generation/rag/retrieval/fusion.py
        app/generation/rag/retrieval/pipeline.py
        app/generation/rag/retrieval/reranker.py
        app/embedding_pipeline/store/repository.py   (the new lexical function)

    The full run output and the survivor list are written to
    var/mutation_s10_report.txt (gitignored), so the run itself can be read
    after the fact; the console gets a one-screen summary.

    This is a manual/CI gate. It is deliberately NOT a pytest test: a full mutmut
    run re-runs the suite once per mutant and must never be collected by
    `uv run pytest -q`.

.PARAMETER Threshold
    Minimum killed-mutant percentage (0-100) **of the mapped mutants**, i.e. those
    at least one selected test reaches. Mutants no test covers are reported, never
    counted; below the threshold the script exits 5.

.PARAMETER DryRun
    Check the tool and configuration, print the exact commands, run nothing.

.NOTES
    THE ONE MANUAL STEP (the script never edits pyproject.toml or installs
    anything silently):

        uv add --dev "mutmut==3.8.1"

    and, in the same pyproject.toml edit, the mutmut 3.x configuration block:

        [tool.mutmut]
        paths_to_mutate = [
            "app/generation/rag/retrieval/fusion.py",
            "app/generation/rag/retrieval/pipeline.py",
            "app/generation/rag/retrieval/reranker.py",
            "app/embedding_pipeline/store/repository.py",
        ]
        tests_dir = "tests"
        also_copy = ["alembic.ini", "alembic"]

    mutmut 3.x reads its configuration from pyproject.toml, not from the command
    line. When the installed CLI *does* expose `--paths-to-mutate` this script
    passes the four files explicitly and runs one mutmut pass per file (so the
    run cannot silently widen to a whole tree); otherwise it verifies that the
    block above is present and names all four targets. `also_copy` matters
    because pytest imports `alembic.ini`/`alembic` in the Postgres tests.

    Scope caveat: mutmut mutates a FILE, not a function. The fourth target is
    therefore the whole `store/repository.py`; its mutants outside
    `search_lexical` are counted too, and every survivor is printed with its
    mutant id so the lexical ones can be told apart.

    Usage:
        powershell -File scripts/mutation_s10.ps1
        powershell -File scripts/mutation_s10.ps1 -Threshold 95

    Exit codes:
        0  kill rate at or above the threshold
        2  mutmut is not installed (the install hint was printed)
        3  a target is missing, or the mutmut config does not name all targets
        4  mutmut produced no parsable results
        5  kill rate below the threshold
#>

[CmdletBinding()]
param(
    [ValidateRange(0, 100)]
    [int]$Threshold = 90,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ReportPath = Join-Path $RepoRoot 'var/mutation_s10_report.txt'
$WorkDir = Join-Path $RepoRoot 'var/mutation_s10'

$Targets = @(
    'app/generation/rag/retrieval/fusion.py',
    'app/generation/rag/retrieval/pipeline.py',
    'app/generation/rag/retrieval/reranker.py',
    'app/embedding_pipeline/store/repository.py'
)

$ConfigBlock = @'
[tool.mutmut]
paths_to_mutate = [
    "app/generation/rag/retrieval/fusion.py",
    "app/generation/rag/retrieval/pipeline.py",
    "app/generation/rag/retrieval/reranker.py",
    "app/embedding_pipeline/store/repository.py",
]
tests_dir = "tests"
also_copy = ["alembic.ini", "alembic"]
'@

function Write-Report {
    param([string[]]$Lines, [switch]$Reset)

    if ($Reset -and (Test-Path $ReportPath)) {
        Remove-Item $ReportPath -Force
    }
    if (-not $Lines -or $Lines.Count -eq 0) {
        return
    }
    if (-not (Test-Path (Split-Path -Parent $ReportPath))) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $ReportPath) -Force | Out-Null
    }
    Add-Content -Path $ReportPath -Value $Lines -Encoding utf8
}

function Invoke-Mutmut {
    param([string[]]$MutmutArguments, [string]$LogPath)

    # Native commands write to stderr routinely (progress, warnings); with
    # $ErrorActionPreference = 'Stop' PowerShell 5.1 turns that into a
    # terminating NativeCommandError *before* the exit code is read. Scope a
    # Continue preference to this one call so only the real exit code decides.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # *> merges every stream so a mutmut progress bar cannot mask the exit code.
        & uv run mutmut @MutmutArguments *> $LogPath
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return $code
}

function Get-MutationCounts {
    param([string]$XmlPath)

    $result = [pscustomobject]@{
        Tests    = 0
        Killed   = 0
        Survived = 0
        Skipped  = 0
        Survivors = @()
    }

    if (-not (Test-Path $XmlPath)) { return $result }
    if ((Get-Item $XmlPath).Length -eq 0) { return $result }

    try {
        [xml]$document = Get-Content -Path $XmlPath -Raw
    }
    catch {
        return $result
    }

    $testcases = @($document.SelectNodes('//testcase'))
    if ($testcases.Count -eq 0) { return $result }

    $survivors = New-Object System.Collections.Generic.List[string]
    $survived = 0
    $skipped = 0

    foreach ($case in $testcases) {
        $failed = $case.SelectSingleNode('failure') -or $case.SelectSingleNode('error')
        $wasSkipped = $case.SelectSingleNode('skipped')

        if ($failed) {
            $survived++
            $file = $case.GetAttribute('classname')
            $name = $case.GetAttribute('name')
            $mutantId = $case.GetAttribute('mutant')
            if ([string]::IsNullOrWhiteSpace($mutantId)) { $mutantId = $name }
            $survivors.Add("$file :: $mutantId")
        }
        elseif ($wasSkipped) {
            # mutmut marks mutants no test covers as skipped; they are not kills.
            $skipped++
            $file = $case.GetAttribute('classname')
            $name = $case.GetAttribute('name')
            $survivors.Add("$file :: $name (no test covered it)")
        }
    }

    $result.Tests = $testcases.Count
    $result.Survived = $survived
    $result.Skipped = $skipped
    $result.Killed = $testcases.Count - $survived - $skipped
    $result.Survivors = $survivors.ToArray()
    return $result
}

Push-Location $RepoRoot
try {
    foreach ($target in $Targets) {
        if (-not (Test-Path (Join-Path $RepoRoot $target))) {
            Write-Host "FAIL: mutation target is missing: $target" -ForegroundColor Red
            exit 3
        }
    }

    if (-not (Test-Path $WorkDir)) {
        New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
    }

    # --- 1. mutmut must be installed. Never install it from here. -----------
    $versionOutput = ''
    # See Invoke-Mutmut: a native command's stderr must not become terminating.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $versionOutput = (& uv run mutmut --version 2>&1 | Out-String).Trim()
        $versionExit = $LASTEXITCODE
    }
    catch {
        $versionExit = 1
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    if ($versionExit -ne 0) {
        # Distinguish "not installed" from "installed but refuses here": mutmut 3.x
        # exits non-zero on native Windows with a platform message, and reporting
        # that as "not available" sends the reader to install something they
        # already have.
        if ($versionOutput -match 'WSL') {
            Write-Host 'mutmut is installed but refuses to run on native Windows.' -ForegroundColor Yellow
            Write-Host 'Its own message:' -ForegroundColor Yellow
            Write-Host "    $versionOutput" -ForegroundColor Yellow
            Write-Host ''
            Write-Host 'Run this harness from WSL or a Linux CI job instead:'
            Write-Host '    wsl -- bash -lc "cd /mnt/c/... && uv run mutmut run"'
            Write-Host 'or, with the compose stack up, inside the Linux api container:'
            Write-Host '    docker compose exec api sh -c "cd /srv/app && uv run mutmut run"'
            Write-Host ''
            Write-Host 'Neither run is claimed as evidence until it produces a report file.'
            exit 2
        }
        Write-Host 'mutmut is not installed in this environment.' -ForegroundColor Red
        Write-Host 'Install it (do not skip this - the harness never does it for you):'
        Write-Host '    uv add --dev "mutmut==3.8.1"'
        Write-Host ''
        Write-Host 'and add exactly this block to pyproject.toml (mutmut 3.x reads it from there):'
        Write-Host ''
        Write-Host $ConfigBlock
        exit 2
    }
    Write-Host "mutmut: $versionOutput"

    # --- 2. Decide how to scope the four targets. ---------------------------
    $helpOutput = ''
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $helpOutput = (& uv run mutmut run --help 2>&1 | Out-String)
    }
    catch {
        # An unreadable --help means "cannot confirm the flag"; fall through to
        # the pyproject.toml check rather than aborting the whole run.
        $helpOutput = ''
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    $cliSupportsPaths = $helpOutput -match '--paths-to-mutate'

    if (-not $cliSupportsPaths) {
        $pyproject = Get-Content -Path (Join-Path $RepoRoot 'pyproject.toml') -Raw
        $missing = @()
        if ($pyproject -notmatch '(?m)^\[tool\.mutmut\]') {
            $missing += 'the [tool.mutmut] section'
        }
        foreach ($target in $Targets) {
            $leaf = Split-Path $target -Leaf
            if ($pyproject -notmatch [regex]::Escape($leaf)) {
                $missing += $leaf
            }
        }
        if ($missing.Count -gt 0) {
            Write-Host 'pyproject.toml does not scope mutmut to the four targets.' -ForegroundColor Red
            Write-Host ('Missing: ' + ($missing -join ', '))
            Write-Host 'Add exactly this block to pyproject.toml (the one manual step):'
            Write-Host ''
            Write-Host $ConfigBlock
            exit 3
        }
    }

    Write-Report -Reset -Lines @(
        "Session-10 mutation run ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))",
        "threshold: $Threshold%",
        "mutmut: $versionOutput",
        "targets: $($Targets -join ', ')",
        ''
    )

    if ($DryRun) {
        foreach ($target in $Targets) {
            if ($cliSupportsPaths) {
                Write-Host "would run: uv run mutmut run --paths-to-mutate $target"
            }
            else {
                Write-Host "would run: uv run mutmut run   # scoped by [tool.mutmut] to $target"
            }
        }
        Write-Host "would run: uv run mutmut junitxml   # parsed for the kill rate"
        Write-Host "report:    $ReportPath"
        exit 0
    }

    # --- 3. Run mutmut. One pass per target when the CLI accepts a path. ----
    $xmlFiles = @()
    $runFailed = $false

    if ($cliSupportsPaths) {
        $index = 0
        foreach ($target in $Targets) {
            $index++
            $logPath = Join-Path $WorkDir ("run_{0}.log" -f $index)
            Write-Report -Lines @("", "=== mutmut run --paths-to-mutate $target ===")
            Write-Host "running mutmut on $target ..."
            $exit = Invoke-Mutmut -MutmutArguments @('run', '--paths-to-mutate', $target) -LogPath $logPath
            if (Test-Path $logPath) {
                Get-Content $logPath | ForEach-Object { Write-Report -Lines $_ }
            }
            if ($exit -ne 0 -and $exit -ne 1) {
                # mutmut returns 1 while mutants survive; anything else is a real error.
                Write-Host "mutmut failed on $target (exit $exit); see $logPath" -ForegroundColor Red
                $runFailed = $true
            }

            $xmlPath = Join-Path $WorkDir ("run_{0}.xml" -f $index)
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                & uv run mutmut junitxml 2>$null | Set-Content -Path $xmlPath -Encoding utf8
                $xmlExit = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousPreference
            }
            if ($xmlExit -eq 0 -and (Test-Path $xmlPath)) {
                $xmlFiles += $xmlPath
            }
        }
    }
    else {
        $logPath = Join-Path $WorkDir 'run_1.log'
        Write-Report -Lines @('', '=== mutmut run (scoped by [tool.mutmut]) ===')
        Write-Host 'running mutmut over the configured targets ...'
        $exit = Invoke-Mutmut -MutmutArguments @('run') -LogPath $logPath
        if (Test-Path $logPath) {
            Get-Content $logPath | ForEach-Object { Write-Report -Lines $_ }
        }
        if ($exit -ne 0 -and $exit -ne 1) {
            Write-Host "mutmut failed (exit $exit); see $logPath" -ForegroundColor Red
            $runFailed = $true
        }

        $xmlPath = Join-Path $WorkDir 'run_1.xml'
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & uv run mutmut junitxml 2>$null | Set-Content -Path $xmlPath -Encoding utf8
            $xmlExit = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($xmlExit -eq 0 -and (Test-Path $xmlPath)) {
            $xmlFiles += $xmlPath
        }
    }

    # --- 4. Aggregate the kill rate. ---------------------------------------
    $totals = [pscustomobject]@{
        Tests     = 0
        Killed    = 0
        Survived  = 0
        Skipped   = 0
        Survivors = @()
    }
    $survivors = @()

    foreach ($xmlFile in $xmlFiles) {
        $counts = Get-MutationCounts -XmlPath $xmlFile
        $totals.Tests += $counts.Tests
        $totals.Killed += $counts.Killed
        $totals.Survived += $counts.Survived
        $totals.Skipped += $counts.Skipped
        $survivors += $counts.Survivors
    }

    Write-Report -Lines @(
        '',
        '=== summary ===',
        "mutants:  $($totals.Tests)",
        "killed:   $($totals.Killed)",
        "survived: $($totals.Survived)",
        "skipped:  $($totals.Skipped) (no test covered them - not kills)"
    )

    if ($totals.Tests -eq 0) {
        Write-Host 'No mutant results were produced; cannot compute a kill rate.' -ForegroundColor Red
        Write-Host "mutmut output: $ReportPath"
        exit 4
    }

    $killRate = [math]::Round(($totals.Killed / $totals.Tests) * 100, 2)
    # The gate is judged on the mutants a test could reach. Unmapped mutants
    # ("no test covered it") are a coverage finding, reported separately and
    # never folded into the rate: counting them as kills would hide the gap, and
    # gating on them would demand a test for code this gate deliberately does not
    # scope (the corpus-side ChunkStore CRUD, mutated only because mutmut works on
    # a file rather than a function).
    $mapped = $totals.Tests - $totals.Skipped
    $mappedRate = if ($mapped -gt 0) {
        [math]::Round(($totals.Killed / $mapped) * 100, 2)
    }
    else { 0 }

    Write-Report -Lines @(
        ("kill rate: {0}% overall, {1}% of the {2} mapped mutants (threshold {3}% of mapped)" -f `
            $killRate, $mappedRate, $mapped, $Threshold),
        '',
        '=== survivors ==='
    )

    if ($survivors.Count -eq 0) {
        Write-Report -Lines @('(none)')
    }
    else {
        foreach ($survivor in $survivors) {
            Write-Report -Lines @($survivor)
        }
    }

    Write-Host ''
    Write-Host ("mutants: {0}  killed: {1}  survived: {2}  unmapped: {3}" -f `
        $totals.Tests, $totals.Killed, $totals.Survived, $totals.Skipped)
    Write-Host ("kill rate: {0}% overall, {1}% of the {2} mapped mutants" -f `
        $killRate, $mappedRate, $mapped)

    if ($survivors.Count -gt 0) {
        Write-Host ''
        Write-Host 'Surviving mutants (id from mutmut):'
        foreach ($survivor in $survivors) {
            Write-Host "  $survivor"
        }
        Write-Host 'List each survivor in the report with a one-line justification, or kill it with a test.'
    }

    Write-Host ''
    Write-Host "report: $ReportPath"

    if ($runFailed) {
        Write-Host 'At least one mutmut invocation failed for a reason other than survivors.' -ForegroundColor Red
        exit 4
    }

    if ($mappedRate -lt $Threshold) {
        Write-Host ("FAIL: {0}% of the mapped mutants were killed, below {1}%" -f `
            $mappedRate, $Threshold) -ForegroundColor Red
        Write-Host "report: $ReportPath"
        exit 5
    }

    Write-Host ("PASS: {0}% of the mapped mutants were killed (>= {1}%)" -f `
        $mappedRate, $Threshold) -ForegroundColor Green
    if ($totals.Skipped -gt 0) {
        Write-Host ("note: {0} mutants had no covering test; they are listed in the report and are not counted." -f `
            $totals.Skipped) -ForegroundColor Yellow
    }
    exit 0
}
finally {
    Pop-Location
}
