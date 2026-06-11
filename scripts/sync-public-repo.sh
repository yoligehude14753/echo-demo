#!/usr/bin/env bash
set -euo pipefail

# Sync the public distribution repository content.
# This publishes documentation only; installers are uploaded by GitHub Releases.

PUBLIC_REPO="${PUBLIC_REPO:-yoligehude14753/echodesk-public}"
WORKDIR="${WORKDIR:-$(mktemp -d)}"
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_SRC="$SRC_ROOT/public"

if [[ ! -d "$PUBLIC_SRC" ]]; then
  echo "public source dir not found: $PUBLIC_SRC" >&2
  exit 1
fi

echo "Syncing public docs to $PUBLIC_REPO"
echo "Workdir: $WORKDIR"

if gh repo view "$PUBLIC_REPO" >/dev/null 2>&1; then
  gh repo clone "$PUBLIC_REPO" "$WORKDIR/repo"
else
  gh repo create "$PUBLIC_REPO" --public --description "EchoDesk public downloads and documentation" --clone=false
  git clone "https://github.com/$PUBLIC_REPO.git" "$WORKDIR/repo"
fi

cd "$WORKDIR/repo"

git config user.name "${GIT_AUTHOR_NAME:-github-actions[bot]}"
git config user.email "${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"

shopt -s dotglob nullglob
for item in *; do
  case "$item" in
    .git|.github)
      continue
      ;;
  esac
  rm -rf "$item"
done

rsync -a "$PUBLIC_SRC/" "$WORKDIR/repo/"

git add .
if git diff --cached --quiet; then
  echo "No public repo changes."
  exit 0
fi

git commit -m "docs: update public distribution docs"
git push origin HEAD:main

echo "Public repo synced: https://github.com/$PUBLIC_REPO"

