#!/usr/bin/env bash
set -eu

# Build and run cargo-mend under one placement in the same checkout.
mend_bin="${CARGO_TARGET_DIR:-$PWD/target}/debug/cargo-mend"
echo '+ cargo build -p cargo-mend --bin cargo-mend'
cargo build -p cargo-mend --bin cargo-mend
if [[ ! -x "$mend_bin" ]]; then
    printf 'lint: no cargo-mend binary at %s after the build\n' "$mend_bin" >&2
    exit 1
fi

# A compiler cache can hide the diagnostics cargo-mend reads.
printf '+ env RUSTC_WRAPPER= %s --workspace --all-targets %s\n' "$mend_bin" "$*"
exec env RUSTC_WRAPPER= "$mend_bin" --workspace --all-targets "$@"
