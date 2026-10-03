#!/bin/sh
# Fake tool: copies the staged source input and reports the copy's path.
set -eu
echo ran > ran.marker
source_path=$(sed -n 's/^ *"source": "\(.*\)",\{0,1\}$/\1/p' inputs.json)
cp "$source_path" out/copy.txt
printf '{"copy": "copy.txt"}\n' > outputs.json
