#!/usr/bin/env bash
# All local checks. Each tool-dependent check reports SKIP when the tool is
# missing instead of pretending; the exit status is non-zero only for a
# failure of a check that ran.
#
#   scripts/check.sh            # everything available
#   TERRAFORM=/path/to/terraform CFN_LINT=/path/to/cfn-lint scripts/check.sh
set -u
cd "$(dirname "$0")/.." || exit 1

fail=0
run() { # run LABEL CMD...
  local label=$1; shift
  if "$@"; then echo "PASS  $label"; else echo "FAIL  $label"; fail=1; fi
}
skip() { echo "SKIP  $1 ($2)"; }

echo "== python unit tests (the repository's test command)"
run "python3 -m unittest discover -s tests" python3 -m unittest discover -s tests

echo "== shell"
run "bash -n host script" bash -n lambda/spot_handoff/spot-handoff-notify.sh
run "bash -n check.sh" bash -n scripts/check.sh
if command -v shellcheck >/dev/null 2>&1; then
  run "shellcheck host script" shellcheck -S warning lambda/spot_handoff/spot-handoff-notify.sh
else
  skip "shellcheck" "not installed"
fi

echo "== cloudformation"
run "cfn_check.py (template vs example parameters, IAM parity)" python3 scripts/cfn_check.py
run "package-lambda.py (reproducible zip)" python3 scripts/package-lambda.py --out build/spot-handoff-lambda.zip
CFNLINT=${CFN_LINT:-cfn-lint}
if command -v "$CFNLINT" >/dev/null 2>&1; then
  run "cfn-lint" "$CFNLINT" cloudformation/spot-handoff.json
else
  skip "cfn-lint" "not on PATH; pipx install cfn-lint, or set CFN_LINT=/path/to/cfn-lint"
fi

echo "== terraform"
TF=${TERRAFORM:-terraform}
if command -v "$TF" >/dev/null 2>&1; then
  run "terraform fmt -check -recursive" "$TF" fmt -check -recursive -diff terraform examples
  for dir in terraform examples/basic; do
    # init -backend=false downloads providers only; validate needs no credentials.
    if (cd "$dir" && "$TF" init -backend=false -input=false >/dev/null 2>&1); then
      run "terraform validate $dir" bash -c "cd '$dir' && '$TF' validate -no-color"
    else
      skip "terraform validate $dir" "init failed: no network or registry access"
    fi
  done
else
  skip "terraform fmt/validate" "terraform not on PATH; set TERRAFORM=/path/to/terraform"
fi

exit "$fail"
