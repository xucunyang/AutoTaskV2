param(
  [string]$Device = "192.168.31.208:5555",
  [string]$WiFiDevice = "192.168.31.208:5555",
  [string]$UsbDevice = "AUIFLJTKSGSWU4TK",
  [int]$Round = 1,
  [string]$ActiveStart = "09:00",
  [string]$ActiveEnd = "23:30",
  [int]$SleepMinutes = 0,
  [string]$SessionId = "xianyu-pad",
  [string]$BaseDir = "E:\code\AutoTaskByMuseSpark1.3\xianyu-hunter",
  [string]$ParseScript = "C:\Users\22651\.config\opencode\skills\android-phone\scripts\parse_ui.py",
  [string]$Keywords = "m1max64g,m2max,m4max,macstudio64,imac24,m1max64"
)
$ErrorActionPreference = "Continue"
$logFile = "$BaseDir\scan-log.md"
$OutDir = "$BaseDir\scans"
$LeaseTool = "$BaseDir\device-lease\devlease.py"
$script:Dev = $Device

function Log($msg) {
  $line = "- $(Get-Date -Format 'MM-dd HH:mm:ss') PAD R${Round}: $msg"
  Add-Content -Path $logFile -Value $line -Encoding UTF8
  Write-Output $msg
}

if ($SleepMinutes -gt 0) { Start-Sleep -Seconds ($SleepMinutes * 60) }

$t = (Get-Date).ToString("HH:mm")
if (($t -lt $ActiveStart) -or ($t -gt $ActiveEnd)) { Log "OUT_OF_WINDOW ($t), skip"; exit 0 }

$leaseOut = python $LeaseTool acquire --device $Device --session $SessionId --ttl 1500 --task "pad-widescan-r$Round" 2>$null
if ($LASTEXITCODE -eq 2) { Log "LEASE_BUSY: $leaseOut (skip)"; exit 0 }
if ($LASTEXITCODE -ne 0) { Log "LEASE_ERROR: $leaseOut"; exit 1 }
Log "LEASE_OK"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tag = "pad$Round" + "_" + (Get-Date).ToString("MMdd_HHmm")

function A([string]$cmd) {
  $r = adb -s $script:Dev shell $cmd 2>$null
  return "$r".Trim()
}

function Ensure-Device {
  if ((A "echo ok") -eq "ok") { return $true }
  Log "DEVICE_STALE, recovering..."
  adb connect $WiFiDevice 2>$null | Out-Null
  Start-Sleep 5
  if ((A "echo ok") -eq "ok") { Log "RECOVERED (wifi)"; return $true }
  $list = adb devices 2>$null | Out-String
  if ($list -match [regex]::Escape($UsbDevice)) {
    $script:Dev = $UsbDevice
    Log "SWITCHED to usb $UsbDevice"
    if ((A "echo ok") -eq "ok") { return $true }
  }
  Log "DEVICE_LOST"
  return $false
}

function Dump-Uixml([string]$name) {
  if (-not (Ensure-Device)) { return }
  A "uiautomator dump /sdcard/ui.xml" | Out-Null
  adb -s $script:Dev pull /sdcard/ui.xml "$OutDir\$name.xml" 2>$null | Out-Null
  $f = Get-Item "$OutDir\$name.xml" -ErrorAction SilentlyContinue
  if (-not $f -or $f.Length -lt 8000) {
    Start-Sleep 3
    A "uiautomator dump /sdcard/ui.xml" | Out-Null
    adb -s $script:Dev pull /sdcard/ui.xml "$OutDir\$name.xml" 2>$null | Out-Null
  }
  # NOTE: no .txt via `>` redirection -- PS 5.1 writes GBK and mangles CJK.
  # XML is authoritative UTF-8; parse with python explicitly when needed.
}

