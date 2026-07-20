#!/bin/bash
set -euo pipefail
export LC_ALL=C

MIRROR=https://ftp.altlinux.org/pub/distributions/ALTLinux
REQUIRE_LIMIT=100

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/arsv-set-check.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

for command in apt-get apt-cache awk sort join comm paste cc; do
    command -v "$command" >/dev/null || {
        printf 'required command not found: %s\n' "$command" >&2
        exit 2
    }
done

build_comparator()
{
    : >"$WORK/rpmlib.h"
    : >"$WORK/system.h"
    cat >"$WORK/compare.c" <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>

int rpmsetcmp(const char *, const char *);

int main(void)
{
    char *provided = NULL;
    char *required = NULL;
    size_t provided_size = 0;
    size_t required_size = 0;
    ssize_t provided_length;

    while ((provided_length = getline(&provided, &provided_size, stdin)) >= 0) {
        ssize_t required_length = getline(&required, &required_size, stdin);
        if (provided_length < 1 || required_length < 1) {
            free(provided);
            free(required);
            return 2;
        }
        if (provided[provided_length - 1] == '\n')
            provided[provided_length - 1] = '\0';
        if (required[required_length - 1] == '\n')
            required[required_length - 1] = '\0';

        printf("%d\n", rpmsetcmp(provided, required));
    }
    free(provided);
    free(required);
    return 0;
}
EOF

    cc -O2 -std=gnu11 -D_GNU_SOURCE -I"$WORK" \
        -include "$SCRIPT_DIR/newset_compat.h" \
        "$REPO_ROOT/reimplement/newset.c" "$WORK/compare.c" \
        -o "$WORK/compare"
}

fetch_metadata()
{
    mkdir -p "$WORK/apt/lists/partial" "$WORK/apt/archives/partial"
    : >"$WORK/apt/apt.conf"
    : >"$WORK/apt/status"
    printf '%s\n' \
        "rpm [alt] $MIRROR Sisyphus/x86_64 classic" \
        "rpm [alt] $MIRROR Sisyphus/noarch classic" \
        >"$WORK/apt/sources.list"

    local options=(
        -o 'Dir::Etc::main=-'
        -o 'Dir::Etc::parts=-'
        -o "Dir::Etc::sourcelist=$WORK/apt/sources.list"
        -o 'Dir::Etc::sourceparts=-'
        -o "Dir::State::lists=$WORK/apt/lists"
        -o "Dir::State::status=$WORK/apt/status"
        -o "Dir::Cache::archives=$WORK/apt/archives"
        -o "Dir::Cache::pkgcache=$WORK/apt/pkgcache.bin"
        -o "Dir::Cache::srcpkgcache=$WORK/apt/srcpkgcache.bin"
    )

    APT_CONFIG="$WORK/apt/apt.conf" apt-get "${options[@]}" -qq update
    APT_CONFIG="$WORK/apt/apt.conf" apt-cache "${options[@]}" dumpavail \
        >"$WORK/available.txt"
}

extract_relations()
{
    awk '
    function emit(kind, text,    count, parts, i, item, capability, encoded) {
        count = split(text, parts, /,[[:space:]]*/)
        for (i = 1; i <= count; i++) {
            item = parts[i]
            if (kind == "P" && item !~ /[[:space:]]+\(?=[[:space:]]+set:[[:alnum:]]+\)?$/)
                continue
            if (kind == "R" && item !~ /[[:space:]]+\(?>=[[:space:]]+set:[[:alnum:]]+\)?$/)
                continue

            capability = item
            sub(/[[:space:]]+\(?>?=[[:space:]]+set:[[:alnum:]]+\)?$/, "", capability)
            encoded = item
            sub(/^.*[[:space:]]+\(?>?=[[:space:]]+/, "", encoded)
            sub(/\)$/, "", encoded)
            printf "%s\t%s\t%s\t%s\n", kind, package, capability, encoded
        }
    }
    function flush() {
        if (field != "")
            emit(field, value)
        field = ""
        value = ""
    }
    {
        if ($0 ~ /^[[:space:]]/ && field != "") {
            line = $0
            sub(/^[[:space:]]+/, "", line)
            value = value " " line
            next
        }

        flush()
        if ($0 ~ /^Package: /)
            package = substr($0, 10)
        else if ($0 ~ /^Provides: /) {
            field = "P"
            value = substr($0, 11)
        } else if ($0 ~ /^(Depends|Pre-Depends): /) {
            field = "R"
            value = substr($0, index($0, ":") + 2)
        } else if ($0 == "")
            package = ""
    }
    END { flush() }
    ' "$WORK/available.txt" >"$WORK/relations.tsv"
}

