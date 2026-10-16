from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".bsl", ".json", ".md", ".py", ".toml", ".xml", ".yml", ".yaml"}
FORBIDDEN_SUFFIXES = {".epf", ".erf", ".key", ".pem", ".pfx"}


class RepositorySafetyTests(unittest.TestCase):
    def test_no_binary_1c_artifacts_or_private_keys(self) -> None:
        forbidden = [path for path in ROOT.rglob("*") if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES]
        self.assertEqual([], forbidden)

    def test_no_long_numeric_business_identifiers(self) -> None:
        pattern = re.compile(r"(?<!\d)\d{10}(?:\d{2})?(?!\d)")
        hits: list[str] = []
        for path in self._text_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual([], hits)

    def test_no_private_key_markers(self) -> None:
        markers = ["BEGIN " + "PRIVATE KEY", "BEGIN OPENSSH " + "PRIVATE KEY"]
        hits: list[str] = []
        for path in self._text_files():
            text = path.read_text(encoding="utf-8")
            if any(marker in text for marker in markers):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual([], hits)

    def test_no_guid_or_email_literals(self) -> None:
        guid = re.compile(
            r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
            r"(?![0-9a-f])",
            re.IGNORECASE,
        )
        email = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
        hits: list[str] = []
        for path in self._text_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if guid.search(line) or email.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual([], hits)

    def _text_files(self) -> list[Path]:
        return [
            path
            for path in ROOT.rglob("*")
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES and ".git" not in path.parts
        ]


if __name__ == "__main__":
    unittest.main()
