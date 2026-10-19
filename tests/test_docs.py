"""The documentation set stays connected, and the skill stays loadable.

The README, the architecture and setup pages and the skill refer to each
other by relative links, and the skill is read by an agent through its
YAML frontmatter before anything else. A dangling link or a malformed
frontmatter is only noticed by a reader, so these tests pin both: every
relative link in the tracked Markdown resolves to a file in the tree, the
README links the three new documents, and the skill carries a valid name
and description (the subset of the skill-creator validator's rules that
needs no YAML library).
"""

import re
import unittest
from pathlib import Path

import support  # noqa: F401

REPO_ROOT = Path(support.__file__).resolve().parent.parent
MARKDOWN = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "cloudformation" / "README.md",
    *sorted((REPO_ROOT / "docs").glob("*.md")),
    *sorted((REPO_ROOT / "skills").glob("*/SKILL.md")),
]
SKILL = REPO_ROOT / "skills" / "agentic-on-spot" / "SKILL.md"
LINK = re.compile(r"\]\(([^)\s]+)\)")


def relative_links(path: Path) -> list[str]:
    return [t for t in LINK.findall(path.read_text(encoding="utf-8"))
            if not t.startswith(("http://", "https://", "mailto:", "#"))]


class LinksTest(unittest.TestCase):
    def test_every_relative_link_resolves_to_a_file(self):
        for md in MARKDOWN:
            for target in relative_links(md):
                resolved = (md.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(resolved.is_file(), f"{md.relative_to(REPO_ROOT)} links {target}, which is not a file")
                self.assertTrue(REPO_ROOT in resolved.parents, f"{md.relative_to(REPO_ROOT)} links outside the tree: {target}")

    def test_readme_links_the_architecture_setup_and_skill(self):
        links = set(relative_links(REPO_ROOT / "README.md"))
        self.assertTrue({"docs/architecture.md", "docs/setup.md", "skills/agentic-on-spot/SKILL.md"} <= links, links)

    def test_architecture_page_carries_a_mermaid_diagram(self):
        text = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        self.assertIn("```mermaid\n", text)
        self.assertEqual(text.count("```"), 2, "exactly one fenced block, the diagram, opened and closed")


class SkillFrontmatterTest(unittest.TestCase):
    def frontmatter(self) -> dict[str, str]:
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), "frontmatter must open the file")
        block = text.split("\n---\n", 1)[0][4:]
        fields = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            self.assertTrue(sep and key.strip() and not line.startswith((" ", "\t")), f"flat key: value lines only, got {line!r}")
            fields[key.strip()] = value.strip()
        return fields

    def test_name_matches_the_directory_and_is_hyphen_case(self):
        fields = self.frontmatter()
        self.assertEqual(fields["name"], SKILL.parent.name)
        self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(fields["name"]), 64)

    def test_description_is_present_and_plain(self):
        fields = self.frontmatter()
        self.assertTrue(fields["description"])
        self.assertLessEqual(len(fields["description"]), 1024)
        self.assertNotRegex(fields["description"], r"[<>]")
        self.assertFalse(set(fields) - {"name", "description", "license", "allowed-tools", "metadata"}, fields.keys())

    def test_body_has_no_unfinished_placeholder(self):
        self.assertNotIn("[TODO:", SKILL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
