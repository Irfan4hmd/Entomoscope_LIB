#!/usr/bin/env bash
#
# Sync this working tree onto the installed ENIMAS (C:\Program Files\ENIMAS\src).
#
# Copies every git-tracked source file whose content differs, backing up
# whatever it replaces. It never deletes anything in the install, so the venv,
# the ONNX models, lenses.json and the Drive credentials all survive untouched.
#
# servo.ino is excluded on purpose: it is firmware. The code that runs lives in
# the Arduino's flash, so copying the .ino into src/ changes nothing. Flash it
# from the Arduino IDE instead.
#
# Usage:
#   ./Tools/sync_to_install.sh              # show the plan, then ask
#   ./Tools/sync_to_install.sh -n           # dry run, change nothing
#   ./Tools/sync_to_install.sh -y           # no prompt
#   ./Tools/sync_to_install.sh --install "/c/somewhere/else/src"
#
# Environment: ENIMAS_DEV and ENIMAS_INSTALL override the two paths.

set -uo pipefail

DEV_DIR="${ENIMAS_DEV:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTALL_DIR="${ENIMAS_INSTALL:-/c/Program Files/ENIMAS/src}"
DRY_RUN=0
ASSUME_YES=0
DO_BACKUP=1

# Tracked files that must never be pushed to the install. Glob patterns, matched
# against the repo-relative path. *.ino rather than a fixed path because the
# Arduino IDE relocates a sketch into a folder of its own name the first time it
# is opened, so servo.ino does not stay where the repo put it.
EXCLUDES=(
    "*.ino"                       # firmware - flash it; copying does nothing
    "config.txt"                  # the install's own live settings
    "lenses.json"                 # lens calibration, rewritten by autofocus
    ".gitignore"                  # repo metadata, meaningless in the install
    ".gitattributes"
    "Tools/sync_to_install.sh"    # this script
    "update_manifest.json"        # the manifest does not list itself, so a real
                                  # install has no copy in src/ - don't add one
)

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

usage() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run)  DRY_RUN=1 ;;
        -y|--yes)      ASSUME_YES=1 ;;
        --no-backup)   DO_BACKUP=0 ;;
        --dev)         DEV_DIR="${2:-}"; shift ;;
        --install)     INSTALL_DIR="${2:-}"; shift ;;
        -h|--help)     usage ;;
        *)             die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[ -d "$DEV_DIR" ]     || die "source folder not found: $DEV_DIR"
[ -d "$INSTALL_DIR" ] || die "install folder not found: $INSTALL_DIR"
[ -f "$DEV_DIR/constants.py" ] || die "$DEV_DIR does not look like an ENIMAS source tree"
[ -f "$INSTALL_DIR/constants.py" ] || die "$INSTALL_DIR does not look like an ENIMAS install"

INSTALL_ROOT="$(dirname "$INSTALL_DIR")"

# --- refuse to overwrite a running app -------------------------------------
# Ask the updater rather than guessing: it knows whether the lock owner is
# still alive and clears the lock itself when it is stale. Exit 11 means the
# application is up. Overwriting ui.py underneath a running process gives a
# half-old, half-new import the next time it touches a module.
check_app_running() {
    local py upd
    for py in "/c/Program Files/Python310/python.exe" "$(command -v python)"; do
        [ -n "$py" ] && [ -x "$py" ] && break
    done
    [ -n "${py:-}" ] && [ -x "$py" ] || return 0
    for upd in "$INSTALL_ROOT/updates/bin/enimas_updater.py" \
               "$INSTALL_ROOT/updates/bootstrap/enimas_updater.py"; do
        [ -f "$upd" ] || continue
        "$py" "$upd" --install-root "$INSTALL_ROOT" application-status >/dev/null 2>&1
        [ $? -eq 11 ] && return 1
        return 0
    done
    return 0
}

if ! check_app_running; then
    die "ENIMAS is running. Close it first, then re-run this script."
fi

# --- work out what actually changed ----------------------------------------
is_excluded() {
    local f="$1" e
    for e in "${EXCLUDES[@]}"; do
        # shellcheck disable=SC2254  # $e is deliberately a glob
        case "$f" in $e) return 0 ;; esac
    done
    return 1
}

# Compare text files with CRLF/LF normalised, so a line-ending difference alone
# is not mistaken for a real change. Binaries are compared byte for byte.
is_text() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        *.py|*.txt|*.md|*.json|*.bat|*.cfg|*.ino|*.sh|*.vbs|*.yml|*.yaml|*.lock) return 0 ;;
        *) return 1 ;;
    esac
}

