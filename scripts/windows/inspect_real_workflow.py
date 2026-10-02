"""Read a private ComfyUI API workflow and emit deterministic mapping candidates."""

import argparse
import json
from pathlib import Path
import re

import yaml


def _literal(value):
    return not isinstance(value, (list, dict))


def _record(node_id, node, input_name=None, score=0, reason=""):
    value = node["inputs"].get(input_name) if input_name else None
    return {
        "node_id": node_id,
        "class_type": node["class_type"],
        "title": node.get("_meta", {}).get("title", ""),
        "input": input_name,
        "value": value,
        "inputs": node["inputs"],
        "score": score,
        "reason": reason,
    }


def _pick(candidates, minimum):
    if not candidates:
        return None, "MISSING"
    ordered = sorted(candidates, key=lambda item: item["score"], reverse=True)
    if ordered[0]["score"] < minimum:
        return None, "LOW_CONFIDENCE"
    if len(ordered) > 1 and ordered[0]["score"] == ordered[1]["score"]:
        return None, "AMBIGUOUS"
    return ordered[0], "HIGH"


def inspect(workflow):
    if "nodes" in workflow or "links" in workflow:
        raise ValueError("UI Format detected; export API Format before mapping")
    if not workflow or not all(
        isinstance(node_id, str)
        and isinstance(node, dict)
        and isinstance(node.get("class_type"), str)
        and isinstance(node.get("inputs"), dict)
        for node_id, node in workflow.items()
    ):
        raise ValueError("Workflow is not an API-format node object")

    prompt, duration, scheduler, seed, outputs = [], [], [], [], []
    reference_links = []
    for node_id, node in workflow.items():
        class_type = node["class_type"]
        title = str(node.get("_meta", {}).get("title", ""))
        label = f"{class_type} {title}".lower()
        for name, value in node["inputs"].items():
            lowered = name.lower()
            if _literal(value) and isinstance(value, str) and lowered in {"text", "value", "prompt"}:
                score = 2 + 2 * ("prompt" in label) + ("text" in label or "string" in label)
                prompt.append(_record(node_id, node, name, score, "literal text input on prompt/text node"))
            if _literal(value) and type(value) in {int, float} and lowered in {"value", "duration", "seconds"}:
                score = 2 + 3 * ("duration" in label) + ("float" in label)
                duration.append(_record(node_id, node, name, score, "numeric duration/float input"))
            if _literal(value) and type(value) is int and lowered == "steps":
                score = 3 + 3 * ("scheduler" in label)
                scheduler.append(_record(node_id, node, name, score, "integer steps input on scheduler"))
            if _literal(value) and type(value) is int and lowered in {"seed", "noise_seed"}:
                score = 3 + 2 * ("noise" in label or "seed" in label)
                seed.append(_record(node_id, node, name, score, "integer seed/noise_seed input"))
            if re.search(r"ref.*image", lowered) and isinstance(value, list) and len(value) == 2:
                source_id = str(value[0])
                source = workflow.get(source_id)
                image_input = None
                if isinstance(source, dict):
                    for source_input, source_value in source.get("inputs", {}).items():
                        if "image" in source_input.lower() and isinstance(source_value, str):
                            image_input = source_input
                            break
                reference_links.append({
                    "reference_node": node_id,
                    "reference_class_type": class_type,
                    "reference_input": name,
                    "link": value,
                    "load_node": source_id,
                    "load_class_type": source.get("class_type") if isinstance(source, dict) else None,
                    "load_input": image_input,
                    "current_value": source["inputs"].get(image_input) if image_input else None,
                })
        if "video" in label and ("save" in label or "combine" in label or "animated" in label):
            fields = [name for name in node["inputs"] if name.lower() in {
                "filename", "filename_prefix", "output_path", "path", "name"
            }]
            score = 3 + 2 * ("save" in label) + bool(fields)
            outputs.append(_record(node_id, node, fields[0] if fields else None, score,
                                   "video save/output node"))

    selected_prompt, prompt_confidence = _pick(prompt, 4)
    selected_duration, duration_confidence = _pick(duration, 5)
    selected_scheduler, scheduler_confidence = _pick(scheduler, 5)
    selected_output, output_confidence = _pick(outputs, 4)
    selected_seed, seed_confidence = _pick(seed, 4)

    owners = {}
    for link in reference_links:
        owners.setdefault(link["reference_node"], []).append(link)
    reference_confidence = "MISSING"
    selected_references = []
    if owners:
        ranked = sorted(owners.values(), key=len, reverse=True)
        if len(ranked) == 1 or len(ranked[0]) > len(ranked[1]):
            selected_references = ranked[0]
            valid = all(
                link["load_class_type"] == "LoadImage" and link["load_input"]
                for link in selected_references
            )
            unique = len({link["load_node"] for link in selected_references}) == len(selected_references)
            reference_confidence = "HIGH" if valid and unique else "AMBIGUOUS"
        else:
            reference_confidence = "AMBIGUOUS"

    mapping = {}
    for key, selected in (
        ("prompt", selected_prompt),
        ("duration", selected_duration),
        ("scheduler", selected_scheduler),
    ):
        if selected:
            mapping[f"{key}_node"] = selected["node_id"]
    if selected_prompt:
        mapping["prompt_input"] = selected_prompt["input"]
    if selected_duration:
        mapping["duration_input"] = selected_duration["input"]
    if selected_scheduler:
        mapping["steps_input"] = selected_scheduler["input"]
    if selected_seed:
        mapping["seed_node"] = selected_seed["node_id"]
        mapping["seed_input"] = selected_seed["input"]
    if selected_output:
        mapping["output_node"] = selected_output["node_id"]
        if selected_output["input"]:
            mapping["output_input"] = selected_output["input"]
    if reference_confidence == "HIGH":
        mapping["reference_inputs"] = {
            f"ref_image_{index}": {"node": link["load_node"], "input": link["load_input"]}
            for index, link in enumerate(selected_references)
        }

    confidence = {
        "prompt": prompt_confidence,
        "duration": duration_confidence,
        "scheduler": scheduler_confidence,
        "references": reference_confidence,
        "seed": seed_confidence,
        "output": output_confidence,
    }
    required = ("prompt", "duration", "scheduler", "references", "output")
    checks = {
        "api_format": True,
        "required_targets": all(confidence[key] == "HIGH" for key in required),
        "reference_links_preserved": reference_confidence == "HIGH",
        "mapping_targets_literal_inputs": all(
            not isinstance(workflow[target["node"]]["inputs"][target["input"]], (list, dict))
            for target in mapping.get("reference_inputs", {}).values()
        ),
    }
    prompt_text = selected_prompt["value"] if selected_prompt else ""
    prompt_contract = {
        "has_duration_header": bool(re.search(r"(?im)^\s*duration\s*:", prompt_text)),
        "has_reference_declarations": bool(re.search(r"(?m)^\s*@image_[1-9]\s*:", prompt_text)),
        "compatible_with_v01_parser": False,
    }
    prompt_contract["compatible_with_v01_parser"] = (
        prompt_contract["has_duration_header"] and prompt_contract["has_reference_declarations"]
    )
    return {
        "node_count": len(workflow),
        "candidates": {
            "prompt": prompt,
            "duration": duration,
            "scheduler": scheduler,
            "references": reference_links,
            "seed": seed,
            "output": outputs,
        },
        "selected": {
            "prompt": selected_prompt,
            "duration": selected_duration,
            "scheduler": selected_scheduler,
            "references": selected_references,
            "seed": selected_seed,
            "output": selected_output,
        },
        "confidence": confidence,
        "checks": checks,
        "prompt_contract": prompt_contract,
        "mapping": mapping,
    }


