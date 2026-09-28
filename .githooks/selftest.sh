#!/bin/sh
# Self-check for the hooks: ./.githooks/selftest.sh (needs gitleaks, like the hooks).
# Builds a throwaway repo and asserts each guard blocks what it should and passes a clean commit.
set -eu
hooks=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
git init -q
git config core.hooksPath "$hooks"
git config user.name test
git config user.email 1+test@users.noreply.github.com
printf 'secret-host\\.example\n' > .private-terms
echo .private-terms > .gitignore
git add .gitignore

expect() { # expect pass|block <what> <command...>
  want=$1 what=$2
  shift 2
  if "$@" >/dev/null 2>&1; then got=pass; else got=block; fi
  [ "$got" = "$want" ] || { echo "FAIL: $what (wanted $want, got $got)"; exit 1; }
  echo "ok: $what"
}

expect pass "clean commit" git commit -q -m "chore: init"
echo "host secret-host.example" > leak.txt
git add leak.txt
expect block "term in a staged file" git commit -q -m "add"
git rm -q --cached leak.txt
rm leak.txt
echo fine > ok.txt
git add ok.txt
expect block "term in the commit message" git commit -q -m "deploy to secret-host.example"
expect block "non-noreply email" git -c user.email=me@example.com commit -q -m "add ok"
printf '(unclosed\n' >> .private-terms
expect block "malformed pattern" git commit -q -m "add ok"
