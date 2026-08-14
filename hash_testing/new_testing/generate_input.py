#!/usr/bin/env python3
"""Generate unique words similar to a given word by applying random mutations."""

from __future__ import annotations

import argparse
import random
import string
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

DEFAULT_ALPHABET = string.ascii_letters + string.digits + "_"

OPERATION_ALIASES = {
    "1": "replace",
    "replace": "replace",
    "2": "delete",
    "delete": "delete",
    "3": "add",
    "add": "add",
    "4": "swap",
    "swap": "swap",
    "5": "case",
    "case": "case",
    "6": "first",
    "first": "first",
    "7": "last",
    "last": "last",
}

OPERATION_HELP = """операция изменения:
  1, replace  заменить случайный символ
  2, delete   удалить случайный символ
  3, add      добавить символ в случайную позицию
  4, swap     переставить два соседних символа
  5, case     сменить регистр случайного символа
  6, first    изменить первый символ
  7, last     изменить последний символ"""


class MutationError(ValueError):
    """Raised when the selected mutation cannot be applied."""


def different_character(current: str, alphabet: str, rng: random.Random) -> str:
    choices = [character for character in alphabet if character != current]
    if not choices:
        raise MutationError("алфавит не содержит символа, отличного от заменяемого")
    return rng.choice(choices)


def replace_character(word: str, alphabet: str, rng: random.Random) -> str:
    if not word:
        raise MutationError("нельзя заменить символ в пустом слове")
    index = rng.randrange(len(word))
    replacement = different_character(word[index], alphabet, rng)
    return word[:index] + replacement + word[index + 1 :]


def delete_character(word: str, _alphabet: str, rng: random.Random) -> str:
    if not word:
        raise MutationError("нельзя удалить символ из пустого слова")
    index = rng.randrange(len(word))
    return word[:index] + word[index + 1 :]


def add_character(word: str, alphabet: str, rng: random.Random) -> str:
    index = rng.randrange(len(word) + 1)
    return word[:index] + rng.choice(alphabet) + word[index:]


def swap_adjacent(word: str, _alphabet: str, rng: random.Random) -> str:
    indexes = [
        index for index in range(len(word) - 1) if word[index] != word[index + 1]
    ]
    if not indexes:
        raise MutationError(
            "для перестановки нужны хотя бы два соседних различных символа"
        )
    index = rng.choice(indexes)
    return word[:index] + word[index + 1] + word[index] + word[index + 2 :]


def change_case(word: str, _alphabet: str, rng: random.Random) -> str:
    indexes = []
    replacements: dict[int, str] = {}
    for index, character in enumerate(word):
        swapped = character.swapcase()
        if swapped != character and len(swapped) == 1:
            indexes.append(index)
            replacements[index] = swapped
    if not indexes:
        raise MutationError("в слове нет символов, у которых можно сменить регистр")
    index = rng.choice(indexes)
    return word[:index] + replacements[index] + word[index + 1 :]


def change_first(word: str, alphabet: str, rng: random.Random) -> str:
    if not word:
        raise MutationError("нельзя изменить первый символ пустого слова")
    replacement = different_character(word[0], alphabet, rng)
    return replacement + word[1:]


def change_last(word: str, alphabet: str, rng: random.Random) -> str:
    if not word:
        raise MutationError("нельзя изменить последний символ пустого слова")
    replacement = different_character(word[-1], alphabet, rng)
    return word[:-1] + replacement


Mutation = Callable[[str, str, random.Random], str]
MUTATIONS: dict[str, Mutation] = {
    "replace": replace_character,
    "delete": delete_character,
    "add": add_character,
    "swap": swap_adjacent,
    "case": change_case,
    "first": change_first,
    "last": change_last,
}


def parse_operation(value: str) -> str:
    try:
        return OPERATION_ALIASES[value.lower()]
    except KeyError as error:
        valid = ", ".join(OPERATION_ALIASES)
        raise argparse.ArgumentTypeError(
            f"неизвестная операция {value!r}; допустимы: {valid}"
        ) from error


def generate_words(
    source: str,
    count: int,
    operation: str,
    operation_count: int,
    alphabet: str = DEFAULT_ALPHABET,
    seed: int | None = None,
    max_attempts: int | None = None,
) -> list[str]:
    """Generate up to ``count`` unique mutations made by exactly N operations."""
    if count < 1:
        raise ValueError("количество выходных слов должно быть положительным")
    if operation_count < 1:
        raise ValueError("количество операций должно быть положительным")
    if not alphabet:
        raise ValueError("алфавит не должен быть пустым")

    mutation = MUTATIONS[operation]
    rng = random.Random(seed)
    attempt_limit = max_attempts or max(10_000, count * 1_000)
    words: list[str] = []
    seen: set[str] = set()

    for _attempt in range(attempt_limit):
        candidate = source
        try:
            for _ in range(operation_count):
                candidate = mutation(candidate, alphabet, rng)
        except MutationError:
            continue

        if candidate != source and candidate not in seen:
            seen.add(candidate)
            words.append(candidate)
            if len(words) == count:
                return words

    return words


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Создаёт список уникальных слов, похожих на исходное. "
            "Каждое слово получается независимо от исходного ровно заданным "
            "числом случайных операций."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"{OPERATION_HELP}\n\n"
            "Пример:\n"
            "  python3 nexus.py example -o swap -n 20 -k 2 --seed 42"
        ),
    )
    parser.add_argument("word", help="исходное слово")
    parser.add_argument(
        "-o", "--operation", required=True, type=parse_operation, help=OPERATION_HELP
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=10,
        help="верхняя граница числа уникальных выходных слов (по умолчанию: 10)",
    )
    parser.add_argument(
        "-k",
        "--operations",
        type=int,
        default=1,
        help="число операций над каждым словом (по умолчанию: 1)",
    )
    parser.add_argument(
        "--alphabet",
        default=DEFAULT_ALPHABET,
        help=("символы для добавления и замены " f"(по умолчанию: {DEFAULT_ALPHABET})"),
    )
    parser.add_argument(
        "--seed", type=int, help="seed генератора для воспроизводимого результата"
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        help="предельное число попыток собрать уникальные слова",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="записать слова в файл вместо стандартного вывода",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_attempts is not None and args.max_attempts < 1:
        parser.error("--max-attempts должен быть положительным")

    try:
        words = generate_words(
            source=args.word,
            count=args.count,
            operation=args.operation,
            operation_count=args.operations,
            alphabet=args.alphabet,
            seed=args.seed,
            max_attempts=args.max_attempts,
        )
    except (MutationError, ValueError) as error:
        parser.error(str(error))

    if len(words) < args.count:
        print(
            f"warning: operation={args.operation}: generated {len(words)} "
            f"of at most {args.count} unique words",
            file=sys.stderr,
        )

    output = "".join(f"{word}\n" for word in words)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
