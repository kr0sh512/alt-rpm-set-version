попытка воссоздания алгоритма с roaring bitmap

Даже эффективней в создании, но проигрывает в дешифровке (сравнении) и занимаемому месту

Кратно выше становится сама длина строки

```text
symbols=1000 required=500 bpp=32
implementation  set_chars  format
set9                3994  golomb
bitmap             19924  R1
```

```text
operation                 set9       bitmap   bitmap/set9
set_fini only           266.24 us  202.89 us         0.76x
new+add+fini           1696.05 us 1600.44 us         0.94x
rpmsetcmp cold          293.87 us 1321.17 us         4.50x
rpmsetcmp warm            5.24 us  399.20 us        76.14x
```

### Использование zstd

```text
symbols=1000 required=500 bpp=32
implementation  set_chars  format
set9                3994  golomb
bitmap             14126  R2
```

без zstd строка становится ~40% длинее

```text
operation                 set9       bitmap   bitmap/set9
set_fini only           171.84 us  314.20 us         1.83x
new+add+fini           1185.33 us 1726.32 us         1.46x
rpmsetcmp cold          285.04 us 1412.84 us         4.96x
rpmsetcmp warm            4.82 us  490.94 us       101.86x
```
