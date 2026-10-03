#!/bin/sh
# Fake tool: writes /work/out/out.txt and the lines output.
set -eu
echo ran > ran.marker
printf 'hello\n' > out/out.txt
printf '{"lines": 1}\n' > outputs.json
