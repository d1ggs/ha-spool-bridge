import pytest

from custom_components.spool_bridge.naming import RuleError, parse_rules, renamed

CLEANUP = "™|®\n\\s*\\(Formerly[^)]*\\)"


def test_empty_text_has_no_rules():
    assert parse_rules("") == []
    assert parse_rules("\n  \n") == []
    assert renamed("Anything", []) is None


def test_cleanup_rules_shorten_database_name():
    rules = parse_rules(CLEANUP)
    assert renamed("Panchroma™ Matte (Formerly PolyTerra™) Earth Brown", rules) == "Panchroma Matte Earth Brown"


def test_replacement_and_backreference():
    rules = parse_rules(r"^PLA-(\w+) => PLA \1")
    assert renamed("PLA-Basic Silver", rules) == "PLA Basic Silver"


def test_unchanged_name_returns_none():
    assert renamed("PLA Basic", parse_rules(CLEANUP)) is None


def test_rule_that_empties_the_name_is_ignored():
    assert renamed("PLA", parse_rules(".*")) is None


def test_unstable_rule_is_ignored():
    # Applying it again would grow the name on every sync.
    assert renamed("PLA Matte", parse_rules("Matte => Matte Finish")) is None


@pytest.mark.parametrize("text", ["(unclosed", " => nothing", r"(a) => \2"])
def test_invalid_rules_raise(text):
    with pytest.raises(RuleError):
        parse_rules(text)
