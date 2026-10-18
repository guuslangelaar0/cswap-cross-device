#!/usr/bin/env bash
# check-secrets.sh - refuse to commit secrets.
#
#   scripts/check-secrets.sh               scan the files staged for commit (pre-commit mode)
#   scripts/check-secrets.sh --all         scan every tracked and untracked, non-ignored file
#   scripts/check-secrets.sh FILE...       scan the given files
#   scripts/check-secrets.sh --self-test   prove the check goes red on planted fake secrets
#
# Install as a pre-commit hook (once per clone):
#   ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit
#
# Blocks: private key blocks, GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_/github_pat_),
# Anthropic (sk-ant-) and other sk- style API keys, AWS access keys and secret keys,
# .env files, and long base64 blobs. Exit 0 = clean, 1 = findings, 2 = usage error.
# Findings print as file:line [rule]; the matched value itself is never printed.

set -u

RULES_NAMES=(
  "private-key"
  "github-token"
  "github-pat"
  "anthropic-key"
  "sk-api-key"
  "aws-access-key"
  "aws-secret-key"
)
RULES_RES=(
  '-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY( BLOCK)?-----'
  '(^|[^A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36}'
  'github_pat_[A-Za-z0-9_]{22,}'
  'sk-ant-[A-Za-z0-9_-]{20,}'
  '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}'
  '(^|[^A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}([^A-Z0-9]|$)'
  '[Aa][Ww][Ss]_?[Ss][Ee][Cc][Rr][Ee][Tt]_?[Aa][Cc][Cc][Ee][Ss][Ss]_?[Kk][Ee][Yy][[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9/+]{40}'
)
# Base64: a run of 41+ base64 characters that mixes upper case, lower case and digits
# (a 40-character hex commit SHA does not qualify).
B64_RE='[A-Za-z0-9+/]{41,}={0,2}'

findings=0

report() { # file line rule
  printf '%s:%s [%s]\n' "$1" "$2" "$3"
  findings=$((findings + 1))
}

is_env_file() { # path
  local base="${1##*/}"
  case "$base" in
    .env.example|.env.sample|.env.template) return 1 ;;
    .env|.env.*) return 0 ;;
  esac
  return 1
}

scan_stream() { # label ; content on stdin
  local label="$1" content i hit m line
  content="$(cat)"
  [ -n "$content" ] || return 0
  # Skip binary content.
  if ! printf '%s' "$content" 2>/dev/null | LC_ALL=C grep -Iq . 2>/dev/null; then  # grep -q may exit first
    return 0
  fi
  i=0
  while [ "$i" -lt "${#RULES_NAMES[@]}" ]; do
    while IFS= read -r hit; do
      [ -n "$hit" ] && report "$label" "${hit%%:*}" "${RULES_NAMES[$i]}"
    done < <(printf '%s\n' "$content" | LC_ALL=C grep -nE -e "${RULES_RES[$i]}" 2>/dev/null)
    i=$((i + 1))
  done
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    line="${hit%%:*}"
    m="${hit#*:}"
    case "$m" in *[0-9]*) ;; *) continue ;; esac
    case "$m" in *[a-z]*) ;; *) continue ;; esac
    case "$m" in *[A-Z]*) ;; *) continue ;; esac
    report "$label" "$line" "long-base64"
  done < <(printf '%s\n' "$content" | LC_ALL=C grep -noE -e "$B64_RE" 2>/dev/null)
}

scan_path_name() { # path
  if is_env_file "$1"; then
    report "$1" 0 "env-file"
  fi
}

scan_staged() {
  local f
  while IFS= read -r -d '' f; do
    scan_path_name "$f"
    scan_stream "$f" < <(git show ":$f" 2>/dev/null)
  done < <(git diff --cached --name-only --diff-filter=ACMR -z)
}

scan_files() { # files...
  local f
  for f in "$@"; do
    scan_path_name "$f"
    [ -f "$f" ] || continue
    scan_stream "$f" < "$f"
  done
}

scan_all() {
  local f
  local files=()
  while IFS= read -r -d '' f; do
    files+=("$f")
  done < <(git ls-files -z --cached --others --exclude-standard)
  [ "${#files[@]}" -gt 0 ] || return 0
  scan_files "${files[@]}"
}

self_test() {
  local self tmp body pass=0 fail=0 out rc name content file expect
  self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/check-secrets-selftest.XXXXXX")" || exit 2
  # The fake values are assembled at run time, so this file never contains one.
  body="aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5"   # 36 characters, upper/lower/digits
  run_case() { # name expect(red|green) file content
    name="$1"; expect="$2"; file="$3"; content="$4"
    rm -rf "$tmp/repo"
    mkdir -p "$tmp/repo"
    (
      cd "$tmp/repo" || exit 2
      git init -q . && git config user.email selftest@example.invalid && git config user.name selftest
      mkdir -p "$(dirname "$file")"
      printf '%s\n' "$content" > "$file"
      git add -- "$file"
    ) || { echo "self-test: setup failed for $name"; fail=$((fail + 1)); return; }
    out="$(cd "$tmp/repo" && "$self" 2>&1)"
    rc=$?
    if { [ "$expect" = red ] && [ "$rc" -eq 1 ]; } || { [ "$expect" = green ] && [ "$rc" -eq 0 ]; }; then
      printf 'ok    %-22s expected %-5s rc=%s %s\n' "$name" "$expect" "$rc" "$(printf '%s' "$out" | tr '\n' ' ')"
      pass=$((pass + 1))
    else
      printf 'FAIL  %-22s expected %-5s rc=%s %s\n' "$name" "$expect" "$rc" "$(printf '%s' "$out" | tr '\n' ' ')"
      fail=$((fail + 1))
    fi
  }
  run_case "github-token"   red   "config.py"  "TOKEN = \"gh""p_${body}\""
  run_case "github-oauth"   red   "a/b.txt"    "auth: gh""o_${body}"
  run_case "github-pat"     red   "notes.md"   "github""_pat_11ABCDEFG0${body}"
  run_case "anthropic-key"  red   "run.sh"     "export ANTHROPIC_API_KEY=sk-""ant-api03-${body}"
  run_case "sk-api-key"     red   "client.py"  "key = 'sk""-proj-${body}'"
  run_case "aws-access-key" red   "aws.cfg"    "aws_access_key_id = AK""IAIOSFODNN7EXAMPLE"
  run_case "aws-secret-key" red   "aws.ini"    "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCY${body:0:10}"
  run_case "private-key"    red   "id_test"    "-----BEGIN ""OPENSSH PRIVATE KEY-----"
  run_case "long-base64"    red   "blob.json"  "{\"data\": \"${body}${body}\"}"
  run_case "env-file"       red   ".env"       "HARMLESS=1"
  run_case "clean-file"     green "README.md"  "See task-AbCdEfGhIjKlMnOpQrStUvWx0123 and commit 3d3c42e5aac5ba805825da76410c181273ba90b1."
  run_case "env-example"    green ".env.example" "API_KEY=changeme"
  rm -rf "$tmp"
  echo "self-test: $pass passed, $fail failed"
  [ "$fail" -eq 0 ]
}

case "${1:-}" in
  --self-test) self_test; exit $? ;;
  --all) scan_all ;;
  -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  -*) echo "check-secrets: unknown option $1" >&2; exit 2 ;;
  "") scan_staged ;;
  *) scan_files "$@" ;;
esac

if [ "$findings" -gt 0 ]; then
  echo "check-secrets: $findings finding(s). Remove the secret (and rotate it if it was ever real)." >&2
  exit 1
fi
exit 0
