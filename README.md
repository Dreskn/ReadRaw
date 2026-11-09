# ReadRaw

Right-click Thermo Scientific mass-spec `.raw` files (or a folder of them) and get an HTML report of their metadata that opens in any web browser.

## Install once

```bat
git clone https://github.com/Dreskn/ReadRaw.git
cd ReadRaw
```

Then double-click `install.bat` (or run `install.ps1`).

That will create a local Python environment, download Thermo's official [RawFileReader](https://github.com/thermofisherlsms/RawFileReader) libraries, and add **Extract RAW metadata** to Explorer / Send to.

Using the Thermo libraries means you accept Thermo Fisher's RawFileReader license (see that repository).

## Use

Right-click one `.raw` file, several `.raw` files, or a folder → **Extract RAW metadata**.

- One HTML report is written next to each `.raw`, using the same name (`sample.raw` → `sample.html`). If that name is already taken, it becomes `sample_1.html`, `sample_2.html`, …
- If two or more files are processed, a combined `raw_metadata.html` is also written in the shared folder, with a summary table (one row per file, easy to paste into Excel) and comparison tables across all samples.
- Individual files are read in parallel.

## Command line

```bat
.venv\Scripts\python.exe read_raw_metadata.py path\to\file.raw
.venv\Scripts\python.exe read_raw_metadata.py path\to\folder
.venv\Scripts\python.exe read_raw_metadata.py file1.raw file2.raw -j 4
.venv\Scripts\python.exe read_raw_metadata.py path	oolder --format md
```

`--format md` writes Markdown reports instead of HTML. `--no-recursive` skips subfolders.

## Uninstall

Double-click `uninstall.bat` to remove the context-menu entries. The project folder is left in place.
