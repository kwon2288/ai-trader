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

# 이미 설치된 systemd 파일이 바뀌었으면 함께 갱신 (설치 안 한 것은 건드리지 않음)
reload=0
for f in deploy/*.service deploy/*.timer; do
  dst="/etc/systemd/system/$(basename "$f")"
  if [ -f "$dst" ] && ! cmp -s "$f" "$dst"; then
    cp "$f" "$dst" && echo "  · $(basename "$f") 갱신" && reload=1
  fi
done
# 미국 자동 실행만 설치돼 있고 국내가 빠졌으면 국내도 설치 (둘 다 쓰는 게 기본)
if [ -f /etc/systemd/system/ai-trader-us.timer ] && [ ! -f /etc/systemd/system/ai-trader.timer ]; then
  cp deploy/ai-trader.service deploy/ai-trader.timer /etc/systemd/system/ && echo "  · 국내 자동 실행 타이머 설치" && reload=1
fi
[ "$reload" = 1 ] && systemctl daemon-reload
# 설치된 자동 실행 타이머가 꺼져 있으면 켬 (재부팅 후 안 켜지는 경우 등)
for t in ai-trader.timer ai-trader-us.timer; do
  if [ -f "/etc/systemd/system/$t" ] && ! systemctl is-enabled --quiet "$t"; then
    systemctl enable --now "$t" && echo "  · $t 켬"
  elif [ -f "/etc/systemd/system/$t" ] && ! systemctl is-active --quiet "$t"; then
    systemctl start "$t" && echo "  · $t 시작"
  fi
done
# 장중 감시 타이머: 자동 실행을 설치해 둔 서버면 설정(watch_minutes)대로 처음 한 번 설치
if [ -f /etc/systemd/system/ai-trader.timer ] && [ ! -f /etc/systemd/system/ai-trader-watch-kr.timer ]; then
  .venv/bin/python run.py timers || true
fi
if systemctl is-active --quiet ai-trader-web; then
  systemctl restart ai-trader-web && echo "↻ 웹 관리 화면 재시작"
fi
echo "✅ 업데이트 완료: $(git log --oneline -1)"
