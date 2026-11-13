#!/bin/bash
# test fixture for RPA-Python run() regression suite.
# deliberately installed under a directory containing spaces, and invoked
# via an argv list (no shell quoting) to prove space-bearing paths work.
#
# usage:
#   convert_helper.sh <input-file> <output-file> [fail]
#
# - copies input-file to output-file, prefixing each line with "converted: "
# - echoes a marker to stdout and an audit line to stderr (separate streams)
# - with the third arg "fail", exits with code 7 to exercise non-zero paths
set -u

if [ "$#" -lt 2 ]; then
    echo "usage: $0 <input-file> <output-file> [fail]" >&2
    exit 2
fi

input_file="$1"
output_file="$2"

echo "helper starting: $input_file -> $output_file"
echo "audit: converting $(wc -l < "$input_file") line(s)" >&2

while IFS= read -r line || [ -n "$line" ]; do
    echo "converted: $line" >> "$output_file"
done < "$input_file"

if [ "${3:-}" = "fail" ]; then
    echo "helper failing on demand" >&2
    exit 7
fi

echo "helper done"
exit 0
