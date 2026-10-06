#!/bin/sh
# fake.write: write `text` to /work/out/<name>.txt. busybox has no JSON
# parser, so values are read from glow-exec's indented inputs.json with sed
# and must not contain quotes or backslashes.
set -eu
value() {
    sed -n "s/^  \"$1\": \"\(.*\)\",\{0,1\}\$/\1/p" /work/inputs.json
}
name=$(value name)
printf '%s\n' "$(value text)" > "/work/out/$name.txt"
printf '{"result": "%s.txt"}' "$name" > /work/outputs.json
