import importlib.resources
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from itertools import islice
from pathlib import Path

import yaml
from rich import box
from rich.progress import Progress
from rich.table import Table

from blint.binary import parse
from blint.logger import LOG, console
from blint.utils import find_exe_files

review_files = []
review_methods_dir = importlib.resources.contents("blint.data.annotations")
review_files = [rf for rf in review_methods_dir if rf.endswith(".yml")]
# review_files = [p.as_posix() for p in Path(review_methods_dir).rglob("*.yml")]

rules_dict = {}
review_methods_dict = defaultdict(list)
review_symbols_dict = defaultdict(list)
review_rules_cache = {}

# Debug mode
DEBUG_MODE = os.getenv("SCAN_DEBUG_MODE") == "debug"

# Load the rules
with importlib.resources.open_text("blint.data", "rules.yml") as fp:
    raw_data = fp.read().split("---")
    for tmp_data in raw_data:
        if not tmp_data:
            continue
        rules_list = yaml.safe_load(tmp_data)
        for rule in rules_list:
            rules_dict[rule.get("id")] = rule

# Load the default review methods
for review_methods_file in review_files:
    if DEBUG_MODE:
        LOG.debug(f"Loading review file {review_methods_file}")
    with importlib.resources.open_text(
        "blint.data.annotations", review_methods_file
    ) as fp:
        raw_data = fp.read().split("---")
        for tmp_data in raw_data:
            if not tmp_data:
                continue
            methods_reviews_groups = yaml.safe_load(tmp_data)
            all_rules = methods_reviews_groups.get("rules")
            method_rules_dict = {}
            for rule in all_rules:
                method_rules_dict[rule.get("id")] = rule
                review_rules_cache[rule.get("id")] = rule
            if methods_reviews_groups.get("group") == "METHOD_REVIEWS":
                review_methods_dict[methods_reviews_groups.get("exe_type")].append(
                    method_rules_dict
                )
            elif methods_reviews_groups.get("group") == "SYMBOL_REVIEWS":
                review_symbols_dict[methods_reviews_groups.get("exe_type")].append(
                    method_rules_dict
                )


def check_nx(f, metadata, rule_obj):
    if metadata.get("has_nx") is False:
        return False
    return True


def check_pie(f, metadata, rule_obj):
    if metadata.get("is_pie") is False:
        return False
    return True


def check_relro(f, metadata, rule_obj):
    if metadata.get("relro") == "no":
        return False
    return True


def check_canary(f, metadata, rule_obj):
    if metadata.get("has_canary") is False:
        return False
    return True


def check_rpath(f, metadata, rule_obj):
    # Do not recommend setting rpath or runpath
    if metadata.get("has_rpath") or metadata.get("has_runpath"):
        return False
    return True


def check_virtual_size(f, metadata, rule_obj):
    if metadata.get("virtual_size"):
        virtual_size = metadata.get("virtual_size") / 1024 / 1024
        size_limit = 30
        if rule_obj.get("limit"):
            limit = rule_obj.get("limit")
            limit = limit.replace("MB", "").replace("M", "")
            if isinstance(limit, str) and rule_obj.get("limit").isdigit():
                size_limit = int(rule_obj.get("limit"))
        return virtual_size < size_limit
    return True


def run_checks(f, metadata):
    results = []
    if not rules_dict:
        LOG.warn("No rules loaded!")
        return None
    if not metadata:
        return None
    for cid, rule_obj in rules_dict.items():
        exe_type = metadata.get("exe_type")
        rule_exe_types = rule_obj.get("exe_types")
        # Skip rules that are not valid for this exe type
        if exe_type and rule_exe_types and exe_type not in rule_exe_types:
            continue
        cfn = getattr(sys.modules[__name__], cid.lower(), None)
        if cfn:
            result = cfn(f, metadata, rule_obj=rule_obj)
            if result is False:
                aresult = {**rule_obj, "filename": f}
                if metadata.get("name"):
                    aresult["exe_name"] = metadata.get("name")
                results.append(aresult)
    return results


def run_review_methods_symbols(review_methods_list, functions_list):
    results = defaultdict(list)
    found_cid = {}
    found_pattern = {}
    found_function = {}
    for review_methods in review_methods_list:
        for cid, rule_obj in review_methods.items():
            if found_cid.get(cid):
                continue
            patterns = rule_obj.get("patterns")
            for apattern in patterns:
                if found_pattern.get(apattern) or found_cid.get(cid):
                    continue
                for afun in functions_list:
                    if apattern.lower() in afun.lower() and not found_function.get(
                        afun.lower()
                    ):
                        result = {
                            "pattern": apattern,
                            "function": afun,
                        }
                        results[cid].append(result)
                        found_cid[cid] = True
                        found_pattern[apattern] = True
                        found_function[afun.lower()] = True
    return results


