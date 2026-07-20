# Сравнение set-зависимостей Sisyphus с `newset.c`

Скрипт [`scripts/compare_sisyphus_set_versions.sh`](../scripts/compare_sisyphus_set_versions.sh)
сравнивает set-строки из RPM-заголовка с результатом `reimplement/newset.c`.

## Что именно сравнивается

1. Из изолированных APT-индексов `Sisyphus/x86_64` и `Sisyphus/noarch`
   берутся `Package`, `Architecture`, `Version`, `Filename`, `MD5Sum`,
   `Provides` и `Depends`/`Pre-Depends`.
2. RPM скачивается непосредственно из `Sisyphus/files/<arch>/RPMS` и
   проверяется по `MD5Sum` из индекса.
3. Эталонные зависимости читаются из RPM-заголовка через массивы тегов:
   `PROVIDENAME/PROVIDEFLAGS/PROVIDEVERSION` и
   `REQUIRENAME/REQUIREFLAGS/REQUIREVERSION`.
4. Для воспроизведения зависимостей вызываются установленные вместе с
   `rpm-build` программы `%_rpmlibdir/find-provides` и
   `%_rpmlibdir/find-requires` с методом `none,lib`.
5. В каталоге инструментов заменяется **только** `mkset`: вместо штатного
   `mkset` подставляется совместимая обвязка над `newset.c`. `provided_symbols`,
   `ldd --bindings`, `eu-readelf`, `objdump`, `file` и остальные средства
   отбора/извлечения символов остаются штатными инструментами `rpm-build`.

Для `Requires` APT получает точный уже скачанный RPM-файл как вход и скачивает
его полный runtime dependency closure в пустой пользовательский RPM root. Так
версия и архитектура целевого пакета не выбираются повторно по имени. APT
запускается с пустым `APT_CONFIG`; source list, package cache, archives, lists,
status и preferences перенаправлены во временный каталог, поэтому настройки,
hooks и RPM-база основной системы не участвуют в разрешении зависимостей.

Перед распаковкой проверяются имена элементов cpio и цели символических ссылок.
Абсолютная RPM-ссылка считается путём внутри виртуального package root: например,
`/usr/sbin/update-alternatives -> /bin/true` безопасно переписывается в
`../../bin/true`, а не направляется в `/bin/true` основной системы. Все symlink
сначала собираются из RPM-заголовков и создаются в порядке от родительских путей
к дочерним; затем `cpio` извлекает только нессылочные элементы. Благодаря этому
usrmerge-ссылки вроде `/bin -> usr/bin` создаются до файлов `/bin/*`.

Имена элементов с выходом через `..` и относительные symlink, фактически
выходящие из временного root, по-прежнему отклоняются. После проверки RPM-файлы
распаковываются без установки. Это позволяет штатному `rpm-build/ldd`
использовать интерпретатор и библиотеки из того же снимка Sisyphus, не изменяя
систему.

## Ограниченный тест

На ALT p11 с установленным `rpm-build`:

```sh
bash scripts/compare_sisyphus_set_versions.sh \
    --package strace \
    --report strace-set-report.tsv
```

Несколько пакетов:

```sh
bash scripts/compare_sisyphus_set_versions.sh \
    --package strace \
    --package libdw \
    --report sample-set-report.tsv
```

Либо первые десять записей индекса:

```sh
bash scripts/compare_sisyphus_set_versions.sh \
    --limit 10 \
    --report sample-set-report.tsv
```

Без `--all`, `--limit` или `--package` скрипт завершится до обращения к
репозиторию. Это защищает от случайного полного запуска.

## Полный запуск

Команда предусмотрена, но в ходе разработки не запускалась:

```sh
bash scripts/compare_sisyphus_set_versions.sh \
    --all \
    --report sisyphus-set-report.tsv
```

Продолжение после прерывания:

```sh
bash scripts/compare_sisyphus_set_versions.sh \
    --all \
    --resume \
    --report sisyphus-set-report.tsv
```

`--resume` пропускает только уже завершённую комбинацию
`package/architecture/version`; обновившийся пакет будет обработан заново.
Завершённость подтверждается маркером `complete=1` в полной 11-польной строке
`SUMMARY`, поэтому оборванная при записи строка не считается результатом.

Во время работы отчёт можно наблюдать отдельно:

```sh
tail -f sisyphus-set-report.tsv
```

`START` записывается до обработки пакета, а `DEPENDENCY` и `SUMMARY` — сразу
после сравнения пакета. Поэтому файл обновляется в процессе, а не только после
завершения всего обхода.

## Статусы отчёта

- `match` — set-строка из RPM совпала с результатом `newset.c`;
- `mismatch` — capability и оператор совпали, set-строки различаются;
- `missing_generated` — RPM содержит set-зависимость, но повторный запуск
  `rpm-build` её не сгенерировал;
- `extra_generated` — повторный запуск сгенерировал зависимость, которой нет в
  RPM-заголовке;
- `no_set_metadata` — в APT metadata пакета нет set-строк;
- `*_error` — ошибка скачивания, контрольной суммы, распаковки либо генератора.

`extra_generated` показывается в строках `DEPENDENCY` и делает итоговый статус
пакета равным `mismatch`. При анализе нужно учитывать, что бинарный RPM не
содержит исходных spec-фильтров (`filter_from_requires` и аналогичных), поэтому
часть таких расхождений может относиться не к `newset.c`, а к невозможности
полностью воспроизвести фильтрацию исходного spec. Их число отдельно указано в
поле `extras=N` строки `SUMMARY`.
