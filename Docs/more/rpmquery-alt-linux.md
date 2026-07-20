# `rpmquery` в ALT Linux

## Краткий вывод

`rpmquery` — отдельная точка входа в тот же механизм запросов RPM, что и
`rpm -q`. В ALT Linux p11 `/usr/bin/rpmquery` является символьной ссылкой на
ELF-программу `/usr/lib/rpm/rpmq`; имя запуска заранее включает режим запроса,
поэтому `-q` обычно не нужен:

```sh
rpmquery rpm
rpmquery -q rpm
rpm -q rpm
```

Все три команды дают один и тот же результат. При этом `rpmquery` не является
интерфейсом к репозиторию APT: без `-p` он читает только установленную базу RPM,
по умолчанию `/var/lib/rpm`. Для поиска доступных, но не установленных пакетов
нужны `apt-cache`, `apt-get` или веб/RDB-интерфейсы ALT.

Исследование проверено на официальном контейнере `alt:p11`, обновлённом до
`rpm-4.13.0.1-alt45.x86_64`, и по ветке `p11` исходников RPM, commit
`7e71ed3da438289d916f1cccc9c0a83e594455dc`.

## Где ищет `rpmquery`

| Вызов | Источник данных |
|---|---|
| `rpmquery NAME` | установленная база RPM |
| `rpmquery -a` | все пакеты из установленной базы RPM |
| `rpmquery -p FILE.rpm` | заголовок указанного RPM-файла, установка не требуется |
| `rpmquery --root ROOT ...` | база и файловая система относительно `ROOT` |
| `rpmquery --dbpath DIR ...` | явно указанная база RPM |

Практическая граница была проверена так: `apt-cache show strace` находил пакет
в p11, тогда как `rpmquery strace` отвечал `package strace is not installed` и
завершался с кодом `1`.

## Работа с APT-репозиторием

APT-RPM загружает индексы подключённых репозиториев в `/var/lib/apt/lists`.
Обычный порядок работы:

```sh
# Показать настроенные источники ALT, если установлен пакет apt-repo.
apt-repo

# Обновить локальные индексы после изменения источников.
apt-get update
```

В минимальных контейнерах `apt-repo` может отсутствовать; источники всё равно
задаются файлами `/etc/apt/sources.list` и `/etc/apt/sources.list.d/*.list`.

Сами `pkglist.*` являются бинарными header-list файлами RPM, поэтому искать в
них обычным `grep` не следует. Для доступа к кэшу предназначен `apt-cache`:

| Задача | Команда |
|---|---|
| Поиск по имени и описанию | `apt-cache search REGEX` |
| Перечень имён | `apt-cache pkgnames` |
| Выбранная версия и приоритеты источников | `apt-cache policy PACKAGE` |
| Полная доступная запись, включая версии зависимостей | `apt-cache show PACKAGE` |
| Граф разрешения зависимостей | `apt-cache depends PACKAGE` |
| Обратные зависимости | `apt-cache whatdepends PACKAGE` |
| Низкоуровневое представление версий и зависимостей | `apt-cache showpkg PACKAGE` |
| Метаданные исходного пакета | `apt-cache showsrc PACKAGE` |
| Все доступные записи репозиториев | `apt-cache dumpavail` |
| Проверка плана установки без изменений | `apt-get -s install PACKAGE` |
| Только скачать бинарный пакет и зависимости | `apt-get -d install PACKAGE` |
| Скачать исходники | `apt-get source PACKAGE` |

`apt-cache depends` удобен для просмотра найденных провайдеров, но в его
обычном выводе set-версия зависимости теряется. Для полных set-строк нужны
`apt-cache show`, `apt-cache showpkg`, скачанный RPM или RDB API.

## Set-строки пакета из APT-репозитория

### Способ 1: прямо из локального кэша APT

Это самый короткий способ, установка и скачивание RPM не нужны:

```sh
pkg=libdw

# Сначала проверить, какую версию и из какого источника выбрал APT.
apt-cache policy "$pkg"

# Полные поля с set-Requires и set-Provides.
apt-cache show "$pkg" |
sed -n -E '/^(Pre-Depends|Depends|Provides):/p' |
grep -F 'set:'
```

В проверенном p11 поля имели вид:

```text
Depends: ... liblzma.so.5()(64bit) (>= set:kiyIz7cr3p0), ...
Provides: libdw.so.1()(64bit) (= set:ldm5ZH1pjPBqjZ9bdJbLDnKGbC1hkJSX...)
```

`Depends` и `Pre-Depends` соответствуют требованиям пакета, `Provides` — тому,
что пакет предоставляет. `apt-cache show` может вывести несколько записей,
если в подключённых источниках доступны разные версии; выбранный кандидат
виден в `apt-cache policy`.

Для выгрузки всего подключённого репозитория можно использовать:

