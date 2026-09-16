# ReadRaw

Right-click Thermo Scientific mass-spec `.raw` files (or a folder of them) and export their metadata to Markdown.

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

- One Markdown file is written next to each `.raw`, using the same name (`sample.raw` → `sample.md`). If that name is already taken, it becomes `sample_1.md`, `sample_2.md`, …
- If two or more files are processed, a combined `raw_metadata.md` is also written in the shared folder, with comparison tables across all samples.
- Individual files are read in parallel.

## Command line

```bat
.venv\Scripts\python.exe read_raw_metadata.py path\to\file.raw
.venv\Scripts\python.exe read_raw_metadata.py path\to\folder
.venv\Scripts\python.exe read_raw_metadata.py file1.raw file2.raw -j 4
```

## Uninstall

Double-click `uninstall.bat` to remove the context-menu entries. The project folder is left in place.
