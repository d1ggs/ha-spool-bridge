"""Optional rewrite rules for the names of filaments imported from the external database."""

from __future__ import annotations

import re

Rules = list[tuple[re.Pattern[str], str]]


class RuleError(ValueError):
    """A rule line that is not a valid regex or replacement."""


def parse_rules(text: str) -> Rules:
    """One rule per line: `pattern => replacement`, or just `pattern` to delete the match."""
    rules: Rules = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        pattern, _, replacement = line.partition("=>")
        pattern, replacement = pattern.strip(), replacement.strip()
        if not pattern:
            raise RuleError(f"line {number}: empty pattern")
        try:
            compiled = re.compile(pattern)
            _check_replacement(compiled, replacement)
        except re.error as err:
            raise RuleError(f"line {number}: {err}") from err
        rules.append((compiled, replacement))
    return rules


def _check_replacement(compiled: re.Pattern[str], replacement: str) -> None:
    # Bad group references only fail on a match, so force one: the empty
    # alternative always matches "" and keeps the pattern's group numbering.
    try:
        probe = re.compile(f"(?:{compiled.pattern})|", compiled.flags)
    except re.error:
        return  # e.g. inline global flags, which cannot be wrapped
    probe.sub(replacement, "")


def _apply(name: str, rules: Rules) -> str:
    for compiled, replacement in rules:
        name = compiled.sub(replacement, name)
    return " ".join(name.split())


def renamed(name: str, rules: Rules) -> str | None:
    """The rewritten name, or None when unchanged, emptied, or not stable.

    A rule whose output changes again when re-applied (e.g. `Matte => Matte
    Finish`) would grow the name on every sync, so it is never applied.
    """
    if not rules:
        return None
    new = _apply(name, rules)
    if not new or new == name or _apply(new, rules) != new:
        return None
    return new
