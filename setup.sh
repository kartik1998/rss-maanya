#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup.sh  —  one-time setup for the Daily Digest
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INDEX_PY="$SCRIPT_DIR/index.py"
CONFIG="$SCRIPT_DIR/config.json"
SAMPLE="$SCRIPT_DIR/config.sample.json"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Daily Digest — Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 1. Python check ──────────────────────────────────────────────────────────
PYTHON=$(which python3 2>/dev/null || echo "")
if [ -z "$PYTHON" ]; then
  echo "✗  python3 not found. Install it from https://python.org and rerun."
  exit 1
fi
echo "✓  Found: $PYTHON  ($($PYTHON --version 2>&1))"

# ── 2. Install pip packages ──────────────────────────────────────────────────
echo ""
echo "Installing Python packages…"
$PYTHON -m pip install --quiet --upgrade feedparser requests
echo "✓  feedparser, requests installed."

# ── 3. Seed config.json ──────────────────────────────────────────────────────
echo ""
if [ ! -f "$CONFIG" ]; then
  if [ ! -f "$SAMPLE" ]; then
    echo "✗  Missing $SAMPLE — cannot seed config.json."
    exit 1
  fi
  cp "$SAMPLE" "$CONFIG"
  echo "✓  Created $CONFIG from config.sample.json"
else
  echo "✓  config.json already exists — will update fields interactively."
fi

# Helper: update a single key inside config.json
update_cfg () {   # $1 = JSON path (e.g. name, phone_number, smtp.user), $2 = value
  $PYTHON - "$CONFIG" "$1" "$2" <<'PY'
import json, sys
path, key_path, value = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: cfg = json.load(f)
node, keys = cfg, key_path.split(".")
for k in keys[:-1]:
    node = node.setdefault(k, {})
# Cast bools / numbers where obvious
if value.lower() in ("true", "false"):
    node[keys[-1]] = value.lower() == "true"
elif value.isdigit():
    node[keys[-1]] = int(value)
else:
    node[keys[-1]] = value
with open(path, "w") as f: json.dump(cfg, f, indent=2)
PY
}

# ── 4. Name ──────────────────────────────────────────────────────────────────
echo ""
echo "━━━  Your Name  ━━━"
CURRENT_NAME=$($PYTHON -c "import json;print(json.load(open('$CONFIG'))['name'])")
echo "  Current: $CURRENT_NAME  (header will read: \"$CURRENT_NAME's Daily Digest\")"
read -r -p "  Enter your name (or Enter to keep): " NEWNAME
[ -n "$NEWNAME" ] && update_cfg "name" "$NEWNAME" && echo "✓  Name → $NEWNAME"

# ── 5. Phone number ──────────────────────────────────────────────────────────
echo ""
echo "━━━  iMessage Phone Number  ━━━"
echo "  Format: +91XXXXXXXXXX  (with country code)"
CURRENT_PHONE=$($PYTHON -c "import json;print(json.load(open('$CONFIG'))['phone_number'])")
echo "  Current: $CURRENT_PHONE"
read -r -p "  Enter phone (or Enter to keep): " PHONE
[ -n "$PHONE" ] && update_cfg "phone_number" "$PHONE" && echo "✓  Phone → $PHONE"

# ── 6. Groq API key ──────────────────────────────────────────────────────────
echo ""
echo "━━━  Groq API Key  ━━━"
echo "  Get a free key at: https://console.groq.com"
CURRENT_KEY=$($PYTHON -c "import json;print(json.load(open('$CONFIG')).get('groq_api_key','') or '(not set)')")
echo "  Current: ${CURRENT_KEY:0:10}…"
read -r -p "  Paste your Groq API key (or Enter to keep / use GROQ_API_KEY env var): " KEY
[ -n "$KEY" ] && update_cfg "groq_api_key" "$KEY" && echo "✓  Key saved to config.json"

# ── 7. Email delivery (optional) ─────────────────────────────────────────────
echo ""
echo "━━━  Email Delivery (optional)  ━━━"
read -r -p "  Enable email delivery? [y/N]: " USE_EMAIL
if [[ "$USE_EMAIL" =~ ^[Yy]$ ]]; then
  read -r -p "  SMTP host (default smtp.gmail.com): " H
  read -r -p "  SMTP user (your email): "             U
  read -r -s -p "  SMTP password (app-password, hidden): " P; echo
  read -r -p "  Send TO (default = same as user): "   T
  update_cfg "delivery.send_email" "true"
  [ -n "$H" ] && update_cfg "smtp.host"     "$H"
  [ -n "$U" ] && update_cfg "smtp.user"     "$U"
  [ -n "$P" ] && update_cfg "smtp.password" "$P"
  [ -n "$T" ] && update_cfg "smtp.to"       "$T"
  echo "✓  Email enabled in config.json"
  echo "   (Gmail users: create an app password at myaccount.google.com/apppasswords)"
fi

# ── 8. Test run ──────────────────────────────────────────────────────────────
echo ""
echo "━━━  Test Run  ━━━"
read -r -p "  Run the digest now to verify everything works? [Y/n]: " RUN
if [[ ! "$RUN" =~ ^[Nn]$ ]]; then
  echo ""
  $PYTHON "$INDEX_PY"
fi

# ── 9. Cron instructions ─────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Schedule it to run at 8 AM every morning"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  1. Open your crontab:"
echo "       crontab -e"
echo ""
echo "  2. Add this line:"
echo ""
echo "       0 8 * * * cd $SCRIPT_DIR && $PYTHON index.py >> digest.log 2>&1"
echo ""
echo "  To verify: crontab -l"
echo "  To debug:  tail -f $SCRIPT_DIR/digest.log"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete! ✓"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