```sh
apt-cache dumpavail >available.txt
grep -E '^(Pre-Depends|Depends|Provides):.*set:' available.txt
```

В отличие от HTML-страниц `packages.altlinux.org`, кэш APT содержит полные
set-строки без визуального многоточия.

### Способ 2: скачать RPM через APT и запросить его заголовок

Это наиболее точный локальный способ: выводятся исходные массивы тегов RPM и
их удобно преобразовать в TSV. `apt-get -d` скачивает также отсутствующие
зависимости, но ничего не устанавливает.

```sh
pkg=strace
cache=$(mktemp -d)
trap 'rm -rf "$cache"' EXIT
mkdir -p "$cache/partial"

apt-get -y -d \
  -o "Dir::Cache::archives=$cache" \
  install "$pkg"

rpmfile=$(find "$cache" -maxdepth 1 -type f \
  -name "${pkg}_*.rpm" -print -quit)
```

Set-Requires выбранного пакета:

```sh
rpmquery -p --qf \
  '[%{REQUIRENAME}\t%{REQUIREFLAGS:depflags}\t%{REQUIREVERSION}\n]' \
  "$rpmfile" |
awk -F '\t' '$3 ~ /^set:/'
```

Set-Provides выбранного пакета:

```sh
rpmquery -p --qf \
  '[%{PROVIDENAME}\t%{PROVIDEFLAGS:depflags}\t%{PROVIDEVERSION}\n]' \
  "$rpmfile" |
awk -F '\t' '$3 ~ /^set:/'
```

Для `strace-7.0-alt1.x86_64.rpm` в p11 получены два требования:

```text
libdw.so.1()(64bit)       >=  set:lgHyMNBkvSTjjY0en8Yi3vFojgjVM7jRsRF4zCFBzNVmhhGF
libselinux.so.1()(64bit)  >=  set:liZ0N709Wr2dbKPs2
```

Set-Provides у этого пакета нет: исполняемый `strace` требует библиотеки, но не
экспортирует собственную разделяемую библиотеку. У скачанного вместе с ним
пакета `libdw` присутствует длинный Provide
`libdw.so.1()(64bit) = set:...`.

Если пакет уже установлен и APT не скачивает его повторно, его заголовок можно
прочитать напрямую командой `rpmquery PACKAGE`; для гарантированного получения
конкретного репозиторного RPM удобнее использовать RDB API.

### Способ 3: структурированные данные RDB API

RDB удобен для автоматизации, другой ветки/архитектуры и больших выборок. Он
возвращает полные строки и явно различает `provide`, `require` и `conflict`:

```sh
branch=p11
arch=x86_64
pkg=libdw

hash=$(curl -fsS \
  "https://rdb.altlinux.org/api/site/pkghash_by_binary_name?branch=$branch&name=$pkg&arch=$arch" |
  jq -r '.pkghash')

curl -fsS \
  "https://rdb.altlinux.org/api/dependencies/binary_package_dependencies/$hash" |
jq -r '
  .dependencies[]
  | select(
      (.type == "provide" or .type == "require") and
      (.version | startswith("set:"))
    )
  | [.type, .name, .version]
  | @tsv
'
```

Проверенный результат для `libdw` содержит один set-Provide и четыре
set-Requires. Поле `flag_decoded` того же JSON позволяет восстановить оператор:
`RPMSENSE_EQUAL` для обычного Provide и
`RPMSENSE_GREATER` + `RPMSENSE_EQUAL` для `>=` Require.

Через RDB можно также получить URL самого RPM:

```sh
curl -fsS \
  "https://rdb.altlinux.org/api/site/package_downloads_bin/$hash?branch=$branch&arch=$arch" |
jq -r '.downloads[].packages[].url'
```

## Основные запросы

### Пакет и его метаданные

```sh
# Версия установленного пакета в стандартном формате NVRA.
rpmquery rpm

# Подробная информация. В ALT вывод включает DistTag.
rpmquery -i rpm
rpmquery --info rpm

# Стабильный формат для скрипта.
rpmquery --qf '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{DISTTAG}|%{ARCH}\n' rpm

# Доступные имена тегов заголовка.
rpmquery --querytags
```

В `rpmquery` короткая опция `-i` означает `--info`, а не установку пакета. Это
ALT-алиас из `rpmpopt`; он появился в changelog RPM в версии
`4.13.0-alt7`. Для установки используется другой режим программы `rpm`, но в
обычной работе с ALT зависимости и репозитории следует поручать APT.

### Файлы

```sh
# Какому установленному пакету принадлежит файл.
rpmquery -f /usr/bin/rpmquery

# Все файлы пакета.
rpmquery -l rpm

# Только конфигурационные или документационные файлы.
rpmquery -c rpm
rpmquery -d rpm

# Пакет из файла без установки.
rpmquery -p ./package.rpm
rpmquery -lp ./package.rpm
rpmquery -ip ./package.rpm
```

