#!/usr/bin/env python3
"""
Installer for radiolog.

  install.bat           (Windows: double-click it)
  ./install.sh          (Linux / macOS)
  python install.py     (any OS)

Options:
  --uninstall           remove the radiolog command
  --no-path             don't touch PATH
"""
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WIN = sys.platform == "win32"
IN_VENV = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
EXE = "radiolog.exe" if IS_WIN else "radiolog"


def say(msg=""):
    print(msg, flush=True)


def run(cmd):
    """Run a command, echo its output, return (returncode, output)."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.stdout:
        print(p.stdout.rstrip(), flush=True)
    return p.returncode, p.stdout or ""


# --------------------------------------------------------------------------- install

def pip_install():
    target = f"{HERE}[windows]" if IS_WIN else str(HERE)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
           "--no-warn-script-location"]
    if not IN_VENV:
        cmd.append("--user")
    cmd.append(target)
    say("Installing radiolog with pip…")
    code, out = run(cmd)
    if code == 0:
        return "pip"

    if "No module named pip" in out:
        say("pip is missing; trying to set it up…")
        run([sys.executable, "-m", "ensurepip", "--upgrade"])
        code, out = run(cmd)
        if code == 0:
            return "pip"

    if "externally-managed-environment" in out:
        # Debian/Ubuntu/Fedora etc. don't allow pip outside a venv: use pipx.
        say("\nThis system's Python doesn't allow pip installs; using pipx instead…")
        if run(["pipx", "--version"])[0] != 0:
            raise SystemExit(
                "pipx is not installed. Install it, then run this installer again:\n"
                "  Debian/Ubuntu:  sudo apt install pipx\n"
                "  Fedora:         sudo dnf install pipx\n"
                "  Arch:           sudo pacman -S python-pipx\n"
                "  macOS:          brew install pipx"
            )
        if run(["pipx", "install", "--force", str(HERE)])[0] != 0:
            raise SystemExit("pipx install failed (see messages above).")
        run(["pipx", "ensurepath"])
        return "pipx"

    raise SystemExit("\nInstallation failed (see messages above).")


def user_scheme():
    try:
        return sysconfig.get_preferred_scheme("user")   # Python 3.10+
    except AttributeError:
        if IS_WIN:
            return "nt_user"
        if sys.platform == "darwin" and sys._framework:
            return "osx_framework_user"
        return "posix_user"


def find_script_dir():
    candidates = [sysconfig.get_path("scripts")]
    try:
        candidates.append(sysconfig.get_path("scripts", user_scheme()))
    except KeyError:
        pass
    for d in candidates:
        if d and (Path(d) / EXE).exists():
            return Path(d)
    return None


def on_path(directory):
    norm = lambda p: os.path.normcase(os.path.normpath(p))
    return norm(str(directory)) in {norm(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p}


def add_to_path_windows(directory):
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in current.split(";") if p]
        if any(os.path.normcase(p.rstrip("\\")) == os.path.normcase(str(directory)) for p in parts):
            return False
        parts.append(str(directory))
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
    # Tell Windows the environment changed so new terminals pick it up.
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x2
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None)
    return True


def add_to_path_unix(directory):
    line = f'export PATH="{directory}:$PATH"  # added by radiolog installer'
    home = Path.home()
    rc_files = [home / f for f in (".bashrc", ".zshrc") if (home / f).exists()] or [home / ".profile"]
    changed = []
    for rc in rc_files:
        text = rc.read_text() if rc.exists() else ""
        if str(directory) in text:
            continue
        with rc.open("a") as fh:
            fh.write(f"\n{line}\n")
        changed.append(rc)
    return changed


def install(fix_path=True):
    method = pip_install()
    say()
    if method == "pipx":
        say("✔ radiolog installed with pipx.")
        say("  Open a NEW terminal and run:  radiolog --help")
        return

    script_dir = find_script_dir()
    if not script_dir:
        say("✔ Installed, but I couldn't locate the radiolog command file.")
        say(f"  You can always run it as:  {Path(sys.executable).name} -m radiolog")
        return

    if IN_VENV:
        say(f"✔ radiolog installed into the virtual environment ({sys.prefix}).")
        say("  It's available whenever that environment is activated.")
        return

    if on_path(script_dir):
        say("✔ radiolog installed. Try it now:  radiolog --help")
        return

    if not fix_path:
        say(f"✔ Installed to {script_dir}, which is not on your PATH (left unchanged).")
        return

    if IS_WIN:
        add_to_path_windows(script_dir)
        say(f"✔ radiolog installed, and {script_dir} was added to your PATH.")
    else:
        changed = add_to_path_unix(script_dir)
        if changed:
            where = ", ".join(str(c) for c in changed)
            say(f"✔ radiolog installed, and {script_dir} was added to PATH in {where}.")
        else:
            say("✔ radiolog installed (PATH was already set up in your shell config).")
    say("  Open a NEW terminal window and run:  radiolog --help")


# --------------------------------------------------------------------------- uninstall

def uninstall():
    say("Removing radiolog…")
    code, _ = run([sys.executable, "-m", "pip", "uninstall", "-y", "radiolog"])
    if code != 0:
        run(["pipx", "uninstall", "radiolog"])
    say("\n✔ radiolog removed. Your song history was kept.")


if __name__ == "__main__":
    if sys.version_info < (3, 8):
        raise SystemExit("radiolog needs Python 3.8 or newer.")
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install(fix_path="--no-path" not in sys.argv)
