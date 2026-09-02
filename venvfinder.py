#!/usr/bin/env python3
"""
venvfinder — locate Python virtual environments (uv, venv, virtualenv) on disk
and inspect their Python version and installed packages.

Every venv leaves a `pyvenv.cfg` at its root; uv-created venvs additionally
write a `uv = <version>` line into it. This tool scans a directory tree for
those markers, then reads each venv's site-packages to list packages without
having to activate or invoke anything.

GUI (default):   python venvfinder.py
CLI:             python venvfinder.py --cli ~/projects
                 python venvfinder.py --cli ~/projects --uv-only --packages
                 python venvfinder.py --cli ~/projects --json out.json

Stdlib only — no dependencies. Run it however you like, e.g. `uv run venvfinder.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Directories that never contain a venv worth reporting and are expensive to walk.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".idea", ".vscode", ".Trash", ".cache",
    "Library",  # macOS user Library is huge and full of app caches
}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Package:
    name: str
    version: str
    editable: bool = False


@dataclass
class VenvInfo:
    path: Path
    python_version: str = "?"
    uv_version: str | None = None          # non-None => created by uv
    home: str | None = None                # base interpreter location
    prompt: str | None = None
    system_site: bool = False
    packages: list[Package] = field(default_factory=list)
    error: str | None = None

    @property
    def is_uv(self) -> bool:
        return self.uv_version is not None

    def activate_cmd(self) -> str:
        if os.name == "nt":
            return str(self.path / "Scripts" / "activate")
        return f"source {self.path / 'bin' / 'activate'}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        d["is_uv"] = self.is_uv
        d["base_label"] = classify_base(self.home)
        return d


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_pyvenv_cfg(venv: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return cfg
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            cfg[k.strip().lower()] = v.strip()
    return cfg


def find_site_packages(venv: Path) -> Path | None:
    # Windows layout
    win = venv / "Lib" / "site-packages"
    if win.is_dir():
        return win
    # POSIX layout: lib/pythonX.Y/site-packages  (also matches pypy* dirs)
    lib = venv / "lib"
    if lib.is_dir():
        try:
            for child in sorted(lib.iterdir()):
                sp = child / "site-packages"
                if sp.is_dir():
                    return sp
        except OSError:
            pass
    return None


def python_version(venv: Path, cfg: dict[str, str]) -> str:
    # Prefer the concrete lib/pythonX.Y path; fall back to the cfg fields.
    lib = venv / "lib"
    if lib.is_dir():
        try:
            for child in lib.iterdir():
                name = child.name
                if name.startswith("python") and child.is_dir():
                    return name.replace("python", "")
        except OSError:
            pass
    return cfg.get("version_info") or cfg.get("version") or "?"


def classify_base(home: str | None) -> str:
    """Short label for where a venv's base interpreter came from, derived from
    the `home` path in pyvenv.cfg. Order matters (MacPorts also uses a
    Python.framework path, so it must be checked before python.org)."""
    if not home:
        return "unknown"
    low = home.lower()
    if "uv/python" in low or "/uv/cpython" in low or "share/uv" in low:
        return "uv-managed"
    if any(k in low for k in ("anaconda", "miniconda", "miniforge", "mambaforge")) or "/conda" in low:
        return "Conda"
    if "pyenv" in low:
        return "pyenv"
    if "/opt/homebrew" in low or "/usr/local/cellar" in low or "/usr/local/opt/" in low or "homebrew" in low:
        return "Homebrew"
    if "/opt/local/" in low:
        return "MacPorts"
    if "python.framework" in low:
        return "python.org"
    if low.startswith(("/usr/bin", "/usr/lib", "/bin", "/sbin", "/system/", "/library/")):
        return "system"
    if "/microsoft/windowsapps" in low or "\\windowsapps" in low:
        return "Microsoft Store"
    return "system"


def list_packages(site_packages: Path) -> list[Package]:
    pkgs: list[Package] = []
    try:
        entries = list(site_packages.iterdir())
    except OSError:
        return pkgs
    for entry in entries:
        name = entry.name
        if name.endswith(".dist-info"):
            stem = name[: -len(".dist-info")]
        elif name.endswith(".egg-info"):
            stem = name[: -len(".egg-info")]
        else:
            continue
        if "-" in stem:
            pkg_name, ver = stem.rsplit("-", 1)
        else:
            pkg_name, ver = stem, "?"
        editable = (entry / "direct_url.json").exists() if entry.is_dir() else False
        if editable:
            try:
                data = json.loads((entry / "direct_url.json").read_text())
                editable = bool(data.get("dir_info", {}).get("editable"))
            except Exception:
                editable = False
        pkgs.append(Package(pkg_name.replace("_", "-"), ver, editable))
    pkgs.sort(key=lambda p: p.name.lower())
    return pkgs


def list_conda_meta_packages(conda_root: Path) -> list[Package]:
    """Read package records from conda-meta/*.json for packages that conda
    installs without creating a .dist-info entry in site-packages."""
    conda_meta = conda_root / "conda-meta"
    pkgs: list[Package] = []
    try:
        for f in sorted(conda_meta.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                name = data.get("name", "")
                version = data.get("version", "?")
                if name:
                    pkgs.append(Package(name.replace("_", "-"), str(version), False))
            except Exception:
                continue
    except OSError:
        pass
    return pkgs


def build_venv_info(venv: Path, load_packages: bool = True) -> VenvInfo:
    cfg = parse_pyvenv_cfg(venv)
    info = VenvInfo(
        path=venv,
        python_version=python_version(venv, cfg),
        uv_version=cfg.get("uv"),
        home=cfg.get("home"),
        prompt=cfg.get("prompt"),
        system_site=cfg.get("include-system-site-packages", "false").lower() == "true",
    )
    if load_packages:
        sp = find_site_packages(venv)

        # Detect the conda environment root. Two cases:
        # 1. The venv itself IS a conda env (conda creates pyvenv.cfg in its envs).
        # 2. The venv was created with a conda Python as the base interpreter
        #    (home in pyvenv.cfg points into a conda installation).
        conda_root: Path | None = None
        if (venv / "conda-meta").is_dir():
            conda_root = venv
        elif info.home and classify_base(info.home) == "Conda":
            candidate = Path(info.home).parent
            if (candidate / "conda-meta").is_dir():
                conda_root = candidate

        if sp is None and conda_root is None:
            info.error = "site-packages not found"
        else:
            seen: dict[str, Package] = {}

            # Venv's own site-packages first (highest priority).
            if sp:
                for p in list_packages(sp):
                    seen[p.name.lower()] = p

            # For venvs built on top of a conda env (not the conda env itself),
            # also pull in packages from the conda env's site-packages so all
            # installed packages are visible, not just what was pip-installed
            # into the thin venv wrapper.
            if conda_root is not None and conda_root != venv:
                conda_sp = find_site_packages(conda_root)
                if conda_sp:
                    for p in list_packages(conda_sp):
                        if p.name.lower() not in seen:
                            seen[p.name.lower()] = p

            # Supplement with conda-meta records for any packages that conda
            # installed without writing a .dist-info into site-packages.
            if conda_root is not None:
                for p in list_conda_meta_packages(conda_root):
                    if p.name.lower() not in seen:
                        seen[p.name.lower()] = p

            info.packages = sorted(seen.values(), key=lambda p: p.name.lower())
    return info


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def iter_scan(
    root: Path,
    max_depth: int | None = None,
    uv_only: bool = False,
    follow_symlinks: bool = False,
    load_packages: bool = True,
):
    """Generator over a directory tree.

    Yields ("venv", VenvInfo) for each environment found and ("tick", dirpath)
    for every directory visited, so a caller can show progress or stay
    responsive between finds. A directory containing pyvenv.cfg is a venv root;
    we record it and do not descend into it.
    """
    root = root.expanduser()
    root_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        yield ("tick", dirpath)
        here = Path(dirpath)

        if "pyvenv.cfg" in filenames:
            info = build_venv_info(here, load_packages=load_packages)
            if not (uv_only and not info.is_uv):
                yield ("venv", info)
            dirnames[:] = []  # never descend into a venv
            continue

        # Prune noise and enforce depth.
        depth = len(here.parts) - root_depth
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not (d.startswith(".") and d not in (".venv", ".virtualenvs"))
        ]


def scan(
    root: Path,
    max_depth: int | None = None,
    uv_only: bool = False,
    follow_symlinks: bool = False,
    load_packages: bool = True,
    on_found=None,
    should_stop=None,
) -> list[VenvInfo]:
    """Eager wrapper around iter_scan, used by the CLI."""
    results: list[VenvInfo] = []
    for kind, payload in iter_scan(
        root, max_depth=max_depth, uv_only=uv_only,
        follow_symlinks=follow_symlinks, load_packages=load_packages,
    ):
        if should_stop and should_stop():
            break
        if kind == "venv":
            results.append(payload)
            if on_found:
                on_found(payload)
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run_cli(args) -> int:
    root = Path(args.path)
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    print(f"Scanning {root.expanduser()} ...", file=sys.stderr)
    start = time.time()
    found = scan(
        root,
        max_depth=args.max_depth,
        uv_only=args.uv_only,
        load_packages=args.packages or args.json,
    )
    elapsed = time.time() - start

    if args.json:
        payload = [v.to_dict() for v in found]
        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"Wrote {len(found)} venv(s) to {args.json} ({elapsed:.1f}s)", file=sys.stderr)
        return 0

    if not found:
        print("No virtual environments found.", file=sys.stderr)
        return 0

    for v in found:
        tag = f"uv {v.uv_version}" if v.is_uv else "std"
        pkgcount = f"{len(v.packages)} pkgs" if args.packages else ""
        base = classify_base(v.home)
        print(f"[{tag:>10}]  {v.python_version:<9}  {base:<12}  {pkgcount:<9}  {v.path}")
        if args.packages:
            for p in v.packages:
                mark = " (editable)" if p.editable else ""
                print(f"                 {p.name}=={p.version}{mark}")
    print(f"\n{len(found)} venv(s) in {elapsed:.1f}s", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# Persistent settings (window size/position, text size, sash, last folder)
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "venvfinder"


def config_file() -> Path:
    return config_dir() / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(config_file().read_text())
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = config_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(config_file())
    except Exception:
        pass  # settings are best-effort; never break the app over them


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
def run_gui(scale: float | None = None, font_size: int | None = None) -> int:
    try:
        import re
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk, filedialog, messagebox
    except Exception:
        print("tkinter is not available; use the CLI mode instead:", file=sys.stderr)
        print("  python venvfinder.py --cli <directory> --packages", file=sys.stderr)
        return 1

    # CLI flags win; otherwise fall back to saved settings, then built-in defaults.
    settings = load_settings()
    if font_size is None:
        font_size = int(settings.get("font_size", 13))
    if scale is None:
        scale = settings.get("scale")  # may be None

    class App(tk.Tk):
        def __init__(self, scale=None, font_size=13, settings=None):
            super().__init__()
            self.title("venvfinder — Python virtual environments")

            self.settings = settings or {}
            self.scale = scale
            self.font_size = font_size
            self._default_size = font_size

            self.venvs: list[VenvInfo] = []
            self._scan_gen = None
            self._stop_requested = False
            self._start = 0.0

            # Location column: user-resizable (stretch=False). We auto-fill it to
            # the available width until the user drags it, then respect + remember.
            self._loc_custom = bool(self.settings.get("loc_custom", False))
            self._loc_width = self.settings.get("loc_width")
            self._loc_auto = 0

            # HiDPI / big-display handling must happen before widgets are built
            # so they inherit the scaled fonts and row heights.
            self._apply_scaling()

            geo = self.settings.get("geometry")
            if geo:
                self._restore_geometry(geo)
            else:
                w = max(900, int(font_size * 78))
                h = max(560, int(font_size * 46))
                self.geometry(f"{w}x{h}")
            self.minsize(int(max(900, font_size * 78) * 0.7),
                         int(max(560, font_size * 46) * 0.7))

            self._build_controls()
            self._build_panes()
            self._build_statusbar()
            self._apply_scaling()          # re-apply now that widgets exist
            self._bind_zoom()
            # Start with the list pane wider; the divider stays drag-adjustable.
            self.after(60, self._init_sash)
            # Save size/position/text-size/etc. when the window is closed.
            self.protocol("WM_DELETE_WINDOW", self._on_close)

            if os.environ.get("VF_SELFTEST"):
                self.after(200, self._selftest)

        # ---- settings persistence -------------------------------------- #
        def _restore_geometry(self, geo: str):
            # Guard against restoring off-screen if the display changed.
            m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", geo)
            if not m:
                try:
                    self.geometry(geo)
                except Exception:
                    pass
                return
            w, h, x, y = map(int, m.groups())
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, h = min(w, sw), min(h, sh)
            if x < 0 or y < 0 or x > sw - 100 or y > sh - 100:
                x, y = 60, 60
            self.geometry(f"{w}x{h}+{x}+{y}")

        def _save_state(self):
            frac = None
            try:
                pw = self.paned.winfo_width()
                if pw > 1:
                    frac = round(self.paned.sashpos(0) / pw, 4)
            except Exception:
                pass
            data = dict(self.settings)  # keep any keys we don't manage
            data.update({
                "geometry": self.winfo_geometry(),
                "font_size": self.font_size,
                "scale": self.scale,
                "last_dir": self.dir_var.get(),
                "depth": self.depth_var.get(),
                "uv_only": bool(self.uv_only.get()),
                "loc_custom": bool(getattr(self, "_loc_custom", False)),
            })
            try:
                if self._loc_custom:
                    data["loc_width"] = self.tree.column("#0", "width")
            except Exception:
                pass
            if frac is not None:
                data["sash_fraction"] = frac
            save_settings(data)

        def _on_close(self):
            self._save_state()
            self.destroy()

        def _selftest(self):
            style = ttk.Style(self)
            print("loaded settings keys:", sorted(self.settings.keys()))
            print("startup font_size:", self.font_size,
                  "geometry:", self.winfo_geometry())
            for sz in (self._default_size, 24):
                self._set_font_size(sz)
                self.update_idletasks()
                base = tkfont.nametofont("TkDefaultFont")
                rowh = style.lookup("Treeview", "rowheight")
                ls = base.metrics("linespace")
                clipped = "CLIPPED" if int(rowh) < ls else "ok"
                cw = {c: self.tree.column(c, "width") for c in ("#0", "kind", "python", "pkgs")}
                mw = {c: self.tree.column(c, "minwidth") for c in ("kind", "python", "pkgs")}
                print(f"font={base.actual('size')}pt linespace={ls} "
                      f"rowheight={rowh} -> {clipped}")
                print(f"    col widths {cw}  minwidths {mw}")
            print("geometry:", self.winfo_width(), "x", self.winfo_height())
            try:
                print("sash pos:", self.paned.sashpos(0), "of", self.paned.winfo_width())
            except Exception as e:
                print("sash check failed:", e)

            # --- new: base column, labels, and manual-resize behavior ---
            self._set_font_size(self._default_size)
            self.update_idletasks()
            samples = [
                VenvInfo(path=Path("/proj/uvenv"), python_version="3.13",
                         uv_version="0.5.9",
                         home="/Users/x/.local/share/uv/python/cpython-3.13.1-macos-x86_64-none/bin"),
                VenvInfo(path=Path("/proj/brew"), python_version="3.13",
                         home="/usr/local/opt/python@3.13/bin"),
                VenvInfo(path=Path("/proj/conda"), python_version="3.11",
                         home="/Users/x/opt/anaconda3/bin"),
            ]
            for s in samples:
                self._add_venv(s)
            rows = [(self.tree.set(iid, "base")) for iid in self.tree.get_children()]
            print("base column values:", rows)
            self.base_filter_var.set("Homebrew")
            self._render_list()
            print("rows after Homebrew filter:", len(self.tree.get_children()))
            self.base_filter_var.set("All")
            self._render_list()

            self.update_idletasks()
            auto = self.tree.column("#0", "width")
            print("Location auto-fill width:", auto, "custom?", self._loc_custom)
            self.tree.column("#0", width=auto + 120)   # simulate a manual drag
            self._on_tree_release()
            print("after manual drag -> custom?", self._loc_custom,
                  "width:", self.tree.column("#0", "width"))
            self._fit_location()  # should NOT override a custom width now
            print("after _fit_location (should be unchanged):",
                  self.tree.column("#0", "width"))

            if os.environ.get("VF_SELFTEST_SAVE"):
                self._save_state()
                print("saved to:", config_file())
                try:
                    print("file:", config_file().read_text())
                except Exception as e:
                    print("readback failed:", e)
            self.destroy()

        # ---- HiDPI / scaling ------------------------------------------- #
        def _apply_scaling(self):
            # Tk 'scaling' is pixels-per-point (1 pt = 1/72"). Default ~1.333
            # at 96 dpi; raising it enlarges everything measured in points.
            if self.scale:
                try:
                    self.tk.call("tk", "scaling", float(self.scale))
                except Exception:
                    pass
            size = self.font_size
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                         "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
                         "TkIconFont", "TkTooltipFont", "TkFixedFont"):
                try:
                    tkfont.nametofont(name).configure(size=size)
                except tk.TclError:
                    pass
            base = tkfont.nametofont("TkDefaultFont")
            # The real cause of "squished" rows: default rowheight doesn't grow
            # with the font, so text gets clipped. Tie it to the font's height.
            rowh = int(base.metrics("linespace") * 1.35) + 4
            style = ttk.Style(self)
            style.configure(".", font=base)
            style.configure("Treeview", font=base, rowheight=rowh)
            style.configure("Treeview.Heading",
                            font=(base.actual("family"), size, "bold"))
            if hasattr(self, "detail"):
                self.detail.configure(font="TkFixedFont")
            if hasattr(self, "zoom_lbl"):
                self.zoom_lbl.configure(text=f"{size}pt")
            self._resize_columns()

        def _resize_columns(self):
            # Size each column from the CURRENT font so nothing clips or squishes
            # when the text is scaled up. The data columns get exactly what their
            # heading/values need plus a minwidth; Location (#0) is user-resizable
            # (stretch=False) and auto-filled by _fit_location.
            base = tkfont.nametofont("TkDefaultFont")
            pad = base.measure("00") * 2

            def need(*samples):
                return max(base.measure(s) for s in samples) + pad

            if hasattr(self, "tree"):
                kind_w = need("Kind", "uv 0.5.99")
                py_w = need("Python", "3.12.99")
                base_w = need("Base", "Microsoft Store")
                pk_w = need("Pkgs", "9999")
                self.tree.column("kind", width=kind_w, minwidth=kind_w, stretch=False)
                self.tree.column("python", width=py_w, minwidth=py_w, stretch=False)
                self.tree.column("base", width=base_w, minwidth=base_w, stretch=False)
                self.tree.column("pkgs", width=pk_w, minwidth=pk_w, stretch=False, anchor="e")
                self.tree.column("#0", minwidth=need("/a/path/x"), stretch=False)
                self._fit_location()
            if hasattr(self, "pkg_tree"):
                ver_w = need("Version", "1.2.3.post1")
                self.pkg_tree.column("version", width=ver_w, minwidth=ver_w, stretch=False)
                self.pkg_tree.column("#0", minwidth=need("package-name"), stretch=True)

        def _fit_location(self, event=None):
            # Expand Location to use the space the other columns don't need —
            # unless the user has manually resized it (then honor their width).
            if getattr(self, "_loc_custom", False) or not hasattr(self, "tree"):
                return
            try:
                other = sum(self.tree.column(c, "width")
                            for c in ("kind", "python", "base", "pkgs"))
                avail = self.tree.winfo_width()
                target = max(self.tree.column("#0", "minwidth"), avail - other - 4)
                self.tree.column("#0", width=target)
                self._loc_auto = self.tree.column("#0", "width")
            except Exception:
                pass

        def _on_tree_release(self, event=None):
            # If the Location width no longer matches what we last auto-set, the
            # user dragged it — switch to manual mode and remember it from now on.
            if not hasattr(self, "tree"):
                return
            try:
                cur = self.tree.column("#0", "width")
            except Exception:
                return
            if not self._loc_custom and abs(cur - self._loc_auto) > 2:
                self._loc_custom = True
            elif not self._loc_custom:
                self._fit_location()

        def _bind_zoom(self):
            self.bind_all("<Control-plus>", lambda e: self._bump_font(1))
            self.bind_all("<Control-equal>", lambda e: self._bump_font(1))
            self.bind_all("<Control-KP_Add>", lambda e: self._bump_font(1))
            self.bind_all("<Control-minus>", lambda e: self._bump_font(-1))
            self.bind_all("<Control-KP_Subtract>", lambda e: self._bump_font(-1))
            self.bind_all("<Control-0>", lambda e: self._set_font_size(self._default_size))
            self.bind_all("<Control-MouseWheel>",
                          lambda e: self._bump_font(1 if e.delta > 0 else -1))
            self.bind_all("<Control-Button-4>", lambda e: self._bump_font(1))
            self.bind_all("<Control-Button-5>", lambda e: self._bump_font(-1))
            # On macOS the native modifier is Command (⌘), so bind it too.
            if sys.platform == "darwin":
                self.bind_all("<Command-plus>", lambda e: self._bump_font(1))
                self.bind_all("<Command-equal>", lambda e: self._bump_font(1))
                self.bind_all("<Command-KP_Add>", lambda e: self._bump_font(1))
                self.bind_all("<Command-minus>", lambda e: self._bump_font(-1))
                self.bind_all("<Command-KP_Subtract>", lambda e: self._bump_font(-1))
                self.bind_all("<Command-0>", lambda e: self._set_font_size(self._default_size))
                self.bind_all("<Command-MouseWheel>",
                              lambda e: self._bump_font(1 if e.delta > 0 else -1))

        def _set_font_size(self, size):
            self.font_size = max(7, min(48, int(size)))
            self._loc_custom = False   # a deliberate relayout; re-fit Location
            self._apply_scaling()

        def _init_sash(self, tries=0):
            # Place the divider so the venv list gets more room (saved fraction
            # if we have one, else ~62%). It remains drag-adjustable.
            frac = float(self.settings.get("sash_fraction", 0.62))
            try:
                w = self.paned.winfo_width()
                if w > 1:
                    self.paned.sashpos(0, int(w * frac))
                elif tries < 10:
                    self.after(60, lambda: self._init_sash(tries + 1))
            except Exception:
                pass

        def _bump_font(self, delta):
            self._set_font_size(self.font_size + delta)

        # ---- layout ---------------------------------------------------- #
        def _build_controls(self):
            bar = ttk.Frame(self, padding=(8, 8, 8, 4))
            bar.pack(fill="x")

            ttk.Label(bar, text="Directory:").pack(side="left")
            last_dir = self.settings.get("last_dir")
            start_dir = last_dir if last_dir and Path(last_dir).is_dir() else str(Path.home())
            self.dir_var = tk.StringVar(value=start_dir)
            self.dir_entry = ttk.Entry(bar, textvariable=self.dir_var)
            self.dir_entry.pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(bar, text="Browse…", command=self.on_browse).pack(side="left")

            opts = ttk.Frame(self, padding=(8, 0, 8, 6))
            opts.pack(fill="x")
            self.uv_only = tk.BooleanVar(value=bool(self.settings.get("uv_only", False)))
            ttk.Checkbutton(opts, text="uv-created only", variable=self.uv_only).pack(side="left")
            ttk.Label(opts, text="   Max depth:").pack(side="left")
            self.depth_var = tk.StringVar(value=str(self.settings.get("depth", "8")))
            ttk.Spinbox(opts, from_=1, to=30, width=4, textvariable=self.depth_var).pack(side="left", padx=(2, 0))
            ttk.Label(opts, text="   Base:").pack(side="left")
            self.base_filter_var = tk.StringVar(value="All")
            self.base_combo = ttk.Combobox(opts, textvariable=self.base_filter_var,
                                           width=13, state="readonly", values=["All"])
            self.base_combo.pack(side="left", padx=(2, 0))
            self.base_combo.bind("<<ComboboxSelected>>", lambda e: self._render_list())
            self.scan_btn = ttk.Button(opts, text="Scan", command=self.on_scan)
            self.scan_btn.pack(side="right")
            self.stop_btn = ttk.Button(opts, text="Stop", command=self.on_stop, state="disabled")
            self.stop_btn.pack(side="right", padx=(0, 6))

            # text-size zoom (also Ctrl +/- / Ctrl-0 / Ctrl-scroll)
            ttk.Button(opts, text="A+", width=3, command=lambda: self._bump_font(1)).pack(side="right", padx=(0, 8))
            self.zoom_lbl = ttk.Label(opts, text=f"{self.font_size}pt", width=5, anchor="center")
            self.zoom_lbl.pack(side="right")
            ttk.Button(opts, text="A−", width=3, command=lambda: self._bump_font(-1)).pack(side="right")
            ttk.Label(opts, text="Text size:").pack(side="right", padx=(12, 4))

        def _build_panes(self):
            paned = ttk.PanedWindow(self, orient="horizontal")
            paned.pack(fill="both", expand=True, padx=8, pady=4)
            self.paned = paned

            # left: venv list
            left = ttk.Frame(paned)
            cols = ("kind", "python", "base", "pkgs")
            self.tree = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="browse")
            self.tree.heading("#0", text="Location")
            self.tree.heading("kind", text="Kind")
            self.tree.heading("python", text="Python")
            self.tree.heading("base", text="Base")
            self.tree.heading("pkgs", text="Pkgs")
            self.tree.column("#0", anchor="w", stretch=False)
            self.tree.column("kind", anchor="w", stretch=False)
            self.tree.column("python", anchor="w", stretch=False)
            self.tree.column("base", anchor="w", stretch=False)
            self.tree.column("pkgs", anchor="e", stretch=False)
            if self._loc_custom and self._loc_width:
                try:
                    self.tree.column("#0", width=int(self._loc_width))
                except Exception:
                    pass
            vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
            hsb = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            vsb.grid(row=0, column=1, sticky="ns")
            hsb.grid(row=1, column=0, sticky="ew")
            left.rowconfigure(0, weight=1)
            left.columnconfigure(0, weight=1)
            self.tree.bind("<<TreeviewSelect>>", self.on_select)
            self.tree.bind("<Configure>", self._fit_location, add="+")
            self.tree.bind("<ButtonRelease-1>", self._on_tree_release, add="+")
            self.tree.tag_configure("uv", foreground="#5b21b6")
            paned.add(left, weight=4)

            # right: details
            right = ttk.Frame(paned)
            self.detail = tk.Text(right, height=6, wrap="word", relief="flat",
                                  font="TkFixedFont",
                                  background=self.cget("background"))
            self.detail.pack(fill="x", padx=2, pady=(0, 4))
            self.detail.configure(state="disabled")

            act = ttk.Frame(right)
            act.pack(fill="x", pady=(0, 4))
            ttk.Button(act, text="Copy path", command=self.copy_path).pack(side="left")
            ttk.Button(act, text="Copy activate cmd", command=self.copy_activate).pack(side="left", padx=6)
            ttk.Button(act, text="Reveal in file manager", command=self.reveal).pack(side="left")

            filt = ttk.Frame(right)
            filt.pack(fill="x")
            ttk.Label(filt, text="Filter packages:").pack(side="left")
            self.pkg_filter = tk.StringVar()
            self.pkg_filter.trace_add("write", lambda *_: self.refresh_packages())
            ttk.Entry(filt, textvariable=self.pkg_filter).pack(side="left", fill="x", expand=True, padx=6)

            pkg_frame = ttk.Frame(right)
            pkg_frame.pack(fill="both", expand=True, pady=(4, 0))
            self.pkg_tree = ttk.Treeview(pkg_frame, columns=("version",), show="tree headings")
            self.pkg_tree.heading("#0", text="Package")
            self.pkg_tree.heading("version", text="Version")
            self.pkg_tree.column("#0", anchor="w", stretch=True)
            self.pkg_tree.column("version", anchor="w", stretch=False)
            pvsb = ttk.Scrollbar(pkg_frame, orient="vertical", command=self.pkg_tree.yview)
            phsb = ttk.Scrollbar(pkg_frame, orient="horizontal", command=self.pkg_tree.xview)
            self.pkg_tree.configure(yscrollcommand=pvsb.set, xscrollcommand=phsb.set)
            self.pkg_tree.grid(row=0, column=0, sticky="nsew")
            pvsb.grid(row=0, column=1, sticky="ns")
            phsb.grid(row=1, column=0, sticky="ew")
            pkg_frame.rowconfigure(0, weight=1)
            pkg_frame.columnconfigure(0, weight=1)
            paned.add(right, weight=3)

        def _build_statusbar(self):
            self.status = tk.StringVar(value="Pick a directory and click Scan.")
            ttk.Label(self, textvariable=self.status, relief="sunken",
                      anchor="w", padding=(6, 3)).pack(fill="x", side="bottom")

        # ---- actions --------------------------------------------------- #
        def on_browse(self):
            d = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
            if d:
                self.dir_var.set(d)

        def on_scan(self):
            root = Path(self.dir_var.get()).expanduser()
            if not root.is_dir():
                messagebox.showerror("venvfinder", f"Not a directory:\n{root}")
                return
            self.venvs.clear()
            self.tree.delete(*self.tree.get_children())
            self.pkg_tree.delete(*self.pkg_tree.get_children())
            self._set_detail("")
            self.base_filter_var.set("All")
            self.base_combo["values"] = ["All"]
            self._stop_requested = False
            self.scan_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            try:
                depth = int(self.depth_var.get())
            except ValueError:
                depth = 8
            self.status.set(f"Scanning {root} …")
            self._start = time.time()
            # No background thread: Tkinter/X11 is not thread-safe (touching Tk
            # from another thread aborts with an xcb assertion on Linux). Drive
            # the scan cooperatively on the main loop in short time slices.
            self._scan_gen = iter_scan(
                root, max_depth=depth, uv_only=self.uv_only.get(), load_packages=True,
            )
            self.after(1, self._step_scan)

        def _step_scan(self):
            if self._stop_requested or self._scan_gen is None:
                self._finish_scan(stopped=True)
                return
            deadline = time.time() + 0.03  # ~30 ms slice keeps the UI fluid
            ticks = 0
            try:
                while time.time() < deadline:
                    kind, payload = next(self._scan_gen)
                    if kind == "venv":
                        self._add_venv(payload)
                    else:
                        ticks += 1
                        if ticks % 40 == 0:
                            self.status.set(f"Scanning… {len(self.venvs)} found — {payload}")
            except StopIteration:
                self._finish_scan(stopped=False)
                return
            self.after(1, self._step_scan)

        def _finish_scan(self, stopped: bool):
            self._scan_gen = None
            elapsed = time.time() - self._start
            n = len(self.venvs)
            nuv = sum(1 for v in self.venvs if v.is_uv)
            verb = "Stopped —" if stopped else "Found"
            self.status.set(f"{verb} {n} venv(s) — {nuv} uv-created — in {elapsed:.1f}s")
            self.scan_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            # Offer only the base types actually present, keeping the selection.
            present = sorted({classify_base(v.home) for v in self.venvs})
            self.base_combo["values"] = ["All"] + present
            if self.base_filter_var.get() not in ("All", *present):
                self.base_filter_var.set("All")
                self._render_list()

        def on_stop(self):
            self._stop_requested = True
            self.status.set("Stopping…")

        def _add_venv(self, info: VenvInfo):
            self.venvs.append(info)
            self._insert_row(len(self.venvs) - 1, info)

        def _insert_row(self, i: int, info: VenvInfo):
            label = classify_base(info.home)
            flt = self.base_filter_var.get() if hasattr(self, "base_filter_var") else "All"
            if flt != "All" and label != flt:
                return
            kind = f"uv {info.uv_version}" if info.is_uv else "std"
            tags = ("uv",) if info.is_uv else ()
            self.tree.insert(
                "", "end", iid=str(i),
                text=str(info.path),
                values=(kind, info.python_version, label, len(info.packages)),
                tags=tags,
            )

        def _render_list(self):
            # Rebuild the list from self.venvs applying the current base filter.
            self.tree.delete(*self.tree.get_children())
            for i, v in enumerate(self.venvs):
                self._insert_row(i, v)

        def _current(self) -> VenvInfo | None:
            sel = self.tree.selection()
            if not sel:
                return None
            return self.venvs[int(sel[0])]

        def on_select(self, _evt=None):
            v = self._current()
            if not v:
                return
            lines = [
                f"Path:    {v.path}",
                f"Kind:    {'uv ' + v.uv_version if v.is_uv else 'standard venv'}",
                f"Python:  {v.python_version}",
                f"Origin:  {classify_base(v.home)}",
            ]
            if v.home:
                lines.append(f"Base:    {v.home}")
            if v.system_site:
                lines.append("Note:    includes system site-packages")
            if v.error:
                lines.append(f"Warning: {v.error}")
            self._set_detail("\n".join(lines))
            self.refresh_packages()

        def refresh_packages(self):
            self.pkg_tree.delete(*self.pkg_tree.get_children())
            v = self._current()
            if not v:
                return
            needle = self.pkg_filter.get().lower().strip()
            for p in v.packages:
                if needle and needle not in p.name.lower():
                    continue
                label = p.name + ("  (editable)" if p.editable else "")
                self.pkg_tree.insert("", "end", text=label, values=(p.version,))

        def _set_detail(self, text: str):
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", text)
            self.detail.configure(state="disabled")

        # ---- small helpers -------------------------------------------- #
        def copy_path(self):
            v = self._current()
            if v:
                self.clipboard_clear()
                self.clipboard_append(str(v.path))
                self.status.set("Path copied to clipboard.")

        def copy_activate(self):
            v = self._current()
            if v:
                self.clipboard_clear()
                self.clipboard_append(v.activate_cmd())
                self.status.set("Activate command copied to clipboard.")

        def reveal(self):
            v = self._current()
            if not v:
                return
            p = str(v.path)
            try:
                if sys.platform == "darwin":
                    subprocess.run(["open", p])
                elif os.name == "nt":
                    subprocess.run(["explorer", p])
                else:
                    subprocess.run(["xdg-open", p])
            except Exception as e:
                messagebox.showerror("venvfinder", f"Could not open file manager:\n{e}")

    App(scale=scale, font_size=font_size, settings=settings).mainloop()
    return 0


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Locate and inspect Python virtual environments.")
    parser.add_argument("--cli", dest="path", metavar="DIR",
                        help="run headless: scan DIR and print results (no GUI)")
    parser.add_argument("--uv-only", action="store_true", help="only report uv-created venvs")
    parser.add_argument("--packages", action="store_true", help="list packages for each venv (CLI)")
    parser.add_argument("--max-depth", type=int, default=8, help="max directory depth to descend (default 8)")
    parser.add_argument("--json", metavar="FILE", help="write full results as JSON to FILE (CLI)")
    parser.add_argument("--font-size", type=int, default=None,
                        help="GUI base text size in points (default 13, or last used; try 18-22 on a TV)")
    parser.add_argument("--scale", type=float, default=None,
                        help="GUI Tk scaling factor (pixels per point; e.g. 2.0 for HiDPI)")
    parser.add_argument("--reset", action="store_true",
                        help="forget saved window size/position/text-size and start fresh")
    args = parser.parse_args(argv)

    if args.reset:
        try:
            config_file().unlink()
            print(f"Removed saved settings: {config_file()}", file=sys.stderr)
        except FileNotFoundError:
            print("No saved settings to remove.", file=sys.stderr)
        except Exception as e:
            print(f"Could not remove settings: {e}", file=sys.stderr)

    if args.path:
        return run_cli(args)
    if args.json:
        parser.error("--json requires --cli DIR")
    return run_gui(scale=args.scale, font_size=args.font_size)


if __name__ == "__main__":
    raise SystemExit(main())
