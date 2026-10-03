"""Check the environment and install what is missing.

    python scripts/setup.py            # report what is present
    python scripts/setup.py --install  # install anything missing
    python scripts/setup.py --check    # exit non-zero if something is missing

Only the packages needed for the engine and provider you intend to use are
required. DuckDB and a local Ollama model need nothing but a working install;
a cloud provider only needs its key in ``.env``.
"""
import argparse
import importlib
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent

REQUIREMENTS = {
    "sqlglot": ("sqlglot", "parsing and rewriting checks, needed by every engine"),
    "dotenv": ("python-dotenv", "reads .env for connection strings and keys"),
    "duckdb": ("duckdb", "the duckdb engine, embedded and serverless"),
    "psycopg2": ("psycopg2-binary", "the postgres engine"),
    "pymysql": ("pymysql", "the mysql engine"),
    "openai": ("openai", "the openai provider"),
    "genai": ("google-genai", "the gemini provider"),
    "pytest": ("pytest", "running the tests"),
}

ENV_KEYS = ("SQLRW_DSN", "OLLAMA_HOST", "OPENAI_API_KEY", "GEMINI_API_KEY")


def present(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def report() -> list[tuple[str, bool, str]]:
    return [(pkg, present(mod), why) for mod, (pkg, why) in REQUIREMENTS.items()]


def install(packages: list[str]) -> None:
    print(f"installing: pip install {' '.join(packages)}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *packages])


def env_report() -> dict[str, bool]:
    try:
        from dotenv import load_dotenv

        load_dotenv(DIR / ".env")
    except ImportError:
        pass
    import os

    return {key: bool(os.getenv(key)) for key in ENV_KEYS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true", help="pip install anything missing")
    parser.add_argument("--check", action="store_true", help="exit 1 if anything is missing")
    args = parser.parse_args(argv)

    rows = report()
    width = max(len(pkg) for pkg, _, _ in rows)
    print("Python packages")
    for pkg, ok, why in rows:
        print(f"  {'ok ' if ok else 'MISSING'}  {pkg:<{width}}  {why}")

    missing = [pkg for pkg, ok, _ in rows if not ok]
    if missing and args.install:
        install(missing)
        rows = report()
        missing = [pkg for pkg, ok, _ in rows if not ok]

    print("\nConfiguration (.env)")
    keys = env_report()
    for key, ok in keys.items():
        print(f"  {'ok ' if ok else 'unset'}  {key}")

    print("\nData")
    for path, label in ((DIR / "imdb.7z", "dataset archive"), (DIR / "data" / "imdb.duckdb", "loaded database")):
        print(f"  {'ok ' if path.exists() else 'missing'}  {label}: {path.name}")

    if missing:
        print(f"\n{len(missing)} package(s) missing. Run: python scripts/setup.py --install")
        return 1 if args.check else 0
    print("\nReady. Next: python scripts/load_data.py && python scripts/run_experiments.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())