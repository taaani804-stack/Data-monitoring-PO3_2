#!/usr/bin/env bash
# Persists the scanner state between GitHub Actions runs on a dedicated orphan branch ("scanner-state"),
# so main stays free of runtime commits. Files kept on the branch (all optional):
#   sent_signals.json   - duplicate-prevention registry + per-timeframe scan progress
#   global_ranking.json - cached global futures-volume ranking (CoinMarketCap)
#   state_branch.sh load   -> writes state/<file> for each file found on the branch
#   state_branch.sh save   -> commits + pushes every existing state/<file> to the branch
# `save` uses git plumbing (hash-object / mktree / commit-tree) and NEVER touches the working tree, so it
# can safely be called in the middle of a scan run (the scanner does so after every timeframe scan).
set -euo pipefail

BRANCH="${STATE_BRANCH:-scanner-state}"
REMOTE="${STATE_REMOTE:-origin}"
DIR="${STATE_DIR:-state}"
FILES=("sent_signals.json" "global_ranking.json")

remote_has_branch() {
  git ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null 2>&1
}

load() {
  mkdir -p "$DIR"
  if ! remote_has_branch; then
    echo "No ${BRANCH} branch yet - starting empty (it is created on the first save)."
    return 0
  fi
  git fetch --no-tags --depth=1 "$REMOTE" "$BRANCH"
  local f
  for f in "${FILES[@]}"; do
    if git show "FETCH_HEAD:${f}" > "${DIR}/${f}.tmp" 2>/dev/null; then
      mv "${DIR}/${f}.tmp" "${DIR}/${f}"
      echo "Loaded ${f} from branch ${BRANCH}."
    else
      rm -f "${DIR}/${f}.tmp"
      echo "${f} not on the state branch yet."
    fi
  done
}

save() {
  export GIT_AUTHOR_NAME="github-actions[bot]" GIT_COMMITTER_NAME="github-actions[bot]"
  export GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
  export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

  local attempt f tree parent commit entries changed
  for attempt in 1 2 3 4; do
    parent=""
    if remote_has_branch; then
      git fetch --no-tags --depth=1 "$REMOTE" "$BRANCH"
      parent="$(git rev-parse FETCH_HEAD)"
    fi
    entries=""
    changed=0
    for f in "${FILES[@]}"; do
      if [ -f "${DIR}/${f}" ]; then
        entries+="$(printf '100644 blob %s\t%s\n' "$(git hash-object -w "${DIR}/${f}")" "$f")"$'\n'
        if [ -z "$parent" ] || [ "$(git show "FETCH_HEAD:${f}" 2>/dev/null | git hash-object --stdin)" != "$(git hash-object "${DIR}/${f}")" ]; then
          changed=1
        fi
      elif [ -n "$parent" ] && git cat-file -e "FETCH_HEAD:${f}" 2>/dev/null; then
        # keep a file that exists remotely but was not loaded locally (never silently drop state)
        entries+="$(printf '100644 blob %s\t%s\n' "$(git rev-parse "FETCH_HEAD:${f}")" "$f")"$'\n'
      fi
    done
    if [ -z "$entries" ]; then
      echo "No state files to save."
      return 0
    fi
    if [ "$changed" = "0" ]; then
      echo "State unchanged - nothing to push."
      return 0
    fi
    tree="$(printf '%s' "$entries" | git mktree)"
    if [ -n "$parent" ]; then
      commit="$(git commit-tree "$tree" -p "$parent" -m "scanner state $(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    else
      commit="$(git commit-tree "$tree" -m "scanner state $(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    fi
    if git push "$REMOTE" "${commit}:refs/heads/${BRANCH}"; then
      echo "State pushed to ${BRANCH}."
      return 0
    fi
    echo "Push attempt ${attempt} failed (remote moved?) - retrying" >&2
    sleep $((attempt * 2))
  done
  echo "ERROR: could not push state after 4 attempts" >&2
  return 1
}

main() {
  case "${1:-}" in
    load) load ;;
    save) save ;;
    *) echo "usage: $0 load|save" >&2; return 2 ;;
  esac
}

main "$@"
exit $?
