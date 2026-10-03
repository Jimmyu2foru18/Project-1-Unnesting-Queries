"""Write or update the .env file.

    python scripts/configure_keys.py --interactive
    python scripts/configure_keys.py --openai sk-... --gemini AIza...
    python scripts/configure_keys.py --ollama-model gpt-oss:20b

Existing values are kept unless you overwrite them, so running this twice does
not erase a key you already set. Nothing is printed back except whether a value
is set, so a key cannot end up in a terminal scrollback or a log.
"""
import argparse
import os
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
ENV = DIR / ".env"

FIELDS = {
    "ollama-model": ("OLLAMA_MODEL", "gpt-oss:20b", "local Ollama model"),
    "ollama-host": ("OLLAMA_HOST", "http://localhost:11434", "Ollama server"),
    "openai": ("OPENAI_API_KEY", "", "OpenAI key"),
    "openai-model": ("OPENAI_MODEL", "gpt-4.1", "OpenAI model"),
    "gemini": ("GEMINI_API_KEY", "", "Gemini key"),
    "gemini-model": ("GEMINI_MODEL", "gemini-2.5-pro", "Gemini model"),
    "dsn": ("SQLRW_DSN", "data/imdb.duckdb", "default connection string"),
}

SECRET = {"OPENAI_API_KEY", "GEMINI_API_KEY"}


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV.exists():
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
        pass  # not every filesystem supports this; the file is local either way
    print(f"wrote {ENV}")


def ask(label: str, current: str, secret: bool) -> str:
    shown = ("set" if current else "unset") if secret else (current or "unset")
    try:
        answer = input(f"  {label} [{shown}]: ").strip()
    except EOFError:
        return current
    return answer if answer else current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interactive", action="store_true", help="prompt for each value")
    parser.add_argument("--ollama-model")
    parser.add_argument("--ollama-host")
    parser.add_argument("--openai")
    parser.add_argument("--openai-model")
    parser.add_argument("--gemini")
    parser.add_argument("--gemini-model")
    parser.add_argument("--dsn")
    parser.add_argument("--show", action="store_true", help="print which keys are set")
    args = parser.parse_args(argv)

    if args.show:
        values = read_env()
        for key in sorted(set(list(FIELDS[name][0] for name in FIELDS)) | set(values)):
            is_set = bool(values.get(key))
            print(f"  {'set   ' if is_set else 'unset '} {key}")
        return 0

    values = read_env()
    given = {name: getattr(args, name.replace("-", "_")) for name in FIELDS}
    supplied = {name: value for name, value in given.items() if value is not None}

    if args.interactive:
        print("Press enter to keep the current value.")
        supplied = {}
        for name, (key, _, label) in FIELDS.items():
            supplied[name] = ask(label, values.get(key, ""), key in SECRET)

    if not supplied:
        parser.print_help()
        print("\nNothing to do. Pass --interactive or one of the --flags.")
        return 0

    for name, value in supplied.items():
        key = FIELDS[name][0]
        values[key] = value
        shown = "set" if key in SECRET else value
        print(f"  {key} = {shown if key not in SECRET else 'set'}")
    write_env(values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())