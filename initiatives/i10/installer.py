#!/usr/bin/env python3
"""One-command installer for Veto (Epic 1).

``python3 initiatives/i10/installer.py install`` performs:

1. Environment checks — Python version, pip, disk space, writable data
   dir, optional network reachability.
2. Dependency setup — creates a virtualenv (default
   ``~/.local/share/veto/venv``), installs ``requirements.txt`` plus the
   packaging extras, optionally installs the Playwright Chromium browser.
3. Guided onboarding — name/email, data-directory choice, privacy
   defaults (local-only sync), writes the install record and a
   ``profile-v1``-stamped starter profile.
4. Wiring — prints the exact commands to run the CLI, web UI, and MCP
   server; writes a ``veto`` launcher shim.

``python3 initiatives/i10/installer.py uninstall [--purge-data]`` removes
the venv and install record; data is removed ONLY with ``--purge-data``
plus the exact typed confirmation ``PURGE MY DATA <absolute data dir>``.
Purge is validate-first: the confirmation, the safety checks (the data
dir must carry the installer-written sentinel file or live under the
install root — never ``/``, ``$HOME``, or system roots), and an encrypted
pre-purge backup are all settled BEFORE anything is deleted, so a refused
purge leaves the install 100% intact.

Passphrase handling follows the i10 Rule 1 convention: a passphrase is
NEVER placed on argv. The pre-purge backup reads ``$VETO_BACKUP_PASSPHRASE``
or prompts interactively via getpass.

Non-interactive use: pass ``--yes`` to accept defaults (CI / scripted
installs). Every step is idempotent — re-running ``install`` repairs a
partial install.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # repo root
PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGE_VERSION = "0.1.0"

INSTALL_ROOT = Path.home() / ".local" / "share" / "veto"
INSTALL_RECORD = INSTALL_ROOT / "install.json"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "veto" / "data"
MIN_PYTHON = (3, 10)
MIN_DISK_BYTES = 500 * 1024 * 1024  # 500 MB

# Exact pins for the dependencies the installer manages itself, so two
# installs months apart reproduce. (requirements.txt keeps the
# application's own version policy; the installer installs it as-is.)
# Hashes are deliberately not pinned: cryptography ships platform-specific
# wheels, so one hash cannot cover every target — the == pin is the
# reproducible contract. Pinned to the version this repo's test suite runs
# against.
INSTALL_EXTRA_DEPS = ["cryptography==50.0.1"]

DATA_SENTINEL = ".veto-data-dir"
"""Sentinel file the installer writes into every data dir it creates.

