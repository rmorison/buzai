#!/bin/sh
# Proves each buzai shape rule in .gitleaks.toml (the block after "buzai
# additions") fails on a planted value and passes its near misses, before any
# real scan is trusted. CI runs this from .github/workflows/ci.yml; run it
# locally with the pinned gitleaks on PATH, or with GITLEAKS set to its path.
# scripts/test-leak-gate.sh, copied from engineering-standards, proves the
# upstream rules and the wrapper; this script covers only buzai's additions.
#
# It follows test-leak-gate.sh's conventions. Planted values are assembled from
# shell variables at run time, so this file matches none of the rules it tests.
# Fixtures live in a temporary directory and never enter a commit. A planted
# value must exit exactly 3 and name its rule, because gitleaks also exits
# non-zero on a missing config or a bad path. The planted text must be absent
# from the output, because the output of a CI run is public.
#
# Usage:  sh scripts/test-shape-rules.sh
# Exits non-zero if any fixture fails. Needs git and gitleaks; nothing else.

set -eu

ROOT=$(git rev-parse --show-toplevel)
GITLEAKS=${GITLEAKS:-gitleaks}
CONFIG="$ROOT/.gitleaks.toml"
TEMPLATE="$ROOT/scripts/gitleaks-report.tmpl"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

passed=0
failed=0
ok() { passed=$((passed + 1)); }
bad() { echo "FAIL: $*" >&2; failed=$((failed + 1)); }

# The flags every gitleaks call in CI uses (.github/workflows/leaks.yml).
scan() {
  "$GITLEAKS" dir "$1" --config "$CONFIG" --no-banner --no-color --log-level warn \
    --redact --ignore-gitleaks-allow --exit-code 3 \
    --report-format template --report-template "$TEMPLATE" --report-path -
}

n=0
# leak <rule> <description> <value> <line>: the line, which holds the value,
# must be refused by that rule, and neither the line nor the bare value may
# appear in the output.
leak() {
  rule=$1 desc=$2 value=$3 line=$4
  n=$((n + 1))
  mkdir -p "$WORK/$n"
  printf '%s\n' "$line" > "$WORK/$n/notes.md"
  rc=0
  out=$(scan "$WORK/$n" 2>&1) || rc=$?
  if [ "$rc" -ne 3 ]; then
    bad "$desc: exit $rc, expected 3"
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    return
  fi
  case $out in
    *"[$rule]"*) ;;
    *) bad "$desc: refused, but not by $rule"; return ;;
  esac
  case $out in
    *"$line"* | *"$value"*) bad "$desc: the planted text appears in the output"; return ;;
  esac
  ok
}

# leaks <rule> <description> <count> <content>: the content must be refused
# with exactly <count> findings of that rule.
leaks() {
  rule=$1 desc=$2 want=$3 content=$4
  n=$((n + 1))
  mkdir -p "$WORK/$n"
  printf '%s\n' "$content" > "$WORK/$n/notes.md"
  rc=0
  out=$(scan "$WORK/$n" 2>&1) || rc=$?
  got=$(printf '%s\n' "$out" | grep -c "\[$rule\]" || true)
  if [ "$rc" -ne 3 ] || [ "$got" -ne "$want" ]; then
    bad "$desc: exit $rc with $got findings, expected 3 with $want"
    return
  fi
  ok
}

