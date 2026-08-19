#Requires -Version 5.1
<#
windows_readiness.ps1 - READ-ONLY Windows demo readiness checker
(Windows demo mission, section 11).

This is NOT an installer. It never installs packages, never changes
configuration, never initializes or resets a repository, never creates
commits or worktrees, never modifies the ledger, and never launches a
Docket ticket. Its only writes are: a temporary create/delete probe
file per writability check, the report files under -OutDir (with a
documented fallback to %TEMP% when -OutDir cannot be written), and,
under -SelfTest only, a throwaway fixture tree under %TEMP%.

Stock Windows PowerShell 5.1; no PowerShell 7, Node, npm, WSL, admin
rights or third-party modules.

USAGE
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\docket\tools\windows_readiness.ps1 `
    -Root "C:\Docket_Testing" -Project "data_project" `
    -ExpectedModule "datacompare" -ExpectedPolars "1.43.0" `
    -ExpectedTests 40 -ExpectedPassed 40

VALIDATE THE CHECKER ITSELF FIRST
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    .\docket\tools\windows_readiness.ps1 -SelfTest

WHAT THE FIRST REAL WINDOWS 5.1 RUN TAUGHT US (0.0.2 -> 0.0.3)
  1. PowerShell resolves an ALIAS before a FUNCTION. The 0.0.2 checker
     defined "function R" for its result rows, and Windows PowerShell
     ships the built-in alias r -> Invoke-History. Every call site
     therefore invoked Invoke-History, which rejected the second
     argument, and ~50 unrelated rows read
     "check body raised: A positional parameter cannot be found that
     accepts argument 'Windows version ...'". One checker-system defect
     was rendered as dozens of host and project failures. The result
     helper is now New-ReadinessResult, and Assert-HarnessHealth proves
     - at startup, before any real check - that EVERY function this
     script declares actually resolves to a Function and that the
     result model can build PASS/WARN/FAIL/SKIP. If it cannot, the
     checker prints CHECKER ERROR and exits 2 rather than inventing
     host failures.
  2. (Resolve-Path $p).Path returns a PROVIDER-QUALIFIED string for
     UNC/provider locations - "Microsoft.PowerShell.Core\FileSystem::
     \\Mac\Home\Downloads\Docket_Testing" - which System.IO.Path, git,
     python and Get-FileHash all reject. ConvertTo-NativePath is now
     the single normalization authority for -Root, -Workbench, the
     project path and -OutDir.
  3. The demo cannot run from a network/share root. HOST-ROOT-LOCAL is
     a blocking prerequisite, and HOST-DISK no longer assumes
     (Get-Item $Root).PSDrive exists (it is $null for UNC, and
     dereferencing .Free then throws under StrictMode).
  4. A wrong -Root (pointing at the project instead of its parent) used
     to cascade into 50+ unrelated failures. Checks now declare
     prerequisites with -RequiresPass; a dependent check becomes
     "SKIP - prerequisite <id> failed: <original detail>" instead of
     inventing its own failure, and a BLOCKING check skipped that way
     counts as UNPROVEN, so a cascade can never buy WINDOWS DEMO READY.

THE BASELINE CONTRACT (-ExpectedTests / -ExpectedPassed / -AllowedSkip)
  -ExpectedTests is the COLLECTED count (passed + failed + errors +
  skipped), the suite-shape invariant: fewer means tests vanished or
  collection broke. -ExpectedPassed, when given, additionally demands
  an exact pass count. -AllowedSkip lists skip identifiers the
  operator EXPLICITLY accepts; it defaults to EMPTY, so any skip is a
  blocking failure until someone consciously accepts it. Accepting a
  skip NEVER produces WINDOWS DEMO READY: it downgrades an unexplained
  skip to a declared one so the run is useful for diagnosis, while the
  machine is still reported as not demo-ready. The demo contract is
  -ExpectedTests 40 -ExpectedPassed 40 with NO -AllowedSkip, and both
  the direct and the contained baseline reporting 40 passed, 0 failed,
  0 errors, 0 skipped.

  Why: the demo project collects 40 tests, two of which
  (tests/test_spark_engine.py::test_spark_matches_polars_end_to_end
  and ::test_spark_full_run) skip themselves when a local Spark
  session cannot start - that is, when Java is absent. The same
  healthy project therefore reports "40 passed" on a machine with
  Java and "38 passed, 2 skipped" without it, and neither number
  alone proves anything. So the contract is: collected ==
  -ExpectedTests, zero failed, zero errors, every skip explicitly
  accepted, and the DIRECT and CONTAINED runs must agree on the pass
  count AND on the skip set. That last clause is the one that matters
  for Docket: a test that passes directly but skips under
  containment is not an accepted skip, it is a DOCKET CONTAINMENT
  DEFECT - the contained child was denied something Spark needs -
  and it is reported as one.

Exit codes: 0 all blocking checks pass and none was left unproven;
1 one or more blocking checks fail (or could not run because a
prerequisite failed); 2 the checker itself is broken or its inputs are
invalid - it measured nothing, so it must not pretend a verdict.

Heavy Python facts come from Docket's own production door:
  <python> loop.py --project-preflight-json
which runs the SAME Python selection, environment sanitizer
(containment.sanitize_env), contained runner (containment.run_contained)
and working directory a real ticket uses. Containment logic is NEVER
duplicated in PowerShell. A probe that passes directly but fails
contained is flagged DOCKET CONTAINMENT DEFECT.

The checker CANNOT prove Copilot model consent/quota - the in-extension
"Docket: Run Preflight Probe" owns that check and is a REQUIRED manual
step before any live ticket (row MANUAL-PROBE).
#>

param(
    [string]$Root = (Get-Location).Path,
    [string]$Workbench = "",
    [string]$Project = "",
    [string]$ExpectedModule = "",
    [string]$ExpectedPolars = "",
    [int]$ExpectedTests = 0,
    [int]$ExpectedPassed = 0,
    [string[]]$AllowedSkip = @(),
    [switch]$SkipTests,
    [string]$OutDir = "",
    [switch]$SelfTest,
    [switch]$SelfTestNoMutations
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$script:ExpectedKitVersion = "0.0.4"

# ---------------------------------------------------------------- model

$script:Checks = @()
$script:CheckIndex = @{}

function Redact {
    # Never let a credential-shaped value reach a report. Names may
    # survive; values die.
    param([string]$s)
    if ($null -eq $s) { return "" }
    $pat = '(?i)\S*(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH)\S*\s*[:=]\s*\S+'
    return ($s -replace $pat, '[redacted]')
}

function New-ReadinessResult {
    # The result model. NAMED New-ReadinessResult and not something
    # short on purpose: PowerShell command precedence puts ALIAS ahead
    # of FUNCTION, so a one-or-two-letter helper is silently shadowed
    # by a built-in (r -> Invoke-History is what broke 0.0.2).
    # Assert-HarnessHealth proves at startup that this name still
    # resolves to this function.
    param(
        [string]$Status,
        [string]$Detail,
        [string]$Evidence = "",
        [string]$Fix = ""
    )
    return New-Object PSObject -Property @{
        Status = $Status; Detail = $Detail; Evidence = $Evidence
        Fix = $Fix
    }
}

function Add-Check {
    param(
        [string]$Id, [string]$Category, [string]$Status,
        [string]$Detail, [string]$Evidence = "", [string]$Fix = "",
        [bool]$Blocking = $true, [int]$DurationMs = 0,
        [bool]$SkippedByPrerequisite = $false, [string]$BlockedBy = "",
        [string]$RootDetail = ""
    )
    $row = New-Object PSObject -Property @{
        Id = $Id; Category = $Category; Status = $Status
        Detail = (Redact $Detail); Evidence = (Redact $Evidence)
        Fix = $Fix; Blocking = $Blocking; DurationMs = $DurationMs
        SkippedByPrerequisite = $SkippedByPrerequisite
        BlockedBy = $BlockedBy; RootDetail = (Redact $RootDetail)
    }
    $script:Checks += $row
    $script:CheckIndex[$Id] = $row
}

function Invoke-Check {
    # Times one check body; a crashing body is a FAIL row, never a
    # checker crash (exit 2 is reserved for the harness itself).
    #
    # -RequiresPass names the checks this one DEPENDS ON. If any of
    # them failed - or was itself skipped because its own prerequisite
    # failed - this check does not run and does not invent a failure of
    # its own. It records SKIP naming the ROOT blocking check and
    # repeating its detail, so the skip can never conceal the original
    # failure. One wrong -Root must produce ONE actionable row, not 50.
    param([string]$Id, [string]$Category, [bool]$Blocking,
          [scriptblock]$Body, [string[]]$RequiresPass = @())
    $blockedBy = ""
    $rootDetail = ""
    foreach ($need in $RequiresPass) {
        if (-not $need) { continue }
        if (-not $script:CheckIndex.ContainsKey($need)) { continue }
        $prev = $script:CheckIndex[$need]
        if ($prev.Status -eq "FAIL") {
            $blockedBy = $need
            $rootDetail = "" + $prev.Detail
            break
        }
        if ($prev.SkippedByPrerequisite) {
            $blockedBy = "" + $prev.BlockedBy
            $rootDetail = "" + $prev.RootDetail
            break
        }
    }
    if ($blockedBy -ne "") {
        Add-Check -Id $Id -Category $Category -Status "SKIP" `
            -Detail ("prerequisite " + $blockedBy + " failed: " + $rootDetail) `
            -Blocking $Blocking -DurationMs 0 `
            -SkippedByPrerequisite $true -BlockedBy $blockedBy `
            -RootDetail $rootDetail
        return
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $r = & $Body
        $sw.Stop()
        Add-Check -Id $Id -Category $Category -Status $r.Status `
            -Detail $r.Detail -Evidence $(if ($r.PSObject.Properties['Evidence']) { $r.Evidence } else { "" }) `
            -Fix $(if ($r.PSObject.Properties['Fix']) { $r.Fix } else { "" }) `
            -Blocking $Blocking -DurationMs $sw.ElapsedMilliseconds
    } catch {
        $sw.Stop()
        Add-Check -Id $Id -Category $Category -Status "FAIL" `
            -Detail ("check body raised: " + $_.Exception.Message) `
            -Blocking $Blocking -DurationMs $sw.ElapsedMilliseconds
    }
}

