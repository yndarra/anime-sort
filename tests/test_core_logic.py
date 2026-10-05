"""Tests for the pure logic of anime-sort: config expressions, model-response parsing, window layout, names.json order.

Run: venv\\Scripts\\python -m pytest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tools" / "fix_name"))

from engine.client import NO_IMAGE_RE, parse_json_response  # noqa: E402
from engine.config import WhereError, parse_where  # noqa: E402
from engine.winlayout import slot_position  # noqa: E402
from regroup import render  # noqa: E402


# ---------- "where" expressions of pipeline stages ----------

def test_where_respects_parentheses_and_precedence():
    assert parse_where("[AI-GF] and ([AI-K3] or [AI-F5])") == (
        "and", ("ref", "AI-GF"), ("or", ("ref", "AI-K3"), ("ref", "AI-F5")))


@pytest.mark.parametrize("text", ["[AI-GF] and", "([AI-GF]", "AI-GF", ""])
def test_where_rejects_broken_expressions(text):
    with pytest.raises(WhereError):
        parse_where(text)


# ---------- model responses ----------

def test_json_response_inside_code_fence_and_prose():
    assert parse_json_response('```json\n{"known": true}\n```') == {"known": True}
    assert parse_json_response('Here you go: {"known": false} hope it helps') == {"known": False}


@pytest.mark.parametrize("answer, lost", [
    ("No image was provided to inspect.", True),
    ("It looks like no image was attached. Please upload it", True),
    ("I don't see any image attached to your message.", True),
    ("Blue hair girl with twin tails, typical of Hatsune Miku", False),
    ("Insufficient detail in the image to identify the character", False),
])
def test_lost_image_detection(answer, lost):
    assert bool(NO_IMAGE_RE.search(answer)) is lost


# ---------- window layout ----------

SCREEN = {"monitor": [0, 0, 3440, 1392], "margin": 20, "gap": 10}


def test_center_layout_stacks_down_then_skips_main_window_on_next_round():
    state = {**SCREEN, "mode": "center"}
    positions = [slot_position(state, slot, 1802, 332) for slot in range(6)]
    assert positions[0] == (819, 20)                           # main window: centred, top
    assert positions[1][1] - positions[0][1] == 332 + 10       # gap between windows
    rows = len({y for _, y in positions[:3]})
    assert rows == 3                                           # 3 windows fit in height 1392
    assert positions[3] == positions[1]                        # second round starts on slot 1, not on top of slot 0


def test_left_right_layout_fills_left_column_then_right():
    state = {**SCREEN, "mode": "left-right"}
    left = slot_position(state, 0, 1802, 332)
    right = slot_position(state, 3, 1802, 332)
    assert left == (20, 20)
    assert right == (3440 - 20 - 1802, 20)


def test_tall_windows_may_overlap_main_window():
    state = {**SCREEN, "mode": "center"}
    assert slot_position(state, 1, 1802, 900) == slot_position(state, 0, 1802, 900)


# ---------- names.json order ----------

def test_names_grouped_by_target_title_with_other_first():
    text = render([("Kimi No Na Wa", "Your Name"), ("Original", "Other"),
                   ("Your Name\\Miyamizu Mitsuha", "Your Name\\Mitsuha Miyamizu"), ("Kimi No Na Wa", "Your Name")], [])
    data = json.loads(text)
    assert data["new"] == {}
    assert list(data["titles"]) == ["Other", "Your Name"]
    assert data["titles"]["Other"] == {"Original": "Other"}
    assert list(data["titles"]["Your Name"].items()) == [("Kimi No Na Wa", "Your Name"),
                                                         ("Your Name\\Miyamizu Mitsuha", "Your Name\\Mitsuha Miyamizu")]


def test_global_neighbors_shift_file_order_and_known_results():
    from types import SimpleNamespace

    from stages.neighbors import LocalOrder

    ds = SimpleNamespace(entries=[(None, None, "a.jpg"), (None, None, "b.jpg"), (None, None, "c.jpg")])
    order = LocalOrder(ds, {0: {"series": "X"}, 2: {"series": "X"}})
    assert order.position("b.jpg") == 1 and order.order == [0, 2] and order.name_at(5) is None


def test_back_button_skips_files_checked_by_stage_ahead():
    """Кнопка «⇤»: предыдущий этап доделывает только файлы, которых не касался этап, с которого вернулись."""
    from types import SimpleNamespace

    from stages import common

    items = {"a": {"result": None, "checked_stages": ["B"], "stage_results": {"B": None}, "stage_details": {"B": {}}},
             "b": {"result": None, "checked_stages": [], "stage_results": {}, "stage_details": {}}}
    stage = SimpleNamespace(id="A", index=1, from_=None, where=None, apis=[])
    ds = SimpleNamespace(entries=[(None, None, "a"), (None, None, "b")], stages=[stage], item=items.__getitem__,
                         exclude_checked_by={"B"})
    assert [key for _, _, key in common.candidates(ds, stage)] == ["b"]
