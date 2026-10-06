#!/bin/sh
# fake.concat: join the `parts` files, in the order given, into joined.txt.
# Array items are not staged, so each file is read from its URI: the local
# runner mounts the run directory at the same path. Each "uri" line of
# glow-exec's indented inputs.json is one part.
set -eu
count=0
: > /work/out/joined.txt
for uri in $(sed -n 's/^ *"uri": "\(.*\)",\{0,1\}$/\1/p' /work/inputs.json); do
    cat "${uri#file://}" >> /work/out/joined.txt
    count=$((count + 1))
done
printf '{"count": %d}' "$count" > /work/outputs.json
