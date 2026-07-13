#!/usr/bin/env bash
# EchoDesk 公共后端：不可变 release、隔离 canary、原子切换与快照回滚。

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEPLOY_ROOT="${ECHODESK_DEPLOY_ROOT:-$HOME/echodesk-public}"
SERVICE_NAME="${ECHODESK_PUBLIC_SERVICE:-echodesk-demo-backend.service}"
SECRET_ENV_FILE=""
PROD_DB=""
PROD_PORT=8769
CANARY_PORT=8870
HEALTH_TIMEOUT_S=90
SOURCE_ROOT="$REPO_ROOT"
PYTHON_BIN="${ECHODESK_DEPLOY_PYTHON:-python3}"
SYSTEMCTL_BIN="${ECHODESK_SYSTEMCTL_BIN:-systemctl}"
SS_BIN="${ECHODESK_SS_BIN:-ss}"
RELEASE_ID=""
DEPLOYMENT_ID=""
LEGACY_ENV_FILE=""
LEGACY_DB=""
LEGACY_DATA_ROOT=""
INGRESS_GATE=""
PUBLIC_BASE_URL=""
COMMAND=""
DRY_RUN=0
ALLOW_DIRTY=0
PREPARE_TMP=""
SELF_TEST_TMP=""
LOCK_FD=9
PROMOTION_RECOVERY_ARMED=0
PROMOTION_RECOVERY_RUNNING=0
PROMOTION_SWITCHED=0
PROMOTION_BACKUP_DIR=""
PROMOTION_PREVIOUS_RELEASE=""
ROLLBACK_RECOVERY_ARMED=0
ROLLBACK_RECOVERY_RUNNING=0
ROLLBACK_MUTATED=0
ROLLBACK_SAFETY_DIR=""
ROLLBACK_CURRENT_RELEASE=""
BOOTSTRAP_RECOVERY_ARMED=0
BOOTSTRAP_RECOVERY_RUNNING=0
BOOTSTRAP_BACKUP_DIR=""
BOOTSTRAP_RELEASE=""
BOOTSTRAP_PHASE="not_started"
BOOTSTRAP_PHASE_FILE=""

usage() {
    cat <<'EOF'
Usage:
  scripts/public-backend-deploy.sh [global options] <command> [command options]

Commands:
  prepare       Create a new immutable versioned release directory.
  install-unit  Install the stable systemd-user unit; never starts/restarts it.
  canary        Clone the production DB, start an isolated canary, run health + isolation smoke.
  bootstrap     First safe cutover from a non-isolated legacy service; fail closed on error.
  bootstrap-resume
                Resume a failed bootstrap from its stopped-service SQLite snapshot.
  bootstrap-abort
                Abort a failed bootstrap, preserving evidence with service masked and ingress closed.
  promote       Require a passing canary, snapshot prod, atomically switch, validate; auto-rollback on failure.
  rollback      Restore the pre-deploy DB/config/release snapshot for one successful deployment.
  status        Show non-secret release/service/canary state.
  self-test     Exercise path guards, atomic symlink switch, SQLite backup/restore, and smoke self-test locally.

Global options:
  --root PATH          Deployment root (default: ~/echodesk-public)
  --service NAME       systemd-user service name (default: echodesk-demo-backend.service)
  --env-file PATH      Secret EnvironmentFile; content is never sourced or printed
  --db PATH            Production SQLite DB (default: ROOT/shared/data/echodesk.db)
  --prod-port PORT     Production loopback port (default: 8769)
  --canary-port PORT   Canary loopback port (default: 8870; must differ from prod)
  --health-timeout SEC Health/readiness deadline (default: 90)
  --dry-run            Run read-only path/release/systemd/health/gate preflight, then print plan
  -h, --help           Show this help

Command options:
  --release ID         Required by prepare/canary/bootstrap/resume/abort/promote; optional for install-unit
  --source PATH        Clean source checkout for prepare (default: repository containing this script)
  --python PATH        Python 3.11+ used for release/deployment operations (default: python3)
  --allow-dirty        Allow prepare from a dirty Git checkout (recorded in RELEASE.json)
  --deployment ID      Optional bootstrap id, or rollback id; rollback defaults to latest.json
  --legacy-env PATH    Legacy secret env file, required only by bootstrap
  --legacy-db PATH     Legacy SQLite database, required only by bootstrap
  --legacy-data-root PATH
                       Legacy user-data root containing storage/ and rag_index/, bootstrap only
  --ingress-gate PATH Dedicated executable implementing: status|close|open SERVICE PORT.
                       Defaults to the immutable helper inside --release; no valid gate means no cutover.
  --public-url URL     Public HTTPS origin used for no-header post-open isolation smoke.

Safe first-time migration from a legacy service that cannot pass isolation:
  1. prepare the first safe release with Python 3.11+;
  2. run canary against an online SQLite backup of --legacy-db;
  3. run bootstrap with a distinct new --db under ROOT/shared/data;
  4. bootstrap pre-syncs and final-syncs legacy storage/RAG/user data, then validates the
     new release. Any failure after stopping production leaves it stopped and never starts legacy.

The script never deletes or overwrites a release/canary/backup directory. Rollback restores the
pre-deploy database snapshot, intentionally discarding writes made after promotion to avoid running
an older schema against newer tenant data.
EOF
}

log() {
    printf '[echodesk-deploy] %s\n' "$*"
}

warn() {
    printf '[echodesk-deploy] WARNING: %s\n' "$*" >&2
}

die() {
    printf '[echodesk-deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local status="$1"
    if ((status != 0 && PROMOTION_RECOVERY_ARMED && !PROMOTION_RECOVERY_RUNNING)); then
        PROMOTION_RECOVERY_RUNNING=1
        PROMOTION_RECOVERY_ARMED=0
        set +e
        emergency_restore_promotion
        set -e
    fi
    if ((status != 0 && BOOTSTRAP_RECOVERY_ARMED && !BOOTSTRAP_RECOVERY_RUNNING)); then
        BOOTSTRAP_RECOVERY_RUNNING=1
        BOOTSTRAP_RECOVERY_ARMED=0
        set +e
        emergency_fail_closed_bootstrap
        set -e
    fi
    if ((status != 0 && ROLLBACK_RECOVERY_ARMED && !ROLLBACK_RECOVERY_RUNNING)); then
        ROLLBACK_RECOVERY_RUNNING=1
        ROLLBACK_RECOVERY_ARMED=0
        set +e
        emergency_restore_rollback
        set -e
    fi
    if [[ -n "$PREPARE_TMP" && -d "$PREPARE_TMP" ]]; then
        chmod -R u+w "$PREPARE_TMP" >/dev/null 2>&1 || true
        rm -rf -- "$PREPARE_TMP"
    fi
    if [[ -n "$SELF_TEST_TMP" && -d "$SELF_TEST_TMP" ]]; then
        chmod -R u+w "$SELF_TEST_TMP" >/dev/null 2>&1 || true
        rm -rf -- "$SELF_TEST_TMP"
    fi
}

trap 'cleanup $?' EXIT

shell_join() {
    local arg
    printf '  '
    for arg in "$@"; do
        printf '%q ' "$arg"
    done
    printf '\n'
}