function ConvertTo-NativePath {
    # THE ONE PATH AUTHORITY (root cause 2). Turns anything the shell
    # can hand us - relative, provider-qualified, UNC, spaces - into a
    # NATIVE filesystem path that System.IO.Path, git, python and
    # Get-FileHash all accept. Never throws.
    param([string]$InputPath)
    if ($null -eq $InputPath) { return "" }
    $p = "" + $InputPath
    if ($p.Trim() -eq "") { return "" }
    # "<provider>::<path>" is what (Resolve-Path).Path returns for a
    # provider/UNC location. Strip the qualifier before anything else
    # ever sees the string.
    $idx = $p.IndexOf("::")
    if ($idx -ge 0) { $p = $p.Substring($idx + 2) }
    $native = $p
    try {
        $native = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p)
    } catch {
        $native = $p
    }
    try {
        $native = [System.IO.Path]::GetFullPath($native)
    } catch {
        # Keep the provider-stripped form; Test-NativePathLocal reports
        # it as unsupported rather than letting it reach .NET again.
        $native = $p
    }
    if ($native.Length -gt 3 -and $native.EndsWith("\")) {
        $native = $native.TrimEnd('\')
    }
    return $native
}

function Test-NativePathLocal {
    # Is this a LOCAL Windows filesystem path? A Parallels/network
    # share (\\Mac\Home\..., \\server\share\...) and a drive letter
    # MAPPED to one are both unsupported demo roots: contained
    # children, venvs, git worktrees and file locking all behave
    # differently there.
    param([string]$NativePath)
    $p = "" + $NativePath
    $kind = "local"
    $reason = ""
    if ($p -eq "") {
        $kind = "empty"
        $reason = "the path is empty"
    } elseif ($p.Contains("::")) {
        $kind = "provider"
        $reason = ("the path is still provider-qualified after " +
                   "normalization (" + $p + ")")
    } elseif ($p.StartsWith("\\")) {
        $kind = "unc"
        $reason = ("the path is a UNC network/share location (" +
                   $p + ")")
    } elseif ($p -match '^[A-Za-z]:\\') {
        $drv = Get-ReadinessDrive $p
        if ($null -ne $drv -and $drv.Mapped) {
            $kind = "mapped"
            $reason = ("drive " + $drv.Name + ": is MAPPED to the " +
                       "network location " + $drv.DisplayRoot)
        }
    } else {
        $kind = "unknown"
        $reason = ("the path is not an ordinary local Windows drive " +
                   "path (" + $p + ")")
    }
    return New-Object PSObject -Property @{
        IsLocal = ($kind -eq "local"); Kind = $kind; Reason = $reason
    }
}

function Get-ReadinessDrive {
    # HOST-DISK must never assume (Get-Item $Root).PSDrive exists: for
    # a UNC path it is $null, and dereferencing .Free then throws under
    # StrictMode - which is how "free disk 165.8 GB" became a raised
    # check body instead of an unsupported-root report.
    param([string]$NativePath)
    $p = "" + $NativePath
    if ($p.Length -lt 2) { return $null }
    if ($p.Substring(1, 1) -ne ":") { return $null }
    $letter = $p.Substring(0, 1)
    $drv = Get-PSDrive -Name $letter -PSProvider FileSystem -ErrorAction SilentlyContinue
    if ($null -eq $drv) { return $null }
    $free = $null
    try { $free = $drv.Free } catch { $free = $null }
    $display = ""
    try { if ($drv.DisplayRoot) { $display = "" + $drv.DisplayRoot } } catch { $display = "" }
    return New-Object PSObject -Property @{
        Name = $letter; Free = $free; DisplayRoot = $display
        Mapped = ($display -ne "" -and $display.StartsWith("\\"))
    }
}

function Probe-Writable {
    param([string]$dir)
    $probe = Join-Path $dir (".readiness_probe_" + [guid]::NewGuid().ToString("N"))
    try {
        Set-Content -Path $probe -Value "probe" -ErrorAction Stop
        Remove-Item -Path $probe -Force -ErrorAction Stop
        return $true
    } catch {
        try { Remove-Item -Path $probe -Force -ErrorAction SilentlyContinue } catch {}
        return $false
    }
}

function Run-Exe {
    # Run one executable, capture combined output + exit code. Never
    # throws; a missing exe returns code -1. ErrorActionPreference is
    # relaxed LOCALLY: with Stop, 2>&1 on a native command that writes
    # ordinary stderr (git warnings) would throw mid-stream (a classic
    # PowerShell 5.1 hazard).
    param([string]$Exe, [string[]]$Arguments, [string]$Cwd = "")
    $ErrorActionPreference = "Continue"
    $out = ""
    $code = -1
    try {
        if ($Cwd -ne "") { Push-Location $Cwd }
        try {
            $out = (& $Exe @Arguments 2>&1 | Out-String)
            $code = $LASTEXITCODE
        } finally {
            if ($Cwd -ne "") { Pop-Location }
        }
    } catch {
        $out = $_.Exception.Message
        $code = -1
    }
    return New-Object PSObject -Property @{ Output = $out; Code = $code }
}

function PF-Row {
    param([string]$pfId)
    if ($null -eq $script:Preflight) { return $null }
    foreach ($c in $script:Preflight.checks) {
        if ($c.id -eq $pfId) { return $c }
    }
    return $null
}

function Map-PF {
    param([string]$rtId, [string]$pfId, [bool]$blocking,
          [string[]]$RequiresPass = @())
    Invoke-Check $rtId "runtime" $blocking -RequiresPass $RequiresPass {
        $row = PF-Row $pfId
        if ($null -eq $row) { return (New-ReadinessResult "SKIP" ("preflight did not run or has no " + $pfId + " row")) }
        $status = $row.status
        $detail = $row.detail
        $isDefect = $false
        try { if ($row.containment_defect) { $isDefect = $true } } catch {}
        if ($isDefect) {
            # Prominence rule: direct PASS + contained FAIL.
            return (New-ReadinessResult "FAIL" ("DOCKET CONTAINMENT DEFECT - " + $detail))
        }
        New-ReadinessResult $status $detail
    }
}

function Assert-HarnessHealth {
    # THE CHECKER-SYSTEM GATE. Runs before any real check and asks two
    # questions a broken harness cannot answer:
    #   1. does every function this script declares actually RESOLVE to
    #      that function - or is it shadowed by a built-in alias, the
    #      way "function R" was shadowed by r -> Invoke-History? This
    #      rule needs no alias table: it asks PowerShell itself.
    #   2. can the result model build PASS / WARN / FAIL / SKIP objects
    #      with the shape the report depends on?
    # A checker-system failure must never be rendered as dozens of
    # host/project failures, so a problem here means exit 2 and nothing
    # else is reported.
    $problems = @()
    foreach ($fn in $script:DeclaredFunctions) {
        $cmd = Get-Command -Name $fn -ErrorAction SilentlyContinue
        if ($null -eq $cmd) {
            $problems += ("function '" + $fn + "' does not resolve at all")
            continue
        }
        $ct = "" + $cmd.CommandType
        if ($ct -ne "Function") {
            $problems += ("the name '" + $fn + "' resolves to a " + $ct +
                          " (" + $cmd.Name + "), not to this script's " +
                          "function - PowerShell resolves an alias " +
                          "before a function, so every call to it is " +
                          "misdirected")
        }
    }
    foreach ($st in @("PASS", "WARN", "FAIL", "SKIP")) {
        $probe = $null
        try {
            $probe = New-ReadinessResult $st "detail" "evidence" "fix"
        } catch {
            $probe = $null
        }
        if ($null -eq $probe) {
            $problems += ("cannot construct a " + $st + " result object")
            continue
        }
        $shapeOk = $true
        foreach ($prop in @("Status", "Detail", "Evidence", "Fix")) {
            if (-not $probe.PSObject.Properties[$prop]) { $shapeOk = $false }
        }
        if (-not $shapeOk) {
            $problems += ("the " + $st + " result object is missing " +
                          "Status/Detail/Evidence/Fix")
        } elseif ($probe.Status -ne $st -or $probe.Detail -ne "detail" `
                  -or $probe.Evidence -ne "evidence" -or $probe.Fix -ne "fix") {
            $problems += ("the " + $st + " result object did not carry " +
                          "its arguments through")
        }
    }
    # Fail-closed fault injection, used ONLY by -SelfTest to prove the
    # exit-2 contract. It can ADD a problem; it can never remove one,
    # so it cannot make a broken checker look healthy.
    if ($env:DOCKET_READINESS_SELFTEST_FAULT -eq "result-model") {
        $problems += "injected fault: result model declared broken"
    }
    return $problems
}

function Invoke-ReadinessSelfTest {
    # REAL WINDOWS SELF-TEST. This tier EXECUTES behaviour on stock
    # Windows PowerShell 5.1; it never parses source text to decide
    # whether something works. That distinction is the whole lesson of
    # 0.0.2, whose text-matching harness reported 16/16 while the
    # checker could not execute a single row on Windows.
    #
    # It builds throwaway fixtures under %TEMP%, runs THIS script as a
    # child process against them, and then mutates a COPY of its own
    # bytes to prove each guard has teeth. The shipped file is never
    # modified, no git command is ever run, and nothing outside the
    # sandbox is written.
    $sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("docket-readiness-selftest-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
    $sandbox = ConvertTo-NativePath $sandbox

    $rec = {
        param([string]$Id, [bool]$Ok, [string]$Note)
        $script:StRows += (New-Object PSObject -Property @{
            Id = $Id; Ok = $Ok; Note = $Note })
    }
    $script:StRows = @()

    $runScript = {
        param([string]$ScriptPath, [string[]]$Extra)
        $exe = Join-Path $PSHOME "powershell.exe"
        $a = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               $ScriptPath) + $Extra
        return (Run-Exe $exe $a)
    }

    $mkKit = {
        param([string]$RootDir)
        foreach ($sub in @("agents", "scripts", "tickets", "tools")) {
            New-Item -ItemType Directory -Force `
                -Path (Join-Path (Join-Path $RootDir "docket") $sub) | Out-Null
        }
        $d = Join-Path $RootDir "docket"
        foreach ($f in @("ledger.py", "schema.sql", "loop.py",
                         "containment.py", "model_authority.py")) {
            Set-Content -Path (Join-Path $d $f) -Value "# marker" -Encoding ASCII
        }
        # The python pin carries a credential-SHAPED value on purpose:
        # ST-NO-CREDENTIALS proves the reports redact the VALUE while
        # the row still explains itself. It is a fixture string in a
        # throwaway temp tree, never a real secret.
        $cfg = '{"python": "C:\\creds\\PASSWORD=hunter2SECRETVALUE\\python.exe",'
        $cfg = $cfg + ' "governor": {"max_tokens_per_run": 100},'
        $cfg = $cfg + ' "models": {"worker": "m"},'
        $cfg = $cfg + ' "gates": {"comprehension": {"enabled": true}}}'
        Set-Content -Path (Join-Path $d "config.json") -Value $cfg -Encoding ASCII
        # A fixture ledger inside the sandbox, so ST-LEDGER-UNTOUCHED
        # can prove CFG-LEDGER-UNTOUCHED has something to compare. The
        # production ledger ($LedgerPath) is only ever hashed.
        $fixtureLedger = Join-Path $d "ledger.db"
        Set-Content -Path $fixtureLedger -Value "fixture-ledger" -Encoding ASCII
        $tk = Join-Path $d "tickets"
        Set-Content -Path (Join-Path $tk "_template.md") -Value "# template" -Encoding ASCII
        Set-Content -Path (Join-Path $tk "DATACMP-0.md") `
            -Value "Issue: DATACMP-0`nAcceptance Criteria`n1. x" -Encoding ASCII
        $proj = Join-Path $RootDir "data_project"
        New-Item -ItemType Directory -Force -Path $proj | Out-Null
        Set-Content -Path (Join-Path $proj "a.py") -Value "x = 1" -Encoding ASCII
        return $proj
    }

    $treeHash = {
        param([string]$Dir)
        $acc = ""
        if (Test-Path -LiteralPath $Dir) {
            $files = @(Get-ChildItem -LiteralPath $Dir -Recurse -File -ErrorAction SilentlyContinue | Sort-Object FullName)
            foreach ($f in $files) {
                $acc = $acc + $f.FullName.Substring($Dir.Length) + ":"
                $acc = $acc + (Get-FileHash -Algorithm SHA256 -LiteralPath $f.FullName).Hash + ";"
            }
        }
        return $acc
    }

    # ---------------------------------------------------------------
    # 1. the alias hazard itself, proven live on this host
    # ---------------------------------------------------------------
    $aliasR = Get-Alias -Name "r" -ErrorAction SilentlyContinue
    $resolvedR = ""
    if ($null -ne $aliasR) {
        try { $resolvedR = "" + $aliasR.ResolvedCommandName } catch { $resolvedR = "" }
    }
    $fnType = ""
    $fnCmd = Get-Command -Name "New-ReadinessResult" -ErrorAction SilentlyContinue
    if ($null -ne $fnCmd) { $fnType = "" + $fnCmd.CommandType }
    # The 0.0.2 call form, invoked through a variable so this file
    # contains no literal shadowed call site. It MUST fail: the alias
    # wins, and Invoke-History rejects the second argument.
    $oldName = "R"
    $oldFormFailed = $false
    try {
        $null = & $oldName "PASS" "Windows version 10.0.26100"
    } catch {
        $oldFormFailed = $true
    }
    & $rec "ST-ALIAS-R" `
        (($resolvedR -eq "Invoke-History") -and ($fnType -eq "Function") -and $oldFormFailed) `
        ("Get-Alias r -> '" + $resolvedR + "'; New-ReadinessResult resolves to '" +
         $fnType + "'; the old one-letter call form failed = " + $oldFormFailed)

    # ---------------------------------------------------------------
    # 2. the result model renders all four statuses
    # ---------------------------------------------------------------
    $shapeOk = $true
    $shapeNote = ""
    foreach ($st in @("PASS", "WARN", "FAIL", "SKIP")) {
        $o = New-ReadinessResult $st ("d-" + $st) ("e-" + $st) ("f-" + $st)
        if ($o.Status -ne $st -or $o.Detail -ne ("d-" + $st) `
            -or $o.Evidence -ne ("e-" + $st) -or $o.Fix -ne ("f-" + $st)) {
            $shapeOk = $false
            $shapeNote = $shapeNote + $st + " "
        }
    }
    & $rec "ST-RESULT-SHAPES" $shapeOk ("bad: " + $shapeNote)

    # ---------------------------------------------------------------
    # 3. path normalization: spaces, relative, provider-qualified, UNC
    # ---------------------------------------------------------------
    $spaced = ConvertTo-NativePath "C:\Docket Agentic Kit"
    $relBase = Join-Path $sandbox "sub dir"
    New-Item -ItemType Directory -Force -Path $relBase | Out-Null
    Push-Location -LiteralPath $sandbox
    $rel = ConvertTo-NativePath ".\sub dir"
    Pop-Location
    & $rec "ST-PATH-SPACES" `
        (($spaced -eq "C:\Docket Agentic Kit") -and ($rel -eq $relBase)) `
        ("spaced='" + $spaced + "' relative='" + $rel + "'")

    $provIn = "Microsoft.PowerShell.Core\FileSystem::" + $sandbox
    $prov = ConvertTo-NativePath $provIn
    $uncNative = ConvertTo-NativePath "\\Mac\Home\Downloads\Docket_Testing"
    $uncProvNative = ConvertTo-NativePath ("Microsoft.PowerShell.Core\FileSystem::\\Mac\Home\Downloads\Docket_Testing")
    $uncLoc = Test-NativePathLocal $uncNative
    $uncProvLoc = Test-NativePathLocal $uncProvNative
    $localLoc = Test-NativePathLocal $prov
    & $rec "ST-PATH-PROVIDER" `
        (($prov -eq $sandbox) -and (-not $prov.Contains("::")) `
         -and $localLoc.IsLocal `
         -and (-not $uncNative.Contains("::")) `
         -and (-not $uncProvNative.Contains("::")) `
         -and (-not $uncLoc.IsLocal) -and (-not $uncProvLoc.IsLocal)) `
        ("provider-qualified local -> '" + $prov + "'; UNC -> '" +
         $uncNative + "' local=" + $uncLoc.IsLocal +
         "; provider-qualified UNC -> '" + $uncProvNative +
         "' local=" + $uncProvLoc.IsLocal)

    # ---------------------------------------------------------------
    # 4. child scenarios against a fixture kit
    # ---------------------------------------------------------------
    $kitRoot = Join-Path $sandbox "Docket Testing"
    New-Item -ItemType Directory -Force -Path $kitRoot | Out-Null
    $kitRoot = ConvertTo-NativePath $kitRoot
    $projPath = & $mkKit $kitRoot
    $ledgerPath = Join-Path (Join-Path $kitRoot "docket") "ledger.db"
    $ledgerBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $ledgerPath).Hash
    $projBefore = & $treeHash $projPath

    # 4a. UNC root -> ONE actionable blocking failure, exit 1
    $uncOut = Join-Path $sandbox "out-unc"
    $rUnc = & $runScript $PSCommandPath @("-Root", "\\Mac\Home\Docket_Testing",
                                          "-Project", "data_project",
                                          "-SkipTests", "-OutDir", $uncOut)
    $uncJson = Join-Path $uncOut "windows-readiness.json"
    $uncBlockers = @()
    if (Test-Path -LiteralPath $uncJson) {
        $uncRep = (Get-Content -LiteralPath $uncJson -Raw | ConvertFrom-Json)
        $uncBlockers = @($uncRep.Checks | Where-Object { $_.Status -eq "FAIL" -and $_.Blocking })
    }
    & $rec "ST-ROOT-UNC" `
        (($rUnc.Code -eq 1) -and ($uncBlockers.Count -eq 1) `
         -and ($uncBlockers[0].Id -eq "HOST-ROOT-LOCAL") `
         -and ($rUnc.Output.Contains("Docket demo execution requires a local Windows filesystem."))) `
        ("exit " + $rUnc.Code + "; blocking failures: " +
         (@($uncBlockers | ForEach-Object { $_.Id }) -join ","))

    # 4b. Root pointed at the PROJECT -> one exact correction, no cascade
    $ripOut = Join-Path $sandbox "out-rip"
    $rRip = & $runScript $PSCommandPath @("-Root", $projPath,
                                          "-Project", "data_project",
                                          "-SkipTests", "-OutDir", $ripOut)
    $ripJson = Join-Path $ripOut "windows-readiness.json"
    $ripBlockers = @()
    $ripSkips = @()
    $ripFix = ""
    $ripRep = $null
    if (Test-Path -LiteralPath $ripJson) {
        $ripRep = (Get-Content -LiteralPath $ripJson -Raw | ConvertFrom-Json)
        $ripBlockers = @($ripRep.Checks | Where-Object { $_.Status -eq "FAIL" -and $_.Blocking })
        $ripSkips = @($ripRep.Checks | Where-Object { $_.SkippedByPrerequisite })
        foreach ($c in $ripRep.Checks) {
            if ($c.Id -eq "KIT-ROOT-IS-PROJECT") { $ripFix = "" + $c.Fix }
        }
    }
    $wantFix = ('-Root "' + $kitRoot + '" -Project "data_project"')
    & $rec "ST-ROOT-IS-PROJECT" `
        (($rRip.Code -eq 1) -and ($ripBlockers.Count -eq 1) `
         -and ($ripBlockers[0].Id -eq "KIT-ROOT-IS-PROJECT") `
         -and $ripFix.Contains("Root points to the project directory.") `
         -and $ripFix.Contains($wantFix)) `
        ("exit " + $rRip.Code + "; blocking failures: " +
         (@($ripBlockers | ForEach-Object { $_.Id }) -join ",") +
         "; correction offered: " + $ripFix.Replace("`n", " "))

    & $rec "ST-ONE-FAILURE" `
        (($ripBlockers.Count -eq 1) -and ($ripBlockers[0].Id -eq "KIT-ROOT-IS-PROJECT")) `
        ("one wrong -Root must produce exactly one blocking row; got " +
         $ripBlockers.Count + " (" +
         (@($ripBlockers | ForEach-Object { $_.Id }) -join ",") + ")")

    $skipsNamed = $true
    foreach ($s in $ripSkips) {
        if (-not ("" + $s.Detail).StartsWith("prerequisite ")) { $skipsNamed = $false }
        if (("" + $s.BlockedBy) -eq "") { $skipsNamed = $false }
    }
    & $rec "ST-DEPENDENT-SKIPS" `
        (($ripSkips.Count -ge 20) -and $skipsNamed) `
        ($ripSkips.Count.ToString() + " dependent rows became SKIP naming their prerequisite (cascade avoided)")

    # 4c. reports parse, sidecars match, credentials never appear
    $ripTxt = Join-Path $ripOut "windows-readiness.txt"
    $txtBody = ""
    $jsonOk = $false
    if (Test-Path -LiteralPath $ripTxt) { $txtBody = (Get-Content -LiteralPath $ripTxt -Raw) }
    if ($null -ne $ripRep) { $jsonOk = (("" + $ripRep.Schema) -eq "docket.windows_readiness.v1") }
    & $rec "ST-REPORTS-PARSE" `
        ($jsonOk -and $txtBody.Contains("VERDICT: WINDOWS DEMO BLOCKED")) `
        ("json schema ok = " + $jsonOk + "; txt bytes = " + $txtBody.Length)

    $sideOk = $true
    $sideNote = ""
    foreach ($rp in @($ripTxt, $ripJson)) {
        $sc = $rp + ".sha256"
        if (-not (Test-Path -LiteralPath $sc)) { $sideOk = $false; $sideNote = $sideNote + "missing " + $sc + " "; continue }
        $want = (Get-FileHash -Algorithm SHA256 -LiteralPath $rp).Hash.ToLower()
        $got = ((Get-Content -LiteralPath $sc -Raw) -split '\s+')[0].ToLower()
        if ($want -ne $got) { $sideOk = $false; $sideNote = $sideNote + "mismatch " + $sc + " " }
    }
    & $rec "ST-REPORT-SIDECARS" $sideOk ("sidecar check: " + $(if ($sideNote -eq "") { "all match" } else { $sideNote }))

    $credLeak = $txtBody.Contains("hunter2SECRETVALUE")
    $jsonBody = ""
    if (Test-Path -LiteralPath $ripJson) { $jsonBody = (Get-Content -LiteralPath $ripJson -Raw) }
    if ($jsonBody.Contains("hunter2SECRETVALUE")) { $credLeak = $true }
    & $rec "ST-NO-CREDENTIALS" (-not $credLeak) `
        ("a credential-shaped config value reached a report = " + $credLeak)

    # 4d. nothing was mutated
    $ledgerAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $ledgerPath).Hash
    & $rec "ST-LEDGER-UNTOUCHED" ($ledgerAfter -eq $ledgerBefore) `
        ("ledger.db sha256 before/after: " + $ledgerBefore.Substring(0, 16) +
         " / " + $ledgerAfter.Substring(0, 16))
    $projAfter = & $treeHash $projPath
    & $rec "ST-PROJECT-UNTOUCHED" ($projAfter -eq $projBefore) `
        ("project tree rollup identical = " + ($projAfter -eq $projBefore))

    # 4e. exit-code contract
    $rMissing = & $runScript $PSCommandPath @("-Root", (Join-Path $sandbox "does not exist"),
                                              "-Project", "data_project", "-SkipTests")
    $env:DOCKET_READINESS_SELFTEST_FAULT = "result-model"
    $faultOut = Join-Path $sandbox "out-fault"
    $rFault = & $runScript $PSCommandPath @("-Root", $kitRoot, "-Project", "data_project",
                                            "-SkipTests", "-OutDir", $faultOut)
    Remove-Item -Path "Env:\DOCKET_READINESS_SELFTEST_FAULT" -ErrorAction SilentlyContinue
    & $rec "ST-MODEL-FAULT-EXIT2" `
        (($rFault.Code -eq 2) `
         -and $rFault.Output.Contains("CHECKER ERROR: readiness result model failed") `
         -and (-not (Test-Path -LiteralPath (Join-Path $faultOut "windows-readiness.json")))) `
        ("exit " + $rFault.Code + "; a broken result model reports one checker error and no host failures")

    # exit 0 is withheld whenever the report is not READY. The POSITIVE
    # exit-0 path is proven by the real readiness run in the VM
    # acceptance sequence, not faked here: a synthetic fixture has no
    # venv, so a green run is unreachable and would have to be
    # simulated - which is exactly the kind of proof this mission
    # rejects.
    $verdictAgrees = $true
    foreach ($pair in @(@($rRip.Code, $ripJson), @($rUnc.Code, $uncJson))) {
        $code = $pair[0]
        $jp = $pair[1]
        if (-not (Test-Path -LiteralPath $jp)) { continue }
        $rep = (Get-Content -LiteralPath $jp -Raw | ConvertFrom-Json)
        $ready = (("" + $rep.Verdict) -eq "WINDOWS DEMO READY")
        if ($ready -and $code -ne 0) { $verdictAgrees = $false }
        if ((-not $ready) -and $code -eq 0) { $verdictAgrees = $false }
    }
    & $rec "ST-EXIT-CODES" `
        (($rMissing.Code -eq 2) -and ($rFault.Code -eq 2) `
         -and ($rRip.Code -eq 1) -and ($rUnc.Code -eq 1) -and $verdictAgrees) `
        ("nonexistent local root = " + $rMissing.Code + " (want 2); broken model = " +
         $rFault.Code + " (want 2); blocking failure = " + $rRip.Code +
         " (want 1); exit code agrees with the printed verdict = " + $verdictAgrees +
         ". The positive exit-0 path is proven by the real readiness run, not simulated here.")

    # ---------------------------------------------------------------
    # 5. mutation tier - every guard must have teeth. Each mutant is a
    #    COPY under the sandbox; the shipped file is never touched.
    # ---------------------------------------------------------------
    if ($SelfTestNoMutations) {
        foreach ($mid in @("ST-MUT-FUNCTION-R", "ST-MUT-NO-PROVIDER-NORM",
                           "ST-MUT-ALLOW-UNC", "ST-MUT-NO-DEP-SKIPS",
                           "ST-MUT-WEAK-WRONG-ROOT")) {
            & $rec $mid $true "skipped: this run is itself a mutant (recursion bound)"
        }
    } else {
        $srcText = Get-Content -LiteralPath $PSCommandPath -Raw
        $mutations = @(
            @("ST-MUT-FUNCTION-R", "mut-function-r.ps1"),
            @("ST-MUT-NO-PROVIDER-NORM", "mut-no-provider.ps1"),
            @("ST-MUT-ALLOW-UNC", "mut-allow-unc.ps1"),
            @("ST-MUT-NO-DEP-SKIPS", "mut-no-dep-skips.ps1"),
            @("ST-MUT-WEAK-WRONG-ROOT", "mut-weak-wrong-root.ps1")
        )
        foreach ($mu in $mutations) {
            $mid = $mu[0]
            $mtext = $srcText
            if ($mid -eq "ST-MUT-FUNCTION-R") {
                $mtext = $srcText.Replace("New-ReadinessResult", "R")
            } elseif ($mid -eq "ST-MUT-NO-PROVIDER-NORM") {
                $mtext = $srcText.Replace('$idx = $p.IndexOf("::")', '$idx = -1')
                $mtext = $mtext.Replace('$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p)', '$p')
            } elseif ($mid -eq "ST-MUT-ALLOW-UNC") {
                $mtext = $srcText.Replace('IsLocal = ($kind -eq "local")', 'IsLocal = $true')
            } elseif ($mid -eq "ST-MUT-NO-DEP-SKIPS") {
                $mtext = $srcText.Replace('if ($blockedBy -ne "") {', 'if ($false) {')
            } elseif ($mid -eq "ST-MUT-WEAK-WRONG-ROOT") {
                $mtext = $srcText.Replace('$rootIsProject = ($looksLikeProject -and $parentHasDocket)', '$rootIsProject = $false')
            }
            $changed = ($mtext -ne $srcText)
            $mpath = Join-Path $sandbox $mu[1]
            Set-Content -LiteralPath $mpath -Value $mtext -Encoding ASCII
            $rm = & $runScript $mpath @("-SelfTest", "-SelfTestNoMutations")
            & $rec $mid ($changed -and ($rm.Code -ne 0)) `
                ("source actually changed = " + $changed + "; mutant self-test exit = " +
                 $rm.Code + " (must be non-zero)")
        }
    }

    # ---------------------------------------------------------------
    foreach ($row in $script:StRows) {
        $mark = "FAIL"
        if ($row.Ok) { $mark = "PASS" }
        Write-Host ("  [" + $mark + "] " + $row.Id + " - " + $row.Note)
    }
    $good = @($script:StRows | Where-Object { $_.Ok }).Count
    $total = $script:StRows.Count
    Write-Host ""
    Write-Host ("  SELF-TEST: " + $good + "/" + $total + " passed")
    try { Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue } catch {}
    if ($good -eq $total) {
        Write-Host "  READINESS CHECKER SELF-TEST OK"
        return 0
    }
    Write-Host "  READINESS CHECKER SELF-TEST FAILED - do not trust this checker"
    return 1
}

# The inventory Assert-HarnessHealth verifies. Every function declared
# above must appear here; windows_readiness_check.py fails the build if
# the two ever diverge, because a function missing from this list would
# escape the runtime alias-collision guard entirely.
$script:DeclaredFunctions = @(
    'Redact', 'New-ReadinessResult', 'Add-Check', 'Invoke-Check',
    'ConvertTo-NativePath', 'Test-NativePathLocal', 'Get-ReadinessDrive',
    'Probe-Writable', 'Run-Exe', 'PF-Row', 'Map-PF',
    'Assert-HarnessHealth', 'Invoke-ReadinessSelfTest'
)

# @(...) on purpose: a function returning an EMPTY array yields $null
# in PowerShell, and $null.Count is not something to bet the exit-2
# contract on.
$script:HarnessProblems = @(Assert-HarnessHealth)
if ($script:HarnessProblems.Count -gt 0) {
    Write-Host "CHECKER ERROR: readiness result model failed"
    foreach ($hp in $script:HarnessProblems) { Write-Host ("  - " + $hp) }
    Write-Host ""
    Write-Host ("This is a defect in the CHECKER, not in this machine. " +
                "No host or project conclusion can be drawn from this run.")
    exit 2
}

if ($SelfTest) {
    # Take the LAST pipeline value: if any statement in the self-test
    # ever emits, exit must still receive the return code and not a
    # cast error.
    $stCode = @(Invoke-ReadinessSelfTest) | Select-Object -Last 1
    exit ([int]$stCode)
}

# ---------------------------------------------------------- input guard

# Invalid inputs are exit 2 by contract: the checker did not measure
# anything, so it must not pretend a verdict. An UNSUPPORTED root
# (UNC/share/mapped drive) is NOT invalid input - it is a measured,
# actionable, blocking finding, so it takes the normal exit-1 path with
# exactly one row and every dependent check skipped.
$script:RootInput = "" + $Root
$Root = ConvertTo-NativePath $Root
if ($Root -eq "") {
    Write-Host "INVALID INPUT: -Root is empty."
    exit 2
}
if ($Project -eq "") {
    Write-Host "INVALID INPUT: -Project is required (sibling directory name or absolute path)."
    exit 2
}
$script:RootLocality = Test-NativePathLocal $Root
if ($script:RootLocality.IsLocal -and -not (Test-Path -LiteralPath $Root)) {
    Write-Host ("INVALID INPUT: -Root '" + $Root + "' does not exist.")
    exit 2
}

$q = [char]34

try {
    # =================================================================
    # main body - any uncaught error below lands in the outer catch
    # and exits 2 (checker crash), per the exit-code model.
    # =================================================================

    if ($Workbench -eq "") { $Workbench = Join-Path $Root "docket" }
    $Workbench = ConvertTo-NativePath $Workbench
    $projRooted = $false
    try { $projRooted = [System.IO.Path]::IsPathRooted($Project) } catch { $projRooted = $true }
    if ($projRooted) {
        $ProjPath = ConvertTo-NativePath $Project
        $ProjName = "" + (Split-Path -Path $ProjPath -Leaf)
    } else {
        $ProjPath = ConvertTo-NativePath (Join-Path $Root $Project)
        $ProjName = $Project
    }
    if ($OutDir -eq "") {
        if ($script:RootLocality.IsLocal) {
            $OutDir = Join-Path $Root "preflight-results"
        } else {
            # An unsupported root is usually unwritable too; the report
            # must still be produced so the operator can read the one
            # actionable row.
            $OutDir = Join-Path ([System.IO.Path]::GetTempPath()) "docket-preflight-results"
        }
    }
    $OutDir = ConvertTo-NativePath $OutDir

    $LedgerPath = Join-Path $Workbench "ledger.db"
    $LedgerHashBefore = ""
    if (Test-Path -LiteralPath $LedgerPath) {
        $LedgerHashBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $LedgerPath).Hash
    }

    Add-Check -Id "HARNESS-HEALTH" -Category "harness" -Status "PASS" `
        -Detail ("checker self-check passed before any host check: all " +
                 $script:DeclaredFunctions.Count + " declared functions " +
                 "resolve to this script's own functions (no built-in " +
                 "alias shadows one) and the result model builds " +
                 "PASS/WARN/FAIL/SKIP correctly") `
        -Blocking $true

    # ------------------------------------------------------- A. host
    Invoke-Check "HOST-OS" "host" $true {
        $os = [System.Environment]::OSVersion
        New-ReadinessResult "PASS" ("Windows version " + $os.VersionString)
    }
    Invoke-Check "HOST-ARCH" "host" $true {
        $arch = $env:PROCESSOR_ARCHITECTURE
        if ($arch -eq "AMD64" -or $arch -eq "ARM64") {
            New-ReadinessResult "PASS" ("architecture " + $arch)
        } elseif ($null -eq $arch) {
            New-ReadinessResult "WARN" "PROCESSOR_ARCHITECTURE not set - architecture unknown"
        } else {
            New-ReadinessResult "FAIL" ("32-bit or unknown architecture '" + $arch + "' - the demo needs x64/ARM64") `
              "" "Use a 64-bit Windows host."
        }
    }
    Invoke-Check "HOST-PS" "host" $true {
        $v = $PSVersionTable.PSVersion
        if ($v.Major -ge 5) { New-ReadinessResult "PASS" ("PowerShell " + $v.ToString()) }
        else { New-ReadinessResult "FAIL" ("PowerShell " + $v.ToString() + " is below 5.1") }
    }
    Invoke-Check "HOST-USER" "host" $true {
        if (("" + $env:USERNAME).Length -gt 0) { New-ReadinessResult "PASS" "current user resolved" }
        else { New-ReadinessResult "WARN" "USERNAME is empty" }
    }
    Invoke-Check "HOST-ROOT-LOCAL" "host" $true {
        # THE PREREQUISITE. A Parallels/network share root
        # (\\Mac\Home\..., \\server\share\..., or a drive letter mapped
        # to one) is not a supported demo location, and every layout,
        # git, python, venv, worktree and isolation check below depends
        # on it. One actionable row, then silence - never a cascade.
        if ($script:RootLocality.IsLocal) {
            $note = "root is a local Windows filesystem path: " + $Root
            if ($script:RootInput -ne $Root) {
                $note = $note + " (normalized from '" + $script:RootInput + "')"
            }
            return (New-ReadinessResult "PASS" $note)
        }
        $fixText = "Docket demo execution requires a local Windows filesystem."
        $fixText = $fixText + "`nCopy/extract the complete kit to C:\Docket Testing and rerun."
        New-ReadinessResult "FAIL" `
            ("unsupported demo root - " + $script:RootLocality.Reason) `
            ("-Root was given as '" + $script:RootInput + "' and normalizes to '" + $Root + "' (kind: " + $script:RootLocality.Kind + ")") `
            $fixText
    }
    Invoke-Check "HOST-ROOT" "host" $true -RequiresPass @("HOST-ROOT-LOCAL") {
        New-ReadinessResult "PASS" ("root exists: " + $Root)
    }
    Invoke-Check "HOST-ROOT-WRITE" "host" $true -RequiresPass @("HOST-ROOT-LOCAL") {
        if (Probe-Writable $Root) { New-ReadinessResult "PASS" "root is writable (create/delete probe)" }
        else { New-ReadinessResult "FAIL" "root is NOT writable" "" "Extract the kit somewhere the current user can write." }
    }
    Invoke-Check "HOST-DISK" "host" $false -RequiresPass @("HOST-ROOT-LOCAL") {
        $driveInfo = Get-ReadinessDrive $Root
        if ($null -eq $driveInfo) {
            return (New-ReadinessResult "WARN" ("no filesystem drive resolves for " + $Root + " - free space unknown"))
        }
        if ($null -eq $driveInfo.Free) {
            return (New-ReadinessResult "WARN" ("drive " + $driveInfo.Name + ": reports no free-space figure"))
        }
        $freeGB = [math]::Round($driveInfo.Free / 1GB, 1)
        if ($freeGB -gt 5) { New-ReadinessResult "PASS" ("free disk " + $freeGB + " GB on " + $driveInfo.Name + ":") }
        else { New-ReadinessResult "WARN" ("only " + $freeGB + " GB free - runs and artifacts add up") }
    }
    Invoke-Check "HOST-PATHLEN" "host" $false -RequiresPass @("HOST-ROOT-LOCAL") {
        if ($Root.Length -le 120) { New-ReadinessResult "PASS" ("root path length " + $Root.Length) }
        else { New-ReadinessResult "WARN" ("root path length " + $Root.Length + " risks MAX_PATH issues in deep venv trees") `
                 "" "Prefer a shorter root such as C:\Docket_Testing." }
    }
    Invoke-Check "HOST-TEMP" "host" $true {
        $t = $env:TEMP; $t2 = $env:TMP
        if ($t -and $t2 -and (Test-Path $t) -and (Test-Path $t2) -and (Probe-Writable $t)) {
            New-ReadinessResult "PASS" "TEMP and TMP exist and are writable"
        } else {
            New-ReadinessResult "FAIL" "TEMP/TMP missing or not writable" "" "Fix the user TEMP/TMP environment variables."
        }
    }
    foreach ($pair in @(
        @("HOST-SYSTEMROOT", "SystemRoot", $true),
        @("HOST-WINDIR", "windir", $true),
        @("HOST-SYSTEMDRIVE", "SystemDrive", $true),
        @("HOST-COMSPEC", "ComSpec", $true),
        @("HOST-NPROC", "NUMBER_OF_PROCESSORS", $true),
        @("HOST-PROCARCH", "PROCESSOR_ARCHITECTURE", $true))) {
        $cid = $pair[0]; $name = $pair[1]; $blocking = $pair[2]
        Invoke-Check $cid "host" $blocking {
            $val = [System.Environment]::GetEnvironmentVariable($name)
            if ($val) { New-ReadinessResult "PASS" ($name + " is present") }
            else { New-ReadinessResult "FAIL" ($name + " is MISSING - Python/native Windows execution cannot be reliable") `
                     "" "Restore the machine environment variable (System Properties -> Environment Variables)." }
        }
    }
    Invoke-Check "HOST-PROCMETA" "host" $false {
        $present = @()
        $missing = @()
        foreach ($n in @("PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION")) {
            if ([System.Environment]::GetEnvironmentVariable($n)) { $present += $n } else { $missing += $n }
        }
        if ($missing.Count -eq 0) { New-ReadinessResult "PASS" "PROCESSOR_IDENTIFIER/LEVEL/REVISION present" }
        else { New-ReadinessResult "WARN" ("optional processor metadata missing: " + ($missing -join ", ")) }
    }
    Invoke-Check "HOST-PATHEXT" "host" $false {
        $pe = "" + $env:PATHEXT
        if ($pe -match "(?i)\.EXE" -and $pe -match "(?i)\.CMD") {
            New-ReadinessResult "PASS" "PATHEXT contains expected executable extensions"
        } else { New-ReadinessResult "WARN" ("PATHEXT looks unusual: " + $pe) }
    }

    # -------------------------------------------- B. distribution layout
    Invoke-Check "KIT-ROOT-IS-PROJECT" "layout" $true -RequiresPass @("HOST-ROOT-LOCAL") {
        # THE WRONG-ROOT DIAGNOSIS. Passing -Root <kit>\data_project
        # -Project data_project makes the checker hunt for
        # <kit>\data_project\docket and <kit>\data_project\data_project.
        # That is one operator mistake, so it gets one row and one exact
        # correction, and every dependent check below skips.
        $hasDocket = Test-Path -LiteralPath (Join-Path $Root "docket")
        if ($hasDocket) {
            return (New-ReadinessResult "PASS" ("-Root names the kit root (docket\ is directly under " + $Root + ")"))
        }
        $parent = ""
        try { $parent = "" + (Split-Path -Path $Root -Parent) } catch { $parent = "" }
        $leaf = ""
        try { $leaf = "" + (Split-Path -Path $Root -Leaf) } catch { $leaf = "" }
        $looksLikeProject = $false
        foreach ($marker in @(".git", "requirements.txt", "pyproject.toml",
                              "setup.py", "venv", ".venv", "tests")) {
            if (Test-Path -LiteralPath (Join-Path $Root $marker)) { $looksLikeProject = $true }
        }
        if ($leaf -ieq $ProjName) { $looksLikeProject = $true }
        $parentHasDocket = $false
        if ($parent -ne "") {
            $parentHasDocket = (Test-Path -LiteralPath (Join-Path $parent "docket"))
        }
        $rootIsProject = ($looksLikeProject -and $parentHasDocket)
        if (-not $rootIsProject) {
            return (New-ReadinessResult "PASS" "-Root does not look like the project directory")
        }
        $fixText = "Root points to the project directory."
        $fixText = $fixText + "`nUse:"
        $fixText = $fixText + "`n-Root " + $q + $parent + $q + " -Project " + $q + $leaf + $q
        New-ReadinessResult "FAIL" `
            ("-Root points at the PROJECT directory (" + $Root + "), not at the kit root that holds BOTH docket\ and the project. Left alone this would send every layout, git, python, config, ticket and isolation check hunting for " + (Join-Path $Root "docket") + " - so it is reported ONCE here and the dependent rows are skipped, not failed.") `
            ("the parent directory " + $parent + " does contain docket\") `
            $fixText
    }
    Invoke-Check "KIT-LAYOUT" "layout" $true -RequiresPass @("HOST-ROOT-LOCAL", "KIT-ROOT-IS-PROJECT") {
        # THE PACKAGE-LAYOUT DIAGNOSIS. The complete kit extracts to ONE
        # root holding package-manifest.json, START-HERE.md,
        # OPEN-DOCKET.code-workspace, the VSIX and docket\ - with the
        # project placed beside docket\.
        $hasDocket = Test-Path -LiteralPath (Join-Path $Root "docket")
        $hasManifest = Test-Path -LiteralPath (Join-Path $Root "package-manifest.json")
        $hasStart = Test-Path -LiteralPath (Join-Path $Root "START-HERE.md")
        if ($hasDocket -and $hasManifest) {
            $extra = "START-HERE.md present"
            if (-not $hasStart) { $extra = "START-HERE.md absent (harmless)" }
            return (New-ReadinessResult "PASS" ("complete-kit root: package-manifest.json and docket\ are directly under " + $Root + "; " + $extra))
        }
        if ($hasDocket) {
            $fixText = ("Extract the whole docket-complete-" + $script:ExpectedKitVersion +
                        ".zip contents into one root so that package-manifest.json, " +
                        "START-HERE.md, OPEN-DOCKET.code-workspace, the VSIX and docket\ " +
                        "all sit directly under it.")
            return (New-ReadinessResult "WARN" `
                ("docket\ is under " + $Root + " but package-manifest.json is NOT. Either only the docket\ folder was copied, or the root was assembled by hand. Version and hash verification are unavailable; the rest of the demo can still be checked.") `
                "" $fixText)
        }
        $inner = @()
        try {
            $inner = @(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue |
                       Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "docket") })
        } catch { $inner = @() }
        if ($inner.Count -ge 1) {
            $target = "" + $inner[0].FullName
            return (New-ReadinessResult "FAIL" `
                ("-Root is ONE DIRECTORY ABOVE the kit root: " + $Root + " has no docket\, but " + $target + " does. That is what extracting the zip WITH its own top-level folder looks like.") `
                ("candidate kit roots found here: " + ((@($inner | ForEach-Object { $_.Name }) | Select-Object -First 5) -join ", ")) `
                ("Use: -Root " + $q + $target + $q))
        }
        $leaf2 = ""
        try { $leaf2 = "" + (Split-Path -Path $Root -Leaf) } catch { $leaf2 = "" }
        if (($leaf2 -ieq "docket") `
            -and (Test-Path -LiteralPath (Join-Path $Root "config.json")) `
            -and (Test-Path -LiteralPath (Join-Path $Root "loop.py"))) {
            $up = ""
            try { $up = "" + (Split-Path -Path $Root -Parent) } catch { $up = "" }
            return (New-ReadinessResult "FAIL" `
                ("-Root points at the docket WORKBENCH folder itself (" + $Root + "), not at the kit root that contains it.") `
                "" `
                ("Use: -Root " + $q + $up + $q + " -Project " + $q + $ProjName + $q))
        }
        New-ReadinessResult "FAIL" `
            ("no docket\ folder under " + $Root + ", no kit root one level down, and this is neither the workbench nor the project directory - so the kit was never extracted here.") `
            "" `
            ("Extract docket-complete-" + $script:ExpectedKitVersion + ".zip so that <Root>\docket and <Root>\package-manifest.json both exist, then place the project beside docket\.")
    }
    Invoke-Check "KIT-DOCKET-DIR" "layout" $true -RequiresPass @("KIT-ROOT-IS-PROJECT", "KIT-LAYOUT") {
        if (Test-Path -LiteralPath (Join-Path $Root "docket")) { New-ReadinessResult "PASS" ("docket\ exists under " + $Root) }
        else { New-ReadinessResult "FAIL" ("no docket\ folder under " + $Root) "" ("Extract docket-complete-" + $script:ExpectedKitVersion + ".zip so that <Root>\docket exists.") }
    }
    Invoke-Check "KIT-SIBLING" "layout" $true -RequiresPass @("KIT-DOCKET-DIR") {
        $projFull = $ProjPath
        $wbFull = $Workbench
        if (-not (Test-Path -LiteralPath $ProjPath)) {
            New-ReadinessResult "FAIL" ("project directory does not exist: " + $ProjPath) `
              "" "Copy or clone the project BESIDE docket\ (same parent folder)."
        } elseif ($projFull.StartsWith($wbFull + "\")) {
            New-ReadinessResult "FAIL" ("project is NESTED INSIDE docket\ (" + $projFull + ") - it must be a SIBLING of docket\, sharing the same parent folder") `
              "" "Move the project so the layout is <Root>\docket and <Root>\<project> side by side."
        } else {
            New-ReadinessResult "PASS" ("project is a sibling of docket\ at " + $projFull)
        }
    }
    Invoke-Check "KIT-MISPLACED-CONFIG" "layout" $false -RequiresPass @("KIT-DOCKET-DIR") {
        $rootCfg = Join-Path $Root "config.json"
        $realCfg = Join-Path $Workbench "config.json"
        if ((Test-Path -LiteralPath $rootCfg) -and (Test-Path -LiteralPath $realCfg)) {
            New-ReadinessResult "WARN" ("a stray config.json sits at " + $rootCfg + " while the real file is " + $realCfg + " - the earlier incorrect layout. Docket reads ONLY docket\config.json; the correct structure keeps config.json, ledger.py and schema.sql INSIDE docket\, with your project as a sibling folder.") `
              "" "Delete or ignore the stray root config.json."
        } else {
            New-ReadinessResult "PASS" "no misplaced root config.json"
        }
    }
    $RequiredKitFiles = @("config.json", "ledger.py", "schema.sql",
                          "loop.py", "containment.py", "model_authority.py")
    $RequiredKitDirs = @("agents", "scripts", "tickets")
    Invoke-Check "KIT-FILES" "layout" $true -RequiresPass @("KIT-DOCKET-DIR") {
        $missing = @()
        foreach ($f in $RequiredKitFiles) {
            if (-not (Test-Path -LiteralPath (Join-Path $Workbench $f))) { $missing += $f }
        }
        foreach ($d in $RequiredKitDirs) {
            if (-not (Test-Path -LiteralPath (Join-Path $Workbench $d))) { $missing += ($d + "\") }
        }
        if ($missing.Count -eq 0) { New-ReadinessResult "PASS" "all required workbench files and folders present (config.json, ledger.py, schema.sql, loop.py, containment.py, model_authority.py, agents\, scripts\, tickets\)" }
        else { New-ReadinessResult "FAIL" ("required workbench content MISSING: " + ($missing -join ", ")) `
                 "" ("Re-extract docket-workbench-" + $script:ExpectedKitVersion + ".zip / docket-complete-" + $script:ExpectedKitVersion + ".zip - do not hand-assemble the folder.") }
    }
    Invoke-Check "KIT-ZEROBYTE" "layout" $true -RequiresPass @("KIT-DOCKET-DIR") {
        $zero = @()
        foreach ($f in $RequiredKitFiles) {
            $p = Join-Path $Workbench $f
            if ((Test-Path -LiteralPath $p) -and ((Get-Item -LiteralPath $p).Length -eq 0)) { $zero += $f }
        }
        if ($zero.Count -eq 0) { New-ReadinessResult "PASS" "no required file is zero bytes" }
        else { New-ReadinessResult "FAIL" ("zero-byte required file(s): " + ($zero -join ", ")) "" "Re-extract the kit." }
    }
    $script:KitVersion = ""
    Invoke-Check "KIT-MANIFEST" "layout" $false -RequiresPass @("KIT-DOCKET-DIR") {
        # Never guess a version or a hash when the manifest is absent -
        # say which of the six layout situations this is instead.
        $man = Join-Path $Root "package-manifest.json"
        if (-not (Test-Path -LiteralPath $man)) {
            return (New-ReadinessResult "WARN" ("no package-manifest.json at " + $Root + " - version and hash verification are UNAVAILABLE (not passed). See the KIT-LAYOUT row for which layout situation this is.") `
                      "" ("Extract docket-complete-" + $script:ExpectedKitVersion + ".zip so the manifest sits beside docket\."))
        }
        $m = $null
        try {
            $m = Get-Content -LiteralPath $man -Raw | ConvertFrom-Json
        } catch {
            return (New-ReadinessResult "FAIL" ("package-manifest.json is present but does NOT parse as JSON - the extraction is corrupt: " + $_.Exception.Message) `
                      "" "Re-download and re-extract the complete zip; verify its SHA256 first.")
        }
        try { $script:KitVersion = "" + $m.extension.version } catch { $script:KitVersion = "" }
        if ($script:KitVersion -eq "") {
            return (New-ReadinessResult "FAIL" "package-manifest.json parses but carries no extension.version - the manifest is malformed" `
                      "" "Re-extract the complete zip.")
        }
        New-ReadinessResult "PASS" ("package manifest present; extension " + $m.extension.id + " " + $script:KitVersion)
    }
    Invoke-Check "KIT-VERSION" "layout" $true -RequiresPass @("KIT-DOCKET-DIR") {
        if ($script:KitVersion -eq "") {
            New-ReadinessResult "WARN" ("kit version unknown (no usable manifest) - cannot pin the expected distribution version " + $script:ExpectedKitVersion)
        } elseif ($script:KitVersion -eq $script:ExpectedKitVersion) {
            New-ReadinessResult "PASS" ("distribution version " + $script:ExpectedKitVersion + " as expected")
        } else {
            New-ReadinessResult "FAIL" ("distribution version is " + $script:KitVersion + ", expected " + $script:ExpectedKitVersion + " - the workbench and the VSIX must belong to the same release") `
              "" ("Install docket-" + $script:ExpectedKitVersion + ".vsix and extract the matching docket-complete-" + $script:ExpectedKitVersion + ".zip together. Any 0.0.2 artifact is SUPERSEDED (its checker could not run on Windows) and must not be used.")
        }
    }
    Invoke-Check "KIT-HASHES" "layout" $true -RequiresPass @("KIT-DOCKET-DIR") {
        $man = Join-Path $Root "package-manifest.json"
        if (-not (Test-Path -LiteralPath $man)) {
            return (New-ReadinessResult "SKIP" "no manifest - hash verification not possible here")
        }
        $m = $null
        try { $m = Get-Content -LiteralPath $man -Raw | ConvertFrom-Json } catch { $m = $null }
        if ($null -eq $m) {
            return (New-ReadinessResult "SKIP" "manifest does not parse - see KIT-MANIFEST")
        }
        $bad = @()
        $checked = 0
        foreach ($f in $m.files) {
            $p = Join-Path $Root ($f.path -replace "/", "\")
            if (-not (Test-Path -LiteralPath $p)) { $bad += ($f.path + " (missing)"); continue }
            $h = (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLower()
            if ($h -ne $f.sha256) { $bad += $f.path }
            $checked += 1
        }
        if ($bad.Count -eq 0) { New-ReadinessResult "PASS" ("all " + $checked + " manifest hashes match the extracted bytes") }
        else { New-ReadinessResult "FAIL" ("hash mismatch or missing: " + (($bad | Select-Object -First 8) -join ", ")) `
                 "" "The extraction is corrupt or files were edited - re-extract the zip." }
    }

    # ------------------------------------------ C. VS Code and extension
    $codeCmd = Get-Command code -ErrorAction SilentlyContinue
    Invoke-Check "VSCODE-CLI" "vscode" $false {
        if ($codeCmd) {
            $v = Run-Exe "code" @("--version")
            New-ReadinessResult "PASS" ("code CLI present: " + (($v.Output -split "`n")[0]).Trim())
        } else {
            New-ReadinessResult "WARN" "'code' is not on PATH - extension checks limited" `
              "" "In VS Code run 'Shell Command: Install ''code'' command in PATH', or inspect the Extensions view manually."
        }
    }
    $script:ExtList = ""
    if ($codeCmd) {
        $r = Run-Exe "code" @("--list-extensions", "--show-versions")
        $script:ExtList = $r.Output
    }
    Invoke-Check "VSCODE-EXT" "vscode" $false {
        if (-not $codeCmd) { return (New-ReadinessResult "SKIP" "no code CLI - check the Extensions view for 'Docket' manually") }
        if ($script:ExtList -match "(?im)^docket\.docket@") { New-ReadinessResult "PASS" "docket.docket is installed" }
        else { New-ReadinessResult "FAIL" "docket.docket is NOT installed" "" ("Install docket-" + $script:ExpectedKitVersion + ".vsix via Extensions -> ... -> Install from VSIX.") }
    }
    Invoke-Check "VSCODE-EXT-VERSION" "vscode" $false {
        if (-not $codeCmd) { return (New-ReadinessResult "SKIP" "no code CLI") }
        $m = [regex]::Match($script:ExtList, "(?im)^docket\.docket@(\S+)")
        if (-not $m.Success) { return (New-ReadinessResult "SKIP" "extension not installed") }
        $v = $m.Groups[1].Value
        if ($v -eq $script:ExpectedKitVersion) { New-ReadinessResult "PASS" ("installed extension is " + $script:ExpectedKitVersion) }
        else { New-ReadinessResult "FAIL" ("installed extension is " + $v + ", expected " + $script:ExpectedKitVersion) `
                 "" ("Uninstall the old version, then Install from VSIX with docket-" + $script:ExpectedKitVersion + ".vsix.") }
    }
    Invoke-Check "VSCODE-COPILOT" "vscode" $false {
        if (-not $codeCmd) { return (New-ReadinessResult "SKIP" "no code CLI - verify GitHub Copilot + Copilot Chat in the Extensions view") }
        $hasChat = $script:ExtList -match "(?im)^github\.copilot-chat@"
        $hasBase = $script:ExtList -match "(?im)^github\.copilot@"
        if ($hasChat -and $hasBase) { New-ReadinessResult "PASS" "GitHub Copilot and Copilot Chat are installed" }
        else { New-ReadinessResult "WARN" ("copilot=" + $hasBase + " copilot-chat=" + $hasChat + " - vscode.lm needs Copilot Chat") }
    }

    # ------------------------------------------------- D. git project
    # GIT-EXE is the single blocking row for a missing git; every
    # git-dependent check below declares it as a prerequisite and
    # becomes SKIP rather than inventing its own failure.
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    Invoke-Check "GIT-EXE" "git" $true {
        if ($gitCmd) {
            $v = Run-Exe "git" @("--version")
            New-ReadinessResult "PASS" (($v.Output -split "`n")[0]).Trim()
        } else { New-ReadinessResult "FAIL" "git is not on PATH" "" "Install Git for Windows." }
    }
    Invoke-Check "GIT-REPO" "git" $true -RequiresPass @("GIT-EXE", "KIT-SIBLING") {
        if (Test-Path -LiteralPath (Join-Path $ProjPath ".git")) {
            return (New-ReadinessResult "PASS" "project has a .git directory")
        }
        $r = Run-Exe "git" @("-C", $ProjPath, "rev-parse", "--is-inside-work-tree")
        if ($r.Code -eq 0 -and $r.Output.Trim() -eq "true") { New-ReadinessResult "PASS" "project is inside a git work tree" }
        else { New-ReadinessResult "FAIL" "project is not a git repository - Docket's checkpointing and isolation need one" `
                 "" "Clone the project as a repository (or create one and commit a baseline) BEFORE the demo." }
    }
    Invoke-Check "GIT-HEAD" "git" $true -RequiresPass @("GIT-EXE", "GIT-REPO") {
        $r = Run-Exe "git" @("-C", $ProjPath, "rev-parse", "--short", "HEAD")
        if ($r.Code -eq 0) {
            $b = Run-Exe "git" @("-C", $ProjPath, "rev-parse", "--abbrev-ref", "HEAD")
            $bn = $b.Output.Trim()
            $state = "branch " + $bn
            if ($bn -eq "HEAD") { $state = "detached HEAD" }
            New-ReadinessResult "PASS" ("HEAD resolves to " + $r.Output.Trim() + " (" + $state + ")")
        } else { New-ReadinessResult "FAIL" "HEAD does not resolve (empty repository?)" "" "Commit a baseline in the project." }
    }
    Invoke-Check "GIT-STATE" "git" $true -RequiresPass @("GIT-EXE", "GIT-REPO") {
        $gd = Join-Path $ProjPath ".git"
        $busy = @()
        foreach ($marker in @("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply")) {
            if (Test-Path -LiteralPath (Join-Path $gd $marker)) { $busy += $marker }
        }
        if ($busy.Count -eq 0) { New-ReadinessResult "PASS" "no merge/rebase/cherry-pick in progress" }
        else { New-ReadinessResult "FAIL" ("git operation in progress: " + ($busy -join ", ")) "" "Finish or abort the operation before the demo." }
    }
    Invoke-Check "GIT-CLEAN" "git" $true -RequiresPass @("GIT-EXE", "GIT-REPO") {
        $r = Run-Exe "git" @("-C", $ProjPath, "status", "--porcelain")
        if ($r.Code -ne 0) { return (New-ReadinessResult "FAIL" "git status failed") }
        $lines = @($r.Output.Trim() -split "`n" | Where-Object { $_.Trim() -ne "" })
        if ($lines.Count -eq 0) { New-ReadinessResult "PASS" "project tree is clean" }
        else {
            New-ReadinessResult "FAIL" ("project tree is DIRTY (" + $lines.Count + " entr(y/ies)) - a demo run must start clean") `
              (($lines | Select-Object -First 20) -join "; ") `
              "Commit or remove the listed files (relative names shown; the checker changes nothing itself)."
        }
    }
    Invoke-Check "GIT-VENV-IGNORED" "git" $false -RequiresPass @("GIT-EXE", "GIT-REPO") {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjPath "venv"))) { return (New-ReadinessResult "SKIP" "no venv yet") }
        $r = Run-Exe "git" @("-C", $ProjPath, "check-ignore", "venv")
        if ($r.Code -eq 0) { New-ReadinessResult "PASS" "venv\ is git-ignored (it never dirties the tree)" }
        else { New-ReadinessResult "WARN" "venv\ is NOT git-ignored - it will show as untracked and dirty the tree" `
                 "" "Add 'venv/' to the project .gitignore and commit that." }
    }

    # -------------------------------------------- E. python selection
    $VenvPy = Join-Path $ProjPath "venv\Scripts\python.exe"
    $script:Py = ""
    Invoke-Check "PY-EXISTS" "python" $true -RequiresPass @("KIT-SIBLING") {
        if (Test-Path -LiteralPath $VenvPy) {
            $script:Py = $VenvPy
            New-ReadinessResult "PASS" ("project venv python exists: " + $VenvPy)
        } else {
            $alt = Join-Path $ProjPath ".venv\Scripts\python.exe"
            if (Test-Path -LiteralPath $alt) {
                $script:Py = $alt
                New-ReadinessResult "PASS" ("project .venv python exists: " + $alt)
            } else {
                New-ReadinessResult "FAIL" ("no venv python at " + $VenvPy) `
                  "" "Recreate the venv from the demo lock: see data_project docs\DEMO-ENVIRONMENT.md."
            }
        }
    }
    Invoke-Check "PY-RESOLVE" "python" $true -RequiresPass @("PY-EXISTS") {
        # The same production order Docket uses (config pin, venv,
        # .venv). The config pin is read from docket\config.json.
        $cfgPy = $null
        try {
            $cfg = Get-Content -LiteralPath (Join-Path $Workbench "config.json") -Raw | ConvertFrom-Json
            $cfgPy = $cfg.python
        } catch {}
        if ($cfgPy) {
            if ($script:Py -ne "" -and ($cfgPy -ne $script:Py)) {
                New-ReadinessResult "WARN" ("config.json pins python to '" + $cfgPy + "' which will be used INSTEAD of the venv interpreter") `
                  "" "Set config.json python to null to auto-resolve the project venv."
            } else { New-ReadinessResult "PASS" ("config pin and venv agree: " + $cfgPy) }
        } elseif ($script:Py -ne "") {
            New-ReadinessResult "PASS" ("Docket will resolve exactly: " + $script:Py)
        } else {
            New-ReadinessResult "FAIL" "no interpreter resolvable (no pin, no venv)"
        }
    }
    Invoke-Check "PY-RUNS" "python" $true -RequiresPass @("PY-EXISTS") {
        $r = Run-Exe $script:Py @("-c", "print('ok')")
        if ($r.Code -eq 0 -and $r.Output.Trim() -eq "ok") { New-ReadinessResult "PASS" "python starts" }
        else { New-ReadinessResult "FAIL" ("python did not start: " + $r.Output.Trim()) }
    }
    Invoke-Check "PY-VERSION" "python" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])")
        $v = $r.Output.Trim()
        if ($r.Code -eq 0 -and [version]$v -ge [version]"3.10") { New-ReadinessResult "PASS" ("python " + $v) }
        else { New-ReadinessResult "FAIL" ("python version '" + $v + "' unsupported (need 3.10+)") }
    }
    Invoke-Check "PY-ARCH64" "python" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-c", "import struct; print(struct.calcsize('P') * 8)")
        if ($r.Output.Trim() -eq "64") { New-ReadinessResult "PASS" "64-bit interpreter" }
        else { New-ReadinessResult "FAIL" ("interpreter is " + $r.Output.Trim() + "-bit - native wheels (polars) need 64-bit") }
    }
    Invoke-Check "PY-SYSEXE" "python" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-c", "import sys; print(sys.executable)")
        $exe = $r.Output.Trim()
        if ($exe -ieq $script:Py) { New-ReadinessResult "PASS" "sys.executable matches the selected interpreter" }
        else { New-ReadinessResult "WARN" ("sys.executable reports '" + $exe + "'") }
    }
    Invoke-Check "PY-PREFIX" "python" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-c", "import sys; print(sys.prefix != sys.base_prefix)")
        $inVenv = $r.Output.Trim()
        $r2 = Run-Exe $script:Py @("-c", "import sys, os; print(os.path.abspath(sys.prefix))")
        $prefix = $r2.Output.Trim()
        $projFull = $ProjPath
        if ($inVenv -eq "True" -and $prefix.StartsWith($projFull)) {
            New-ReadinessResult "PASS" "sys.prefix is the project venv (not a global python)"
        } else {
            New-ReadinessResult "FAIL" ("interpreter is NOT the project venv (is_venv=" + $inVenv + ") - a global python was selected") `
              "" "Recreate the venv and let Docket auto-resolve it (config python = null)."
        }
    }
    Invoke-Check "PY-PIP" "python" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-m", "pip", "--version")
        if ($r.Code -eq 0 -and $r.Output -match [regex]::Escape($ProjName)) {
            New-ReadinessResult "PASS" "pip belongs to the same interpreter"
        } elseif ($r.Code -eq 0) {
            New-ReadinessResult "PASS" ("pip present: " + (($r.Output -split "`n")[0]).Trim())
        } else { New-ReadinessResult "FAIL" "python -m pip does not run" }
    }
    Invoke-Check "PY-PIPCHECK" "python" $false -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-m", "pip", "check")
        if ($r.Code -eq 0) { New-ReadinessResult "PASS" "pip check: no broken requirements" }
        else { New-ReadinessResult "WARN" ("pip check reports problems: " + (($r.Output -split "`n")[0]).Trim()) }
    }
    Invoke-Check "PY-PYTEST" "python" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-c", "import pytest; print(pytest.__version__)")
        if ($r.Code -eq 0) { New-ReadinessResult "PASS" ("pytest " + $r.Output.Trim()) }
        else { New-ReadinessResult "FAIL" "pytest does not import in the project venv" "" "Recreate the venv from the demo lock (docs\DEMO-ENVIRONMENT.md)." }
    }
    Invoke-Check "PY-COVERAGE" "python" $false -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $r = Run-Exe $script:Py @("-c", "import coverage; print(coverage.__version__)")
        if ($r.Code -eq 0) { New-ReadinessResult "PASS" ("coverage " + $r.Output.Trim()) }
        else { New-ReadinessResult "WARN" "coverage does not import (repo-map inversion and coverage tooling degrade)" }
    }

    # ------- F/G/H/I. runtime + parity + deps + baseline, through the
    # PRODUCTION door: loop.py --project-preflight-json (containment is
    # never duplicated here).
    $script:Preflight = $null
    $script:DirectPassed = 0
    $script:DirectSkipped = 0
    $script:DirectCollected = 0
    $script:DirectSkipLines = @()
    $script:ContainedPassed = 0
    $script:ContainedSkipped = 0
    $script:ContainedCollected = 0
    $script:ContainedSkipLines = @()
    Invoke-Check "RT-PREFLIGHT" "runtime" $true -RequiresPass @("PY-EXISTS", "PY-RUNS", "KIT-FILES") {
        $loopPy = Join-Path $Workbench "loop.py"
        if (-not (Test-Path -LiteralPath $loopPy)) { return (New-ReadinessResult "FAIL" "loop.py missing from the workbench") }
        $pfArgs = @($loopPy, "--project-preflight-json",
                    "--workbench", $Workbench,
                    "--project", $ProjName,
                    "--project-path", $ProjPath)
        if ($SkipTests) { $pfArgs += "--skip-tests" }
        $r = Run-Exe $script:Py $pfArgs
        $jsonText = $r.Output
        $start = $jsonText.IndexOf("{")
        if ($start -lt 0) {
            return (New-ReadinessResult "FAIL" ("loop.py --project-preflight-json produced no JSON: " + $jsonText.Trim().Substring(0, [math]::Min(200, $jsonText.Trim().Length))))
        }
        $script:Preflight = $jsonText.Substring($start) | ConvertFrom-Json
        New-ReadinessResult "PASS" ("project preflight ran through the production contained path; verdict " + $script:Preflight.verdict)
    }

    Map-PF "RT-OVERLAPPED" "PF-OVERLAPPED" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "RT-ASYNCIO" "PF-ASYNCIO" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "RT-SOCKET" "PF-SOCKET" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "RT-SSL" "PF-SSL" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "RT-SQLITE" "PF-SQLITE" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "RT-SUBPROCESS" "PF-SUBPROCESS" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "RT-TEMPFILE" "PF-TEMPFILE" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "RT-ENV-PARITY" "PF-ENV-PARITY" $true -RequiresPass @("RT-PREFLIGHT")
    Map-PF "DEP-MODULE" "PF-MODULE-IMPORT" $true -RequiresPass @("RT-PREFLIGHT")

    Invoke-Check "DEP-POLARS" "deps" $true -RequiresPass @("RT-PREFLIGHT") {
        $row = PF-Row "PF-DEPS"
        if ($null -eq $row) { return (New-ReadinessResult "SKIP" "preflight has no PF-DEPS row") }
        $ver = ""
        try { $ver = "" + $row.polars_version } catch {}
        if ($row.status -eq "FAIL") { return (New-ReadinessResult "FAIL" $row.detail) }
        if ($ExpectedPolars -ne "" -and $ver -ne "" -and $ver -ne $ExpectedPolars) {
            return (New-ReadinessResult "FAIL" ("polars is " + $ver + " but the demo expects exactly " + $ExpectedPolars + " (see -ExpectedPolars and the demo lock)") `
                      "" "Recreate the venv from demo-constraints.txt (docs\DEMO-ENVIRONMENT.md).")
        }
        if ($ver -ne "") { New-ReadinessResult "PASS" ("polars " + $ver + " (contained)") }
        else { New-ReadinessResult $row.status $row.detail }
    }
    Invoke-Check "DEP-POLARS-FUNC" "deps" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        $code = "import codecs, os, tempfile`n" +
                "import polars as pl`n" +
                "df = pl.DataFrame({'a': [1, 2], 'b': ['x', 'y']})`n" +
                "fd, p = tempfile.mkstemp(suffix='.csv'); os.close(fd)`n" +
                "df.write_csv(p)`n" +
                "back = pl.read_csv(p)`n" +
                "os.unlink(p)`n" +
                "assert back.shape == (2, 2)`n" +
                "codecs.lookup('utf-8'); codecs.lookup('latin-1')`n" +
                "print('polars-func-ok')"
        $r = Run-Exe $script:Py @("-c", $code)
        if ($r.Output -match "polars-func-ok") { New-ReadinessResult "PASS" "polars DataFrame + CSV round-trip + codec lookup work" }
        else { New-ReadinessResult "FAIL" ("polars functional probe failed: " + (($r.Output -split "`n") | Select-Object -Last 2) -join " ") }
    }

    Invoke-Check "BASE-DIRECT" "baseline" $true -RequiresPass @("PY-EXISTS", "PY-RUNS") {
        if ($SkipTests) { return (New-ReadinessResult "SKIP" "-SkipTests: baseline not executed") }
        $r = Run-Exe $script:Py @("-m", "pytest", "-o", 'addopts=', "-q", "-ra", '--tb=short') $ProjPath
        $passed = 0; $failed = 0; $errors = 0; $skipped = 0
        $m = [regex]::Match($r.Output, "(\d+) passed")
        if ($m.Success) { $passed = [int]$m.Groups[1].Value }
        $m2 = [regex]::Match($r.Output, "(\d+) failed")
        if ($m2.Success) { $failed = [int]$m2.Groups[1].Value }
        $m3 = [regex]::Match($r.Output, "(\d+) error")
        if ($m3.Success) { $errors = [int]$m3.Groups[1].Value }
        $m4 = [regex]::Match($r.Output, "(\d+) skipped")
        if ($m4.Success) { $skipped = [int]$m4.Groups[1].Value }
        $collected = $passed + $failed + $errors + $skipped
        $skipLines = @($r.Output -split "`n" |
                       Where-Object { $_.Trim().StartsWith("SKIPPED") } |
                       ForEach-Object { $_.Trim() })
        $script:DirectPassed = $passed
        $script:DirectSkipped = $skipped
        $script:DirectCollected = $collected
        $script:DirectSkipLines = $skipLines
        $tail = (($r.Output.Trim() -split "`n") | Select-Object -Last 3) -join " | "
        $counts = ("" + $passed + " passed, " + $failed + " failed, " +
                   $errors + " error(s), " + $skipped + " skipped, " +
                   $collected + " collected")
        if ($failed -gt 0) {
            return (New-ReadinessResult "FAIL" ("direct baseline red: " + $counts + ", exit " + $r.Code) $tail)
        }
        if ($errors -gt 0) {
            return (New-ReadinessResult "FAIL" ("direct baseline has " + $errors + " collection/setup error(s) (" + $failed + " failed) - an environment/test-harness condition, not test failures [" + $counts + "]") $tail)
        }
        if ($ExpectedTests -gt 0 -and $collected -ne $ExpectedTests) {
            return (New-ReadinessResult "FAIL" ("direct baseline COLLECTED " + $collected + ", expected " + $ExpectedTests + " - tests vanished or collection changed [" + $counts + "]") $tail `
                      "Compare the test tree against the demo commit; do not lower -ExpectedTests to make this pass.")
        }
        if ($ExpectedPassed -gt 0 -and $passed -ne $ExpectedPassed) {
            return (New-ReadinessResult "FAIL" ("direct baseline passed " + $passed + ", expected exactly " + $ExpectedPassed + " [" + $counts + "]") $tail)
        }
        New-ReadinessResult "PASS" ("direct baseline: " + $counts)
    }
    Invoke-Check "BASE-CONTAINED" "baseline" $true -RequiresPass @("RT-PREFLIGHT") {
        if ($SkipTests) { return (New-ReadinessResult "SKIP" "-SkipTests: baseline not executed") }
        $row = PF-Row "PF-BASELINE"
        if ($null -eq $row) { return (New-ReadinessResult "SKIP" "preflight has no PF-BASELINE row") }
        try { $script:ContainedPassed = [int]$row.passed } catch {}
        try { $script:ContainedSkipped = [int]$row.skipped } catch {}
        try { $script:ContainedCollected = [int]$row.collected } catch {}
        try { $script:ContainedSkipLines = @($row.skip_lines) } catch {}
        if ($row.status -ne "PASS") {
            $direct = $false
            try { $direct = ($script:DirectPassed -gt 0) } catch {}
            if ($direct) {
                return (New-ReadinessResult "FAIL" ("DOCKET CONTAINMENT DEFECT - the baseline passes directly but fails contained: " + $row.detail))
            }
            return (New-ReadinessResult "FAIL" $row.detail)
        }
        $counts = ("" + $script:ContainedPassed + " passed, " +
                   $script:ContainedSkipped + " skipped, " +
                   $script:ContainedCollected + " collected")
        if ($ExpectedTests -gt 0 -and $script:ContainedCollected -ne $ExpectedTests) {
            return (New-ReadinessResult "FAIL" ("contained baseline COLLECTED " + $script:ContainedCollected + ", expected " + $ExpectedTests + " [" + $counts + "]"))
        }
        if ($ExpectedPassed -gt 0 -and $script:ContainedPassed -ne $ExpectedPassed) {
            return (New-ReadinessResult "FAIL" ("contained baseline passed " + $script:ContainedPassed + ", expected exactly " + $ExpectedPassed + " [" + $counts + "]"))
        }
        New-ReadinessResult "PASS" ("contained baseline: " + $counts)
    }
    Invoke-Check "BASE-SKIPS" "baseline" $true -RequiresPass @("BASE-DIRECT", "BASE-CONTAINED") {
        # THE DEMO CONTRACT: a demo-ready machine RUNS every collected
        # test. -AllowedSkip does NOT buy a READY verdict - it only
        # changes an unexplained skip into a declared one, so a
        # Java-less machine is useful for diagnosis while still being
        # reported as not demo-ready. Skips block either way.
        if ($SkipTests) { return (New-ReadinessResult "SKIP" "-SkipTests: baseline not executed") }
        $all = @()
        try { $all += $script:DirectSkipLines } catch {}
        try { $all += $script:ContainedSkipLines } catch {}
        $all = @($all | Where-Object { $_ -and ("" + $_).Trim() -ne "" } | Select-Object -Unique)
        if ($all.Count -eq 0) {
            return (New-ReadinessResult "PASS" "no tests were skipped in either run - every collected test actually executed")
        }
        $unaccepted = @()
        foreach ($line in $all) {
            $okSkip = $false
            foreach ($pat in $AllowedSkip) {
                if ($pat -and ("" + $line).Contains($pat)) { $okSkip = $true; break }
            }
            if (-not $okSkip) { $unaccepted += $line }
        }
        $evidence = ($all | Select-Object -First 5) -join " | "
        if ($unaccepted.Count -gt 0) {
            return (New-ReadinessResult "FAIL" ("" + $unaccepted.Count + " skipped test group(s) are NOT explicitly accepted - a skipped test is an UNVERIFIED code path, never a pass") `
                      $evidence `
                      "Make them run on this machine: the Spark tests skip when no local Spark session can start, which means no JDK is installed. Install the same x64 JDK the physical demo machine uses (pyspark 4.x supports Java 17 or 21), then re-run.")
        }
        New-ReadinessResult "FAIL" ("" + $all.Count + " skipped test group(s) were explicitly accepted via -AllowedSkip, so this run is DIAGNOSTICALLY useful - but a demo-ready machine must RUN every collected test, so it cannot be WINDOWS DEMO READY") `
          $evidence `
          "Install the same x64 JDK as the physical demo machine (pyspark 4.x supports Java 17 or 21) so the Spark tests execute, then re-run with -ExpectedPassed equal to -ExpectedTests and no -AllowedSkip."
    }
    Invoke-Check "BASE-MATCH" "baseline" $true -RequiresPass @("BASE-DIRECT", "BASE-CONTAINED") {
        # The clause that matters for Docket: direct and contained must
        # agree on the interpreter, the pass count AND the skip set. A
        # test that passes directly and skips contained is not an
        # accepted skip - it is a containment defect.
        if ($SkipTests) { return (New-ReadinessResult "SKIP" "-SkipTests: baseline not executed") }
        if ($null -eq $script:Preflight) { return (New-ReadinessResult "SKIP" "preflight did not run") }
        $pfPy = "" + $script:Preflight.python
        $sameExe = ($pfPy -ieq $script:Py)
        $dp = 0; $cp = 0; $ds = 0; $cs = 0
        try { $dp = [int]$script:DirectPassed } catch {}
        try { $cp = [int]$script:ContainedPassed } catch {}
        try { $ds = [int]$script:DirectSkipped } catch {}
        try { $cs = [int]$script:ContainedSkipped } catch {}
        if (-not $sameExe) {
            return (New-ReadinessResult "FAIL" ("interpreter mismatch: direct used " + $script:Py + ", contained preflight used " + $pfPy))
        }
        if ($cp -lt $dp -or $cs -gt $ds) {
            return (New-ReadinessResult "FAIL" ("DOCKET CONTAINMENT DEFECT - the contained run is weaker than the direct one (direct " + $dp + " passed / " + $ds + " skipped, contained " + $cp + " passed / " + $cs + " skipped). A test that runs directly and skips under containment means the contained child was denied something it needs - Docket's sanitized environment, not the project, is at fault.") `
                      "" "Compare the PF-ENV-PARITY row: a variable present directly and absent contained is the usual cause.")
        }
        if ($dp -ne $cp -or $ds -ne $cs) {
            return (New-ReadinessResult "FAIL" ("direct and contained disagree (direct " + $dp + " passed / " + $ds + " skipped, contained " + $cp + " passed / " + $cs + " skipped)"))
        }
        $dSet = (@($script:DirectSkipLines) | Sort-Object) -join "||"
        $cSet = (@($script:ContainedSkipLines) | Sort-Object) -join "||"
        if ($dSet -ne $cSet -and ($ds -gt 0 -or $cs -gt 0)) {
            return (New-ReadinessResult "WARN" "direct and contained skip the same NUMBER of tests but their skip summary text differs - check the two reports before trusting the match")
        }
        New-ReadinessResult "PASS" ("direct and contained agree: same interpreter, " + $dp + " passed, " + $ds + " skipped, identical skip set")
    }

    # -------------------------------------------- J. docket configuration
    $script:Cfg = $null
    Invoke-Check "CFG-JSON" "config" $true -RequiresPass @("KIT-DOCKET-DIR", "KIT-FILES") {
        $script:Cfg = Get-Content -LiteralPath (Join-Path $Workbench "config.json") -Raw | ConvertFrom-Json
        New-ReadinessResult "PASS" "config.json parses as JSON"
    }
    Invoke-Check "CFG-PYTHON" "config" $false -RequiresPass @("CFG-JSON") {
        $pin = $null
        try { $pin = $script:Cfg.python } catch {}
        if ($null -eq $pin) { New-ReadinessResult "PASS" "config python is null (auto-resolve the project venv - correct for the demo)" }
        elseif (("" + $pin) -ieq $script:Py) { New-ReadinessResult "PASS" "config python pin equals the selected venv interpreter" }
        else { New-ReadinessResult "WARN" ("config pins python to '" + $pin + "', which contradicts the selected venv '" + $script:Py + "'") }
    }
    Invoke-Check "CFG-ISOLATION" "config" $false -RequiresPass @("CFG-JSON") {
        $mode = $null
        try { $mode = $script:Cfg.workflow.isolation } catch {}
        if ($null -eq $mode) { New-ReadinessResult "PASS" "workflow isolation not configured (default applies)" }
        elseif (@("worktree", "shared") -contains ("" + $mode)) { New-ReadinessResult "PASS" ("workflow isolation: " + $mode) }
        else { New-ReadinessResult "FAIL" ("invalid workflow isolation value: " + $mode) }
    }
    Invoke-Check "CFG-TOKENCAP" "config" $false -RequiresPass @("CFG-JSON") {
        $cap = $null
        try { $cap = $script:Cfg.governor.max_tokens_per_run } catch {}
        if ($null -ne $cap -and [double]$cap -gt 0) { New-ReadinessResult "PASS" ("token cap " + $cap) }
        elseif ($null -ne $cap -and [double]$cap -eq 0) { New-ReadinessResult "WARN" "token cap 0 = brake disabled (configured)" }
        else { New-ReadinessResult "WARN" "no token cap configured" }
    }
    Invoke-Check "CFG-MODELS" "config" $false -RequiresPass @("CFG-JSON") {
        $bad = @()
        try {
            foreach ($p in $script:Cfg.models.PSObject.Properties) {
                if ($null -ne $p.Value -and ("" + $p.Value).Trim() -eq "") { $bad += $p.Name }
            }
        } catch {}
        if ($bad.Count -eq 0) { New-ReadinessResult "PASS" "model role pins are syntactically valid (null = auto-resolve)" }
        else { New-ReadinessResult "FAIL" ("empty model pin for role(s): " + ($bad -join ", ")) }
    }
    Invoke-Check "CFG-GATES" "config" $false -RequiresPass @("CFG-JSON") {
        $has = $false
        try { $has = ($null -ne $script:Cfg.gates.comprehension) } catch {}
        if ($has) { New-ReadinessResult "PASS" "gate configuration present (comprehension et al.)" }
        else { New-ReadinessResult "FAIL" "gates section missing or empty" }
    }
    Invoke-Check "CFG-SCHEMA-TEMP" "config" $true -RequiresPass @("PY-EXISTS", "PY-RUNS", "KIT-FILES") {
        $schema = Join-Path $Workbench "schema.sql"
        if (-not (Test-Path -LiteralPath $schema)) { return (New-ReadinessResult "FAIL" "schema.sql missing - cannot verify schema") }
        $code = "import sqlite3, sys, tempfile, os`n" +
                "fd, p = tempfile.mkstemp(suffix='.db'); os.close(fd)`n" +
                "con = sqlite3.connect(p)`n" +
                "con.executescript(open(sys.argv[1], encoding='utf-8').read())`n" +
                "con.close(); os.unlink(p)`n" +
                "print('schema-ok')"
        $r = Run-Exe $script:Py @("-c", $code, $schema)
        if ($r.Output -match "schema-ok") { New-ReadinessResult "PASS" "schema.sql initializes a temporary database (the real ledger is untouched)" }
        else { New-ReadinessResult "FAIL" ("schema init failed: " + (($r.Output -split "`n") | Select-Object -Last 2) -join " ") }
    }
    Invoke-Check "CFG-SECURITY-STATE" "config" $false -RequiresPass @("CFG-JSON") {
        $enabled = $null
        try { $enabled = $script:Cfg.gates.security_snyk.enabled } catch {}
        if ($enabled -eq $true) { New-ReadinessResult "PASS" "security gate enabled" }
        else { New-ReadinessResult "WARN" "security gate is DISABLED in config (a configured state, reported - never silently a pass)" }
    }

    # ------------------------------------------------------ K. tickets
    $TicketsDir = Join-Path $Workbench "tickets"
    Invoke-Check "TICKET-EXISTS" "tickets" $true -RequiresPass @("KIT-DOCKET-DIR", "KIT-FILES") {
        $runnable = @()
        if (Test-Path -LiteralPath $TicketsDir) {
            $runnable = @(Get-ChildItem -LiteralPath $TicketsDir -Filter "*.md" |
                          Where-Object { -not $_.Name.StartsWith("_") })
        }
        if ($runnable.Count -gt 0) { New-ReadinessResult "PASS" ($runnable.Count.ToString() + " runnable ticket file(s)") }
        else { New-ReadinessResult "FAIL" "no runnable ticket (files starting with _ are templates and are ignored)" `
                 "" "Copy tickets\_template.md to a real name such as DATACMP-0.md and fill it in." }
    }
    Invoke-Check "TICKET-UNDERSCORE" "tickets" $false -RequiresPass @("KIT-DOCKET-DIR") {
        if (Test-Path -LiteralPath (Join-Path $TicketsDir "_template.md")) {
            New-ReadinessResult "PASS" "_template.md present and correctly underscore-ignored"
        } else { New-ReadinessResult "WARN" "_template.md missing (harmless if a real ticket exists)" }
    }
    Invoke-Check "TICKET-DATACMP0" "tickets" $true -RequiresPass @("KIT-DOCKET-DIR", "TICKET-EXISTS") {
        $t = Join-Path $TicketsDir "DATACMP-0.md"
        if ((Test-Path -LiteralPath $t) -and ((Get-Item -LiteralPath $t).Length -gt 0)) {
            New-ReadinessResult "PASS" "DATACMP-0.md exists and is non-empty (the demo ticket)"
        } else { New-ReadinessResult "FAIL" "tickets\DATACMP-0.md missing or empty - the demo needs it" }
    }
    Invoke-Check "TICKET-AC" "tickets" $true -RequiresPass @("TICKET-DATACMP0") {
        $t = Join-Path $TicketsDir "DATACMP-0.md"
        $body = Get-Content -LiteralPath $t -Raw
        if ($body -match "(?i)acceptance criteria") { New-ReadinessResult "PASS" "acceptance criteria found in DATACMP-0.md" }
        else { New-ReadinessResult "FAIL" "no 'Acceptance Criteria' section found in the demo ticket" }
    }
    Invoke-Check "TICKET-NOJIRA" "tickets" $false {
        New-ReadinessResult "PASS" "'Run Ticket From File' reads tickets from disk - no Jira credentials are required for the demo"
    }

    # ------------------------------------------- L. isolation readiness
    Invoke-Check "ISO-HEAD" "isolation" $true -RequiresPass @("GIT-EXE", "GIT-REPO") {
        $r = Run-Exe "git" @("-C", $ProjPath, "rev-parse", "HEAD")
        if ($r.Code -eq 0) { New-ReadinessResult "PASS" "repository HEAD is valid" }
        else { New-ReadinessResult "FAIL" "repository has no valid HEAD" }
    }
    Invoke-Check "ISO-MODE" "isolation" $false -RequiresPass @("CFG-JSON") {
        $mode = $null
        try { $mode = $script:Cfg.workflow.isolation } catch {}
        if ($null -eq $mode -or @("worktree", "shared") -contains ("" + $mode)) {
            New-ReadinessResult "PASS" "configured isolation mode is valid"
        } else { New-ReadinessResult "FAIL" ("invalid isolation mode: " + $mode) }
    }
    Invoke-Check "ISO-WRITE" "isolation" $true -RequiresPass @("KIT-DOCKET-DIR") {
        $cacheParent = Join-Path $Workbench "cache"
        $target = $Workbench
        if (Test-Path -LiteralPath $cacheParent) { $target = $cacheParent }
        if (Probe-Writable $target) { New-ReadinessResult "PASS" ("worktree/cache parent writable (probe in " + (Split-Path -Path $target -Leaf) + ")") }
        else { New-ReadinessResult "FAIL" ("cannot write under " + $target) }
    }
    Invoke-Check "ISO-WORKTREE" "isolation" $true -RequiresPass @("GIT-EXE", "GIT-REPO") {
        $r = Run-Exe "git" @("-C", $ProjPath, "worktree", "list")
        if ($r.Code -eq 0) { New-ReadinessResult "PASS" "git supports worktree (list works; nothing was created)" }
        else { New-ReadinessResult "FAIL" "git worktree list failed" }
    }
    Invoke-Check "ISO-LOCKS" "isolation" $true -RequiresPass @("KIT-SIBLING") {
        $gd = Join-Path $ProjPath ".git"
        $stale = @()
        foreach ($lock in @("index.lock", "HEAD.lock", "MERGE_HEAD")) {
            if (Test-Path -LiteralPath (Join-Path $gd $lock)) { $stale += $lock }
        }
        if ($stale.Count -eq 0) { New-ReadinessResult "PASS" "no stale lock files or unfinished git operations" }
        else { New-ReadinessResult "FAIL" ("stale git state present: " + ($stale -join ", ")) `
                 "" "Close other git tools; remove the stale lock yourself if you are sure nothing is running (the checker changes nothing)." }
    }

    # ------------------------------------ M. the live-model limitation
    Invoke-Check "MANUAL-PROBE" "manual" $false {
        New-ReadinessResult "WARN" "REQUIRED MANUAL STEP - this checker cannot prove Copilot model consent/quota. In VS Code run 'Docket: Run Preflight Probe' (it now includes the PROJECT-RUNTIME section) and require zero blockers BEFORE any live ticket."
    }

    # ----------------------------------- ledger untouched, end to end
    Invoke-Check "CFG-LEDGER-UNTOUCHED" "config" $true {
        if ($LedgerHashBefore -eq "") {
            return (New-ReadinessResult "PASS" "no ledger.db exists yet - nothing to touch (a first run will create it)")
        }
        $after = (Get-FileHash -Algorithm SHA256 -LiteralPath $LedgerPath).Hash
        if ($after -eq $LedgerHashBefore) { New-ReadinessResult "PASS" "ledger.db byte-identical before and after every check" }
        else { New-ReadinessResult "FAIL" "ledger.db CHANGED during the readiness run - investigate before trusting anything" }
    }

    # ------------------------------------------------ N. verdict + report
    $blockers = @($script:Checks | Where-Object { $_.Status -eq "FAIL" -and $_.Blocking })
    # A BLOCKING check that never ran because its prerequisite failed is
    # UNPROVEN, not passed. Silence must never buy WINDOWS DEMO READY.
    $unproven = @($script:Checks | Where-Object { $_.Blocking -and $_.SkippedByPrerequisite })
    $verdict = "WINDOWS DEMO READY"
    if ($blockers.Count -gt 0 -or $unproven.Count -gt 0) { $verdict = "WINDOWS DEMO BLOCKED" }

    # -OutDir may be unwritable (an unsupported or read-only root). The
    # verdict still has to reach the operator, so fall back to %TEMP%
    # and say so - never turn a measured finding into a crash.
    $reportDir = $OutDir
    $outNote = ""
    $dirOk = $true
    try {
        if (-not (Test-Path -LiteralPath $reportDir)) {
            New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
        }
    } catch { $dirOk = $false }
    if ($dirOk -and -not (Probe-Writable $reportDir)) { $dirOk = $false }
    if (-not $dirOk) {
        $outNote = "the requested -OutDir (" + $OutDir + ") could not be written, so the reports went to the fallback location below"
        $reportDir = Join-Path ([System.IO.Path]::GetTempPath()) "docket-preflight-results"
        if (-not (Test-Path -LiteralPath $reportDir)) {
            New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
        }
    }

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $txtLines = @()
    $txtLines += ("DOCKET WINDOWS READINESS - " + $stamp)
    $txtLines += ("checker version: " + $script:ExpectedKitVersion)
    $txtLines += ("root: " + $Root)
    if ($script:RootInput -ne $Root) {
        $txtLines += ("root as supplied: " + $script:RootInput + " (normalized to a native path)")
    }
    $txtLines += ("workbench: " + $Workbench)
    $txtLines += ("project: " + $ProjName + " (" + $ProjPath + ")")
    $txtLines += ""
    foreach ($c in $script:Checks) {
        $flags = ""
        if ($c.Blocking) { $flags = ", blocking" }
        if ($c.SkippedByPrerequisite) { $flags = $flags + ", not run" }
        $txtLines += ("[" + $c.Status.PadRight(4) + "] " + $c.Id + " (" + $c.Category + ", " + $c.DurationMs + "ms" + $flags + ")")
        foreach ($dl in (("" + $c.Detail) -split "`n")) { $txtLines += ("        " + $dl.TrimEnd()) }
        if ($c.Evidence -ne "") { $txtLines += ("        evidence: " + $c.Evidence) }
        if ($c.Status -ne "PASS" -and $c.Fix -ne "") {
            $fixLines = @(("" + $c.Fix) -split "`n")
            $txtLines += ("        fix: " + $fixLines[0].TrimEnd())
            for ($fi = 1; $fi -lt $fixLines.Count; $fi++) {
                $txtLines += ("             " + $fixLines[$fi].TrimEnd())
            }
        }
    }
    $txtLines += ""
    $txtLines += ("VERDICT: " + $verdict)
    if ($blockers.Count -gt 0 -or $unproven.Count -gt 0) {
        $txtLines += ""
        $txtLines += "REMEDIATION (ordered, blocking failures only):"
        $i = 1
        foreach ($b in $blockers) {
            $fixText = $b.Fix
            if ($fixText -eq "") { $fixText = $b.Detail }
            foreach ($fl in (("" + $fixText) -split "`n")) {
                if ($fl.Trim() -eq "") { continue }
                if ($fl -eq (("" + $fixText) -split "`n")[0]) {
                    $txtLines += ("  " + $i + ". [" + $b.Id + "] " + $fl.TrimEnd())
                } else {
                    $txtLines += ("       " + $fl.TrimEnd())
                }
            }
            $i += 1
        }
        if ($unproven.Count -gt 0) {
            $txtLines += ""
            $txtLines += ("  " + $unproven.Count + " further blocking check(s) never ran because a " +
                          "prerequisite above failed. They are UNPROVEN, not passed, so this " +
                          "machine cannot be called ready until the failure(s) above are fixed " +
                          "and the checker is run again.")
        }
    } else {
        $txtLines += ""
        $txtLines += "Next steps, exactly:"
        $txtLines += "  1. In VS Code: 'Docket: Run Preflight Probe' (the required manual model check)."
        $txtLines += "  2. Docket Hub -> Run with Overrides ->"
        $txtLines += "       Risk Profile: Medium ->"
        $txtLines += "       Run Ticket From File -> DATACMP-0"
    }
    if ($outNote -ne "") {
        $txtLines += ""
        $txtLines += ("NOTE: " + $outNote)
    }

    $txtPath = Join-Path $reportDir "windows-readiness.txt"
    $jsonPath = Join-Path $reportDir "windows-readiness.json"
    $txtLines | Set-Content -Path $txtPath -Encoding ASCII
    $reportObj = New-Object PSObject -Property @{
        Schema = "docket.windows_readiness.v1"
        Generated = $stamp
        CheckerVersion = $script:ExpectedKitVersion
        Root = $Root
        RootAsSupplied = $script:RootInput
        Workbench = $Workbench
        Project = $ProjName
        ProjectPath = $ProjPath
        Verdict = $verdict
        Blockers = $blockers.Count
        Unproven = $unproven.Count
        Checks = $script:Checks
    }
    $reportObj | ConvertTo-Json -Depth 6 | Set-Content -Path $jsonPath -Encoding ASCII

    # SHA256 sidecars, so the VM can prove the report it reads is the
    # report the checker wrote.
    $txtSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $txtPath).Hash.ToLower()
    ($txtSha + "  windows-readiness.txt") | Set-Content -Path ($txtPath + ".sha256") -Encoding ASCII
    $jsonSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $jsonPath).Hash.ToLower()
    ($jsonSha + "  windows-readiness.json") | Set-Content -Path ($jsonPath + ".sha256") -Encoding ASCII

    foreach ($line in $txtLines) { Write-Host $line }
    Write-Host ""
    Write-Host ("report (txt):  " + $txtPath)
    Write-Host ("  sha256 " + $txtSha)
    Write-Host ("report (json): " + $jsonPath)
    Write-Host ("  sha256 " + $jsonSha)

    if ($blockers.Count -gt 0 -or $unproven.Count -gt 0) { exit 1 }
    exit 0

} catch {
    Write-Host ("READINESS CHECKER CRASHED: " + $_.Exception.Message)
    Write-Host ($_.ScriptStackTrace)
    exit 2
}
