#!/bin/bash
set -euo pipefail
export LC_ALL=C

# Настройки.
SETC_DIR="$HOME/setc"
WORK_ROOT="$HOME/setc-bench"
RESULT_DIR="$HOME/res"
RUNS=3
CPU=0
RESET_WORK=1  # 0 — продолжить готовые сборки, 1 — начать всё заново.
COLLECT_PERF=1
PERF_RECORD=1  # Отдельный профильный прогон; не входит в среднее время.
PERF_FREQUENCY=499
PERF_EVENTS='task-clock,context-switches,cpu-migrations,page-faults,minor-faults,major-faults,cycles,instructions,branches,branch-misses,cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses,LLC-loads,LLC-load-misses'
PACKAGER='krosh <gudovdo@my.msu.ru>'
APT_SOURCE=/etc/apt/sources.list.d/alt.list
APT_GET=/usr/lib/apt/apt-get
RPM_BUILD_GIT=https://git.altlinux.org/gears/r/rpm-build.git
RPM_GIT=https://git.altlinux.org/gears/r/rpm.git

fail()
{
    printf 'error: %s\n' "$*" >&2
    exit 1
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
- Local set.c benchmark build.\\
" "$spec"
}

apt_options()
{
    local root=$1

    APT_OPTIONS=(
        -o 'Dir::Etc::main=-'
        -o 'Dir::Etc::parts=-'
        -o "Dir::Etc::sourcelist=$COMMON/sources.list"
        -o "Dir::Etc::sourceparts=$COMMON/sources.list.d"
        -o 'Dir::Etc::preferences=-'
        -o 'Dir::Etc::preferencesparts=-'
        -o "Dir::State::lists=$COMMON/lists/"
        -o "Dir::State::status=$COMMON/status"
        -o "Dir::Cache=$COMMON/cache/"
        -o "Dir::Cache::archives=$COMMON/cache/archives"
        -o "Dir::Cache::pkgcache=$COMMON/cache/pkgcache.bin"
        -o "Dir::Cache::srcpkgcache=$COMMON/cache/srcpkgcache.bin"
        -o "RPM::RootDir=$root"
    )
}

operation_args()
{
    case $1 in
        check) ARGS=(-s check) ;;
        autoremove) ARGS=(-s autoremove) ;;
        install-rpm-build) ARGS=(-s install rpm-build) ;;
        install-openuds-server) ARGS=(-s install openuds-server) ;;
        install-password-store) ARGS=(-s install password-store) ;;
        upgrade) ARGS=(-o APT::Get::EnableUpgrade=true -s upgrade) ;;
        dist-upgrade) ARGS=(-s dist-upgrade) ;;
        *) fail "unknown operation: $1" ;;
    esac
}

operation_label()
{
    case $1 in
        check) printf '%s' '-s check' ;;
        autoremove) printf '%s' '-s autoremove' ;;
        install-rpm-build) printf '%s' '-s install rpm-build' ;;
        install-openuds-server) printf '%s' '-s install openuds-server' ;;
        install-password-store) printf '%s' '-s install password-store' ;;
        upgrade) printf '%s' '--enable-upgrade -s upgrade' ;;
        dist-upgrade) printf '%s' '-s dist-upgrade' ;;
    esac
}

run_once()
{
    local operation=$1 variant=$2 run=$3 start end status perf_stat
    local libdir="$variant/lib/usr/lib64"
    local root="$COMMON/root"
    local raw="$variant/raw"
    local -a command

    operation_args "$operation"
    apt_options "$root"
    command=("$APT_GET" -q "${APT_OPTIONS[@]}" "${ARGS[@]}")

    if ((COLLECT_PERF)); then
        perf_stat="$raw/perf-stat/$operation.$run.tsv"
        command=(
            perf stat
            --all-user
            --no-big-num
            -x $'\t'
            -o "$perf_stat"
            -e "$PERF_EVENTS"
            --
            "${command[@]}"
        )
    fi

    start=$(date +%s%N)
    if env LC_ALL=C APT_CONFIG="$COMMON/apt.conf" \
        LD_LIBRARY_PATH="$libdir" \
        taskset -c "$CPU" "${command[@]}" \
        >"$raw/$operation.$run.stdout" \
        2>"$raw/$operation.$run.stderr"; then
        status=0
    else
        status=$?
    fi
    end=$(date +%s%N)

    RUN_TIME=$(awk -v start="$start" -v end="$end" \
        'BEGIN { printf "%.6f", (end - start) / 1000000000 }')
    RUN_STATUS=$status
}

append_perf_stat()
{
    local operation=$1 run=$2 raw_file=$3 result_file=$4 label

    label=$(operation_label "$operation")
    awk -F '\t' -v OFS='\t' -v operation="$label" -v run="$run" '
        /^[[:space:]]*#/ || NF < 3 { next }
        { print operation, run, $1, $2, $3, $4, $5, $6, $7, $8 }
    ' "$raw_file" >>"$result_file"
}

