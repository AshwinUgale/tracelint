"""Declared per-tool failure predicates (failure_when) and JSON-pointer resolution."""

from __future__ import annotations

from tracelint.predicates import FailurePredicate, resolve_pointer


def test_resolve_pointer_nested_dict_and_list():
    doc = {"a": {"b": [10, {"c": "x"}]}}
    assert resolve_pointer(doc, "/a/b/0") == (10, True)
    assert resolve_pointer(doc, "/a/b/1/c") == ("x", True)
    assert resolve_pointer(doc, "/a/missing") == (None, False)
    assert resolve_pointer(doc, "") == (doc, True)


def test_resolve_pointer_bare_key_convenience():
    assert resolve_pointer({"status": "declined"}, "status") == ("declined", True)


def test_resolve_pointer_escapes():
    assert resolve_pointer({"a/b": 1}, "/a~1b") == (1, True)


def test_in_predicate():
    p = FailurePredicate.from_dict({"pointer": "/status", "in": ["declined", "failed"]})
    assert p.matches({"status": "declined"})
    assert not p.matches({"status": "approved"})
    assert not p.matches({"other": "declined"})  # pointer absent → not a declared failure


def test_equals_predicate_handles_false():
    p = FailurePredicate.from_dict({"pointer": "/ok", "equals": False})
    assert p.matches({"ok": False})
    assert not p.matches({"ok": True})


def test_exists_predicate_and_default():
    explicit = FailurePredicate.from_dict({"pointer": "/error_code", "exists": True})
    assert explicit.matches({"error_code": 42})
    assert not explicit.matches({"other": 1})
    # A pointer with no condition defaults to "exists".
    default = FailurePredicate.from_dict({"pointer": "/error"})
    assert default.matches({"error": "x"}) and not default.matches({"ok": 1})


def test_contains_predicate_on_free_text():
    # The MCP case: a bare "Error: ..." string result, matched against the whole content ("").
    p = FailurePredicate.from_dict({"pointer": "", "contains": "Error:"})
    assert p.matches("Error: upstream 500")
    assert not p.matches("ok, done")


def test_matches_regex_predicate():
    p = FailurePredicate.from_dict({"pointer": "", "matches": r"^(Error|Failed)\b"})
    assert p.matches("Failed to connect")
    assert p.matches("Error: boom")
    assert not p.matches("succeeded")


def test_contains_on_nested_value_stringifies():
    p = FailurePredicate.from_dict({"pointer": "/detail", "contains": "declined"})
    assert p.matches({"detail": {"reason": "card declined"}})


def test_malformed_regex_never_matches():
    p = FailurePredicate.from_dict({"pointer": "", "matches": "("})  # invalid regex
    assert not p.matches("Error: boom")  # fails safe, no crash


def test_from_dict_rejects_malformed():
    assert FailurePredicate.from_dict(None) is None
    assert FailurePredicate.from_dict({"in": ["x"]}) is None  # no pointer key
    assert FailurePredicate.from_dict("nope") is None
    assert FailurePredicate.from_dict({"pointer": ""}) is None  # empty pointer, no condition


def test_describe_names_path_and_value():
    p = FailurePredicate.from_dict({"pointer": "/status", "in": ["declined"]})
    assert p.describe({"status": "declined"}) == "/status='declined'"
