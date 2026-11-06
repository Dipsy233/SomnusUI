"""Unit tests for the SUI-Text template matcher (test-only helper)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.sui_template import assert_sui, matches

ACTUAL = """@rev 4
WINDOWS w1 "Store" active
WINDOW w1 "Store" active
  SCREEN s1 "store"
    LIST l1 "Products" count=3 entity=item +find
      ITEM i1 "Big Mac" price=5.99 +add
      ITEM i2 "Fries" price=2.49 +add
"""


def test_only_written_fields_are_checked():
    template = """@rev ?
WINDOW w? "Store"
  SCREEN s? "store"
    LIST l? "Products" count=3
      ITEM i? "Big Mac" +add
"""
    assert matches(ACTUAL, template)


def test_a_wrong_attribute_fails():
    template = """@rev ?
WINDOW w? "Store"
  SCREEN s? "store"
    LIST l? "Products" count=4
"""
    assert not matches(ACTUAL, template)


def test_a_missing_capability_fails():
    template = """@rev ?
WINDOW w? "Store"
  LIST l? "Products" +submit
"""
    assert not matches(ACTUAL, template)


def test_a_single_top_level_node_matches_at_any_depth():
    template = 'LIST l? "Products" count=3 entity=item'
    assert matches(ACTUAL, template)


def test_a_regex_name_is_accepted():
    template = """@rev ?
WINDOW w? /^Store$/
  SCREEN s? /^store$/
    LIST l? /Products/
"""
    assert matches(ACTUAL, template)


def test_an_omitted_ref_is_a_wildcard():
    actual = 'BUTTON b1 "\u4fdd\u5b58"'
    assert matches(actual, 'BUTTON "\u4fdd\u5b58"')


def test_states_are_a_subset_check():
    actual = 'BUTTON b1 "保存" disabled'
    assert matches(actual, 'BUTTON b? "保存"')
    assert matches(actual, 'BUTTON b? "保存" disabled')
    assert not matches(actual, 'BUTTON b? "保存" readonly')


def test_count_lower_bound():
    actual = 'LIST l1 "Items" count>=240'
    assert matches(actual, 'LIST l? "Items" count>=200')
    assert not matches(actual, 'LIST l? "Items" count>=300')
    assert not matches(actual, 'LIST l? "Items" count=240')


def test_collapse_marker():
    actual = 'LIST l1 "Items" +240 more'
    assert matches(actual, 'LIST l? "Items" +? more')
    assert matches(actual, 'LIST l? "Items" +240 more')
    assert not matches(actual, 'LIST l? "Items" +200 more')


def test_ellipsis_consumes_remaining_children():
    actual = """SCREEN s1 "store"
  TEXT t1 "one"
  TEXT t2 "two"
  TEXT t3 "three"
"""
    template = """SCREEN s? "store"
  TEXT t? "two"
  ...
"""
    assert matches(actual, template)


THREE_CHILDREN = """SCREEN s1 "store"
  TEXT t1 "one"
  TEXT t2 "two"
  TEXT t3 "three"
"""


def test_extra_children_are_allowed_without_ellipsis():
    template = """SCREEN s? "store"
  TEXT t? "two"
"""
    assert matches(THREE_CHILDREN, template)


def test_quoted_escapes_follow_the_sui_parser():
    actual = 'TEXT t1 "a \\"quoted\\" name"'
    assert matches(actual, 'TEXT t? "a \\"quoted\\" name"')


def test_unknown_line_role_and_attributes():
    actual = "? r9 canvas reason=visual"
    assert matches(actual, "? r? canvas reason=visual")
    assert not matches(actual, "? r? image reason=visual")


def test_a_different_type_does_not_match():
    assert not matches(ACTUAL, 'TREE t? "Products" count=3 entity=item +find')


def test_strict_mode_compares_the_whole_text():
    assert_sui(ACTUAL, ACTUAL, strict=True)
    with pytest.raises(AssertionError):
        assert_sui(ACTUAL, 'WINDOW w? "Store"', strict=True)


def test_update_mode_rewrites_a_template_file(tmp_path, monkeypatch):
    path = Path(tmp_path) / "screen.sui"
    path.write_text("WINDOW w? \"stale\"\n", encoding="utf-8")
    monkeypatch.setenv("UPDATE_SUI_TEMPLATES", "1")
    assert_sui(ACTUAL, path)
    assert path.read_text(encoding="utf-8").rstrip("\n") == ACTUAL.rstrip("\n")


def test_a_template_mismatch_raises_with_both_texts():
    with pytest.raises(AssertionError) as exc:
        assert_sui(ACTUAL, 'WINDOW w? "Other"')
    assert "expected" in str(exc.value) and "actual" in str(exc.value)