record_profile()
{
    local operation=$1 variant=$2 perf_dir=$3 dso_name=$4 status data report
    local libdir="$variant/lib/usr/lib64"
    local root="$COMMON/root"
    local -a command

    operation_args "$operation"
    apt_options "$root"
    data="$perf_dir/$operation.perf.data"
    report="$perf_dir/$operation.report.txt"
    command=("$APT_GET" -q "${APT_OPTIONS[@]}" "${ARGS[@]}")

    if env LC_ALL=C APT_CONFIG="$COMMON/apt.conf" \
        LD_LIBRARY_PATH="$libdir" \
        taskset -c "$CPU" perf \
        --buildid-dir "$perf_dir/buildid-cache" \
        record \
        --all-user \
        --quiet \
        --freq "$PERF_FREQUENCY" \
        --event cycles \
        --call-graph dwarf,8192 \
        --output "$data" \
        -- "${command[@]}" \
        >"$perf_dir/$operation.stdout" \
        2>"$perf_dir/$operation.stderr"; then
        status=0
    else
        status=$?
    fi

    printf '%s\n' "$status" >"$perf_dir/$operation.status"
    if [[ -s $data ]]; then
        perf --buildid-dir "$perf_dir/buildid-cache" report \
            --stdio \
            --no-children \
            --percent-limit 0.5 \
            --sort comm,dso,symbol \
            --input "$data" \
            >"$report" \
            2>"$perf_dir/$operation.report.stderr" || true

        perf --buildid-dir "$perf_dir/buildid-cache" report \
            --stdio \
            --no-children \
            --inline \
            --percent-limit 0 \
            --sort dso,symbol,srcline \
            --input "$data" \
            >"$perf_dir/$operation.lines.txt" \
            2>"$perf_dir/$operation.lines.stderr" || true

        # Без TUI: perf 6.18 может упасть при выборе символа без self-samples.
        perf --buildid-dir "$perf_dir/buildid-cache" annotate \
            --stdio \
            --dsos "$dso_name" \
            --input "$data" \
            >"$perf_dir/$operation.annotate.txt" \
            2>"$perf_dir/$operation.annotate.stderr" || true
    fi
}

prepare_perf_symbols()
{
    local perf_dir=$1 runtime_file=$2 debug_file=$3 debuginfo_rpm=$4
    local unstripped="$perf_dir/librpm.unstripped"

    mkdir -p "$perf_dir/buildid-cache"
    cp -a "$debuginfo_rpm" "$perf_dir/"
    # Split-debug ELF содержит DWARF, но не байты .text для annotate.
    # eu-unstrip объединяет runtime-код и matching debuginfo с тем же Build ID.
    eu-unstrip -o "$unstripped" "$runtime_file" "$debug_file"
    perf --buildid-dir "$perf_dir/buildid-cache" buildid-cache \
        --add "$unstripped" \
        >"$perf_dir/buildid-cache.stdout" \
        2>"$perf_dir/buildid-cache.stderr"
}

benchmark_variant()
{
    local variant=$1 result=$2 debug_file=$3 debuginfo_rpm=$4
    local runtime_file=$5 dso_name=$6
    local operation run average label status_text perf_result perf_dir
    local -a times statuses
    local -a operations=(
        check
        autoremove
        install-rpm-build
        install-openuds-server
        install-password-store
        upgrade
        dist-upgrade
    )

    mkdir -p "$variant/raw/perf-stat"
    printf 'command\taverage_seconds\trun1_seconds\trun2_seconds\trun3_seconds\texit_status\n' \
        >"$result"
    perf_result=${result%.tsv}.perf-stat.tsv
    perf_dir=${result%.tsv}.perf
    if ((COLLECT_PERF)); then
        printf 'command\trun\tvalue\tunit\tevent\tcounter_runtime\trunning_percent\tmetric_value\tmetric_unit\textra\n' \
            >"$perf_result"
        if ((PERF_RECORD)); then
            rm -rf "$perf_dir"
            mkdir -p "$perf_dir"
            prepare_perf_symbols "$perf_dir" "$runtime_file" \
                "$debug_file" "$debuginfo_rpm"
        fi
    fi

    for operation in "${operations[@]}"; do
        times=()
        statuses=()

        for ((run = 1; run <= RUNS; ++run)); do
            run_once "$operation" "$variant" "$run"
            times+=("$RUN_TIME")
            statuses+=("$RUN_STATUS")
            if ((COLLECT_PERF)); then
                append_perf_stat "$operation" "$run" \
                    "$variant/raw/perf-stat/$operation.$run.tsv" "$perf_result"
            fi
            printf '%s: run %d/%d: %ss, status=%s\n' \
                "$operation" "$run" "$RUNS" "$RUN_TIME" "$RUN_STATUS"
        done

        average=$(printf '%s\n' "${times[@]}" | \
            awk '{ total += $1 } END { printf "%.6f", total / NR }')

        status_text=${statuses[0]}
        for status in "${statuses[@]}"; do
            if [[ $status != "$status_text" ]]; then
                status_text=$(IFS=,; printf 'mixed:%s' "${statuses[*]}")
                break
            fi
        done

        label=$(operation_label "$operation")
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$label" "$average" \
            "${times[0]}" "${times[1]}" "${times[2]}" "$status_text" \
            >>"$result"

        if ((COLLECT_PERF && PERF_RECORD)); then
            printf '%s: perf record\n' "$operation"
            record_profile "$operation" "$variant" "$perf_dir" "$dso_name"
        fi
    done
}