def _display_value(value):
    if isinstance(value, str):
        return f"string ({len(value)} chars)"
    return json.dumps(value, ensure_ascii=False)


def write_analysis(result, target):
    lines = ["# Real Workflow Analysis", "", f"API format: {'PASS' if result['checks']['api_format'] else 'FAIL'}",
             f"Node count: {result['node_count']}", ""]
    for name in ("prompt", "duration", "scheduler", "seed", "output"):
        selected = result["selected"][name]
        lines.extend([f"## {name.title()}", ""])
        if selected:
            lines.extend([
                f"- Node ID: `{selected['node_id']}`",
                f"- Class: `{selected['class_type']}`",
                f"- Input: `{selected['input']}`",
                f"- Current value: {_display_value(selected['value'])}",
                f"- Reason: {selected['reason']}",
                f"- Confidence: {result['confidence'][name]}",
                f"- Manual confirmation: {'NO' if result['confidence'][name] == 'HIGH' else 'YES'}",
            ])
        else:
            lines.extend([
                f"- Status: {result['confidence'][name]}",
                "- Manual confirmation: YES",
            ])
        lines.append("")
    lines.extend(["## References", ""])
    for index, link in enumerate(result["selected"]["references"]):
        lines.append(
            f"- `ref_image_{index}` → LoadImage node `{link['load_node']}` input "
            f"`{link['load_input']}`; existing `{link['reference_input']}` link "
            f"`{link['link']}` remains unchanged."
        )
    lines.extend([
        "",
        f"Confidence: {result['confidence']['references']}",
        f"Manual confirmation: {'NO' if result['confidence']['references'] == 'HIGH' else 'YES'}",
        "",
        "## Prompt Contract",
        "",
        f"- V0.1 parser compatible: {'YES' if result['prompt_contract']['compatible_with_v01_parser'] else 'NO'}",
        "- Production Prompt has no duration header or @image declarations; deterministic metadata must come from the episode manifest.",
        "- The Prompt text is preserved byte-for-byte and is not rewritten to satisfy the worker.",
        "",
        "## Validation",
        "",
    ])
    for key, value in result["checks"].items():
        lines.append(f"- {key}: {'PASS' if value else 'FAIL'}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow", nargs="?", type=Path,
                        default=Path("private/production_h3_api.json"))
    args = parser.parse_args()
    workflow = json.loads(args.workflow.read_text(encoding="utf-8-sig"))
    result = inspect(workflow)
    output_dir = args.workflow.parent
    (output_dir / "workflow_inspection.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "mapping_candidate.yaml").write_text(
        yaml.safe_dump(
            {"workflow": {"template": "./production_h3_api.json", "mapping": result["mapping"]}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    write_analysis(result, output_dir / "REAL_WORKFLOW_ANALYSIS.md")
    print(f"API format: PASS ({result['node_count']} nodes)")
    for name, confidence in result["confidence"].items():
        selected = result["selected"][name]
        if name == "references":
            value = ", ".join(link["load_node"] for link in selected) or "-"
        else:
            value = selected["node_id"] if selected else "-"
        print(f"{name}: {value} [{confidence}]")
    print("validation:", "PASS" if all(result["checks"].values()) else "FAIL")


if __name__ == "__main__":
    main()
