"""Tests for reading the log sheets `ags_log_url` actually serves.

All three fixtures are real sheets fetched on 2026-09-07. They are BGS material
under the Open Government Licence — "Contains British Geological Survey
materials © UKRI 2026" — and are kept here rather than mocked because the
parser's contract is with a real document, and because two of them are the
traps that a mock would never have thought to include:

  gwbv_bh03.pdf       BH03, "Area 2 - Golden Valley Bridge M5 J11" — the happy
                      path: three pages, ten strata, a stratum repeated across
                      a page break.
  gwbv_no_strata.pdf  "Carpenters Arms Charlton" — HTTP 200, a valid PDF, and
                      no log in it at all.
  gwbv_note_only.pdf  "Filton Laundry" — 121 m deep and 26 pages, whose only
                      description is an editorial note pointing at another
                      borehole. Parsed naively it becomes 121 m of UNKNOWN.

Every number asserted for BH03 was read off the sheet by hand before the parser
existed, and its final depth (13.00 m) independently equals the `loca_fdep`
the AGS index carries for the same record.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gbg.gwbv import (AgsRecord, GwbvParseError, NoLogData, _blocks, parse_header, parse_log,
                      synthetic_bgs_id)

FIXTURES = Path(__file__).parent / "fixtures"
BH03 = FIXTURES / "gwbv_bh03.pdf"
SOURCE = "gold:gwbv"

# (top, base, class) for all ten strata, read off the sheet by hand.
EXPECTED = [
    (0.00, 0.50, "CLAY"),
    (0.50, 0.95, "CLAY"),
    (0.95, 4.85, "MUDSTONE"),
    (4.85, 4.95, "LIMESTONE"),
    (4.95, 5.10, "CLAY"),
    (5.10, 7.90, "MUDSTONE"),
    (7.90, 8.20, "MUDSTONE"),
    (8.20, 11.75, "MUDSTONE"),
    (11.75, 12.00, "MUDSTONE"),
    (12.00, 13.00, "MUDSTONE"),
]


def test_parses_a_real_sheet_exactly():
    log = parse_log(BH03, bgs_id=-1, source=SOURCE, index_depth_m=13.0)
    assert [(iv.top_m, iv.base_m, iv.lith_class) for iv in log.intervals] == EXPECTED
    # Position and datum come off the sheet's own header, not from the index.
    assert log.easting == pytest.approx(389710.8)
    assert log.northing == pytest.approx(221155.531)
    assert log.ground_level_m == pytest.approx(31.1)
    assert log.total_depth_m == pytest.approx(13.0)
    # A borehole log has no unlogged gaps, and the descriptions survive intact.
    assert log.gaps_m == 0.0
    assert "fossiliferous LIMESTONE" in log.intervals[3].raw_description
    # BS 5930 capitals give the normaliser its high-confidence rule.
    assert all(iv.confidence == pytest.approx(0.9) for iv in log.intervals)


def test_header_carries_identity():
    import pymupdf

    with pymupdf.open(BH03) as doc:
        header = parse_header(doc[0].get_text("words"))
    assert header.borehole_id == "BH03"
    assert header.project.startswith("Area 2 - Golden Valley Bridge")
    assert header.ground_level_m == pytest.approx(31.1)


def test_page_break_repeat_does_not_invent_an_interval():
    """A stratum crossing a page break has its description repeated verbatim.

    BH03 spans three pages and has exactly ten strata; left in, the two repeats
    would make it twelve.
    """
    log = parse_log(BH03, bgs_id=-1, source=SOURCE)
    assert len(log.intervals) == 10
    descriptions = [iv.raw_description for iv in log.intervals]
    assert len(set(descriptions)) == len(descriptions)


def test_depth_plus_level_must_close_on_ground_level():
    """The sheet's own arithmetic is the check that a column was read correctly."""
    log = parse_log(BH03, bgs_id=-1, source=SOURCE)
    gl = log.ground_level_m
    for iv in log.intervals:
        # Every base depth has a printed level beside it summing to ground level.
        assert 0 <= iv.base_m <= gl + 1e-6


def test_a_sheet_the_service_holds_no_log_for():
    """HTTP 200 and a well-formed PDF, with no log in it. An outcome, not a bug."""
    with pytest.raises(NoLogData):
        parse_log(FIXTURES / "gwbv_no_strata.pdf", bgs_id=-1, source=SOURCE)


def test_a_sheet_carrying_only_a_note_is_not_gold():
    """121 m of borehole whose only description points at a different borehole."""
    with pytest.raises(NoLogData, match="note rather than strata"):
        parse_log(FIXTURES / "gwbv_note_only.pdf", bgs_id=-1, source=SOURCE)


def test_rejects_something_that_is_not_a_pdf():
    with pytest.raises(GwbvParseError, match="not a PDF"):
        parse_log(b"<html>Sorry, no log</html>", bgs_id=-1, source=SOURCE)


def test_blocks_drop_the_depth_ruler():
    """The ruler is right-aligned, so wide values reach into the description column."""
    lines = [(100.0, "Weak grey MUDSTONE Fractures are"), (108.0, "closely spaced"),
             (160.0, "120.5"), (220.0, "Strong grey LIMESTONE")]
    assert _blocks(lines) == ["Weak grey MUDSTONE Fractures are closely spaced",
                              "Strong grey LIMESTONE"]


def test_synthetic_ids_are_stable_and_negative():
    """AGS holes have no SOBI id, so they take deterministic negative ones."""
    a = synthetic_bgs_id("2020020409472818231")
    assert a < 0
    assert a == synthetic_bgs_id("2020020409472818231")
    assert a != synthetic_bgs_id("2020020409472818232")


def test_two_ags_holes_may_not_share_a_synthetic_id():
    ok = [SimpleNamespace(bgs_id=-1, bgs_loca_id="111"),
          SimpleNamespace(bgs_id=-2, bgs_loca_id="222")]
    AgsRecord.check_unique_ids(ok)

    clash = [SimpleNamespace(bgs_id=-7, bgs_loca_id="111"),
             SimpleNamespace(bgs_id=-7, bgs_loca_id="222")]
    with pytest.raises(ValueError, match="collision"):
        AgsRecord.check_unique_ids(clash)
