#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
NEWSET_C="$REPO_ROOT/reimplement/newset.c"
NEWSET_COMPAT="$SCRIPT_DIR/newset_compat.h"
NEWSET_WRAPPER="$SCRIPT_DIR/newset_mkset.c"
PACKAGE_PARSER="$SCRIPT_DIR/parse_sisyphus_packages.awk"
RESUME_PARSER="$SCRIPT_DIR/completed_set_versions.awk"
PAYLOAD_HELPERS="$SCRIPT_DIR/rpm_payload_safety.sh"

# shellcheck source=scripts/rpm_payload_safety.sh
source "$PAYLOAD_HELPERS"

MIRROR=https://ftp.altlinux.org/pub/distributions/ALTLinux
REPORT=sisyphus-set-compare.tsv
LIMIT=
ALL=0
RESUME=0
KEEP_WORK=0
BUILD_MKSET=
PACKAGES=()

usage()
{
    cat <<'EOF'
Usage:
  compare_sisyphus_set_versions.sh --all [OPTIONS]
  compare_sisyphus_set_versions.sh --limit N [OPTIONS]
  compare_sisyphus_set_versions.sh --package NAME [--package NAME ...] [OPTIONS]
  compare_sisyphus_set_versions.sh --build-mkset PATH

Scopes:
  --all               Process every x86_64/noarch package record.
  --limit N           Process only the first N records (testing).
  --package NAME      Process one named package; may be repeated (testing).

Options:
  --report FILE       Streaming TSV report (default: sisyphus-set-compare.tsv).
  --resume            Skip completed package/architecture/version records.
  --keep-work         Preserve the temporary working directory.
  --mirror URL        ALT repository root.
  --build-mkset PATH  Build only the mkset-compatible newset.c wrapper and exit.
  -h, --help          Show this help.

The report gets a START row before processing and DEPENDENCY/SUMMARY rows are
appended after every package, so it can be monitored while the script runs.
No repository-wide run is possible without the explicit --all option.
EOF
}

