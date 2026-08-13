#!/bin/bash
set -euo pipefail
export LC_ALL=C

# A/B benchmark:
#   set9: librpm из Sisyphus с reimplement/set9.c и исходными pkglist Sisyphus;
#   d1:   librpm из того же commit с direct_hash/hash_set.c и теми же pkglist,
#         заранее преобразованными run_sisyphus_pkglist.py в формат set:D1.
#
# Все измеряемые команды работают без сети. Конвертация, сборка и gencaches
# выполняются до таймера. Порядок замеров в каждом раунде: set9/d1/d1/set9.

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SET9_C=${SET9_C:-$REPO_ROOT/reimplement/set9.c}
D1_C=${D1_C:-$REPO_ROOT/new_version/direct_hash/hash_set.c}
PKGLIST_CONVERTER=${PKGLIST_CONVERTER:-$REPO_ROOT/new_version/direct_hash/apt_benchmark/run_sisyphus_pkglist.py}

WORK_ROOT=${WORK_ROOT:-$HOME/sisyphus-set9-d1-bench}
RESULT_DIR=${RESULT_DIR:-$WORK_ROOT/results}
IMAGE=${IMAGE:-registry.altlinux.org/sisyphus/alt:latest}
RPM_GIT=${RPM_GIT:-https://git.altlinux.org/gears/r/rpm.git}
RPM_BRANCH=${RPM_BRANCH:-sisyphus}
PACKAGER=${PACKAGER:-krosh <gudovdo@my.msu.ru>}
CPU=${CPU:-0}
ROUNDS=${ROUNDS:-2}
RESET_WORK=${RESET_WORK:-1}  # 0 — продолжить подготовку/сборки, 1 — начать заново.
OPERATIONS=${OPERATIONS:-unmet install-rpm-build install-openuds-server install-password-store}
APT_GET=${APT_GET:-/usr/lib/apt/apt-get}
APT_CACHE=${APT_CACHE:-$(command -v apt-cache 2>/dev/null || true)}

usage()
{
    cat <<'EOF'
Usage: scripts/run-sisyphus-set9-d1-bench.sh

The script has no positional arguments. Configuration is passed through env:
  CPU=2 ROUNDS=3 RESET_WORK=1 ./scripts/run-sisyphus-set9-d1-bench.sh
  RESET_WORK=0 OPERATIONS='unmet check' ./scripts/run-sisyphus-set9-d1-bench.sh

Main variables:
  WORK_ROOT, RESULT_DIR, IMAGE, RPM_GIT, RPM_BRANCH, PACKAGER,
  SET9_C, D1_C, PKGLIST_CONVERTER, CPU, ROUNDS, RESET_WORK, OPERATIONS.

RESET_WORK=1 removes WORK_ROOT after running hsh --cleanup-only for old hasher
workdirs. Timed runs never update repositories and never install packages.
EOF
}

fail()
{
    printf 'error: %s\n' "$*" >&2
    exit 1
}

safe_remove_work_root()
{
    local marker="$WORK_ROOT/.arsv-sisyphus-set-bench-root"

    [[ -n $WORK_ROOT && $WORK_ROOT == /* && $WORK_ROOT != / &&
       $WORK_ROOT != "$HOME_REAL" && $HOME_REAL != "$WORK_ROOT/"* ]] ||
        fail "unsafe WORK_ROOT for removal: $WORK_ROOT"
    [[ $WORK_ROOT != "$REPO_ROOT" && $REPO_ROOT != "$WORK_ROOT/"* &&
       $WORK_ROOT != "$REPO_ROOT/"* && $WORK_ROOT != "$CWD_REAL" &&
       $CWD_REAL != "$WORK_ROOT/"* && $WORK_ROOT != "$CWD_REAL/"* ]] ||
        fail "WORK_ROOT overlaps the source repository or current directory: $WORK_ROOT"
    [[ ! -e $WORK_ROOT || (-f $marker && ! -L $marker) ]] ||
        fail "refusing to remove unmarked WORK_ROOT: $WORK_ROOT"
    if [[ -f $marker ]]; then
        grep -Fx 'ARSV Sisyphus set9/D1 benchmark work root' "$marker" >/dev/null ||
            fail "invalid WORK_ROOT ownership marker: $marker"
    fi
}

write_snapshot_fingerprint()
{
    {
        printf 'image=%s\n' "$IMAGE"
        printf 'image_digest=%s\n' "$(podman image inspect "$IMAGE" --format '{{.Digest}}')"
        printf 'image_id=%s\n' "$(podman image inspect "$IMAGE" --format '{{.Id}}')"
        printf 'converter=%s\n' "$(sha256sum "$PKGLIST_CONVERTER" | awk '{print $1}')"
        printf 'set9=%s\n' "$(sha256sum "$SET9_C" | awk '{print $1}')"
        printf 'rewrite=%s\n' "$(sha256sum "$REPO_ROOT/new_version/direct_hash/apt_benchmark/rewrite_sisyphus_pkglist.c" | awk '{print $1}')"
        printf 'compat=%s\n' "$(sha256sum "$REPO_ROOT/scripts/rpmsetcmp/newset_compat.h" | awk '{print $1}')"
    }
}

validate_snapshot_reuse()
{
    local current
    [[ -f $SNAPSHOT/.complete && -f $SNAPSHOT/input-fingerprint.txt ]] || return 1
    current=$(mktemp)
    write_snapshot_fingerprint >"$current"
    if ! cmp -s "$current" "$SNAPSHOT/input-fingerprint.txt"; then
        rm -f "$current"
        fail 'snapshot inputs changed; use RESET_WORK=1'
    fi
    rm -f "$current"
    return 0
}

write_source_fingerprint()
{
    {
        printf 'rpm_git=%s\n' "$RPM_GIT"
        printf 'rpm_branch=%s\n' "$RPM_BRANCH"
        printf 'rpm_commit=%s\n' "$(git -C "$SOURCE_BASE" rev-parse HEAD)"
    }
}

validate_source_reuse()
{
    local current
    [[ -f $SOURCE_FINGERPRINT ]] ||
        fail 'source fingerprint missing; use RESET_WORK=1'
    [[ -z $(git -C "$SOURCE_BASE" status --porcelain) ]] ||
        fail 'RPM base source has local changes; use RESET_WORK=1'
    current=$(mktemp)
    write_source_fingerprint >"$current"
    if ! cmp -s "$current" "$SOURCE_FINGERPRINT"; then
        rm -f "$current"
        fail 'RPM source URL, branch, or commit changed; use RESET_WORK=1'
    fi
    rm -f "$current"
}

prepare_spec()
{
    local spec=$1 suffix=$2 version release new_release date

    version=$(sed -n 's/^Version:[[:space:]]*//p' "$spec" | sed -n '1p')
    release=$(sed -n 's/^Release:[[:space:]]*//p' "$spec" | sed -n '1p')
    [[ -n $version && -n $release ]] || fail "cannot read Version/Release from $spec"

    new_release="$release.$suffix"
    sed -i "0,/^Release:[[:space:]]*$release$/s//Release: $new_release/" "$spec"

    date=$(date '+%a %b %d %Y')
    sed -i "/^%changelog/a\\
* $date $PACKAGER $version-$new_release\\
- Local Sisyphus set format benchmark build.\\
" "$spec"
}

apt_options()
{
    local variant=$1
    APT_OPTIONS=(
        -o 'Dir::Etc::main=-'
        -o 'Dir::Etc::parts=-'
        -o "Dir::Etc::sourcelist=$SNAPSHOT/etc-apt/sources.list"
        -o "Dir::Etc::sourceparts=$SNAPSHOT/etc-apt/sources.list.d"
        -o 'Dir::Etc::preferences=-'
        -o 'Dir::Etc::preferencesparts=-'
        -o "Dir::State::lists=$variant/apt/lists/"
        -o "Dir::State::status=$COMMON/status"
        -o "Dir::Cache=$variant/apt/cache/"
        -o "Dir::Cache::archives=$variant/apt/cache/archives"
        -o "Dir::Cache::pkgcache=$variant/apt/cache/pkgcache.bin"
        -o "Dir::Cache::srcpkgcache=$variant/apt/cache/srcpkgcache.bin"
        -o "RPM::RootDir=$COMMON/root"
    )
}

operation_command()
{
    local operation=$1 variant=$2
    apt_options "$variant"

    case $operation in
        unmet)
            COMMAND=("$APT_CACHE" -q "${APT_OPTIONS[@]}" unmet)
            ;;
        check)
            COMMAND=("$APT_GET" -qq "${APT_OPTIONS[@]}" -s check)
            ;;
        autoremove)
            COMMAND=("$APT_GET" -qq "${APT_OPTIONS[@]}" -s autoremove)
            ;;
        install-rpm-build)
            COMMAND=("$APT_GET" -qq "${APT_OPTIONS[@]}" -s install rpm-build)
            ;;
        install-openuds-server)
            COMMAND=("$APT_GET" -qq "${APT_OPTIONS[@]}" -s install openuds-server)
            ;;
        install-password-store)
            COMMAND=("$APT_GET" -qq "${APT_OPTIONS[@]}" -s install password-store)
            ;;
        upgrade)
            COMMAND=("$APT_GET" -qq "${APT_OPTIONS[@]}" -o APT::Get::EnableUpgrade=true -s upgrade)
            ;;
        dist-upgrade)
            COMMAND=("$APT_GET" -qq "${APT_OPTIONS[@]}" -s dist-upgrade)
            ;;
        *)
            fail "unknown operation: $operation"
            ;;
    esac
}

