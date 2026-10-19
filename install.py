#!/usr/bin/env python3
""" Set up the local text game without creating a virtual environment.

Usage: python3 install.py """

import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PROTO_FILE = PROJECT_DIR / "m_m.proto"
REQUIREMENTS_FILE = PROJECT_DIR / "requirements.txt"

def run_command(command, description):
    print(f"\n{description}...", flush=True)

    try:
        subprocess.run(
            command,
            cwd=PROJECT_DIR,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"\nERROR: {description} failed "
            f"(exit code {exc.returncode}).",
            file=sys.stderr,
        )
        sys.exit(exc.returncode or 1)
    except OSError as exc:
        print(
            f"\nERROR: Could not run {description}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    print("===================================")
    print(" Local Text Game - Setup")
    print("===================================")

    required_files = [
        PROTO_FILE,
        REQUIREMENTS_FILE,
        PROJECT_DIR / "server.py",
        PROJECT_DIR / "client.py",
        PROJECT_DIR / "communications.py",
    ]

    missing = [path.name for path in required_files if not path.is_file()]

    if missing:
        print(
            "\nERROR: Required files are missing:",
            file=sys.stderr,
        )
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        sys.exit(1)

    if sys.version_info < (3, 10):
        print(
            "\nERROR: Python 3.10 or newer is required. "
            f"Current version: {sys.version.split()[0]}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\nProject directory: {PROJECT_DIR}")
    print(f"Python executable: {sys.executable}")

    # Install dependencies into the current Python environment.
    run_command(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--break-system-packages",
            "-r",
            str(REQUIREMENTS_FILE),
        ],
        "Installing Python dependencies",
    )
    
    # Generate the gRPC and Protocol Buffers Python files.
    run_command(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROJECT_DIR}",
            f"--python_out={PROJECT_DIR}",
            f"--grpc_python_out={PROJECT_DIR}",
            str(PROTO_FILE),
        ],
        "Generating Protocol Buffers files",
    )
    
    generated_files = [
        PROJECT_DIR / "m_m_pb2.py",
        PROJECT_DIR / "m_m_pb2_grpc.py",
    ]

    if not all(path.is_file() for path in generated_files):
        print(
            "\nERROR: One or more generated protobuf files "
            "could not be found.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\n===================================")
    print(" Setup completed successfully!")
    print("===================================")
    print("\nStart the moderator's server:")
    print("  python3 server.py")
    print("\nStart a player's client:")
    print("  python3 client.py")


if __name__ == "__main__":
    main()
