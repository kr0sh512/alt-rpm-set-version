# set:version

what is happening inside [set:version](https://git.altlinux.org/gears/r/rpm.git?a=blob;f=lib/set.c)

## Зачем

set:version в alt-rpm позволяет сопоставлять Provides и Requires пакетов не обычным сравнением версий, а сравнением специальных set-строк зависимостей вида
```text
libfoo.so.X = set:<encoded-set>
```

`encoded-set` формируется на основе символов, необходимых/предоставляемых пакетом. Данный механизм позволяет гарантировать (с точностью до коллизий хэша, об этом будет далее) наличие всех требуемых символов в библиотеке. Это исключает ситуации, при которых ">= версий" ломается при удалении символа из библиотеки, а также ситуаций совпадения SONAME библиотек с разным набором символов.

set-строки (являющиеся перекодированным списком символов) генерируются способом, который позволяет их сравнивать между собой на предмет включения одного множества символов в другой.

## Реализация `set.c`

`set.c` предоставляет 5 "публичных" API для работы set:version

```c
int rpmsetcmp(const char *set1, const char *set2);

struct set *set_new(void);
void set_add(struct set *set, const char *sym);
const char *set_fini(struct set *set, int bpp);
struct set *set_free(struct set *set);
```

### `rpmsetcmp()`

Основная функция, сравнивающая строки и выдающая результат в зависимости от включения:

*  1: set1  >  set2
*  0: set1 ==  set2
* -1: set1  <  set2 (aka set1 \subset set2)
* -2: set1 !=  set2
* -3: set1 decoder error
* -4: set2 decoder error

на основе [данной](https://github.com/svpv/rpmss/blob/4256d86cc9ba1aa4ceb8c0f03f7d48675d9d27bb/set.h#L12) заметки, `set1` лучше делать как `Provides` для лучшей производительности.

### `set_new()`

Создаёт пустой объект `struct set`.

Внутри `struct set` — это временный контейнер для строк символов и их будущих hash-значений.
Из релизации:
> internally struct set is just a bag of strings and their hash values.

### `set_add()`

Добавляет строковый символ в set.

Использование:
```c
set_add(s, "printf@@GLIBC_2.2.5");
set_add(s, "malloc@@GLIBC_2.2.5");
```

### `set_fini()`

Финализирует множество и возвращает готовую set-version строку.

Внутри происходит основная работа:
1. Jenkins hash
2. обрезка до bpp бит
3. сортировка
4. delta кодирование
5. golomb кодирование
6. base62(64) кодирование

### `set_free()`

Освобождает struct set и его внутренние строки.

## `set.c` под капотом

### hash

### delta

### golomb

### base62

## Комментарии

## additional

[коды](https://altlinux.space/arseny/atsv-research) от Арсения для наглядности происходящего

some funny [msg's](https://lists.pld-linux.org/mailman/pipermail/pld-devel-en/2013-November/012467.html)
