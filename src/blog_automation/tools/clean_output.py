"""Delete generated files from the output directory.

Dry run by default:
    python clean_output.py

Actually delete files:
    python clean_output.py --yes
"""

from __future__ import annotations

from blog_automation.paths import PROJECT_ROOT

import argparse
from pathlib import Path



OUTPUT_DIR = PROJECT_ROOT / "output"


def output_files() -> list[Path]:
    if not OUTPUT_DIR.exists():
        return []
    return sorted(path for path in OUTPUT_DIR.rglob("*") if path.is_file())


def remove_empty_dirs() -> None:
    if not OUTPUT_DIR.exists():
        return

    for path in sorted(OUTPUT_DIR.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean generated pipeline output files.")
    parser.add_argument("--yes", action="store_true", help="Actually delete files. Without this, only lists files.")
    args = parser.parse_args()

    files = output_files()
    if not files:
        print("[clean] No generated output files found.")
        return

    action = "Deleting" if args.yes else "Would delete"
    for path in files:
        print(f"[clean] {action}: {path.relative_to(PROJECT_ROOT)}")
        if args.yes:
            path.unlink()

    if args.yes:
        remove_empty_dirs()
        OUTPUT_DIR.mkdir(exist_ok=True)
        print(f"[clean] Deleted {len(files)} files from {OUTPUT_DIR.relative_to(PROJECT_ROOT)}")
    else:
        print(f"[clean] Dry run only. Re-run with --yes to delete {len(files)} files.")


if __name__ == "__main__":
    main()
