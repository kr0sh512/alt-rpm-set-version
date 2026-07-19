BEGIN {
    RS = ""
    FS = "\n"
    OFS = "\034"
}

{
    package = architecture = version = filename = md5 = ""
    has_provides = has_requires = 0
    dependency_field = ""

    for (i = 1; i <= NF; ++i) {
        if ($i ~ /^Package: /)
            package = substr($i, 10)
        else if ($i ~ /^Architecture: /)
            architecture = substr($i, 15)
        else if ($i ~ /^Version: /)
            version = substr($i, 10)
        else if ($i ~ /^Filename: /)
            filename = substr($i, 11)
        else if ($i ~ /^MD5Sum: /)
            md5 = substr($i, 9)

        if ($i ~ /^Provides: /)
            dependency_field = "provides"
        else if ($i ~ /^(Pre-Depends|Depends): /)
            dependency_field = "requires"
        else if ($i !~ /^[[:space:]]/)
            dependency_field = ""

        if (dependency_field == "provides" && index($i, "set:"))
            has_provides = 1
        if (dependency_field == "requires" && index($i, "set:"))
            has_requires = 1
    }

    if (package != "" && architecture != "" && version != "" && filename != "")
        print package, architecture, version, filename, md5, has_provides, has_requires
}