die()
{
    printf 'error: %s\n' "$*" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --all) ALL=1; shift ;;
        --limit) (($# >= 2)) || die '--limit requires N'; LIMIT=$2; shift 2 ;;
        --package) (($# >= 2)) || die '--package requires NAME'; PACKAGES+=("$2"); shift 2 ;;
        --report) (($# >= 2)) || die '--report requires FILE'; REPORT=$2; shift 2 ;;
        --resume) RESUME=1; shift ;;
        --keep-work) KEEP_WORK=1; shift ;;
        --mirror) (($# >= 2)) || die '--mirror requires URL'; MIRROR=${2%/}; shift 2 ;;
        --build-mkset) (($# >= 2)) || die '--build-mkset requires PATH'; BUILD_MKSET=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

build_mkset()
(
{
    local output=$1 build
    build=$(mktemp -d "${TMPDIR:-/tmp}/arsv-newset-build.XXXXXX")
    trap 'rm -rf "$build"' EXIT
    : >"$build/rpmlib.h"
    : >"$build/system.h"
    mkdir -p "$(dirname -- "$output")"

    "${CC:-cc}" -O2 -std=gnu11 -D_GNU_SOURCE \
        -I"$build" -include "$NEWSET_COMPAT" \
        -c "$NEWSET_C" -o "$build/newset.o"
    "${CC:-cc}" -O2 -std=gnu11 -D_GNU_SOURCE \
        "$NEWSET_WRAPPER" "$build/newset.o" -o "$output"
    chmod 755 "$output"
}
)

if [[ -n $BUILD_MKSET ]]; then
    build_mkset "$BUILD_MKSET"
    exit 0
fi

[[ $MIRROR == http://* || $MIRROR == https://* ]] || die '--mirror must use http:// or https://'
[[ $MIRROR != *[$'\t\r\n ']* ]] || die '--mirror must not contain whitespace'
for package in "${PACKAGES[@]}"; do
    [[ $package =~ ^[A-Za-z0-9][A-Za-z0-9+_.-]*$ ]] || die "invalid package name: $package"
done

scope_count=$ALL
[[ -n $LIMIT ]] && scope_count=$((scope_count + 1))
((${#PACKAGES[@]} > 0)) && scope_count=$((scope_count + 1))
((scope_count == 1)) || die 'use --all, --limit, or --package (exactly one scope)'
if [[ -n $LIMIT && ! $LIMIT =~ ^[1-9][0-9]*$ ]]; then
    die '--limit must be a positive integer'
fi

for command in apt-get apt-cache rpm rpmquery rpm2cpio cpio curl md5sum awk sed sort find cp cc realpath; do
    command -v "$command" >/dev/null || die "required command not found: $command"
done

rpmlibdir=$(rpm --eval '%_rpmlibdir')
[[ -x $rpmlibdir/find-provides ]] || die "$rpmlibdir/find-provides is missing (install rpm-build)"
[[ -x $rpmlibdir/find-requires ]] || die "$rpmlibdir/find-requires is missing (install rpm-build)"

case $REPORT in
    /*) ;;
    *) REPORT="$PWD/$REPORT" ;;
esac
mkdir -p "$(dirname -- "$REPORT")"
if [[ ! -s $REPORT ]]; then
    printf 'timestamp\trecord\tpackage\tarchitecture\tstatus\tside\tcapability\toperator\texpected\tgenerated\tdetail\n' >"$REPORT"
fi

clean_field()
{
    local value=${1-}
    value=${value//$'\t'/ }
    value=${value//$'\r'/ }
    value=${value//$'\n'/ }
    printf '%s' "$value"
}

report_row()
{
    local fields=() field row
    (($# == 11)) || {
        printf 'internal error: report row has %s fields, expected 11\n' "$#" >&2
        return 1
    }
    for field in "$@"; do
        fields+=("$(clean_field "$field")")
    done
    if [[ ${fields[1]} == SUMMARY ]]; then
        case ${fields[10]} in
            '') fields[10]='complete=1' ;;
            *'; ') fields[10]="${fields[10]}complete=1" ;;
            *) fields[10]="${fields[10]}; complete=1" ;;
        esac
    fi
    row=${fields[0]}
    for field in "${fields[@]:1}"; do
        printf -v row '%s\t%s' "$row" "$field"
    done
    printf '%s\n' "$row" >>"$REPORT"
}

timestamp()
{
    date -u '+%Y-%m-%dT%H:%M:%SZ'
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/arsv-sisyphus-sets.XXXXXX")
cleanup()
{
    if ((KEEP_WORK)); then
        printf 'work directory preserved: %s\n' "$WORK" >&2
    else
        rm -rf "$WORK"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$WORK/apt/lists/partial" "$WORK/apt/archives/partial" "$WORK/tools"
: >"$WORK/apt/apt.conf"
: >"$WORK/apt/status"
printf '%s\n' \
    "rpm [alt] $MIRROR Sisyphus/x86_64 classic" \
    "rpm [alt] $MIRROR Sisyphus/noarch classic" >"$WORK/apt/sources.list"

APT_OPTIONS=(
    -o 'Dir::Etc::main=-'
    -o 'Dir::Etc::parts=-'
    -o "Dir::Etc::sourcelist=$WORK/apt/sources.list"
    -o 'Dir::Etc::sourceparts=-'
    -o 'Dir::Etc::preferences=-'
    -o 'Dir::Etc::preferencesparts=-'
    -o "Dir::State::lists=$WORK/apt/lists"
    -o "Dir::State::status=$WORK/apt/status"
    -o "Dir::Cache::archives=$WORK/apt/archives"
    -o "Dir::Cache::pkgcache=$WORK/apt/pkgcache.bin"
    -o "Dir::Cache::srcpkgcache=$WORK/apt/srcpkgcache.bin"
)

apt_get()
{
    APT_CONFIG="$WORK/apt/apt.conf" apt-get "${APT_OPTIONS[@]}" "$@"
}

apt_cache()
{
    APT_CONFIG="$WORK/apt/apt.conf" apt-cache "${APT_OPTIONS[@]}" "$@"
}

NEW_MKSET="$WORK/new-mkset"
build_mkset "$NEW_MKSET"
cp -as "$rpmlibdir"/. "$WORK/tools/"
rm -f "$WORK/tools/mkset"
ln -s "$NEW_MKSET" "$WORK/tools/mkset"

report_row "$(timestamp)" RUN - - start - - - - - "updating isolated Sisyphus indexes"
if ! apt_get update >"$WORK/apt-update.log" 2>&1; then
    detail=$(sed -n '/./{p;q;}' "$WORK/apt-update.log")
    report_row "$(timestamp)" RUN - - apt_update_error - - - - - "$detail"
    exit 1
fi

QUEUE="$WORK/packages.tsv"
if ! apt_cache dumpavail |
    awk -f "$PACKAGE_PARSER" |
    LC_ALL=C sort -t$'\034' -k1,1 -k2,2 >"$QUEUE"; then
    report_row "$(timestamp)" RUN - - metadata_error - - - - - 'unable to parse apt-cache dumpavail'
    exit 1
fi

SELECTED="$WORK/selected.tsv"
if ((${#PACKAGES[@]})); then
    : >"$SELECTED"
    for package in "${PACKAGES[@]}"; do
        if ! awk -F '\034' -v package="$package" '$1 == package { print; found=1 } END { exit !found }' \
            "$QUEUE" >>"$SELECTED"; then
            die "package not found in Sisyphus x86_64/noarch: $package"
        fi
    done
elif [[ -n $LIMIT ]]; then
    sed -n "1,${LIMIT}p" "$QUEUE" >"$SELECTED"
else
    cp "$QUEUE" "$SELECTED"
fi

selected_count=$(wc -l <"$SELECTED")
report_row "$(timestamp)" RUN - - ready - - - - - "selected packages: $selected_count"
printf 'selected packages: %s; report: %s\n' "$selected_count" "$REPORT"

declare -A COMPLETED=()
if ((RESUME)); then
    while IFS=$'\t' read -r package architecture version; do
        COMPLETED["$package"$'\t'"$architecture"$'\t'"$version"]=1
    done < <(awk -f "$RESUME_PARSER" "$REPORT")
fi

first_log_line()
{
    local file=$1
    [[ -s $file ]] || return 0
    sed -n '/./{p;q;}' "$file" | cut -c1-500
}

last_log_line()
{
    local file=$1
    [[ -s $file ]] || return 0
    awk 'NF { line=$0 } END { print line }' "$file" | cut -c1-500
}

extract_rpm()
{
    local package_file=$1 root=$2 log=$3 mode=$4
    local metadata="$root/../.arsv-file-metadata"
    local patterns="$root/../.arsv-nonlink-patterns"
    local symlinks="$root/../.arsv-symlinks"
    local field_separator=$'\034' record_separator=$'\035' record
    local filename link_target relative archive_name
    local query_format="[%{FILENAMES}${field_separator}%{FILELINKTOS}${record_separator}]"

    if ! rpmquery -p --qf "$query_format" \
        "$package_file" >"$metadata" 2>>"$log"; then
        return 1
    fi
    case $mode in
        symlinks) : >"$symlinks" ;;
        files) : >"$patterns" ;;
        *) printf 'internal error: invalid RPM extraction mode: %s\n' "$mode" >>"$log"; return 1 ;;
    esac

    while IFS= read -r -d "$record_separator" record; do
        if ! rpm_split_file_record "$record"; then
            printf 'unsupported RPM filename or symlink record\n' >>"$log"
            return 1
        fi
        filename=$RPM_FILENAME
        link_target=$RPM_LINK_TARGET
        [[ -n $filename && $filename == /* ]] || {
            printf 'unsafe RPM filename: %q\n' "$filename" >>"$log"
            return 1
        }
        relative=${filename#/}
        if [[ $relative == '..' || $relative == ../* || $relative == */../* || $relative == */.. ]]; then
            printf 'unsafe RPM filename: %q\n' "$filename" >>"$log"
            return 1
        fi

        if [[ -n $link_target && $link_target != '(none)' ]]; then
            if [[ $mode == symlinks ]]; then
                printf '%s\t%s\n' "$relative" "$link_target" >>"$symlinks"
            fi
        elif [[ $mode == files ]]; then
            archive_name=./$relative
            if ! rpm_cpio_literal_pattern "$archive_name" >>"$patterns"; then
                printf 'unsupported RPM filename: %q\n' "$filename" >>"$log"
                return 1
            fi
        fi
    done <"$metadata"

    if [[ $mode == files ]]; then
        if ! rpm2cpio "$package_file" |
            cpio -it --quiet --no-absolute-filenames 2>>"$log" |
            while IFS= read -r member; do
                case $member in
                    /*|../*|*/../*|*/..)
                        printf 'unsafe cpio member: %q\n' "$member" >>"$log"
                        exit 1
                        ;;
                esac
            done; then
            return 1
        fi

        if [[ -s $patterns ]]; then
            if ! rpm2cpio "$package_file" |
                (cd "$root" && cpio -idm --quiet --no-absolute-filenames -E "$patterns") \
                    2>>"$log"; then
                return 1
            fi
        fi
        return 0
    fi

    cat "$symlinks"
}

normalize_expected()
{
    local side=$1 package_file=$2 output=$3
    if [[ $side == provides ]]; then
        rpmquery -p --qf \
            '[%{PROVIDENAME}\t%{PROVIDEFLAGS:depflags}\t%{PROVIDEVERSION}\n]' \
            "$package_file"
    else
        rpmquery -p --qf \
            '[%{REQUIRENAME}\t%{REQUIREFLAGS:depflags}\t%{REQUIREVERSION}\n]' \
            "$package_file"
    fi | awk -F '\t' '$3 ~ /^set:/ { print $1 "\t" $2 "\t" $3 }' |
        LC_ALL=C sort -u >"$output"
}

normalize_generated()
{
    local side=$1 input=$2 output=$3
    if [[ $side == provides ]]; then
        awk '$2 == "=" && $3 ~ /^set:/ { print $1 "\t" $2 "\t" $3 }' "$input"
    else
        awk '$2 == ">=" && $3 ~ /^set:/ { print $1 "\t" $2 "\t" $3 }' "$input"
    fi | LC_ALL=C sort -u >"$output"
}

compare_side()
{
    local side=$1 package=$2 architecture=$3 expected=$4 generated=$5 output=$6
    if ! awk -F '\t' -v OFS='\034' '
        FILENAME == ARGV[1] { key=$1 "\034" $2; expected[key]=$3; keys[key]=1; next }
        { key=$1 "\034" $2; generated[key]=$3; keys[key]=1 }
        END {
            for (key in keys) {
                split(key, parts, "\034")
                e=expected[key]; g=generated[key]
                if (e == "") status="extra_generated"
                else if (g == "") status="missing_generated"
                else if (e == g) status="match"
                else status="mismatch"
                print status, parts[1], parts[2], e, g
            }
        }
    ' "$expected" "$generated" |
        LC_ALL=C sort -t$'\034' -k2,2 -k3,3 >"$output"; then
        return 1
    fi

    # A non-whitespace separator preserves empty expected/generated fields.
    while IFS=$'\034' read -r status capability operator expected_set generated_set; do
        [[ -n $status ]] || continue
        report_row "$(timestamp)" DEPENDENCY "$package" "$architecture" "$status" \
            "$side" "$capability" "$operator" "$expected_set" "$generated_set" -
    done <"$output"
}

process_package()
{
    local package=$1 architecture=$2 version=$3 filename=$4 md5=$5
    local has_provides=$6 has_requires=$7
    local key=$package$'\t'$architecture$'\t'$version
    if [[ -n ${COMPLETED[$key]-} ]]; then
        printf 'skip completed: %s.%s\n' "$package" "$architecture"
        return 0
    fi

    report_row "$(timestamp)" START "$package" "$architecture" processing - - - - - "$version"
    printf 'process: %s.%s\n' "$package" "$architecture"

    if ((has_provides == 0 && has_requires == 0)); then
        report_row "$(timestamp)" SUMMARY "$package" "$architecture" no_set_metadata \
            - - - - - 'APT metadata contains no set: Provides/Requires'
        return 0
    fi

    local package_work="$WORK/package"
    rm -rf "$package_work"
    mkdir -p "$package_work/root" "$package_work/archives/partial"
    local target="$package_work/target.rpm"
    local url="$MIRROR/Sisyphus/files/$architecture/RPMS/$filename"
    local log="$package_work/package.log"

    if ! curl --silent --show-error -fL --retry 3 --retry-delay 2 \
        -o "$target" "$url" >"$log" 2>&1; then
        report_row "$(timestamp)" SUMMARY "$package" "$architecture" download_error \
            - - - - - "$(first_log_line "$log")"
        return 0
    fi
    if [[ -n $md5 ]]; then
        local actual_md5
        if ! actual_md5=$(md5sum "$target" | awk '{print $1}'); then
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" checksum_error \
                - - - "$md5" - "$filename"
            return 0
        fi
        if [[ $actual_md5 != "$md5" ]]; then
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" checksum_error \
                - - - "$md5" "$actual_md5" "$filename"
            return 0
        fi
    fi

    local -a payload_rpms=("$target")
    if ((has_requires)); then
        mkdir -p "$package_work/root/var/lib/rpm"
        if ! rpm --root "$package_work/root" --initdb >>"$log" 2>&1; then
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" rpmdb_error \
                - - - - - "$(last_log_line "$log")"
            return 0
        fi
        if ! apt_get -y -d \
            -o "Dir::Cache::archives=$package_work/archives" \
            -o "RPM::RootDir=$package_work/root" \
            install "$target" >>"$log" 2>&1; then
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" dependency_download_error \
                - - - - - "$(last_log_line "$log")"
            return 0
        fi
        local dependency_rpm
        for dependency_rpm in "$package_work"/archives/*.rpm; do
            [[ -f $dependency_rpm ]] || continue
            payload_rpms+=("$dependency_rpm")
        done
    fi

    local payload_rpm extraction_status
    local symlink_manifest="$package_work/symlinks.tsv"
    : >"$symlink_manifest"
    for payload_rpm in "${payload_rpms[@]}"; do
        if ! extract_rpm "$payload_rpm" "$package_work/root" "$log" symlinks \
            >>"$symlink_manifest"; then
            if [[ $payload_rpm == "$target" ]]; then
                extraction_status=extract_error
            else
                extraction_status=dependency_extract_error
            fi
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" "$extraction_status" \
                - - - - - "$(last_log_line "$log")"
            return 0
        fi
    done
    if ! rpm_install_symlink_manifest "$package_work/root" "$symlink_manifest" "$log"; then
        if ((has_requires)); then
            extraction_status=dependency_extract_error
        else
            extraction_status=extract_error
        fi
        report_row "$(timestamp)" SUMMARY "$package" "$architecture" "$extraction_status" \
            - - - - - "$(last_log_line "$log")"
        return 0
    fi

    for payload_rpm in "${payload_rpms[@]}"; do
        if ! extract_rpm "$payload_rpm" "$package_work/root" "$log" files; then
            if [[ $payload_rpm == "$target" ]]; then
                extraction_status=extract_error
            else
                extraction_status=dependency_extract_error
            fi
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" "$extraction_status" \
                - - - - - "$(last_log_line "$log")"
            return 0
        fi
    done

    if ! rpmquery -p --qf '[%{FILENAMES}\n]' "$target" |
        awk -v root="$package_work/root" \
            '{ if (substr($0,1,1)=="/") print root $0; else print root "/" $0 }' |
        while IFS= read -r path; do
            if [[ -e $path || -L $path ]]; then
                printf '%s\n' "$path"
            fi
        done >"$package_work/files"; then
        report_row "$(timestamp)" SUMMARY "$package" "$architecture" file_list_error \
            - - - - - 'rpmquery failed while reading package filenames'
        return 0
    fi

    local scanner_env=(
        "RPM_BUILD_ROOT=$package_work/root"
        "RPM_SUBPACKAGE_NAME=$package"
        "RPM_PACKAGE_NAME=$package"
        "RPMB_TOOLS_DIR=$WORK/tools"
        "RPMB_LIB_DIR=$rpmlibdir"
        "RPMB_AUTODEPS_DIR=$rpmlibdir"
        'RPM_FINDPROV_METHOD=none,lib'
        'RPM_FINDREQ_METHOD=none,lib'
        'RPM_SCRIPTS_DEBUG=0'
    )

    local side generator expected generated comparison
    local total_mismatches=0 detail=''
    for side in provides requires; do
        [[ $side == provides && $has_provides == 1 ]] ||
        [[ $side == requires && $has_requires == 1 ]] || continue

        generator="$package_work/generated.$side.raw"
        expected="$package_work/expected.$side.tsv"
        generated="$package_work/generated.$side.tsv"
        comparison="$package_work/comparison.$side.tsv"
        if ! normalize_expected "$side" "$target" "$expected"; then
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" metadata_error \
                "$side" - - - - 'rpmquery failed while reading set dependencies'
            return 0
        fi

        if [[ $side == provides ]]; then
            if ! env "${scanner_env[@]}" "$rpmlibdir/find-provides" \
                <"$package_work/files" >"$generator" 2>"$package_work/$side.log"; then
                report_row "$(timestamp)" SUMMARY "$package" "$architecture" generator_error \
                    "$side" - - - - "$(first_log_line "$package_work/$side.log")"
                return 0
            fi
        else
            if ! env "${scanner_env[@]}" "$rpmlibdir/find-requires" \
                <"$package_work/files" >"$generator" 2>"$package_work/$side.log"; then
                report_row "$(timestamp)" SUMMARY "$package" "$architecture" generator_error \
                    "$side" - - - - "$(first_log_line "$package_work/$side.log")"
                return 0
            fi
        fi

        if ! normalize_generated "$side" "$generator" "$generated"; then
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" generator_output_error \
                "$side" - - - - 'unable to normalize generator output'
            return 0
        fi
        if ! compare_side "$side" "$package" "$architecture" "$expected" "$generated" "$comparison"; then
            report_row "$(timestamp)" SUMMARY "$package" "$architecture" comparison_error \
                "$side" - - - - 'unable to compare generated dependencies'
            return 0
        fi

        local expected_count generated_count mismatch_count extra_count
        expected_count=$(wc -l <"$expected")
        generated_count=$(wc -l <"$generated")
        mismatch_count=$(awk -F '\034' \
            '$1 == "mismatch" || $1 == "missing_generated" { count++ } END { print count+0 }' \
            "$comparison")
        extra_count=$(awk -F '\034' '$1 == "extra_generated" { count++ } END { print count+0 }' \
            "$comparison")
        total_mismatches=$((total_mismatches + mismatch_count + extra_count))
        detail+="$side expected=$expected_count generated=$generated_count differences=$mismatch_count extras=$extra_count; "
    done

    local status=match
    ((total_mismatches == 0)) || status=mismatch
    report_row "$(timestamp)" SUMMARY "$package" "$architecture" "$status" - - - - - "$detail"
    sync -f "$REPORT" 2>/dev/null || true
}

while IFS=$'\034' read -r package architecture version filename md5 has_provides has_requires extra; do
    if [[ -n ${extra-} || -z $package || -z $architecture || -z $version || -z $filename ||
          ! $has_provides =~ ^[01]$ || ! $has_requires =~ ^[01]$ ]]; then
        report_row "$(timestamp)" RUN "$package" "$architecture" queue_error - - - - - \
            'invalid package queue record'
        continue
    fi
    process_package "$package" "$architecture" "$version" "$filename" "$md5" \
        "$has_provides" "$has_requires"
done <"$SELECTED"

report_row "$(timestamp)" RUN - - complete - - - - - "processed selection: $selected_count"
printf 'complete; report: %s\n' "$REPORT"
