В этой реалзации попытка хранить хэш функции напрямую, без кодирования

В теории это должно увеличить размер set-строки ~x2, но

- позволить индексироваться по элементам хэша
- кратно сократить время на дешифровку
- кэшировать можно соответствия индекс - значение, чтобы не работать с битами (может и не стоит того)
  - стоит этого

```text
symbols=1000 required=500 bpp=32
implementation  set_chars  format
set9                3994  golomb/base62
direct              5340  D1/base64

operation                 set9       direct   direct/set9
set_fini only           150.44 us   99.28 us         0.66x
new+add+fini           1034.22 us  949.62 us         0.92x
rpmsetcmp cold          292.86 us  210.30 us         0.72x
rpmsetcmp warm            5.89 us   23.02 us         3.91x
```
