# Copyright 2013 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import json
import os
import os.path as path
import subprocess
from subprocess import CompletedProcess
from shutil import copy2
from typing import Any, Optional, List
import zipfile

import mozdebug

from mach.decorators import (
    CommandArgument,
    CommandProvider,
    Command,
)
from mach.registrar import Registrar

import servo.util
import servo.platform
from servo.size_report import (
    DEFAULT_SIZE_REPORT_PROFILE,
    VariantMeasurement,
    build_markdown_report,
    features_for_variant,
    load_default_features,
    parse_dynamic_library_paths,
    parse_linker_map_crates,
    parse_nm_symbols,
    parse_section_sizes,
    selected_variants,
    summarize_tarball,
)

from servo.command_base import (
    CommandBase,
    check_call,
    is_linux,
    is_freebsd,
)
from servo.platform.build_target import is_android

from python.servo.command_base import BuildType

ANDROID_APP_NAME = "org.servo.servoshell"


def read_file(filename: str, if_exists: bool = False) -> str | None:
    if if_exists and not path.exists(filename):
        return None
    with open(filename) as f:
        return f.read()


# Copied from Python 3.3+'s shlex.quote()
def shell_quote(arg: str) -> str:
    # use single quotes, and put single quotes into double quotes
    # the string $'b is then quoted as '$'"'"'b'
    return "'" + arg.replace("'", "'\"'\"'") + "'"


