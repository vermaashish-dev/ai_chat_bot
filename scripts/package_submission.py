"""Build the submission zip: code, tests, docs, Sarvam spec and sample outputs, without data, database, venv or secrets.

Usage:
  python -m scripts.package_submission                # -> dist/VANI_PS04_submission.zip
  python -m scripts.package_submission --name TeamX   # -> dist/TeamX.zip

Fails if any secret value from .env (keys, tokens, workspace ID) appears in an included file.
"""

import argparse
import zipfile
from pathlib import Path

from app.config import PROJECT_ROOT

INCLUDE_DIRS = ["app", "scripts", "tests", "sarvam", "outputs", "slide"]
INCLUDE_FILES = ["README.md", "SUBMISSION.md", "skills.md", "VIDEO_SCRIPT.md", "requirements.txt", ".env.example", ".gitignore"]
# The data dictionary documents the input files; the CSVs themselves hold real seller data and stay out.
INCLUDE_GLOBS = ["data/*.md"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".db", ".db-wal", ".db-shm", ".log"}
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "WORKSPACE")


def secret_values(env_file: Path) -> list[str]:
    if not env_file.exists():
        return []
    values = []
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if sep and any(h in name.upper() for h in SECRET_HINTS) and len(value) >= 8:
            values.append(value)
    return values


def collect(root: Path) -> list[Path]:
    files = [root / f for f in INCLUDE_FILES if (root / f).exists()]
    for d in INCLUDE_DIRS:
        files += [p for p in (root / d).rglob("*") if p.is_file()]
    for g in INCLUDE_GLOBS:
        files += list(root.glob(g))
    return sorted({p for p in files if not SKIP_PARTS & set(p.parts) and p.suffix not in SKIP_SUFFIXES})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="VANI_PS04_submission")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "dist")
    args = ap.parse_args()

    files = collect(PROJECT_ROOT)
    secrets = secret_values(PROJECT_ROOT / ".env")
    leaks = [str(p.relative_to(PROJECT_ROOT)) for p in files
             if any(s in p.read_bytes().decode("utf-8", "ignore") for s in secrets)]
    if leaks:
        raise SystemExit(f"Refusing to package: a .env secret value appears in {leaks}")

    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{args.name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, Path(args.name) / p.relative_to(PROJECT_ROOT))
    size_kb = target.stat().st_size // 1024
    print(f"{len(files)} files, {size_kb} KB, secret scan clean ({len(secrets)} values checked) -> {target}")


if __name__ == "__main__":
    main()
