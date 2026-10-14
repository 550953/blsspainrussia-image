# gen_batch.ps1 - N email-template generations (optionally in parallel) -> dashboard in console -> one zip -> Explorer opens with the zip selected.
# ASCII only, Windows PowerShell 5 compatible (no ForEach-Object -Parallel).
#
# Config: -Base / TEMPLATE_BASE_URL (env or .env), key from TEMPLATE_API_KEYS (or TEMPLATE_API_KEY).
# The model is NOT read from .env any more: default is gemini-3.5-flash-lite (the stable one).
# Use -Model "name" to change it, or -Model "" to let the server choose.
#
# Typical runs:
#   powershell -ExecutionPolicy Bypass -File .\gen_batch.ps1
#   powershell -ExecutionPolicy Bypass -File .\gen_batch.ps1 -Count 10 -Parallel 10 -CurrencyMethod 4
#   powershell -ExecutionPolicy Bypass -File .\gen_batch.ps1 -Parallel 1          # one by one (baseline)
#
# What it does:
#   1. submits jobs keeping up to -Parallel of them active; a 429 (queue full / rate limit) is waited out and retried
#   2. polls every job (html=0), saves html / text / json for each finished job
#   3. samples /health every few seconds (keys ready/in_use/cooldown, OCR pending/running, latency) -> health_log.csv
#   4. prints a DASHBOARD in the console (bars, per-job grid, rule charts), saves it to summary.txt, writes summary.csv
#   5. zips the whole run folder, opens Explorer with the zip selected
# Server defaults to keep in mind: TEMPLATE_MAX_CONCURRENCY=3 (really parallel), TEMPLATE_MAX_PENDING=6 (queue + running).
# With -Parallel 10 the script submits all 10 immediately; the server accepts up to TEMPLATE_MAX_PENDING, the rest get 429 and are re-sent every few seconds.
param(
    [string]$Base = "",
    [string]$Model = "gemini-3.5-flash-lite",
    [int]$Count = 10,                 # how many generations
    [int]$Parallel = 10,              # how many jobs are in flight at once (default 10 = all at once; 1 = sequential)
    [int]$CurrencyMethod = 4,         # 1..20, 0 = let the server choose
    [int]$MaxWait = 1800,             # overall limit for the whole run, seconds
    [int]$HealthEvery = 5,            # /health sampling period, seconds
    [switch]$NoOpen                   # do not open Explorer at the end
)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# ------------------------------------------------------------------ helpers
function Invoke-Api {
    param([string]$Method, [string]$Path, [hashtable]$Headers, [string]$Body = "", [int]$Timeout = 60)
    $req = @{ Uri = "$Base$Path"; Method = $Method; Headers = $Headers; UseBasicParsing = $true; TimeoutSec = $Timeout }
    if ($Body) { $req.Body = $Body; $req.ContentType = "application/json" }
    try {
        $resp = Invoke-WebRequest @req
        $text = [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
        return @{ Ok = $true; Code = [int]$resp.StatusCode; Json = ($text | ConvertFrom-Json); Text = $text; RetryAfter = 0 }
    } catch {
        $code = 0
        $text = $_.Exception.Message
        $retry = 0
        if ($_.Exception.Response) {
            $code = [int]$_.Exception.Response.StatusCode
            try { $ra = $_.Exception.Response.Headers["Retry-After"]; if ($ra) { $retry = [int]$ra } } catch {}
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $text = $_.ErrorDetails.Message
            } else {
                try {
                    $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream(), [Text.Encoding]::UTF8)
                    $text = $sr.ReadToEnd()
                } catch {}
            }
        }
        return @{ Ok = $false; Code = $code; Json = $null; Text = $text; RetryAfter = $retry }
    }
}
function Write-Utf8 {
    # UTF-8 WITHOUT BOM (Windows PowerShell 5 Set-Content -Encoding UTF8 adds a BOM).
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}
function Get-DotEnv {
    $map = @{}
    $files = @()
    if ($PSScriptRoot) { $files += (Join-Path $PSScriptRoot ".env") }
    $files += (Join-Path (Get-Location).Path ".env")
    foreach ($f in $files) {
        if (-not (Test-Path $f)) { continue }
        foreach ($line in Get-Content -Path $f -Encoding UTF8) {
            $l = $line.Trim()
            if (-not $l -or $l.StartsWith("#")) { continue }
            if ($l.StartsWith("export ")) { $l = $l.Substring(7).Trim() }
            $i = $l.IndexOf("=")
            if ($i -lt 1) { continue }
            $k = $l.Substring(0, $i).Trim()
            $v = $l.Substring($i + 1).Trim()
            if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) {
                $v = $v.Substring(1, $v.Length - 2)
            }
            if (-not $map.ContainsKey($k)) { $map[$k] = $v }
        }
    }
    return $map
}
function Find-Cfg {
    param([string[]]$Names)
    foreach ($n in $Names) {
        $v = [Environment]::GetEnvironmentVariable($n)
        if ($v -and $v.Trim()) { return @{ Value = $v.Trim(); Source = ("environment " + $n) } }
        if ($dot.ContainsKey($n) -and $dot[$n]) { return @{ Value = [string]$dot[$n]; Source = (".env " + $n) } }
    }
    return @{ Value = ""; Source = "" }
}
function Get-GroupCount {
    param($Group)
    if ($null -eq $Group) { return 0 }
    if ($Group -is [array]) { return @($Group).Count }
    $n = 0
    foreach ($p in $Group.PSObject.Properties) { $n += @($p.Value).Count }
    return $n
}
function Write-Log {
    param([string]$Text, [string]$Color = "")
    $line = ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Text)
    if ($Color) { Write-Host $line -ForegroundColor $Color } else { Write-Host $line }
    if ($script:LogPath) { Add-Content -Path $script:LogPath -Value $line -Encoding UTF8 }
}
function Join-Rules {
    param($Items)
    $r = @()
    foreach ($i in @($Items)) { if ($i -and $i.rule) { $r += [string]$i.rule } }
    return (($r | Select-Object -Unique) -join ",")
}
function Get-Bar {
    # ASCII bar: Value/Max of Width characters.
    param([double]$Value, [double]$Max, [int]$Width = 20)
    $n = 0
    if ($Max -gt 0) { $n = [int][Math]::Round($Width * $Value / $Max) }
    if ($n -gt $Width) { $n = $Width }
    if ($n -lt 0) { $n = 0 }
    return (("#" * $n) + ("." * ($Width - $n)))
}
function Get-Pct {
    param([double]$A, [double]$B)
    if ($B -le 0) { return 0 }
    return [int][Math]::Round(100.0 * $A / $B)
}
function Cut {
    param([string]$S, [int]$N)
    if ($null -eq $S) { $S = "" }
    if ($S.Length -gt $N) { return ($S.Substring(0, $N - 1) + "~") }
    return $S
}

