"""Load and validate the single SteadyRoute routing-policy source."""

import json
import pathlib
import re


SCHEMA_VERSION = 1
MODES = {"shadow", "active"}
BUILTIN_CANDIDATES = {
    "DIRECT", "COMPATIBLE", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE",
}
REQUIRED_POLICY_FIELDS = {
    "id", "group_name", "discovery_group_name", "region", "include_pattern",
    "exclude_pattern", "exclude_types", "empty_fallback", "warmup_samples",
    "warmup_successes", "retire_after_seconds", "business_test_urls",
    "static_candidates",
}


class PolicyConfigError(ValueError):
    """The policy document cannot safely drive discovery."""


def _require_string(item, name):
    value = item.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PolicyConfigError("policy %s must be a non-empty string" % name)


def validate_policy_config(config):
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise PolicyConfigError("unsupported route policy schema")
    if config.get("mode") not in MODES:
        raise PolicyConfigError("mode must be shadow or active")
    activation = config.get("activation", {})
    if not isinstance(activation, dict):
        raise PolicyConfigError("activation must be an object")
    if config.get("mode") == "active":
        measured = activation.get("measured_rss_delta_mb")
        maximum = activation.get("max_rss_delta_mb", 1.0)
        if not activation.get("approved"):
            raise PolicyConfigError("active mode requires separate approval")
        if measured is None or float(measured) > min(1.0, float(maximum)):
            raise PolicyConfigError("active mode blocked by RSS activation budget")
    policies = config.get("policies")
    if not isinstance(policies, list) or not policies:
        raise PolicyConfigError("policies must be a non-empty array")
    identifiers = set()
    group_names = set()
    discovery_names = set()
    for item in policies:
        if not isinstance(item, dict) or not REQUIRED_POLICY_FIELDS.issubset(item):
            raise PolicyConfigError("policy is missing required fields")
        for name in ("id", "group_name", "discovery_group_name", "region"):
            _require_string(item, name)
        if item["id"] in identifiers or item["group_name"] in group_names:
            raise PolicyConfigError("duplicate policy id or active group")
        if item["discovery_group_name"] in discovery_names:
            raise PolicyConfigError("duplicate discovery group")
        identifiers.add(item["id"])
        group_names.add(item["group_name"])
        discovery_names.add(item["discovery_group_name"])
        if item["group_name"] == item["discovery_group_name"]:
            raise PolicyConfigError("active and discovery groups must differ")
        if item.get("empty_fallback") != "REJECT":
            raise PolicyConfigError("empty_fallback must be REJECT")
        if "direct" not in {str(value).lower() for value in item.get("exclude_types", [])}:
            raise PolicyConfigError("exclude_types must contain direct")
        for name in ("warmup_samples", "warmup_successes", "retire_after_seconds"):
            if not isinstance(item.get(name), int) or isinstance(item.get(name), bool) or item[name] <= 0:
                raise PolicyConfigError("%s must be a positive integer" % name)
        if item["warmup_samples"] < 10 or item["warmup_successes"] < 3:
            raise PolicyConfigError("warmup thresholds cannot weaken the safety floor")
        if item["retire_after_seconds"] < 86400:
            raise PolicyConfigError("retirement audit retention must be at least 24 hours")
        for name in ("include_pattern", "exclude_pattern"):
            _require_string(item, name)
            if "(?=" in item[name] or "(?!" in item[name] or "(?<" in item[name]:
                raise PolicyConfigError("patterns must remain RE2 compatible")
            try:
                re.compile(item[name])
            except re.error as error:
                raise PolicyConfigError("invalid %s: %s" % (name, error))
        if not isinstance(item.get("static_candidates"), list) or not item["static_candidates"]:
            raise PolicyConfigError("static_candidates must be a non-empty array")
        if any(name in BUILTIN_CANDIDATES for name in item["static_candidates"]):
            raise PolicyConfigError("built-in policies cannot be static candidates")
    additional = config.get("additional_groups", [])
    if not isinstance(additional, list) or any(not isinstance(item, dict) for item in additional):
        raise PolicyConfigError("additional_groups must be an array of objects")
    all_names = group_names | discovery_names
    for group in additional:
        _require_string(group, "name")
        if group["name"] in all_names:
            raise PolicyConfigError("duplicate generated group name")
        all_names.add(group["name"])
    return config