@CommandProvider
class PostBuildCommands(CommandBase):
    @Command("run", description="Run Servo", category="post-build")
    @CommandArgument(
        "--android", action="store_true", default=None, help="Run on an Android device through `adb shell`"
    )
    @CommandArgument("--emulator", action="store_true", help="For Android, run in the only emulated device")
    @CommandArgument("--usb", action="store_true", help="For Android, run in the only USB device")
    @CommandArgument(
        "--debugger",
        action="store_true",
        help="Enable the debugger. Not specifying a "
        "--debugger-cmd option will result in the default "
        "debugger being used. The following arguments "
        "have no effect without this.",
    )
    @CommandArgument("--debugger-cmd", default=None, type=str, help="Name of debugger to use.")
    @CommandArgument("--headless", "-z", action="store_true", help="Launch in headless mode")
    @CommandArgument("--software", "-s", action="store_true", help="Launch with software rendering")
    @CommandArgument("params", nargs="...", help="Command-line arguments to be passed through to Servo")
    @CommandBase.common_command_arguments(binary_selection=True)
    @CommandBase.allow_target_configuration
    def run(
        self,
        servo_binary: str,
        params: list[str],
        debugger: bool = False,
        debugger_cmd: str | None = None,
        headless: bool = False,
        software: bool = False,
        emulator: bool = False,
        usb: bool = False,
    ) -> int | None:
        return self._run(servo_binary, params, debugger, debugger_cmd, headless, software, emulator, usb)

    def _run(
        self,
        servo_binary: str,
        params: list[str],
        debugger: bool = False,
        debugger_cmd: str | None = None,
        headless: bool = False,
        software: bool = False,
        emulator: bool = False,
        usb: bool = False,
    ) -> int | None:
        env = self.build_env()
        env["RUST_BACKTRACE"] = "1"
        if software:
            if not (is_linux() or is_freebsd()):
                print("Software rendering is only supported on Linux and FreeBSD at the moment.")
                return

            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        os.environ.update(env)

        # Make --debugger-cmd imply --debugger
        if debugger_cmd:
            debugger = True

        if is_android(self.target):
            if debugger:
                print("Android on-device debugging is not supported by mach yet. See")
                print("https://github.com/servo/servo/wiki/Building-for-Android#debugging-on-device")
                return
            script = [
                f"am force-stop {ANDROID_APP_NAME}",
            ]
            json_params = shell_quote(json.dumps(params))
            extra = "-e servoargs " + json_params
            rust_log = env.get("RUST_LOG", None)
            if rust_log:
                extra += " -e servolog " + rust_log
            gst_debug = env.get("GST_DEBUG", None)
            if gst_debug:
                extra += " -e gstdebug " + gst_debug
            script += [
                f"am start {extra} {ANDROID_APP_NAME}/{ANDROID_APP_NAME}.MainActivity",
                "sleep 0.5",
                f"echo Servo PID: $(pidof {ANDROID_APP_NAME})",
                f"logcat --pid=$(pidof {ANDROID_APP_NAME})",
                "exit",
            ]
            args = [self.android_adb_path(env)]
            if emulator and usb:
                print("Cannot run in both emulator and USB at the same time.")
                return 1
            if emulator:
                args += ["-e"]
            if usb:
                args += ["-d"]
            shell = subprocess.Popen(args + ["shell"], stdin=subprocess.PIPE)
            shell.communicate("\n".join(script) + "\n")
            return shell.wait()

        args = [servo_binary]

        if headless:
            args.append("-z")

        # Borrowed and modified from:
        # http://hg.mozilla.org/mozilla-central/file/c9cfa9b91dea/python/mozbuild/mozbuild/mach_commands.py#l883
        if debugger:
            if not debugger_cmd:
                # No debugger name was provided. Look for the default ones on
                # current OS.
                debugger_cmd = mozdebug.get_default_debugger_name(mozdebug.DebuggerSearch.KeepLooking)

            debugger_info = mozdebug.get_debugger_info(debugger_cmd)
            if not debugger_info:
                print("Could not find a suitable debugger in your PATH.")
                return 1

            command = debugger_info.path
            if debugger_cmd == "gdb" or debugger_cmd == "lldb":
                rust_command = "rust-" + debugger_cmd
                try:
                    subprocess.check_call([rust_command, "--version"], env=env, stdout=open(os.devnull, "w"))
                except (OSError, subprocess.CalledProcessError):
                    pass
                else:
                    command = rust_command

            # Prepend the debugger args.
            args = [command] + debugger_info.args + args + params
        else:
            args = args + params

        try:
            check_call(args, env=env)
        except subprocess.CalledProcessError as exception:
            if exception.returncode < 0:
                print(f"Servo was terminated by signal {-exception.returncode}")
            else:
                print(f"Servo exited with non-zero status {exception.returncode}")
            return exception.returncode
        except OSError as exception:
            if exception.errno == 2:
                print("Servo Binary can't be found! Run './mach build' and try again!")
            else:
                raise exception

    @Command("coverage-report", description="Create Servo Code Coverage report.", category="post-build")
    @CommandArgument("params", nargs="...", help="Command-line arguments to be passed through to cargo llvm-cov")
    @CommandBase.common_command_arguments(binary_selection=True, build_type=True, coverage_report=True)
    def coverage_report(self, build_type: BuildType, params: Optional[List[str]] = None, **kwargs: Any) -> int:
        target_dir = servo.util.get_target_dir()
        # See `cargo llvm-cov show-env`. We only export the values required at runtime.
        os.environ["CARGO_LLVM_COV"] = "1"
        os.environ["CARGO_LLVM_COV_SHOW_ENV"] = "1"
        os.environ["CARGO_LLVM_COV_TARGET_DIR"] = target_dir
        try:
            cargo_llvm_cov_cmd = ["cargo", "llvm-cov", "report", "--target", self.target.triple()]
            cargo_llvm_cov_cmd.extend(build_type.as_cargo_arg())
            cargo_llvm_cov_cmd.extend(params or [])
            subprocess.check_call(cargo_llvm_cov_cmd)
        except subprocess.CalledProcessError as exception:
            if exception.returncode < 0:
                print(f"`cargo llvm-cov` was terminated by signal {-exception.returncode}")
            else:
                print(f"`cargo llvm-cov` exited with non-zero status {exception.returncode}")
            return exception.returncode
        return 0

    @Command("size-report", description="Build and compare production-stripped size variants", category="post-build")
    @CommandArgument("--jobs", "-j", default=None, help="Number of jobs to run in parallel")
    @CommandArgument("--skip-package", action="store_true", help="Skip package generation and package-size metrics")
    @CommandArgument(
        "--include-second-order",
        action="store_true",
        help="Also run the currently supported second-order size variants",
    )
    @CommandArgument(
        "--variant",
        action="append",
        dest="variants",
        help="Only run the named variant (repeat to select multiple variants)",
    )
    @CommandArgument("--top", type=int, default=15, help="Number of top crates and symbols to report")
    @CommandArgument("--output", default=None, help="Write the Markdown report to this file")
    @CommandArgument("--json-output", default=None, help="Write machine-readable JSON results to this file")
    @CommandBase.common_command_arguments(build_configuration=True, package_configuration=True)
    def size_report(
        self,
        jobs: str | None = None,
        skip_package: bool = False,
        include_second_order: bool = False,
        variants: list[str] | None = None,
        top: int = 15,
        output: str | None = None,
        json_output: str | None = None,
        flavor: str | None = None,
        **kwargs: Any,
    ) -> int:
        manifest_path = path.join(self.get_top_dir(), "ports", "servoshell", "Cargo.toml")
        default_features = load_default_features(manifest_path)
        try:
            selected, skipped = selected_variants(variants, include_second_order)
        except ValueError as error:
            print(error)
            return 1
        extra_features = list(self.features)
        base_media_enabled = self.enable_media
        build_type = BuildType.custom(DEFAULT_SIZE_REPORT_PROFILE)
        output_dir = path.join(servo.util.get_target_dir(), build_type.directory_name(), "size-report")
        os.makedirs(output_dir, exist_ok=True)
        self.ensure_bootstrapped()

        measurements: list[VariantMeasurement] = []
        for variant in selected:
            measurement = VariantMeasurement(
                name=variant.name,
                description=variant.description,
                feature_list=[],
                removed_features=list(variant.removed_features),
                media_stack=variant.media_stack,
            )

            variant_features = features_for_variant(default_features, variant)
            for feature in extra_features:
                if feature not in variant_features:
                    variant_features.append(feature)
            measurement.feature_list = variant_features

            self.features = variant_features
            self.enable_media = base_media_enabled if variant.media_stack is None else self.is_media_enabled(variant.media_stack)
            env = self.build_env()
            map_path = path.join(output_dir, f"{variant.name}.map")
            env["RUSTFLAGS"] = env.get("RUSTFLAGS", "") + f" -C link-arg=-Wl,-Map,{map_path}"

            cargo_args = ["--profile", build_type.profile, "--no-default-features"]
            if jobs is not None:
                cargo_args += ["-j", jobs]

            status = self.run_cargo_build_like_command("rustc", cargo_args, env=env)
            if status != 0:
                return status

            binary_path = self.get_binary_path(build_type)
            measurement.binary_path = binary_path
            measurement.binary_size = path.getsize(binary_path)
            measurement.shared_libraries, measurement.shared_library_footprint = self._collect_shared_libraries(binary_path)

            if variant.name == "baseline":
                measurement.section_sizes = self._capture_section_sizes(binary_path)
                measurement.top_symbols = self._capture_top_symbols(binary_path, top)
                if path.exists(map_path):
                    with open(map_path, encoding="utf-8", errors="replace") as linker_map:
                        measurement.top_crates = parse_linker_map_crates(linker_map.read(), top)
                else:
                    measurement.notes.append(f"No linker map was produced for `{variant.name}`.")
                if not measurement.top_symbols:
                    measurement.notes.append(
                        "No symbol-level entries were recovered from the stripped baseline binary; crate attribution comes from the linker map."
                    )

            if not skip_package:
                package_status = Registrar.dispatch(
                    "package",
                    context=self.context,
                    build_type=build_type,
                    flavor=flavor,
                    preserve_app="darwin" in self.target.triple(),
                )
                if package_status not in (0, None):
                    return package_status
                self._populate_package_metrics(measurement, build_type)

            measurements.append(measurement)

        report = build_markdown_report(build_type.profile, self.target.triple(), measurements, skipped)
        output_path = output or path.join(output_dir, "report.md")
        with open(output_path, "w", encoding="utf-8") as report_file:
            report_file.write(report)

        payload = {
            "target": self.target.triple(),
            "profile": build_type.profile,
            "measurements": [measurement.to_dict() for measurement in measurements],
            "skipped_variants": [
                {
                    "name": variant.name,
                    "description": variant.description,
                    "reason": variant.unavailable_reason,
                }
                for variant in skipped
            ],
        }
        json_output_path = json_output or path.join(output_dir, "report.json")
        with open(json_output_path, "w", encoding="utf-8") as json_file:
            json.dump(payload, json_file, indent=2, sort_keys=True)

        print(report, end="")
        print(f"Wrote Markdown report to {output_path}")
        print(f"Wrote JSON report to {json_output_path}")
        return 0

    def _run_capture(self, command: list[str]) -> str | None:
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        return result.stdout

    def _capture_section_sizes(self, binary_path: str) -> dict[str, int]:
        for command in (["llvm-size", "-A", binary_path], ["size", "-A", binary_path]):
            output = self._run_capture(command)
            if output:
                return parse_section_sizes(output)
        return {}

    def _capture_top_symbols(self, binary_path: str, limit: int) -> list[tuple[str, int]]:
        for command in (
            ["llvm-nm", "--print-size", "--size-sort", "--radix=d", binary_path],
            ["nm", "-S", "--size-sort", "--radix=d", binary_path],
        ):
            output = self._run_capture(command)
            if output:
                return parse_nm_symbols(output, limit)
        return []

    def _collect_shared_libraries(self, binary_path: str) -> tuple[list[tuple[str, int]], int]:
        commands = []
        if is_linux() or is_freebsd():
            commands.append(["ldd", binary_path])
        elif servo.platform.get().is_macos:
            commands.append(["otool", "-L", binary_path])

        library_paths: list[str] = []
        for command in commands:
            output = self._run_capture(command)
            if output:
                library_paths = parse_dynamic_library_paths(output)
                break

        libraries: list[tuple[str, int]] = []
        total = 0
        for library_path in library_paths:
            if not path.exists(library_path):
                continue
            library_size = path.getsize(library_path)
            libraries.append((library_path, library_size))
            total += library_size
        return libraries, total

    def _package_root(self, build_type: BuildType) -> str:
        return path.dirname(self.get_binary_path(build_type))

    def _locate_package_path(self, build_type: BuildType) -> str | None:
        package_root = self._package_root(build_type)
        target_triple = self.target.triple()
        if is_android(self.target):
            return self.target.get_package_path(build_type.directory_name())
        if "darwin" in target_triple:
            return path.join(package_root, "Servo.app")
        if "windows" in target_triple:
            return path.join(package_root, "msi", "ServoShell.zip")
        return path.join(package_root, "servo-tech-demo.tar.gz")

    def _directory_size(self, root: str) -> int:
        total = 0
        for current_root, _, files in os.walk(root):
            for filename in files:
                total += path.getsize(path.join(current_root, filename))
        return total

    def _directory_subset_size(self, root: str, folder_name: str) -> int:
        total = 0
        for current_root, _, files in os.walk(root):
            rel_root = path.relpath(current_root, root)
            rel_root = "" if rel_root == "." else rel_root
            if folder_name not in rel_root.split(path.sep):
                continue
            for filename in files:
                total += path.getsize(path.join(current_root, filename))
        return total

    def _zip_summary(self, zip_path: str) -> tuple[int, int, int, int]:
        installed_size = 0
        resource_size = 0
        library_size = 0
        binary_size = 0
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                installed_size += member.file_size
                member_name = member.filename.lower()
                if "/resources/" in member_name:
                    resource_size += member.file_size
                if member_name.endswith((".dll", ".dylib", ".so")):
                    library_size += member.file_size
                if path.basename(member.filename).startswith("servoshell"):
                    binary_size += member.file_size
        return installed_size, resource_size, library_size, binary_size

    def _populate_package_metrics(self, measurement: VariantMeasurement, build_type: BuildType) -> None:
        package_path = self._locate_package_path(build_type)
        if package_path is None or not path.exists(package_path):
            measurement.notes.append(f"Package artifact not found for `{measurement.name}`.")
            return

        measurement.package_path = package_path

        target_triple = self.target.triple()
        if path.isdir(package_path):
            measurement.installed_size = self._directory_size(package_path)
            measurement.packaged_resource_size = self._directory_subset_size(package_path, "Resources")
            measurement.packaged_library_size = self._directory_subset_size(package_path, "lib")
            binary_path = path.join(package_path, "Contents", "MacOS", "servoshell")
            if path.exists(binary_path):
                measurement.packaged_binary_size = path.getsize(binary_path)
            return

        measurement.package_archive_size = path.getsize(package_path)

        if package_path.endswith((".tar", ".tar.gz", ".tgz")):
            (
                measurement.installed_size,
                measurement.packaged_resource_size,
                measurement.packaged_library_size,
                measurement.packaged_binary_size,
            ) = summarize_tarball(package_path)
            return

        if package_path.endswith(".zip"):
            (
                measurement.installed_size,
                measurement.packaged_resource_size,
                measurement.packaged_library_size,
                measurement.packaged_binary_size,
            ) = self._zip_summary(package_path)
            return

        if is_android(self.target) or "openharmony" in target_triple or "ohos" in target_triple:
            measurement.notes.append(
                f"Only the archive size was recorded for `{measurement.name}` because packaged contents are not expanded on this platform."
            )
            return

        measurement.notes.append(
            f"Archive size was recorded for `{measurement.name}`, but installed/package breakdown is not implemented for `{target_triple}`."
        )

    @Command("android-emulator", description="Run the Android emulator", category="post-build")
    @CommandArgument("args", nargs="...", help="Command-line arguments to be passed through to the emulator")
    def android_emulator(self, args: list[str] | None = None) -> int:
        if not args:
            args = []
            print("AVDs created by `./mach bootstrap-android` are servo-arm and servo-x86.")
        emulator = self.android_emulator_path(self.build_env())
        return subprocess.call([emulator] + args)

    @Command("rr-record", description="Run Servo whilst recording execution with rr", category="post-build")
    @CommandArgument("params", nargs="...", help="Command-line arguments to be passed through to Servo")
    @CommandBase.common_command_arguments(binary_selection=True)
    def rr_record(self, servo_binary: str, params: list[str] = []) -> None:
        env = self.build_env()
        env["RUST_BACKTRACE"] = "1"

        servo_cmd = [servo_binary] + params
        rr_cmd = ["rr", "--fatal-errors", "record"]
        try:
            check_call(rr_cmd + servo_cmd)
        except OSError as e:
            if e.errno == 2:
                print("rr binary can't be found!")
            else:
                raise e

    @Command(
        "rr-replay",
        description="Replay the most recent execution of Servo that was recorded with rr",
        category="post-build",
    )
    def rr_replay(self) -> None:
        try:
            check_call(["rr", "--fatal-errors", "replay"])
        except OSError as e:
            if e.errno == 2:
                print("rr binary can't be found!")
            else:
                raise e

    @Command("doc", description="Generate documentation", category="post-build")
    @CommandArgument("params", nargs="...", help="Command-line arguments to be passed through to cargo doc")
    @CommandBase.common_command_arguments(build_configuration=True, build_type=False)
    def doc(self, params: list[str], **kwargs: Any) -> CompletedProcess[bytes] | int | None:
        self.ensure_bootstrapped()

        docs = path.join(servo.util.get_target_dir(), "doc")
        if not path.exists(docs):
            os.makedirs(docs)

        # Document library crates to avoid package name conflict between servoshell
        # and libservo. Besides, main.rs in servoshell is just a stub.
        params.insert(0, "--lib")
        # Documentation build errors shouldn't cause the entire build to fail. This
        # prevents issues with dependencies from breaking our documentation build,
        # with the downside that it hides documentation issues.
        params.insert(0, "--keep-going")

        env = self.build_env()
        env["RUSTC"] = "rustc"
        returncode = self.run_cargo_build_like_command("doc", params, env=env, **kwargs)
        if returncode:
            return returncode

        static = path.join(self.context.topdir, "etc", "doc.servo.org")
        for name in os.listdir(static):
            copy2(path.join(static, name), path.join(docs, name))
