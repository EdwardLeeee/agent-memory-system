#!/bin/bash
# fails_on.sh <commit> <test id>... : run today's tests against an older
# program (spec 記憶路由樹 v1.1, 8.0 ruling 52: every new test must fail on
# the commit before its fix). Test ids are dotted from the repo root
# (tests.test_router.Class.test_name). The commit's code is archived into a
# fresh temporary directory beside a copy of the working tree's tests.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
commit=$1; shift
for t in "$@"; do
  case "$t" in
    tests.*) ;;
    *) echo "test ids start with tests. (tests.test_router.Class.test_name): $t" >&2; exit 2 ;;
  esac
done
dir=$(mktemp -d "${TMPDIR:-/tmp}/fails-on.XXXXXX")
git -C "$repo" archive "$commit" agent_memory bin docs adapters MEMORY_PROTOCOL.md tools | tar -x -C "$dir"
cp -a "$repo/tests" "$dir/tests"
cd "$dir" || exit 2
for t in "$@"; do
  if timeout 600 python3 -m unittest "$t" >/dev/null 2>&1; then echo "PASSES on $commit: $t"; else echo "fails on $commit: $t"; fi
done
echo "(code and tests left in $dir)"
