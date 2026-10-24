param(
  [string]$BaseDir = "E:\code\AutoTaskByMuseSpark1.3\xianyu-hunter",
  [string]$PadKeywords = "macbookpro m1max 64g,m1max 64g 1t,16寸m1max",
  [string]$HonorKeywords = "mac studio 64g,studio m1 max,m1max 台式",
  [string]$PadDevice = "AUIFLJTKSGSWU4TK",
  [string]$PadWifi = "192.168.31.208:5555",
  [string]$HonorDevice = "AADEGL4701000138",
  [string]$HonorWiFi = "192.168.31.95:5555"
)
$ErrorActionPreference = "Continue"
$ts = Get-Date -Format "MMdd-HHmm"
$counterFile = $BaseDir + "\round_counter.txt"
$Round = 30
if (Test-Path $counterFile) {
  $n = (Get-Content $counterFile -TotalCount 1)
  if ($n -match '^\d+$') { $Round = [int]$n + 1 }
}
Set-Content -Path $counterFile -Value ([string]$Round) -Encoding UTF8
$logFile = $BaseDir + "\scan-log.md"
function Log($msg) {
  $line = "- " + (Get-Date -Format "MM-dd HH:mm:ss") + " AUTO-R" + $Round + ": " + $msg
  Add-Content -Path $logFile -Value $line -Encoding UTF8
  Write-Output $msg
}
function Newest-Tag($prefix) {
  $files = Get-ChildItem ($BaseDir + "\scans") -Filter ($prefix + "*_s1.xml") -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $files) { return $null }
  return ($files.BaseName -replace "_s1$", "")
}
try {
  Log ("START round " + $Round + " (" + $ts + ")")
  # 快速门禁：两台采集机都不可用时立即跳过，不做 30s 级设备探测
  $padUsb = (adb devices | Out-String) -match [regex]::Escape($PadDevice)
  $padWifi = (adb devices | Out-String) -match [regex]::Escape($PadWifi)
  $honorUsb = (adb devices | Out-String) -match [regex]::Escape($HonorDevice)
  $honorWifi = (adb devices | Out-String) -match [regex]::Escape($HonorWiFi)
  $padOk = $padUsb -or $padWifi
  $honorOk = $honorUsb -or $honorWifi
  if (-not ($padOk -or $honorOk)) {
    Log "NO_DEVICE (pad+honor both offline) - skipped fast, needs physical USB"
    $pauseFile = $BaseDir + "\AUTO_PAUSED.txt"
    ("paused at " + $ts + " - connect pad USB AUIFLJTKSGSWU4TK or honor USB AADEGL4701000138") | Set-Content $pauseFile -Encoding UTF8
    exit 0
  }
  if (Test-Path ($BaseDir + "\AUTO_PAUSED.txt")) { Remove-Item ($BaseDir + "\AUTO_PAUSED.txt") -Force }
  # 无线优先：USB 在但可能不稳，无线在线则用无线
  if ($padWifi) { $PadDevice = $PadWifi }
  if ($honorWifi) { $HonorDevice = $HonorWiFi }
  Log ("device pick: pad=" + $PadDevice + " honor=" + $HonorDevice)
  powershell -NoProfile -ExecutionPolicy Bypass -File ($BaseDir + "\scan_pad.ps1") -Round $Round -SessionId ("auto-pad-r" + $Round) -Device $PadDevice -WiFiDevice $PadWifi -Keywords $PadKeywords 2>&1 | ForEach-Object { Log ("PAD: " + $_) }
  powershell -NoProfile -ExecutionPolicy Bypass -File ($BaseDir + "\scan_round.ps1") -Round $Round -SessionId ("auto-honor-r" + $Round) -Device $HonorDevice -WiFiDevice $HonorWiFi -Keywords $HonorKeywords 2>&1 | ForEach-Object { Log ("HONOR: " + $_) }
  $env:PYTHONIOENCODING = "utf-8"
  $padTag = Newest-Tag ("pad" + $Round)
  $honorTag = Newest-Tag ("round" + $Round)
  $allRows = @()
  $jobs = @(
    @{ tag = $padTag; kws = $PadKeywords; who = "pad" },
    @{ tag = $honorTag; kws = $HonorKeywords; who = "honor" }
  )
  foreach ($j in $jobs) {
    if (-not $j.tag) { Log ($j.who + ": no scans, skip"); continue }
    $out = $BaseDir + "\scans\" + $j.tag + "_leads.md"
    python ($BaseDir + "\pad_leads.py") $j.tag $j.kws $out 2>&1 | ForEach-Object { Log ($j.who + "-parse: " + $_) }
    if (Test-Path $out) {
      $lines = Get-Content $out -Encoding UTF8 | Where-Object { $_ -match "^-\s\*\*" }
      foreach ($ln in $lines) { $allRows += $ln }
    }
  }
  $seenFile = $BaseDir + "\seen_listings.txt"
  if (-not (Test-Path $seenFile)) { New-Item $seenFile -ItemType File -Force | Out-Null }
  $seen = @{}
  $old = Get-Content $seenFile -Encoding UTF8
  foreach ($o in $old) { if ($o) { $seen[$o] = $true } }
  $fresh = @()
  foreach ($row in $allRows) {
    $m = [regex]::Match($row, "^-\s\*\*(\d+)\*\*\s*(\S*)\s*(.*)$")
    if ($m.Success) {
      $desc = $m.Groups[3].Value
      if ($desc.Length -gt 30) { $desc = $desc.Substring(0, 30) }
      $key = $m.Groups[1].Value + "|" + $desc
      if (-not $seen.ContainsKey($key)) { $fresh += $row; $seen[$key] = $true }
    }
  }
  $seen.Keys | Set-Content $seenFile -Encoding UTF8
  Log ("PARSED total=" + $allRows.Count + " fresh=" + $fresh.Count)
  $rep = $BaseDir + "\reports\R" + $Round + "_" + $ts + "_auto.md"
  $rl = @()
  $rl += "# R" + $Round + " auto (" + $ts + ")"
  $rl += ""
  $rl += ("pad=" + $padTag + " honor=" + $honorTag)
  $rl += ("total=" + $allRows.Count + " fresh=" + $fresh.Count)
  $rl += ""
  $rl += "## fresh"
  if ($fresh.Count -eq 0) { $rl += "(none, all deduped)" } else { foreach ($f in $fresh) { $rl += $f } }
  $rl += ""
  $rl += "## note"
  $rl += "auto round does scan+parse+dedup only; detail trace needs interactive session"
  $rl -join [Environment]::NewLine | Set-Content $rep -Encoding UTF8
  Log ("REPORT " + $rep)
} finally {
  adb -s $HonorDevice shell "ime set com.baidu.input_hihonor/com.baidu.input_honor.ImeService" 2>$null | Out-Null
  adb -s $HonorWiFi shell "ime set com.baidu.input_hihonor/com.baidu.input_honor.ImeService" 2>$null | Out-Null
  adb -s $PadDevice shell "ime set com.sohu.inputmethod.sogouoem/.SogouIME" 2>$null | Out-Null
  foreach ($d in @($PadDevice, $HonorDevice, $HonorWiFi)) {
    adb -s $d shell "rm -f /sdcard/s.png /sdcard/ui.xml /sdcard/d.xml" 2>$null | Out-Null
  }
  Log "IME restored + sdcard cleaned"
}
$repoRoot = Split-Path $BaseDir
git -C $repoRoot add xianyu-hunter/scan-log.md xianyu-hunter/seen_listings.txt xianyu-hunter/round_counter.txt 2>$null | Out-Null
git -C $repoRoot -c core.quotePath=false commit -m ("AUTO-R" + $Round + " halfhour tick") 2>$null | Out-Null
Log ("DONE round " + $Round)
