#!/bin/sh
# fake.copy: copy the staged source to /work/out/ under its basename.
set -eu
name=$(ls /work/in/source)
cp "/work/in/source/$name" "/work/out/$name"
printf '{"result": "%s"}' "$name" > /work/outputs.json