# clean <description> <line>: the line must pass every rule.
clean() {
  desc=$1 line=$2
  n=$((n + 1))
  mkdir -p "$WORK/$n"
  printf '%s\n' "$line" > "$WORK/$n/notes.md"
  rc=0
  out=$(scan "$WORK/$n" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    bad "$desc: exit $rc, expected 0"
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    return
  fi
  ok
}

# --- session-handoff-reference ----------------------------------------------------
S=SESSION
leak session-handoff-reference "handoff file named in prose" "$S-HANDOFF.md" "see $S-HANDOFF.md for the details"
leak session-handoff-reference "handoff file in a path" "$S-HANDOFF.md" "notes/$S-HANDOFF.md"
leak session-handoff-reference "handoff file alone on a line" "$S-HANDOFF.md" "$S-HANDOFF.md"
clean "handoff without the file extension" "the $S-HANDOFF step"

# --- us-ssn-formatted -------------------------------------------------------------
# An invented number in an issued range; never a real person's.
A=219 G=09 N=9999
V=$A-$G-$N
leak us-ssn-formatted "SSN in prose" "$V" "my number is $V, thanks"
leak us-ssn-formatted "SSN alone on a line" "$V" "$V"
leak us-ssn-formatted "SSN in a quoted string" "$V" "ssn = \"$V\""
# One planted value per area alternative and at each group and serial bound,
# so narrowing any part of the pattern fails a fixture.
for area in 001 009 010 099 100 599 700 899 600 659 665 667 699; do
  leak us-ssn-formatted "SSN area $area is issued" "$area-$G-$N" "ref $area-$G-$N"
done
leak us-ssn-formatted "SSN group 01" "$A-01-$N" "ref $A-01-$N"
leak us-ssn-formatted "SSN group 99" "$A-99-$N" "ref $A-99-$N"
# Split across lines: four four-digit groups on one line are a card shape.
for serial in 0001 0099 0100 \
  0999 1000; do
  leak us-ssn-formatted "SSN serial $serial" "$A-$G-$serial" "ref $A-$G-$serial"
done
leaks us-ssn-formatted "SSNs on consecutive lines are each reported" 2 "$V
$A-$G-1000"
clean "SSN area 000 is never issued" "000-$G-$N"
clean "SSN area 666 is never issued" "666-$G-$N"
clean "SSN area 9xx is never issued" "9${G}-$G-$N"
clean "SSN group 00 is never issued" "$A-00-$N"
clean "SSN serial 0000 is never issued" "$A-$G-0000"
clean "SSN shape inside a longer digit run" "1$A-$G-$N"
clean "SSN shape followed by more digits" "$A-$G-${N}1"
clean "SSN shape inside a hyphenated token" "id-$A-$G-$N"
clean "ISO date" "2026-10-08"
clean "unseparated nine digits" "$A$G$N"

# --- card-number-grouped ----------------------------------------------------------
# Invented digits in card-like groups; they pass no issuer's check digit.
C1=4000 C2=1234 C3=5678 C4=9010
leak card-number-grouped "card in hyphen groups" "$C1-$C2-$C3-$C4" "card $C1-$C2-$C3-$C4 on file"
leak card-number-grouped "card in space groups" "$C1 $C2 $C3 $C4" "card $C1 $C2 $C3 $C4 on file"
leak card-number-grouped "card alone on a line" "$C1-$C2-$C3-$C4" "$C1-$C2-$C3-$C4"
leak card-number-grouped "card in space groups alone on a line" "$C1 $C2 $C3 $C4" "$C1 $C2 $C3 $C4"
X1=3712 X2=345678 X3=90123
leak card-number-grouped "card in 4-6-5 grouping" "$X1 $X2 $X3" "amex $X1 $X2 $X3"
leak card-number-grouped "card in 4-6-5 with hyphens" "$X1-$X2-$X3" "amex $X1-$X2-$X3"
leak card-number-grouped "card in 4-6-5 alone on a line" "$X1 $X2 $X3" "$X1 $X2 $X3"
leak card-number-grouped "card in 4-6-5 with a 34 prefix" "3412 $X2 $X3" "amex 3412 $X2 $X3"
leaks card-number-grouped "cards on consecutive lines are each reported" 2 "$C1-$C2-$C3-$C4
$C4-$C3-$C2-$C1"
clean "card with mixed separators" "$C1-$C2 $C3-$C4"
clean "three groups of four" "$C1-$C2-$C3"
clean "card shape inside a longer digit run" "1$C1-$C2-$C3-$C4"
clean "card shape inside a hyphenated token" "$C1-$C2-$C3-$C4-$C1"
clean "unseparated sixteen digits" "$C1$C2$C3$C4"
clean "4-6-5 grouping without a 34 or 37 prefix" "4012 $X2 $X3"

echo "Shape rule fixtures: $passed passed, $failed failed."
[ "$failed" -eq 0 ]
