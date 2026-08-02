#!/bin/sh

set -eu

SEED_SRC="${SEED_SRC:-/app/seed_storage}"
STORAGE_DST="${STORAGE_DST:-/app/environment/frontend_server/storage}"
MARKER=".seed-complete"

if [ ! -d "$SEED_SRC" ]; then
    echo "[seed] No seed data found at $SEED_SRC; nothing to do."
    exit 0
fi

mkdir -p "$STORAGE_DST" 2>/dev/null || {
    echo "[seed] ERROR: cannot create $STORAGE_DST" >&2
    exit 1
}
if [ ! -w "$STORAGE_DST" ]; then
    echo "[seed] ERROR: $STORAGE_DST is not writable by uid $(id -u)." >&2
    echo "[seed] The storage volume must be writable by the container user." >&2
    exit 1
fi

validate_seed() {
    seed_dir="$1"
    [ -f "$seed_dir/reverie/meta.json" ] || return 1
    [ -d "$seed_dir/personas" ] || return 1
    [ -f "$seed_dir/environment/0.json" ] || return 1
    if ! python3 - "$seed_dir/reverie/meta.json" "$seed_dir/environment/0.json" <<'PY'
import json, sys
for path in sys.argv[1:]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        sys.exit(1)
PY
    then
        return 1
    fi
    for persona_dir in "$seed_dir/personas"/*/; do
        if [ -d "$persona_dir" ]; then
            return 0
        fi
    done
    return 1
}

seeded=0
for sim_dir in "$SEED_SRC"/*/; do
    [ -d "$sim_dir" ] || continue
    sim_name=$(basename "$sim_dir")
    target="$STORAGE_DST/$sim_name"
    staging="$STORAGE_DST/.staging-$sim_name"

    if [ -e "$target" ]; then
        if [ ! -d "$target" ]; then
            echo "[seed] ERROR: $target exists but is not a directory." >&2
            exit 1
        fi
        if [ -f "$target/$MARKER" ]; then
            echo "[seed] $sim_name already seeded and complete; skipping (no overwrite)."
            continue
        fi
        if validate_seed "$target"; then
            : > "$target/$MARKER"
            echo "[seed] $sim_name present without marker; validated and marked complete."
            continue
        fi
        echo "[seed] ERROR: $sim_name exists in storage volume but is incomplete or invalid." >&2
        echo "[seed] Refusing to skip or overwrite. Remove or repair it manually." >&2
        exit 1
    fi

    if [ -e "$staging" ]; then
        echo "[seed] Cleaning stale staging '$staging' from an interrupted run."
        rm -rf "$staging"
    fi

    if ! validate_seed "$sim_dir"; then
        echo "[seed] ERROR: seed source $sim_dir is invalid (missing or corrupt meta/personas/environment snapshot)." >&2
        exit 1
    fi

    echo "[seed] Copying $sim_name into storage volume staging..."
    if ! cp -r "$sim_dir" "$staging"; then
        echo "[seed] ERROR: failed to copy $sim_name into staging." >&2
        rm -rf "$staging"
        exit 1
    fi
    if ! validate_seed "$staging"; then
        echo "[seed] ERROR: staged copy of $sim_name failed validation; aborting." >&2
        rm -rf "$staging"
        exit 1
    fi
    : > "$staging/$MARKER"
    if ! mv "$staging" "$target"; then
        echo "[seed] ERROR: failed to atomically rename staging to $target." >&2
        rm -rf "$staging"
        exit 1
    fi
    seeded=$((seeded + 1))
    echo "[seed] $sim_name seeded and marked complete."
done

echo "[seed] Storage volume ready ($seeded simulation(s) seeded)."
