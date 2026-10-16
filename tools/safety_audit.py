from __future__ import annotations

import math
import re
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".bsl", ".json", ".md", ".py", ".toml", ".xml", ".yml", ".yaml"}
FORBIDDEN_SUFFIXES = {".epf", ".erf", ".key", ".pem", ".pfx"}
ALLOWED_URL_PREFIXES = ("http://127.0.0.1:",)
GUID_PATTERN = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")


def text_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES and ".git" not in path.parts
    ]


def scan_forbidden_files() -> list[str]:
    return [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES
    ]


def scan_identifiers() -> list[str]:
    pattern = re.compile(r"(?<!\d)\d{10}(?:\d{2})?(?!\d)")
    return find_lines(pattern) + find_lines(GUID_PATTERN) + find_lines(EMAIL_PATTERN)


def scan_network_locations() -> list[str]:
    url_pattern = re.compile(r"https?://[^\s\"')>]+", re.IGNORECASE)
    path_pattern = re.compile(r"[A-Za-z]:\\(?:Users|Documents and Settings)\\", re.IGNORECASE)
    connection_pattern = re.compile(r"\b(?:Srvr|Ref|File)\s*=", re.IGNORECASE)
    ip_pattern = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
    hits = find_lines(path_pattern) + find_lines(connection_pattern)
    for path in text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for url in url_pattern.findall(line):
                if not url.startswith(ALLOWED_URL_PREFIXES):
                    hits.append(f"{path.relative_to(ROOT)}:{number}: unexpected URL")
            for ip in ip_pattern.findall(line):
                if ip != "127.0.0.1":
                    hits.append(f"{path.relative_to(ROOT)}:{number}: unexpected IP")
    return hits


def scan_secret_markers() -> list[str]:
    parts = [
        "BEGIN " + "PRIVATE KEY",
        "BEGIN OPENSSH " + "PRIVATE KEY",
        "gh" + "p_",
        "github_" + "pat_",
    ]
    pattern = re.compile("|".join(re.escape(part) for part in parts), re.IGNORECASE)
    return find_lines(pattern) + scan_credential_literals()


def scan_credential_literals() -> list[str]:
    assignment = re.compile(
        r"(?i)(?:password|пароль|client_secret|session_token|authorization)\s*=\s*[\"'][^\"']+[\"']"
    )
    hits: list[str] = []
    for path in text_files():
        if path.suffix.lower() not in {".bsl", ".py"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if assignment.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}: credential literal")
    return hits


def scan_high_entropy_literals() -> list[str]:
    quoted = re.compile(r"[\"']([A-Za-z0-9+/=_-]{32,})[\"']")
    hits: list[str] = []
    for path in text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for value in quoted.findall(line):
                counts = Counter(value)
                entropy = -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())
                if entropy >= 4.2:
                    hits.append(f"{path.relative_to(ROOT)}:{number}: high-entropy literal")
    return hits


def find_lines(pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for path in text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}")
    return hits


def main() -> int:
    checks = {
        "forbidden-files": scan_forbidden_files(),
        "business-identifiers": scan_identifiers(),
        "network-locations": scan_network_locations(),
        "secret-markers": scan_secret_markers(),
        "high-entropy-literals": scan_high_entropy_literals(),
    }
    failed = False
    for name, hits in checks.items():
        print(f"{name}: {'PASS' if not hits else 'FAIL'}")
        for hit in hits:
            print(f"  {hit}")
        failed = failed or bool(hits)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
