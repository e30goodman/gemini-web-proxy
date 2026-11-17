#!/bin/sh
# Sanity check: every shipped .py must parse/compile clean.
for f in src/*.py; do
  python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" || { echo "FAIL $f"; exit 1; }
done
echo "all src/*.py compile clean"