variant_dir()
{
    case $1 in
        set9) printf '%s' "$SET9_VARIANT" ;;
        d1) printf '%s' "$D1_VARIANT" ;;
        *) fail "unknown variant: $1" ;;
    esac
}

variant_libdir()
{
    local variant
    variant=$(variant_dir "$1")
    printf '%s' "$variant/lib/usr/lib64"
}

prepare_command()
{
    local operation=$1 variant_name=$2 variant
    variant=$(variant_dir "$variant_name")
    RUN_LIBDIR=$(variant_libdir "$variant_name")
    operation_command "$operation" "$variant"
}

execute_command()
{
    local stdout=$1 stderr=$2 status

    if env LC_ALL=C APT_CONFIG="$COMMON/apt.conf" LD_LIBRARY_PATH="$RUN_LIBDIR" \
        taskset -c "$CPU" "${COMMAND[@]}" >"$stdout" 2>"$stderr"; then
        status=0
    else
        status=$?
    fi
    printf '%s' "$status"
}

run_once()
{
    local operation=$1 variant_name=$2 sequence=$3 sample=$4
    local variant raw stdout stderr start end status elapsed
    local stdout_sha stderr_sha stdout_bytes stderr_bytes
    variant=$(variant_dir "$variant_name")
    raw="$RESULT_DIR/raw/$operation/$variant_name"
    mkdir -p "$raw"
    stdout="$raw/$sample.stdout"
    stderr="$raw/$sample.stderr"

    prepare_command "$operation" "$variant_name"
    start=$(date +%s%N)
    status=$(execute_command "$stdout" "$stderr")
    end=$(date +%s%N)
    elapsed=$(awk -v start="$start" -v end="$end" \
        'BEGIN { printf "%.6f", (end - start) / 1000000000 }')

    stdout_sha=$(sha256sum "$stdout" | awk '{print $1}')
    stderr_sha=$(sha256sum "$stderr" | awk '{print $1}')
    stdout_bytes=$(stat -c %s "$stdout")
    stderr_bytes=$(stat -c %s "$stderr")
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$operation" "$sequence" "$variant_name" "$sample" "$elapsed" "$status" \
        "$stdout_sha" "$stdout_bytes" "$stderr_sha" "$stderr_bytes" >>"$RAW_RESULTS"
    printf '%-28s sequence=%-2s %-4s sample=%-2s %ss status=%s\n' \
        "$operation" "$sequence" "$variant_name" "$sample" "$elapsed" "$status"
}