files_differ() {
    local src="$1" dst="$2"
    [ -e "$dst" ] || return 0
    if is_text "$src"; then
        ! cmp -s <(tr -d '\r' < "$src") <(tr -d '\r' < "$dst")
    else
        ! cmp -s "$src" "$dst"
    fi
}

cd "$DEV_DIR" || die "cannot enter $DEV_DIR"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "$DEV_DIR is not inside a git repository (needed to list source files)"

CHANGED=(); NEW=(); EXCLUDED=()
while IFS= read -r f; do
    [ -f "$f" ] || continue                 # tracked but deleted locally
    if is_excluded "$f"; then EXCLUDED+=("$f"); continue; fi
    if [ ! -e "$INSTALL_DIR/$f" ]; then
        NEW+=("$f")
    elif files_differ "$f" "$INSTALL_DIR/$f"; then
        CHANGED+=("$f")
    fi
done < <(git ls-files)

TOTAL=$(( ${#CHANGED[@]} + ${#NEW[@]} ))

printf '\nsource : %s\ninstall: %s\n\n' "$DEV_DIR" "$INSTALL_DIR"
if [ "$TOTAL" -eq 0 ]; then
    printf 'Already up to date (%d excluded).\n' "${#EXCLUDED[@]}"
    printf 'Remember servo.ino is firmware - flash it separately if you changed it.\n'
    exit 0
fi

[ ${#CHANGED[@]} -gt 0 ] && { printf 'update (%d):\n' "${#CHANGED[@]}"; printf '  %s\n' "${CHANGED[@]}"; }
[ ${#NEW[@]} -gt 0 ]     && { printf 'add (%d):\n'    "${#NEW[@]}";     printf '  %s\n' "${NEW[@]}"; }
if [ ${#EXCLUDED[@]} -gt 0 ]; then
    printf 'excluded (%d):\n' "${#EXCLUDED[@]}"
    printf '  %s\n' "${EXCLUDED[@]}"
fi
printf '\n'

if [ "$DRY_RUN" -eq 1 ]; then
    printf 'Dry run - nothing was changed.\n'
    exit 0
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    printf 'Copy these %d file(s) into the install? [y/N] ' "$TOTAL"
    read -r reply </dev/tty || reply=""
    case "$reply" in [yY]*) ;; *) printf 'Aborted.\n'; exit 1 ;; esac
fi

# --- back up what is about to be replaced ----------------------------------
if [ "$DO_BACKUP" -eq 1 ] && [ ${#CHANGED[@]} -gt 0 ]; then
    BACKUP_DIR="$INSTALL_ROOT/updates/backups/sync-$(date +%Y%m%d-%H%M%S)"
    for f in "${CHANGED[@]}"; do
        mkdir -p "$BACKUP_DIR/$(dirname "$f")" || die "cannot create backup folder"
        cp -p "$INSTALL_DIR/$f" "$BACKUP_DIR/$f" || die "cannot back up $f"
    done
    printf 'backup: %s\n' "$BACKUP_DIR"
fi

# --- copy ------------------------------------------------------------------
FAILED=0
for f in "${CHANGED[@]}" "${NEW[@]}"; do
    mkdir -p "$INSTALL_DIR/$(dirname "$f")" 2>/dev/null
    if cp "$f" "$INSTALL_DIR/$f"; then
        printf '  ok   %s\n' "$f"
    else
        printf '  FAIL %s\n' "$f" >&2
        FAILED=$((FAILED + 1))
    fi
done

# Stale .pyc files are keyed on mtime, so a copied-in file normally invalidates
# them by itself. Clearing them anyway costs nothing and removes the doubt.
find "$INSTALL_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null

# --- verify ----------------------------------------------------------------
MISMATCH=0
for f in "${CHANGED[@]}" "${NEW[@]}"; do
    files_differ "$f" "$INSTALL_DIR/$f" && { printf '  MISMATCH %s\n' "$f" >&2; MISMATCH=$((MISMATCH + 1)); }
done

printf '\n%d copied, %d failed, %d verified mismatched.\n' "$TOTAL" "$FAILED" "$MISMATCH"
if [ "$FAILED" -gt 0 ] || [ "$MISMATCH" -gt 0 ]; then
    exit 1
fi

printf 'Install is in sync.\n'
if [ ${#EXCLUDED[@]} -gt 0 ]; then
    printf '\nNote: firmware (*.ino) was not copied - the code that runs lives in the\n'
    printf "Arduino's flash. Upload it from the Arduino IDE if you changed it.\n"
fi
exit 0