# ------------------------------------------------------------------ config
$dot = Get-DotEnv
if (-not $Base) { $Base = (Find-Cfg @("TEMPLATE_BASE_URL")).Value }
if (-not $Base) { $Base = "https://bls.shikinn.com" }
$Base = $Base.TrimEnd("/")
$kc = Find-Cfg @("TEMPLATE_API_KEY", "TEMPLATE_API_KEYS")
$key = $kc.Value
if ($key) { $key = ($key -split ",")[0].Trim() }
if (-not $key) { $key = Read-Host "TEMPLATE_API_KEYS not found in environment or .env. Enter the key" }
if ($Count -lt 1) { $Count = 1 }
if ($Parallel -lt 1) { $Parallel = 1 }
$h = @{ "X-API-Key" = $key }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$outName = "gen_out_$stamp"
$out = Join-Path $root $outName
New-Item -ItemType Directory -Path $out | Out-Null
$script:LogPath = Join-Path $out "run_log.txt"
$healthPath = Join-Path $out "health_log.csv"
Add-Content -Path $healthPath -Encoding UTF8 -Value "time,elapsed_s,http,health_ms,keys_ready,keys_in_use,keys_cooldown,ocr_pending,ocr_running,tpl_pending,tpl_running,active_jobs"

$cmLabel = if ($CurrencyMethod -gt 0) { "$CurrencyMethod" } else { "(server choice)" }
$modelLabel = if ($Model) { $Model } else { "(server choice)" }
Write-Log ("Base={0} Model={1} Count={2} Parallel={3} CurrencyMethod={4}" -f $Base, $modelLabel, $Count, $Parallel, $cmLabel)
Write-Log ("Output folder: {0}" -f $out)