for command in git gear-hsh hsh rpm rpmquery rpm2cpio cpio apt-get apt-cache \
    taskset awk sed date sha256sum ldd readelf readlink eu-unstrip; do
    command -v "$command" >/dev/null || fail "required command not found: $command"
done
if ((COLLECT_PERF)); then
    command -v perf >/dev/null || \
        fail "perf is not installed; install the ALT package: apt-get install perf"
    perf stat --all-user -e task-clock -- true >/dev/null 2>&1 || \
        fail 'perf events are unavailable; set kernel.perf_event_paranoid=2 or lower'
fi
[[ -x $APT_GET ]] || fail "not found: $APT_GET"
[[ $RUNS -eq 3 ]] || fail 'RUNS must stay equal to 3 for the TSV format below'

shopt -s nullglob
setc_files=("$SETC_DIR"/*.c)
((${#setc_files[@]} > 0)) || fail "no .c files in $SETC_DIR"

if ((RESET_WORK)); then
    # После прерванной сборки часть chroot принадлежит hasher-псевдопользователю;
    # обычный rm её не удалит.
    old_hashers=("$WORK_ROOT"/variants/*/hasher)
    for old_hasher in "${old_hashers[@]}"; do
        [[ -d $old_hasher ]] || continue
        hsh --cleanup-only --workdir="$old_hasher" >/dev/null 2>&1 || true
    done
    rm -rf "$WORK_ROOT"
fi
mkdir -p "$WORK_ROOT/src" "$WORK_ROOT/variants" "$RESULT_DIR"
COMMON="$WORK_ROOT/common"
if [[ ! -s $COMMON/cache/pkgcache.bin ]]; then
    rm -rf "$COMMON"
    mkdir -p \
        "$COMMON/lists/partial" \
        "$COMMON/cache/archives/partial" \
        "$COMMON/sources.list.d" \
        "$COMMON/root/var/lib/rpm"
    : >"$COMMON/apt.conf"
    : >"$COMMON/status"

    # Один официальный p11 snapshot без hasher/stuff. Он создаётся до сборок
    # и затем одинаково используется всеми вариантами.
    awk '$1 == "rpm" && /p11\/branch/ && $0 !~ /(hasher|stuff)/ { print }' \
        "$APT_SOURCE" >"$COMMON/sources.list"
    [[ -s $COMMON/sources.list ]] || fail "no p11 sources in $APT_SOURCE"

    cp -a /var/lib/rpm/. "$COMMON/root/var/lib/rpm/"
    apt_options "$COMMON/root"
    APT_CONFIG="$COMMON/apt.conf" apt-get -qq "${APT_OPTIONS[@]}" update
    APT_CONFIG="$COMMON/apt.conf" apt-cache -q "${APT_OPTIONS[@]}" gencaches >/dev/null
    sha256sum "$COMMON/cache/pkgcache.bin" "$COMMON/cache/srcpkgcache.bin" \
        >"$COMMON/cache.sha256"
fi

# Чистые p11 gear-деревья клонируются один раз.
if [[ ! -d $WORK_ROOT/src/rpm-build-base/.git ]]; then
    git clone --branch p11 --single-branch "$RPM_BUILD_GIT" \
        "$WORK_ROOT/src/rpm-build-base"
fi
if [[ ! -d $WORK_ROOT/src/rpm-base/.git ]]; then
    git clone --branch p11 --single-branch "$RPM_GIT" \
        "$WORK_ROOT/src/rpm-base"
fi

