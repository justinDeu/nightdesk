"""Tests for the list view's pure grouping/ordering helpers in
``domain.display`` — the query/rendering surface itself was HTMX-only and
was removed with the HTMX rip-out; the SPA does grouping/ordering
client-side against the same JSON ticket list.
"""
from nightdesk.domain import display


def test_normalize_group_and_order_defaults():
    assert display.normalize_group("bogus") == "status"
    assert display.normalize_group("PROJECT") == "project"
    assert display.normalize_order("") == "manual"
    assert display.normalize_order("Title") == "title"


class _T:
    def __init__(self, title, priority=0):
        self.title = title
        self.priority = priority


def test_order_tickets_title_and_priority():
    items = [_T("b", 1), _T("a", 3), _T("c", 2)]
    assert [t.title for t in display.order_tickets(items, "title")] == ["a", "b", "c"]
    assert [t.priority for t in display.order_tickets(items, "priority")] == [3, 2, 1]
    # 'manual' preserves incoming order.
    assert [t.title for t in display.order_tickets(items, "manual")] == ["b", "a", "c"]


def test_group_tickets_priority_buckets_descending():
    items = [_T("low", 1), _T("urgent", 4), _T("mid", 2)]
    groups = display.group_tickets(items, group="priority", order="manual")
    labels = [g["label"] for g in groups]
    assert labels[0] == "Urgent"  # highest priority first
    assert all(g["count"] == 1 for g in groups)


# --------------------------------------------------------------------------- #
# domain.display label grouping unit tests
# --------------------------------------------------------------------------- #


class _LabelStub:
    def __init__(self, id_, name, color=""):
        self.id = id_
        self.name = name
        self.color = color


class _TWithLabels:
    def __init__(self, title, labels=()):
        self.title = title
        self.labels = list(labels)
        self.priority = 0


def test_label_in_list_group_options():
    keys = [k for k, _ in display.LIST_GROUP_OPTIONS]
    assert "label" in keys


def test_group_by_label_multi_membership():
    la = _LabelStub("la", "alpha")
    lb = _LabelStub("lb", "beta")
    t1 = _TWithLabels("t1", [la, lb])
    t2 = _TWithLabels("t2", [la])
    t3 = _TWithLabels("t3", [])
    groups = display.group_tickets(
        [t1, t2, t3], group="label", order="manual", labels=[la, lb]
    )
    keys = [g["key"] for g in groups]
    assert "label:la" in keys
    assert "label:lb" in keys
    assert "label:__none__" in keys
    alpha = next(g for g in groups if g["key"] == "label:la")
    beta = next(g for g in groups if g["key"] == "label:lb")
    unlabeled = next(g for g in groups if g["key"] == "label:__none__")
    # t1 appears in both alpha and beta; t2 only in alpha.
    assert t1 in alpha["tickets"] and t1 in beta["tickets"]
    assert t2 in alpha["tickets"] and t2 not in beta["tickets"]
    # t3 (no labels) lands in Unlabeled.
    assert t3 in unlabeled["tickets"]
    assert t3 not in alpha["tickets"]


def test_group_by_label_swatch_color():
    la = _LabelStub("la", "alpha", "#abcdef")
    t = _TWithLabels("t", [la])
    groups = display.group_tickets([t], group="label", order="manual", labels=[la])
    alpha = next(g for g in groups if g["key"] == "label:la")
    assert alpha["swatch_color"] == "#abcdef"


def test_group_by_label_unlabeled_last():
    la = _LabelStub("la", "alpha")
    t1 = _TWithLabels("t1", [la])
    t2 = _TWithLabels("t2", [])
    groups = display.group_tickets(
        [t1, t2], group="label", order="manual", labels=[la]
    )
    assert groups[-1]["key"] == "label:__none__"