def load_policy_config(path):
    path = pathlib.Path(path)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PolicyConfigError("cannot read route policies: %s" % error)
    return validate_policy_config(config)


def name_matches(policy, name):
    if not isinstance(name, str) or name in BUILTIN_CANDIDATES:
        return False
    return bool(re.search(policy["include_pattern"], name)) and not bool(
        re.search(policy["exclude_pattern"], name)
    )


def discovery_group(policy):
    return {
        "name": policy["discovery_group_name"],
        "type": "select",
        "include-all-proxies": True,
        "filter": policy["include_pattern"],
        "exclude-filter": policy["exclude_pattern"],
        "exclude-type": "|".join(policy["exclude_types"]),
        "empty-fallback": policy["empty_fallback"],
        "interrupt-exist-connections": False,
    }


def active_group(policy, mode="shadow"):
    if mode == "active":
        group = discovery_group(policy)
        group["name"] = policy["group_name"]
        group.update(policy.get("group_options") or {})
        return group
    group = {
        "name": policy["group_name"],
        "type": "select",
        "interrupt-exist-connections": False,
        "proxies": list(policy["static_candidates"]),
    }
    group.update(policy.get("group_options") or {})
    return group


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    unsafe_initial = "-?:,[]{}#&*!|>'\"%@`"
    if (
        text and text[0] not in unsafe_initial and ":" not in text and " #" not in text
        and text.lower() not in {"true", "false", "null", "yes", "no", "on", "off"}
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def _yaml_mapping_lines(mapping, indent):
    lines = []
    prefix = " " * indent
    for key, value in mapping.items():
        if isinstance(value, list):
            if not value:
                lines.append("%s%s: []" % (prefix, key))
            else:
                lines.append("%s%s:" % (prefix, key))
                for item in value:
                    lines.append("%s  - %s" % (prefix, _yaml_scalar(item)))
        elif isinstance(value, dict):
            lines.append("%s%s:" % (prefix, key))
            lines.extend(_yaml_mapping_lines(value, indent + 2))
        else:
            lines.append("%s%s: %s" % (prefix, key, _yaml_scalar(value)))
    return lines


def render_enhancement_yaml(config):
    validate_policy_config(config)
    groups = []
    for policy in config["policies"]:
        groups.append(active_group(policy, config["mode"]))
        groups.append(discovery_group(policy))
    groups.extend(config.get("additional_groups", []))
    lines = [
        "# Generated by scripts/generate-groups.py from config/route-policies.json.",
        "# Do not edit this file directly.",
        "",
        "prepend:",
    ]
    for group in groups:
        first = True
        for line in _yaml_mapping_lines(group, 4):
            if first:
                lines.append("  -" + line[3:])
                first = False
            else:
                lines.append(line)
    lines.extend(["", "append: []", "", "delete: []", ""])
    return "\n".join(lines)


def render_staged_mihomo_config(config):
    """Render a standalone no-network config for the core's syntax validator."""
    validate_policy_config(config)
    groups = []
    referenced = []
    for policy in config["policies"]:
        groups.extend((active_group(policy, config["mode"]), discovery_group(policy)))
        referenced.extend(policy["static_candidates"])
    groups.extend(config.get("additional_groups", []))
    for group in config.get("additional_groups", []):
        referenced.extend(group.get("proxies", []))
    names = list(dict.fromkeys(referenced))
    lines = [
        "mode: rule", "log-level: silent", "allow-lan: false", "proxies:",
    ]
    for index, name in enumerate(names, 1):
        proxy = {
            "name": name, "type": "socks5", "server": "127.0.0.1",
            "port": 10000 + index,
        }
        first = True
        for line in _yaml_mapping_lines(proxy, 4):
            if first:
                lines.append("  -" + line[3:])
                first = False
            else:
                lines.append(line)
    lines.append("proxy-groups:")
    for group in groups:
        first = True
        for line in _yaml_mapping_lines(group, 4):
            if first:
                lines.append("  -" + line[3:])
                first = False
            else:
                lines.append(line)
    lines.extend(["rules:", "  - MATCH,REJECT", ""])
    return "\n".join(lines)
