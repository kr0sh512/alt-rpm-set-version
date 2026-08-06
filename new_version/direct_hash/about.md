В этой реализации хэши хранятся напрямую, без Golomb-Rice-кодирования.

Set-строка целиком декодируется и проверяется при первом обращении. Как и в
`set9.c`, полностью декодированные массивы хэшей сохраняются в двух LRU-кэшах
по 512 записей — отдельно для первого и второго операнда.

Для 32-битного формата Base64
декодируется сразу в три `unsigned` за блок с одновременной проверкой порядка.
Большое понижение BPP выполняется radix-сортировкой, а не отдельным проходом на
каждый бит.

Последний запуск `taskset -c 2 python3 benchmark.py`:

```text
symbols=1000 required=500 bpp=32
implementation  set_chars  format
set9                3994  golomb/base62
direct              5340  D1/base64

operation                 set9       direct   direct/set9
set_fini only           147.45 us  105.09 us         0.71x
new+add (ctypes)        611.28 us  608.24 us         1.00x
new+add+fini (ctypes)   769.66 us  693.60 us         0.90x
rpmsetcmp cold          129.67 us  113.04 us         0.87x
rpmsetcmp warm            2.99 us    1.04 us         0.35x
```

Для разреженного сравнения
(`taskset -c 2 python3 benchmark.py --required 1`) получено
`83.22 us` на холодном кэше и `0.86 us` на прогретом: соответственно `0.88x`
и `1.02x` от времени `set9`.
