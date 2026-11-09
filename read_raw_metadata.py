#!/usr/bin/env python3
"""Extract metadata from Thermo Scientific mass-spec .raw files.

Works from the command line or from the Windows Explorer context menu
(right-click a .raw file, several .raw files, or a folder).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import sys
import traceback
import urllib.request
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
DLL_DIR = ROOT / "libs"
DLL_NAMES = (
    "ThermoFisher.CommonCore.Data.dll",
    "ThermoFisher.CommonCore.RawFileReader.dll",
    "ThermoFisher.CommonCore.BackgroundSubtraction.dll",
)
DLL_BASE_URL = (
    "https://raw.githubusercontent.com/thermofisherlsms/"
    "RawFileReader/main/Libs/Net471/"
)

ENERGY_RE = re.compile(r"@([A-Za-z]+)(\d+(?:\.\d+)?)")
RANGE_RE = re.compile(r"\[(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\]")
FILTER_MS_RE = re.compile(r"\bms(\d*)\b", re.I)
METHOD_RES_RE = re.compile(
    r"(?:orbitrap\s+)?resolution(?:\s*\([^)]*\))?\s*[:=]\s*([\d,]+)",
    re.I,
)
METHOD_RANGE_RE = re.compile(
    r"(?:scan range(?:\s*\(m/z\))?|mass range(?:\s*\(m/z\))?)\s*[:=]\s*([\d.]+)\s*(?:[-–]|to)\s*([\d.]+)",
    re.I,
)
METHOD_CYCLE_RE = re.compile(
    r"cycle time(?:\s*\([^)]*\))?\s*[:=]\s*([\d.]+)",
    re.I,
)
METHOD_TOPN_RE = re.compile(
    r"(?:top\s*n|topn|number of (?:dependent )?scans|loop count|dependent scans?)\s*[:=]\s*(\d+)",
    re.I,
)
METHOD_WINDOWS_RE = re.compile(
    r"(?:number of (?:dia )?windows|dia windows|(?<![a-z])windows)\s*[:=]\s*(\d+)",
    re.I,
)

SUMMARY_KEYS = [
    "file_name",
    "sample_name",
    "method_file",
    "acquisition_mode",
    "top_mode",
    "cycle_time",
    "fragmentation",
    "ms1_mz_range",
    "ms2_mz_range",
    "ms1_resolution",
    "ms2_resolution",
]

SUMMARY_LABELS = {
    "file_name": "File",
    "sample_name": "Sample",
    "method_file": "Instrument method",
    "acquisition_mode": "Acquisition",
    "top_mode": "Top mode",
    "cycle_time": "Cycle",
    "fragmentation": "Fragmentation",
    "ms1_mz_range": "MS mass range",
    "ms2_mz_range": "MS/MS mass range",
    "ms1_resolution": "MS resolution",
    "ms2_resolution": "MS/MS resolution",
}

# Header/filter/trailer only — never walk every spectrum in a multi-GB file.
SCAN_WALK_CAP = 250
MS2_TRAILERS_NEEDED = 3

RESOLUTION_TRAILER_KEYS = (
    "Orbitrap Resolution",
    "FT Resolution",
    "Resolution",
    "Orbitrap Resolution Setting",
)

PREFERRED_COLUMNS = [
    "file_name",
    "file_path",
    "file_size_mb",
    "error",
    "sample_name",
    "sample_id",
    "sample_type",
    "comment",
    "vial",
    "injection_volume",
    "sample_volume",
    "dilution_factor",
    "row_number",
    "barcode",
    "instrument_method",
    "processing_method",
    "calibration_file",
    "instrument_model",
    "instrument_name",
    "serial_number",
    "software_version",
    "firmware_version",
    "operator",
    "creation_date",
    "description",
    "raw_file_version",
    "rt_start_min",
    "rt_end_min",
    "duration_min",
    "mz_low",
    "mz_high",
    "mass_resolution",
    "scan_count",
    "first_scan",
    "last_scan",
    "ms1_scans",
    "ms2_scans",
    "ms3_scans",
    "other_scans",
    "polarity",
    "ionization",
    "mass_analyzer",
    "activation_types",
    "collision_energies",
    "isolation_widths",
    "precursor_masses",
    "faims_cv",
    "scan_mz_ranges",
    "unique_filter_count",
    "scan_filters",
    "instrument_method_names",
    "instrument_method_text",
]

LONG_FIELDS = {
    "instrument_method_text",
    "scan_filters",
}

FIELD_GROUPS = [
    (
        "Sample",
        [
            "sample_name",
            "sample_id",
            "sample_type",
            "comment",
            "vial",
            "injection_volume",
            "sample_volume",
            "dilution_factor",
            "row_number",
            "barcode",
            "user_text",
            "user_label",
        ],
    ),
    (
        "Method",
        [
            "instrument_method",
            "instrument_method_names",
            "processing_method",
            "calibration_file",
        ],
    ),
    (
        "Instrument",
        [
            "instrument_model",
            "instrument_name",
            "serial_number",
            "software_version",
            "firmware_version",
            "operator",
            "creation_date",
            "description",
            "raw_file_version",
        ],
    ),
    (
        "Acquisition",
        [
            "rt_start_min",
            "rt_end_min",
            "duration_min",
            "mz_low",
            "mz_high",
            "mass_resolution",
            "scan_count",
            "first_scan",
            "last_scan",
            "ms1_scans",
            "ms2_scans",
            "ms3_scans",
            "other_scans",
            "polarity",
            "ionization",
            "mass_analyzer",
            "activation_types",
            "collision_energies",
            "isolation_widths",
            "precursor_masses",
            "faims_cv",
            "scan_mz_ranges",
            "unique_filter_count",
        ],
    ),
]

# ---------------------------------------------------------------------------
# .NET / RawFileReader bootstrap
# ---------------------------------------------------------------------------

_API = None


def dlls_present() -> bool:
    return all((DLL_DIR / name).is_file() for name in DLL_NAMES[:2])


def download_dlls() -> None:
    DLL_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading Thermo RawFileReader libraries (official GitHub repo)...")
    print("Using them means you accept Thermo Fisher's RawFileReader license:")
    print("  https://github.com/thermofisherlsms/RawFileReader")
    for name in DLL_NAMES:
        dest = DLL_DIR / name
        if dest.is_file() and dest.stat().st_size > 0:
            print(f"  already present: {name}")
            continue
        url = DLL_BASE_URL + name
        print(f"  {name}")
        urllib.request.urlretrieve(url, dest)
        if dest.stat().st_size < 1000:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Download failed or too small: {url}")


def load_api():
    """Load pythonnet + Thermo assemblies. Cached after the first call."""
    global _API
    if _API is not None:
        return _API

    if not dlls_present():
        download_dlls()

    try:
        from pythonnet import load as load_runtime
    except ImportError as exc:
        raise RuntimeError(
            "pythonnet is not installed. Run install.ps1 (or: pip install pythonnet)."
        ) from exc

    for runtime in ("netfx", "coreclr"):
        try:
            load_runtime(runtime)
            break
        except Exception:
            continue

    import clr  # noqa: WPS433  (imported after the runtime is loaded)

    sys.path.insert(0, str(DLL_DIR))
    clr.AddReference(str(DLL_DIR / "ThermoFisher.CommonCore.Data.dll"))
    clr.AddReference(str(DLL_DIR / "ThermoFisher.CommonCore.RawFileReader.dll"))

    from ThermoFisher.CommonCore.Data.Business import Device
    from ThermoFisher.CommonCore.RawFileReader import RawFileReaderAdapter

    try:
        from System.Globalization import CultureInfo
        from System.Threading import Thread

        invariant = CultureInfo.InvariantCulture
        Thread.CurrentThread.CurrentCulture = invariant
        Thread.CurrentThread.CurrentUICulture = invariant
    except Exception:
        pass

    _API = (RawFileReaderAdapter, Device)
    return _API


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text
    if isinstance(value, (int, str)):
        return str(value).strip()

    # .NET DateTime
    if hasattr(value, "Year") and hasattr(value, "ToString"):
        try:
            return value.ToString("yyyy-MM-dd HH:mm:ss")
        except Exception:
            pass

    # .NET / Python sequences
    if hasattr(value, "Length") and hasattr(value, "GetValue") and not isinstance(value, (str, bytes)):
        try:
            parts = [stringify(value.GetValue(i)) for i in range(value.Length)]
            return "; ".join(p for p in parts if p)
        except Exception:
            pass
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        try:
            parts = [stringify(item) for item in value]
            return "; ".join(p for p in parts if p)
        except Exception:
            pass

    text = str(value).strip()
    return "" if text in {"None", "null"} else text


def net_properties(obj, prefix: str = "") -> OrderedDict[str, str]:
    """Flatten public .NET properties one level deep."""
    result: OrderedDict[str, str] = OrderedDict()
    if obj is None:
        return result
    try:
        props = obj.GetType().GetProperties()
    except Exception:
        return result

    for prop in props:
        name = prop.Name
        if name.startswith("_") or name in {"Item"}:
            continue
        key = f"{prefix}{name}"
        try:
            value = prop.GetValue(obj, None)
        except Exception:
            continue
        if value is None:
            continue
        if hasattr(value, "Length") and hasattr(value, "GetValue") and not isinstance(value, (str, bytes)):
            text = stringify(value)
            if text:
                result[key] = text
            continue
        try:
            type_name = value.GetType().FullName if hasattr(value, "GetType") else ""
        except Exception:
            type_name = ""
        is_simple = isinstance(value, (str, int, float, bool)) or (
            (type_name or "").startswith("System.")
            and "Collection" not in (type_name or "")
            and "[]" not in (type_name or "")
        )
        if is_simple or not hasattr(value, "GetType"):
            text = stringify(value)
            if text:
                result[key] = text
        else:
            nested = net_properties(value, prefix=f"{key}.")
            if nested:
                result.update(nested)
            else:
                text = stringify(value)
                if text:
                    result[key] = text
    return result


def join_unique(values: Iterable[object]) -> str:
    seen: list[str] = []
    for value in values:
        text = stringify(value)
        if text and text not in seen:
            seen.append(text)
    return "; ".join(seen)


def format_size_mb(path: Path) -> str:
    try:
        return f"{path.stat().st_size / (1024 * 1024):.2f}"
    except OSError:
        return ""


def collect_raw_paths(inputs: Iterable[str], recursive: bool = True) -> list[Path]:
    found: list[Path] = []
    for item in inputs:
        path = Path(item)
        try:
            path = path.resolve()
        except OSError:
            path = Path(item)
        if path.is_file() and path.suffix.lower() == ".raw":
            found.append(path)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            for child in iterator:
                if child.is_file() and child.suffix.lower() == ".raw":
                    found.append(child.resolve())
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def common_parent(paths: list[Path]) -> Path:
    if not paths:
        return Path.cwd()
    try:
        return Path(os.path.commonpath([str(p.parent) for p in paths]))
    except ValueError:
        return paths[0].parent


# ---------------------------------------------------------------------------
# Scan-filter parsing and metadata extraction
# ---------------------------------------------------------------------------


def parse_filter_string(filter_text: str) -> dict[str, set[str]]:
    parsed: dict[str, set[str]] = {
        "activation_types": set(),
        "collision_energies": set(),
        "scan_mz_ranges": set(),
    }
    for match in ENERGY_RE.finditer(filter_text):
        parsed["activation_types"].add(match.group(1).upper())
        energy = match.group(2).rstrip("0").rstrip(".")
        parsed["collision_energies"].add(energy)
    for match in RANGE_RE.finditer(filter_text):
        parsed["scan_mz_ranges"].add(f"{match.group(1)}-{match.group(2)}")
    return parsed


def filter_ms_order(filter_text: str) -> str:
    matches = FILTER_MS_RE.findall(filter_text or "")
    if not matches:
        return ""
    number = matches[-1]
    if number in {"", "1"}:
        return "ms1"
    return f"ms{number}"


def pretty_mz_range(low: str, high: str) -> str:
    return f"{stringify(float(low))}-{stringify(float(high))}"


def ranges_from_filters(filters: list[str]) -> dict[str, list[str]]:
    by_order: dict[str, list[str]] = {"ms1": [], "ms2": []}
    for text in filters:
        order = filter_ms_order(text)
        if order not in by_order:
            continue
        matches = RANGE_RE.findall(text)
        if not matches:
            continue
        low, high = matches[-1]
        pretty = pretty_mz_range(low, high)
        if pretty not in by_order[order]:
            by_order[order].append(pretty)
    return by_order


def fragmentation_from_filters(filters: list[str]) -> str:
    pairs: list[str] = []
    seen: set[str] = set()
    for text in filters:
        if filter_ms_order(text) not in {"ms2", "ms3"}:
            continue
        for match in ENERGY_RE.finditer(text):
            activation = match.group(1).upper()
            energy = match.group(2).rstrip("0").rstrip(".")
            item = f"{activation} {energy}"
            if item not in seen:
                seen.add(item)
                pairs.append(item)
    if not pairs:
        return ""
    by_type: dict[str, list[str]] = {}
    for item in pairs:
        activation, energy = item.split(" ", 1)
        by_type.setdefault(activation, []).append(energy)
    parts = []
    for activation, energies in by_type.items():
        parts.append(f"{activation} {'/'.join(energies)}")
    return "; ".join(parts)


def method_file_name(*candidates: str) -> str:
    for raw in candidates:
        if not raw:
            continue
        for part in re.split(r"[;|]", raw):
            part = part.strip().strip('"')
            if not part:
                continue
            name = Path(part.replace("\\", "/")).name
            if name:
                return name
    return ""


def _method_context(line: str) -> str:
    lowered = line.lower()
    if re.search(r"\bdia\b|data[-\s]*independent|tms2|tmss", lowered):
        return "dia"
    if re.search(
        r"ddms|dependent scan|data[-\s]*dependent|msn scan|ms/ms|ms2|tms²",
        lowered,
    ):
        return "ms2"
    if re.search(r"master scan|full ms(?!\d)|\bms1\b|scan type:\s*ms\b", lowered):
        return "ms1"
    return ""


def parse_method_hints(method_text: str) -> dict[str, str]:
    hints = {
        "acquisition_mode": "",
        "top_mode": "",
        "cycle_time": "",
        "msms_per_cycle": "",
        "ms1_mz_range": "",
        "ms2_mz_range": "",
        "ms1_resolution": "",
        "ms2_resolution": "",
    }
    if not method_text:
        return hints

    lowered = method_text.lower()
    if re.search(r"\bdia\b|data[-\s]*independent", lowered):
        hints["acquisition_mode"] = "DIA"
    elif re.search(r"\bdda\b|data[-\s]*dependent|\bddms", lowered):
        hints["acquisition_mode"] = "DDA"
    elif re.search(r"\bprm\b|\bsrm\b|targeted", lowered):
        hints["acquisition_mode"] = "Targeted"

    cycle = METHOD_CYCLE_RE.search(method_text)
    if cycle:
        hints["cycle_time"] = f"{cycle.group(1)} s"

    windows = METHOD_WINDOWS_RE.search(method_text)
    topn = METHOD_TOPN_RE.search(method_text)
    if windows:
        hints["msms_per_cycle"] = windows.group(1)
    elif topn:
        hints["msms_per_cycle"] = topn.group(1)

    if re.search(r"number of scans\s*[:=]\s*dynamic|top\s*speed|\btops\b", lowered):
        hints["top_mode"] = f"TopS {cycle.group(1)} s" if cycle else "TopS"
    else:
        if topn:
            hints["top_mode"] = f"TopN {topn.group(1)}"
        elif re.search(r"top\s*n|\btopn\b", lowered):
            hints["top_mode"] = "TopN"

    context = "ms1"
    for line in method_text.splitlines():
        new_context = _method_context(line)
        if new_context == "dia":
            hints["acquisition_mode"] = hints["acquisition_mode"] or "DIA"
            context = "ms2"
        elif new_context:
            context = new_context
        res = METHOD_RES_RE.search(line)
        if res:
            value = res.group(1).replace(",", "")
            key = "ms2_resolution" if context == "ms2" else "ms1_resolution"
            if not hints[key]:
                hints[key] = value
        span = METHOD_RANGE_RE.search(line)
        if span:
            pretty = pretty_mz_range(span.group(1), span.group(2))
            key = "ms2_mz_range" if context == "ms2" else "ms1_mz_range"
            if not hints[key]:
                hints[key] = pretty
    return hints


def infer_acquisition_mode(method_text: str, filters: list[str], ms2_count: int) -> str:
    hints = parse_method_hints(method_text)
    if hints["acquisition_mode"]:
        return hints["acquisition_mode"]
    joined = " ".join(filters).lower()
    if re.search(r"\bdia\b", joined):
        return "DIA"
    has_dependent = any(
        re.search(r"(^|\s)d(\s|$)", text) and "ms2" in text.lower() for text in filters
    )
    ms2_filters = [text for text in filters if filter_ms_order(text) == "ms2"]
    if has_dependent or re.search(r"\bdda\b|ddms", joined):
        return "DDA"
    if ms2_count == 0 and not ms2_filters:
        return "MS1"
    if len(ms2_filters) >= 5 and not has_dependent:
        return "DIA"
    if ms2_filters:
        return "DDA"
    return ""


def resolution_from_trailers(trailers: list[OrderedDict[str, str]]) -> str:
    values: list[str] = []
    for trailer in trailers:
        for key in RESOLUTION_TRAILER_KEYS:
            value = trailer.get(key, "")
            if value:
                compact = value.replace(",", "").strip()
                if compact and compact not in values:
                    values.append(compact)
    return join_unique(values)


def cycle_from_method(hints: dict[str, str]) -> str:
    if hints.get("cycle_time"):
        return hints["cycle_time"]
    count = hints.get("msms_per_cycle") or ""
    if count:
        return f"1MS {count}MSMS"
    return ""


def filter_template(filter_text: str) -> str:
    return re.sub(r"(\bms\d*\s+)\d+(?:\.\d+)?@", r"\1*@", filter_text, flags=re.I)


def inspect_scans(raw, first: int, last: int) -> dict:
    """Read only a short prefix of scan headers — no spectra, no full-file walk."""
    result = {
        "filters": [],
        "polarities": [],
        "ionizations": [],
        "analyzers": [],
        "activations": set(),
        "energies": set(),
        "ranges": set(),
        "faims": set(),
        "trailers_by_order": {"ms1": [], "ms2": []},
        "trailer_values": {},
        "reaction_energies": [],
        "isolation_widths": [],
        "precursor_masses": [],
        "saw_ms2": False,
    }
    if first <= 0 or last < first:
        return result

    seen_templates: set[str] = set()
    visited: set[int] = set()
    extra_points = [last]
    if last > first:
        extra_points.append(first + (last - first) // 2)

    def consume(scan: int, need_trailer: bool = True) -> str:
        if scan in visited or scan < first or scan > last:
            return ""
        visited.add(scan)
        filt = _safe_call(raw.GetFilterForScanNumber, scan)
        if filt is None:
            return ""
        # str() on the wrapped .NET filter gives its type name; ToString() gives the filter text.
        text = stringify(_safe_call(filt.ToString, default="")) or stringify(filt)
        order = filter_ms_order(text) or _ms_order_bucket(stringify(getattr(filt, "MSOrder", "")))
        extra = net_properties(filt)
        if extra.get("Polarity"):
            result["polarities"].append(extra["Polarity"])
        if extra.get("IonizationMode"):
            result["ionizations"].append(extra["IonizationMode"])
        if extra.get("MassAnalyzer"):
            result["analyzers"].append(extra["MassAnalyzer"])
        for key, value in extra.items():
            if "compensation" in key.lower() or key.lower() in {"cv", "faims"}:
                result["faims"].add(value)
        parsed = parse_filter_string(text)
        result["activations"].update(parsed["activation_types"])
        result["energies"].update(parsed["collision_energies"])
        result["ranges"].update(parsed["scan_mz_ranges"])
        template = filter_template(text)
        if template and template not in seen_templates:
            seen_templates.add(template)
            result["filters"].append(template)
        if order == "ms2":
            result["saw_ms2"] = True
        if need_trailer and order in result["trailers_by_order"]:
            if len(result["trailers_by_order"][order]) < (1 if order == "ms1" else MS2_TRAILERS_NEEDED):
                trailer = extract_trailer(raw, scan)
                if trailer:
                    result["trailers_by_order"][order].append(trailer)
                    for key, value in trailer.items():
                        result["trailer_values"].setdefault(key, set()).add(value)
                if order in {"ms2", "ms3"}:
                    for reaction in extract_reactions(raw, scan):
                        if reaction.get("CollisionEnergy"):
                            result["reaction_energies"].append(reaction["CollisionEnergy"])
                        if reaction.get("ActivationType"):
                            result["activations"].add(reaction["ActivationType"])
                        if reaction.get("IsolationWidth"):
                            result["isolation_widths"].append(reaction["IsolationWidth"])
                        if reaction.get("PrecursorMass"):
                            result["precursor_masses"].append(reaction["PrecursorMass"])
        return order

    walk_end = min(last, first + SCAN_WALK_CAP - 1)
    for scan in range(first, walk_end + 1):
        consume(scan)
        if result["trailers_by_order"]["ms1"] and (
            result["trailers_by_order"]["ms2"] or not result["saw_ms2"]
        ):
            break

    for scan in extra_points:
        consume(scan, need_trailer=False)

    return result


def fill_summary_fields(
    record: OrderedDict[str, str],
    filters: list[str],
    method_text: str,
    trailers_by_order: dict[str, list[OrderedDict[str, str]]],
    saw_ms2: bool = False,
) -> None:
    hints = parse_method_hints(method_text)
    ranges = ranges_from_filters(filters)
    ms2_count = 1 if saw_ms2 or ranges["ms2"] else int(record.get("ms2_scans") or 0)
    mode = infer_acquisition_mode(method_text, filters, ms2_count)
    top_mode = hints["top_mode"]
    if mode == "DIA":
        top_mode = ""

    record["method_file"] = method_file_name(
        record.get("instrument_method", ""),
        record.get("instrument_method_names", ""),
    )
    record["acquisition_mode"] = mode
    record["top_mode"] = top_mode
    record["cycle_time"] = cycle_from_method(hints)
    record["fragmentation"] = fragmentation_from_filters(filters) or record.get(
        "collision_energies", ""
    )
    record["ms1_mz_range"] = join_unique(ranges["ms1"]) or hints["ms1_mz_range"]
    record["ms2_mz_range"] = join_unique(ranges["ms2"]) or hints["ms2_mz_range"]
    record["ms1_resolution"] = (
        resolution_from_trailers(trailers_by_order.get("ms1", []))
        or hints["ms1_resolution"]
    )
    record["ms2_resolution"] = (
        resolution_from_trailers(trailers_by_order.get("ms2", []))
        or hints["ms2_resolution"]
    )


def _ms_order_bucket(order_text: str) -> str:
    compact = order_text.replace(" ", "").lower()
    if compact in {"ms", "ms1", "1"}:
        return "ms1"
    if compact in {"ms2", "2"}:
        return "ms2"
    if compact in {"ms3", "3"}:
        return "ms3"
    return "other"


def _safe_call(func, *args, default=None):
    try:
        return func(*args)
    except Exception:
        return default


def extract_trailer(raw, scan: int) -> OrderedDict[str, str]:
    data = _safe_call(raw.GetTrailerExtraInformation, scan)
    result: OrderedDict[str, str] = OrderedDict()
    if data is None:
        return result
    length = getattr(data, "Length", 0) or 0
    labels = getattr(data, "Labels", None)
    values = getattr(data, "Values", None)
    if labels is None or values is None:
        return result
    for i in range(length):
        label = stringify(labels[i]).rstrip(":").strip()
        value = stringify(values[i])
        if label and value:
            result[label] = value
    return result


def extract_reactions(raw, scan: int) -> list[dict[str, str]]:
    event = _safe_call(raw.GetScanEventForScanNumber, scan)
    if event is None:
        return []
    reactions = []
    count = _safe_call(lambda: int(event.MassCount), default=0) or 0
    # MassCount can be precursor count; GetReaction is 0-based.
    if count <= 0:
        count = 4
    for index in range(count):
        reaction = _safe_call(event.GetReaction, index)
        if reaction is None:
            break
        item = net_properties(reaction)
        if item:
            reactions.append(item)
    return reactions


def extract_status_log(raw, scan: int) -> OrderedDict[str, str]:
    result: OrderedDict[str, str] = OrderedDict()
    time = _safe_call(raw.RetentionTimeFromScanNumber, scan)
    if time is None:
        return result
    entry = _safe_call(raw.GetStatusLogForRetentionTime, time)
    if entry is None:
        return result
    length = getattr(entry, "Length", 0) or 0
    labels = getattr(entry, "Labels", None)
    values = getattr(entry, "Values", None)
    if labels is None or values is None:
        return result
    for i in range(length):
        label = stringify(labels[i]).rstrip(":").strip()
        value = stringify(values[i])
        if label and value:
            result[label] = value
    return result


def extract_instrument_methods(raw) -> tuple[str, str]:
    names: list[str] = []
    method_texts: list[str] = []

    name_list = _safe_call(raw.GetAllInstrumentNamesFromInstrumentMethod)
    if name_list is not None:
        text = stringify(name_list)
        if text:
            names.extend([part.strip() for part in text.split(";") if part.strip()])

    count = _safe_call(lambda: int(raw.InstrumentMethodsCount), default=0) or 0
    for i in range(count):
        text = stringify(_safe_call(raw.GetInstrumentMethod, i))
        if text:
            method_texts.append(f"--- method {i} ---\n{text}")

    return join_unique(names), "\n\n".join(method_texts)


def extract_raw_metadata(path: Path, include_method_text: bool = True) -> OrderedDict[str, str]:
    record: OrderedDict[str, str] = OrderedDict()
    record["file_name"] = path.name
    record["file_path"] = str(path)
    record["file_size_mb"] = format_size_mb(path)
    record["error"] = ""

    RawFileReaderAdapter, Device = load_api()
    raw = RawFileReaderAdapter.FileFactory(str(path))
    try:
        if not raw.IsOpen:
            record["error"] = "Could not open RAW file (locked, corrupt, or not a Thermo RAW)."
            return record
        if getattr(raw, "IsError", False):
            message = ""
            try:
                message = stringify(raw.FileError.ErrorMessage)
            except Exception:
                pass
            record["error"] = message or "RawFileReader reported an error."
            return record
        if getattr(raw, "InAcquisition", False):
            record["error"] = "File is still being acquired."
            return record

        header = net_properties(raw.FileHeader)
        record["raw_file_version"] = header.get("Revision", "")
        record["creation_date"] = header.get("CreationDate", "")
        record["operator"] = header.get("WhoCreatedId", "")
        record["description"] = header.get("FileDescription", "")
        if header.get("WhoCreatedComputer"):
            record["created_on_computer"] = header["WhoCreatedComputer"]
        for key, value in header.items():
            dest = f"file_header.{key}"
            if dest not in record and key not in {
                "Revision",
                "CreationDate",
                "WhoCreatedId",
                "FileDescription",
                "WhoCreatedComputer",
            }:
                record[dest] = value

        sample = net_properties(raw.SampleInformation)
        alias = {
            "SampleName": "sample_name",
            "SampleId": "sample_id",
            "SampleType": "sample_type",
            "Comment": "comment",
            "Vial": "vial",
            "InjectionVolume": "injection_volume",
            "SampleVolume": "sample_volume",
            "DilutionFactor": "dilution_factor",
            "RowNumber": "row_number",
            "Barcode": "barcode",
            "SampleBarcode": "barcode",
            "InstrumentMethod": "instrument_method",
            "ProcessingMethod": "processing_method",
            "CalibrationFile": "calibration_file",
            "UserText": "user_text",
            "UserLabel": "user_label",
            "IstdAmount": "istd_amount",
            "CalibrationLevel": "calibration_level",
        }
        used = set()
        for src, dest in alias.items():
            if src in sample and sample[src]:
                if not record.get(dest):
                    record[dest] = sample[src]
                used.add(src)
        for key, value in sample.items():
            if key not in used:
                record[f"sample.{key}"] = value

        ms_count = _safe_call(raw.GetInstrumentCountOfType, Device.MS, default=0) or 0
        record["instrument_count"] = stringify(getattr(raw, "InstrumentCount", ""))
        record["ms_instrument_count"] = stringify(ms_count)
        if ms_count:
            raw.SelectInstrument(Device.MS, 1)

        inst = net_properties(_safe_call(raw.GetInstrumentData))
        record["instrument_model"] = inst.get("Model", "")
        record["instrument_name"] = inst.get("Name", "")
        record["serial_number"] = inst.get("SerialNumber", "")
        record["software_version"] = inst.get("SoftwareVersion", "")
        record["firmware_version"] = inst.get("HardwareVersion", "")
        for key, value in inst.items():
            if key not in {"Model", "Name", "SerialNumber", "SoftwareVersion", "HardwareVersion"}:
                record[f"instrument.{key}"] = value

        run = net_properties(getattr(raw, "RunHeaderEx", None) or getattr(raw, "RunHeader", None))
        record["scan_count"] = run.get("SpectraCount", "")
        record["first_scan"] = run.get("FirstSpectrum", "")
        record["last_scan"] = run.get("LastSpectrum", "")
        record["rt_start_min"] = run.get("StartTime", "")
        record["rt_end_min"] = run.get("EndTime", "")
        record["mz_low"] = run.get("LowMass", "")
        record["mz_high"] = run.get("HighMass", "")
        record["mass_resolution"] = run.get("MassResolution", "")
        try:
            start = float(record["rt_start_min"] or 0)
            end = float(record["rt_end_min"] or 0)
            record["duration_min"] = stringify(end - start)
        except ValueError:
            record["duration_min"] = ""
        for key, value in run.items():
            if key not in {
                "SpectraCount",
                "FirstSpectrum",
                "LastSpectrum",
                "StartTime",
                "EndTime",
                "LowMass",
                "HighMass",
                "MassResolution",
            }:
                record[f"run.{key}"] = value

        method_names, method_text = extract_instrument_methods(raw)
        record["instrument_method_names"] = method_names
        record["instrument_method_text"] = method_text if include_method_text else ""
        if method_names and not record.get("instrument_method"):
            record["instrument_method"] = method_names

        first = int(run.get("FirstSpectrum") or 0)
        last = int(run.get("LastSpectrum") or 0)
        sampled = inspect_scans(raw, first, last)
        filters = sampled["filters"]
        activations = sampled["activations"]
        energies = sampled["energies"]
        ranges = sampled["ranges"]
        faims = sampled["faims"]
        trailers_by_order = sampled["trailers_by_order"]
        trailer_values = sampled["trailer_values"]

        record["polarity"] = join_unique(sampled["polarities"])
        record["ionization"] = join_unique(sampled["ionizations"])
        record["mass_analyzer"] = join_unique(sampled["analyzers"])
        record["activation_types"] = join_unique(sorted(activations))
        record["collision_energies"] = join_unique(
            sorted(
                list(energies) + sampled["reaction_energies"],
                key=lambda x: float(x) if re.fullmatch(r"\d+(?:\.\d+)?", x) else 0,
            )
        )
        record["scan_mz_ranges"] = join_unique(sorted(ranges))
        record["unique_filter_count"] = str(len(filters))
        record["scan_filters"] = " | ".join(filters)
        record["isolation_widths"] = join_unique(sampled["isolation_widths"])
        record["precursor_masses"] = join_unique(sampled["precursor_masses"])

        interesting_trailer = (
            "HCD Energy",
            "Collision Energy",
            "Orbitrap Resolution",
            "FT Resolution",
            "Ion Injection Time (ms)",
            "Ion Injection Time",
            "FAIMS CV",
            "Compensation Voltage",
            "Master Scan Number",
            "Monoisotopic M/Z",
            "Charge State",
            "Analyzer Temperature",
            "Ion Source",
        )
        for label, values in trailer_values.items():
            joined = join_unique(sorted(values))
            key = f"trailer.{label}"
            if label in interesting_trailer or len(values) <= 8:
                record[key] = joined
            if "faims" in label.lower() or "compensation" in label.lower():
                faims.update(values)

        record["faims_cv"] = join_unique(sorted(faims))

        status = extract_status_log(raw, first or 1)
        for label, value in status.items():
            record[f"status.{label}"] = value

        fill_summary_fields(
            record,
            filters,
            method_text,
            trailers_by_order,
            saw_ms2=sampled["saw_ms2"],
        )

    except Exception as exc:
        record["error"] = f"{exc}"
    finally:
        try:
            raw.Dispose()
        except Exception:
            pass
    return record


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


INTERNAL_KEYS = {"_report_path"}


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return directory/stem.html, or directory/stem_1.html if that name is taken."""
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def _md_cell(text: str, limit: int = 220) -> str:
    display = text if len(text) <= limit else text[: limit - 3] + "..."
    return _md_escape(display)


