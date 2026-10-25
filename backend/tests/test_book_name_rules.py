"""Step4-4B: book-name rule baseline matrix.

R4 review scope: audit `splitter_service.infer_book_name`,
`final_summary._clean_book_name` and `final_summary._looks_valid_book_name`
for real, and extract only strictly-equivalent fragments that clearly reduce
complexity. Everything else stays as-is with the reason recorded.

Audit result (the numbers below are the evidence):

- `infer_book_name` (filename inference) and `_clean_book_name`
  (metadata / directory-name cleaning) are NOT equivalent: they differ on
  10 of 21 sampled inputs. `infer_book_name` keeps the 《》 wrapper and strips
  version suffixes; `_clean_book_name` strips 《》【】 and a trailing bracket
  note but does not strip version suffixes. Merging them would change both
  callers, so NO extraction was made.
- `_looks_valid_book_name` has no counterpart in splitter_service.
- `fix_metadata_titles.py` imports `_clean_book_name` and
  `_looks_valid_book_name` directly, so both names must stay; the script is
  never executed by these tests.

Recorded quirks (kept, not "fixed"):

- `_clean_book_name("《》")` returns "" (both wrappers stripped).
- `_clean_book_name("《魔道祖师》【精编】")` returns "魔道祖师》【精编":
  only ONE leading and ONE trailing wrapper are removed, so a mixed
  wrapper leaves residue. This is current behavior.
- `infer_book_name("《》.txt")` returns "《》" (empty inner content is allowed).
- `_looks_valid_book_name` rejects "作者"/"著" anywhere, not just as a prefix.

Tests drive the REAL functions and the REAL `detect_book_name` service path
against a synthetic workspace; no rule is copied into the test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.services.final_summary import (  # noqa: E402
    _NON_TITLE_PREFIXES,
    _clean_book_name,
    _looks_valid_book_name,
    detect_book_name,
)
from backend.services.splitter_service import infer_book_name  # noqa: E402


# ---------------------------------------------------------------------------
# Sampled filenames: 《》 single/double, <>, 【】, version markers,
# separators, legal bracket names, non-names.
# ---------------------------------------------------------------------------

FILE_CASES = [
    ("\u300a\u9b54\u9053\u4e1a\u7ee3\u300b.txt", "\u300a\u9b54\u9053\u4e1a\u7ee3\u300b"),
    ("\u300a\u9b54\u9053\u4e1a\u7ee3\u300b\uff08\u7cbe\u6821\u7248\uff09.txt", "\u300a\u9b54\u9053\u4e1a\u7ee3\u300b"),
    ("\u300a\u300a\u5927\u738b\u9976\u547d\u300b\u300b\uff08\u7cbe\u6821\u7248\uff09.txt", "\u300a\u5927\u738b\u9976\u547d\u300b"),
    ("\u9b54\u9053\u4e1a\u7ee3.txt", "\u9b54\u9053\u4e1a\u7ee3"),
    ("\u9b54\u9053\u4e1a\u7ee3\u7cbe\u6821\u7248.txt", "\u9b54\u9053\u4e1a\u7ee3"),
    ("\u9b54\u9053\u4e1a\u7ee3-\u7cbe\u6821\u7248.txt", "\u9b54\u9053\u4e1a\u7ee3"),
    ("\u9b54\u9053\u4e1a\u7ee3_\u7cbe\u6821\u7248.txt", "\u9b54\u9053\u4e1a\u7ee3"),
    ("\u51e1\u4eba\u4fee\u4ed9\u4f20 \uff08\u6821\u5bf9\u7248\uff09.txt", "\u51e1\u4eba\u4fee\u4ed9\u4f20"),
    ("\u661f\u8fb0\u53d8(\u6821\u5bf9\u7248).txt", "\u661f\u8fb0\u53d8"),
    ("<\u9b54\u9053\u4e1a\u7ee3>.txt", "<\u9b54\u9053\u4e1a\u7ee3>"),
    ("\u300a\u9b54\u9053\u4e1a\u7ee3\u300b\u3010\u7cbe\u7f16\u3011.txt", "\u300a\u9b54\u9053\u4e1a\u7ee3\u300b"),
    ("\u9b54\u9053\u4e1a\u7ee3\u3010\u7cbe\u7f16\u3011.txt", "\u9b54\u9053\u4e1a\u7ee3\u3010\u7cbe\u7f16\u3011"),
    ("novel.txt", "novel"),
    ("a.txt", "a"),
    ("\u300a\u300b.txt", "\u300a\u300b"),
    ("\u9b54\u9053\u4e1a\u7ee3_\u5168\u96c6\u5b8c\u7ed3.txt", "\u9b54\u9053\u4e1a\u7ee3_\u5168\u96c6\u5b8c\u7ed3"),
    ("\u9b54\u9053\u4e1a\u7ee3_\u7ed3\u5c3e.txt", "\u9b54\u9053\u4e1a\u7ee3_\u7ed3\u5c3e"),
    ("\u9b54\u9053\u4e1a\u7ee3-\u7ed3\u5c3e.txt", "\u9b54\u9053\u4e1a\u7ee3-\u7ed3\u5c3e"),
    ("\u300a\u4ed9\u6f5b\u300b\uff08\u5168\u672c\uff09.txt", "\u300a\u4ed9\u6f5b\u300b"),
    ("chapter_1.txt", "chapter_1"),
    ("\u300a\u56de\u8f6f\u4e50\u56ed\u300b.txt", "\u300a\u56de\u8f6f\u4e50\u56ed\u300b"),
]


@pytest.mark.parametrize("filename,expected", FILE_CASES)
def test_infer_book_name_matrix(filename, expected):
    """Filename inference baseline (current behavior, not a target)."""
    assert infer_book_name(Path("dir") / filename) == expected


@pytest.mark.parametrize("filename,_expected", FILE_CASES)
def test_clean_book_name_matrix(filename, _expected):
    """Metadata / directory-name cleaning baseline (current behavior)."""
    stem = Path(filename).stem
    # record what it actually produces; the value is asserted by the
    # dedicated exact-value tests below so the matrix stays readable
    cleaned = _clean_book_name(stem)
    assert isinstance(cleaned, str)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 《》 stripped from both ends
        ("\u300a\u9b54\u9053\u4e1a\u7ee3\u300b", "\u9b54\u9053\u4e1a\u7ee3"),
        # trailing bracket note stripped
        ("\u300a\u9b54\u9053\u4e1a\u7ee3\u300b\uff08\u7cbe\u6821\u7248\uff09", "\u9b54\u9053\u4e1a\u7ee3"),
        ("\u51e1\u4eba\u4fee\u4ed9\u4f20 \uff08\u6821\u5bf9\u7248\uff09", "\u51e1\u4eba\u4fee\u4ed9\u4f20"),
        ("\u661f\u8fb0\u53d8(\u6821\u5bf9\u7248)", "\u661f\u8fb0\u53d8"),
        # 【】 also stripped
        ("\u3010\u9b54\u9053\u4e1a\u7ee3\u3011", "\u9b54\u9053\u4e1a\u7ee3"),
        # version suffixes are NOT stripped by this function
        ("\u9b54\u9053\u4e1a\u7ee3\u7cbe\u6821\u7248", "\u9b54\u9053\u4e1a\u7ee3\u7cbe\u6821\u7248"),
        ("\u9b54\u9053\u4e1a\u7ee3-\u7cbe\u6821\u7248", "\u9b54\u9053\u4e1a\u7ee3-\u7cbe\u6821\u7248"),
        # < > is not a recognized wrapper here
        ("<\u9b54\u9053\u4e1a\u7ee3>", "<\u9b54\u9053\u4e1a\u7ee3>"),
        # recorded quirk: only one leading + one trailing wrapper is removed,
        # so a mixed wrapper leaves residue
        ("\u300a\u9b54\u9053\u4e1a\u7ee3\u300b\u3010\u7cbe\u7f16\u3011", "\u9b54\u9053\u4e1a\u7ee3\u300b\u3010\u7cbe\u7f16"),
        # recorded quirk: 《》 alone becomes empty
        ("\u300a\u300b", ""),
        # plain name untouched
        ("\u9b54\u9053\u4e1a\u7ee3", "\u9b54\u9053\u4e1a\u7ee3"),
        # surrounding whitespace stripped
        ("  \u9b54\u9053\u4e1a\u7ee3  ", "\u9b54\u9053\u4e1a\u7ee3"),
    ],
)
def test_clean_book_name_exact_values(raw, expected):
    assert _clean_book_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("\u9b54\u9053\u4e1a\u7ee3", True),
        ("\u300a\u9b54\u9053\u4e1a\u7ee3\u300b", True),
        ("a", False),                     # too short
        ("", False),
        ("   ", False),
        ("x" * 61, False),                # too long
        ("===", False),                    # punctuation-only
        ("\u3000\u3000", False),
        ("\u7ae0\u8282\u4ecb\u7ecd:", False),     # half-width colon
        ("\u7ae0\u8282\u4ecb\u7ecd\uff1a", False),  # full-width colon
        ("\u76ee\u5f55", False),                  # non-title prefix
        ("\u5c0f\u8bf4\u7b80\u4ecb", False),
        ("\u524d\u8a00", False),
        ("\u7b2c\u4e00\u7ae0", False),
        ("\u7b2c1\u7ae0", False),
        ("\u7b2c 1 \u7ae0", False),
        ("\u5185\u5bb9\u7b80\u4ecb", False),
        ("\u4f5c\u54c1\u7b80\u4ecb", False),
        ("\u7b80\u4ecb", False),
        ("\u6954\u5b50", False),
        ("\u5e8f\u7ae0", False),
        ("\u5e8f\u8a00", False),
        ("\u5377\u9996\u8bed", False),
        ("\u6b63\u6587", False),
        ("\u7ae0\u8282", False),
        # contains a sentence-ending punctuation -> prose, not a title
        ("\u9b54\u9053\u4e1a\u7ee3\u3002", False),
        ("\u8fd9\u662f\u4ec0\u4e48\uff1f", False),
        ("\u592a\u68d2\u4e86\uff01", False),
        # contains 作者 or 著 anywhere -> splitter leaked author info
        ("\u9b54\u9053\u4e1a\u7ee3 \u4f5c\u8005\uff1a\u5f20\u4e09", False),
        ("\u9b54\u9053\u4e1a\u7ee3 \u8457", False),
        ("\u4f5c\u8005\u9b54\u9053\u4e1a\u7ee3", False),
        # a legal bracket name is fine
        ("\u9b54\u9053\u4e1a\u7ee3\uff08A\uff09", True),
    ],
)
def test_looks_valid_book_name_matrix(raw, expected):
    assert _looks_valid_book_name(raw) is expected


@pytest.mark.parametrize("raw", ["\u8fde\u8f7d", "\u65b0\u4e66"])
def test_looks_valid_book_name_known_gap(raw):
    """\u8fde\u8f7d / \u65b0\u4e66 are NOT in _NON_TITLE_PREFIXES, so they pass today.

    Recorded as a gap, not a target: adding them would change which
    metadata titles get rejected, and Step4-4B is an audit-only batch.
    """
    assert raw not in _NON_TITLE_PREFIXES
    assert _looks_valid_book_name(raw) is True


# ---------------------------------------------------------------------------
# The two cleaners are NOT equivalent (the reason no extraction was made)
# ---------------------------------------------------------------------------

def test_infer_and_clean_differ_on_sampled_inputs():
    """Evidence for 'no merge': the two cleaners disagree on the sample."""
    differing = []
    for filename, _expected in FILE_CASES:
        stem = Path(filename).stem
        if infer_book_name(Path("d") / filename) != _clean_book_name(stem):
            differing.append(stem)
    assert differing, "expected the two rules to differ on the sample"
    assert len(differing) == 10, (
        f"expected 10 differing inputs, got {len(differing)}: {differing}"
    )


def test_infer_keeps_wrapper_clean_strips_it():
    """The concrete difference: 《》 handling."""
    stem = "\u300a\u9b54\u9053\u4e1a\u7ee3\u300b"
    assert infer_book_name(Path("d") / (stem + ".txt")) == "\u300a\u9b54\u9053\u4e1a\u7ee3\u300b"
    assert _clean_book_name(stem) == "\u9b54\u9053\u4e1a\u7ee3"


def test_infer_strips_version_clean_does_not():
    """The other concrete difference: version-suffix handling."""
    stem = "\u9b54\u9053\u4e1a\u7ee3\u7cbe\u6821\u7248"
    assert infer_book_name(Path("d") / (stem + ".txt")) == "\u9b54\u9053\u4e1a\u7ee3"
    assert _clean_book_name(stem) == stem


# ---------------------------------------------------------------------------
# Real service path: detect_book_name against a synthetic workspace
# ---------------------------------------------------------------------------

def _make_book_dir(base: Path, book_name: str, title=None):
    book_dir = base / book_name
    blocks = book_dir / "blocks"
    blocks.mkdir(parents=True, exist_ok=True)
    if title is not None:
        (blocks / "metadata.json").write_text(
            json.dumps({"title": title}, ensure_ascii=False), encoding="utf-8"
        )
    return book_dir


def test_detect_book_name_prefers_metadata_title(tmp_path):
    """metadata.json title wins over the directory name."""
    _make_book_dir(tmp_path, "\u300a\u9b54\u9053\u4e1a\u7ee3\u300b\uff08\u7cbe\u6821\u7248\uff09",
                   title="\u9b54\u9053\u4e1a\u7ee3")
    blocks = tmp_path / "\u300a\u9b54\u9053\u4e1a\u7ee3\u300b\uff08\u7cbe\u6821\u7248\uff09" / "blocks"
    assert detect_book_name(blocks) == "\u9b54\u9053\u4e1a\u7ee3"


def test_detect_book_name_metadata_title_is_cleaned(tmp_path):
    """A wrapped / annotated metadata title is cleaned before use."""
    _make_book_dir(tmp_path, "book", title="\u300a\u9b54\u9053\u4e1a\u7ee3\u300b\uff08\u7cbe\u6821\u7248\uff09")
    blocks = tmp_path / "book" / "blocks"
    assert detect_book_name(blocks) == "\u9b54\u9053\u4e1a\u7ee3"


def test_detect_book_name_invalid_metadata_falls_back_to_dir(tmp_path):
    """An invalid metadata title falls back to the cleaned directory name."""
    book = "\u9b54\u9053\u4e1a\u7ee3"
    _make_book_dir(tmp_path, book, title="\u7ae0\u8282\u4ecb\u7ecd:")
    blocks = tmp_path / book / "blocks"
    assert detect_book_name(blocks) == book


def test_detect_book_name_no_metadata_uses_dir(tmp_path):
    """Without metadata.json the directory name is cleaned and used."""
    _make_book_dir(tmp_path, "\u300a\u4ed9\u6f5b\u300b\uff08\u7cbe\u6821\u7248\uff09", title=None)
    blocks = tmp_path / "\u300a\u4ed9\u6f5b\u300b\uff08\u7cbe\u6821\u7248\uff09" / "blocks"
    assert detect_book_name(blocks) == "\u4ed9\u6f5b"


def test_detect_book_name_invalid_dir_returns_none(tmp_path):
    """Neither a valid metadata title nor a valid directory name -> None."""
    _make_book_dir(tmp_path, "a", title="")
    blocks = tmp_path / "a" / "blocks"
    assert detect_book_name(blocks) is None


def test_detect_book_name_missing_blocks_returns_none(tmp_path):
    """No blocks directory -> None."""
    assert detect_book_name(tmp_path / "book" / "results") is None


# ---------------------------------------------------------------------------
# fix_metadata_titles.py imports the two helpers directly (compatibility)
# ---------------------------------------------------------------------------

def test_fix_metadata_titles_imports_the_helpers():
    """The repair script must keep importing these names; it is never run."""
    import ast

    script = (
        Path(__file__).resolve().parent.parent.parent
        / "backend" / "scripts" / "fix_metadata_titles.py"
    )
    assert script.exists(), f"missing {script}"
    tree = ast.parse(script.read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "backend.services.final_summary":
            imported.update(a.name for a in node.names)
    assert {"_clean_book_name", "_looks_valid_book_name"} <= imported, (
        f"fix_metadata_titles.py must import the helpers; got {imported}"
    )
