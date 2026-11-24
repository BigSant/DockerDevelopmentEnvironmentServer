#!/usr/bin/env bash
# Screenshot the web UI preview (tests/preview_ui.py on 127.0.0.1:9178) into .design-review/<round>/ (git-ignored):
# desktop pages at 1600 px and a few at phone width (390 px). See DESIGN.md.
# Usage: tests/shoot_ui.sh round-1      (the preview must be running; needs firefox)
set -euo pipefail
round=${1:?round name}
out="$(cd "$(dirname "$0")/.." && pwd)/.design-review/$round"
mkdir -p "$out"
base=http://127.0.0.1:9178
desktop=(/ /projects /projects/new /runners /runners/new /runners/github/bigsant /access /access/tomas /access/new /files /files/asana_tracker/prod
         /update /actions /instructions /instructions/example /logs /worker-metrics /users /users/new /groups/operators
         /account /project/asana_tracker /login)
phone=(/ /projects /runners /actions /users/new /access)
shot() {  # width height path file
    local profile; profile=$(mktemp -d)
    # Reduced motion, and a dark system theme (the UI is dark only; this keeps native controls dark too).
    { echo 'user_pref("ui.prefersReducedMotion", 1);'
      echo 'user_pref("layout.css.prefers-color-scheme.content-override", 0);'
      echo 'user_pref("ui.systemUsesDarkTheme", 1);'; } > "$profile/user.js"
    timeout 90 firefox --headless --no-remote --profile "$profile" --window-size="$1,$2" --screenshot "$4" "$base$3" >/dev/null 2>&1 || true
    rm -rf "$profile"
}
export -f shot; export base
{
    for p in "${desktop[@]}"; do n=$(echo "${p#/}" | tr / _); echo "1600 1800 $p $out/desktop_${n:-overview}.png"; done
    for p in "${phone[@]}"; do n=$(echo "${p#/}" | tr / _); echo "390 1400 $p $out/phone_${n:-overview}.png"; done
} | xargs -P 6 -L 1 bash -c 'shot "$0" "$1" "$2" "$3"'
ls "$out" | wc -l
