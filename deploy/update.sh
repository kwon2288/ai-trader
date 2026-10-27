#!/usr/bin/env bash
# GitHub 최신 버전으로 업데이트:  bash deploy/update.sh
# .env · config.toml · data/ 는 git 에 없으므로 그대로 유지됩니다.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "▶ 현재: $(git log --oneline -1)"
git fetch --quiet origin
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
  echo "✅ 이미 최신입니다."
  exit 0
fi
git log --oneline HEAD..origin/main | sed 's/^/  + /'
git pull --ff-only --quiet origin main

.venv/bin/pip install --quiet -r requirements.txt
if systemctl is-active --quiet ai-trader-web; then
  systemctl restart ai-trader-web && echo "↻ 웹 관리 화면 재시작"
fi
echo "✅ 업데이트 완료: $(git log --oneline -1)"
