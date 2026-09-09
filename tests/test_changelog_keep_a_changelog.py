"""Keep a Changelog headings stay Added/Changed/Fixed (1.2.0 style)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
TEMPLATE_DIR = REPO_ROOT / ".github" / "semantic-release" / "templates"
ALLOWED_H3 = {"Added", "Changed", "Fixed", "Removed"}
H3 = re.compile(r"^### (.+)$", re.MULTILINE)
H2 = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def test_changelog_keeps_insertion_flag() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "<!-- version list -->" in text
    assert not text.startswith("\n")


def test_changelog_template_strips_leading_control_whitespace() -> None:
    """PSR writes the rendered template as-is. Unstripped `{% %}` tags emit one
    leading blank line each (1.2.2 left CHANGELOG.md starting with newlines).
    """
    text = (TEMPLATE_DIR / "CHANGELOG.md.j2").read_text(encoding="utf-8")
    preamble = text.split("{{", 1)[0]
    for line in preamble.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("{#"):
            continue
        assert stripped.startswith("{%-"), stripped


def test_changelog_headings_are_keep_a_changelog() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "### bug fixes" not in text.lower()
    assert "### features" not in text
    headings = H3.findall(text)
    assert headings, "expected Keep a Changelog section headings"
    bad = [h for h in headings if h not in ALLOWED_H3]
    assert bad == [], f"non-Keep-a-Changelog headings: {bad}"


def test_changelog_1_2_1_matches_github_release_style() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.index("## [1.2.1]")
    end = text.index("## [1.2.0]")
    section = text[start:end]
    assert "### Fixed" in section
    assert "AWS stack no longer creates RDS; SQLite + Litestream replicas go to S3" in section
    assert "### bug fixes" not in section.lower()


def test_changelog_1_2_2_is_prewritten_keep_a_changelog() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    versions = H2.findall(text)
    # Newest first, but do not pin which version that is: every release adds one.
    assert versions.index("1.2.2") == versions.index("1.2.1") - 1
    start = text.index("## [1.2.2]")
    end = text.index("## [1.2.1]")
    section = text[start:end]
    assert "### Changed" in section
    assert "### Fixed" in section
    assert "DATABASE_BACKEND" in section
    assert "### bug fixes" not in section.lower()


def test_changelog_1_3_and_later_bullets_are_short() -> None:
    """From 1.3.0 on, match 1.2.0 length: one short line, not a paragraph."""
    text = CHANGELOG.read_text(encoding="utf-8")
    versions = H2.findall(text)
    starts = {m.group(1): m.start() for m in H2.finditer(text)}
    max_len = 160
    too_long: list[str] = []
    for i, ver in enumerate(versions):
        major, minor, _patch = (int(p) for p in ver.split("."))
        if (major, minor) < (1, 3):
            continue
        start = starts[ver]
        end = starts[versions[i + 1]] if i + 1 < len(versions) else len(text)
        for line in text[start:end].splitlines():
            if not line.startswith("- "):
                continue
            bullet = line[2:].strip()
            if len(bullet) > max_len:
                too_long.append(f"{ver} ({len(bullet)}): {bullet[:80]}…")
    assert too_long == [], "changelog bullets from 1.3.0 on must stay 1.2.0-short:\n" + "\n".join(too_long)


def test_semantic_release_templates_map_psr_type_names() -> None:
    helper = (TEMPLATE_DIR / "_keep_a_changelog.j2").read_text(encoding="utf-8")
    changelog_j2 = (TEMPLATE_DIR / "CHANGELOG.md.j2").read_text(encoding="utf-8")
    notes_j2 = (TEMPLATE_DIR / ".release_notes.md.j2").read_text(encoding="utf-8")
    assert '"bug fixes": "Fixed"' in helper
    assert '"features": "Added"' in helper
    assert "kac_heading" in changelog_j2
    assert "kac_heading" in notes_j2
    assert 'if "[" ~ ver ~ "]" not in changelog_parts[1]' in changelog_j2
