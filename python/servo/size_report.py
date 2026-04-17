from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import tarfile
import tomllib
from typing import Iterable, Sequence

DEFAULT_SIZE_REPORT_PROFILE = "production-stripped"
NOT_AVAILABLE = "n/a"
# Cargo artifact filenames conventionally include a short hexadecimal hash. Keep this
# in one place so the linker-map parsing assumptions are easy to update if Cargo changes.
CARGO_HASH_PATTERN = r"[0-9a-f]{7,16}"


@dataclass(frozen=True)
class SizeVariant:
    name: str
    description: str
    removed_features: tuple[str, ...] = ()
    media_stack: str | None = None
    supported: bool = True
    unavailable_reason: str | None = None


@dataclass
class VariantMeasurement:
    name: str
    description: str
    feature_list: list[str]
    removed_features: list[str]
    media_stack: str | None = None
    binary_path: str | None = None
    binary_size: int | None = None
    shared_library_footprint: int | None = None
    shared_libraries: list[tuple[str, int]] = field(default_factory=list)
    package_path: str | None = None
    package_archive_size: int | None = None
    installed_size: int | None = None
    packaged_resource_size: int | None = None
    packaged_library_size: int | None = None
    packaged_binary_size: int | None = None
    section_sizes: dict[str, int] = field(default_factory=dict)
    top_symbols: list[tuple[str, int]] = field(default_factory=list)
    top_crates: list[tuple[str, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def load_default_features(manifest_path: str) -> list[str]:
    with open(manifest_path, "rb") as manifest_file:
        manifest = tomllib.load(manifest_file)
    return list(manifest["features"]["default"])


def primary_variants() -> list[SizeVariant]:
    return [
        SizeVariant("baseline", "Current default servoshell feature set"),
        SizeVariant("no-webgpu", "Baseline minus webgpu", ("webgpu",)),
        SizeVariant("no-webxr", "Baseline minus webxr", ("webxr",)),
        SizeVariant("no-js-jit", "Baseline minus js_jit", ("js_jit",)),
        SizeVariant("no-gamepad", "Baseline minus gamepad", ("gamepad",)),
        SizeVariant("no-baked-in-resources", "Baseline minus baked-in-resources", ("baked-in-resources",)),
        SizeVariant("no-clipboard", "Baseline minus clipboard", ("clipboard",)),
        SizeVariant("no-bluetooth", "Baseline minus bluetooth", ("bluetooth",)),
        SizeVariant("no-webdriver", "Baseline minus webdriver", ("webdriver",)),
        SizeVariant("no-webgpu-webxr", "Baseline minus webgpu and webxr", ("webgpu", "webxr")),
        SizeVariant(
            "minimal-shell",
            "Shell-oriented build with all optional shell features removed",
            ("baked-in-resources", "bluetooth", "clipboard", "gamepad", "js_jit", "webdriver", "webgpu", "webxr"),
        ),
    ]


def second_order_variants() -> list[SizeVariant]:
    return [
        SizeVariant("media-dummy", "Use the dummy media stack", media_stack="dummy"),
        SizeVariant(
            "no-devtools",
            "Disable devtools if linked into servoshell",
            supported=False,
            unavailable_reason="servoshell does not currently expose a dedicated devtools Cargo feature",
        ),
        SizeVariant(
            "reduced-image-codecs",
            "Reduce image codec support",
            supported=False,
            unavailable_reason="servoshell does not currently expose image codec feature gates",
        ),
        SizeVariant(
            "reduced-storage",
            "Reduce storage / SQLite usage",
            supported=False,
            unavailable_reason="servoshell does not currently expose storage feature gates",
        ),
    ]


def selected_variants(selected_names: Sequence[str] | None, include_second_order: bool) -> tuple[list[SizeVariant], list[SizeVariant]]:
    supported = primary_variants()
    skipped = []
    if include_second_order:
        for variant in second_order_variants():
            if variant.supported:
                supported.append(variant)
            else:
                skipped.append(variant)

    if not selected_names:
        return supported, skipped

    selected = []
    selected_set = set(selected_names)
    known = {variant.name: variant for variant in supported + skipped}
    missing = sorted(selected_set - set(known))
    if missing:
        raise ValueError(f"Unknown size-report variants: {', '.join(missing)}")
    for name in selected_names:
        variant = known[name]
        if variant.supported:
            selected.append(variant)
        else:
            skipped.append(variant)
    return selected, skipped


def features_for_variant(default_features: Sequence[str], variant: SizeVariant) -> list[str]:
    removed = set(variant.removed_features)
    return [feature for feature in default_features if feature not in removed]


def parse_section_sizes(output: str) -> dict[str, int]:
    section_sizes: dict[str, int] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("section", "address", "Total")):
            continue
        match = re.match(r"^(?P<name>\S+)\s+(?P<size>0x[0-9a-fA-F]+|\d+)\s+(0x[0-9a-fA-F]+|\d+)$", stripped)
        if not match:
            continue
        section_sizes[match.group("name")] = int(match.group("size"), 0)
    return section_sizes


def parse_nm_symbols(output: str, limit: int) -> list[tuple[str, int]]:
    symbols: list[tuple[str, int]] = []
    for line in output.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        try:
            size = int(parts[1], 0)
        except ValueError:
            continue
        symbols.append((parts[3], size))
    symbols.sort(key=lambda item: item[1], reverse=True)
    return symbols[:limit]


def parse_dynamic_library_paths(output: str) -> list[str]:
    library_paths: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        for candidate in re.findall(r"(/[^\s()]+)", line):
            if candidate not in seen:
                seen.add(candidate)
                library_paths.append(candidate)
    return library_paths


def crate_name_from_path(object_path: str) -> str:
    archive_match = re.search(rf"/lib(?P<crate>[^/]+?)(?:-{CARGO_HASH_PATTERN})?\.rlib(?:\(|$)", object_path)
    if archive_match:
        return archive_match.group("crate")

    base_name = Path(object_path).name
    for suffix in (".rcgu.o", ".o", ".obj", ".rlib"):
        if base_name.endswith(suffix):
            base_name = base_name[: -len(suffix)]
            break

    hash_match = re.match(rf"(?P<crate>.+?)-{CARGO_HASH_PATTERN}(?:[.-].*)?$", base_name)
    if hash_match:
        return hash_match.group("crate")

    if base_name.startswith("lib") and len(base_name) > 3:
        base_name = base_name[3:]
    return base_name


def parse_linker_map_crates(output: str, limit: int) -> list[tuple[str, int]]:
    crate_sizes: dict[str, int] = defaultdict(int)
    line_pattern = re.compile(
        r"^\s*(?:0x[0-9a-fA-F]+|\d+)\s+(?P<size>0x[0-9a-fA-F]+|\d+)\s+(?P<path>\S+\.(?:o|obj|rlib)(?:\([^)]+\))?)\s*$"
    )

    for line in output.splitlines():
        match = line_pattern.match(line)
        if not match:
            continue
        crate_name = crate_name_from_path(match.group("path"))
        crate_sizes[crate_name] += int(match.group("size"), 0)

    return sorted(crate_sizes.items(), key=lambda item: item[1], reverse=True)[:limit]


def summarize_tarball(tarball_path: str) -> tuple[int, int, int, int]:
    installed_size = 0
    resource_size = 0
    library_size = 0
    binary_size = 0
    with tarfile.open(tarball_path, "r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            installed_size += member.size
            member_name = member.name.lower()
            if "/resources/" in member_name:
                resource_size += member.size
            if member_name.endswith((".so", ".dylib", ".dll")):
                library_size += member.size
            if Path(member.name).name.startswith("servoshell"):
                binary_size += member.size
    return installed_size, resource_size, library_size, binary_size


def format_bytes(size: int | None) -> str:
    if size is None:
        return NOT_AVAILABLE
    suffixes = ["B", "KiB", "MiB", "GiB"]
    value = float(size)
    for suffix in suffixes:
        if value < 1024.0 or suffix == suffixes[-1]:
            return f"{value:.1f} {suffix}"
        value /= 1024.0
    return f"{size} B"


def format_delta(value: int | None, baseline: int | None) -> str:
    if value is None or baseline is None:
        return "n/a"
    delta = value - baseline
    if delta == 0:
        return "0 B"
    sign = "+" if delta > 0 else "-"
    return f"{sign}{format_bytes(abs(delta))}"


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def build_markdown_report(
    profile: str,
    target_triple: str,
    measurements: Sequence[VariantMeasurement],
    skipped_variants: Sequence[SizeVariant],
) -> str:
    baseline = next((measurement for measurement in measurements if measurement.name == "baseline"), None)
    if baseline is None:
        raise ValueError("A baseline measurement is required")

    baseline_rows = [
        ("Binary", format_bytes(baseline.binary_size)),
        ("Shared-library footprint", format_bytes(baseline.shared_library_footprint)),
        ("Installed package size", format_bytes(baseline.installed_size)),
        ("Packaged resources", format_bytes(baseline.packaged_resource_size)),
        ("Bundled libraries", format_bytes(baseline.packaged_library_size)),
        ("Packaged binary", format_bytes(baseline.packaged_binary_size)),
    ]

    feature_rows = []
    for measurement in measurements:
        feature_rows.append(
            [
                measurement.name,
                ", ".join(measurement.removed_features) or "—",
                format_bytes(measurement.binary_size),
                format_delta(measurement.binary_size, baseline.binary_size),
                format_bytes(measurement.shared_library_footprint),
                format_delta(measurement.shared_library_footprint, baseline.shared_library_footprint),
                format_bytes(measurement.installed_size),
                format_delta(measurement.installed_size, baseline.installed_size),
            ]
        )

    package_rows = []
    for measurement in measurements:
        package_rows.append(
            [
                measurement.name,
                format_bytes(measurement.package_archive_size),
                format_bytes(measurement.installed_size),
                format_bytes(measurement.packaged_resource_size),
                format_bytes(measurement.packaged_library_size),
            ]
        )

    cheapest = sorted(
        (
            measurement
            for measurement in measurements
            if baseline.binary_size is not None
            and measurement.removed_features and measurement.binary_size is not None
            and (baseline.binary_size or 0) > (measurement.binary_size or 0)
        ),
        key=lambda measurement: (
            ((measurement.binary_size or 0) - (baseline.binary_size or 0)) / max(1, len(measurement.removed_features)),
            measurement.name,
        ),
    )
    cheapest_rows = []
    for measurement in cheapest:
        savings = (baseline.binary_size or 0) - (measurement.binary_size or 0)
        cheapest_rows.append(
            [
                measurement.name,
                ", ".join(measurement.removed_features),
                format_bytes(savings),
                format_bytes(int(savings / len(measurement.removed_features))),
            ]
        )

    lines = [
        f"# Servo size report ({profile})",
        "",
        f"- Target: `{target_triple}`",
        f"- Profile: `{profile}`",
        "",
        "## Baseline totals",
        "",
        markdown_table(["Metric", "Value"], baseline_rows),
    ]

    if baseline.section_sizes:
        section_rows = [[section, format_bytes(size)] for section, size in sorted(baseline.section_sizes.items())]
        lines.extend(["", "## Baseline section breakdown", "", markdown_table(["Section", "Size"], section_rows)])

    if baseline.top_crates:
        crate_rows = [[crate, format_bytes(size)] for crate, size in baseline.top_crates]
        lines.extend(["", "## Top crate contributors", "", markdown_table(["Crate", "Attributed size"], crate_rows)])

    if baseline.top_symbols:
        symbol_rows = [[symbol, format_bytes(size)] for symbol, size in baseline.top_symbols]
        lines.extend(["", "## Top symbols", "", markdown_table(["Symbol", "Size"], symbol_rows)])

    lines.extend(["", "## Feature ablation table", "", markdown_table(
        ["Variant", "Removed features", "Binary", "Binary delta", "Shared libs", "Shared-lib delta", "Installed package", "Package delta"],
        feature_rows,
    )])

    lines.extend(["", "## Packaging/resource table", "", markdown_table(
        ["Variant", "Archive", "Installed package", "Resources", "Bundled libraries"],
        package_rows,
    )])

    if cheapest_rows:
        lines.extend(["", "## Cheapest removals by size saved per feature", "", markdown_table(
            ["Variant", "Removed features", "Binary saved", "Saved per feature"],
            cheapest_rows,
        )])

    skipped_lines = []
    for variant in skipped_variants:
        if variant.unavailable_reason:
            skipped_lines.append(f"- `{variant.name}`: {variant.unavailable_reason}")
    if skipped_lines:
        lines.extend(["", "## Deferred follow-ups", "", *skipped_lines])

    notes = [note for measurement in measurements for note in measurement.notes]
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)

    return "\n".join(lines) + "\n"
