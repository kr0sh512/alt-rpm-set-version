rg rpmsetcmp:

```
tools/setcmp.c
12-{
13:    int cmp = rpmsetcmp(s1, s2);
14-    switch (cmp) {

lib/depends.c
124-    if (aset && bset) {
125:	sense = rpmsetcmp(AEVR, BEVR);
126-	if (sense < -1) {

rpm-4_0.spec
1231-- set.c: Increased cache size from 160 to 256 slots, 75 percent hit ratio.
1232:- set.c: Implemented 4-byte and 8-byte steppers for rpmsetcmp main loop.
1233-
--
1451-* Mon Sep 20 2010 Alexey Tourbin <at@altlinux.ru> 4.0.4-alt98.47
1452:- set.c (rpmsetcmp): Fixed check for set2 decoding error.
1453-- brp-cleanup: Updated for /usr/lib64/perl5 and /usr/share/perl5.

build/reqprov.c
175-	if (aset && bset) {
176:	   sense = rpmsetcmp(Aevr, Bevr);
177-	   if (sense < -1)
```

---

rg set_new:

```
tools/mkset.c
11-    assert(bpp <= 32);
12:    struct set *set = set_new();
13-    char *line = NULL;
```

---

rg set_add:

```
tools/mkset.c
21-	   continue;
22:	set_add(set, line);
23-	added++;
```

---

rg set_fini:

```
tools/mkset.c
25-    assert(added > 0);
26:    const char *str = set_fini(set, bpp);
27-    assert(str);
```

---

rg set_free:
none

---

rg mkset:

```
rpm-4_0.spec
431-# set-version helpers
432:%rpmattr %_rpmlibdir/mkset
433-%rpmattr %_rpmlibdir/setcmp
--
1458-- rpmlibprov.c: Added rpmlib(SetVersions) feature.
1459:- %_rpmlibdir/mkset: Command-line helper for making set-versions.
1460-- lib.prov: Implemented soname set-versioning with exported symbols.

tools/Makefile.am
37-	relative \
38:	mkset \
39-	setcmp \
--
62-
63:mkset_SOURCES = mkset.c
64-setcmp_SOURCES = setcmp.c

autodeps/lib.prov.in
112-		Info "$f: $n symbols, $bpp bpp"
113:		set=$(printf '%s\n' "$sym" |"${RPMB_TOOLS_DIR-@RPMCONFIGDIR@}/mkset" "$bpp")
114-		printf '%s = %s\n' "$provname" "$set"

autodeps/lib.req.in
306-	#printf '%s\n' "$reqsym" |LC_ALL=C sort -c -u
307:	set=$(printf '%s\n' "$reqsym" |"${RPMB_TOOLS_DIR-@RPMCONFIGDIR@}/mkset" "$bpp")
308-	printf '%s >= %s\n' "$dep" "$set"
```
