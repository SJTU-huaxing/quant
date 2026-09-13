"""Scan the entire Git index without printing secret values. Run before pushing."""

import os
import re
import subprocess
from pathlib import Path


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def forbidden_path(name: str) -> bool:
    parts = Path(name).parts
    base = Path(name).name.lower()
    return (
        (base.startswith(".env") and base != ".env.example")
        or base.endswith((".pem", ".key", ".log"))
        or any(part.lower() in {"secrets", "private", "data", "logs", "reports"} for part in parts)
    )


def content_rules(content: bytes, local_secrets: list[bytes]) -> list[str]:
    rules = []
    if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----", content):
        rules.append("private key material")
    if re.search(rb"\b[A-Za-z0-9]{64}\b", content):
        rules.append("possible Binance HMAC credential (64 characters)")
    if re.search(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", content):
        rules.append("possible GitHub token")
    if any(secret in content for secret in local_secrets):
        rules.append("matches locally configured credential")
    return rules


def main() -> int:
    try:
        os.chdir(git("rev-parse", "--show-toplevel").decode().strip())
        filenames = [name for name in git("ls-files", "-z").decode().split("\0") if name]
        if not filenames:
            print("Secret scan failed: Git index is empty. Stage project files first.")
            return 1
        # Never load local files or credential environment variables for this scan.
        findings = []
        for name in filenames:
            if forbidden_path(name):
                findings.append((name, "private/local file must not be tracked"))
                continue  # Do not even read a protected file from the Git index.
            content = git("show", ":" + name)
            findings.extend((name, rule) for rule in content_rules(content, []))
            if Path(name).name == ".env.example":
                for line in content.splitlines():
                    if re.match(rb"BINANCE_API_(KEY|SECRET)\s*=\s*\S+", line):
                        findings.append((name, "example credentials must be empty"))
        if findings:
            for name, rule in findings:
                print(f"BLOCKED: {name}: {rule}")
            return 1
        print(f"Secret scan passed: {len(filenames)} staged/tracked files; no values printed.")
        return 0
    except Exception:
        print(
            "Secret scan could not complete; details suppressed. Check Git and local file access."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
