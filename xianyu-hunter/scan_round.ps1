param(
  [string]$Device = "AADEGL4701000138",
  [string]$WiFiDevice = "192.168.31.95:5555",
  [int]$Round = 2,
  [string]$ActiveStart = "09:00",
  [string]$ActiveEnd = "23:30",
  [int]$SleepMinutes = 0,
  [string]$SessionId = "xianyu-hunter",
  [string]$BaseDir = "E:\code\AutoTaskByMuseSpark1.3\xianyu-hunter",
  [string]$ParseScript = "C:\Users\22651\.config\opencode\skills\android-phone\scripts\parse_ui.py"
)
$ErrorActionPreference = "Continue"
$logFile = "$BaseDir\scan-log.md"
$OutDir = "$BaseDir\scans"
$LeaseTool = "$BaseDir\device-lease\devlease.py"

function Log($msg) {
  $line = "- $(Get-Date -Format 'MM-dd HH:mm:ss') R${Round}: $msg"
  Add-Content -Path $logFile -Value $line -Encoding UTF8
  Write-Output $msg
}

# 0) wait for scheduled slot
if ($SleepMinutes -gt 0) { Start-Sleep -Seconds ($SleepMinutes * 60) }

# 1) active window
$now = Get-Date
$t = $now.ToString("HH:mm")
if (($t -lt $ActiveStart) -or ($t -gt $ActiveEnd)) {
  Log "OUT_OF_WINDOW ($t), skip"
  exit 0
}

# 2) acquire device lease (multi-session coordination)
$leaseOut = python $LeaseTool acquire --device $Device --session $SessionId --ttl 900 --task "xianyu-scan-r$Round" 2>$null
if ($LASTEXITCODE -eq 2) {
  Log "LEASE_BUSY: $leaseOut (skip this round, no fighting)"
  exit 0
}
if ($LASTEXITCODE -ne 0) {
  Log "LEASE_ERROR: $leaseOut"
  exit 1
}
Log "LEASE_OK"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tag = "round$Round" + "_" + (Get-Date).ToString("MMdd_HHmm")

function Ensure-Device {
  $ok = adb -s $Device shell echo ok 2>$null
  if ("$ok".Trim() -eq "ok") { return $true }
  Log "DEVICE_STALE, recovering..."
  adb reconnect offline 2>$null | Out-Null
  adb connect $WiFiDevice 2>$null | Out-Null
  Start-Sleep 5
  $ok = adb -s $Device shell echo ok 2>$null
  if ("$ok".Trim() -eq "ok") { Log "RECOVERED (reconnect)"; return $true }
  $list = adb devices 2>$null | Out-String
  if ($list -match [regex]::Escape($WiFiDevice)) {
    $Device = $WiFiDevice
    Log "SWITCHED to wifi device"
    return $true
  }
  Start-Sleep 6
  $ok = adb -s $Device shell echo ok 2>$null
  if ("$ok".Trim() -eq "ok") { Log "RECOVERED (retry)"; return $true }
  Log "DEVICE_LOST"
  return $false
}

function Dump-Uixml([string]$name) {
  if (-not (Ensure-Device)) { return }
  adb -s $Device shell uiautomator dump /sdcard/ui.xml 2>$null | Out-Null
  adb -s $Device pull /sdcard/ui.xml "$OutDir\$name.xml" 2>$null | Out-Null
  $f = Get-Item "$OutDir\$name.xml" -ErrorAction SilentlyContinue
  if (-not $f -or $f.Length -lt 5000) {
    Start-Sleep 3
    if (-not (Ensure-Device)) { return }
    adb -s $Device shell uiautomator dump /sdcard/ui.xml 2>$null | Out-Null
    adb -s $Device pull /sdcard/ui.xml "$OutDir\$name.xml" 2>$null | Out-Null
  }
  python $ParseScript "$OutDir\$name.xml" > "$OutDir\$name.txt" 2>$null
}

# 3) wake + cold-restart xianyu
if (-not (Ensure-Device)) { python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null; exit 1 }
adb -s $Device shell input keyevent 224; Start-Sleep 1
adb -s $Device shell am force-stop com.taobao.idlefish; Start-Sleep 2
adb -s $Device shell monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1 2>$null | Out-Null
Start-Sleep 12
Dump-Uixml "${tag}_health"
$h = Get-Item "$OutDir\${tag}_health.xml" -ErrorAction SilentlyContinue
if (-not $h -or $h.Length -lt 10000) {
  adb -s $Device shell am force-stop com.taobao.idlefish; Start-Sleep 3
  adb -s $Device shell monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1 2>$null | Out-Null
  Start-Sleep 15
}
# navigate back to home feed safely (BACK never sends messages)
$atHome = $false
for ($j = 1; $j -le 5; $j++) {
  Dump-Uixml "${tag}_nav$j"
  $navContent = [IO.File]::ReadAllText("$OutDir\${tag}_nav$j.xml")
  if ($navContent -and ($navContent -match '卖闲置' -or $navContent -match '猜你喜欢')) { $atHome = $true; break }
  adb -s $Device shell input keyevent 4; Start-Sleep 2
}
if (-not $atHome) {
  adb -s $Device shell am force-stop com.taobao.idlefish; Start-Sleep 2
  adb -s $Device shell monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1 2>$null | Out-Null
  Start-Sleep 12
}
# dismiss app-upgrade popup if present (fresh dump)
Dump-Uixml "${tag}_popupchk"
$popContent = [IO.File]::ReadAllText("$OutDir\${tag}_popupchk.xml")
if ($popContent -match '暂不升级') {
  adb -s $Device shell input tap 211 924; Start-Sleep 2
  Log "UPGRADE_POPUP dismissed"
}
adb -s $Device shell input tap 85 1521; Start-Sleep 2   # xianyu home tab
adb -s $Device shell input tap 354 92; Start-Sleep 2    # search box
Dump-Uixml "${tag}_searchpage"
adb -s $Device shell input tap 267 92; Start-Sleep 1    # clear old keyword (if present)
adb -s $Device shell input tap 350 190; Start-Sleep 1   # input field
adb -s $Device shell input text "m1max64"; Start-Sleep 1
adb -s $Device shell input keyevent 66; Start-Sleep 4   # enter = search
# filters: sort newest, then personal idle
adb -s $Device shell input tap 423 256; Start-Sleep 3
adb -s $Device shell input tap 376 176; Start-Sleep 3
# collect 3 screens
for ($i = 1; $i -le 3; $i++) {
  Dump-Uixml "${tag}_s$i"
  if ($i -lt 3) {
    if (-not (Ensure-Device)) { exit 1 }
    adb -s $Device shell input swipe 360 1300 360 500 500; Start-Sleep 3
  }
}
if (-not (Ensure-Device)) { exit 1 }
adb -s $Device shell screencap -p /sdcard/s.png
adb -s $Device pull /sdcard/s.png "$OutDir\${tag}_final.png" 2>$null | Out-Null
adb -s $Device shell rm /sdcard/ui.xml /sdcard/s.png 2>$null | Out-Null
python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null
Log "DONE $tag"