def _md_section(
    title: str, pairs: list[tuple[str, str]], keep_empty: bool = False
) -> list[str]:
    rows = [(key, value) for key, value in pairs if value or keep_empty]
    if not rows:
        return []
    lines = [f"## {title}", "", "| Field | Value |", "| --- | --- |"]
    for key, value in rows:
        lines.append(f"| {_md_escape(key)} | {_md_cell(value, 400)} |")
    lines.append("")
    return lines


def _summary_rows(record: dict) -> list[tuple[str, str]]:
    return [(SUMMARY_LABELS[key], record.get(key, "")) for key in SUMMARY_KEYS]


def _report_groups(record: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """Field/value sections of an individual report, after the summary."""
    used = {
        "file_name",
        "file_path",
        "file_size_mb",
        "error",
        "scan_filters",
        "instrument_method_text",
        *SUMMARY_KEYS,
        *INTERNAL_KEYS,
    }
    for _, keys in FIELD_GROUPS:
        used.update(keys)

    groups = [
        (title, [(key, record.get(key, "")) for key in keys])
        for title, keys in FIELD_GROUPS
    ]
    extras = [(key, value) for key, value in record.items() if key not in used and value]
    trailer = [(key, value) for key, value in extras if key.startswith("trailer.")]
    status = [(key, value) for key, value in extras if key.startswith("status.")]
    other = [
        (key, value)
        for key, value in extras
        if not key.startswith(("trailer.", "status."))
    ]
    groups += [
        ("Other fields", other),
        ("Scan trailer extra (sampled)", trailer),
        ("Status log (first scan)", status),
    ]
    return groups


def write_individual_markdown(record: OrderedDict[str, str] | dict, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {record.get('file_name', 'RAW file')}",
        "",
        f"Generated: {generated}  ",
        f"`{record.get('file_path', '')}`  ",
    ]
    if record.get("file_size_mb"):
        lines.append(f"Size: {record['file_size_mb']} MB")
    lines.append("")
    if record.get("error"):
        lines += [f"**Error:** {record['error']}", ""]
        dest.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return

    lines.extend(_md_section("Summary", _summary_rows(record), keep_empty=True))
    for title, pairs in _report_groups(record):
        lines.extend(_md_section(title, pairs))

    if record.get("scan_filters"):
        lines += ["## Scan filters", ""]
        for filt in record["scan_filters"].split(" | "):
            lines.append(f"- `{filt}`")
        lines.append("")
    if record.get("instrument_method_text"):
        lines += ["## Instrument method", "", "```", record["instrument_method_text"], "```", ""]

    dest.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _column_labels(records: list[dict]) -> list[str]:
    stems = [Path(rec.get("file_name", "file")).stem for rec in records]
    names = [(rec.get("sample_name") or stem) for rec, stem in zip(records, stems)]
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return [stem if counts[name] > 1 else name for name, stem in zip(names, stems)]


def _ordered_keys(records: list[dict]) -> list[str]:
    skip = INTERNAL_KEYS | {"file_name", "file_path"} | set(SUMMARY_KEYS)
    keys: list[str] = []
    seen: set[str] = set()
    for preferred in PREFERRED_COLUMNS:
        if preferred in skip:
            continue
        if any(rec.get(preferred) for rec in records):
            keys.append(preferred)
            seen.add(preferred)
    for rec in records:
        for key, value in rec.items():
            if key in seen or key in skip or not value:
                continue
            keys.append(key)
            seen.add(key)
    return keys


def _comparison_rows(records: list[dict], keys: list[str]) -> list[tuple[str, list[str]]]:
    rows = []
    for key in keys:
        values = [rec.get(key, "") for rec in records]
        if any(values):
            rows.append((key, values))
    return rows


OVERVIEW_KEYS = [
    "error",
    "sample_name",
    "sample_id",
    "method_file",
    "acquisition_mode",
    "top_mode",
    "cycle_time",
    "fragmentation",
    "ms1_mz_range",
    "ms2_mz_range",
    "ms1_resolution",
    "ms2_resolution",
    "instrument_model",
    "scan_count",
    "rt_start_min",
    "rt_end_min",
    "polarity",
    "ionization",
]


def _combined_groups(records: list[dict]) -> list[tuple[str, list[str]]]:
    """Comparison sections of the combined report (long text fields excluded)."""
    groups = [("Overview", OVERVIEW_KEYS)] + list(FIELD_GROUPS)
    grouped_keys = {key for _, keys in FIELD_GROUPS for key in keys}
    remaining = [
        key
        for key in _ordered_keys(records)
        if key not in grouped_keys and key not in LONG_FIELDS
    ]
    trailer = [key for key in remaining if key.startswith("trailer.")]
    status = [key for key in remaining if key.startswith("status.")]
    other = [
        key
        for key in remaining
        if not key.startswith(("trailer.", "status."))
    ]
    groups += [
        ("Other fields", other),
        ("Scan trailer extra (sampled)", trailer),
        ("Status log (first scan)", status),
    ]
    return groups


def _comparison_table(records: list[dict], labels: list[str], keys: list[str]) -> list[str]:
    rows = _comparison_rows(records, keys)
    if not rows:
        return []
    header = "| Field | " + " | ".join(_md_escape(label) for label in labels) + " |"
    divider = "| --- | " + " | ".join("---" for _ in labels) + " |"
    lines = [header, divider]
    for key, values in rows:
        limit = 80 if key in LONG_FIELDS else 180
        cells = " | ".join(_md_cell(value, limit) for value in values)
        lines.append(f"| {_md_escape(key)} | {cells} |")
    lines.append("")
    return lines


def write_combined_markdown(records: list[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    labels = _column_labels(records)
    lines = [
        "# Thermo RAW metadata — combined",
        "",
        f"Generated: {generated}  ",
        f"Files: {len(records)}",
        "",
        "## Summary",
        "",
        "| "
        + " | ".join(SUMMARY_LABELS[key] for key in SUMMARY_KEYS)
        + " | Report |",
        "| "
        + " | ".join("---" for _ in SUMMARY_KEYS)
        + " | --- |",
    ]
    for rec in records:
        report = rec.get("_report_path", "")
        report_cell = f"[open]({Path(report).name})" if report else ""
        cells = [_md_cell(rec.get(key, ""), 80) for key in SUMMARY_KEYS]
        lines.append("| " + " | ".join(cells) + f" | {report_cell} |")
    lines += [
        "",
        "## Individual reports",
        "",
        "| File | Sample | Report |",
        "| --- | --- | --- |",
    ]
    for rec, label in zip(records, labels):
        report = rec.get("_report_path", "")
        report_cell = f"[open]({Path(report).name})" if report else ""
        lines.append(
            f"| {_md_escape(rec.get('file_name', ''))} | {_md_escape(label)} | {report_cell} |"
        )
    lines.append("")
    for title, keys in _combined_groups(records):
        section = _comparison_table(records, labels, keys)
        if section or title == "Overview":
            lines += [f"## {title}", ""] + section

    filter_keys = [key for key in LONG_FIELDS if any(rec.get(key) for rec in records)]
    if filter_keys:
        lines += [
            "## Long text fields",
            "",
            "Full instrument method and scan-filter lists are in the individual reports. Summary:",
            "",
        ]
        lines += _comparison_table(records, labels, filter_keys)

    dest.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML output (self-contained pages that open in any browser)
# ---------------------------------------------------------------------------

HTML_CSS = """
:root {
  --bg: #ffffff; --fg: #1b2228; --muted: #5a6670; --rule: #dde2e6;
  --head: #f1f4f5; --accent: #0b6e79; --error: #b42318;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12171b; --fg: #e3e8eb; --muted: #97a3ac; --rule: #2a343b;
    --head: #1b2328; --accent: #5cc3cd; --error: #f97066;
  }
}
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
main { max-width: 72rem; margin: 0 auto; padding: 2rem 16px 3rem; }
.eyebrow { margin: 0; font-size: 0.75rem; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--accent); }
h1 { margin: 0.2rem 0 0.3rem; font-size: 1.7rem; overflow-wrap: anywhere; }
.meta { margin: 0; color: var(--muted); font-size: 0.9rem; overflow-wrap: anywhere; }
section { margin-top: 1.8rem; }
details { margin-top: 0.9rem; }
section + details { margin-top: 1.8rem; }
h2, summary { font-size: 1.1rem; font-weight: 600; margin: 0 0 0.6rem; }
summary { cursor: pointer; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; vertical-align: top; padding: 0.35rem 0.75rem;
  border-bottom: 1px solid var(--rule); white-space: pre-wrap; }
thead th { background: var(--head); font-weight: 600; white-space: nowrap; }
.kv { width: 100%; max-width: 60rem; table-layout: fixed; }
.kv th { width: 15rem; font-weight: 500; color: var(--muted); }
.kv td { overflow-wrap: anywhere; }
@media (max-width: 600px) { .kv th { width: 40%; } }
.summary td { white-space: nowrap; }
.compare th[scope=row] { position: sticky; left: 0; background: var(--bg);
  font-weight: 500; color: var(--muted); white-space: nowrap; }
.empty { color: var(--muted); }
.error { color: var(--error); }
.hint { margin: 0.6rem 0 0; color: var(--muted); font-size: 0.85rem; }
a { color: var(--accent); }
code, pre { font-family: Consolas, "Cascadia Mono", monospace; font-size: 13px; }
pre { background: var(--head); padding: 0.8rem 1rem; border-radius: 4px;
  overflow-x: auto; line-height: 1.45; }
ul.filters { padding-left: 1.2rem; margin: 0; }
""".strip()

LABEL_WORDS = {
    "mz": "m/z", "rt": "RT", "ms1": "MS1", "ms2": "MS2", "ms3": "MS3",
    "id": "ID", "faims": "FAIMS", "cv": "CV", "min": "(min)", "mb": "(MB)",
}


def _h(text: object) -> str:
    return html.escape(str(text))


def _label(key: str) -> str:
    """Readable field name: 'rt_start_min' -> 'RT start (min)'."""
    if key in SUMMARY_LABELS:
        return SUMMARY_LABELS[key]
    if key.startswith(("trailer.", "status.")):
        return key.split(".", 1)[1]
    text = " ".join(LABEL_WORDS.get(word, word) for word in key.split("_"))
    return text if text.startswith("m/z") else text[:1].upper() + text[1:]


def _html_value(value: str) -> str:
    return _h(value) if value else '<span class="empty">—</span>'


def _html_kv_table(pairs: list[tuple[str, str]], keep_empty: bool = False) -> str:
    rows = [(key, value) for key, value in pairs if value or keep_empty]
    if not rows:
        return ""
    body = "".join(
        f'<tr><th scope="row">{_h(_label(key))}</th><td>{_html_value(value)}</td></tr>'
        for key, value in rows
    )
    return f'<table class="kv"><tbody>{body}</tbody></table>'


def _html_section(title: str, content: str, collapsed: bool = False) -> str:
    if not content:
        return ""
    if collapsed:
        return f"<details><summary>{_h(title)}</summary>{content}</details>"
    return f"<section><h2>{_h(title)}</h2>{content}</section>"


def _html_page(title: str, eyebrow: str, meta: list[str], body: str) -> str:
    meta_html = " · ".join(_h(item) for item in meta if item)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_h(title)}</title>\n<style>\n{HTML_CSS}\n</style>\n</head>\n<body>\n<main>\n"
        f'<header><p class="eyebrow">{_h(eyebrow)}</p><h1>{_h(title)}</h1>'
        f'<p class="meta">{meta_html}</p></header>\n'
        f"{body}\n</main>\n</body>\n</html>\n"
    )


def write_individual_html(record: OrderedDict[str, str] | dict, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = [
        record.get("file_path", ""),
        f"{record['file_size_mb']} MB" if record.get("file_size_mb") else "",
        f"Generated {generated}",
    ]
    parts: list[str] = []
    if record.get("error"):
        parts.append(f'<p class="error"><b>Could not read this file:</b> {_h(record["error"])}</p>')
    else:
        parts.append(_html_section("Summary", _html_kv_table(_summary_rows(record), keep_empty=True)))
        main_titles = {title for title, _ in FIELD_GROUPS}
        for title, pairs in _report_groups(record):
            parts.append(_html_section(title, _html_kv_table(pairs), title not in main_titles))
        if record.get("scan_filters"):
            items = "".join(
                f"<li><code>{_h(filt)}</code></li>" for filt in record["scan_filters"].split(" | ")
            )
            parts.append(_html_section("Scan filters", f'<ul class="filters">{items}</ul>', True))
        if record.get("instrument_method_text"):
            parts.append(_html_section(
                "Instrument method", f"<pre>{_h(record['instrument_method_text'])}</pre>", True
            ))
    page = _html_page(record.get("file_name", "RAW file"), "Thermo RAW report", meta, "\n".join(parts))
    dest.write_text(page, encoding="utf-8")


def write_combined_html(records: list[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    labels = _column_labels(records)

    head = "".join(f'<th scope="col">{_h(SUMMARY_LABELS[key])}</th>' for key in SUMMARY_KEYS)
    rows = []
    for rec in records:
        report = rec.get("_report_path", "")
        name = _h(rec.get("file_name", ""))
        file_cell = f'<a href="{_h(Path(report).name)}">{name}</a>' if report else name
        if rec.get("error"):
            cells = f'<td colspan="{len(SUMMARY_KEYS) - 1}" class="error">{_h(rec["error"])}</td>'
        else:
            cells = "".join(f"<td>{_h(rec.get(key, ''))}</td>" for key in SUMMARY_KEYS[1:])
        rows.append(f"<tr><td>{file_cell}</td>{cells}</tr>")
    summary = (
        f'<div class="scroll"><table class="summary"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        '<p class="hint">Click a file name to open its full report. '
        "To copy this table into Excel, select it with the mouse and paste.</p>"
    )
    parts = [_html_section("Summary", summary)]

    col_head = '<th scope="col">Field</th>' + "".join(
        f'<th scope="col">{_h(label)}</th>' for label in labels
    )
    for title, keys in _combined_groups(records):
        if title == "Overview":  # already covered by the summary table
            continue
        comparison = _comparison_rows(records, keys)
        if not comparison:
            continue
        body = "".join(
            f'<tr><th scope="row">{_h(_label(key))}</th>'
            + "".join(f"<td>{_h(value)}</td>" for value in values)
            + "</tr>"
            for key, values in comparison
        )
        table = (
            f'<div class="scroll"><table class="compare"><thead><tr>{col_head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )
        parts.append(_html_section(title, table, collapsed=True))

    meta = [f"{len(records)} files", str(dest.parent), f"Generated {generated}"]
    page = _html_page("Thermo RAW metadata", "Combined report", meta, "\n".join(parts))
    dest.write_text(page, encoding="utf-8")


WRITERS = {
    "html": (".html", write_individual_html, write_combined_html),
    "md": (".md", write_individual_markdown, write_combined_markdown),
}


# ---------------------------------------------------------------------------
# Parallel analysis / CLI
# ---------------------------------------------------------------------------


def analyze_one(path_str: str, fmt: str = "html") -> dict:
    """Worker: extract one RAW file and write its individual report."""
    path = Path(path_str)
    try:
        record = extract_raw_metadata(path, include_method_text=True)
    except Exception as exc:
        record = OrderedDict(
            (
                ("file_name", path.name),
                ("file_path", str(path)),
                ("file_size_mb", format_size_mb(path)),
                ("error", str(exc)),
            )
        )
    suffix, write_individual, _ = WRITERS[fmt]
    dest = unique_path(path.parent, path.stem, suffix)
    write_individual(record, dest)
    payload = dict(record)
    payload["_report_path"] = str(dest)
    return payload


def default_workers(count: int) -> int:
    cpu = os.cpu_count() or 4
    return max(1, min(count, cpu, 8))


def process(paths: list[Path], workers: int | None = None, fmt: str = "html") -> list[Path]:
    if not paths:
        raise SystemExit("No .raw files found.")

    worker_count = workers or default_workers(len(paths))
    print(f"Reading {len(paths)} RAW file(s) with {worker_count} worker(s)...")

    records: list[dict] = [{} for _ in paths]
    if len(paths) == 1 or worker_count == 1:
        for index, path in enumerate(paths):
            print(f"  [{index + 1}/{len(paths)}] {path.name}")
            records[index] = analyze_one(str(path), fmt)
            report = records[index].get("_report_path", "")
            if report:
                print(f"    -> {report}")
            if records[index].get("error"):
                print(f"    ! {records[index]['error']}")
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(analyze_one, str(path), fmt): index for index, path in enumerate(paths)}
            done = 0
            for future in as_completed(futures):
                index = futures[future]
                records[index] = future.result()
                done += 1
                print(f"  [{done}/{len(paths)}] {paths[index].name}")
                report = records[index].get("_report_path", "")
                if report:
                    print(f"    -> {report}")
                if records[index].get("error"):
                    print(f"    ! {records[index]['error']}")

    written = [Path(rec["_report_path"]) for rec in records if rec.get("_report_path")]
    if len(records) >= 2:
        wrap_dir = common_parent(paths)
        wrap_dir.mkdir(parents=True, exist_ok=True)
        suffix, _, write_combined = WRITERS[fmt]
        wrap_path = unique_path(wrap_dir, "raw_metadata", suffix)
        write_combined(records, wrap_path)
        written.append(wrap_path)
        print(f"Wrote combined report {wrap_path}")

    errors = sum(1 for rec in records if rec.get("error"))
    if errors:
        print(f"Finished with {errors} file(s) that could not be read.")
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract metadata from Thermo Scientific .raw mass-spec files."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=".raw file(s) and/or folder(s) to scan",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not scan subfolders",
    )
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        help="Parallel workers for individual files (default: min(files, CPUs, 8))",
    )
    parser.add_argument(
        "--format",
        choices=sorted(WRITERS),
        default="html",
        help="Report format: html (opens in any browser, default) or md (Markdown)",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Download Thermo RawFileReader DLLs and verify the reader loads",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.setup:
        download_dlls()
        load_api()
        print("RawFileReader is ready.")
        return 0

    if not args.paths:
        parse_args(["-h"])
        return 1

    paths = collect_raw_paths(args.paths, recursive=not args.no_recursive)
    if not paths:
        print("No .raw files found in the selection.")
        return 1

    try:
        process(paths, workers=args.workers, fmt=args.format)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