Для аргумента-пути `--whatprovides` сначала обращается к индексу установленных
файлов, затем к индексу capability. Поэтому для существующего установленного
файла эти команды обычно совпадают:

```sh
rpmquery -f /usr/bin/rpmquery
rpmquery --whatprovides /usr/bin/rpmquery
```

### Provides, Requires и обратные запросы

```sh
# Что объявляет один пакет.
rpmquery --provides rpm
rpmquery --requires rpm
rpmquery -R rpm

# Какие установленные пакеты объявляют capability или требуют его.
rpmquery --whatprovides 'libpopt.so.0()(64bit)'
rpmquery --whatrequires 'libpopt.so.0()(64bit)'
```

`--provides`, `--requires`, `--info`, `--last`, `--scripts` и ряд других
удобных режимов реализованы как `popt`-алиасы над `--queryformat`, а не как
отдельные алгоритмы запросов.

## Формат запросов для автоматической обработки

Стандартный вывод удобен человеку, но надёжнее не разбирать его пробелами.
Массивы тегов RPM обходятся форматом в квадратных скобках. Чтобы внутри такого
итератора повторять скалярный тег для каждого элемента массива, знак `=` ставят
внутри имени тега: `%{=NAME}`.

### Все set-Requires в TSV

```sh
LC_ALL=C rpmquery -a --qf \
  '[%{=NAME}\t%{REQUIRENAME}\t%{REQUIREFLAGS:depflags}\t%{REQUIREVERSION}\n]' |
awk -F '\t' '$4 ~ /^set:/'
```

Поля: пакет, имя capability, оператор, версия зависимости.

Пример:

```text
rpm	libpopt.so.0()(64bit)	>=	set:jgtcU6BLBccTnteGxrE0
```

### Все set-Provides в TSV

```sh
LC_ALL=C rpmquery -a --qf \
  '[%{=NAME}\t%{PROVIDENAME}\t%{PROVIDEFLAGS:depflags}\t%{PROVIDEVERSION}\n]' |
awk -F '\t' '$4 ~ /^set:/'
```

Пример:

```text
libpopt	libpopt.so.0()(64bit)	=	set:jdtcJcAdqxTmPJUyuYcVIbLNhqmj...
```

Это предпочтительнее `grep` по человекочитаемому выводу, если нужны имена
пакетов, операторы или однозначное разделение полей.

## Что `rpmquery` делает и не делает с `set:version`

### Показывает сохранённые зависимости

`rpmquery --requires` и `rpmquery --provides` извлекают из заголовков пакетов
имя capability, флаги отношения и строку версии. Они не декодируют `set:` и не
показывают исходные ELF-символы.

Для установленного `rpm` в p11 наблюдалось:

```text
libpopt.so.0()(64bit) >= set:jgtcU6BLBccTnteGxrE0
```

Установленный провайдер находился по одному имени capability:

```sh
rpmquery --whatprovides 'libpopt.so.0()(64bit)'
# libpopt-1.18-alt1.x86_64
```

### Не сравнивает выражение, переданное в `--whatprovides`

Передача полного выражения не запускает `rpmsetcmp()`:

```sh
rpmquery --whatprovides \
  'libpopt.so.0()(64bit) >= set:jgtcU6BLBccTnteGxrE0'
```

Проверенный результат:

```text
no package provides libpopt.so.0()(64bit) >= set:jgtcU6BLBccTnteGxrE0
```

Для непутевого аргумента `--whatprovides` делает точный поиск по индексу
`PROVIDENAME`. Поэтому сначала нужно искать провайдера по имени capability, а
его set-строку получать отдельным запросом:

```sh
name='libpopt.so.0()(64bit)'
provider=$(rpmquery --whatprovides "$name" | head -n1)
rpmquery --qf \
  '[%{PROVIDENAME}\t%{PROVIDEFLAGS:depflags}\t%{PROVIDEVERSION}\n]' \
  "$provider" |
grep -F "$name"
```

Настоящее сравнение двух `set:`-версий происходит в `rpmdsCompareEVR()` при
проверке совместимости зависимостей. Если обе версии начинаются с `set:`, код
вызывает `rpmsetcmp()`; если `set:` имеет только одна сторона, зависимости не
пересекаются. Следовательно, `rpmquery` полезен для извлечения корпуса строк,
но не является CLI для непосредственного сравнения двух наборов.

## Как `rpmquery` используется при сборке самого ALT RPM

В `alt/rpm.spec` ветки p11 после первой сборки выполняется:

```sh
rpmquery -a --provides | fgrep '= set:' | sort >P
rpmquery -a --requires | fgrep '= set:' | sort >R
join -o 1.3,2.3 P R | shuf >setcmp-data
time ./setcmp <setcmp-data >/dev/null
```

