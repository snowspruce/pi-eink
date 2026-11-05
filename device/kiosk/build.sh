#!/bin/bash
# Build the kiosk APK. Needs a JDK (17+) and an Android SDK with build-tools + android-19.
#
#   ANDROID_SDK=/tmp/asdk JAVA_HOME=/path/to/jdk ./build.sh
#
# Why a custom app at all: the Kindle's stock browser (AOSP Browser 4.4.2) keeps a stale
# 89px content offset once it collapses its quick-controls toolbar - a black band over the
# page - and, being a fullscreen window, it is never resized for the soft keyboard, so the
# input box ends up underneath it. Both are that app's UI, not the WebView's. This app is
# ~120 lines and has no UI of its own to get wrong.
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_SDK:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
BT="$SDK/build-tools/34.0.0"
AJ="$SDK/platforms/android-19/android.jar"
: "${JAVA_HOME:=}"
[ -n "$JAVA_HOME" ] && PATH="$JAVA_HOME/bin:$PATH"

for f in "$BT/aapt2" "$BT/d8" "$BT/apksigner" "$AJ"; do
  [ -e "$f" ] || { echo "缺少 $f（ANDROID_SDK 指错了？需要 build-tools/34.0.0 + platforms/android-19）" >&2; exit 1; }
done

rm -rf out && mkdir -p out/classes out/dex
javac -nowarn -source 8 -target 8 -bootclasspath "$AJ" -d out/classes \
  src/ink/eink/kiosk/Kiosk.java 2>&1 | grep -vE "warning|bootstrap" || true

"$BT/aapt2" link -o out/base.apk --manifest AndroidManifest.xml -I "$AJ" \
  --min-sdk-version 19 --target-sdk-version 19 --version-code 1 --version-name 1.0
"$BT/d8" --min-api 19 --lib "$AJ" --output out/dex/ $(find out/classes -name '*.class')

python3 - <<'PY'
import shutil, zipfile
shutil.copy("out/base.apk", "out/unsigned.apk")
with zipfile.ZipFile("out/unsigned.apk", "a") as z:
    z.write("out/dex/classes.dex", "classes.dex")
PY

# A throwaway key: this APK only ever talks to one machine on the LAN.
[ -f debug.keystore ] || keytool -genkeypair -keystore debug.keystore -storepass android \
  -keypass android -alias kiosk -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=pi-eink Kiosk" >/dev/null 2>&1
"$BT/apksigner" sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android \
  --min-sdk-version 19 --out out/kiosk.apk out/unsigned.apk 2>&1 | grep -v WARNING || true

echo "APK: $(pwd)/out/kiosk.apk ($(wc -c < out/kiosk.apk) 字节)"
echo "安装: adb install -r out/kiosk.apk"
