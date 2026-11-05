#!/bin/sh
# MicroPaw installer for Linux, macOS and WSL. No sudo.
#   sh install.sh [--extras telegram,discord] [--version 0.4.0] [--no-setup] [--modify-path]
set -eu

extras=""
version=""
run_setup=1
modify_path=0

usage() {
    echo "Usage: sh install.sh [--extras a,b] [--version X] [--no-setup] [--modify-path]"
    echo "Extras: telegram discord slack whatsapp browser pdf embeddings all"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --extras)
            [ $# -ge 2 ] || { echo "--extras needs a value" >&2; exit 2; }
            extras="$2"; shift 2 ;;
        --version)
            [ $# -ge 2 ] || { echo "--version needs a value" >&2; exit 2; }
            version="$2"; shift 2 ;;
        --no-setup) run_setup=0; shift ;;
        --modify-path) modify_path=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# The extras go into a package spec, so only plain names are allowed.
case "$extras" in
    *[!a-z0-9,]*) echo "Invalid --extras: use lowercase names separated by commas." >&2; exit 2 ;;
esac
case "$version" in
    *[!A-Za-z0-9.+!-]*) echo "Invalid --version." >&2; exit 2 ;;
esac

spec="micropaw"
[ -z "$extras" ] || spec="micropaw[$extras]"
[ -z "$version" ] || spec="$spec==$version"

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (official installer, user directory)..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "Need curl or wget to install uv." >&2
        exit 1
    fi
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || { echo "uv was installed but is not on PATH. Open a new terminal and run this again." >&2; exit 1; }
fi

echo "Installing $spec ..."
uv tool install --compile-bytecode --upgrade "$spec"

bin_dir="$(uv tool dir --bin 2>/dev/null || true)"
[ -n "$bin_dir" ] || bin_dir="$HOME/.local/bin"

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        line="export PATH=\"$bin_dir:\$PATH\""
        if [ "$modify_path" -eq 1 ]; then
            for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
                [ -f "$rc" ] || continue
                grep -qF "$line" "$rc" || printf '\n%s\n' "$line" >> "$rc"
            done
            echo "Added $bin_dir to PATH in your shell startup files. Open a new terminal."
        else
            echo "$bin_dir is not on your PATH. Add this line to your shell startup file:"
            echo "  $line"
        fi
        PATH="$bin_dir:$PATH"
        export PATH
        ;;
esac

if [ "$run_setup" -eq 1 ]; then
    if [ -t 0 ]; then
        paw setup
    elif (: </dev/tty) 2>/dev/null; then
        paw setup </dev/tty
    else
        echo "No terminal for the setup wizard. Run 'paw setup' later."
    fi
else
    echo "Done. Run 'paw setup' to configure MicroPaw."
fi
