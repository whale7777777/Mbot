# AGENTS.md

## Cursor Cloud specific instructions

Qbot is an AI-oriented quantitative trading platform. The core product is a **wxPython
desktop GUI** (`main.py`) plus several optional companion services (a Vue web trader, a Go
stock screener, JS fund tools). The Python environment is a **conda env named `qbot`
(Python 3.9)** living at `~/miniconda3/envs/qbot`.

### Activating the environment
`conda init bash` has already been run, so `conda activate qbot` works in a fresh login
shell. In a non-login/non-interactive shell, first run:

```
source ~/miniconda3/etc/profile.d/conda.sh
conda activate qbot
```

### Running the core product (Qbot GUI)
A VNC X server is available on `DISPLAY=:1`. Launch the GUI with:

```
conda activate qbot
DISPLAY=:1 python main.py
```

The GUI loads several tabs; most "回测/交易/加载数据" (backtest / trade / load-data)
buttons are gated behind a paywall in this open-source build and simply pop up a
"付费功能" dialog — that is expected, not a bug. Charts render from bundled local CSVs.

### WebView gotcha (important, non-obvious)
The GUI embeds `wx.html2.WebView` (the "投研智库"/"AI 选股" tabs). Getting WebView working
on this VM required specific, fragile steps that are already baked into the environment:

- wxPython is the **official Linux wheel** (`wxPython==4.2.2`, ubuntu-24.04 cp39 from
  `extras.wxpython.org`), NOT the conda-forge `wxpython` build (conda-forge has no WebKit
  backend and `WebView.New` raises `NotImplementedError`).
- System WebKit/GTK runtime libs are installed via apt (`libwebkit2gtk-4.1-0`, `libgtk-3-0`, etc.).
- The conda-forge `gstreamer` and `gst-plugins-base` packages were **removed** from the
  `qbot` env. They shadowed the system `libgstgl-1.0.so.0` and caused
  `undefined symbol: gst_gl_display_egl_new_with_egl_display` when importing `wx.html2`.
  **Do NOT reinstall conda-forge `wxpython`, `gstreamer`, or `gst-plugins-base`** into the
  `qbot` env or WebView will break again.

### Dependency notes
- The scientific/binary stack (`numpy`, `pandas`, `scipy`, `statsmodels`, `scikit-learn`,
  `matplotlib`, `pillow`, `ta-lib`) is installed from **conda-forge**, not pip.
- Do **not** `pip install -r requirements.txt` / `dev/requirements.txt` verbatim: the pinned
  `matplotlib==3.2.2` fails to build on Python 3.9, and `anyjson` (in
  `pytrader/requirements.txt`) fails to build on modern setuptools. These pins are ignored;
  a newer working matplotlib is installed instead.
- Keep **`numpy<2`**. `empyrical`/`backtrader`/`akshare` use numpy aliases removed in numpy 2.
  (`opencv-python-headless` wants `numpy>=2`, but that only affects `ddddocr`, used solely for
  broker-login captcha OCR — irrelevant to the GUI/backtests.)

### Lint / test
- Lint: `flake8` (config in `.flake8`), plus `black` and `isort` are installed. e.g. `flake8 main.py`.
- `pytrader/test_backtrade.py` (the example backtest from `env_setup.sh`) currently fails for
  two **pre-existing, non-environment** reasons: it imports `easyquant.easytrader` which does
  not exist in the repo tree (the module is at `pytrader/easytrader`), and it uses
  `quotation="jqdata"` which needs JoinQuant (`jqdatasdk`) credentials. This is a repo/data
  issue, not a setup issue.

### Optional companion services (not required for the core GUI)
- `pytrader/` — FastAPI backend (`web_server.py`, port 8000) + Vue 3 frontend
  (`pytrader/frontend`, `npm run dev`). Node 22 available.
- `qbot/plugins/investool` — Go 1.22 stock screener (`go run`).
- `pyfunds/fund-strategies`, `pyfunds/web-extension` — JS fund tools.
These need external market-data/broker API tokens for real data.
