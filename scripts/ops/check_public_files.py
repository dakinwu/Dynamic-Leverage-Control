"""Check Git's actual public candidates without staging files or printing secrets."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BLOCKED = {".pfx", ".p12", ".pem", ".key", ".db", ".sqlite", ".sqlite3", ".zip", ".7z"}
REVIEWED_BINARY_SHA256 = {
    "docs/assets/overview.jpg": "27d51808df866b4d293845f76fb3bc09eff8b3817666a8c172ab92365c7c025a",
}
RULES = {
    "private_home_path": re.compile(r"(?i)[a-z]:[\\/]Users[\\/][^\\/\s]+|/c/" + r"Users/[^/\s]+"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "telegram_token": re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b"),
}


def public_candidates(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    return sorted(set(name for name in result.stdout.decode("utf-8").split("\0") if name))


def inspect(root: Path, names: list[str]) -> list[dict]:
    issues = []
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        if (
            path.suffix.lower() in BLOCKED
            or name.startswith(("data/private/", "docs/research/", "backups/"))
            or path.name in {".env", "runtime.env"}
        ):
            issues.append({"file": name, "kind": "private_or_generated_file"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != REVIEWED_BINARY_SHA256.get(name):
                issues.append({"file": name, "kind": "binary_requires_manual_review"})
            continue
        for kind, pattern in RULES.items():
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    issues.append({"file": name, "line": number, "kind": kind})
    return issues


if __name__ == "__main__":
    names = public_candidates(ROOT)
    issues = inspect(ROOT, names)
    print(json.dumps({"candidate_files": len(names), "findings": issues}, ensure_ascii=False))
    raise SystemExit(bool(issues))
