rg rpmsetcmp:

```
tools/setcmp.c
12-{
13: int cmp = rpmsetcmp(s1, s2);
14- switch (cmp) {

alt/rpm.spec
709-_ Mon Nov 25 2019 Andrew Savchenko <bircoph@altlinux.org> 4.13.0.1-alt15
710:- Support rpmsetcmp profiling on E2K.
711-
--
713-- Added triggers circumvention for packagekit offline update (by Aleksei Nikiforov).
714:- Imported rpmsetcmp optimization from rpm-build.
715-
--
1094-- set.c: Increased cache size from 160 to 256 slots, 75 percent hit ratio.
1095:- set.c: Implemented 4-byte and 8-byte steppers for rpmsetcmp main loop.
1096-
--
1314-_ Mon Sep 20 2010 Alexey Tourbin <at@altlinux.ru> 4.0.4-alt98.47
1315:- set.c (rpmsetcmp): Fixed check for set2 decoding error.
1316-- brp-cleanup: Updated for /usr/lib64/perl5 and /usr/share/perl5.

lib/rpmds.c
1087- if (aset && bset) {
1088: sense = rpmsetcmp(AEVR, BEVR);
1089- if (sense < -1) {

---

rg set_new:
none

---

rg set_add
none

---

rg set_fini:
none

---

rg set_free:
none
```

---

rg setcmp:

```
Makefile.am
218-
219:noinst_PROGRAMS = setcmp
220:setcmp_SOURCES = tools/setcmp.c
221:setcmp_LDADD = lib/librpm.la
222-

alt/rpm.spec
311-rpmquery -a --requires |fgrep '= set:' |sort >R
312:join -o 1.3,2.3 P R |shuf >setcmp-data
313:time ./setcmp <setcmp-data >/dev/null
314-rm lib/set.lo lib/librpm.la
--
318-%if_with profile
319:time ./setcmp <setcmp-data >/dev/null
320-rm lib/set.lo lib/librpm.la
321-%make_build -C lib set.lo librpm.la CFLAGS="$set_c_cflags -fprofile-generate"
322:./setcmp <setcmp-data >/dev/null
323-%ifnarch %e2k
--
332-%make_build
333:time ./setcmp <setcmp-data >/dev/null
334-
```
