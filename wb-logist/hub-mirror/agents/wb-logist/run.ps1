param([switch]$Live, [switch]$DryRun, [switch]$Quiet)

$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$hub = Split-Path -Parent (Split-Path -Parent $dir)
. (Join-Path $hub 'lib\mcp.ps1')

$utf8 = New-Object System.Text.UTF8Encoding($false)
$t0 = Get-Date
$now = Get-Date

function Write-Json($p, $o) {
  $j = $o | ConvertTo-Json -Depth 14
  [System.IO.File]::WriteAllText($p, $j, $utf8)
}
function Add-Event($level, $text) {
  if ($null -ne $text -and $text.Length -gt 300) { $text = $text.Substring(0, 300) + '...' }
  $o = [ordered]@{ ts = (Get-Date).ToString('o'); level = $level; text = $text }
  Add-Content -LiteralPath (Join-Path $dir 'events.jsonl') -Value ($o | ConvertTo-Json -Compress) -Encoding UTF8
}

$meta = Read-JsonFile (Join-Path $dir 'agent.json')
if (-not $meta) { throw 'agent.json not found' }

$project = [string]$meta.projectPath
$python = Join-Path $project ([string]$meta.pythonExe)
$script = [string]$meta.script
$keep = if ($meta.keepRuns) { [int]$meta.keepRuns } else { 40 }

$mode = if ($Live) { 'live' } elseif ($DryRun) { 'dry' } elseif ($meta.dryRunDefault) { 'dry' } else { 'live' }

$stamp = $now.ToString('yyyyMMdd_HHmmss')
$runDir = Join-Path $dir ('artifacts\' + $stamp)
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

try {
  if (-not (Test-Path -LiteralPath $python)) { throw ('Не найден Python: ' + $python) }
  if (-not (Test-Path -LiteralPath (Join-Path $project $script))) { throw ('Не найден скрипт: ' + $script) }

  $argList = @($script)
  foreach ($a in @($meta.defaultArgs)) { if ($a) { $argList += [string]$a } }
  if ($mode -eq 'dry') { $argList += '--dry-run' }

  $outFile = Join-Path $runDir 'stdout.json'
  $errFile = Join-Path $runDir 'stderr.log'

  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $proc = Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $project `
    -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru -NoNewWindow -Wait
  $sw.Stop()
  $code = $proc.ExitCode

  $raw = ''
  if (Test-Path -LiteralPath $outFile) { $raw = [System.IO.File]::ReadAllText($outFile, $utf8).Trim() }
  $result = $null
  if ($raw) {
    $lines = @($raw -split "`r?`n" | Where-Object { $_.Trim() })
    if ($lines.Count -gt 0) {
      try { $result = ($lines[$lines.Count - 1] | ConvertFrom-Json) } catch { }
    }
  }
  $ok = ($code -eq 0) -and $result -and ([string]$result.status -eq 'success')

  $artifacts = @()
  foreach ($p in @($result.excel_path, $result.manifest_path)) {
    if ($p -and (Test-Path -LiteralPath $p)) {
      $name = Split-Path -Leaf $p
      $dest = Join-Path $runDir $name
      if ($dest -ne $p) { Copy-Item -LiteralPath $p -Destination $dest -Force -ErrorAction SilentlyContinue }
      if (Test-Path -LiteralPath $dest) {
        $fi = Get-Item -LiteralPath $dest
        $artifacts += [ordered]@{
          name = $name
          size = $fi.Length
          run  = $stamp
          kind = if ($name -like '*.xlsx') { 'xlsx' } else { 'json' }
          url  = '/api/artifact/wb-logist/' + $stamp + '/' + [uri]::EscapeDataString($name)
        }
      }
    }
  }

  $errTail = ''
  if (Test-Path -LiteralPath $errFile) {
    $errTail = ((@([System.IO.File]::ReadAllText($errFile, $utf8) -split "`r?`n") | Select-Object -Last 40) -join "`n").Trim()
  }

  Write-Json (Join-Path $runDir 'run.json') ([ordered]@{
    ts         = $now.ToString('o')
    mode       = $mode
    ok         = [bool]$ok
    exitCode   = $code
    durationMs = [int]$sw.Elapsed.TotalMilliseconds
    result     = $result
    artifacts  = $artifacts
    stderrTail = $errTail
  })
}
catch {
  $code = -1
  $ok = $false
  $result = [ordered]@{ status = 'error'; error = $_.Exception.Message; error_code = 'WRAPPER_ERROR' }
  $artifacts = @()
  $errTail = $_.Exception.Message
  Write-Json (Join-Path $runDir 'run.json') ([ordered]@{
    ts = $now.ToString('o'); mode = $mode; ok = $false; exitCode = -1
    durationMs = [int]((Get-Date) - $t0).TotalMilliseconds
    result = $result; artifacts = @(); stderrTail = $errTail
  })
}

# --- история прогонов из манифестов проекта ---
$outDir = Join-Path $project 'output'
$runs = @()
if (Test-Path -LiteralPath $outDir) {
  $files = @(Get-ChildItem -LiteralPath $outDir -File -Filter 'manifest_*.json' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First $keep)
  foreach ($f in $files) {
    try { $m = Read-JsonFile $f.FullName } catch { $m = $null }
    if (-not $m) { continue }
    $draftId = [string]$m.draft_id
    $runs += [ordered]@{
      ts        = [string]$m.generated_at
      draftId   = $draftId
      positions = @($m.items).Count
      items     = if ($m.total_items) { [int]$m.total_items } else { 0 }
      skipped   = @($m.skipped).Count
      dry       = (-not $draftId)
      manifest  = $f.Name
      when      = $f.LastWriteTime.ToString('dd.MM HH:mm')
    }
  }
}