# find node center from raw XML (avoids encoding pitfalls of txt round-trip)
# needles are passed as unicode-escape sequences by the caller (e.g. "\u641c\u7d22")
$script:NeedleCache = @{}
function Find-Tap([string]$name, [string]$needleEsc) {
  $needle = [regex]::Unescape($needleEsc)
  $f = "$OutDir\$name.xml"
  if (-not (Test-Path $f)) { Write-Output "DBG missing-file: $f"; return $null }
  $xml = [IO.File]::ReadAllText($f, [Text.Encoding]::UTF8)
  if (-not $xml) { Write-Output "DBG empty-xml"; return $null }
  Write-Output "DBG file=$name len=$($xml.Length) needleHas=$($xml.Contains($needle))"
  $rx = [regex]('content-desc="([^"]*' + [regex]::Escape($needle) + '[^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
  $m = $rx.Match($xml)
  if (-not $m.Success) {
    Write-Output "DBG desc-regex-miss"
    $rx2 = [regex]('text="([^"]*' + [regex]::Escape($needle) + '[^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
    $m = $rx2.Match($xml)
    if (-not $m.Success) { Write-Output "DBG text-regex-miss"; return $null }
  }
  $x = [int](([int]$m.Groups[2].Value + [int]$m.Groups[4].Value) / 2)
  $y = [int](([int]$m.Groups[3].Value + [int]$m.Groups[5].Value) / 2)
  Write-Output "DBG hit: $x,$y"
  return @($x, $y)
}

function Tap-Text([string]$name, [string]$needle) {
  $p = Find-Tap $name $needle
  if ($p) {
    A "input tap $($p[0]) $($p[1])" | Out-Null
    Start-Sleep 2
    return $true
  }
  return $false
}

if (-not (Ensure-Device)) { python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null; exit 1 }

# wake (no password on pad; if locked, log and abort gracefully)
A "input keyevent 224" | Out-Null
Start-Sleep 2
$lock = A "dumpsys window"
if ($lock -match 'mDreamingLockscreen=true') { Log "LOCKED (need user unlock), abort"; python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null; exit 0 }

# cold start xianyu
A "am force-stop com.taobao.idlefish" | Out-Null
Start-Sleep 2
A "monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1" | Out-Null
Start-Sleep 12

Dump-Uixml "${tag}_home"

# navigate to home feed root
$atHome = $false
for ($j = 1; $j -le 4; $j++) {
  $hc = [IO.File]::ReadAllText("$OutDir\${tag}_home.xml", [Text.Encoding]::UTF8)
  if ($hc -and ($hc -match ([regex]::Unescape("\u5356\u95f2\u90e8")))) { $atHome = $true; break }
  A "input keyevent 4" | Out-Null
  Start-Sleep 2
  Dump-Uixml "${tag}_home"
}

# dismiss upgrade popup if present
$hc2 = [IO.File]::ReadAllText("$OutDir\${tag}_home.xml", [Text.Encoding]::UTF8)
if ($hc2 -and ($hc2 -match ([regex]::Unescape("\u6682\u4e0d\u5347\u7ea7")))) {
  if (Tap-Text "${tag}_home" ([regex]::Unescape("\u6682\u4e0d\u5347\u7ea7"))) { Log "UPGRADE_POPUP dismissed"; Start-Sleep 2 }
}

$kwList = $Keywords.Split(",") | Where-Object { $_.Trim() -ne "" }
$ki = 0
foreach ($kw in $kwList) {
  $ki++
  $kw = $kw.Trim()
  $safe = $kw -replace '[^a-zA-Z0-9]', '_'
  Log "KEYWORD $ki/$($kwList.Count): $kw"

  Dump-Uixml "${tag}_${safe}_searchpage"
  # step 1: tap home search bar by resource-id (resolution independent)
  $f0 = "$OutDir\${tag}_${safe}_searchpage.xml"
  $hX = $null; $hY = $null
  if (Test-Path $f0) {
    $xml0 = [IO.File]::ReadAllText($f0, [Text.Encoding]::UTF8)
    foreach ($rid in @('id/default_search', 'id/fy26_home_search_bar', 'id/search_bar_layout')) {
      $m = [regex]::Match($xml0, ('resource-id="com\.taobao\.idlefish:' + $rid + '"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'))
      if ($m.Success) {
        $hX = [int](([int]$m.Groups[1].Value + [int]$m.Groups[3].Value) / 2)
        $hY = [int](([int]$m.Groups[2].Value + [int]$m.Groups[4].Value) / 2)
        break
      }
    }
  }
  if ($hX) { A "input tap $hX $hY" | Out-Null; Log "tapped home search bar ($hX,$hY)" }
  else { Log "WARN: home search bar id not found for $kw" }
  Start-Sleep 4
  Dump-Uixml "${tag}_${safe}_sbox"

  # step 2: locate the search input on the search page
  # NOTE: on this build the input is a plain EditText with NO resource-id,
  # so match by class + top-area position (search bar sits near y<250).
  $f = "$OutDir\${tag}_${safe}_sbox.xml"
  $edX = $null; $edY = $null
  if (Test-Path $f) {
    $xml = [IO.File]::ReadAllText($f, [Text.Encoding]::UTF8)
    $cands = [regex]::Matches($xml, 'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
    foreach ($c in $cands) {
      $x1 = [int]$c.Groups[1].Value; $y1 = [int]$c.Groups[2].Value
      $x2 = [int]$c.Groups[3].Value; $y2 = [int]$c.Groups[4].Value
      $w = $x2 - $x1
      if ($y1 -lt 260 -and $w -gt 400) {
        $edX = [int](($x1 + $x2) / 2); $edY = [int](($y1 + $y2) / 2)
        break
      }
    }
  }
  if ($edX) {
    A "input tap $edX $edY" | Out-Null
    Log "tapped search input ($edX,$edY)"
    Start-Sleep 2
    # clear any pre-filled keyword: select-all (ctrl+A) then delete, 3x for safety
    A "input keyevent --longpress 29" | Out-Null
    Start-Sleep 1
    A "input keyevent 123" | Out-Null   # move to end
    for ($d = 0; $d -lt 3; $d++) { A "input keyevent 67" | Out-Null; Start-Sleep 0.3 }
  }
  else { Log "WARN: search input not found for $kw" }
  Start-Sleep 2
  A "input text $kw" | Out-Null
  Start-Sleep 1
  A "input keyevent 66" | Out-Null
  Start-Sleep 5

  Dump-Uixml "${tag}_${safe}_res0"
  # personal-idle filter chip (new layout)
  if (Tap-Text "${tag}_${safe}_res0" "\u4e2a\u4eba\u95f2\u7f6e") { Log "filter personal-idle tapped"; Start-Sleep 3 }
  else { Log "WARN: personal-idle filter not found for $kw" }

  # collect 4 screens (pad is fast, screen is tall -> more items per screen)
  for ($i = 1; $i -le 4; $i++) {
    Dump-Uixml "${tag}_${safe}_s$i"
    if ($i -lt 4) {
      if (-not (Ensure-Device)) { break }
      A "input swipe 1000 2400 1000 700 400" | Out-Null
      Start-Sleep 2
    }
  }
  # back out to home for next keyword
  A "input keyevent 4" | Out-Null
  Start-Sleep 2
  A "input keyevent 4" | Out-Null
  Start-Sleep 2
  Dump-Uixml "${tag}_${safe}_after"
}

A "rm /sdcard/ui.xml" | Out-Null
python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null
Log "DONE $tag"

# === 三件套溯源（10-15 用户硬规则）===
$leadsMd = "$OutDir\${tag}_leads.md"
if (Test-Path $leadsMd) {
  $PYTHONIOENCODING = "utf-8"
  $env:PYTHONIOENCODING = "utf-8"
  python "$BaseDir\trace_leads.py" $Round $leadsMd $Device 2>&1 | Out-String | ForEach-Object { Log "TRACE: $_" }
} else {
  Log "TRACE: no leads.md, skip"
}
