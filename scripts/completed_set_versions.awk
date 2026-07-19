BEGIN { FS = "\t" }

NF == 11 && $2 == "START" {
    version[$3 "\034" $4] = $11
}

NF == 11 && $2 == "SUMMARY" && $11 ~ /(^|; )complete=1$/ && \
    version[$3 "\034" $4] != "" {
    print $3 "\t" $4 "\t" version[$3 "\034" $4]
}
