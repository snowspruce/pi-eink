#!/bin/bash
# eink-addr on|off — 切换 Kindle 浏览器地址栏（跑在电脑上，通过 adb 操作设备）。
#
# stock AOSP 浏览器只有两个状态：地址栏常驻，或 quick-controls 隐藏态。隐藏态要用
# 「贴右边缘往左滑」才唤得出来（触发带只有 ~20dp），实测不可靠 —— 所以开关放在这里，
# 一条命令、可预期。
#   on  = 地址栏常驻（quick_controls=false）
#   off = 隐藏地址栏（quick_controls=true，页面多出约 100px）
#
# 设备侧前提：有 su、浏览器 uid 是 10018（`adb shell dumpsys package com.android.browser
# | grep userId` 确认）、自启 URL 存在 /data/local/tmp/eink-url。
set -euo pipefail

want="${1:-}"
case "$want" in
  on)  val=false ;;
  off) val=true ;;
  *)   echo "usage: $(basename "$0") on|off" >&2; exit 2 ;;
esac

PF=/data/data/com.android.browser/shared_prefs/com.android.browser_preferences.xml
BROWSER_UID="${EINK_BROWSER_UID:-10018}"

adb shell "am force-stop com.android.browser" >/dev/null
adb shell "su -c 'cp $PF /sdcard/prefs.xml'" >/dev/null
adb pull /sdcard/prefs.xml /tmp/eink_prefs.xml >/dev/null 2>&1

python3 - "$val" <<'PY'
import re, sys

v = sys.argv[1]
p = "/tmp/eink_prefs.xml"
s = open(p).read()
pat = r'<boolean name="enable_quick_controls" value="(true|false)" />'
new = '<boolean name="enable_quick_controls" value="%s" />' % v
# 只 patch 这一个键：写完整 prefs 会覆盖掉设备上其它设置（autofit_pages 之类缺了
# 渲染就变样）。键不存在时才追加。
s = re.sub(pat, new, s) if re.search(pat, s) else s.replace("</map>", "    %s\n</map>" % new)
open(p, "w").write(s)
PY

adb push /tmp/eink_prefs.xml /sdcard/prefs.xml >/dev/null
adb shell "su -c 'cp /sdcard/prefs.xml $PF; chown $BROWSER_UID:$BROWSER_UID $PF; chmod 660 $PF'" >/dev/null

url=$(adb shell 'su -c "cat /data/local/tmp/eink-url"' | tr -d '\r')
if [ -n "$url" ]; then
  adb shell "am start -a android.intent.action.VIEW -d '$url'" >/dev/null 2>&1
fi
echo "地址栏: $want (quick_controls=$val)"
