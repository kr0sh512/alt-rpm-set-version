# alt-rpm-set-version

Research on ALT Linux RPM set.c and dependency comparison using encoded symbol sets.

- [Docs about current set:version implementation](Docs/set:version.md)

# Compare results

| command                     | original set.c | orig set (w/out optim) | set9.c |
| :-------------------------- | :------------: | :--------------------: | :----: |
| -s check                    |       x1       |         x3.134         | x1.041 |
| -s autoremove               |       x1       |         x2.564         | x1.053 |
| -s install rpm-build        |       x1       |         x2.578         | x1.036 |
| -s install openuds-server   |       x1       |         x1.946         | x1.094 |
| -s install password-store   |       x1       |         x2.941         | x1.073 |
| --enable-upgrade -s upgrade |       x1       |         x1.351         | x1.100 |
| -s dist-upgrade             |       x1       |         x1.204         | x1.044 |

Более подробные данные сравнения приведены [здесь](Docs/test_result.md)

# Testing

Для проверки используются четыре скрипта из каталога `scripts`.

## Случайные наборы символов

### `scripts/rpmsetinit/compare_random_sets.py`

Проверяет совместимость кодировщиков: компилирует `set.c` и
`reimplement/newset.c`, генерирует случайные списки символов и сравнивает
полученные set-строки при случайном `bpp` от 10 до 32.

Пути до наобходимых файлов приведены в коде скрипта.
Минимальный запуск из корня репозитория:

```sh
python3 scripts/rpmsetinit/compare_random_sets.py
```

Нужны Python 3 и C-компилятор `cc`. Скрипт работает непрерывно до `Ctrl-C`;
входы, на которых результаты различаются, сохраняются в
`scripts/rpmsetinit/error/`.

### `scripts/rpmsetcmp/compare_random_sets.py`

Проверяет совместимость `rpmsetcmp()` из `set.c` и
`reimplement/newset.c`. Скрипт генерирует равные, вложенные, несравнимые и
некорректные множества, проверяет оба направления сравнения и сопоставляет
коды результатов двух реализаций.

Пути до наобходимых файлов приведены в коде скрипта.
Минимальный запуск:

```sh
python3 scripts/rpmsetcmp/compare_random_sets.py
```

Нужны Python 3 и `cc`. Тест также работает до `Ctrl-C`, а найденные
расхождения сохраняет в `scripts/rpmsetcmp/error/`.

## Проверка зависимостей Sisyphus

### `scripts/rpmsetcmp/check_sisyphus_set_relations.sh`

Скрипт загружает в изолированный временный APT-каталог метаданные Sisyphus,
извлекает set:version `Provides` и `Requires`, компилирует компаратор из
`reimplement/newset.c` и проверяет, что требования пакетов удовлетворяются
хотя бы одним провайдером. По умолчанию проверяются первые 100 уникальных
требований; лимит задаётся переменной `REQUIRE_LIMIT` в начале файла.
`REQUIRE_LIMIT=0` для снятия лимита

Минимальный запуск на ALT Linux с доступом к зеркалу Sisyphus:

```sh
bash scripts/rpmsetcmp/check_sisyphus_set_relations.sh
```

Нужны `apt-get`, `apt-cache`, `cc`. В конце выводятся числа совместимых требований,
требований без провайдера, несовместимых пар и ошибок декодирования.

## Сравнение производительности

### `scripts/run-setc-bench.sh`

Скрипт берёт все `*.c` из `~/setc`, для каждого варианта собирает отдельные
`rpm-build` и `librpm7` в hasher, подставляя файл как `lib/set.c`. Затем он
запускает с этой `librpm` одинаковый набор симуляций APT (`check`,
`autoremove`, `install`, `upgrade`, `dist-upgrade`), записывает коды возврата и
время трёх прогонов в TSV. При включённых `COLLECT_PERF` и
`PERF_RECORD` дополнительно создаются результаты `perf stat`, `perf record`,
`perf report` и `perf annotate`.

Минимальная подготовка:

1. Запускать на ALT Linux p11 с настроенными `gear-hsh`/`hasher` и разрешённым
   монтированием `/proc`.
2. Скопировать сравниваемые реализации в `~/setc/`, по одному файлу `*.c` на
   вариант.
3. Проверить настройки в начале скрипта: `PACKAGER`, `APT_SOURCE`, `CPU` и
   каталоги `SETC_DIR`, `WORK_ROOT`, `RESULT_DIR`.
4. Для запуска без perf установить `COLLECT_PERF=0`; для профилирования
   оставить `COLLECT_PERF=1`, установить пакет `perf` и разрешить доступ к
   счётчикам производительности.

Запуск:

```sh
bash scripts/run-setc-bench.sh
```

По умолчанию рабочие каталоги создаются в `~/setc-bench`, а итоговые TSV и
perf-артефакты — в `~/res`. Первый запуск скачивает исходники и собирает RPM,
поэтому занимает значительно больше времени, чем случайные проверки.