Purge refuses to ``rmtree`` a directory that lacks this sentinel unless
the directory lives under the install root — so a mis-pointed data dir
can never nuke an arbitrary folder.
"""

_SYSTEM_ROOTS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/var",
    "/opt",
    "/root",
    "/System",
    "/Applications",
    "/Library",
    "/private",
)
"""System locations a purge must never touch (exact match or ancestor)."""


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"  # "error" | "warning"
    fix: str = ""


def check_environment() -> list[Check]:
    """Run every environment check. ``ok`` overall means no error-severity
    check failed; warnings never block the install."""
    checks: list[Check] = []

    py = sys.version_info
    checks.append(
        Check(
            "python",
            (py.major, py.minor) >= MIN_PYTHON,
            f"Python {py.major}.{py.minor}.{py.micro}",
            fix=f"Install Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        )
    )

    pip_ok = shutil.which("pip3") or shutil.which("pip")
    checks.append(
        Check(
            "pip",
            bool(pip_ok),
            f"pip at {pip_ok}" if pip_ok else "pip not found on PATH",
            fix="Install pip (python3 -m ensurepip)",
        )
    )

    try:
        usage = shutil.disk_usage(str(Path.home()))
        checks.append(
            Check(
                "disk",
                usage.free >= MIN_DISK_BYTES,
                f"{usage.free / 1024**3:.1f} GB free",
                fix="Free at least 500 MB of disk space",
            )
        )
    except OSError as exc:
        checks.append(Check("disk", False, f"could not query disk: {exc}"))

    try:
        probe = INSTALL_ROOT / ".write-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
        detail = f"{INSTALL_ROOT} is writable"
    except OSError as exc:
        writable = False
        detail = f"{INSTALL_ROOT} not writable: {exc}"
    checks.append(
        Check(
            "install-dir",
            writable,
            detail,
            fix="Run as a user who can write to ~/.local/share",
        )
    )

    venv_mod = True
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", "--help"],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        venv_mod = False
    # Error-severity (not a warning): stock Debian/Ubuntu ships Python
    # WITHOUT the venv module, and everything after this point needs it.
    # Fail fast with the remediation instead of dying mid-install in
    # `python -m venv` — the `curl | bash` path must fail BEFORE mutating.
    checks.append(
        Check(
            "venv",
            venv_mod,
            "python -m venv available"
            if venv_mod
            else "venv module missing (stock Debian/Ubuntu splits it out)",
            fix="Install python3-venv (Debian/Ubuntu: sudo apt install python3-venv; "
            "macOS: brew install python3)",
        )
    )

    return checks


def environment_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.severity == "error")


def print_checks(checks: list[Check]) -> None:
    for c in checks:
        mark = "✓" if c.ok else ("!" if c.severity == "warning" else "✗")
        print(f"  [{mark}] {c.name}: {c.detail}")
        if not c.ok and c.fix:
            print(f"       fix: {c.fix}")


# ---------------------------------------------------------------------------
# Dependency setup
# ---------------------------------------------------------------------------


def venv_python(install_root: Path = INSTALL_ROOT) -> Path:
    exe = install_root / "venv" / "bin" / "python"
    return exe if exe.exists() else Path(sys.executable)


def _venv_python_path(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_is_healthy(venv_dir: Path) -> bool:
    """A venv is healthy only if its interpreter exists AND actually runs.

    Checking existence alone lets a corrupt venv (directory present, broken
    interpreter) crash every re-run with FileNotFoundError.
    """
    py = _venv_python_path(venv_dir)
    if not py.is_file():
        return False
    try:
        subprocess.run(
            [str(py), "--version"], capture_output=True, check=True, timeout=30
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def setup_dependencies(
    install_root: Path = INSTALL_ROOT,
    *,
    with_browser: bool = False,
    verbose: bool = False,
) -> dict:
    """Create the venv and install dependencies. Idempotent.

    A corrupt venv (directory present but interpreter missing or broken)
    is removed and rebuilt — a re-run recovers instead of crash-looping.
    """
    venv_dir = install_root / "venv"
    steps: list[str] = []
    if not _venv_is_healthy(venv_dir):
        if venv_dir.is_symlink() or venv_dir.is_file():
            venv_dir.unlink()
            steps.append("removed corrupt venv")
        elif venv_dir.is_dir():
            shutil.rmtree(venv_dir)
            steps.append("removed corrupt venv")
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
            capture_output=not verbose,
            timeout=300,
        )
        steps.append("created venv")
        if not _venv_is_healthy(venv_dir):
            raise RuntimeError(
                f"venv creation did not yield a working interpreter at {venv_dir}; "
                "check that python3-venv is installed for this Python."
            )
    else:
        steps.append("venv already present")

    vpy = _venv_python_path(venv_dir)
    req = BASE_DIR / "requirements.txt"
    cmd = [str(vpy), "-m", "pip", "install", "-r", str(req), *INSTALL_EXTRA_DEPS]
    subprocess.run(cmd, check=True, capture_output=not verbose, timeout=1200)
    steps.append(f"installed requirements.txt + {' '.join(INSTALL_EXTRA_DEPS)}")

    if with_browser:
        subprocess.run(
            [str(vpy), "-m", "playwright", "install", "chromium"],
            check=True,
            capture_output=not verbose,
            timeout=1200,
        )
        steps.append("installed playwright chromium")

    return {"venv": str(venv_dir), "steps": steps}


# ---------------------------------------------------------------------------
# Guided onboarding
# ---------------------------------------------------------------------------


@dataclass
class OnboardingAnswers:
    full_name: str = ""
    email: str = ""
    data_dir: str = ""
    sync_opt_in: bool = False  # local-only remains the default
    channel: str = "stable"


def validate_data_dir(raw: str | Path) -> Path:
    """Validate an onboarding data-directory choice. Returns the absolute path.

    Rejects empty input, ``/``, the home directory itself, and paths that
    already exist as non-directories. Creates nothing — callers mkdir only
    after validation passes.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("data directory must not be empty")
    candidate = Path(text).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"cannot resolve data directory {text!r}: {exc}")
    if resolved == Path("/").resolve():
        raise ValueError("data directory must not be the filesystem root (/)")
    if resolved == Path.home().resolve():
        raise ValueError("data directory must not be your home directory itself")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError(f"{resolved} exists and is not a directory")
    return resolved


