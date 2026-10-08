# venvfinder

Find every Python virtual environment on your disk and see what's inside it —
without activating anything.

Scientists and developers accumulate environments: a `.venv` per project, a few
conda envs, the odd `virtualenv` from years ago. `venvfinder` walks a directory
tree, spots the markers each tool leaves behind (`pyvenv.cfg`, `conda-meta/`),
and reports for each environment:

- which tool created it (**uv**, plain `venv`/`virtualenv`, or **conda**)
- the Python version it runs
- the base interpreter it was built from
- the packages installed in it

It ships with a Tkinter GUI (the default) and a headless CLI that can emit JSON.
Pure standard library — no dependencies.

## Install

Try it without installing anything:

```bash
uvx venvfinder
```

Or install it as a tool:

```bash
uv tool install venvfinder      # or: pipx install venvfinder
```

## Usage

```bash
venvfinder                                  # open the GUI
venvfinder --cli ~/projects                 # scan a tree, print a summary
venvfinder --cli ~/projects --uv-only       # only uv-created environments
venvfinder --cli ~/projects --packages      # include installed packages
venvfinder --cli ~/projects --json out.json # full results as JSON
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--max-depth N` | How deep to descend (default 8) |
| `--font-size PT` | GUI text size; try 18–22 on a large display |
| `--scale F` | GUI Tk scaling factor, e.g. `2.0` for HiDPI |
| `--reset` | Forget the saved window geometry and text size |

`python -m venvfinder` works as well.

## Standalone app

For colleagues who don't have Python installed, `build.sh` produces a
single-file executable (and a `.app` bundle on macOS) with PyInstaller:

```bash
./build.sh          # output lands in dist/
```

Builds are per-platform; run it on the OS you want to distribute for.
The macOS app is unsigned, so recipients will need to right-click → Open
the first time.

## Development

```bash
git clone https://github.com/saltikov/venvfinder
cd venvfinder
uv sync                 # creates .venv with the dev tools
uv run venvfinder       # run from source
uv run ruff check .     # lint
uv build                # build sdist + wheel into dist/
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