prepare_snapshot()
{
    local image_digest image_id converter_rel
    validate_snapshot_reuse && return

    case $PKGLIST_CONVERTER in
        "$REPO_ROOT"/*) converter_rel=${PKGLIST_CONVERTER#"$REPO_ROOT/"} ;;
        *) fail "PKGLIST_CONVERTER must be inside REPO_ROOT: $PKGLIST_CONVERTER" ;;
    esac

    printf '\n===== Preparing one immutable Sisyphus metadata snapshot =====\n'
    rm -rf "$SNAPSHOT"
    mkdir -p "$SNAPSHOT/container-output"
    podman pull "$IMAGE"
    image_digest=$(podman image inspect "$IMAGE" --format '{{.Digest}}')
    image_id=$(podman image inspect "$IMAGE" --format '{{.Id}}')

    podman run --rm \
        -e "ARSV_IMAGE=$IMAGE" \
        -e "ARSV_IMAGE_DIGEST=$image_digest" \
        -e "ARSV_IMAGE_ID=$image_id" \
        -e "ARSV_CONVERTER_REL=$converter_rel" \
        -v "$REPO_ROOT:/src:ro" \
        -v "$SNAPSHOT/container-output:/out:rw" \
        "$IMAGE" sh -euc '
            apt-get update >/dev/null
            apt-get install -y gcc librpm-devel apt-repo-tools python3 >/dev/null
            python3 "/src/$ARSV_CONVERTER_REL" \
                --inner /out/conversion \
                --image "$ARSV_IMAGE" \
                --image-digest "$ARSV_IMAGE_DIGEST" \
                --image-id "$ARSV_IMAGE_ID"
            cp -a /var/lib/apt/lists /out/original-lists
            cp -a /etc/apt /out/etc-apt
        '

    mv "$SNAPSHOT/container-output/original-lists" "$SNAPSHOT/lists"
    mv "$SNAPSHOT/container-output/etc-apt" "$SNAPSHOT/etc-apt"
    mv "$SNAPSHOT/container-output/conversion/d1-pkglists/manifest.json" "$SNAPSHOT/manifest.json"
    mkdir -p "$SNAPSHOT/d1-pkglists"
    mv "$SNAPSHOT/container-output/conversion/d1-pkglists/"*.classic "$SNAPSHOT/d1-pkglists/"
    rm -rf "$SNAPSHOT/container-output"
    rm -f "$SNAPSHOT/lists/lock"
    rm -rf "$SNAPSHOT/lists/partial"
    mkdir -p "$SNAPSHOT/lists/partial" "$SNAPSHOT/etc-apt/sources.list.d"
    [[ -e $SNAPSHOT/etc-apt/sources.list ]] || : >"$SNAPSHOT/etc-apt/sources.list"
    write_snapshot_fingerprint >"$SNAPSHOT/input-fingerprint.txt"
    : >"$SNAPSHOT/.complete"
}

prepare_variant_lists()
{
    local variant=$1 format=$2
    if [[ -d $variant/apt ]]; then
        chmod -R u+w "$variant/apt"
    fi
    rm -rf "$variant/apt"
    mkdir -p "$variant/apt/lists" "$variant/apt/cache/archives/partial"
    cp -a "$SNAPSHOT/lists/." "$variant/apt/lists/"

    if [[ $format == d1 ]]; then
        python3 - "$SNAPSHOT/manifest.json" "$SNAPSHOT/lists" \
            "$SNAPSHOT/d1-pkglists" "$variant/apt/lists" <<'PY'
import hashlib
import json
import shutil
import sys
from pathlib import Path

manifest_path, original_dir, d1_dir, target_dir = map(Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text())
for architecture, data in manifest["architectures"].items():
    source = original_dir / data["source"]["path"]
    converted = d1_dir / data["output"]["path"]
    target = target_dir / data["source"]["path"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != data["source"]["sha256"]:
        raise SystemExit(f"source checksum mismatch for {architecture}: {source}")
    if hashlib.sha256(converted.read_bytes()).hexdigest() != data["output"]["sha256"]:
        raise SystemExit(f"D1 checksum mismatch for {architecture}: {converted}")
    shutil.copyfile(converted, target)
PY
    fi
}

build_variant()
{
    local name=$1 source_c=$2 suffix=$3 variant source hasher repo spec
    local expected_fingerprint artifact_fingerprint
    local -a librpm_rpms
    variant=$(variant_dir "$name")
    source="$variant/src/rpm"
    hasher="$variant/hasher"
    repo="$hasher/repo/x86_64/RPMS.hasher"
    spec="$source/alt/rpm.spec"

    printf '\n===== Building %s with %s =====\n' "$name" "$source_c"
    mkdir -p "$variant/src" "$variant/logs" "$hasher"
    shopt -s nullglob
    librpm_rpms=("$repo"/librpm7-[0-9]*".$suffix".x86_64.rpm)
    expected_fingerprint=$(mktemp)
    {
        printf 'base_commit=%s\n' "$(git -C "$SOURCE_BASE" rev-parse HEAD)"
        printf 'set_source=%s\n' "$(sha256sum "$source_c" | awk '{print $1}')"
        printf 'suffix=%s\n' "$suffix"
        printf 'packager=%s\n' "$PACKAGER"
    } >"$expected_fingerprint"
    if [[ -f $variant/input-fingerprint.txt ]]; then
        if ! cmp -s "$expected_fingerprint" "$variant/input-fingerprint.txt"; then
            rm -f "$expected_fingerprint"
            fail "$name build inputs changed; use RESET_WORK=1"
        fi
    elif ((${#librpm_rpms[@]} > 0)); then
        rm -f "$expected_fingerprint"
        fail "$name RPM exists without its input fingerprint; use RESET_WORK=1"
    fi
    if ((${#librpm_rpms[@]} == 0)) && [[ -d $source ]]; then
        rm -rf "$source"
    fi
    if [[ ! -d $source/.git ]]; then
        git clone --local "$SOURCE_BASE" "$source"
        cp "$source_c" "$source/lib/set.c"
        prepare_spec "$spec" "$suffix"
        cp "$expected_fingerprint" "$variant/input-fingerprint.txt"
    else
        cmp -s "$source_c" "$source/lib/set.c" ||
            fail "$source_c changed; use RESET_WORK=1"
    fi
    rm -f "$expected_fingerprint"

    librpm_rpms=("$repo"/librpm7-[0-9]*".$suffix".x86_64.rpm)
    if ((${#librpm_rpms[@]} == 0)); then
        (
            cd "$source"
            gear-hsh \
                --commit \
                --with-stuff \
                --packager="$PACKAGER" \
                --no-sisyphus-check=changelog,packager,gpg \
                --mountpoints=/proc \
                --workdir="$hasher" \
                --target=x86_64
        ) 2>&1 | tee "$variant/logs/rpm.log"
    else
        printf 'already built: %s\n' "${librpm_rpms[0]}"
    fi

    librpm_rpms=("$repo"/librpm7-[0-9]*".$suffix".x86_64.rpm)
    ((${#librpm_rpms[@]} == 1)) ||
        fail "expected one librpm7 package for $name, got ${#librpm_rpms[@]}"
    artifact_fingerprint=$(sha256sum "${librpm_rpms[0]}")
    if [[ -f $variant/librpm-artifact.sha256 ]]; then
        [[ $artifact_fingerprint == "$(<"$variant/librpm-artifact.sha256")" ]] ||
            fail "$name librpm artifact changed; use RESET_WORK=1"
    else
        printf '%s\n' "$artifact_fingerprint" >"$variant/librpm-artifact.sha256"
    fi

    rm -rf "$variant/lib"
    mkdir -p "$variant/lib"
    (
        cd "$variant/lib"
        rpm2cpio "${librpm_rpms[0]}" | cpio -idm --quiet
    )
    [[ -e $variant/lib/usr/lib64/librpm.so.7 && -e $variant/lib/usr/lib64/librpmio.so.7 ]] ||
        fail "librpm libraries were not extracted for $name"
    for apt_binary in "$APT_GET" "$APT_CACHE"; do
        LD_LIBRARY_PATH="$variant/lib/usr/lib64" ldd "$apt_binary" |
            awk -v expected="$variant/lib/usr/lib64/librpm.so.7" '
                index($0, expected) { found = 1 }
                END { exit found ? 0 : 1 }
            ' || fail "$apt_binary does not load the built librpm for $name"
    done
}

build_cache()
{
    local name=$1 variant libdir start end elapsed stdout stderr status
    variant=$(variant_dir "$name")
    libdir=$(variant_libdir "$name")
    apt_options "$variant"
    stdout="$variant/logs/gencaches.stdout"
    stderr="$variant/logs/gencaches.stderr"

    chmod -R u+w "$variant/apt/cache"
    rm -f "$variant/apt/cache/pkgcache.bin" "$variant/apt/cache/srcpkgcache.bin"
    start=$(date +%s%N)
    if env LC_ALL=C APT_CONFIG="$COMMON/apt.conf" LD_LIBRARY_PATH="$libdir" \
        "$APT_CACHE" -q "${APT_OPTIONS[@]}" gencaches >"$stdout" 2>"$stderr"; then
        status=0
    else
        status=$?
    fi
    end=$(date +%s%N)
    elapsed=$(awk -v start="$start" -v end="$end" \
        'BEGIN { printf "%.6f", (end - start) / 1000000000 }')
    printf '%s\t%s\t%s\n' "$name" "$elapsed" "$status" >>"$CACHE_RESULTS"
    [[ $status -eq 0 && -s $variant/apt/cache/pkgcache.bin ]] ||
        fail "gencaches failed for $name; see $stderr"
    chmod -R a-w "$variant/apt/cache"
}

record_cache_checksums()
{
    local name variant
    : >"$RESULT_DIR/cache-files.before.txt"
    for name in set9 d1; do
        variant=$(variant_dir "$name")
        for cache_file in "$variant/apt/cache/pkgcache.bin" \
            "$variant/apt/cache/srcpkgcache.bin"; do
            stat -c '%n\t%D\t%i\t%s\t%Y\t%A' "$cache_file"
            sha256sum "$cache_file"
        done >>"$RESULT_DIR/cache-files.before.txt"
    done
}

verify_cache_checksums()
{
    local name variant
    : >"$RESULT_DIR/cache-files.after.txt"
    for name in set9 d1; do
        variant=$(variant_dir "$name")
        for cache_file in "$variant/apt/cache/pkgcache.bin" \
            "$variant/apt/cache/srcpkgcache.bin"; do
            stat -c '%n\t%D\t%i\t%s\t%Y\t%A' "$cache_file"
            sha256sum "$cache_file"
        done >>"$RESULT_DIR/cache-files.after.txt"
    done
    cmp -s "$RESULT_DIR/cache-files.before.txt" \
        "$RESULT_DIR/cache-files.after.txt" ||
        fail "APT cache changed during timed runs"
}

write_provenance()
{
    {
        printf 'rpm_git=%s\n' "$RPM_GIT"
        printf 'rpm_branch=%s\n' "$RPM_BRANCH"
        printf 'rpm_commit=%s\n' "$(git -C "$SOURCE_BASE" rev-parse HEAD)"
        printf 'image=%s\n' "$IMAGE"
        printf 'image_digest=%s\n' "$(podman image inspect "$IMAGE" --format '{{.Digest}}')"
        printf 'image_id=%s\n' "$(podman image inspect "$IMAGE" --format '{{.Id}}')"
        printf 'apt=%s\n' "$(rpmquery --qf '%{VERSION}-%{RELEASE}' apt)"
        printf 'rpm=%s\n' "$(rpm --version)"
        printf 'cpu=%s\n' "$CPU"
        printf 'rounds=%s\n' "$ROUNDS"
        printf 'operations=%s\n' "$OPERATIONS"
        sha256sum "$SET9_C" "$D1_C" "$PKGLIST_CONVERTER" \
            "$REPO_ROOT/new_version/direct_hash/apt_benchmark/rewrite_sisyphus_pkglist.c" \
            "$REPO_ROOT/scripts/rpmsetcmp/newset_compat.h"
        printf 'set9_librpm='; cat "$SET9_VARIANT/librpm-artifact.sha256"
        printf 'd1_librpm='; cat "$D1_VARIANT/librpm-artifact.sha256"
    } >"$RESULT_DIR/provenance.txt"
    cp "$SNAPSHOT/manifest.json" "$RESULT_DIR/pkglist-manifest.json"
}

summarize_results()
{
    python3 - "$RAW_RESULTS" "$RESULT_DIR/summary.tsv" "$RESULT_DIR/summary.md" <<'PY'
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

raw_path, tsv_path, markdown_path = map(Path, sys.argv[1:])
with raw_path.open(newline="") as stream:
    rows = list(csv.DictReader(stream, delimiter="\t"))

groups = defaultdict(list)
for row in rows:
    groups[row["operation"]].append(row)

summary = []
all_equivalent = True
for operation, items in groups.items():
    by_variant = defaultdict(list)
    signatures = set()
    statuses = set()
    for item in items:
        by_variant[item["variant"]].append(float(item["seconds"]))
        statuses.add(item["status"])
        signatures.add(
            (
                item["status"],
                item["stdout_sha256"],
                item["stdout_bytes"],
                item["stderr_sha256"],
                item["stderr_bytes"],
            )
        )
    if set(by_variant) != {"set9", "d1"}:
        raise SystemExit(f"missing variant samples for {operation}")
    if len(by_variant["set9"]) != len(by_variant["d1"]):
        raise SystemExit(f"unbalanced variant samples for {operation}")
    set9 = statistics.median(by_variant["set9"])
    d1 = statistics.median(by_variant["d1"])
    equivalent = len(signatures) == 1
    all_equivalent &= equivalent
    status = next(iter(statuses)) if len(statuses) == 1 else "mixed:" + ",".join(sorted(statuses))
    path = "normal" if status == "0" else "failure-path"
    summary.append(
        (
            operation,
            len(by_variant["set9"]),
            set9,
            min(by_variant["set9"]),
            max(by_variant["set9"]),
            d1,
            min(by_variant["d1"]),
            max(by_variant["d1"]),
            d1 / set9,
            status,
            path,
            equivalent,
        )
    )

with tsv_path.open("w", newline="") as stream:
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(
        [
            "operation", "runs_per_variant", "set9_median_seconds", "set9_min_seconds",
            "set9_max_seconds", "d1_median_seconds", "d1_min_seconds", "d1_max_seconds",
            "d1/set9", "exit_status", "benchmark_path", "outputs_equal"
        ]
    )
    for operation, count, set9, set9_min, set9_max, d1, d1_min, d1_max, ratio, status, path, equivalent in summary:
        writer.writerow(
            [
                operation, count, f"{set9:.6f}", f"{set9_min:.6f}", f"{set9_max:.6f}",
                f"{d1:.6f}", f"{d1_min:.6f}", f"{d1_max:.6f}", f"{ratio:.4f}",
                status, path, "yes" if equivalent else "NO"
            ]
        )

lines = [
    "# Sisyphus set9 vs D1 benchmark",
    "",
    "Timed scope: resolver commands only; conversion, builds and `gencaches` are excluded.",
    "Each round uses the balanced order `set9 / d1 / d1 / set9` after one warm-up per variant.",
    "",
    "| operation | runs/variant | set9 median [min–max], s | D1 median [min–max], s | D1/set9 | status/path | output equivalence |",
    "|---|---:|---:|---:|---:|:---:|:---:|",
]
for operation, count, set9, set9_min, set9_max, d1, d1_min, d1_max, ratio, status, path, equivalent in summary:
    lines.append(
        f"| `{operation}` | {count} | {set9:.6f} [{set9_min:.6f}–{set9_max:.6f}] | "
        f"{d1:.6f} [{d1_min:.6f}–{d1_max:.6f}] | {ratio:.4f} | "
        f"{status}/{path} | {'yes' if equivalent else '**NO**'} |"
    )
lines.extend(
    [
        "",
        "`D1/set9 < 1` means that the D1 variant was faster.",
        "Rows with non-zero status are explicitly labelled `failure-path`; do not treat them as successful resolver workloads.",
        "Output equivalence includes exit status plus exact stdout and stderr hashes for every repeat and both variants.",
    ]
)
markdown_path.write_text("\n".join(lines) + "\n")
if not all_equivalent:
    raise SystemExit(2)
PY
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi
(($# == 0)) || fail "unexpected arguments; use --help"

for command in git podman gear-hsh hsh rpm rpmquery rpm2cpio cpio taskset awk sed \
    date sha256sum stat ldd python3 cmp cp mv tee realpath mktemp grep; do
    command -v "$command" >/dev/null || fail "required command not found: $command"
done
[[ -n $APT_CACHE && -x $APT_CACHE ]] || fail "apt-cache not found: $APT_CACHE"
[[ -x $APT_GET ]] || fail "apt-get executable not found: $APT_GET"
[[ -f $SET9_C ]] || fail "set9 source not found: $SET9_C"
[[ -f $D1_C ]] || fail "D1 source not found: $D1_C"
[[ -f $PKGLIST_CONVERTER ]] || fail "pkglist converter not found: $PKGLIST_CONVERTER"
[[ $ROUNDS =~ ^[1-9][0-9]*$ ]] || fail "ROUNDS must be a positive integer"
taskset -c "$CPU" true >/dev/null 2>&1 || fail "CPU $CPU is unavailable to taskset"
read -r -a OPERATION_LIST <<<"$OPERATIONS"
((${#OPERATION_LIST[@]} > 0)) || fail "OPERATIONS is empty"
for operation in "${OPERATION_LIST[@]}"; do
    case $operation in
        unmet|check|autoremove|install-rpm-build|install-openuds-server|install-password-store|upgrade|dist-upgrade) ;;
        *) fail "unknown operation in OPERATIONS: $operation" ;;
    esac
done

HOME_REAL=$(realpath -m "$HOME")
CWD_REAL=$(realpath -m "$PWD")
REPO_ROOT=$(realpath -m "$REPO_ROOT")
WORK_ROOT=$(realpath -m "$WORK_ROOT")
RESULT_DIR=$(realpath -m "$RESULT_DIR")
[[ $RESULT_DIR == "$WORK_ROOT"/* ]] ||
    fail "RESULT_DIR must be inside WORK_ROOT: $RESULT_DIR"

COMMON="$WORK_ROOT/common"
SNAPSHOT="$COMMON/snapshot"
SOURCE_BASE="$WORK_ROOT/src/rpm-base"
SOURCE_FINGERPRINT="$WORK_ROOT/src/rpm-base.fingerprint.txt"
SET9_VARIANT="$WORK_ROOT/variants/set9"
D1_VARIANT="$WORK_ROOT/variants/d1"

WORK_ROOT_EXISTED=0
[[ -e $WORK_ROOT ]] && WORK_ROOT_EXISTED=1
if ((RESET_WORK)); then
    safe_remove_work_root
    for old_hasher in "$SET9_VARIANT/hasher" "$D1_VARIANT/hasher"; do
        [[ -d $old_hasher ]] || continue
        hsh --cleanup-only --workdir="$old_hasher" ||
            fail "hasher cleanup failed: $old_hasher"
    done
    for old_apt in "$SET9_VARIANT/apt" "$D1_VARIANT/apt"; do
        [[ -d $old_apt ]] || continue
        chmod -R u+w "$old_apt"
    done
    rm -rf "$WORK_ROOT"
    WORK_ROOT_EXISTED=0
elif ((WORK_ROOT_EXISTED)); then
    marker="$WORK_ROOT/.arsv-sisyphus-set-bench-root"
    [[ -f $marker && ! -L $marker ]] ||
        fail "refusing unmarked existing WORK_ROOT: $WORK_ROOT"
    grep -Fx 'ARSV Sisyphus set9/D1 benchmark work root' "$marker" >/dev/null ||
        fail "invalid WORK_ROOT ownership marker: $marker"
fi
mkdir -p "$WORK_ROOT/src" "$WORK_ROOT/variants" "$COMMON/root/var/lib/rpm" "$RESULT_DIR"
MARKER="$WORK_ROOT/.arsv-sisyphus-set-bench-root"
if [[ -e $MARKER ]]; then
    grep -Fx 'ARSV Sisyphus set9/D1 benchmark work root' "$MARKER" >/dev/null ||
        fail "invalid WORK_ROOT ownership marker: $MARKER"
else
    printf '%s\n' 'ARSV Sisyphus set9/D1 benchmark work root' >"$MARKER"
fi
: >"$COMMON/apt.conf"
: >"$COMMON/status"

prepare_snapshot
if [[ ! -d $SOURCE_BASE/.git ]]; then
    git clone --branch "$RPM_BRANCH" --single-branch "$RPM_GIT" "$SOURCE_BASE"
    write_source_fingerprint >"$SOURCE_FINGERPRINT"
else
    validate_source_reuse
fi

build_variant set9 "$SET9_C" arsvset9
build_variant d1 "$D1_C" arsvd1
prepare_variant_lists "$SET9_VARIANT" set9
prepare_variant_lists "$D1_VARIANT" d1

mkdir -p "$RESULT_DIR/raw"
CACHE_RESULTS="$RESULT_DIR/cache-build.tsv"
printf 'variant\tseconds\texit_status\n' >"$CACHE_RESULTS"
build_cache set9
build_cache d1
record_cache_checksums
write_provenance

RAW_RESULTS="$RESULT_DIR/raw.tsv"
printf 'operation\tsequence\tvariant\tsample\tseconds\tstatus\tstdout_sha256\tstdout_bytes\tstderr_sha256\tstderr_bytes\n' >"$RAW_RESULTS"
declare -A SAMPLE_COUNTS=([set9]=0 [d1]=0)
sequence=0

for operation in "${OPERATION_LIST[@]}"; do
    printf '\n===== Warm-up: %s =====\n' "$operation"
    mkdir -p "$RESULT_DIR/warmup/$operation"
    for variant_name in set9 d1; do
        prepare_command "$operation" "$variant_name"
        warm_status=$(execute_command \
            "$RESULT_DIR/warmup/$operation/$variant_name.stdout" \
            "$RESULT_DIR/warmup/$operation/$variant_name.stderr")
        printf '%s\n' "$warm_status" >"$RESULT_DIR/warmup/$operation/$variant_name.status"
    done

    printf '===== Timed ABBA: %s =====\n' "$operation"
    for ((round = 1; round <= ROUNDS; ++round)); do
        for variant_name in set9 d1 d1 set9; do
            sequence=$((sequence + 1))
            SAMPLE_COUNTS[$variant_name]=$((SAMPLE_COUNTS[$variant_name] + 1))
            run_once "$operation" "$variant_name" "$sequence" "${SAMPLE_COUNTS[$variant_name]}"
        done
    done
    SAMPLE_COUNTS[set9]=0
    SAMPLE_COUNTS[d1]=0
done

verify_cache_checksums
if ! summarize_results; then
    fail "variant/repeat outputs differ; inspect $RESULT_DIR/raw and $RESULT_DIR/summary.md"
fi

printf '\nDone. Results:\n'
printf '  %s\n' "$RESULT_DIR/summary.md" "$RESULT_DIR/summary.tsv" \
    "$RESULT_DIR/raw.tsv" "$RESULT_DIR/cache-build.tsv" "$RESULT_DIR/provenance.txt" \
    "$RESULT_DIR/pkglist-manifest.json"
