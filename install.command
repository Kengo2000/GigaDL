#!/bin/bash
# GigaDL インストーラ
#   ターミナルで:  bash install.command
# /Applications/GigaDL.app を作成します（書き込めない場合は ~/Applications）
# 何度実行しても上書き更新されるだけです
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/GigaDL"
APPCODE="$SUPPORT/app"
VENV="$SUPPORT/venv"
if [ -w /Applications ]; then
  APP="/Applications/GigaDL.app"
else
  APP="$HOME/Applications/GigaDL.app"
fi

echo "== GigaDL をインストールします =="

# --- Python を探す（3.9以上）
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3 が見つかりません。"
  echo "ターミナルで  xcode-select --install  を実行してから、もう一度このインストーラを実行してください。"
  exit 1
fi
echo "Python: $PY ($("$PY" --version))"

# --- コード配置と仮想環境
mkdir -p "$APPCODE"
cp "$SRC/engine.py" "$SRC/gigadl.py" "$SRC/platform_util.py" "$SRC/make_icon.py" "$SRC/uploader.py" "$SRC/upload_ui.py" "$APPCODE/"
if [ ! -x "$VENV/bin/python" ]; then
  echo "仮想環境を作成中…"
  "$PY" -m venv "$VENV"
fi
echo "ライブラリをインストール中…（初回は数分かかります）"
"$VENV/bin/python" -m pip install --upgrade pip -q
"$VENV/bin/python" -m pip install -q -r "$SRC/requirements.txt"

# --- .app を作成
rm -rf "${APP}"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/MacOS/GigaDL" <<EOF
#!/bin/bash
cd "$APPCODE"
exec "$VENV/bin/python" "$APPCODE/gigadl.py" >> "$SUPPORT/gigadl.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/GigaDL"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>GigaDL</string>
  <key>CFBundleDisplayName</key><string>GigaDL</string>
  <key>CFBundleIdentifier</key><string>local.gigadl</string>
  <key>CFBundleExecutable</key><string>GigaDL</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.3.1</string>
  <key>CFBundleIconFile</key><string>GigaDL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# --- アイコン（失敗しても動作には影響なし）
if "$VENV/bin/python" "$APPCODE/make_icon.py" "$APPCODE" 2>/dev/null; then
  cp "$APPCODE/GigaDL.icns" "$APP/Contents/Resources/" 2>/dev/null || true
fi
touch "${APP}"

echo ""
echo "✅ インストール完了: ${APP}"
echo "   Spotlight で「GigaDL」と入力して起動できます。"
echo "   このフォルダ（${SRC}）は削除してかまいません。"
open "${APP}"