Смысл конвейера:

1. Из локальной базы сборочного окружения собираются все set-Provides и
   set-Requires.
2. Строка поиска `= set:` захватывает как Provides с `= set:`, так и Requires с
   `>= set:`, потому что вторая строка тоже содержит подстроку `= set:`.
3. После сортировки `join` соединяет файлы по первому полю — имени capability.
4. `-o 1.3,2.3` оставляет только третьи поля: set провайдера и set требования.
5. Полученные реальные пары подаются в `tools/setcmp` для замера реализации
   `rpmsetcmp()` и профиль-управляемой пересборки `lib/set.c`.

В обновлённом минимальном контейнере p11 точный конвейер дал:

```text
P: 75 строк
R: 109 строк, из них 13 полных дублей
setcmp-data: 109 пар
```

Эти числа описывают только состав минимального контейнера, а не весь p11 или
Sisyphus. Также конвейер spec не сохраняет имя capability и пакет-владелец;
для исследовательского корпуса удобнее TSV-форматы выше.

Современная замена устаревающего `fgrep` без изменения смысла:

```sh
grep -F '= set:'
```

## Практические ограничения

- `rpmquery -a` означает все **установленные** пакеты, а не весь репозиторий.
- `--whatprovides` и `--whatrequires` ищут заголовки по имени capability; они
  не принимают полноценное выражение зависимости как запрос сравнения.
- Несколько установленных версий или провайдеров могут дать несколько строк.
- Set-строки крупных библиотек очень длинные; лучше писать полный вывод в файл,
  а в терминале показывать только статистику или начало строки.
- Для скриптов следует фиксировать `LC_ALL=C` и использовать `--queryformat`.
- `--requires`/`--provides` отражают метаданные RPM, а не текущее содержимое ELF
  на диске. Для восстановления исходных символов нужны `nm`, `readelf` и
  генераторы автозависимостей rpm-build.
- При запросе другой системы безопаснее явно использовать `--root`; `--dbpath`
  меняет только путь к БД и требует внимательности к относительным путям.
- ALT предоставляет `--nowait-lock` как popt-алиас, отключающий ожидание
  блокировки БД. Его стоит применять только когда отказ предпочтительнее
  ожидания, а не как способ обходить активную транзакцию RPM/APT.

## Источники

1. [Документация платформы ALT 10.1: утилита RPM](https://docs.altlinux.org/ru-RU/platform/10.1/html/alt-platform/utilita_komandnoj_stroki_rpm.html) — RPM работает с файлами, пакетами, зависимостями и собственной БД, но не знает о репозиториях.
2. [Настройка списка репозиториев APT в ALT](https://docs.altlinux.org/ru-RU/platform/10.1/html/alt-platform/nastrojka_spiska_repozitoriev_apt.html).
3. [Пакет `rpm` в p11](https://packages.altlinux.org/en/p11/srpms/rpm/) и [specfile](https://packages.altlinux.org/en/p11/srpms/rpm/specfiles/).
4. [RDB: текущий бинарный пакет `rpm` для p11/x86_64](https://rdb.altlinux.org/api/site/pkghash_by_binary_name?branch=p11&name=rpm&arch=x86_64).
5. [`rpmqv.c`](https://git.altlinux.org/gears/r/rpm.git?p=rpm.git;a=blob;f=rpmqv.c;hb=7e71ed3da438289d916f1cccc9c0a83e594455dc) — выбор режима Query по имени `rpmquery`/`rpmq`.
6. [`rpmpopt.in`](https://git.altlinux.org/gears/r/rpm.git?p=rpm.git;a=blob;f=rpmpopt.in;hb=7e71ed3da438289d916f1cccc9c0a83e594455dc) — ALT-алиасы `--requires`, `--provides`, `--info`, `-i`, `--nowait-lock`.
7. [`lib/query.c`](https://git.altlinux.org/gears/r/rpm.git?p=rpm.git;a=blob;f=lib/query.c;hb=7e71ed3da438289d916f1cccc9c0a83e594455dc) — индексы для `--whatprovides`, запросы файлов и формат `%{nvra}` по умолчанию.
8. [`lib/rpmds.c`](https://git.altlinux.org/gears/r/rpm.git?p=rpm.git;a=blob;f=lib/rpmds.c;hb=7e71ed3da438289d916f1cccc9c0a83e594455dc) — вызов `rpmsetcmp()` при сравнении двух set-версий.
9. [`alt/rpm.spec`](https://git.altlinux.org/gears/r/rpm.git?p=rpm.git;a=blob;f=alt/rpm.spec;hb=7e71ed3da438289d916f1cccc9c0a83e594455dc) — построение `setcmp-data` через `rpmquery`.
10. [Официальный контейнер ALT](https://hub.docker.com/_/alt) — среда воспроизведения `alt:p11`.