# ------------------------------------------------------------------ health sampling
$hs = @{ Samples = 0; Fails = 0; MaxMs = 0; Ms = @(); MaxCooldown = 0; MinReady = 9999; MaxOcrPending = 0; MaxOcrRunning = 0 }
function Sample-Health {
    param([int]$ActiveJobs)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-Api -Method GET -Path "/health" -Headers @{} -Timeout 20
    $sw.Stop()
    $ms = [int]$sw.ElapsedMilliseconds
    $el = [int]((Get-Date) - $tStart).TotalSeconds
    $hs.Samples++
    if (-not $r.Ok) {
        $hs.Fails++
        Add-Content -Path $healthPath -Encoding UTF8 -Value ("{0},{1},{2},{3},,,,,,,,{4}" -f (Get-Date -Format "HH:mm:ss"), $el, $r.Code, $ms, $ActiveJobs)
        return
    }
    $j = $r.Json
    $k = $j.gemini_keys_by_state
    $ready = Get-GroupCount $k.ready
    $inuse = Get-GroupCount $k.in_use
    $cool = Get-GroupCount $k.cooldown
    $tp = ""; $tr = ""
    if ($j.template) { $tp = $j.template.pending; $tr = $j.template.running }
    $hs.Ms += $ms
    if ($ms -gt $hs.MaxMs) { $hs.MaxMs = $ms }
    if ($cool -gt $hs.MaxCooldown) { $hs.MaxCooldown = $cool }
    if ($ready -lt $hs.MinReady) { $hs.MinReady = $ready }
    if ([int]$j.ocr_jobs_pending -gt $hs.MaxOcrPending) { $hs.MaxOcrPending = [int]$j.ocr_jobs_pending }
    if ([int]$j.ocr_jobs_running -gt $hs.MaxOcrRunning) { $hs.MaxOcrRunning = [int]$j.ocr_jobs_running }
    Add-Content -Path $healthPath -Encoding UTF8 -Value ("{0},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10},{11}" -f `
        (Get-Date -Format "HH:mm:ss"), $el, $r.Code, $ms, $ready, $inuse, $cool, $j.ocr_jobs_pending, $j.ocr_jobs_running, $tp, $tr, $ActiveJobs)
}

# ------------------------------------------------------------------ job bookkeeping
$bodyObj = [ordered]@{}
if ($Model) { $bodyObj["model"] = $Model }
if ($CurrencyMethod -gt 0) { $bodyObj["currency_method"] = $CurrencyMethod }
$submitBody = if ($bodyObj.Count -gt 0) { ($bodyObj | ConvertTo-Json -Compress) } else { "{}" }

$active = New-Object System.Collections.ArrayList
$summary = New-Object System.Collections.ArrayList
$ruleCount = @{}
$afterCount = @{}
$nextIdx = 1
$submitBlockedUntil = Get-Date
$submitFails = 0
$abort = $false
$tStart = Get-Date
$lastHealth = (Get-Date).AddDays(-1)
$lastStatus = (Get-Date).AddDays(-1)

function Save-Finished {
    # Fetches the full result of a finished job, saves files, adds a summary record.
    param($Job, [string]$Final)
    $idx = "{0:D2}" -f $Job.Idx
    $sec = [int]((Get-Date) - $Job.T0).TotalSeconds
    $rec = [ordered]@{
        Idx = $Job.Idx; JobId = $Job.Id; Status = $Final; Valid = ""; Attempts = ""; Seconds = $sec; Model = ""; Bytes = "";
        FinalErrors = ""; FirstAttemptErrors = ""; FirstAfterAutofix = ""; Autofixes = ""; WarningRules = ""; Postprocess = ""; TextChars = ""; Error = ""
    }
    $full = Invoke-Api -Method GET -Path ("/template/result/{0}" -f $Job.Id) -Headers $h
    if (-not $full.Ok) {
        $rec.Status = "fetch_failed"; $rec.Error = ("HTTP {0}" -f $full.Code)
        Write-Log ("job {0}: could not download result HTTP {1}" -f $idx, $full.Code) "Yellow"
        [void]$summary.Add([pscustomobject]$rec)
        return
    }
    # Hide Gemini key names and proxy addresses (the zip is meant to be sent around).
    $safeText = [regex]::Replace($full.Text, '"key_name"\s*:\s*"[^"]*"', '"key_name":"hidden"')
    $safeText = [regex]::Replace($safeText, '"proxy"\s*:\s*"[^"]*://[^"]*"', '"proxy":"hidden"')
    Write-Utf8 (Join-Path $out ("result_{0}_{1}.json" -f $idx, $Job.Id.Substring(0, 8))) $safeText
    $j = $full.Json
    if ($j.status -eq "error" -or -not $j.template) {
        $rec.Status = "error"
        $rec.Error = if ($j.error) { ($j.error | ConvertTo-Json -Compress -Depth 4) } else { "no template in result" }
        Write-Log ("job {0}: ERROR {1}" -f $idx, $rec.Error) "Red"
        [void]$summary.Add([pscustomobject]$rec)
        return
    }
    $t = $j.template
    $rec.Status = $j.status
    $rec.Valid = [bool]$t.valid
    $rec.Attempts = @($t.attempts).Count
    $rec.Model = $t.model
    $rec.Bytes = $t.bytes
    $rec.FinalErrors = Join-Rules $t.report.errors
    $rec.WarningRules = Join-Rules $t.report.warnings
    if ($t.postprocess) { $rec.Postprocess = (@($t.postprocess) -join ",") }
    if ($t.text) { $rec.TextChars = ([string]$t.text).Length }
    $first = @($t.attempts)[0]
    if ($first -and $first.raw_errors) {
        $rec.FirstAttemptErrors = (@($first.raw_errors) -join ",")
        foreach ($rid in @($first.raw_errors)) {
            if ($ruleCount.ContainsKey([string]$rid)) { $ruleCount[[string]$rid]++ } else { $ruleCount[[string]$rid] = 1 }
        }
    }
    if ($first) {
        $rec.FirstAfterAutofix = (@($first.errors) -join ",")
        $rec.Autofixes = (@($first.autofixes) -join ",")
        foreach ($rid in @($first.errors)) {
            if ($afterCount.ContainsKey([string]$rid)) { $afterCount[[string]$rid]++ } else { $afterCount[[string]$rid] = 1 }
        }
    }
    if ($t.generation_error) { $rec.Error = [string]$t.generation_error }
    # files: raw html (with {{variables}}), preview (demo values), text/plain
    $html = [string]$t.html
    if ($html) {
        $demo = [ordered]@{
            brandName = "Test Shop"; name = "Anna"; productName = "Test Set"
            transactionalId = "A-10492"; price = "49.90"
            supportEmail = "support@example.com"; email = "anna@example.com"
        }
        $view = $html
        foreach ($k in $demo.Keys) { $view = $view.Replace("{{$k}}", $demo[$k]) }
        Write-Utf8 (Join-Path $out ("template_{0}.raw.html" -f $idx)) $html
        Write-Utf8 (Join-Path $out ("template_{0}.html" -f $idx)) $view
    }
    if ($t.text) { Write-Utf8 (Join-Path $out ("template_{0}.txt" -f $idx)) ([string]$t.text) }
    $color = if ($t.valid) { "Green" } else { "Yellow" }
    Write-Log ("job {0}: valid={1} attempts={2} {3}s bytes={4} errors=[{5}]" -f $idx, $t.valid, $rec.Attempts, $sec, $t.bytes, $rec.FinalErrors) $color
    [void]$summary.Add([pscustomobject]$rec)
}

# ------------------------------------------------------------------ main loop
Sample-Health 0
while ($true) {
    if (((Get-Date) - $tStart).TotalSeconds -gt $MaxWait) { Write-Log ("Overall limit {0}s reached, stopping." -f $MaxWait) "Red"; break }

    # 1) submit while there is room
    while ($active.Count -lt $Parallel -and $nextIdx -le $Count -and (Get-Date) -ge $submitBlockedUntil) {
        $s = Invoke-Api -Method POST -Path "/template/submit" -Headers $h -Body $submitBody
        if ($s.Ok) {
            $submitFails = 0
            $job = @{ Idx = $nextIdx; Id = [string]$s.Json.job_id; T0 = (Get-Date); Fails = 0; Stage = "queued"; Attempt = 0 }
            [void]$active.Add($job)
            Write-Log ("job {0:D2} submitted: {1}" -f $nextIdx, $job.Id.Substring(0, 8))
            $nextIdx++
            Start-Sleep -Milliseconds 100
            continue
        }
        if ($s.Code -eq 429) {
            if ($s.Text -match "daily") { Write-Log ("Daily limit reached: {0}" -f $s.Text) "Red"; $abort = $true; break }
            $wait = if ($s.RetryAfter -gt 0) { [Math]::Min($s.RetryAfter, 30) } else { 4 }
            $submitBlockedUntil = (Get-Date).AddSeconds($wait)
            Write-Log ("submit: 429 (queue full or rate limit), will retry in {0}s" -f $wait) "DarkYellow"
            break
        }
        if ($s.Code -eq 401 -or $s.Code -eq 400) {
            Write-Log ("submit: HTTP {0} {1}" -f $s.Code, $s.Text) "Red"
            Write-Log ("Stopping. 401 = wrong key; 400 = model '{0}' or currency_method {1} not allowed by the server (check TEMPLATE_MODELS). Try -Model gemini-3.5-flash-lite" -f $Model, $CurrencyMethod) "Red"
            $abort = $true; break
        }
        $submitFails++
        if ($submitFails -ge 5) {
            Write-Log ("submit: HTTP {0} {1} five times in a row. Stopping." -f $s.Code, $s.Text) "Red"
            $abort = $true; break
        }
        $submitBlockedUntil = (Get-Date).AddSeconds(10)
        Write-Log ("submit: HTTP {0}, retry {1}/5 in 10s" -f $s.Code, $submitFails) "Yellow"
        break
    }
    if ($abort) { break }

    # 2) poll all active jobs
    foreach ($job in @($active)) {
        $r = Invoke-Api -Method GET -Path ("/template/result/{0}?html=0" -f $job.Id) -Headers $h
        if (-not $r.Ok) {
            if ($r.Code -eq 404) {
                Write-Log ("job {0:D2}: 404 (server restarted or job expired)" -f $job.Idx) "Red"
                [void]$summary.Add([pscustomobject][ordered]@{ Idx = $job.Idx; JobId = $job.Id; Status = "lost_404"; Valid = ""; Attempts = ""; Seconds = [int]((Get-Date) - $job.T0).TotalSeconds; Model = ""; Bytes = ""; FinalErrors = ""; FirstAttemptErrors = ""; FirstAfterAutofix = ""; Autofixes = ""; WarningRules = ""; Postprocess = ""; TextChars = ""; Error = "404" })
                $active.Remove($job)
                continue
            }
            $job.Fails++
            if ($job.Fails -ge 6) {
                Write-Log ("job {0:D2}: polling failed 6 times (HTTP {1})" -f $job.Idx, $r.Code) "Red"
                [void]$summary.Add([pscustomobject][ordered]@{ Idx = $job.Idx; JobId = $job.Id; Status = "poll_failed"; Valid = ""; Attempts = ""; Seconds = [int]((Get-Date) - $job.T0).TotalSeconds; Model = ""; Bytes = ""; FinalErrors = ""; FirstAttemptErrors = ""; FirstAfterAutofix = ""; Autofixes = ""; WarningRules = ""; Postprocess = ""; TextChars = ""; Error = ("HTTP " + $r.Code) })
                $active.Remove($job)
            }
            continue
        }
        $job.Fails = 0
        $job.Stage = [string]$r.Json.stage
        $job.Attempt = $r.Json.attempt
        if ($r.Json.status -eq "done" -or $r.Json.status -eq "error") {
            Save-Finished -Job $job -Final ([string]$r.Json.status)
            $active.Remove($job)
        }
    }

    # 3) health sample and a compact progress line
    if (((Get-Date) - $lastHealth).TotalSeconds -ge $HealthEvery) {
        Sample-Health $active.Count
        $lastHealth = Get-Date
    }
    if (((Get-Date) - $lastStatus).TotalSeconds -ge 12) {
        $parts = @()
        foreach ($job in $active) { $parts += ("{0:D2}:{1}/a{2}" -f $job.Idx, $job.Stage, $job.Attempt) }
        Write-Log ("finished {0}/{1} | in flight: {2}" -f $summary.Count, $Count, ($parts -join " "))
        $lastStatus = Get-Date
    }

    if ($nextIdx -gt $Count -and $active.Count -eq 0) { break }
    Start-Sleep -Seconds 3
}

# anything still active at this point did not finish
foreach ($job in @($active)) {
    [void]$summary.Add([pscustomobject][ordered]@{ Idx = $job.Idx; JobId = $job.Id; Status = "unfinished"; Valid = ""; Attempts = ""; Seconds = [int]((Get-Date) - $job.T0).TotalSeconds; Model = ""; Bytes = ""; FinalErrors = ""; FirstAttemptErrors = ""; FirstAfterAutofix = ""; Autofixes = ""; WarningRules = ""; Postprocess = ""; TextChars = ""; Error = "run stopped" })
}
Sample-Health 0
$totalSec = [int]((Get-Date) - $tStart).TotalSeconds

# ------------------------------------------------------------------ numbers for the dashboard
$rows = @($summary | Sort-Object Idx)
$rows | Export-Csv -Path (Join-Path $out "summary.csv") -NoTypeInformation -Encoding UTF8
$done = @($rows | Where-Object { $_.Status -eq "done" })
$valid = @($done | Where-Object { $_.Valid -eq $true })
$invalid = @($done | Where-Object { $_.Valid -ne $true })
$validFirst = @($valid | Where-Object { $_.Attempts -eq 1 })
$notDone = @($rows | Where-Object { $_.Status -ne "done" })
$notStarted = [Math]::Max(0, $Count - $rows.Count)
$avgAtt = if ($done.Count -gt 0) { [Math]::Round((($done | Measure-Object -Property Attempts -Average).Average), 2) } else { 0 }
$secSorted = @($done | ForEach-Object { [int]$_.Seconds } | Sort-Object)
$avgSec = if ($done.Count -gt 0) { [Math]::Round((($done | Measure-Object -Property Seconds -Average).Average), 1) } else { 0 }
$p50Sec = if ($secSorted.Count -gt 0) { $secSorted[[int][Math]::Floor(($secSorted.Count - 1) * 0.5)] } else { 0 }
$maxSec = if ($secSorted.Count -gt 0) { $secSorted[$secSorted.Count - 1] } else { 0 }
$maxSecAll = 0
foreach ($r in $rows) { if ([int]$r.Seconds -gt $maxSecAll) { $maxSecAll = [int]$r.Seconds } }
$perMin = if ($totalSec -gt 0) { [Math]::Round($done.Count * 60.0 / $totalSec, 2) } else { 0 }
$msSorted = @($hs.Ms | Sort-Object)
$p50 = if ($msSorted.Count -gt 0) { $msSorted[[int][Math]::Floor(($msSorted.Count - 1) * 0.5)] } else { 0 }
$minReady = if ($hs.MinReady -eq 9999) { "n/a" } else { $hs.MinReady }

# final (after repair) rule counts
$finalRuleCount = @{}
foreach ($r in $rows) {
    if ($r.FinalErrors) {
        foreach ($rid in ([string]$r.FinalErrors -split ",")) {
            if ($rid) { if ($finalRuleCount.ContainsKey($rid)) { $finalRuleCount[$rid]++ } else { $finalRuleCount[$rid] = 1 } }
        }
    }
}

# ------------------------------------------------------------------ dashboard
$dash = New-Object System.Collections.ArrayList
function Add-Dash {
    param([string]$Text, [string]$Color = "")
    [void]$dash.Add(@{ T = $Text; C = $Color })
}
$eq = "=" * 118
$dl = "-" * 118
$bw = 30
Add-Dash $eq "Cyan"
Add-Dash ("  EMAIL TEMPLATE BATCH DASHBOARD   {0}" -f $stamp) "Cyan"
Add-Dash ("  model={0}  method={1}  parallel={2}  wall={3}s  throughput={4} done/min" -f $modelLabel, $cmLabel, $Parallel, $totalSec, $perMin) "Cyan"
Add-Dash $eq "Cyan"
Add-Dash ""
Add-Dash ("  Valid (final)        [{0}] {1,3}%  {2}/{3}" -f (Get-Bar $valid.Count $Count $bw), (Get-Pct $valid.Count $Count), $valid.Count, $Count) "Green"
Add-Dash ("  Valid on 1st attempt [{0}] {1,3}%  {2}/{3}" -f (Get-Bar $validFirst.Count $Count $bw), (Get-Pct $validFirst.Count $Count), $validFirst.Count, $Count) "Green"
Add-Dash ("  Done but invalid     [{0}] {1,3}%  {2}/{3}" -f (Get-Bar $invalid.Count $Count $bw), (Get-Pct $invalid.Count $Count), $invalid.Count, $Count) "Yellow"
Add-Dash ("  Error / unfinished   [{0}] {1,3}%  {2}/{3}" -f (Get-Bar ($notDone.Count + $notStarted) $Count $bw), (Get-Pct ($notDone.Count + $notStarted) $Count), ($notDone.Count + $notStarted), $Count) "Red"
Add-Dash ""
Add-Dash ("  Attempts (done jobs): avg {0}" -f $avgAtt) "White"
$maxAtt = 0
foreach ($r in $done) { if ([int]$r.Attempts -gt $maxAtt) { $maxAtt = [int]$r.Attempts } }
for ($n = 1; $n -le $maxAtt; $n++) {
    $c = @($done | Where-Object { [int]$_.Attempts -eq $n }).Count
    Add-Dash ("    {0} attempt(s)  [{1}] {2}" -f $n, (Get-Bar $c ([Math]::Max($done.Count, 1)) 20), $c) "Gray"
}
$needRepair = @($done | Where-Object { [int]$_.Attempts -gt 1 }).Count
Add-Dash ("  Needed a repair call [{0}] {1,3}%  {2}/{3} done jobs (each costs about one more generation)" -f (Get-Bar $needRepair ([Math]::Max($done.Count, 1)) 20), (Get-Pct $needRepair $done.Count), $needRepair, $done.Count) "Gray"
Add-Dash ("  Time per job (done): avg {0}s  p50 {1}s  max {2}s" -f $avgSec, $p50Sec, $maxSec) "White"
Add-Dash ""
Add-Dash $dl "DarkGray"
Add-Dash ("  {0,2} {1,-8} {2,-10} {3,-5} {4,3} {5,5}  {6,-14} {7,6}  {8,-14} {9,-16} {10,-14}" -f "#", "job", "status", "valid", "att", "sec", "time", "KB", "final errors", "att1 raw", "att1 after fix") "White"
Add-Dash $dl "DarkGray"
foreach ($r in $rows) {
    $jid = [string]$r.JobId; if ($jid.Length -gt 8) { $jid = $jid.Substring(0, 8) }
    $validTxt = "-"
    if ($r.Status -eq "done") { $validTxt = if ($r.Valid -eq $true) { "YES" } else { "NO" } }
    $att = if ($r.Attempts -ne "") { [string]$r.Attempts } else { "-" }
    $kb = if ($r.Bytes -ne "") { [string][Math]::Round([double]$r.Bytes / 1024.0, 1) } else { "-" }
    $fin = if ($r.FinalErrors) { [string]$r.FinalErrors } else { "-" }
    $fst = if ($r.FirstAttemptErrors) { [string]$r.FirstAttemptErrors } else { "-" }
    $fax = if ($r.Status -ne "done") { "-" } elseif ($r.FirstAfterAutofix) { [string]$r.FirstAfterAutofix } else { "ok" }
    $bar = Get-Bar ([int]$r.Seconds) $maxSecAll 14
    $color = "Red"
    if ($r.Status -eq "done") { $color = if ($r.Valid -eq $true) { "Green" } else { "Yellow" } }
    Add-Dash ("  {0:D2} {1,-8} {2,-10} {3,-5} {4,3} {5,5}  {6,-14} {7,6}  {8,-14} {9,-16} {10,-14}" -f $r.Idx, $jid, (Cut ([string]$r.Status) 10), $validTxt, $att, $r.Seconds, $bar, $kb, (Cut $fin 14), (Cut $fst 16), (Cut $fax 14)) $color
}
if ($notStarted -gt 0) { Add-Dash ("  ({0} job(s) were never submitted)" -f $notStarted) "Red" }
Add-Dash $dl "DarkGray"
Add-Dash ""

$top1 = @($ruleCount.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 8)
Add-Dash "  Model mistakes on attempt 1, raw (autofix absorbs most of them), number of jobs:" "White"
if ($top1.Count -eq 0) { Add-Dash "    none" "Green" }
foreach ($e in $top1) { Add-Dash ("    {0,-5} [{1}] {2}" -f $e.Key, (Get-Bar $e.Value $done.Count 20), $e.Value) "Gray" }
$topA = @($afterCount.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 8)
Add-Dash "  Left after autofix on attempt 1 (THESE cost a second Gemini call), number of jobs:" "White"
if ($topA.Count -eq 0) { Add-Dash "    none" "Green" }
foreach ($e in $topA) { Add-Dash ("    {0,-5} [{1}] {2}" -f $e.Key, (Get-Bar $e.Value $done.Count 20), $e.Value) "Yellow" }
$top2 = @($finalRuleCount.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 8)
Add-Dash "  Rules STILL broken in the final result (after repair):" "White"
if ($top2.Count -eq 0) { Add-Dash "    none" "Green" }
foreach ($e in $top2) { Add-Dash ("    {0,-5} [{1}] {2}" -f $e.Key, (Get-Bar $e.Value $done.Count 20), $e.Value) "Yellow" }
Add-Dash ""

$failed = @($rows | Where-Object { $_.Status -ne "done" -and $_.Error })
if ($failed.Count -gt 0) {
    Add-Dash "  Failed jobs:" "Red"
    foreach ($r in $failed) { Add-Dash ("    {0:D2} {1}: {2}" -f $r.Idx, $r.Status, (Cut ([string]$r.Error) 90)) "Red" }
    Add-Dash ""
}

Add-Dash "  OCR / service liveness during the run (/health):" "White"
Add-Dash ("    samples={0}  failed={1}  latency ms p50={2} max={3}" -f $hs.Samples, $hs.Fails, $p50, $hs.MaxMs) "Gray"
Add-Dash ("    min keys ready={0}  max keys in cooldown={1}  max OCR pending={2}  max OCR running={3}" -f $minReady, $hs.MaxCooldown, $hs.MaxOcrPending, $hs.MaxOcrRunning) "Gray"
if ($hs.Fails -eq 0 -and $hs.MaxCooldown -eq 0) {
    Add-Dash "    verdict: OK - service answered every health check, no keys went to cooldown" "Green"
    if ($hs.MaxOcrPending -eq 0 -and $hs.MaxOcrRunning -eq 0) {
        Add-Dash "    NOTE: OCR was idle during this run, so this does NOT prove OCR is unaffected. Run it while OCR is busy." "Yellow"
    }
} else {
    Add-Dash "    verdict: ATTENTION - failed health checks or keys in cooldown (see health_log.csv)" "Yellow"
}
Add-Dash $eq "Cyan"

Write-Host ""
foreach ($d in $dash) {
    if ($d.C) { Write-Host $d.T -ForegroundColor $d.C } else { Write-Host $d.T }
}
$plain = @()
foreach ($d in $dash) { $plain += $d.T }
Write-Utf8 (Join-Path $out "summary.txt") (($plain -join "`r`n") + "`r`n")

# ------------------------------------------------------------------ zip + open folder
$zip = Join-Path $root ("{0}.zip" -f $outName)
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $out "*") -DestinationPath $zip
Write-Host ""
Write-Host ("ZIP READY: {0}" -f $zip) -ForegroundColor Green
Write-Host "Send this zip back for analysis."
if (-not $NoOpen) {
    try { Start-Process explorer.exe -ArgumentList ('/select,"{0}"' -f $zip) } catch { Write-Host "Could not open Explorer: $($_.Exception.Message)" -ForegroundColor Yellow }
}