plan() {
    printf '[dry-run] %s\n' "$1"
    shift
    if (($#)); then
        shell_join "$@"
    fi
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

systemctl() {
    command "$SYSTEMCTL_BIN" "$@"
}

require_python_311() {
    "$PYTHON_BIN" - <<'PY' \
        || die "Python 3.11+ is required; pass --python /absolute/path/to/python3.11"
import sys

raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

validate_release_id() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] \
        || die "invalid release id (allowed: 1-96 alnum/dot/underscore/hyphen): $1"
}

validate_service_name() {
    [[ "$1" =~ ^[A-Za-z0-9_.@-]{1,120}\.service$ ]] \
        || die "invalid systemd service name: $1"
}

validate_port() {
    [[ "$2" =~ ^[0-9]+$ ]] || die "$1 must be an integer"
    ((10#$2 >= 1 && 10#$2 <= 65535)) || die "$1 must be between 1 and 65535"
}

validate_timeout() {
    [[ "$1" =~ ^[0-9]+$ ]] || die "health timeout must be an integer"
    ((10#$1 >= 1 && 10#$1 <= 900)) || die "health timeout must be between 1 and 900"
}

safe_absolute_path() {
    local label="$1"
    local value="$2"
    [[ "$value" == /* ]] || die "$label must be absolute: $value"
    case "$value" in
        *$'\n'*|*$'\r'*|*$'\t'*|*' '*|*'%'*)
            die "$label cannot contain whitespace, control characters, or %: $value"
            ;;
    esac
}

init_paths() {
    DEPLOY_ROOT="${DEPLOY_ROOT%/}"
    [[ -n "$DEPLOY_ROOT" ]] || die "deployment root cannot be empty"
    if [[ "$DEPLOY_ROOT" != /* ]]; then
        DEPLOY_ROOT="$(cd "$(dirname "$DEPLOY_ROOT")" 2>/dev/null && pwd)/$(basename "$DEPLOY_ROOT")"
    fi
    RELEASES_DIR="$DEPLOY_ROOT/releases"
    CANARIES_DIR="$DEPLOY_ROOT/canaries"
    BACKUPS_DIR="$DEPLOY_ROOT/backups"
    DEPLOYMENTS_DIR="$DEPLOY_ROOT/deployments"
    SHARED_DIR="$DEPLOY_ROOT/shared"
    DATA_ROOT="$SHARED_DIR/data"
    CURRENT_LINK="$DEPLOY_ROOT/current"
    LOCK_FILE="$DEPLOY_ROOT/deploy.lock"
    PROD_RUNTIME_ENV="$SHARED_DIR/production.env"
    DEPLOYMENT_GATE_FILE="$SHARED_DIR/deployment-gate.token"
    SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    PROD_UNIT_PATH="$SYSTEMD_USER_DIR/$SERVICE_NAME"
    SECRET_ENV_FILE="${SECRET_ENV_FILE:-$SHARED_DIR/runtime.env}"
    PROD_DB="${PROD_DB:-$DATA_ROOT/echodesk.db}"

    safe_absolute_path "deployment root" "$DEPLOY_ROOT"
    safe_absolute_path "secret env file" "$SECRET_ENV_FILE"
    safe_absolute_path "production database" "$PROD_DB"
    safe_absolute_path "deployment gate file" "$DEPLOYMENT_GATE_FILE"
    safe_absolute_path "systemd user directory" "$SYSTEMD_USER_DIR"
}

ensure_layout() {
    install -d -m 0700 \
        "$DEPLOY_ROOT" "$RELEASES_DIR" "$CANARIES_DIR" "$BACKUPS_DIR" \
        "$DEPLOYMENTS_DIR" "$SHARED_DIR" "$DATA_ROOT" "$SYSTEMD_USER_DIR"
}

assert_secret_file_safe() {
    local path="$1"
    local label="$2"
    [[ -f "$path" && ! -L "$path" ]] \
        || die "$label must be a regular non-symlink file: $path"
    "$PYTHON_BIN" - "$path" "$label" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
label = sys.argv[2]
mode = stat.S_IMODE(os.stat(path).st_mode)
if mode & 0o077:
    raise SystemExit(f"{label} permissions must be 0600 or stricter, got {mode:04o}")
PY
}

assert_secret_env_safe() {
    assert_secret_file_safe "$SECRET_ENV_FILE" "secret env file"
}

release_dir() {
    printf '%s/%s' "$RELEASES_DIR" "$1"
}

canary_dir() {
    printf '%s/%s' "$CANARIES_DIR" "$1"
}

release_python() {
    printf '%s/backend/.venv/bin/python' "$(release_dir "$1")"
}

release_manifest_digest() {
    "$PYTHON_BIN" - "$(release_dir "$1")/RELEASE.json" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
}

release_code_digest() {
    "$PYTHON_BIN" - "$(release_dir "$1")" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
digest = hashlib.sha256()
for path in sorted(root.rglob("*")):
    if (
        not path.is_file()
        or path.name == "RELEASE.json"
        or ".venv" in path.parts
        or "__pycache__" in path.parts
    ):
        continue
    relative = path.relative_to(root).as_posix()
    digest.update(relative.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
}

release_environment_evidence() {
    local python="$1"
    PYTHONDONTWRITEBYTECODE=1 "$python" - <<'PY'
import base64
import csv
import hashlib
import importlib.metadata
import pathlib
import re
import sys

prefix = pathlib.Path(sys.prefix).resolve(strict=True)
aggregate = hashlib.sha256()
distributions = []
for distribution in importlib.metadata.distributions():
    name = re.sub(r"[-_.]+", "-", distribution.metadata.get("Name", "")).lower()
    version = distribution.version
    records = [
        item
        for item in (distribution.files or ())
        if item.name == "RECORD" and any(part.endswith(".dist-info") for part in item.parts)
    ]
    record_entries = []
    for record in sorted(records, key=str):
        record_path = pathlib.Path(distribution.locate_file(record)).resolve(strict=True)
        if not record_path.is_relative_to(prefix):
            raise SystemExit(f"distribution RECORD escapes venv: {name}")
        raw = record_path.read_bytes()
        with record_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        record_root = record_path.parent.parent
        actual_files = hashlib.sha256()
        for row in rows:
            if not row:
                continue
            relative = pathlib.PurePosixPath(row[0])
            if (
                any(
                    part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
                    for part in relative.parts
                )
                or relative.suffix in {".pyc", ".pyo"}
            ):
                continue
            candidate = record_root / relative
            installed = candidate.resolve(strict=False)
            if not installed.is_relative_to(prefix):
                raise SystemExit(f"distribution file escapes venv: {name}:{row[0]}")
            if not candidate.is_file():
                if len(record.parts) <= 2:
                    raise SystemExit(f"distribution file missing from venv: {name}:{row[0]}")
                # Vendored RECORDs can retain wheel entry points deliberately omitted by
                # the parent distribution. Preserve that absence in the aggregate while
                # the complete venv tree digest binds every entry actually installed.
                actual_files.update(row[0].encode())
                actual_files.update(b"\0MISSING\0")
                continue
            installed = candidate.resolve(strict=True)
            if not installed.is_file():
                raise SystemExit(f"distribution file missing or escapes venv: {name}:{row[0]}")
            content = installed.read_bytes()
            actual = hashlib.sha256(content).digest()
            if len(row) > 1 and row[1]:
                algorithm, encoded = row[1].split("=", 1)
                if algorithm == "sha256":
                    expected = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                    if actual != expected:
                        raise SystemExit(f"distribution RECORD mismatch: {name}:{row[0]}")
            actual_files.update(row[0].encode())
            actual_files.update(b"\0")
            actual_files.update(actual)
            actual_files.update(b"\0")
        record_entries.append(
            f"{record}:{hashlib.sha256(raw).hexdigest()}:{actual_files.hexdigest()}"
        )
    if not record_entries:
        record_entries.append("NO_RECORD")
    distributions.append((name, version, tuple(record_entries)))

for name, version, records in sorted(distributions):
    aggregate.update(name.encode())
    aggregate.update(b"\0")
    aggregate.update(version.encode())
    aggregate.update(b"\0")
    for record in records:
        aggregate.update(record.encode())
        aggregate.update(b"\0")
print(f"{len(distributions)}\t{aggregate.hexdigest()}")
PY
}

clean_venv_runtime_cache() {
    "$PYTHON_BIN" - "$1" <<'PY'
import pathlib
import shutil
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
cache_directories = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
    if path.is_symlink():
        if path.name in cache_directories or path.suffix in {".pyc", ".pyo"}:
            path.unlink()
        continue
    if path.is_dir() and path.name in cache_directories:
        shutil.rmtree(path)
    elif path.is_file() and path.suffix in {".pyc", ".pyo"}:
        path.unlink()
PY
}

assert_venv_runtime_cache_absent() {
    "$PYTHON_BIN" - "$1" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
cache_directories = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
for path in root.rglob("*"):
    relative = path.relative_to(root)
    if any(part in cache_directories for part in relative.parts) or path.suffix in {".pyc", ".pyo"}:
        raise SystemExit(f"runtime cache entered immutable venv: {relative.as_posix()}")
PY
}

venv_tree_evidence() {
    "$PYTHON_BIN" - "$1" <<'PY'
import hashlib
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
cache_directories = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def excluded(relative: pathlib.Path) -> bool:
    return (
        any(part in cache_directories for part in relative.parts)
        or relative.suffix in {".pyc", ".pyo"}
    )


def feed(value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


entries = [root, *(path for path in root.rglob("*") if not excluded(path.relative_to(root)))]
entries.sort(key=lambda path: os.fsencode("." if path == root else path.relative_to(root).as_posix()))
digest = hashlib.sha256()
count = 0
for path in entries:
    relative = "." if path == root else path.relative_to(root).as_posix()
    info = path.lstat()
    stable_mode = stat.S_IMODE(info.st_mode) & ~0o222
    if stat.S_ISREG(info.st_mode):
        kind = b"file"
    elif stat.S_ISDIR(info.st_mode):
        kind = b"directory"
    elif stat.S_ISLNK(info.st_mode):
        kind = b"symlink"
    else:
        raise SystemExit(f"unsupported entry in immutable venv: {relative}")
    feed(kind)
    feed(os.fsencode(relative))
    feed(stable_mode.to_bytes(4, "big"))
    if kind == b"file":
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise SystemExit(f"venv file changed type while hashing: {relative}")
            feed(opened.st_size.to_bytes(8, "big"))
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise SystemExit(f"venv file truncated while hashing: {relative}")
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise SystemExit(f"venv file grew while hashing: {relative}")
        finally:
            os.close(descriptor)
    elif kind == b"symlink":
        feed(os.fsencode(os.readlink(path)))
    else:
        feed(b"")
    count += 1
print(f"{count}\t{digest.hexdigest()}")
PY
}

seal_release_tree() {
    "$PYTHON_BIN" - "$1" <<'PY'
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
for path in sorted(root.rglob("*"), reverse=True):
    if path.is_symlink():
        continue
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode & ~0o222)
root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)
PY
}

verify_release_readonly() {
    "$PYTHON_BIN" - "$1" <<'PY'
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve(strict=True)
for path in (root, *root.rglob("*")):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        target = path.resolve(strict=True)
        if not target.is_relative_to(root):
            raise SystemExit(f"release symlink escapes immutable root: {path}")
        continue
    if stat.S_IMODE(info.st_mode) & 0o222:
        raise SystemExit(f"release entry remains writable: {path}")
PY
}

json_get() {
    "$PYTHON_BIN" - "$1" "$2" <<'PY'
import hashlib
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
if not isinstance(value, (str, int, float, bool)):
    raise SystemExit("requested JSON value is not scalar")
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

json_get_optional() {
    "$PYTHON_BIN" - "$1" "$2" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        print("")
        raise SystemExit(0)
    value = value[part]
if value is None:
    print("")
elif isinstance(value, bool):
    print(str(value).lower())
elif isinstance(value, (str, int, float)):
    print(value)
else:
    raise SystemExit("requested JSON value is not scalar")
PY
}

verify_release_integrity() {
    local release="$1"
    local expected
    local actual
    verify_release_readonly "$(release_dir "$release")"
    [[ "$(json_get "$(release_dir "$release")/RELEASE.json" schema)" == "2" ]] \
        || die "unsupported release manifest schema: $release"
    [[ "$(json_get "$(release_dir "$release")/RELEASE.json" venv_tree_digest_schema)" == "1" ]] \
        || die "unsupported release venv tree digest schema: $release"
    expected="$(json_get "$(release_dir "$release")/RELEASE.json" code_sha256)"
    actual="$(release_code_digest "$release")"
    [[ "$actual" == "$expected" ]] || die "release content changed after prepare: $release"
    local venv_tree_count
    local venv_tree_digest
    assert_venv_runtime_cache_absent "$(release_dir "$release")/backend/.venv"
    IFS=$'\t' read -r venv_tree_count venv_tree_digest \
        < <(venv_tree_evidence "$(release_dir "$release")/backend/.venv")
    [[ "$venv_tree_count" == \
        "$(json_get "$(release_dir "$release")/RELEASE.json" venv_tree_entry_count)" ]] \
        || die "release venv tree entry count changed after prepare: $release"
    [[ "$venv_tree_digest" == \
        "$(json_get "$(release_dir "$release")/RELEASE.json" venv_tree_sha256)" ]] \
        || die "release venv tree changed after prepare: $release"
    local environment_count
    local environment_digest
    IFS=$'\t' read -r environment_count environment_digest \
        < <(release_environment_evidence "$(release_python "$release")")
    [[ "$environment_count" == \
        "$(json_get "$(release_dir "$release")/RELEASE.json" venv_distribution_count)" ]] \
        || die "release installed distribution count changed after prepare: $release"
    [[ "$environment_digest" == \
        "$(json_get "$(release_dir "$release")/RELEASE.json" venv_record_aggregate_sha256)" ]] \
        || die "release venv/RECORD aggregate changed after prepare: $release"
}

current_release() {
    "$PYTHON_BIN" - "$CURRENT_LINK" "$RELEASES_DIR" <<'PY'
import pathlib
import sys

link = pathlib.Path(sys.argv[1])
releases = pathlib.Path(sys.argv[2]).resolve()
if not link.is_symlink():
    raise SystemExit(1)
target = link.resolve(strict=True)
if target.parent != releases:
    raise SystemExit("current symlink escapes releases directory")
print(target.name)
PY
}

atomic_switch() {
    local release="$1"
    local target
    target="$(release_dir "$release")"
    [[ -d "$target" ]] || die "release does not exist: $target"
    if [[ -e "$CURRENT_LINK" && ! -L "$CURRENT_LINK" ]]; then
        die "current exists but is not a symlink: $CURRENT_LINK"
    fi
    "$PYTHON_BIN" - "$CURRENT_LINK" "$target" <<'PY'
import os
import pathlib
import sys

link = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2]).resolve(strict=True)
tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
try:
    tmp.symlink_to(target)
    os.replace(tmp, link)
finally:
    tmp.unlink(missing_ok=True)
PY
}

remove_current_release() {
    local release="$1"
    "$PYTHON_BIN" - "$CURRENT_LINK" "$(release_dir "$release")" <<'PY'
import pathlib
import sys

link = pathlib.Path(sys.argv[1])
expected = pathlib.Path(sys.argv[2]).resolve(strict=True)
if not link.is_symlink() or link.resolve(strict=True) != expected:
    raise SystemExit("current symlink changed before fail-closed rollback")
link.unlink()
PY
}

write_runtime_env() {
    local destination="$1"
    local data_dir="$2"
    local database="$3"
    local port="$4"
    local include_loopback_urls="$5"
    local base_url="http://127.0.0.1:$port"
    local tmp="$destination.tmp.$$"

    install -d -m 0700 "$(dirname "$destination")" "$data_dir"
    umask 077
    {
        printf 'ECHO_USER_DIR=%s\n' "$data_dir/user"
        printf 'DB_PATH=%s\n' "$database"
        printf 'STORAGE_DIR=%s\n' "$data_dir/storage"
        printf 'RAG_INDEX_DIR=%s\n' "$data_dir/rag_index"
        printf 'SKILL_EXECUTOR_BUILD_DIR=%s\n' "$data_dir/skill_build"
        printf 'WORKSPACE_STATE_FILE=%s\n' "$data_dir/workspace_state.json"
        printf 'HF_HOME=%s\n' "$data_dir/huggingface"
        printf 'TORCH_HOME=%s\n' "$data_dir/torch"
        printf 'PUBLIC_DEMO_MODE=true\n'
        printf 'WORKSPACE_SCAN_ON_STARTUP=false\n'
        printf 'ECHO_LAN_FULL_API_ENABLED=false\n'
        printf 'DEPLOYMENT_GATE_FILE=%s\n' "$DEPLOYMENT_GATE_FILE"
        printf 'PORT=%s\n' "$port"
        if [[ "$include_loopback_urls" == "1" ]]; then
            printf 'PUBLIC_HTTP_URL=%s\n' "$base_url"
            printf 'PUBLIC_WS_URL=ws://127.0.0.1:%s/ws/echo\n' "$port"
        fi
    } >"$tmp"
    chmod 0600 "$tmp"
    mv -f -- "$tmp" "$destination"
}

render_unit() {
    local destination="$1"
    local description="$2"
    local working_dir="$3"
    local runtime_env="$4"
    local data_dir="$5"
    local port="$6"
    local restart_policy="$7"
    local tmp="$destination.tmp.$$"

    safe_absolute_path "unit working directory" "$working_dir"
    safe_absolute_path "unit runtime env" "$runtime_env"
    safe_absolute_path "unit data directory" "$data_dir"
    safe_absolute_path "unit secret env" "$SECRET_ENV_FILE"

    cat >"$tmp" <<EOF
[Unit]
Description=$description
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$working_dir
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=$SECRET_ENV_FILE
EnvironmentFile=$runtime_env
ExecStart=$working_dir/backend/.venv/bin/python -m uvicorn app.main:app --app-dir $working_dir/backend --host 127.0.0.1 --port $port --ws-max-size 4096 --proxy-headers --forwarded-allow-ips=127.0.0.1,::1
Restart=$restart_policy
RestartSec=3
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadOnlyPaths=$RELEASES_DIR
ReadWritePaths=$data_dir

[Install]
WantedBy=default.target
EOF
    chmod 0600 "$tmp"
    mv -f -- "$tmp" "$destination"
}

sqlite_backup() {
    local source="$1"
    local destination="$2"
    [[ -f "$source" ]] || die "production database missing: $source"
    [[ ! -e "$destination" ]] || die "refusing to overwrite database backup: $destination"
    install -d -m 0700 "$(dirname "$destination")"
    "$PYTHON_BIN" - "$source" "$destination" <<'PY'
import pathlib
import sqlite3
import sys

source_path = pathlib.Path(sys.argv[1]).resolve(strict=True)
destination = pathlib.Path(sys.argv[2])
source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
target = sqlite3.connect(destination)
try:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise RuntimeError("SQLite integrity_check failed")
finally:
    target.close()
    source.close()
destination.chmod(0o600)
PY
}

sqlite_restore() {
    local backup="$1"
    local destination="$2"
    [[ -f "$backup" ]] || die "database backup missing: $backup"
    "$PYTHON_BIN" - "$backup" "$destination" <<'PY'
import os
import pathlib
import shutil
import sqlite3
import sys

backup = pathlib.Path(sys.argv[1]).resolve(strict=True)
destination = pathlib.Path(sys.argv[2])
check = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
try:
    if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise RuntimeError("backup integrity_check failed")
finally:
    check.close()
destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
tmp = destination.with_name(f".{destination.name}.restore-{os.getpid()}")
with backup.open("rb") as source, tmp.open("xb") as target:
    shutil.copyfileobj(source, target)
    target.flush()
    os.fsync(target.fileno())
tmp.chmod(0o600)
os.replace(tmp, destination)
for suffix in ("-wal", "-shm"):
    destination.with_name(destination.name + suffix).unlink(missing_ok=True)
PY
}

backup_configs() {
    local backup_dir="$1"
    install -d -m 0700 "$backup_dir/config"
    install -m 0600 "$SECRET_ENV_FILE" "$backup_dir/config/runtime.env"
    if [[ -f "$PROD_RUNTIME_ENV" ]]; then
        install -m 0600 "$PROD_RUNTIME_ENV" "$backup_dir/config/production.env"
    fi
    if [[ -e "$PROD_UNIT_PATH" || -L "$PROD_UNIT_PATH" ]]; then
        cp -a -- "$PROD_UNIT_PATH" "$backup_dir/config/$SERVICE_NAME"
    fi
}

restore_configs() {
    local backup_dir="$1"
    local env_backup="$backup_dir/config/runtime.env"
    local prod_env_backup="$backup_dir/config/production.env"
    [[ -f "$env_backup" ]] || die "secret env backup missing: $env_backup"
    install -m 0600 "$env_backup" "$SECRET_ENV_FILE.tmp.$$"
    mv -f -- "$SECRET_ENV_FILE.tmp.$$" "$SECRET_ENV_FILE"
    if [[ -f "$prod_env_backup" ]]; then
        install -m 0600 "$prod_env_backup" "$PROD_RUNTIME_ENV.tmp.$$"
        mv -f -- "$PROD_RUNTIME_ENV.tmp.$$" "$PROD_RUNTIME_ENV"
    fi
    local unit_backup="$backup_dir/config/$SERVICE_NAME"
    if [[ -L "$unit_backup" ]]; then
        die "refusing to restore symlinked unit backup: $unit_backup"
    fi
    if [[ -f "$unit_backup" ]]; then
        install -m 0600 "$unit_backup" "$PROD_UNIT_PATH.tmp.$$"
        mv -f -- "$PROD_UNIT_PATH.tmp.$$" "$PROD_UNIT_PATH"
    fi
}

write_release_manifest() {
    local release_root="$1"
    local release="$2"
    local source_root="$3"
    local commit="$4"
    local dirty="$5"
    local environment_count
    local environment_digest
    local venv_tree_count
    local venv_tree_digest
    clean_venv_runtime_cache "$release_root/backend/.venv"
    assert_venv_runtime_cache_absent "$release_root/backend/.venv"
    IFS=$'\t' read -r environment_count environment_digest \
        < <(release_environment_evidence "$release_root/backend/.venv/bin/python")
    IFS=$'\t' read -r venv_tree_count venv_tree_digest \
        < <(venv_tree_evidence "$release_root/backend/.venv")
    "$PYTHON_BIN" - \
        "$release_root" "$release" "$source_root" "$commit" "$dirty" \
        "$environment_count" "$environment_digest" \
        "$venv_tree_count" "$venv_tree_digest" <<'PY'
import datetime
import hashlib
import json
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
source = pathlib.Path(sys.argv[3]).resolve()
digest = hashlib.sha256()
for path in sorted(root.rglob("*")):
    if (
        not path.is_file()
        or path.name == "RELEASE.json"
        or ".venv" in path.parts
        or "__pycache__" in path.parts
    ):
        continue
    relative = path.relative_to(root).as_posix()
    digest.update(relative.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
version_text = (root / "backend/app/__init__.py").read_text(encoding="utf-8")
match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', version_text, re.MULTILINE)
if not match:
    raise SystemExit("backend version not found")
payload = {
    "schema": 2,
    "release_id": sys.argv[2],
    "app_version": match.group(1),
    "source_root": str(source),
    "source_commit": sys.argv[4],
    "source_dirty": sys.argv[5] == "true",
    "code_sha256": digest.hexdigest(),
    "venv_distribution_count": int(sys.argv[6]),
    "venv_record_aggregate_sha256": sys.argv[7],
    "venv_tree_digest_schema": 1,
    "venv_tree_entry_count": int(sys.argv[8]),
    "venv_tree_sha256": sys.argv[9],
    "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
}
(root / "RELEASE.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
    chmod 0600 "$release_root/RELEASE.json"
}

write_canary_marker() {
    local release="$1"
    local marker="$2"
    local digest="$3"
    local port="$4"
    local source_database="$5"
    "$PYTHON_BIN" - "$release" "$marker" "$digest" "$port" "$source_database" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

payload = {
    "schema": 1,
    "release_id": sys.argv[1],
    "release_manifest_sha256": sys.argv[3],
    "port": int(sys.argv[4]),
    "source_database": str(pathlib.Path(sys.argv[5]).resolve(strict=True)),
    "passed_at": datetime.datetime.now(datetime.UTC).isoformat(),
}
path = pathlib.Path(sys.argv[2])
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

write_operation_bindings() {
    local destination="$1"
    local kind="$2"
    local release="$3"
    local allow_missing_database="$4"
    "$PYTHON_BIN" - \
        "$destination" "$kind" "$release" "$allow_missing_database" \
        "$DEPLOY_ROOT" "$SERVICE_NAME" "$PROD_PORT" "$PROD_DB" \
        "$SECRET_ENV_FILE" "$PROD_UNIT_PATH" "$INGRESS_GATE" \
        "$DEPLOYMENT_GATE_FILE" "$PUBLIC_BASE_URL" \
        "$(release_dir "$release")/RELEASE.json" \
        "$LEGACY_ENV_FILE" "$LEGACY_DB" "$LEGACY_DATA_ROOT" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_file(raw: str, label: str) -> pathlib.Path:
    path = pathlib.Path(raw)
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


destination = pathlib.Path(sys.argv[1])
kind = sys.argv[2]
release = sys.argv[3]
allow_missing_database = sys.argv[4] == "1"
root = pathlib.Path(sys.argv[5]).resolve(strict=True)
database = pathlib.Path(sys.argv[8])
if database.exists():
    if database.is_symlink() or not database.is_file():
        raise SystemExit("production database must be a regular non-symlink file")
    database_path = database.resolve(strict=True)
    database_stat = database_path.stat()
elif allow_missing_database:
    database_path = database.parent.resolve(strict=True) / database.name
else:
    raise SystemExit("production database is missing")
env_file = canonical_file(sys.argv[9], "runtime env")
unit_input = pathlib.Path(sys.argv[10])
unit_file = canonical_file(sys.argv[10], "production unit")
gate = canonical_file(sys.argv[11], "ingress gate")
gate_file = pathlib.Path(sys.argv[12])
if gate_file.is_symlink():
    raise SystemExit("deployment gate token path must not be a symlink")
gate_file_path = gate_file.parent.resolve(strict=True) / gate_file.name
manifest = canonical_file(sys.argv[14], "release manifest")
payload = {
    "schema": 1,
    "kind": kind,
    "release_id": release,
    "deploy_root": str(root),
    "service": sys.argv[6],
    "production_port": int(sys.argv[7]),
    "production_database": str(database_path),
    "runtime_env": str(env_file),
    "runtime_env_sha256": sha256(env_file),
    "unit_install_path": str(unit_input.absolute()),
    "unit_path": str(unit_file),
    "unit_sha256": sha256(unit_file),
    "ingress_gate": str(gate),
    "ingress_gate_sha256": sha256(gate),
    "deployment_gate_file": str(gate_file_path),
    "public_url": sys.argv[13],
    "release_manifest": str(manifest),
    "release_manifest_sha256": sha256(manifest),
}
if database.exists():
    payload["production_database_device"] = database_stat.st_dev
    payload["production_database_inode"] = database_stat.st_ino
if sys.argv[15]:
    legacy_env = canonical_file(sys.argv[15], "legacy env")
    legacy_db = canonical_file(sys.argv[16], "legacy database")
    legacy_root = pathlib.Path(sys.argv[17]).resolve(strict=True)
    payload["legacy"] = {
        "env_file": str(legacy_env),
        "env_sha256": sha256(legacy_env),
        "database": str(legacy_db),
        "data_root": str(legacy_root),
        "storage": str((legacy_root / "storage").resolve(strict=True)),
        "rag_index": str((legacy_root / "rag_index").resolve(strict=True)),
        "user_root": str(legacy_root),
    }
canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
payload["binding_sha256"] = hashlib.sha256(canonical).hexdigest()
destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
with destination.open("x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
destination.chmod(0o600)
PY
}

validate_operation_bindings() {
    local binding_file="$1"
    local allow_masked_unit="$2"
    "$PYTHON_BIN" - \
        "$binding_file" "$allow_masked_unit" "$DEPLOY_ROOT" "$SERVICE_NAME" \
        "$PROD_PORT" "$PROD_DB" "$SECRET_ENV_FILE" "$PROD_UNIT_PATH" \
        "$INGRESS_GATE" "$DEPLOYMENT_GATE_FILE" "$PUBLIC_BASE_URL" "$RELEASE_ID" \
        "$(release_dir "$RELEASE_ID")/RELEASE.json" \
        "$LEGACY_ENV_FILE" "$LEGACY_DB" "$LEGACY_DATA_ROOT" <<'PY'
import hashlib
import json
import pathlib
import sys


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


path = pathlib.Path(sys.argv[1]).resolve(strict=True)
payload = json.loads(path.read_text(encoding="utf-8"))
claimed = payload.pop("binding_sha256", "")
canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
if claimed != hashlib.sha256(canonical).hexdigest():
    raise SystemExit("operation binding digest mismatch")
allow_masked = sys.argv[2] == "1"
root = pathlib.Path(sys.argv[3]).resolve(strict=True)
database = pathlib.Path(sys.argv[6])
database_path = (
    database.resolve(strict=True)
    if database.exists()
    else database.parent.resolve(strict=True) / database.name
)
env_file = pathlib.Path(sys.argv[7]).resolve(strict=True)
unit_input = pathlib.Path(sys.argv[8])
gate = pathlib.Path(sys.argv[9]).resolve(strict=True)
gate_file = pathlib.Path(sys.argv[10])
if gate_file.is_symlink():
    raise SystemExit("deployment gate token path must not be a symlink")
gate_file_path = gate_file.parent.resolve(strict=True) / gate_file.name
manifest = pathlib.Path(sys.argv[13]).resolve(strict=True)
expected = {
    "deploy_root": str(root),
    "service": sys.argv[4],
    "production_port": int(sys.argv[5]),
    "production_database": str(database_path),
    "runtime_env": str(env_file),
    "runtime_env_sha256": sha256(env_file),
    "unit_install_path": str(unit_input.absolute()),
    "ingress_gate": str(gate),
    "ingress_gate_sha256": sha256(gate),
    "deployment_gate_file": str(gate_file_path),
    "public_url": sys.argv[11],
    "release_id": sys.argv[12],
    "release_manifest": str(manifest),
    "release_manifest_sha256": sha256(manifest),
}
for key, value in expected.items():
    if payload.get(key) != value:
        raise SystemExit(f"operation binding mismatch: {key}")
if "production_database_inode" in payload:
    database_stat = database.resolve(strict=True).stat()
    if payload.get("production_database_device") != database_stat.st_dev:
        raise SystemExit("operation binding mismatch: production_database_device")
    if payload.get("production_database_inode") != database_stat.st_ino:
        raise SystemExit("operation binding mismatch: production_database_inode")
if not allow_masked:
    if unit_input.is_symlink() or not unit_input.is_file():
        raise SystemExit("production unit is missing, symlinked, or masked")
    if payload.get("unit_sha256") != sha256(unit_input.resolve(strict=True)):
        raise SystemExit("operation binding mismatch: unit_sha256")
    if payload.get("unit_path") != str(unit_input.resolve(strict=True)):
        raise SystemExit("operation binding mismatch: unit_path")
if "legacy" in payload:
    if not all(sys.argv[index] for index in (14, 15, 16)):
        raise SystemExit("legacy CLI bindings are required")
    legacy_env = pathlib.Path(sys.argv[14]).resolve(strict=True)
    legacy_db = pathlib.Path(sys.argv[15]).resolve(strict=True)
    legacy_root = pathlib.Path(sys.argv[16]).resolve(strict=True)
    legacy_expected = {
        "env_file": str(legacy_env),
        "env_sha256": sha256(legacy_env),
        "database": str(legacy_db),
        "data_root": str(legacy_root),
        "storage": str((legacy_root / "storage").resolve(strict=True)),
        "rag_index": str((legacy_root / "rag_index").resolve(strict=True)),
        "user_root": str(legacy_root),
    }
    for key, value in legacy_expected.items():
        if payload["legacy"].get(key) != value:
            raise SystemExit(f"legacy operation binding mismatch: {key}")
PY
}

validate_active_deployment_record() {
    local record="$1"
    "$PYTHON_BIN" - "$record" "$DEPLOYMENTS_DIR" "$BACKUPS_DIR" <<'PY'
import hashlib
import json
import pathlib
import sys

record = pathlib.Path(sys.argv[1]).resolve(strict=True)
deployments = pathlib.Path(sys.argv[2]).resolve(strict=True)
backups = pathlib.Path(sys.argv[3]).resolve(strict=True)
if record.parent != deployments:
    raise SystemExit("deployment record escapes deployments directory")
payload = json.loads(record.read_text(encoding="utf-8"))
if payload.get("status") != "active":
    raise SystemExit("deployment record is not active; refusing replay")
backup = pathlib.Path(payload.get("backup_dir", "")).resolve(strict=True)
if backup.parent != backups:
    raise SystemExit("deployment backup escapes backups directory")
binding_file = pathlib.Path(payload.get("bindings_file", "")).resolve(strict=True)
if binding_file.parent != backup or binding_file.name != "SUCCESS_BINDINGS.json":
    raise SystemExit("deployment binding file is outside its immutable backup")
bindings = json.loads(binding_file.read_text(encoding="utf-8"))
if payload.get("bindings") != bindings:
    raise SystemExit("deployment record binding payload mismatch")
if payload.get("deployment_kind") == "bootstrap":
    snapshot = backup / "legacy/echodesk.db"
    expected = payload.get("stopped_legacy_snapshot_sha256")
else:
    snapshot = backup / "echodesk.db"
    expected = payload.get("pre_deploy_database_sha256")
if not snapshot.is_file() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != expected:
    raise SystemExit("deployment database snapshot digest mismatch")
PY
    local validation_status=$?
    ((validation_status == 0)) || return "$validation_status"
    local binding_file
    binding_file="$(json_get "$record" bindings_file)"
    validate_operation_bindings "$binding_file" 0
}

write_deployment_record() {
    local destination="$1"
    local deployment="$2"
    local release="$3"
    local previous="$4"
    local backup_dir="$5"
    local bindings="$6"
    "$PYTHON_BIN" - \
        "$destination" "$deployment" "$release" "$previous" "$backup_dir" "$bindings" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

payload = {
    "schema": 1,
    "deployment_kind": "release",
    "deployment_id": sys.argv[2],
    "release_id": sys.argv[3],
    "previous_release_id": sys.argv[4],
    "backup_dir": sys.argv[5],
    "bindings_file": str(pathlib.Path(sys.argv[6]).resolve(strict=True)),
    "bindings": json.loads(pathlib.Path(sys.argv[6]).read_text(encoding="utf-8")),
    "pre_deploy_database_sha256": hashlib.sha256(
        (pathlib.Path(sys.argv[5]) / "echodesk.db").read_bytes()
    ).hexdigest(),
    "promoted_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "status": "active",
}
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

write_bootstrap_record() {
    local destination="$1"
    local deployment="$2"
    local release="$3"
    local backup_dir="$4"
    local bindings="$5"
    "$PYTHON_BIN" - \
        "$destination" "$deployment" "$release" "$backup_dir" "$bindings" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

payload = {
    "schema": 1,
    "deployment_kind": "bootstrap",
    "deployment_id": sys.argv[2],
    "release_id": sys.argv[3],
    "backup_dir": sys.argv[4],
    "bindings_file": str(pathlib.Path(sys.argv[5]).resolve(strict=True)),
    "bindings": json.loads(pathlib.Path(sys.argv[5]).read_text(encoding="utf-8")),
    "stopped_legacy_snapshot_sha256": hashlib.sha256(
        (pathlib.Path(sys.argv[4]) / "legacy/echodesk.db").read_bytes()
    ).hexdigest(),
    "legacy_preservation": "read_only_source_preserved",
    "rollback_policy": "fail_closed_never_start_non_isolated_legacy",
    "promoted_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "status": "active",
}
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

mark_deployment_rolled_back() {
    local record="$1"
    local rollback_id="$2"
    local status="$3"
    "$PYTHON_BIN" - "$record" "$rollback_id" "$status" <<'PY'
import datetime
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1]).resolve(strict=True)
payload = json.loads(path.read_text(encoding="utf-8"))
payload["status"] = sys.argv[3]
payload["rolled_back_at"] = datetime.datetime.now(datetime.UTC).isoformat()
payload["rollback_safety_backup"] = sys.argv[2]
tmp = path.with_name(f".{path.name}.tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.chmod(0o600)
tmp.replace(path)
PY
}

atomic_latest_record() {
    local record="$1"
    "$PYTHON_BIN" - "$DEPLOYMENTS_DIR/latest.json" "$record" <<'PY'
import os
import pathlib
import sys

link = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2]).resolve(strict=True)
tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
try:
    tmp.symlink_to(target)
    os.replace(tmp, link)
finally:
    tmp.unlink(missing_ok=True)
PY
}

acquire_lock() {
    require_command flock
    exec 9>"$LOCK_FILE"
    flock -n "$LOCK_FD" || die "another deployment operation holds $LOCK_FILE"
}

wait_http_ready() {
    local port="$1"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_S))
    local health="http://127.0.0.1:$port/healthz"
    local ready="http://127.0.0.1:$port/readyz"
    while ((SECONDS < deadline)); do
        if curl --fail --silent --show-error --max-time 2 "$health" >/dev/null 2>&1 \
            && curl --fail --silent --show-error --max-time 2 "$ready" \
                | "$PYTHON_BIN" -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "ready" else 1)' \
                >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

wait_http_health() {
    local port="$1"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_S))
    local health="http://127.0.0.1:$port/healthz"
    while ((SECONDS < deadline)); do
        if curl --fail --silent --show-error --max-time 2 "$health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

verify_unit_release() {
    local unit="$1"
    local release="$2"
    local expected
    local pid
    local cwd
    expected="$(cd "$(release_dir "$release")" && pwd -P)"
    pid="$(systemctl --user show "$unit" --property=MainPID --value)"
    [[ "$pid" =~ ^[0-9]+$ && "$pid" != "0" ]] || return 1
    cwd="$(readlink "/proc/$pid/cwd")"
    [[ "$cwd" == "$expected" ]]
}

run_isolation_smoke() {
    local release="$1"
    local port="$2"
    local python
    local -a gate_args=()
    python="$(release_python "$release")"
    if [[ -f "$DEPLOYMENT_GATE_FILE" && ! -L "$DEPLOYMENT_GATE_FILE" ]]; then
        gate_args=(--deployment-gate-file "$DEPLOYMENT_GATE_FILE")
    fi
    "$python" "$(release_dir "$release")/scripts/public-isolation-smoke.py" \
        --base-url "http://127.0.0.1:$port" \
        --timeout "$HEALTH_TIMEOUT_S" \
        "${gate_args[@]}"
}

run_public_isolation_smoke() {
    local release="$1"
    local python
    [[ ! -e "$DEPLOYMENT_GATE_FILE" && ! -L "$DEPLOYMENT_GATE_FILE" ]] \
        || die "public no-header smoke requires an atomically open deployment gate"
    python="$(release_python "$release")"
    "$python" "$(release_dir "$release")/scripts/public-isolation-smoke.py" \
        --base-url "$PUBLIC_BASE_URL" \
        --timeout "$HEALTH_TIMEOUT_S"
}

canary_unit_name() {
    printf 'echodesk-canary-%s.service' "$1"
}

stop_canary() {
    local release="$1"
    local unit
    unit="$(canary_unit_name "$release")"
    systemctl --user stop "$unit" >/dev/null 2>&1 || true
}

require_release() {
    local release="$1"
    local directory
    directory="$(release_dir "$release")"
    [[ -d "$directory" ]] || die "release not found: $directory"
    [[ -f "$directory/RELEASE.json" ]] || die "release manifest missing: $directory/RELEASE.json"
    [[ -x "$directory/backend/.venv/bin/python" ]] || die "release venv missing: $directory"
    [[ -f "$directory/scripts/public-isolation-smoke.py" ]] \
        || die "release isolation smoke missing: $directory"
    [[ -x "$directory/scripts/echodesk-ingress-gate.py" ]] \
        || die "release ingress gate helper missing: $directory"
    verify_release_integrity "$release"
}

release_integrity_ok() {
    (require_release "$1") >/dev/null 2>&1
}

verify_canary_gate() {
    local release="$1"
    local marker
    local expected
    local recorded
    marker="$(canary_dir "$release")/PASSED.json"
    [[ -f "$marker" ]] || die "canary pass marker missing: $marker"
    expected="$(release_manifest_digest "$release")"
    recorded="$(json_get "$marker" release_manifest_sha256)"
    [[ "$recorded" == "$expected" ]] || die "release changed after canary passed"
}

verify_bootstrap_canary_gate() {
    local release="$1"
    verify_canary_gate "$release"
    local marker
    local recorded
    local expected
    marker="$(canary_dir "$release")/PASSED.json"
    recorded="$(json_get_optional "$marker" source_database)"
    [[ -n "$recorded" ]] \
        || die "canary marker does not prove which production database was cloned"
    expected="$($PYTHON_BIN - "$LEGACY_DB" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).resolve(strict=True))
PY
)"
    [[ "$recorded" == "$expected" ]] \
        || die "canary was not created from the declared legacy database"
}

assert_ingress_gate_safe() {
    if [[ -z "$INGRESS_GATE" ]]; then
        [[ -n "$RELEASE_ID" ]] \
            || die "cannot resolve the default ingress gate without --release"
        INGRESS_GATE="$(release_dir "$RELEASE_ID")/scripts/echodesk-ingress-gate.py"
    fi
    safe_absolute_path "ingress gate" "$INGRESS_GATE"
    [[ -f "$INGRESS_GATE" && -x "$INGRESS_GATE" && ! -L "$INGRESS_GATE" ]] \
        || die "ingress gate must be an executable regular non-symlink file: $INGRESS_GATE"
    "$PYTHON_BIN" - "$INGRESS_GATE" <<'PY'
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
info = path.stat()
mode = stat.S_IMODE(info.st_mode)
if info.st_uid != os.geteuid():
    raise SystemExit("ingress gate must be owned by the deployment user")
if mode & 0o022:
    raise SystemExit("ingress gate must not be group/world writable")
path.resolve(strict=True)
PY
}

assert_public_url_safe() {
    [[ -n "$PUBLIC_BASE_URL" ]] || die "--public-url is required for production cutover"
    "$PYTHON_BIN" - "$PUBLIC_BASE_URL" <<'PY'
import sys
import urllib.parse

value = urllib.parse.urlsplit(sys.argv[1])
if (
    value.scheme != "https"
    or not value.hostname
    or value.username is not None
    or value.password is not None
    or value.query
    or value.fragment
    or value.path not in {"", "/"}
):
    raise SystemExit("public URL must be an origin-only HTTPS URL without credentials/query/fragment")
PY
}

ingress_gate_status() {
    local value
    value="$(ECHODESK_DEPLOYMENT_GATE_FILE="$DEPLOYMENT_GATE_FILE" \
        "$INGRESS_GATE" status "$SERVICE_NAME" "$PROD_PORT")" \
        || die "ingress gate status command failed"
    case "$value" in
        open|closed) printf '%s' "$value" ;;
        *) die "ingress gate status must return exactly open or closed" ;;
    esac
}

require_ingress_state() {
    local expected="$1"
    [[ "$(ingress_gate_status)" == "$expected" ]] \
        || die "ingress gate did not reach required state: $expected"
}

set_ingress_state() {
    local action="$1"
    local expected="$2"
    ECHODESK_DEPLOYMENT_GATE_FILE="$DEPLOYMENT_GATE_FILE" \
        "$INGRESS_GATE" "$action" "$SERVICE_NAME" "$PROD_PORT" >/dev/null \
        || die "ingress gate $action command failed"
    require_ingress_state "$expected"
}

port_has_listener() {
    local listeners
    listeners="$(command "$SS_BIN" -H -ltn)" || die "failed to inspect listening TCP ports"
    "$PYTHON_BIN" - "$PROD_PORT" "$listeners" <<'PY'
import sys

port = int(sys.argv[1])
for line in sys.argv[2].splitlines():
    fields = line.split()
    if len(fields) < 4:
        continue
    local = fields[3]
    try:
        value = int(local.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        continue
    if value == port:
        raise SystemExit(0)
raise SystemExit(1)
PY
}

service_enabled_state() {
    local value
    value="$(systemctl --user is-enabled "$SERVICE_NAME" 2>/dev/null || true)"
    printf '%s' "$value"
}

assert_service_start_disabled() {
    local state
    state="$(service_enabled_state)"
    [[ "$state" == "disabled" ]] \
        || die "target service must remain start-disabled during validation, got: ${state:-unknown}"
}

assert_service_active_enabled() {
    systemctl --user is-active --quiet "$SERVICE_NAME" \
        || die "production service is not active after cutover"
    [[ "$(service_enabled_state)" == "enabled" ]] \
        || die "production service is not enabled after ingress opened"
    require_ingress_state open
}

atomic_mask_production_unit() {
    "$PYTHON_BIN" - "$PROD_UNIT_PATH" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.is_symlink() and path.readlink() == pathlib.Path("/dev/null"):
    raise SystemExit(0)
if path.exists() and not path.is_file():
    raise SystemExit("production unit path is neither a regular file nor an existing mask")
tmp = path.with_name(f".{path.name}.mask-{os.getpid()}")
try:
    tmp.symlink_to("/dev/null")
    os.replace(tmp, path)
finally:
    tmp.unlink(missing_ok=True)
PY
}

assert_fail_closed() {
    local failed=0
    if systemctl --user is-active --quiet "$SERVICE_NAME"; then
        warn "fail-closed verification: service is still active"
        failed=1
    fi
    local enabled
    enabled="$(service_enabled_state)"
    case "$enabled" in
        masked|masked-runtime) ;;
        *)
            warn "fail-closed verification: service is not masked (${enabled:-unknown})"
            failed=1
            ;;
    esac
    if port_has_listener; then
        warn "fail-closed verification: production port $PROD_PORT still has a listener"
        failed=1
    fi
    if [[ -n "$INGRESS_GATE" ]] && [[ "$(ingress_gate_status)" != "closed" ]]; then
        warn "fail-closed verification: ingress gate is not closed"
        failed=1
    fi
    ((failed == 0))
}

enforce_fail_closed() {
    if [[ -n "$INGRESS_GATE" ]]; then
        ECHODESK_DEPLOYMENT_GATE_FILE="$DEPLOYMENT_GATE_FILE" \
            "$INGRESS_GATE" close "$SERVICE_NAME" "$PROD_PORT" >/dev/null 2>&1 || true
    fi
    systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    atomic_mask_production_unit || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    assert_fail_closed
}

persist_phase() {
    local path="$1"
    local phase="$2"
    local status="$3"
    local kind="$4"
    "$PYTHON_BIN" - "$path" "$phase" "$status" "$kind" <<'PY'
import datetime
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = {"schema": 1, "deployment_kind": sys.argv[4], "history": []}
if path.exists():
    payload = json.loads(path.read_text(encoding="utf-8"))
entry = {
    "phase": sys.argv[2],
    "status": sys.argv[3],
    "at": datetime.datetime.now(datetime.UTC).isoformat(),
}
payload["current_phase"] = sys.argv[2]
payload["status"] = sys.argv[3]
payload.setdefault("history", []).append(entry)
tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
with tmp.open("x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
tmp.chmod(0o600)
os.replace(tmp, path)
directory_fd = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
    BOOTSTRAP_PHASE="$phase"
}

persist_bootstrap_phase() {
    [[ -n "$BOOTSTRAP_PHASE_FILE" ]] || return 0
    persist_phase "$BOOTSTRAP_PHASE_FILE" "$1" "$2" "bootstrap"
}

assert_rsync_target_safe() {
    local target="$1"
    "$PYTHON_BIN" - "$DATA_ROOT" "$target" <<'PY'
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
if not root.is_absolute() or not target.is_absolute():
    raise SystemExit("rsync root and target must be absolute")
root_info = root.lstat()
if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
    raise SystemExit("rsync data root must be a non-symlink directory")
root_real = root.resolve(strict=True)
try:
    relative = target.relative_to(root)
except ValueError as exc:
    raise SystemExit("rsync target escapes lexical data root") from exc
cursor = root
for part in relative.parts:
    cursor = cursor / part
    if not cursor.exists() and not cursor.is_symlink():
        break
    info = cursor.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"rsync target path contains symlink: {cursor}")
resolved = target.resolve(strict=False)
if not resolved.is_relative_to(root_real):
    raise SystemExit("rsync target escapes canonical data root")
if target.exists():
    for base, directories, files in os.walk(target, followlinks=False):
        for name in (*directories, *files):
            child = pathlib.Path(base) / name
            if stat.S_ISLNK(child.lstat().st_mode):
                raise SystemExit(f"rsync target tree contains symlink: {child}")
PY
}

assert_bootstrap_paths_safe() {
    [[ -d "$LEGACY_DATA_ROOT" && ! -L "$LEGACY_DATA_ROOT" ]] \
        || die "legacy data root must be a regular directory: $LEGACY_DATA_ROOT"
    [[ -d "$LEGACY_DATA_ROOT/storage" && ! -L "$LEGACY_DATA_ROOT/storage" ]] \
        || die "legacy storage directory missing or symlinked: $LEGACY_DATA_ROOT/storage"
    [[ -d "$LEGACY_DATA_ROOT/rag_index" && ! -L "$LEGACY_DATA_ROOT/rag_index" ]] \
        || die "legacy RAG directory missing or symlinked: $LEGACY_DATA_ROOT/rag_index"
    [[ -f "$LEGACY_DB" && ! -L "$LEGACY_DB" ]] \
        || die "legacy database must be a regular non-symlink file: $LEGACY_DB"
    [[ ! -e "$PROD_DB" && ! -L "$PROD_DB" ]] \
        || die "new production database already exists; refusing bootstrap overwrite: $PROD_DB"
    [[ -f "$PROD_UNIT_PATH" && ! -L "$PROD_UNIT_PATH" ]] \
        || die "legacy production unit must be a regular file: $PROD_UNIT_PATH"
    [[ ! -e "$CURRENT_LINK" && ! -L "$CURRENT_LINK" ]] \
        || die "bootstrap requires an uninitialized current symlink"
    "$PYTHON_BIN" - \
        "$LEGACY_DATA_ROOT" "$LEGACY_DB" "$DEPLOY_ROOT" "$DATA_ROOT" "$PROD_DB" <<'PY'
import pathlib
import sys

legacy_root = pathlib.Path(sys.argv[1]).resolve(strict=True)
legacy_db = pathlib.Path(sys.argv[2]).resolve(strict=True)
deploy_root = pathlib.Path(sys.argv[3]).resolve(strict=True)
data_root = pathlib.Path(sys.argv[4]).resolve(strict=True)
production_db = pathlib.Path(sys.argv[5]).resolve(strict=False)
if not legacy_db.is_relative_to(legacy_root):
    raise SystemExit("legacy database must be contained by legacy data root")
if legacy_root == deploy_root or legacy_root.is_relative_to(deploy_root) or deploy_root.is_relative_to(legacy_root):
    raise SystemExit("legacy data root and deployment root must be disjoint")
expected_db = (data_root / "echodesk.db").resolve(strict=False)
if production_db != expected_db:
    raise SystemExit(f"bootstrap production database must be {expected_db}")
PY
}

sync_legacy_data() {
    local phase="$1"
    assert_rsync_target_safe "$DATA_ROOT/storage"
    assert_rsync_target_safe "$DATA_ROOT/rag_index"
    assert_rsync_target_safe "$DATA_ROOT/user"
    install -d -m 0700 "$DATA_ROOT/storage" "$DATA_ROOT/rag_index" "$DATA_ROOT/user"
    assert_rsync_target_safe "$DATA_ROOT/storage"
    assert_rsync_target_safe "$DATA_ROOT/rag_index"
    assert_rsync_target_safe "$DATA_ROOT/user"
    rsync -a --delete --safe-links \
        "$LEGACY_DATA_ROOT/storage/" "$DATA_ROOT/storage/"
    rsync -a --delete --safe-links \
        "$LEGACY_DATA_ROOT/rag_index/" "$DATA_ROOT/rag_index/"
    rsync -a --delete --safe-links \
        --exclude='/storage/' --exclude='/rag_index/' --exclude='/logs/' \
        --exclude='/echodesk.db*' --exclude='/*.sqlite*' \
        "$LEGACY_DATA_ROOT/" "$DATA_ROOT/user/"
    assert_rsync_target_safe "$DATA_ROOT/storage"
    assert_rsync_target_safe "$DATA_ROOT/rag_index"
    assert_rsync_target_safe "$DATA_ROOT/user"
    log "$phase legacy storage/RAG/user sync complete; legacy source was read only"
}

cmd_prepare() {
    [[ -n "$RELEASE_ID" ]] || die "prepare requires --release"
    validate_release_id "$RELEASE_ID"
    safe_absolute_path "source root" "$SOURCE_ROOT"
    local destination
    local partial
    destination="$(release_dir "$RELEASE_ID")"
    partial="$RELEASES_DIR/.prepare-$RELEASE_ID-$$"
    require_command rsync
    require_command git
    [[ -d "$SOURCE_ROOT/backend" && -f "$SOURCE_ROOT/backend/requirements.lock" ]] \
        || die "source lacks backend/requirements.lock: $SOURCE_ROOT"
    [[ -f "$SOURCE_ROOT/scripts/public-isolation-smoke.py" ]] \
        || die "source lacks scripts/public-isolation-smoke.py"
    [[ -f "$SOURCE_ROOT/scripts/echodesk-ingress-gate.py" ]] \
        || die "source lacks scripts/echodesk-ingress-gate.py"
    [[ ! -e "$destination" && ! -e "$partial" ]] \
        || die "release or partial already exists; refusing overwrite: $RELEASE_ID"
    if git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        && [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ]] \
        && ((ALLOW_DIRTY == 0)); then
        die "source checkout is dirty; use a clean CI checkout or --allow-dirty"
    fi

    if ((DRY_RUN)); then
        plan "read-only prepare preflight passed for clean source $SOURCE_ROOT"
        plan "create immutable release $destination from $SOURCE_ROOT"
        plan "copy backend excluding .env/.venv/databases/caches" rsync -a "$SOURCE_ROOT/backend/" "$partial/backend/"
        plan "copy only the isolation smoke and ingress-gate deployment helpers" install "$SOURCE_ROOT/scripts/public-isolation-smoke.py" "$SOURCE_ROOT/scripts/echodesk-ingress-gate.py" "$partial/scripts/"
        plan "create copied-interpreter release venv and install hash-locked dependencies" "$PYTHON_BIN" -m venv --copies "$partial/backend/.venv"
        plan "install locked PPT runtime dependencies when package-lock.json is present" npm ci --omit=dev --ignore-scripts
        plan "bind the complete stable venv tree plus installed distributions/RECORD contents, seal read-only, then atomically rename"
        return
    fi

    ensure_layout
    local commit="unknown"
    local dirty="false"
    if git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        commit="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
        if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ]]; then
            dirty="true"
            ((ALLOW_DIRTY)) || die "source checkout is dirty; use a clean CI checkout or --allow-dirty"
        fi
    fi

    PREPARE_TMP="$partial"
    install -d -m 0700 "$partial/backend" "$partial/scripts"
    rsync -a \
        --exclude='.env*' --exclude='.venv/' \
        --exclude='*.db' --exclude='*.db-*' --exclude='*.sqlite' --exclude='*.sqlite-*' \
        --exclude='__pycache__/' --exclude='.pytest_cache/' --exclude='.mypy_cache/' \
        --exclude='.ruff_cache/' --exclude='build/' --exclude='dist/' \
        "$SOURCE_ROOT/backend/" "$partial/backend/"
    install -m 0755 "$SOURCE_ROOT/scripts/public-isolation-smoke.py" \
        "$partial/scripts/public-isolation-smoke.py"
    install -m 0755 "$SOURCE_ROOT/scripts/echodesk-ingress-gate.py" \
        "$partial/scripts/echodesk-ingress-gate.py"

    if find "$partial" -type f \( -name '.env*' -o -name '*.db' \
        -o -name '*.sqlite' \) -print -quit | grep -q .; then
        die "secret/database file entered release staging"
    fi

    "$PYTHON_BIN" -m venv --copies "$partial/backend/.venv"
    "$partial/backend/.venv/bin/python" -m pip install \
        --disable-pip-version-check --require-hashes \
        -r "$partial/backend/requirements.lock"
    "$partial/backend/.venv/bin/python" -m pip check

    local deck_dir="$partial/backend/app/adapters/skill/assets/ppt_ib_deck"
    if [[ -f "$deck_dir/package-lock.json" ]]; then
        require_command npm
        (
            cd "$deck_dir"
            npm ci --omit=dev --ignore-scripts --no-audit --no-fund
        )
    fi

    local smoke_root="$partial/.prepare-smoke"
    (
        cd "$partial/backend"
        env \
            ECHO_USER_DIR="$smoke_root/user" \
            DB_PATH="$smoke_root/echodesk.db" \
            STORAGE_DIR="$smoke_root/storage" \
            RAG_INDEX_DIR="$smoke_root/rag" \
            SKILL_EXECUTOR_BUILD_DIR="$smoke_root/skills" \
            WORKSPACE_STATE_FILE="$smoke_root/workspace.json" \
            PUBLIC_DEMO_MODE=true \
            WORKSPACE_SCAN_ON_STARTUP=false \
            PYTHONDONTWRITEBYTECODE=1 \
            "$partial/backend/.venv/bin/python" -c \
            'from app.main import create_app; assert create_app() is not None' \
            >/dev/null
    )
    rm -rf -- "$smoke_root"

    write_release_manifest "$partial" "$RELEASE_ID" "$SOURCE_ROOT" "$commit" "$dirty"
    seal_release_tree "$partial"
    verify_release_readonly "$partial"
    mv -- "$partial" "$destination"
    PREPARE_TMP=""
    verify_release_integrity "$RELEASE_ID"
    log "prepared immutable release: $destination"
}

cmd_install_unit() {
    if [[ -n "$RELEASE_ID" ]]; then
        validate_release_id "$RELEASE_ID"
    fi
    assert_secret_env_safe
    if [[ -n "$RELEASE_ID" ]]; then
        require_release "$RELEASE_ID"
        if [[ -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]]; then
            current_release >/dev/null || die "current release symlink is unsafe"
        fi
    else
        current_release >/dev/null || die "current release is not initialized"
    fi
    if ((DRY_RUN)); then
        plan "read-only install-unit preflight passed for env/current/release"
        plan "backup existing runtime config and $PROD_UNIT_PATH without printing content"
        if [[ -n "$RELEASE_ID" ]]; then
            plan "set current to $RELEASE_ID only if no current symlink exists"
        fi
        plan "write production path-only env and stable systemd unit; daemon-reload only"
        return
    fi

    require_command "$SYSTEMCTL_BIN"
    ensure_layout
    if [[ -n "$RELEASE_ID" ]]; then
        require_release "$RELEASE_ID"
        if [[ ! -e "$CURRENT_LINK" && ! -L "$CURRENT_LINK" ]]; then
            atomic_switch "$RELEASE_ID"
        fi
    fi
    current_release >/dev/null || die "current release is not initialized"

    local backup_dir="$BACKUPS_DIR/unit-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    [[ ! -e "$backup_dir" ]] || die "unit backup already exists: $backup_dir"
    backup_configs "$backup_dir"
    if [[ -f "$PROD_DB" ]]; then
        sqlite_backup "$PROD_DB" "$backup_dir/echodesk.db"
    else
        warn "production DB does not exist yet; unit migration backup contains config only"
    fi

    write_runtime_env "$PROD_RUNTIME_ENV" "$DATA_ROOT" "$PROD_DB" "$PROD_PORT" 0
    local unit_tmp="$DEPLOY_ROOT/.unit-$SERVICE_NAME-$$"
    render_unit "$unit_tmp" "EchoDesk public backend" "$CURRENT_LINK" \
        "$PROD_RUNTIME_ENV" "$DATA_ROOT" "$PROD_PORT" "on-failure"
    install -m 0600 "$unit_tmp" "$PROD_UNIT_PATH.tmp.$$"
    mv -f -- "$PROD_UNIT_PATH.tmp.$$" "$PROD_UNIT_PATH"
    rm -f -- "$unit_tmp"
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    log "installed unit without restart: $PROD_UNIT_PATH"
    log "previous unit/config backup: $backup_dir"
}

cmd_canary() {
    [[ -n "$RELEASE_ID" ]] || die "canary requires --release"
    validate_release_id "$RELEASE_ID"
    local directory
    local unit
    local unit_path
    local runtime_env
    local marker
    directory="$(canary_dir "$RELEASE_ID")"
    unit="$(canary_unit_name "$RELEASE_ID")"
    unit_path="$SYSTEMD_USER_DIR/$unit"
    runtime_env="$directory/canary.env"
    marker="$directory/PASSED.json"
    require_command "$SYSTEMCTL_BIN"
    require_command curl
    assert_secret_env_safe
    require_release "$RELEASE_ID"
    [[ -f "$PROD_DB" && ! -L "$PROD_DB" ]] \
        || die "production database is missing or unsafe: $PROD_DB"
    [[ "$CANARY_PORT" != "$PROD_PORT" ]] || die "canary port must differ from production"
    [[ ! -e "$directory" ]] || die "canary directory already exists; refusing overwrite: $directory"
    [[ ! -e "$unit_path" && ! -L "$unit_path" ]] \
        || die "canary unit already exists; refusing overwrite: $unit_path"

    if ((DRY_RUN)); then
        plan "read-only canary preflight passed for release/env/DB/port/unit paths"
        plan "create isolated canary directory (never reuse/overwrite): $directory"
        plan "online SQLite backup $PROD_DB -> $directory/echodesk.db"
        plan "install/start $unit on 127.0.0.1:$CANARY_PORT using release $RELEASE_ID"
        plan "verify /healthz, /readyz, and /proc/MainPID/cwd"
        plan "run public-isolation-smoke.py against canary; write PASSED.json only on success"
        return
    fi

    ensure_layout

    install -d -m 0700 "$directory"
    sqlite_backup "$PROD_DB" "$directory/echodesk.db"
    write_runtime_env \
        "$runtime_env" "$directory/data" "$directory/echodesk.db" "$CANARY_PORT" 1
    render_unit "$directory/$unit" "EchoDesk canary $RELEASE_ID" \
        "$(release_dir "$RELEASE_ID")" "$runtime_env" "$directory/data" \
        "$CANARY_PORT" "no"
    install -m 0600 "$directory/$unit" "$unit_path"
    systemctl --user daemon-reload

    local failed=0
    systemctl --user start "$unit" || failed=1
    if ((failed == 0)) && ! wait_http_ready "$CANARY_PORT"; then
        failed=1
    fi
    if ((failed == 0)) && ! verify_unit_release "$unit" "$RELEASE_ID"; then
        failed=1
    fi
    if ((failed == 0)) && ! run_isolation_smoke "$RELEASE_ID" "$CANARY_PORT"; then
        failed=1
    fi
    if ((failed)); then
        stop_canary "$RELEASE_ID"
        die "canary failed; preserved evidence at $directory"
    fi

    local digest
    digest="$(release_manifest_digest "$RELEASE_ID")"
    write_canary_marker "$RELEASE_ID" "$marker" "$digest" "$CANARY_PORT" "$PROD_DB"
    log "canary passed and remains active: $unit"
    log "canary evidence: $marker"
}

write_bootstrap_failure_evidence() {
    [[ -d "$BOOTSTRAP_BACKUP_DIR" ]] || return 0
    "$PYTHON_BIN" - \
        "$BOOTSTRAP_BACKUP_DIR/FAILED.json" "$BOOTSTRAP_RELEASE" "$BOOTSTRAP_PHASE" \
        "$BOOTSTRAP_PHASE_FILE" "$BOOTSTRAP_BACKUP_DIR/PREPARED_BINDINGS.json" \
        "$BOOTSTRAP_BACKUP_DIR/legacy/echodesk.db" <<'PY'
import datetime
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.exists():
    raise SystemExit(0)
payload = {
    "schema": 1,
    "deployment_kind": "bootstrap",
    "release_id": sys.argv[2],
    "failed_phase": sys.argv[3],
    "phase_file": sys.argv[4],
    "prepared_bindings": sys.argv[5],
    "stopped_service_snapshot": sys.argv[6],
    "snapshot_available": pathlib.Path(sys.argv[6]).is_file(),
    "failed_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "recovery": "fail_closed_ingress_closed_service_stopped_disabled_masked_legacy_not_started",
    "resume_command": "bootstrap-resume --deployment <id> with the exact recorded CLI bindings",
    "abort_command": "bootstrap-abort --deployment <id> with the exact recorded CLI bindings",
}
with path.open("x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
path.chmod(0o600)
PY
}

emergency_fail_closed_bootstrap() {
    warn "bootstrap failed after the production stop gate; enforcing fail-closed recovery"
    enforce_fail_closed || true
    persist_bootstrap_phase "failed" "fail_closed" || true
    write_bootstrap_failure_evidence || true
    if ! assert_fail_closed; then
        warn "fail-closed state could not be fully proven; operator intervention is required"
        return 1
    fi
    warn "production ingress is closed and service is stopped, disabled, and masked"
    warn "legacy data remains untouched; bootstrap evidence: $BOOTSTRAP_BACKUP_DIR"
}

bootstrap_common_arguments() {
    [[ -n "$RELEASE_ID" ]] || die "$COMMAND requires --release"
    [[ -n "$LEGACY_ENV_FILE" ]] || die "$COMMAND requires --legacy-env"
    [[ -n "$LEGACY_DB" ]] || die "$COMMAND requires --legacy-db"
    [[ -n "$LEGACY_DATA_ROOT" ]] || die "$COMMAND requires --legacy-data-root"
    validate_release_id "$RELEASE_ID"
    safe_absolute_path "legacy env file" "$LEGACY_ENV_FILE"
    safe_absolute_path "legacy database" "$LEGACY_DB"
    safe_absolute_path "legacy data root" "$LEGACY_DATA_ROOT"
    assert_ingress_gate_safe
    assert_public_url_safe
    require_command "$SYSTEMCTL_BIN"
    require_command "$SS_BIN"
    require_command curl
    require_command rsync
}

bootstrap_initial_readonly_preflight() {
    assert_secret_env_safe
    assert_secret_file_safe "$LEGACY_ENV_FILE" "legacy secret env file"
    require_release "$RELEASE_ID"
    assert_bootstrap_paths_safe
    verify_bootstrap_canary_gate "$RELEASE_ID"
    local canary_marker
    local canary_unit
    canary_marker="$(canary_dir "$RELEASE_ID")/PASSED.json"
    CANARY_PORT="$(json_get "$canary_marker" port)"
    validate_port "recorded canary port" "$CANARY_PORT"
    [[ "$CANARY_PORT" != "$PROD_PORT" ]] || die "recorded canary port matches production"
    canary_unit="$(canary_unit_name "$RELEASE_ID")"
    systemctl --user is-active --quiet "$canary_unit" \
        || die "passing canary is no longer active: $canary_unit"
    wait_http_ready "$CANARY_PORT" || die "canary health recheck failed"
    verify_unit_release "$canary_unit" "$RELEASE_ID" \
        || die "canary process is not target release"
    systemctl --user is-active --quiet "$SERVICE_NAME" \
        || die "legacy production service must be active before bootstrap"
    wait_http_health "$PROD_PORT" || die "legacy health check failed before bootstrap"
    require_ingress_state open
    assert_rsync_target_safe "$DATA_ROOT/storage"
    assert_rsync_target_safe "$DATA_ROOT/rag_index"
    assert_rsync_target_safe "$DATA_ROOT/user"
}

install_bootstrap_target_unit() {
    write_runtime_env "$PROD_RUNTIME_ENV" "$DATA_ROOT" "$PROD_DB" "$PROD_PORT" 0
    if [[ -L "$CURRENT_LINK" ]]; then
        [[ "$(current_release)" == "$RELEASE_ID" ]] \
            || die "current symlink does not match bootstrap release"
    else
        atomic_switch "$RELEASE_ID"
    fi
    local unit_tmp="$DEPLOY_ROOT/.unit-$SERVICE_NAME-$$"
    render_unit "$unit_tmp" "EchoDesk public backend" "$CURRENT_LINK" \
        "$PROD_RUNTIME_ENV" "$DATA_ROOT" "$PROD_PORT" "on-failure"
    install -m 0600 "$unit_tmp" "$PROD_UNIT_PATH.tmp.$$"
    mv -f -- "$PROD_UNIT_PATH.tmp.$$" "$PROD_UNIT_PATH"
    rm -f -- "$unit_tmp"
    systemctl --user daemon-reload
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    assert_service_start_disabled
}

activate_bootstrap_target() {
    local backup_dir="$1"
    local record="$2"
    install_bootstrap_target_unit
    persist_bootstrap_phase "target_installed_start_disabled" "running"
    systemctl --user start "$SERVICE_NAME"
    assert_service_start_disabled
    persist_bootstrap_phase "target_started_ingress_closed" "running"
    require_ingress_state closed
    wait_http_ready "$PROD_PORT" || die "bootstrap target health/readiness failed"
    verify_unit_release "$SERVICE_NAME" "$RELEASE_ID" \
        || die "bootstrap target process is not the immutable release"
    require_release "$RELEASE_ID"
    run_isolation_smoke "$RELEASE_ID" "$PROD_PORT" \
        || die "bootstrap target isolation smoke failed"
    persist_bootstrap_phase "target_validated_ingress_closed" "running"

    local success_bindings="$backup_dir/SUCCESS_BINDINGS.json"
    if [[ -f "$success_bindings" ]]; then
        validate_operation_bindings "$success_bindings" 0
    else
        write_operation_bindings "$success_bindings" "bootstrap-success" "$RELEASE_ID" 0
    fi
    set_ingress_state open open
    persist_bootstrap_phase "ingress_open_target_validated" "running"
    run_public_isolation_smoke "$RELEASE_ID"
    systemctl --user enable "$SERVICE_NAME"
    assert_service_active_enabled

    if [[ -f "$record" ]]; then
        [[ "$(json_get "$record" status)" == "active" ]] \
            || die "existing bootstrap record is not active"
        validate_active_deployment_record "$record"
    else
        write_bootstrap_record \
            "$record" "$DEPLOYMENT_ID" "$RELEASE_ID" "$backup_dir" "$success_bindings"
    fi
    atomic_latest_record "$record"
    persist_bootstrap_phase "complete" "active"
    stop_canary "$RELEASE_ID"
    BOOTSTRAP_RECOVERY_ARMED=0
    log "bootstrap complete: legacy -> $RELEASE_ID"
    log "fail-closed bootstrap rollback id: $DEPLOYMENT_ID"
}

cmd_bootstrap() {
    bootstrap_common_arguments
    bootstrap_initial_readonly_preflight
    if ((DRY_RUN)); then
        plan "read-only preflight passed: release/canary/paths/secrets/gate/service are present and bound"
        plan "pre-sync legacy storage, RAG, and filtered user data into $DATA_ROOT; never write legacy"
        plan "close dedicated ingress gate, stop+disable+mask $SERVICE_NAME, verify no $PROD_PORT listener"
        plan "immediately snapshot stopped legacy DB, then final-sync data and restore snapshot into $PROD_DB"
        plan "start target while disabled+ingress-closed; validate loopback health/ready/cwd/isolation"
        plan "open ingress only for the validated target, then enable service and write bound audit record"
        plan "on error: close ingress, stop+disable+mask, persist phase, expose resume/abort evidence"
        plan "on success: write an auditable fail-closed bootstrap rollback record"
        return
    fi
    acquire_lock
    bootstrap_initial_readonly_preflight

    DEPLOYMENT_ID="${DEPLOYMENT_ID:-bootstrap-$(date -u +%Y%m%dT%H%M%SZ)-$RELEASE_ID}"
    validate_release_id "$DEPLOYMENT_ID"
    local backup_dir="$BACKUPS_DIR/$DEPLOYMENT_ID"
    local record="$DEPLOYMENTS_DIR/$DEPLOYMENT_ID.json"
    [[ ! -e "$backup_dir" && ! -e "$record" ]] \
        || die "bootstrap deployment id already exists: $DEPLOYMENT_ID"
    install -d -m 0700 "$backup_dir/legacy"
    backup_configs "$backup_dir"
    install -m 0600 "$LEGACY_ENV_FILE" "$backup_dir/legacy/runtime.env"
    write_operation_bindings \
        "$backup_dir/PREPARED_BINDINGS.json" "bootstrap-prepared" "$RELEASE_ID" 1
    BOOTSTRAP_BACKUP_DIR="$backup_dir"
    BOOTSTRAP_RELEASE="$RELEASE_ID"
    BOOTSTRAP_PHASE_FILE="$backup_dir/PHASE.json"
    persist_bootstrap_phase "prepared" "running"

    sync_legacy_data "pre-stop"
    BOOTSTRAP_RECOVERY_ARMED=1
    set_ingress_state close closed
    persist_bootstrap_phase "ingress_closed" "running"
    enforce_fail_closed || die "could not prove stopped+disabled+masked fail-closed state"
    persist_bootstrap_phase "legacy_stopped_masked_no_listener" "running"
    sqlite_backup "$LEGACY_DB" "$backup_dir/legacy/echodesk.db"
    persist_bootstrap_phase "stopped_legacy_snapshot_created" "running"
    sync_legacy_data "final"
    sqlite_restore "$backup_dir/legacy/echodesk.db" "$PROD_DB"
    persist_bootstrap_phase "new_database_restored_from_stopped_snapshot" "running"
    activate_bootstrap_target "$backup_dir" "$record"
}

bootstrap_recovery_preflight() {
    local require_snapshot="$1"
    bootstrap_common_arguments
    [[ -n "$DEPLOYMENT_ID" ]] || die "$COMMAND requires --deployment"
    validate_release_id "$DEPLOYMENT_ID"
    local backup_dir="$BACKUPS_DIR/$DEPLOYMENT_ID"
    [[ -d "$backup_dir" && ! -L "$backup_dir" ]] \
        || die "bootstrap evidence directory missing or unsafe: $backup_dir"
    [[ -f "$backup_dir/PREPARED_BINDINGS.json" ]] \
        || die "bootstrap prepared bindings are missing"
    [[ -f "$backup_dir/PHASE.json" ]] || die "bootstrap phase journal is missing"
    if [[ "$require_snapshot" == "1" ]]; then
        [[ -f "$backup_dir/legacy/echodesk.db" ]] \
            || die "stopped-service SQLite snapshot is missing; resume is unsafe"
    fi
    assert_secret_env_safe
    assert_secret_file_safe "$LEGACY_ENV_FILE" "legacy secret env file"
    require_release "$RELEASE_ID"
    validate_operation_bindings "$backup_dir/PREPARED_BINDINGS.json" 1
    local gate_state
    gate_state="$(ingress_gate_status)"
    [[ "$gate_state" == "closed" || "$gate_state" == "open" ]] \
        || die "bootstrap recovery cannot prove ingress state"
    BOOTSTRAP_BACKUP_DIR="$backup_dir"
    BOOTSTRAP_RELEASE="$RELEASE_ID"
    BOOTSTRAP_PHASE_FILE="$backup_dir/PHASE.json"
}

cmd_bootstrap_resume() {
    bootstrap_recovery_preflight 1
    local backup_dir="$BACKUPS_DIR/$DEPLOYMENT_ID"
    local record="$DEPLOYMENTS_DIR/$DEPLOYMENT_ID.json"
    if ((DRY_RUN)); then
        plan "read-only resume preflight passed for stopped snapshot $backup_dir/legacy/echodesk.db"
        plan "final-sync stable legacy data, preserve any partial new DB, restore stopped snapshot"
        plan "start target disabled behind closed gate, revalidate, open gate, enable, record"
        return
    fi
    acquire_lock
    bootstrap_recovery_preflight 1
    BOOTSTRAP_RECOVERY_ARMED=1
    set_ingress_state close closed
    enforce_fail_closed || die "resume could not establish fail-closed state"
    persist_bootstrap_phase "resume_started_fail_closed" "running"
    sync_legacy_data "resume-final"
    if [[ -f "$PROD_DB" ]]; then
        local partial="$backup_dir/resume-$(date -u +%Y%m%dT%H%M%SZ)-production.db"
        sqlite_backup "$PROD_DB" "$partial"
    fi
    sqlite_restore "$backup_dir/legacy/echodesk.db" "$PROD_DB"
    persist_bootstrap_phase "resume_database_restored_from_stopped_snapshot" "running"
    activate_bootstrap_target "$backup_dir" "$record"
}

cmd_bootstrap_abort() {
    bootstrap_recovery_preflight 0
    local backup_dir="$BACKUPS_DIR/$DEPLOYMENT_ID"
    if ((DRY_RUN)); then
        plan "read-only abort preflight passed; keep ingress closed and service masked"
        plan "remove only a matching new current symlink; preserve legacy and all new evidence"
        return
    fi
    acquire_lock
    bootstrap_recovery_preflight 0
    set_ingress_state close closed
    enforce_fail_closed || die "could not prove fail-closed abort state"
    if [[ -L "$CURRENT_LINK" ]]; then
        remove_current_release "$RELEASE_ID"
    fi
    persist_bootstrap_phase "aborted" "fail_closed"
    "$PYTHON_BIN" - "$backup_dir/ABORTED.json" "$RELEASE_ID" <<'PY'
import datetime
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
with path.open("x", encoding="utf-8") as handle:
    json.dump({
        "schema": 1,
        "release_id": sys.argv[2],
        "status": "aborted_fail_closed",
        "at": datetime.datetime.now(datetime.UTC).isoformat(),
        "legacy_started": False,
        "legacy_overwritten": False,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
path.chmod(0o600)
PY
    log "bootstrap abort complete; ingress closed and legacy remains stopped+masked"
}

restore_promotion_backup() {
    local backup_dir="$1"
    local previous="$2"
    systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || return 1
    sqlite_restore "$backup_dir/echodesk.db" "$PROD_DB" || return 1
    restore_configs "$backup_dir" || return 1
    atomic_switch "$previous" || return 1
    systemctl --user daemon-reload || return 1
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    assert_service_start_disabled || return 1
    systemctl --user start "$SERVICE_NAME" || return 1
    if wait_http_ready "$PROD_PORT" \
        && verify_unit_release "$SERVICE_NAME" "$previous" \
        && release_integrity_ok "$previous" \
        && run_isolation_smoke "$previous" "$PROD_PORT"; then
        if ! set_ingress_state open open \
            || ! run_public_isolation_smoke "$previous" \
            || ! systemctl --user enable "$SERVICE_NAME" \
            || ! assert_service_active_enabled; then
            enforce_fail_closed || true
            return 1
        fi
        return 0
    fi
    enforce_fail_closed || true
    return 1
}

emergency_restore_promotion() {
    warn "unexpected error after production stop; attempting fail-safe recovery"
    set_ingress_state close closed >/dev/null 2>&1 || true
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    if ((PROMOTION_SWITCHED)); then
        if [[ -f "$PROMOTION_BACKUP_DIR/echodesk.db" ]]; then
            sqlite_restore "$PROMOTION_BACKUP_DIR/echodesk.db" "$PROD_DB" || true
            restore_configs "$PROMOTION_BACKUP_DIR" || true
        fi
        atomic_switch "$PROMOTION_PREVIOUS_RELEASE" || true
        systemctl --user daemon-reload || true
    fi
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl --user start "$SERVICE_NAME" || true
    if ! assert_service_start_disabled \
        || ! wait_http_ready "$PROD_PORT" \
        || ! verify_unit_release "$SERVICE_NAME" "$PROMOTION_PREVIOUS_RELEASE" \
        || ! release_integrity_ok "$PROMOTION_PREVIOUS_RELEASE" \
        || ! run_isolation_smoke "$PROMOTION_PREVIOUS_RELEASE" "$PROD_PORT" \
        || ! set_ingress_state open open \
        || ! run_public_isolation_smoke "$PROMOTION_PREVIOUS_RELEASE" \
        || ! systemctl --user enable "$SERVICE_NAME" \
        || ! assert_service_active_enabled; then
        warn "automatic recovery could not prove health + isolation; leaving production stopped"
        enforce_fail_closed || true
        return 1
    fi
    warn "automatic recovery restored the previous release"
}

emergency_restore_rollback() {
    warn "unexpected rollback error; attempting to restore the pre-rollback state"
    set_ingress_state close closed >/dev/null 2>&1 || true
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    if ((ROLLBACK_MUTATED)) && [[ -f "$ROLLBACK_SAFETY_DIR/echodesk.db" ]]; then
        sqlite_restore "$ROLLBACK_SAFETY_DIR/echodesk.db" "$PROD_DB" || true
        restore_configs "$ROLLBACK_SAFETY_DIR" || true
        atomic_switch "$ROLLBACK_CURRENT_RELEASE" || true
        systemctl --user daemon-reload || true
    fi
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl --user start "$SERVICE_NAME" || true
    if ! assert_service_start_disabled \
        || ! wait_http_ready "$PROD_PORT" \
        || ! verify_unit_release "$SERVICE_NAME" "$ROLLBACK_CURRENT_RELEASE" \
        || ! release_integrity_ok "$ROLLBACK_CURRENT_RELEASE" \
        || ! run_isolation_smoke "$ROLLBACK_CURRENT_RELEASE" "$PROD_PORT" \
        || ! set_ingress_state open open \
        || ! run_public_isolation_smoke "$ROLLBACK_CURRENT_RELEASE" \
        || ! systemctl --user enable "$SERVICE_NAME" \
        || ! assert_service_active_enabled; then
        warn "pre-rollback state could not be proven healthy and isolated; leaving production stopped"
        enforce_fail_closed || true
        return 1
    fi
    warn "pre-rollback state restored"
}

cmd_promote() {
    [[ -n "$RELEASE_ID" ]] || die "promote requires --release"
    validate_release_id "$RELEASE_ID"
    assert_ingress_gate_safe
    assert_public_url_safe
    require_command "$SYSTEMCTL_BIN"
    require_command "$SS_BIN"
    require_command curl
    assert_secret_env_safe
    require_release "$RELEASE_ID"
    verify_canary_gate "$RELEASE_ID"

    local canary_marker
    canary_marker="$(canary_dir "$RELEASE_ID")/PASSED.json"
    CANARY_PORT="$(json_get "$canary_marker" port)"
    validate_port "recorded canary port" "$CANARY_PORT"
    [[ "$CANARY_PORT" != "$PROD_PORT" ]] || die "recorded canary port matches production"

    local canary_unit
    canary_unit="$(canary_unit_name "$RELEASE_ID")"
    systemctl --user is-active --quiet "$canary_unit" \
        || die "passing canary is no longer active: $canary_unit"
    wait_http_ready "$CANARY_PORT" || die "canary health recheck failed"
    verify_unit_release "$canary_unit" "$RELEASE_ID" || die "canary process is not target release"

    local previous
    previous="$(current_release)" || die "current release is not initialized"
    require_release "$previous"
    [[ "$previous" != "$RELEASE_ID" ]] || die "release is already current: $RELEASE_ID"
    [[ -f "$PROD_DB" && ! -L "$PROD_DB" ]] || die "production database is missing or unsafe"
    [[ -f "$PROD_UNIT_PATH" && ! -L "$PROD_UNIT_PATH" ]] \
        || die "production unit is missing or unsafe"
    require_ingress_state open
    if ((DRY_RUN)); then
        plan "read-only preflight passed: canary/current/release/DB/env/unit/gate are present and immutable"
        plan "close ingress; disable+stop service; snapshot DB/config and persist phase"
        plan "start target while disabled behind closed gate; validate health/ready/cwd/isolation"
        plan "open ingress only after validation, then enable and write bound active record"
        plan "on failure restore the previous safe release behind closed gate, validate, then reopen+enable"
        return
    fi

    acquire_lock
    require_release "$RELEASE_ID"
    require_release "$previous"
    require_ingress_state open
    DEPLOYMENT_ID="${DEPLOYMENT_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$RELEASE_ID}"
    validate_release_id "$DEPLOYMENT_ID"
    local backup_dir="$BACKUPS_DIR/$DEPLOYMENT_ID"
    local record="$DEPLOYMENTS_DIR/$DEPLOYMENT_ID.json"
    [[ ! -e "$backup_dir" && ! -e "$record" ]] \
        || die "deployment id already exists; refusing overwrite: $DEPLOYMENT_ID"

    PROMOTION_BACKUP_DIR="$backup_dir"
    PROMOTION_PREVIOUS_RELEASE="$previous"
    PROMOTION_SWITCHED=0
    PROMOTION_RECOVERY_ARMED=1
    install -d -m 0700 "$backup_dir"
    backup_configs "$backup_dir"
    printf '%s\n' "$previous" >"$backup_dir/previous-release"
    chmod 0600 "$backup_dir/previous-release"
    persist_phase "$backup_dir/PHASE.json" "prepared" "running" "release"
    set_ingress_state close closed
    persist_phase "$backup_dir/PHASE.json" "ingress_closed" "running" "release"
    systemctl --user disable "$SERVICE_NAME"
    systemctl --user stop "$SERVICE_NAME"
    if systemctl --user is-active --quiet "$SERVICE_NAME" || port_has_listener; then
        die "production did not reach stopped/no-listener promotion gate"
    fi
    persist_phase "$backup_dir/PHASE.json" "service_stopped_start_disabled" "running" "release"
    sqlite_backup "$PROD_DB" "$backup_dir/echodesk.db"
    persist_phase "$backup_dir/PHASE.json" "database_snapshotted" "running" "release"

    atomic_switch "$RELEASE_ID"
    PROMOTION_SWITCHED=1
    assert_service_start_disabled
    local failed=0
    systemctl --user start "$SERVICE_NAME" || failed=1
    if ((failed == 0)) && ! assert_service_start_disabled; then
        failed=1
    fi
    if ((failed == 0)) && ! wait_http_ready "$PROD_PORT"; then
        failed=1
    fi
    if ((failed == 0)) && ! verify_unit_release "$SERVICE_NAME" "$RELEASE_ID"; then
        failed=1
    fi
    if ((failed == 0)) && ! release_integrity_ok "$RELEASE_ID"; then
        failed=1
    fi
    if ((failed == 0)) && ! run_isolation_smoke "$RELEASE_ID" "$PROD_PORT"; then
        failed=1
    fi

    if ((failed)); then
        warn "promotion validation failed; restoring pre-deploy snapshot"
        if restore_promotion_backup "$backup_dir" "$previous"; then
            PROMOTION_RECOVERY_ARMED=0
            die "promotion failed; automatic rollback succeeded"
        fi
        die "promotion failed; fail-safe recovery remains armed for $backup_dir"
    fi

    persist_phase "$backup_dir/PHASE.json" "target_validated_ingress_closed" "running" "release"
    local success_bindings="$backup_dir/SUCCESS_BINDINGS.json"
    write_operation_bindings "$success_bindings" "release-success" "$RELEASE_ID" 0
    set_ingress_state open open
    run_public_isolation_smoke "$RELEASE_ID"
    systemctl --user enable "$SERVICE_NAME"
    assert_service_active_enabled
    write_deployment_record \
        "$record" "$DEPLOYMENT_ID" "$RELEASE_ID" "$previous" "$backup_dir" "$success_bindings"
    atomic_latest_record "$record"
    persist_phase "$backup_dir/PHASE.json" "complete" "active" "release"
    stop_canary "$RELEASE_ID"
    PROMOTION_RECOVERY_ARMED=0
    log "promotion complete: $previous -> $RELEASE_ID"
    log "rollback id: $DEPLOYMENT_ID"
}

resolve_deployment_record() {
    if [[ -n "$DEPLOYMENT_ID" ]]; then
        validate_release_id "$DEPLOYMENT_ID"
        printf '%s/%s.json' "$DEPLOYMENTS_DIR" "$DEPLOYMENT_ID"
    else
        printf '%s/latest.json' "$DEPLOYMENTS_DIR"
    fi
}

cmd_rollback() {
    assert_public_url_safe
    require_command "$SYSTEMCTL_BIN"
    require_command "$SS_BIN"
    require_command curl
    assert_secret_env_safe
    local record
    record="$(resolve_deployment_record)"
    [[ -f "$record" ]] || die "deployment record not found: $record"
    local deployment_kind
    deployment_kind="$(json_get_optional "$record" deployment_kind)"
    local recorded_release
    recorded_release="$(json_get "$record" release_id)"
    if [[ -n "$RELEASE_ID" && "$RELEASE_ID" != "$recorded_release" ]]; then
        die "rollback --release does not match the active deployment record"
    fi
    RELEASE_ID="$recorded_release"
    validate_release_id "$RELEASE_ID"
    assert_ingress_gate_safe
    require_release "$RELEASE_ID"
    validate_active_deployment_record "$record"
    [[ "$(current_release)" == "$RELEASE_ID" ]] \
        || die "refusing rollback: current release does not match deployment record"
    require_ingress_state open
    if ((DRY_RUN)); then
        plan "read-only preflight passed: record is active, bound to this CLI, contained, and not replayed"
        plan "close ingress before disable/stop; snapshot current DB/config"
        if [[ "$deployment_kind" == "bootstrap" ]]; then
            plan "restore legacy unit evidence, then persistently mask it; verify inactive/masked/no listener"
        else
            plan "restore previous safe release; start disabled behind closed gate; validate, open, enable"
        fi
        plan "any failed inline recovery restores and re-enables only the already-safe current release"
        return
    fi
    acquire_lock
    validate_active_deployment_record "$record"
    require_ingress_state open
    if [[ "$deployment_kind" == "bootstrap" ]]; then
        cmd_rollback_bootstrap "$record"
        return
    fi
    local release
    local previous
    local backup_dir
    release="$RELEASE_ID"
    previous="$(json_get "$record" previous_release_id)"
    backup_dir="$(json_get "$record" backup_dir)"
    validate_release_id "$release"
    validate_release_id "$previous"
    require_release "$release"
    require_release "$previous"
    [[ -f "$backup_dir/echodesk.db" ]] || die "pre-deploy DB snapshot missing"

    local rollback_id="rollback-$(date -u +%Y%m%dT%H%M%SZ)-$release"
    local safety_dir="$BACKUPS_DIR/$rollback_id"
    [[ ! -e "$safety_dir" ]] || die "rollback safety snapshot already exists: $safety_dir"

    ROLLBACK_SAFETY_DIR="$safety_dir"
    ROLLBACK_CURRENT_RELEASE="$release"
    ROLLBACK_MUTATED=0
    ROLLBACK_RECOVERY_ARMED=1
    set_ingress_state close closed
    systemctl --user disable "$SERVICE_NAME"
    systemctl --user stop "$SERVICE_NAME"
    if systemctl --user is-active --quiet "$SERVICE_NAME" || port_has_listener; then
        die "rollback stop gate did not reach inactive/no-listener state"
    fi
    install -d -m 0700 "$safety_dir"
    sqlite_backup "$PROD_DB" "$safety_dir/echodesk.db"
    backup_configs "$safety_dir"

    sqlite_restore "$backup_dir/echodesk.db" "$PROD_DB"
    ROLLBACK_MUTATED=1
    restore_configs "$backup_dir"
    atomic_switch "$previous"
    systemctl --user daemon-reload
    systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    assert_service_start_disabled

    local failed=0
    systemctl --user start "$SERVICE_NAME" || failed=1
    if ((failed == 0)) && ! assert_service_start_disabled; then
        failed=1
    fi
    if ((failed == 0)) && ! wait_http_ready "$PROD_PORT"; then
        failed=1
    fi
    if ((failed == 0)) && ! verify_unit_release "$SERVICE_NAME" "$previous"; then
        failed=1
    fi
    require_release "$previous"
    if ((failed == 0)) && ! run_isolation_smoke "$previous" "$PROD_PORT"; then
        failed=1
    fi

    if ((failed)); then
        warn "rollback validation failed; restoring state captured immediately before rollback"
        systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
        sqlite_restore "$safety_dir/echodesk.db" "$PROD_DB"
        restore_configs "$safety_dir"
        atomic_switch "$release"
        systemctl --user daemon-reload
        systemctl --user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
        systemctl --user start "$SERVICE_NAME" || true
        if assert_service_start_disabled \
            && wait_http_ready "$PROD_PORT" \
            && verify_unit_release "$SERVICE_NAME" "$release" \
            && release_integrity_ok "$release" \
            && run_isolation_smoke "$release" "$PROD_PORT" \
            && set_ingress_state open open \
            && run_public_isolation_smoke "$release" \
            && systemctl --user enable "$SERVICE_NAME" \
            && assert_service_active_enabled; then
            ROLLBACK_RECOVERY_ARMED=0
            die "rollback failed; original release restored; evidence: $safety_dir"
        fi
        die "rollback failed; fail-safe recovery is still armed; evidence: $safety_dir"
    fi

    set_ingress_state open open
    run_public_isolation_smoke "$previous"
    systemctl --user enable "$SERVICE_NAME"
    assert_service_active_enabled
    mark_deployment_rolled_back "$record" "$rollback_id" "rolled_back"
    ROLLBACK_RECOVERY_ARMED=0
    log "rollback complete: $release -> $previous"
    log "post-deploy writes were replaced by the pre-deploy snapshot; safety copy: $safety_dir"
}

cmd_rollback_bootstrap() {
    local record="$1"
    local release
    local backup_dir
    release="$RELEASE_ID"
    backup_dir="$(json_get "$record" backup_dir)"
    validate_release_id "$release"
    require_release "$release"
    [[ -f "$backup_dir/config/$SERVICE_NAME" && ! -L "$backup_dir/config/$SERVICE_NAME" ]] \
        || die "legacy unit backup missing or unsafe: $backup_dir/config/$SERVICE_NAME"
    [[ -f "$backup_dir/legacy/echodesk.db" ]] \
        || die "legacy SQLite evidence missing: $backup_dir/legacy/echodesk.db"

    local rollback_id="rollback-$(date -u +%Y%m%dT%H%M%SZ)-$release"
    local safety_dir="$BACKUPS_DIR/$rollback_id"
    [[ ! -e "$safety_dir" ]] || die "rollback safety snapshot already exists: $safety_dir"
    ROLLBACK_SAFETY_DIR="$safety_dir"
    ROLLBACK_CURRENT_RELEASE="$release"
    ROLLBACK_MUTATED=0
    ROLLBACK_RECOVERY_ARMED=1
    set_ingress_state close closed
    systemctl --user disable "$SERVICE_NAME"
    systemctl --user stop "$SERVICE_NAME"
    if systemctl --user is-active --quiet "$SERVICE_NAME" || port_has_listener; then
        die "bootstrap rollback stop gate did not reach inactive/no-listener state"
    fi
    install -d -m 0700 "$safety_dir"
    sqlite_backup "$PROD_DB" "$safety_dir/echodesk.db"
    backup_configs "$safety_dir"
    ROLLBACK_MUTATED=1
    restore_configs "$backup_dir"
    remove_current_release "$release"
    enforce_fail_closed || die "bootstrap rollback could not prove legacy masked fail-closed state"
    assert_fail_closed || die "bootstrap rollback fail-closed verification failed"
    mark_deployment_rolled_back "$record" "$rollback_id" "rolled_back_fail_closed"
    ROLLBACK_RECOVERY_ARMED=0
    log "bootstrap rollback complete in fail-closed mode"
    log "legacy unit was restored but deliberately not started; safety copy: $safety_dir"
}

cmd_status() {
    local current="none"
    local service_state="unknown"
    local latest="none"
    current="$(current_release 2>/dev/null || printf 'none')"
    if command -v systemctl >/dev/null 2>&1; then
        service_state="$(systemctl --user is-active "$SERVICE_NAME" 2>/dev/null || printf 'inactive')"
    fi
    if [[ -f "$DEPLOYMENTS_DIR/latest.json" ]]; then
        latest="$(json_get "$DEPLOYMENTS_DIR/latest.json" deployment_id 2>/dev/null || printf 'invalid')"
    fi
    "$PYTHON_BIN" - "$DEPLOY_ROOT" "$SERVICE_NAME" "$current" "$service_state" "$latest" <<'PY'
import json
import sys

print(json.dumps({
    "deploy_root": sys.argv[1],
    "service": sys.argv[2],
    "current_release": sys.argv[3],
    "service_state": sys.argv[4],
    "latest_deployment": sys.argv[5],
}, indent=2, sort_keys=True))
PY
}

cmd_self_test() {
    require_command "$PYTHON_BIN"
    require_command rsync
    local original_root="$DEPLOY_ROOT"
    local original_env="$SECRET_ENV_FILE"
    local original_db="$PROD_DB"
    local original_unit="$PROD_UNIT_PATH"
    local original_legacy_env="$LEGACY_ENV_FILE"
    local original_legacy_db="$LEGACY_DB"
    local original_legacy_data_root="$LEGACY_DATA_ROOT"
    local original_ingress_gate="$INGRESS_GATE"
    local original_public_base_url="$PUBLIC_BASE_URL"
    local original_systemctl_bin="$SYSTEMCTL_BIN"
    local original_ss_bin="$SS_BIN"
    local original_release_id="$RELEASE_ID"
    local original_phase_file="$BOOTSTRAP_PHASE_FILE"
    local root
    root="$(mktemp -d "${TMPDIR:-/tmp}/echodesk-deploy-self-test.XXXXXX")"
    SELF_TEST_TMP="$root"

    DEPLOY_ROOT="$root/deploy"
    SECRET_ENV_FILE="$DEPLOY_ROOT/shared/runtime.env"
    PROD_DB="$DEPLOY_ROOT/shared/data/echodesk.db"
    init_paths
    SYSTEMD_USER_DIR="$root/xdg/systemd/user"
    PROD_UNIT_PATH="$SYSTEMD_USER_DIR/$SERVICE_NAME"
    ensure_layout

    local fake_state="$root/fake-state"
    install -d -m 0700 "$fake_state"
    printf 'active\n' >"$fake_state/active"
    printf 'enabled\n' >"$fake_state/enabled"
    printf '1\n' >"$fake_state/listener"
    printf 'open\n' >"$fake_state/gate"
    SYSTEMCTL_BIN="$root/fake-systemctl"
    SS_BIN="$root/fake-ss"
    INGRESS_GATE="$root/fake-ingress-gate"
    PUBLIC_BASE_URL="https://echodesk.example.invalid"
    export FAKE_SYSTEMD_STATE="$fake_state"
    export FAKE_UNIT_PATH="$PROD_UNIT_PATH"
    export FAKE_GATE_STATE="$fake_state/gate"
    "$PYTHON_BIN" - "$SYSTEMCTL_BIN" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.write_text("""#!/usr/bin/env bash
set -eu
[[ ${1:-} == --user ]] && shift
command=${1:-}; shift || true
state=$FAKE_SYSTEMD_STATE
case $command in
  is-active)
    [[ ${1:-} == --quiet ]] && shift
    [[ $(cat "$state/active") == active ]]
    ;;
  is-enabled)
    if [[ -L $FAKE_UNIT_PATH && $(readlink "$FAKE_UNIT_PATH") == /dev/null ]]; then
      printf 'masked\\n'
      exit 1
    fi
    value=$(cat "$state/enabled")
    printf '%s\\n' "$value"
    [[ $value == enabled ]]
    ;;
  stop)
    printf 'inactive\\n' >"$state/active"
    printf '0\\n' >"$state/listener"
    ;;
  disable)
    printf 'disabled\\n' >"$state/enabled"
    ;;
  enable)
    printf 'enabled\\n' >"$state/enabled"
    ;;
  start)
    [[ ! -L $FAKE_UNIT_PATH ]]
    printf 'active\\n' >"$state/active"
    printf '1\\n' >"$state/listener"
    ;;
  daemon-reload) ;;
  *) exit 2 ;;
esac
""", encoding="utf-8")
path.chmod(0o700)
PY
    "$PYTHON_BIN" - "$SS_BIN" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.write_text("""#!/usr/bin/env bash
set -eu
if [[ $(cat "$FAKE_SYSTEMD_STATE/listener") == 1 ]]; then
  printf 'LISTEN 0 128 127.0.0.1:8769 0.0.0.0:*\\n'
fi
""", encoding="utf-8")
path.chmod(0o700)
PY
    "$PYTHON_BIN" - "$INGRESS_GATE" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.write_text("""#!/usr/bin/env bash
set -eu
action=${1:?}; service=${2:?}; port=${3:?}
case $action in
  status) cat "$FAKE_GATE_STATE" ;;
  close) printf 'closed\\n' >"$FAKE_GATE_STATE" ;;
  open) printf 'open\\n' >"$FAKE_GATE_STATE" ;;
  *) exit 2 ;;
esac
""", encoding="utf-8")
path.chmod(0o700)
PY
    assert_ingress_gate_safe
    printf 'TEST_ONLY_SECRET=must-not-appear-in-unit\n' >"$SECRET_ENV_FILE"
    chmod 0600 "$SECRET_ENV_FILE"
    assert_secret_env_safe

    LEGACY_DATA_ROOT="$root/legacy-data"
    LEGACY_DB="$LEGACY_DATA_ROOT/echodesk.db"
    LEGACY_ENV_FILE="$root/legacy.env"
    install -d -m 0700 \
        "$LEGACY_DATA_ROOT/storage/meetings" "$LEGACY_DATA_ROOT/rag_index" \
        "$LEGACY_DATA_ROOT/logs"
    printf 'legacy-storage\n' >"$LEGACY_DATA_ROOT/storage/meetings/proof.txt"
    printf 'legacy-rag\n' >"$LEGACY_DATA_ROOT/rag_index/proof.json"
    printf 'legacy-user\n' >"$LEGACY_DATA_ROOT/preferences.json"
    printf 'must-not-copy\n' >"$LEGACY_DATA_ROOT/logs/backend.log"
    printf 'SELF_TEST_LEGACY_SECRET=must-not-appear-in-record\n' >"$LEGACY_ENV_FILE"
    chmod 0600 "$LEGACY_ENV_FILE"
    "$PYTHON_BIN" - "$LEGACY_DB" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("CREATE TABLE legacy_proof(value TEXT NOT NULL)")
connection.execute("INSERT INTO legacy_proof VALUES ('preserved')")
connection.commit()
connection.close()
PY
    assert_secret_file_safe "$LEGACY_ENV_FILE" "self-test legacy secret env file"
    local legacy_snapshot="$BACKUPS_DIR/self-test-legacy.db"
    local bootstrap_database="$root/bootstrap-production.db"
    sqlite_backup "$LEGACY_DB" "$legacy_snapshot"
    sqlite_restore "$legacy_snapshot" "$bootstrap_database"
    "$PYTHON_BIN" - "$bootstrap_database" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("UPDATE legacy_proof SET value = 'new-production-only'")
connection.commit()
connection.close()
PY
    [[ "$($PYTHON_BIN - "$LEGACY_DB" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(connection.execute("SELECT value FROM legacy_proof").fetchone()[0])
connection.close()
PY
)" == "preserved" ]] || die "self-test bootstrap mutated legacy database"

    local canary_marker="$CANARIES_DIR/candidate/PASSED.json"
    install -d -m 0700 "$CANARIES_DIR/candidate"
    write_canary_marker "candidate" "$canary_marker" "self-test-digest" 8870 "$LEGACY_DB"
    [[ "$(json_get "$canary_marker" source_database)" == \
        "$($PYTHON_BIN - "$LEGACY_DB" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).resolve(strict=True))
PY
)" ]] || die "self-test canary marker lost legacy database source"

    sync_legacy_data "self-test"
    [[ -f "$DATA_ROOT/storage/meetings/proof.txt" ]] \
        || die "self-test legacy storage sync failed"
    [[ -f "$DATA_ROOT/rag_index/proof.json" ]] \
        || die "self-test legacy RAG sync failed"
    [[ -f "$DATA_ROOT/user/preferences.json" ]] \
        || die "self-test legacy user sync failed"
    [[ ! -e "$DATA_ROOT/user/logs" && ! -e "$DATA_ROOT/user/echodesk.db" ]] \
        || die "self-test copied excluded legacy logs/database"

    install -d -m 0700 \
        "$RELEASES_DIR/baseline" "$RELEASES_DIR/candidate/backend/app" \
        "$RELEASES_DIR/candidate/scripts" "$root/source"
    printf '__version__ = "0.0.0-self-test"\n' \
        >"$RELEASES_DIR/candidate/backend/app/__init__.py"
    "$PYTHON_BIN" -m venv --copies "$RELEASES_DIR/candidate/backend/.venv"
    ln -s pyvenv.cfg "$RELEASES_DIR/candidate/backend/.venv/self-test-link"
    install -m 0755 "$REPO_ROOT/scripts/public-isolation-smoke.py" \
        "$RELEASES_DIR/candidate/scripts/public-isolation-smoke.py"
    install -m 0755 "$REPO_ROOT/scripts/echodesk-ingress-gate.py" \
        "$RELEASES_DIR/candidate/scripts/echodesk-ingress-gate.py"
    write_release_manifest \
        "$RELEASES_DIR/candidate" "candidate" "$root/source" "self-test" "false"
    seal_release_tree "$RELEASES_DIR/candidate"
    verify_release_readonly "$RELEASES_DIR/candidate"
    RELEASE_ID="candidate"
    require_release "$RELEASE_ID"
    [[ "$(json_get "$RELEASES_DIR/candidate/RELEASE.json" schema)" == "2" \
        && "$(json_get "$RELEASES_DIR/candidate/RELEASE.json" venv_tree_digest_schema)" == "1" ]] \
        || die "self-test release manifest schema does not bind the venv tree"
    local venv_tamper_target="$RELEASES_DIR/candidate/backend/.venv/pyvenv.cfg"
    local venv_tamper_backup="$root/venv-tamper-backup.json"
    "$PYTHON_BIN" - "$venv_tamper_target" "$venv_tamper_backup" <<'PY'
import base64
import json
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
backup = pathlib.Path(sys.argv[2])
mode = stat.S_IMODE(path.stat().st_mode)
original = path.read_bytes()
backup.write_text(
    json.dumps({"mode": mode, "content": base64.b64encode(original).decode("ascii")}),
    encoding="utf-8",
)
path.chmod(mode | stat.S_IWUSR)
path.write_bytes(original + b"\n# self-test non-RECORD venv tamper\n")
path.chmod(mode)
PY
    if release_integrity_ok "$RELEASE_ID"; then
        die "self-test accepted non-RECORD venv content tampering"
    fi
    "$PYTHON_BIN" - "$venv_tamper_target" "$venv_tamper_backup" <<'PY'
import base64
import json
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
mode = int(payload["mode"])
path.chmod(mode | stat.S_IWUSR)
path.write_bytes(base64.b64decode(payload["content"], validate=True))
path.chmod(mode)
PY
    require_release "$RELEASE_ID"

    local venv_mode_target="$RELEASES_DIR/candidate/backend/.venv/bin/activate"
    local venv_mode_backup="$root/venv-mode-backup.json"
    "$PYTHON_BIN" - "$venv_mode_target" "$venv_mode_backup" <<'PY'
import json
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
backup = pathlib.Path(sys.argv[2])
mode = stat.S_IMODE(path.lstat().st_mode)
backup.write_text(json.dumps({"mode": mode}), encoding="utf-8")
path.chmod(mode ^ stat.S_IXUSR)
PY
    if release_integrity_ok "$RELEASE_ID"; then
        die "self-test accepted stable venv mode tampering"
    fi
    "$PYTHON_BIN" - "$venv_mode_target" "$venv_mode_backup" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
path.chmod(int(payload["mode"]))
PY
    require_release "$RELEASE_ID"

    local venv_link_target="$RELEASES_DIR/candidate/backend/.venv/self-test-link"
    local venv_link_backup="$root/venv-link-backup.json"
    "$PYTHON_BIN" - "$venv_link_target" "$venv_link_backup" <<'PY'
import json
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
backup = pathlib.Path(sys.argv[2])
parent = path.parent
parent_mode = stat.S_IMODE(parent.lstat().st_mode)
original_target = os.readlink(path)
backup.write_text(
    json.dumps({"parent_mode": parent_mode, "target": original_target}),
    encoding="utf-8",
)
parent.chmod(parent_mode | stat.S_IWUSR)
try:
    path.unlink()
    path.symlink_to("bin/activate")
finally:
    parent.chmod(parent_mode)
PY
    if release_integrity_ok "$RELEASE_ID"; then
        die "self-test accepted venv symlink target tampering"
    fi
    "$PYTHON_BIN" - "$venv_link_target" "$venv_link_backup" <<'PY'
import json
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
parent = path.parent
parent_mode = int(payload["parent_mode"])
parent.chmod(parent_mode | stat.S_IWUSR)
try:
    path.unlink()
    path.symlink_to(str(payload["target"]))
finally:
    parent.chmod(parent_mode)
PY
    require_release "$RELEASE_ID"

    local venv_extra_target="$RELEASES_DIR/candidate/backend/.venv/self-test-extra.txt"
    local venv_extra_backup="$root/venv-extra-backup.json"
    "$PYTHON_BIN" - "$venv_extra_target" "$venv_extra_backup" <<'PY'
import json
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
backup = pathlib.Path(sys.argv[2])
parent = path.parent
parent_mode = stat.S_IMODE(parent.lstat().st_mode)
backup.write_text(json.dumps({"parent_mode": parent_mode}), encoding="utf-8")
parent.chmod(parent_mode | stat.S_IWUSR)
try:
    with path.open("xb") as handle:
        handle.write(b"self-test unmanifested venv file\n")
    path.chmod(0o444)
finally:
    parent.chmod(parent_mode)
PY
    if release_integrity_ok "$RELEASE_ID"; then
        die "self-test accepted an extra stable venv file"
    fi
    "$PYTHON_BIN" - "$venv_extra_target" "$venv_extra_backup" <<'PY'
import json
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
parent = path.parent
parent_mode = int(payload["parent_mode"])
parent.chmod(parent_mode | stat.S_IWUSR)
try:
    path.unlink()
finally:
    parent.chmod(parent_mode)
PY
    require_release "$RELEASE_ID"

    local fake_ingress_gate="$INGRESS_GATE"
    INGRESS_GATE=""
    assert_ingress_gate_safe
    [[ "$INGRESS_GATE" == \
        "$RELEASES_DIR/candidate/scripts/echodesk-ingress-gate.py" ]] \
        || die "self-test default ingress gate did not resolve inside candidate release"
    INGRESS_GATE="$fake_ingress_gate"
    atomic_switch baseline
    [[ "$(current_release)" == "baseline" ]] || die "self-test atomic baseline switch failed"
    atomic_switch candidate
    [[ "$(current_release)" == "candidate" ]] || die "self-test atomic candidate switch failed"

    "$PYTHON_BIN" - "$PROD_DB" <<'PY'
import pathlib
import sqlite3
import sys

path = pathlib.Path(sys.argv[1])
conn = sqlite3.connect(path)
conn.execute("CREATE TABLE proof(value TEXT NOT NULL)")
conn.execute("INSERT INTO proof VALUES ('before')")
conn.commit()
conn.close()
PY
    sqlite_backup "$PROD_DB" "$BACKUPS_DIR/self-test.db"
    "$PYTHON_BIN" - "$PROD_DB" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("UPDATE proof SET value = 'after'")
conn.commit()
conn.close()
PY
    sqlite_restore "$BACKUPS_DIR/self-test.db" "$PROD_DB"
    [[ "$("$PYTHON_BIN" - "$PROD_DB" <<'PY'
import sqlite3
import sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT value FROM proof").fetchone()[0])
conn.close()
PY
)" == "before" ]] || die "self-test SQLite restore failed"

    write_runtime_env "$PROD_RUNTIME_ENV" "$DATA_ROOT" "$PROD_DB" "$PROD_PORT" 1
    render_unit "$root/test.service" "EchoDesk self-test" "$RELEASES_DIR/candidate" \
        "$PROD_RUNTIME_ENV" "$DATA_ROOT" "$PROD_PORT" "no"
    if grep -q 'must-not-appear-in-unit' "$root/test.service"; then
        die "self-test secret leaked into rendered unit"
    fi
    grep -q "EnvironmentFile=$SECRET_ENV_FILE" "$root/test.service" \
        || die "self-test unit does not reference secret env by path"
    install -m 0600 "$root/test.service" "$PROD_UNIT_PATH"

    local unsafe_target="$DATA_ROOT/unsafe-target"
    ln -s /tmp "$unsafe_target"
    if (assert_rsync_target_safe "$unsafe_target") >/dev/null 2>&1; then
        die "self-test accepted a symlinked rsync target"
    fi
    rm -f -- "$unsafe_target"

    local bootstrap_record="$DEPLOYMENTS_DIR/bootstrap-self-test.json"
    local bootstrap_backup="$BACKUPS_DIR/bootstrap-self-test"
    install -d -m 0700 "$bootstrap_backup/legacy" "$DEPLOYMENTS_DIR"
    sqlite_backup "$LEGACY_DB" "$bootstrap_backup/legacy/echodesk.db"
    write_operation_bindings \
        "$bootstrap_backup/SUCCESS_BINDINGS.json" "bootstrap-success" "candidate" 0
    write_bootstrap_record \
        "$bootstrap_record" "bootstrap-self-test" "candidate" \
        "$bootstrap_backup" "$bootstrap_backup/SUCCESS_BINDINGS.json"
    validate_active_deployment_record "$bootstrap_record"
    [[ "$(json_get "$bootstrap_record" deployment_kind)" == "bootstrap" ]] \
        || die "self-test bootstrap record kind failed"
    [[ "$(json_get "$bootstrap_record" rollback_policy)" == \
        "fail_closed_never_start_non_isolated_legacy" ]] \
        || die "self-test bootstrap rollback policy failed"
    if grep -q 'must-not-appear-in-record' "$bootstrap_record"; then
        die "self-test secret leaked into bootstrap record"
    fi
    local original_port="$PROD_PORT"
    PROD_PORT=$((PROD_PORT + 1))
    if (validate_active_deployment_record "$bootstrap_record") >/dev/null 2>&1; then
        die "self-test accepted deployment record with wrong CLI port binding"
    fi
    PROD_PORT="$original_port"
    mark_deployment_rolled_back "$bootstrap_record" "self-test-replay" "rolled_back"
    if (validate_active_deployment_record "$bootstrap_record") >/dev/null 2>&1; then
        die "self-test accepted a replayed deployment record"
    fi

    local release_backup="$BACKUPS_DIR/release-self-test"
    local release_record="$DEPLOYMENTS_DIR/release-self-test.json"
    install -d -m 0700 "$release_backup"
    sqlite_backup "$PROD_DB" "$release_backup/echodesk.db"
    write_operation_bindings \
        "$release_backup/SUCCESS_BINDINGS.json" "release-success" "candidate" 0
    write_deployment_record \
        "$release_record" "release-self-test" "candidate" "baseline" \
        "$release_backup" "$release_backup/SUCCESS_BINDINGS.json"
    validate_active_deployment_record "$release_record"

    BOOTSTRAP_BACKUP_DIR="$BACKUPS_DIR/bootstrap-failure-self-test"
    BOOTSTRAP_RELEASE="candidate"
    BOOTSTRAP_PHASE_FILE="$BOOTSTRAP_BACKUP_DIR/PHASE.json"
    install -d -m 0700 "$BOOTSTRAP_BACKUP_DIR/legacy"
    sqlite_backup "$LEGACY_DB" "$BOOTSTRAP_BACKUP_DIR/legacy/echodesk.db"
    write_operation_bindings \
        "$BOOTSTRAP_BACKUP_DIR/PREPARED_BINDINGS.json" "bootstrap-prepared" "candidate" 0
    persist_bootstrap_phase "target_started_ingress_closed" "running"
    [[ "$(json_get "$BOOTSTRAP_PHASE_FILE" current_phase)" == \
        "target_started_ingress_closed" ]] \
        || die "self-test power-loss phase journal failed"
    set_ingress_state close closed
    enforce_fail_closed || die "self-test fail-closed state transition failed"
    assert_fail_closed || die "self-test could not verify inactive/masked/no-listener state"
    validate_operation_bindings "$BOOTSTRAP_BACKUP_DIR/PREPARED_BINDINGS.json" 1
    write_bootstrap_failure_evidence
    [[ "$(json_get "$BOOTSTRAP_BACKUP_DIR/FAILED.json" recovery)" == \
        "fail_closed_ingress_closed_service_stopped_disabled_masked_legacy_not_started" ]] \
        || die "self-test bootstrap fail-closed evidence failed"

    local smoke_python="$REPO_ROOT/backend/.venv/bin/python"
    if [[ ! -x "$smoke_python" ]]; then
        smoke_python="$PYTHON_BIN"
    fi
    "$smoke_python" "$REPO_ROOT/scripts/public-isolation-smoke.py" --self-test >/dev/null

    DEPLOY_ROOT="$original_root"
    SECRET_ENV_FILE="$original_env"
    PROD_DB="$original_db"
    PROD_UNIT_PATH="$original_unit"
    LEGACY_ENV_FILE="$original_legacy_env"
    LEGACY_DB="$original_legacy_db"
    LEGACY_DATA_ROOT="$original_legacy_data_root"
    INGRESS_GATE="$original_ingress_gate"
    PUBLIC_BASE_URL="$original_public_base_url"
    SYSTEMCTL_BIN="$original_systemctl_bin"
    SS_BIN="$original_ss_bin"
    RELEASE_ID="$original_release_id"
    BOOTSTRAP_PHASE_FILE="$original_phase_file"
    unset FAKE_SYSTEMD_STATE FAKE_UNIT_PATH FAKE_GATE_STATE
    chmod -R u+w "$root"
    rm -rf -- "$root"
    SELF_TEST_TMP=""
    log "self-test passed (no systemd or network mutation)"
}

parse_args() {
    while (($#)); do
        case "$1" in
            prepare|install-unit|canary|bootstrap|bootstrap-resume|bootstrap-abort|promote|rollback|status|self-test)
                [[ -z "$COMMAND" ]] || die "only one command may be specified"
                COMMAND="$1"
                shift
                ;;
            --root)
                (($# >= 2)) || die "--root requires a value"
                DEPLOY_ROOT="$2"
                shift 2
                ;;
            --service)
                (($# >= 2)) || die "--service requires a value"
                SERVICE_NAME="$2"
                shift 2
                ;;
            --env-file)
                (($# >= 2)) || die "--env-file requires a value"
                SECRET_ENV_FILE="$2"
                shift 2
                ;;
            --db)
                (($# >= 2)) || die "--db requires a value"
                PROD_DB="$2"
                shift 2
                ;;
            --prod-port)
                (($# >= 2)) || die "--prod-port requires a value"
                PROD_PORT="$2"
                shift 2
                ;;
            --canary-port)
                (($# >= 2)) || die "--canary-port requires a value"
                CANARY_PORT="$2"
                shift 2
                ;;
            --health-timeout)
                (($# >= 2)) || die "--health-timeout requires a value"
                HEALTH_TIMEOUT_S="$2"
                shift 2
                ;;
            --release)
                (($# >= 2)) || die "--release requires a value"
                RELEASE_ID="$2"
                shift 2
                ;;
            --deployment)
                (($# >= 2)) || die "--deployment requires a value"
                DEPLOYMENT_ID="$2"
                shift 2
                ;;
            --legacy-env)
                (($# >= 2)) || die "--legacy-env requires a value"
                LEGACY_ENV_FILE="$2"
                shift 2
                ;;
            --legacy-db)
                (($# >= 2)) || die "--legacy-db requires a value"
                LEGACY_DB="$2"
                shift 2
                ;;
            --legacy-data-root)
                (($# >= 2)) || die "--legacy-data-root requires a value"
                LEGACY_DATA_ROOT="$2"
                shift 2
                ;;
            --ingress-gate)
                (($# >= 2)) || die "--ingress-gate requires a value"
                INGRESS_GATE="$2"
                shift 2
                ;;
            --public-url)
                (($# >= 2)) || die "--public-url requires a value"
                PUBLIC_BASE_URL="$2"
                shift 2
                ;;
            --source)
                (($# >= 2)) || die "--source requires a value"
                SOURCE_ROOT="$2"
                shift 2
                ;;
            --python)
                (($# >= 2)) || die "--python requires a value"
                PYTHON_BIN="$2"
                shift 2
                ;;
            --allow-dirty)
                ALLOW_DIRTY=1
                shift
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done
}

main() {
    parse_args "$@"
    [[ -n "$COMMAND" ]] || {
        usage >&2
        exit 2
    }
    validate_service_name "$SERVICE_NAME"
    validate_port "production port" "$PROD_PORT"
    validate_port "canary port" "$CANARY_PORT"
    validate_timeout "$HEALTH_TIMEOUT_S"
    [[ "$PROD_PORT" != "$CANARY_PORT" ]] || die "production and canary ports must differ"
    init_paths
    require_command "$PYTHON_BIN"
    case "$COMMAND" in
        prepare|install-unit|canary|bootstrap|bootstrap-resume|bootstrap-abort|promote|rollback|self-test)
            require_python_311
            ;;
    esac

    case "$COMMAND" in
        prepare) cmd_prepare ;;
        install-unit) cmd_install_unit ;;
        canary) cmd_canary ;;
        bootstrap) cmd_bootstrap ;;
        bootstrap-resume) cmd_bootstrap_resume ;;
        bootstrap-abort) cmd_bootstrap_abort ;;
        promote) cmd_promote ;;
        rollback) cmd_rollback ;;
        status) cmd_status ;;
        self-test) cmd_self_test ;;
        *) die "unreachable command: $COMMAND" ;;
    esac
}

main "$@"
