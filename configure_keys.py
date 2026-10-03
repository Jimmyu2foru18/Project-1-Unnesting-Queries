"""Write or update the .env file.

    python scripts/configure_keys.py --interactive
    python scripts/configure_keys.py --ollama-model gpt-oss:20b
    python scripts/configure_keys.py --ollama-host http://localhost:11434

Existing values are kept unless overwritten, and non-secret values display on update.
"""
import argparse
import os
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
ENV = DIR / ".env"

FIELDS = {
    "ollama-model": ("OLLAMA_MODEL", "gpt-oss:20b", "local Ollama model"),
    "ollama-host": ("OLLAMA_HOST", "http://localhost:11434", "Ollama server"),
    "dsn": ("SQLRW_DSN", "data/imdb.duckdb", "default connection string"),
}


def read_env() -> dict[str, str]:
    if not ENV.exists():
        return {}
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    lines = ["# Written by scripts/configure_keys.py", ""]
    for key, value in values.items():
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass
    print(f"wrote {ENV}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interactive", action="store_true", help="prompt for each value")
    parser.add_argument("--ollama-model")
    parser.add_argument("--ollama-host")
    parser.add_argument("--dsn")
    parser.add_argument("--show", action="store_true", help="print which keys are set")
    args = parser.parse_args(argv)

    values = read_env()

    if args.show:
        all_keys = set(k[0] for k in FIELDS.values()) | set(values)
        for key in sorted(all_keys):
            print(f"  {'set   ' if values.get(key) else 'unset '} {key}")
        return 0

    supplied = {}
    if args.interactive:
        print("Press enter to keep the current value.")
        for name, (key, _, label) in FIELDS.items():
            current = values.get(key, "")
            answer = input(f"  {label} [{current or 'unset'}]: ").strip()
            supplied[name] = answer if answer else current
    else:
        for name in FIELDS:
            val = getattr(args, name.replace("-", "_"))
            if val is not None:
                supplied[name] = val

    if not supplied:
        parser.print_help()
        print("\nNothing to do. Pass --interactive or one of the --flags.")
        return 0

    for name, value in supplied.items():
        key = FIELDS[name][0]
        values[key] = value
        print(f"  {key} = {value}")

    write_env(values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())