index=0
for setc in "${setc_files[@]}"; do
    index=$((index + 1))
    id=$(printf 'v%03d' "$index")
    suffix="setc$id"
    filename=$(basename "$setc")
    result_name=${filename%.c}.tsv
    variant="$WORK_ROOT/variants/$id"
    source="$variant/src"
    hasher="$variant/hasher"
    repo="$hasher/repo/x86_64/RPMS.hasher"

    printf '\n===== %s: %s =====\n' "$id" "$filename"
    mkdir -p "$source" "$hasher" "$variant/logs" "$variant/lib"

    if [[ ! -d $source/rpm-build/.git ]]; then
        git clone --local "$WORK_ROOT/src/rpm-build-base" "$source/rpm-build"
        git clone --local "$WORK_ROOT/src/rpm-base" "$source/rpm"

        # По условию один и тот же входной файл кладётся как lib/set.c в оба пакета.
        cp "$setc" "$source/rpm-build/lib/set.c"
        cp "$setc" "$source/rpm/lib/set.c"

        prepare_spec "$source/rpm-build/rpm-4_0.spec" "$suffix"
        prepare_spec "$source/rpm/alt/rpm.spec" "$suffix"
    else
        cmp -s "$setc" "$source/rpm-build/lib/set.c" && \
            cmp -s "$setc" "$source/rpm/lib/set.c" || \
            fail "$filename changed; set RESET_WORK=1 and restart"
    fi

    rpm_build_rpms=("$repo"/rpm-build-[0-9]*".$suffix".x86_64.rpm)
    if ((${#rpm_build_rpms[@]} == 0)); then
        (
            cd "$source/rpm-build"
            gear-hsh \
                --commit \
                --with-stuff \
                --packager="$PACKAGER" \
                --no-sisyphus-check=changelog,packager,gpg \
                --mountpoints=/proc \
                --workdir="$hasher" \
                --target=x86_64
        ) 2>&1 | tee "$variant/logs/rpm-build.log"
    else
        printf 'rpm-build already built: %s\n' "${rpm_build_rpms[0]}"
    fi

    librpm_rpms=("$repo"/librpm7-[0-9]*".$suffix".x86_64.rpm)
    if ((${#librpm_rpms[@]} == 0)); then
        (
            cd "$source/rpm"
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
        printf 'rpm/librpm7 already built: %s\n' "${librpm_rpms[0]}"
    fi

    # [0-9]* исключает отдельный пакет librpm7-debuginfo.
    librpm_rpms=("$repo"/librpm7-[0-9]*".$suffix".x86_64.rpm)
    ((${#librpm_rpms[@]} == 1)) || \
        fail "expected one librpm7 package for $filename, got ${#librpm_rpms[@]}"

    debug_file=
    debuginfo_rpm=
    if ((COLLECT_PERF && PERF_RECORD)); then
        debuginfo_rpms=("$repo"/librpm7-debuginfo-[0-9]*".$suffix".x86_64.rpm)
        ((${#debuginfo_rpms[@]} == 1)) || \
            fail "expected one librpm7-debuginfo package for $filename, got ${#debuginfo_rpms[@]}"
        debuginfo_rpm=${debuginfo_rpms[0]}
    fi

    rm -rf "$variant/lib" "$variant/debug"
    mkdir -p "$variant/lib"
    (
        cd "$variant/lib"
        rpm2cpio "${librpm_rpms[0]}" | cpio -idm --quiet
    )

    libdir="$variant/lib/usr/lib64"
    [[ -e $libdir/librpm.so.7 && -e $libdir/librpmio.so.7 ]] || \
        fail "librpm libraries were not extracted for $filename"
    runtime_file=$(readlink -f "$libdir/librpm.so.7")
    dso_name=$(basename "$runtime_file")
    LD_LIBRARY_PATH="$libdir" ldd "$APT_GET" | \
        grep -F "$libdir/librpm.so.7" >/dev/null || \
        fail "apt-get does not load the built librpm for $filename"

    if ((COLLECT_PERF && PERF_RECORD)); then
        mkdir -p "$variant/debug"
        (
            cd "$variant/debug"
            rpm2cpio "$debuginfo_rpm" | cpio -idm --quiet
        )
        debug_file="$variant/debug/usr/lib/debug/usr/lib64/librpm.so.7.debug"
        [[ -e $debug_file ]] || fail "librpm debuginfo was not extracted for $filename"

        main_build_id=$(readelf -n "$libdir/librpm.so.7" | \
            awk '/Build ID:/ { print $3; exit }')
        debug_build_id=$(readelf -n "$debug_file" | \
            awk '/Build ID:/ { print $3; exit }')
        [[ -n $main_build_id && $main_build_id == "$debug_build_id" ]] || \
            fail "librpm/debuginfo Build ID mismatch for $filename"
    fi

    benchmark_variant "$variant" "$RESULT_DIR/$result_name" \
        "$debug_file" "$debuginfo_rpm" "$runtime_file" "$dso_name"
done

printf '\nResults:\n'
printf '  %s\n' "$RESULT_DIR"/*.tsv