def run_review(f, metadata):
    results = {}
    if not review_methods_dict:
        LOG.warn("No review methods loaded!")
        return None
    exe_type = metadata.get("exe_type")
    if not metadata or not exe_type:
        return None
    review_methods_list = review_methods_dict.get(exe_type)
    review_symbols_list = review_symbols_dict.get(exe_type)
    # Check if reviews are available for this exe type
    if not review_methods_list and not review_symbols_list:
        return None
    if review_methods_list:
        functions_list = [
            re.sub(r"[*&()]", "", f.get("name", ""))
            for f in metadata.get("functions", [])
        ]
        if metadata.get("magic", "").startswith("PE"):
            functions_list += [
                f.get("name", "") for f in metadata.get("static_symbols", [])
            ]
        # If there are no function but static symbols use that instead
        if not functions_list and metadata.get("static_symbols"):
            functions_list = [
                f.get("name", "").lower() for f in metadata.get("static_symbols", [])
            ]
        LOG.debug(f"Reviewing {len(functions_list)} functions")
        results.update(run_review_methods_symbols(review_methods_list, functions_list))
    if review_symbols_list:
        symbols_list = [
            f.get("name", "").lower() for f in metadata.get("dynamic_symbols", [])
        ]
        symbols_list += [
            f.get("name", "").lower() for f in metadata.get("static_symbols", [])
        ]
        LOG.debug(f"Reviewing {len(symbols_list)} symbols")
        results.update(run_review_methods_symbols(review_symbols_list, symbols_list))
    return results


def start(args, src, reports_dir):
    files = [src]
    findings = []
    reviews = []
    if os.path.isdir(src):
        files = find_exe_files(src)
    with Progress(
        transient=True,
        redirect_stderr=False,
        redirect_stdout=False,
        refresh_per_second=1,
    ) as progress:
        task = progress.add_task(
            f"[green] Blinting {len(files)} binaries",
            total=len(files),
            start=True,
        )
        for f in files:
            progress.update(task, description=f"Processing [bold]{f}[/bold]")
            metadata = parse(f)
            exe_name = metadata.get("name", "")
            # In case of debug store raw metadata
            if DEBUG_MODE:
                metadata_file = Path(reports_dir) / (exe_name + "-metadata.json")
                LOG.debug(f"Metadata written to {metadata_file}")
                with open(metadata_file, mode="w") as ffp:
                    json.dump(metadata, ffp, indent=True)
            progress.update(
                task, description=f"Checking [bold]{f}[/bold] against rules"
            )
            finding = run_checks(f, metadata)
            if finding:
                findings += finding
            if not args.no_reviews:
                progress.update(
                    task, description="Checking methods against review rules"
                )
                review = run_review(f, metadata)
                if review:
                    for cid, evidence in review.items():
                        aresult = {
                            **review_rules_cache.get(cid),
                            "evidence": evidence,
                            "filename": f,
                            "exe_name": exe_name,
                        }
                        del aresult["patterns"]
                        reviews.append(aresult)
            progress.advance(task)
    return findings, reviews


def print_findings_table(findings):
    table = Table(
        title="BLint Findings",
        box=box.DOUBLE_EDGE,
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("ID")
    table.add_column("Binary")
    table.add_column("Title")
    table.add_column("Severity")
    for f in findings:
        severity = f.get("severity").upper()
        table.add_row(
            f.get("id"),
            f.get("exe_name"),
            f.get("title"),
            "{}{}".format(
                "[bright_red]" if severity in ("CRITICAL", "HIGH") else "", severity
            ),
        )
    console.print(table)


def print_reviews_table(reviews):
    table = Table(
        title="BLint Capability Review",
        box=box.DOUBLE_EDGE,
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("ID")
    table.add_column("Binary")
    table.add_column("Capabilities")
    table.add_column("Evidence (Top 2)")
    for r in reviews:
        evidences = [e.get("function") for e in r.get("evidence")]
        evidences = list(islice(evidences, 2))
        table.add_row(
            r.get("id"),
            r.get("exe_name"),
            r.get("summary"),
            "\n".join(evidences),
        )
    console.print(table)


def report(args, src_dir, reports_dir, findings, reviews):
    run_uuid = os.environ.get("SCAN_ID", str(uuid.uuid4()))
    common_metadata = {
        "scan_id": run_uuid,
        "created": f"{datetime.now():%Y-%m-%d %H:%M:%S%z}",
    }
    if findings:
        print_findings_table(findings)
        findings_file = Path(reports_dir) / "findings.json"
        LOG.info(f"Findings written to {findings_file}")
        with open(findings_file, mode="w") as ffp:
            json.dump({**common_metadata, "findings": findings}, ffp, indent=True)
    if reviews:
        print_reviews_table(reviews)
        reviews_file = Path(reports_dir) / "reviews.json"
        LOG.info(f"Review written to {reviews_file}")
        with open(reviews_file, mode="w") as rfp:
            json.dump({**common_metadata, "reviews": reviews}, rfp, indent=True)
    if not findings and not reviews:
        LOG.info(f":white_heavy_check_mark: No issues found in {src_dir}!")