def validate_purge_target(
    data_dir: str | Path, install_root: Path = INSTALL_ROOT
) -> Path:
    """Decide whether a data directory may be purged.

    Returns the resolved absolute target. Raises ValueError otherwise.
    Pure validation — performs no mutation. Refuses ``/``, ``$HOME``, and
    system roots explicitly, and requires the target to carry the
    installer-written sentinel file or live under the install root.
    """
    try:
        resolved = Path(str(data_dir)).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"cannot resolve data directory {data_dir!r}: {exc}")
    home = Path.home().resolve()
    if resolved == Path("/").resolve():
        raise ValueError("Refusing to purge: target is the filesystem root (/).")
    if resolved == home:
        raise ValueError("Refusing to purge: target is your home directory.")
    for raw in _SYSTEM_ROOTS:
        try:
            sysroot = Path(raw).resolve()
        except OSError:
            continue
        if sysroot == home:
            continue  # overlaps the home tree; sentinel/under-root checks decide
        if resolved == sysroot or sysroot in resolved.parents:
            raise ValueError(
                f"Refusing to purge: {resolved} is the system directory "
                f"{sysroot} or lives inside it."
            )
    try:
        inst = Path(install_root).resolve()
    except OSError:
        inst = Path(install_root)
    if resolved == inst:
        raise ValueError("Refusing to purge: target is the install root itself.")
    sentinel_ok = (resolved / DATA_SENTINEL).is_file()
    if not (sentinel_ok or inst in resolved.parents):
        raise ValueError(
            f"Refusing to purge {resolved}: not a Veto-managed data directory "
            f"(missing {DATA_SENTINEL} sentinel and not under the install root "
            f"{inst})."
        )
    return resolved


def _prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{text}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _prompt_data_dir(default: str) -> str:
    while True:
        raw = _prompt("Data directory", default)
        try:
            return str(validate_data_dir(raw or default))
        except ValueError as exc:
            print(f"  Not usable: {exc} — try again.")


def guided_onboarding(answers: OnboardingAnswers | None = None) -> OnboardingAnswers:
    """Interactive onboarding. Pass ``answers`` for non-interactive runs."""
    if answers is not None:
        return answers
    print("\n— Veto onboarding —")
    print("Local-first: your data stays on this machine unless you opt in.\n")
    full_name = _prompt("Your full name")
    email = _prompt("Email (for tailored materials)")
    data_dir = _prompt_data_dir(str(DEFAULT_DATA_DIR))
    sync = _prompt("Enable encrypted device sync now? (yes/no)", "no")
    channel = _prompt("Release channel (stable/preview)", "stable")
    return OnboardingAnswers(
        full_name=full_name,
        email=email,
        data_dir=data_dir,
        sync_opt_in=sync.lower().startswith("y"),
        channel=channel if channel in ("stable", "preview") else "stable",
    )


