#!/usr/bin/env bash
# 커밋 메시지가 "vX.Y.Z: ..." 로 시작하는 커밋마다 버전 태그를 달고 GitHub에 올린다 (이미 있는 태그는 건너뜀).
#   bash deploy/tag-versions.sh          # 태그 만들고 push
#   bash deploy/tag-versions.sh --dry    # 무엇을 만들지 보기만
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch --quiet --tags origin
made=0
while read -r sha subject; do
  ver="${subject%%:*}"
  [[ "$ver" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
  git rev-parse -q --verify "refs/tags/$ver" >/dev/null && continue
  if [ "${1:-}" = "--dry" ]; then
    echo "  + $ver  $sha  ${subject#*: }"
  else
    git tag -a "$ver" "$sha" -m "${subject#*: }" && echo "  + $ver  ${subject#*: }"
  fi
  made=$((made + 1))
done < <(git log --reverse --format='%h %s' origin/main)
if [ "$made" = 0 ]; then echo "✅ 새로 달 태그가 없습니다."; exit 0; fi
[ "${1:-}" = "--dry" ] && exit 0
git push --quiet origin --tags && echo "✅ 태그 ${made}개를 GitHub에 올렸습니다."
