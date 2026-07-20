#!/bin/bash

rpm_safe_symlink_target()
{
    local root=$1 relative_path=$2 link_target=$3
    local canonical_root link_directory resolved_target

    canonical_root=$(realpath -m -- "$root") || return 1
    link_directory=$(realpath -m -- "$canonical_root/$(dirname -- "$relative_path")") || return 1

    if [[ $link_target == /* ]]; then
        resolved_target=$(realpath -m -- "$canonical_root/${link_target#/}") || return 1
    else
        resolved_target=$(realpath -m -- "$link_directory/$link_target") || return 1
    fi

    [[ $resolved_target == "$canonical_root" || $resolved_target == "$canonical_root"/* ]] || return 1

    if [[ $link_target == /* ]]; then
        realpath -m --relative-to="$link_directory" -- "$resolved_target"
    else
        printf '%s\n' "$link_target"
    fi
}

rpm_cpio_literal_pattern()
{
    local input=$1 output= character index

    [[ $input != *$'\n'* && $input != *$'\r'* ]] || return 1
    for ((index = 0; index < ${#input}; ++index)); do
        character=${input:index:1}
        case $character in
            '[') output+='[[]' ;;
            ']') output+='[]]' ;;
            '*') output+='[*]' ;;
            '?') output+='[?]' ;;
            '\\') output+='[\\]' ;;
            *) output+=$character ;;
        esac
    done
    printf '%s\n' "$output"
}

rpm_split_file_record()
{
    local record=$1 separator=$'\034'

    RPM_FILENAME=
    RPM_LINK_TARGET=
    [[ $record == *"$separator"* ]] || return 1
    RPM_FILENAME=${record%%"$separator"*}
    RPM_LINK_TARGET=${record#*"$separator"}
    [[ $RPM_LINK_TARGET != *"$separator"* ]] || return 1
    [[ $RPM_FILENAME != *$'\t'* && $RPM_FILENAME != *$'\n'* && $RPM_FILENAME != *$'\r'* ]] || return 1
    [[ $RPM_LINK_TARGET != *$'\t'* && $RPM_LINK_TARGET != *$'\n'* && $RPM_LINK_TARGET != *$'\r'* ]] || return 1
}

rpm_install_symlink_manifest()
{
    local root=$1 manifest=$2 log=$3 separator=$'\034'
    local canonical_root sorted row relative link_target safe_target destination parent existing

    canonical_root=$(realpath -m -- "$root") || return 1

    sorted=$(mktemp "${manifest}.sorted.XXXXXX") || return 1
    if ! awk -F '\t' -v separator="$separator" '
        NF >= 2 {
            path=$1
            depth=gsub(/\//, "/", path)
            printf "%08d%s%s\n", depth, separator, $0
        }
    ' "$manifest" | LC_ALL=C sort -t "$separator" -k1,1n -k2,2 >"$sorted"; then
        rm -f -- "$sorted"
        return 1
    fi

    while IFS=$separator read -r _depth row; do
        IFS=$'\t' read -r relative link_target <<<"$row"
        [[ -n $relative && -n $link_target ]] || continue
        destination=$canonical_root/$relative
        parent=$(realpath -m -- "$(dirname -- "$destination")") || {
            rm -f -- "$sorted"
            return 1
        }
        [[ $parent == "$canonical_root" || $parent == "$canonical_root"/* ]] || {
            printf 'escaping RPM symlink parent rejected: %q\n' "/$relative" >>"$log"
            rm -f -- "$sorted"
            return 1
        }
        if ! safe_target=$(rpm_safe_symlink_target "$canonical_root" "$relative" "$link_target"); then
            printf 'escaping RPM symlink rejected: %q -> %q\n' "/$relative" "$link_target" >>"$log"
            rm -f -- "$sorted"
            return 1
        fi
        mkdir -p -- "$parent" || {
            rm -f -- "$sorted"
            return 1
        }

        if [[ -L $destination ]]; then
            existing=$(readlink -- "$destination") || {
                rm -f -- "$sorted"
                return 1
            }
            if [[ $existing == "$safe_target" ]]; then
                continue
            fi
            printf 'conflicting RPM symlinks: %q -> %q and %q\n' \
                "/$relative" "$existing" "$safe_target" >>"$log"
            rm -f -- "$sorted"
            return 1
        elif [[ -e $destination ]]; then
            printf 'RPM symlink conflicts with existing path: %q\n' "/$relative" >>"$log"
            rm -f -- "$sorted"
            return 1
        fi
        if ! ln -s -- "$safe_target" "$destination"; then
            printf 'unable to create RPM symlink: %q -> %q\n' "/$relative" "$safe_target" >>"$log"
            rm -f -- "$sorted"
            return 1
        fi
    done <"$sorted"

    rm -f -- "$sorted"
}