def write_install_record(answers: OnboardingAnswers, install_root: Path = INSTALL_ROOT) -> Path:
    # Validate BEFORE creating anything: a bad data dir must not mkdir.
    data_dir = validate_data_dir(answers.data_dir or str(DEFAULT_DATA_DIR))
    data_dir.mkdir(parents=True, exist_ok=True)
    # Sentinel so a later purge can prove this dir is Veto-managed.
    sentinel = data_dir / DATA_SENTINEL
    if not sentinel.exists():
        sentinel.write_text(
            json.dumps(
                {
                    "managed_by": "veto-installer",
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            )
            + "\n",
            encoding="utf-8",
        )
    record = {
        "package": "veto",
        "package_version": PACKAGE_VERSION,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": answers.channel,
        "data_dir": str(data_dir),
        "sync_opt_in": answers.sync_opt_in,
        "python": sys.version.split()[0],
    }
    install_root.mkdir(parents=True, exist_ok=True)
    INSTALL_RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    # The record sits next to identity-ish install config: keep it private.
    os.chmod(INSTALL_RECORD, 0o600)

    # Starter profile stamped with the i10-required profile-v1 schema so
    # backup/sync/compat checks accept it from day one.
    profile_path = data_dir / "profiles" / "profile.json"
    if not profile_path.exists():
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            json.dumps(
                {
                    "schema_version": "profile-v1",
                    "full_name": answers.full_name,
                    "email": answers.email,
                    "created_by": "veto-installer",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return INSTALL_RECORD


def install_shim(install_root: Path = INSTALL_ROOT) -> Path:
    """Write a ``veto`` launcher shim into ~/.local/bin."""
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "veto"
    if shim.exists():
        existing = shim.read_text(encoding="utf-8", errors="replace")
        if "written by the installer" not in existing:
            # A pre-existing user file named `veto` — never clobber it.
            return shim
    venv_py = install_root / "venv" / "bin" / "python"
    shim.write_text(
        f"""#!/usr/bin/env bash
# Veto launcher (written by the installer). Do not edit by hand.
exec "{venv_py}" "{BASE_DIR}/cli.py" "$@"
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def shim_on_path(bin_dir: Path | None = None) -> bool:
    """True when the shim directory is on PATH, so ``veto`` will resolve.

    Compares resolved paths so ``~`` and symlinked prefixes match. Pure
    detection logic — no mutation, safe to unit-test with a fake HOME/PATH.
    """
    want = Path(bin_dir) if bin_dir else Path.home() / ".local" / "bin"
    try:
        want_resolved = want.resolve()
    except OSError:
        want_resolved = want
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            candidate = Path(entry).expanduser().resolve()
        except OSError:
            continue
        if candidate == want_resolved:
            return True
    return False


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _load_backup_module():
    """Import the i10 backup module whether installer runs as a script or package.

    Kept lazy so ``installer.py`` stays importable without the backup stack,
    and robust to the working directory when run as a script.
    """
    try:
        from . import backup  # type: ignore[import-not-found]

        return backup
    except ImportError:
        pass
    repo_root = Path(__file__).resolve().parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from initiatives.i10 import backup

    return backup


def uninstall(
    install_root: Path = INSTALL_ROOT,
    *,
    purge_data: bool = False,
    confirm: str = "",
    backup_passphrase: str | None = None,
    no_backup_ack: bool = False,
) -> dict:
    """Remove the venv, shim, and install record.

    Data is removed ONLY when ``purge_data=True`` AND ``confirm`` is exactly
    ``"PURGE MY DATA <absolute data dir>"`` — the path is echoed in the
    confirmation so the operator sees what will be deleted.

    Validate-first: the confirmation, the purge-target safety checks, and
    the pre-purge backup are all settled BEFORE anything is deleted, so a
    refused purge leaves the install 100% intact (venv, shim, record, data).

    Pre-purge backup: when a passphrase is available (``backup_passphrase``
    or ``$VETO_BACKUP_PASSPHRASE`` — never argv, per the i10 Rule 1
    convention), an encrypted backup of the data dir is written to
    ``<install-root>/backups/`` via the i10 backup module before deletion.
    Without a passphrase, purge proceeds ONLY with ``no_backup_ack=True``,
    which acknowledges that NO backup is taken and the data is permanently
    unrecoverable.
    """
    removed: list[str] = []
    purged: list[str] = []
    pre_purge_backup: str | None = None
    data_dir: Path | None = None
    if INSTALL_RECORD.exists():
        try:
            data_dir = Path(
                json.loads(INSTALL_RECORD.read_text(encoding="utf-8"))["data_dir"]
            )
        except (KeyError, json.JSONDecodeError, ValueError, OSError):
            data_dir = None

    target: Path | None = None
    if purge_data:
        # ---- validate-first: no mutation may happen before this block ----
        if data_dir is None:
            raise ValueError(
                "Cannot purge: no install record names a data directory."
            )
        target = validate_purge_target(data_dir, install_root)
        expected = f"PURGE MY DATA {target}"
        if confirm != expected:
            raise ValueError(
                "Refusing to purge data without the exact confirmation.\n"
                f"Type exactly: {expected}"
            )
        passphrase = backup_passphrase or os.environ.get("VETO_BACKUP_PASSPHRASE", "")
        if passphrase:
            backup_mod = _load_backup_module()
            dest_dir = Path(install_root) / "backups"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"pre-purge-{time.strftime('%Y%m%d-%H%M%S')}.vetobackup"
            backup_mod.create_backup(str(target), str(dest), passphrase)
            pre_purge_backup = str(dest)
        elif not no_backup_ack:
            raise ValueError(
                "Refusing to purge without a pre-purge backup: no backup "
                "passphrase available. Set $VETO_BACKUP_PASSPHRASE (or pass "
                "backup_passphrase=...) to take an encrypted backup first, or "
                "pass no_backup_ack=True to acknowledge that NO BACKUP will "
                "be taken and the data will be permanently unrecoverable."
            )
        # NOTE: with no_backup_ack=True the purge below is final — loud by design.

    # ---- mutation starts here ----
    for path in (install_root / "venv", Path.home() / ".local" / "bin" / "veto"):
        if path.is_symlink():
            if path.name == "veto":
                # The installer never writes a symlinked shim; a symlink
                # named `veto` is by definition not ours — leave it alone,
                # dangling or not (foreign-shim protection).
                continue
            path.unlink()
            removed.append(str(path))
        elif path.is_file():
            if path.name == "veto":
                content = path.read_text(encoding="utf-8", errors="replace")
                if "written by the installer" not in content:
                    continue  # not our shim — leave the user's file alone
            path.unlink()
            removed.append(str(path))
        elif path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))

    if INSTALL_RECORD.exists():
        INSTALL_RECORD.unlink()
        removed.append(str(INSTALL_RECORD))

    if target is not None and target.exists():
        shutil.rmtree(target)
        purged.append(str(target))

    return {
        "removed": removed,
        "purged_data": purged,
        "pre_purge_backup": pre_purge_backup,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    print("Veto installer — environment checks")
    checks = check_environment()
    print_checks(checks)
    if not environment_ok(checks):
        print("\nEnvironment checks failed. Fix the ✗ items above and re-run.")
        return 1
    print("\nEnvironment OK. Installing dependencies…")
    info = setup_dependencies(
        with_browser=args.with_browser, verbose=args.verbose
    )
    for step in info["steps"]:
        print(f"  • {step}")
    answers = (
        OnboardingAnswers(
            full_name=args.name or "",
            email=args.email or "",
            data_dir=args.data_dir or "",
            sync_opt_in=False,
            channel=args.channel,
        )
        if args.yes
        else guided_onboarding()
    )
    record_path = write_install_record(answers)
    shim = install_shim()
    print(f"\nInstall record: {record_path}")
    print(f"Launcher shim:  {shim}")
    if answers.sync_opt_in:
        print(
            "\nSync opt-in recorded. Run `python3 -m initiatives.i10.sync enable --init` "
            "to create your sync group on this first device (use --join on "
            "later devices)."
        )
    if shim_on_path():
        print(
            "\nDone. Try it:\n"
            "  veto --help            # CLI\n"
            "  veto serve             # local web UI\n"
            "  python3 doctor.py      # setup health check"
        )
    else:
        # Never claim `veto --help` works when the shim dir isn't on PATH.
        print(
            "\nDone. NOTE: ~/.local/bin is not on your PATH, so the `veto` "
            "command won't resolve yet.\n"
            '  Run:  export PATH="$HOME/.local/bin:$PATH"\n'
            "  To make it permanent, add that line to ~/.bashrc (bash) or "
            "~/.zshrc (zsh), then restart your shell."
        )
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    backup_passphrase: str | None = None
    if args.purge_data and not args.no_backup_ack:
        # Rule 1: passphrase via env var or interactive prompt — never argv.
        backup_passphrase = os.environ.get("VETO_BACKUP_PASSPHRASE", "")
        if not backup_passphrase and sys.stdin.isatty():
            try:
                backup_passphrase = getpass.getpass(
                    "Pre-purge backup passphrase (leave empty to skip): "
                )
            except (EOFError, OSError, KeyboardInterrupt):
                backup_passphrase = ""
        backup_passphrase = backup_passphrase or None
    try:
        result = uninstall(
            purge_data=args.purge_data,
            confirm=args.confirm or "",
            backup_passphrase=backup_passphrase,
            no_backup_ack=args.no_backup_ack,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    for path in result["removed"]:
        print(f"  removed {path}")
    if result.get("pre_purge_backup"):
        print(
            f"  pre-purge backup: {result['pre_purge_backup']}  "
            "(keep this file — it is your only copy)"
        )
    for path in result["purged_data"]:
        print(f"  PURGED data {path}")
    print("Uninstall complete.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="veto-installer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="Install Veto")
    p_install.add_argument("--yes", action="store_true", help="Accept defaults (non-interactive)")
    p_install.add_argument("--with-browser", action="store_true", help="Also install Playwright Chromium")
    p_install.add_argument("--name", default="", help="Full name (with --yes)")
    p_install.add_argument("--email", default="", help="Email (with --yes)")
    p_install.add_argument("--data-dir", default="", help="Data directory (with --yes)")
    p_install.add_argument("--channel", default="stable", choices=("stable", "preview"))
    p_install.add_argument("--verbose", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_check = sub.add_parser("check", help="Run environment checks only")
    p_check.set_defaults(func=lambda a: (print_checks(check_environment()), 0)[1])

    p_uninstall = sub.add_parser("uninstall", help="Remove Veto")
    p_uninstall.add_argument("--purge-data", action="store_true", help="Also delete the data directory")
    p_uninstall.add_argument(
        "--confirm",
        default="",
        help='Must be exactly "PURGE MY DATA <absolute data dir>" with --purge-data',
    )
    p_uninstall.add_argument(
        "--no-backup-ack",
        action="store_true",
        help="Purge without a pre-purge backup (data will be permanently unrecoverable)",
    )
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