$chart = @()
$last8 = @($runs | Select-Object -First 8)
[array]::Reverse($last8)
foreach ($r in $last8) {
  $chart += [ordered]@{ label = ($r.when -replace '^.*\s', ''); value = [int]$r.items }
}

$last = if ($runs.Count -gt 0) { $runs[0] } else { $null }
$draftsTotal = @($runs | Where-Object { -not $_.dry }).Count
$drafts7 = 0
foreach ($r in $runs) {
  if ($r.dry -or -not $r.ts) { continue }
  try { if ([datetime]::Parse($r.ts) -gt $now.AddDays(-7)) { $drafts7++ } } catch { }
}

$metrics = @(
  [ordered]@{ key = 'drafts7'; label = 'Черновиков за 7 дней'; value = $drafts7; unit = '';
    hint = ('всего в истории: ' + $draftsTotal) },
  [ordered]@{ key = 'positions'; label = 'Позиций'; value = $(if ($last) { $last.positions } else { $null }); unit = '';
    hint = $(if ($last) { 'прогон ' + $last.when } else { 'ещё не запускался' }) },
  [ordered]@{ key = 'items'; label = 'Штук'; value = $(if ($last) { $last.items } else { $null }); unit = '';
    hint = $(if ($last) { 'в ТЗ / черновике' } else { '' }) },
  [ordered]@{ key = 'skipped'; label = 'Пропущено'; value = $(if ($last) { $last.skipped } else { $null }); unit = '';
    hint = 'нулевое количество' }
)

$history = @()
foreach ($r in ($runs | Select-Object -First 6)) {
  $short = if ($r.draftId) { $r.draftId.Substring(0, [Math]::Min(8, $r.draftId.Length)) } else { '' }
  $history += [ordered]@{
    title  = $r.when + ' · ' + $(if ($r.dry) { 'тест: ТЗ без черновика' } else { 'черновик ' + $short })
    status = if ($r.dry) { 'queued' } else { 'done' }
    at     = ''
    note   = ('{0} поз. · {1} шт · пропущено {2}' -f $r.positions, $r.items, $r.skipped)
  }
}

$artRoot = Join-Path $dir 'artifacts'
$allFiles = @()
if (Test-Path -LiteralPath $artRoot) {
  Get-ChildItem -LiteralPath $artRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 10 | ForEach-Object {
      $rs = $_.Name
      Get-ChildItem -LiteralPath $_.FullName -File -ErrorAction SilentlyContinue |
        Where-Object { @('stdout.json', 'stderr.log', 'run.json') -notcontains $_.Name } |
        ForEach-Object {
          $allFiles += [ordered]@{
            run  = $rs
            name = $_.Name
            size = $_.Length
            url  = '/api/artifact/wb-logist/' + $rs + '/' + [uri]::EscapeDataString($_.Name)
          }
        }
    }
}

if (-not $ok) {
  $errText = if ($result.error) { [string]$result.error } else { 'см. лог' }
  $errCode = if ($result.error_code) { ' (' + $result.error_code + ')' } else { '' }
  $rec = 'Ошибка: ' + $errText + $errCode
}
elseif ($mode -eq 'dry') {
  $rec = ('Тестовый прогон: {0} поз., {1} шт. Черновик не создавался (--dry-run).' -f [string]$result.positions, [string]$result.total_items)
}
elseif ($result.reused) {
  $rec = ('Похожий черновик уже есть: {0} — второй не создавался.' -f [string]$result.supply_id)
}
else {
  $rec = ('Черновик {0} создан: {1} поз., {2} шт. Слот не бронирован — выбери дату и коэффициент в кабинете WB.' -f [string]$result.supply_id, [string]$result.positions, [string]$result.total_items)
}

$state = [ordered]@{
  id          = 'wb-logist'
  view        = 'runs'
  status      = if ($ok) { 'idle' } else { 'error' }
  statusText  = if ($ok) { 'Готов' } else { 'Ошибка' }
  currentTask = [ordered]@{
    title      = $(if ($mode -eq 'dry') { 'Тестовый прогон: ТЗ без черновика' } else { 'Боевой прогон: черновик FBW' })
    progress   = 1
    startedAt  = $t0.ToString('o')
    finishedAt = (Get-Date).ToString('o')
  }
  metrics     = $metrics
  charts      = [ordered]@{ runs = $chart }
  tasks       = $history
  files       = $artifacts
  allFiles    = $allFiles
  runs        = $runs
  summary     = [ordered]@{
    mode       = $mode
    project    = $project
    reportDir  = [string]$meta.reportDir
    lastWhen   = if ($last) { $last.when } else { '' }
    lastDraft  = if ($last) { $last.draftId } else { '' }
    exitCode   = $code
    durationMs = [int]((Get-Date) - $t0).TotalMilliseconds
    message    = if ($result.message) { [string]$result.message } else { '' }
    stderrTail = $errTail
  }
  recommendation = $rec
  updatedAt      = (Get-Date).ToString('o')
  runMs          = [int]((Get-Date) - $t0).TotalMilliseconds
}

Write-Json (Join-Path $dir 'state.json') $state

if ($ok) { Add-Event 'info' ('Прогон (' + $mode + '): ' + $rec) }
else { Add-Event 'error' ('Сбой прогона (' + $mode + '): ' + $rec) }

if (Test-Path -LiteralPath $artRoot) {
  $dirs = @(Get-ChildItem -LiteralPath $artRoot -Directory | Sort-Object Name -Descending)
  if ($dirs.Count -gt $keep) {
    $dirs[$keep..($dirs.Count - 1)] | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
  }
}

if (-not $Quiet) { $state | ConvertTo-Json -Depth 8 }

