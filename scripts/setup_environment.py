#!/usr/bin/env python3
"""AndroidForge — Toolchain Setup.

Reads the JSON produced by detect_project.py and config/toolchain-rules.yaml,
then decides:

  • Which JDK version to install
  • Which Gradle version to install
  • Which AGP version is in use
  • Which NDK version to install (if any)
  • Whether Flutter is needed
  • Which compatibility fixes to apply

The script writes its decisions to $GITHUB_OUTPUT and prints a JSON summary.
It also applies non-destructive compatibility fixes to the build workspace
(NEVER to the original source ZIP).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "toolchain-rules.yaml"


def load_rules(path: Path | None = None) -> dict[str, Any]:
    p = path or RULES_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data or {}


def major(v: str) -> int:
    try:
        return int(str(v).split(".")[0])
    except (ValueError, IndexError):
        return 0


def minor(v: str) -> int:
    try:
        return int(str(v).split(".")[1])
    except (ValueError, IndexError):
        return 0


def version_tuple(v: str) -> tuple[int, ...]:
    """Convert a version string such as 8.7.3 into a comparable tuple."""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, TypeError):
        return (0,)


def pick_jdk_for_agp(agp: str | None, rules: dict) -> str:
    """Determine JDK version required by AGP."""
    if not agp:
        return "17"

    m = major(agp)

    if m >= 8:
        return "17"
    if m == 7:
        return "11"
    if m == 4:
        return "11"
    if m <= 3:
        return "8"

    for pattern, jdk in rules.get("agp_to_jdk", {}).items():
        if pattern.endswith(".x"):
            pm = pattern[:-2]
            if str(agp).startswith(pm):
                return str(jdk)
        elif str(agp).startswith(pattern):
            return str(jdk)

    return "17"


def pick_jdk_for_gradle(gradle: str | None, rules: dict) -> str:
    if not gradle:
        return "17"

    m = major(gradle)

    if m >= 8:
        return "17"
    if m == 7:
        return "11"
    if m <= 6:
        return "8"

    return "17"


def pick_gradle_for_agp(agp: str | None, rules: dict) -> str | None:
    """Look up the minimum Gradle version required by an AGP version."""
    if not agp:
        return None

    mapping = rules.get("agp_to_gradle", {})

    # Exact match.
    if agp in mapping:
        return str(mapping[agp])

    # Prefix match, e.g. 7.4.2 -> 7.4.
    parts = str(agp).split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in mapping:
            return str(mapping[prefix])

    # Major-version fallback.
    m = major(agp)
    for k, v in mapping.items():
        if major(str(k)) == m:
            return str(v)

    return None


def pick_flutter_version(detect: dict, rules: dict) -> str:
    flutter = detect.get("flutter", {}) or {}
    constraint = flutter.get("flutter_version_constraint")

    if constraint:
        m = re.search(r"(\d+\.\d+\.\d+)", constraint)
        if m:
            return m.group(1)

    return str(
        rules.get("flutter", {}).get("default_version", "3.24.0")
    )


def pick_ndk_version(detect: dict, rules: dict) -> str:
    ndk_v = (detect.get("versions") or {}).get("ndk_version")

    if ndk_v:
        return str(ndk_v)

    return str(
        rules.get("ndk", {}).get("default_version", "26.1.10909125")
    )


def determine_legacy_fixes(
    detect: dict,
    rules: dict,
) -> list[dict]:
    fixes: list[dict] = []

    wrapper = detect.get("wrapper", {}) or {}
    gradle_v = wrapper.get("version")
    has_local = "local.properties" in detect.get("indicator_files", {})

    if wrapper.get("missing_jar") or not wrapper.get("present"):
        fixes.append({
            "name": "patch_gradle_wrapper",
            "reason": "Missing or corrupt gradle-wrapper.jar",
        })

    if wrapper.get("uses_http"):
        fixes.append({
            "name": "migrate_https",
            "reason": "Wrapper uses http:// distribution URL",
        })

    if gradle_v and major(str(gradle_v)) < 4:
        fixes.append({
            "name": "disable_gradle_daemon",
            "reason": f"Very old Gradle: {gradle_v}",
        })

    if not has_local and detect.get("project_type") in ("gradle", "flutter"):
        fixes.append({
            "name": "inject_local_properties",
            "reason": "local.properties missing",
        })

    return fixes


def apply_fixes(
    detect: dict,
    fixes: list[dict],
    android_sdk_root: str,
) -> list[str]:
    """Apply non-destructive compatibility fixes to the project tree."""
    applied: list[str] = []

    root = Path(detect["project_root"])
    props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"

    for fix in fixes:
        name = fix["name"]

        if name == "migrate_https" and props.exists():
            content = props.read_text(
                encoding="utf-8",
                errors="replace",
            )

            new_content = re.sub(
                r"distributionUrl=http://",
                "distributionUrl=https://",
                content,
            )

            if new_content != content:
                props.write_text(
                    new_content,
                    encoding="utf-8",
                )
                applied.append(
                    f"Migrated wrapper distribution URL to HTTPS in {props}"
                )

        elif name == "inject_local_properties":
            (root / "local.properties").write_text(
                f"sdk.dir={android_sdk_root}\n",
                encoding="utf-8",
            )
            applied.append(
                f"Created local.properties with sdk.dir={android_sdk_root}"
            )

        elif name == "disable_gradle_daemon":
            gradle_props = root / "gradle.properties"

            content = (
                gradle_props.read_text(encoding="utf-8")
                if gradle_props.exists()
                else ""
            )

            if "org.gradle.daemon" not in content:
                with gradle_props.open(
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write(
                        "\n# AndroidForge: disable Gradle daemon "
                        "for old Gradle\n"
                        "org.gradle.daemon=false\n"
                    )

                applied.append(
                    "Disabled Gradle daemon via gradle.properties"
                )

        elif name == "patch_gradle_wrapper":
            applied.append(
                "Wrapper missing/corrupt; standalone Gradle will be used."
            )

    return applied


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AndroidForge toolchain setup"
    )

    parser.add_argument(
        "--root",
        required=True,
        help="Project root path",
    )

    parser.add_argument(
        "--detect",
        required=True,
        help="Detection JSON (string or file path)",
    )

    parser.add_argument(
        "--rules",
        default=None,
        help="Path to toolchain-rules.yaml",
    )

    parser.add_argument(
        "--android-sdk",
        default=os.environ.get(
            "ANDROID_SDK_ROOT",
            "/usr/local/lib/android/sdk",
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON summary to this file",
    )

    args = parser.parse_args()

    rules = load_rules(
        Path(args.rules) if args.rules else None
    )

    # ------------------------------------------------------------------
    # Parse detection JSON
    # ------------------------------------------------------------------

    if Path(args.detect).exists():
        detect = json.loads(
            Path(args.detect).read_text(
                encoding="utf-8"
            )
        )
    else:
        detect = json.loads(args.detect)

    versions = detect.get("versions", {}) or {}
    wrapper = detect.get("wrapper", {}) or {}

    agp = versions.get("agp_version")
    gradle_wrapper_v = wrapper.get("version")
    java_in_build = versions.get("java_version")
    kotlin_v = versions.get("kotlin_version")

    # ------------------------------------------------------------------
    # Pick JDK
    # ------------------------------------------------------------------

    jdk_from_agp = pick_jdk_for_agp(
        agp,
        rules,
    )

    jdk_from_gradle = pick_jdk_for_gradle(
        gradle_wrapper_v,
        rules,
    )

    jdk_candidates = [
        jdk_from_agp,
        jdk_from_gradle,
    ]

    if java_in_build:
        jdk_candidates.append(
            str(java_in_build)
        )

    chosen_jdk = max(
        jdk_candidates,
        key=lambda v: (
            major(v),
            minor(v),
        ),
    )

    # ------------------------------------------------------------------
    # Pick Gradle
    #
    # IMPORTANT:
    # A bundled Gradle wrapper must NEVER override the minimum
    # Gradle version required by the detected AGP.
    #
    # Example:
    #   AGP 8.7.3
    #   wrapper 8.0
    #
    # Result:
    #   Gradle 8.9
    #   standalone Gradle
    # ------------------------------------------------------------------

    required_gradle = pick_gradle_for_agp(
        agp,
        rules,
    )

    if required_gradle and gradle_wrapper_v:
        if version_tuple(str(gradle_wrapper_v)) < version_tuple(
            str(required_gradle)
        ):
            chosen_gradle = str(required_gradle)
            use_wrapper = False
        else:
            chosen_gradle = str(gradle_wrapper_v)
            use_wrapper = bool(
                wrapper.get("present")
            )

    elif gradle_wrapper_v:
        chosen_gradle = str(gradle_wrapper_v)
        use_wrapper = bool(
            wrapper.get("present")
        )

    elif required_gradle:
        chosen_gradle = str(required_gradle)
        use_wrapper = False

    else:
        chosen_gradle = "8.9"
        use_wrapper = False

    # ------------------------------------------------------------------
    # Wrapper HTTP URL
    # ------------------------------------------------------------------

    if wrapper.get("uses_http"):
        use_wrapper = True

    # ------------------------------------------------------------------
    # Flutter
    # ------------------------------------------------------------------

    needs_flutter = bool(
        (detect.get("flutter") or {}).get("is_flutter")
    )

    chosen_flutter = (
        pick_flutter_version(
            detect,
            rules,
        )
        if needs_flutter
        else None
    )

    # ------------------------------------------------------------------
    # NDK
    # ------------------------------------------------------------------

    needs_ndk = bool(
        (detect.get("native") or {}).get("has_native")
    )

    chosen_ndk = (
        pick_ndk_version(
            detect,
            rules,
        )
        if needs_ndk
        else None
    )

    # ------------------------------------------------------------------
    # Compatibility fixes
    # ------------------------------------------------------------------

    fixes = determine_legacy_fixes(
        detect,
        rules,
    )

    applied_fixes = apply_fixes(
        detect,
        fixes,
        args.android_sdk,
    )

    # ------------------------------------------------------------------
    # Build commands
    # ------------------------------------------------------------------

    if needs_flutter:
        build_commands = [
            [
                "flutter",
                "pub",
                "get",
            ],
            [
                "flutter",
                "build",
                "apk",
                "--debug",
            ],
            [
                "flutter",
                "build",
                "apk",
                "--release",
            ],
        ]

    else:
        gradlew = (
            Path(detect["project_root"])
            / "gradlew"
        )

        if use_wrapper and gradlew.exists():
            gradle_cmd = str(gradlew)
        else:
            gradle_cmd = "gradle"

        build_commands = [
            [
                gradle_cmd,
                "assembleDebug",
            ],
            [
                gradle_cmd,
                "assembleRelease",
            ],
            [
                gradle_cmd,
                "bundleDebug",
            ],
        ]

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    result = {
        "project_root": detect["project_root"],
        "jdk_version": chosen_jdk,
        "gradle_version": chosen_gradle,
        "agp_version": agp,
        "kotlin_version": kotlin_v,
        "ndk_version": chosen_ndk,
        "flutter_version": chosen_flutter,
        "use_gradle_wrapper": use_wrapper,
        "needs_flutter": needs_flutter,
        "needs_ndk": needs_ndk,
        "needs_gradle": detect.get("project_type")
        in ("gradle", "flutter"),
        "legacy_fixes_applied": applied_fixes,
        "build_commands": build_commands,
    }

    # ------------------------------------------------------------------
    # Output JSON
    # ------------------------------------------------------------------

    output = json.dumps(
        result,
        indent=2,
    )

    if args.output:
        Path(args.output).write_text(
            output,
            encoding="utf-8",
        )
    else:
        print(output)

    # ------------------------------------------------------------------
    # GitHub Actions outputs
    # ------------------------------------------------------------------

    gh_output = os.environ.get(
        "GITHUB_OUTPUT"
    )

    if gh_output:
        with open(
            gh_output,
            "a",
            encoding="utf-8",
        ) as f:
            f.write(
                f"jdk_version={chosen_jdk}\n"
            )

            f.write(
                f"gradle_version={chosen_gradle}\n"
            )

            if agp:
                f.write(
                    f"agp_version={agp}\n"
                )

            if kotlin_v:
                f.write(
                    f"kotlin_version={kotlin_v}\n"
                )

            if chosen_ndk:
                f.write(
                    f"ndk_version={chosen_ndk}\n"
                )

            f.write(
                "needs_flutter="
                f"{'true' if needs_flutter else 'false'}\n"
            )

            f.write(
                "needs_ndk="
                f"{'true' if needs_ndk else 'false'}\n"
            )

            f.write(
                "needs_gradle="
                f"{'true' if result['needs_gradle'] else 'false'}\n"
            )

            f.write(
                "use_wrapper="
                f"{'true' if use_wrapper else 'false'}\n"
            )

            if chosen_flutter:
                f.write(
                    f"flutter_version={chosen_flutter}\n"
                )

            f.write(
                "json<<EOF\n"
                f"{json.dumps(result)}\n"
                "EOF\n"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