check_relations()
{
    local tab=$'\t' result

    awk -F '\t' '$1 == "P" { print $3 "\t" $2 "\t" $4 }' \
        "$WORK/relations.tsv" | LC_ALL=C sort -t "$tab" -k1,1 \
        >"$WORK/providers.tsv"
    awk -F '\t' -v limit="$REQUIRE_LIMIT" \
        '$1 == "R" { print $3 "\t" $2 "\t" $4; if (++n == limit) exit }' \
        "$WORK/relations.tsv" | LC_ALL=C sort -u \
        >"$WORK/requirements.tsv"

    join -t "$tab" "$WORK/providers.tsv" "$WORK/requirements.tsv" \
        >"$WORK/pairs.tsv"
    : >"$WORK/paired.tsv"
    : >"$WORK/compatible.tsv"
    : >"$WORK/decode-errors.tsv"

    awk -F '\t' '{ print $3; print $5 }' "$WORK/pairs.tsv" \
        | "$WORK/compare" >"$WORK/results.txt"
    paste "$WORK/pairs.tsv" "$WORK/results.txt" >"$WORK/compared.tsv"

    while IFS=$'\t' read -r capability provider provided consumer required result; do
        printf '%s\t%s\t%s\n' "$capability" "$consumer" "$required" \
            >>"$WORK/paired.tsv"
        case $result in
            0|1)
                printf '%s\t%s\t%s\n' "$capability" "$consumer" "$required" \
                    >>"$WORK/compatible.tsv"
                ;;
            -3|-4)
                printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$consumer" "$provider" "$capability" "$provided" "$required" "$result" \
                    >>"$WORK/decode-errors.tsv"
                ;;
        esac
    done <"$WORK/compared.tsv"

    for file in requirements paired compatible; do
        LC_ALL=C sort -u "$WORK/$file.tsv" -o "$WORK/$file.tsv"
    done
    comm -23 "$WORK/requirements.tsv" "$WORK/paired.tsv" >"$WORK/no-provider.tsv"
    comm -23 "$WORK/paired.tsv" "$WORK/compatible.tsv" >"$WORK/incompatible.tsv"
}

count_lines()
{
    awk 'END { print NR + 0 }' "$1"
}

build_comparator
printf 'Updating Sisyphus metadata...\n'
fetch_metadata
extract_relations
check_relations

requirements=$(count_lines "$WORK/requirements.tsv")
compatible=$(count_lines "$WORK/compatible.tsv")
no_provider=$(count_lines "$WORK/no-provider.tsv")
incompatible=$(count_lines "$WORK/incompatible.tsv")
decode_errors=$(count_lines "$WORK/decode-errors.tsv")
comparisons=$(count_lines "$WORK/pairs.tsv")

printf '\nrequirements: %s\n' "$requirements"
printf 'comparisons:  %s\n' "$comparisons"
printf 'compatible:   %s\n' "$compatible"
printf 'no provider:  %s\n' "$no_provider"
printf 'incompatible: %s\n' "$incompatible"
printf 'decode errors:%s\n' "$decode_errors"

if ((no_provider)); then
    printf '\nRequirements without a provider (first 10):\n'
    sed -n '1,10p' "$WORK/no-provider.tsv"
fi
if ((incompatible)); then
    printf '\nUnsatisfied set requirements (first 10):\n'
    sed -n '1,10p' "$WORK/incompatible.tsv"
fi

((requirements > 0)) || {
    printf 'no set requirements found\n' >&2
    exit 2
}
((no_provider == 0 && incompatible == 0 && decode_errors == 0))
