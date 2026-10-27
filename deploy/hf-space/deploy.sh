#!/usr/bin/env bash
# Push deploy/hf-space/ to a Hugging Face Space with plain git (no third-party action).
#
# Env:
#   HF_SPACE_ID      owner/name of the Space (required)
#   RTDP_DEPLOY_SHA  the 40-hex commit being deployed; replaces the placeholder in requirements.txt
#   HF_TOKEN         access token, fine-grained with write access to that one Space only
#   HF_USERNAME      account the token belongs to (default: the owner part of HF_SPACE_ID)
#   HF_GIT_BASE      git base URL (default https://huggingface.co/spaces; tests point it at a
#                    local bare repository)
#
# The token travels as an HTTP basic-auth header set through environment variables, so it never
# appears in a command line. The push is a normal fast-forward push, never forced.
set -euo pipefail

space_id="${HF_SPACE_ID:?HF_SPACE_ID is required (owner/name)}"
sha="${RTDP_DEPLOY_SHA:?RTDP_DEPLOY_SHA is required (40-hex commit)}"
[[ "$space_id" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || {
  echo "HF_SPACE_ID must look like owner/name" >&2
  exit 2
}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "RTDP_DEPLOY_SHA must be a 40-character lowercase hex commit id" >&2
  exit 2
}

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="${HF_GIT_BASE:-https://huggingface.co/spaces}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Stage exactly the files that make up the Space, with the commit placeholder filled in.
stage="$work/stage"
mkdir -p "$stage"
cp "$src/app.py" "$src/requirements.txt" "$src/README.md" "$stage/"
sed -i.bak "s/__RTDP_COMMIT_SHA__/$sha/g" "$stage/requirements.txt"
rm -f "$stage/requirements.txt.bak"
if grep -q '__RTDP_COMMIT_SHA__' "$stage/requirements.txt" || ! grep -q "@$sha" "$stage/requirements.txt"; then
  echo "requirements.txt must pin rtdp to @$sha after substitution" >&2
  exit 1
fi

if [ -n "${HF_TOKEN:-}" ]; then
  user="${HF_USERNAME:-${space_id%%/*}}"
  b64="$(printf '%s:%s' "$user" "$HF_TOKEN" | base64 | tr -d '\n')"
  # In Actions, also mask the encoded form (GitHub masks the raw secret on its own).
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then echo "::add-mask::$b64"; fi
  export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
  export GIT_CONFIG_VALUE_0="Authorization: Basic $b64"
fi

git clone --quiet --depth 1 "$base/$space_id" "$work/space"
cd "$work/space"
branch="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"

# Replace the Space's files with the staged ones; keep .git and the Hub's own .gitattributes.
find . -mindepth 1 -maxdepth 1 ! -name .git ! -name .gitattributes -exec rm -rf {} +
cp -R "$stage"/. .
git add -A
if git diff --cached --quiet; then
  echo "Space already up to date; nothing to push."
  exit 0
fi
git -c user.name="rtdp deploy" -c user.email="rtdp-deploy@users.noreply.huggingface.co" \
  commit --quiet -m "Deploy $sha"
git push --quiet origin "HEAD:$branch"
echo "Pushed $sha to $space_id ($branch)"
