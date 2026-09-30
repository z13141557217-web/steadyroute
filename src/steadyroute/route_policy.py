"""Load and validate the single SteadyRoute routing-policy source."""

import json
import pathlib
import re

try:
    import regions
except ModuleNotFoundError:  # pragma: no cover - imported as a package in some tools
    from . import regions


SCHEMA_VERSION = 1
MODES = {"shadow", "active"}
PROFILES = {"fixed", "auto_lock"}
# Auto-lock policies are built at runtime from the user's own groups; their candidates are
# matched by country + residential detection instead of a hand-written pattern.
COUNTRY_RESIDENTIAL = "country_residential"
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


def _require_string_list(item, name):
    value = item.get(name)
    if (
        not isinstance(value, list) or not value
        or any(not isinstance(entry, str) or not entry.strip() for entry in value)
    ):
        raise PolicyConfigError("policy %s must be a non-empty string array" % name)


def validate_auto_lock_config(config):
    """Shared-install profile: no hand-written policies; runtime policies come from auto_lock."""
    settings = config.get("auto_lock", {})
    if not isinstance(settings, dict):
        raise PolicyConfigError("auto_lock must be an object")
    exclude = settings.get("exclude_groups", [])
    if not isinstance(exclude, list) or any(not isinstance(name, str) for name in exclude):
        raise PolicyConfigError("auto_lock.exclude_groups must be a string array")
    urls = settings.get("business_test_urls", [])
    if not isinstance(urls, list) or any(not isinstance(url, str) or not url.startswith("https://") for url in urls):
        raise PolicyConfigError("auto_lock.business_test_urls must be https URLs")
    per_group = settings.get("group_business_urls", {})
    if not isinstance(per_group, dict) or any(
            not isinstance(name, str) or not isinstance(value, list)
            or any(not isinstance(url, str) or not url.startswith("https://") for url in value)
            for name, value in per_group.items()):
        raise PolicyConfigError("auto_lock.group_business_urls must map group names to https URLs")
    line = config.get("ai_line", {})
    if not isinstance(line, dict) or not isinstance(line.get("enabled", False), bool):
        raise PolicyConfigError("ai_line must be an object with a boolean enabled")
    if line.get("enabled"):
        if not isinstance(line.get("group_name"), str) or not line["group_name"].strip():
            raise PolicyConfigError("ai_line.group_name is required when enabled")
        if line.get("country") not in regions.LABELS or line.get("country") == regions.OTHER_REGION[0]:
            raise PolicyConfigError("ai_line.country must be a known country")
    lines = config.get("managed_lines", [])
    if not isinstance(lines, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("group_name"), str)
            or item.get("country") not in regions.LABELS for item in lines):
        raise PolicyConfigError("managed_lines must list {group_name, country}")
    for key in ("legacy_group_names",):
        value = config.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise PolicyConfigError("%s must be a string array" % key)
    manual = (config.get("ai_rules") or {}).get("manual", [])
    if not isinstance(manual, list) or any(not isinstance(item, str) for item in manual):
        raise PolicyConfigError("ai_rules.manual must be a string array")
    policies = config.get("policies", [])
    if not isinstance(policies, list):
        raise PolicyConfigError("policies must be an array")
    for item in policies:
        if not isinstance(item, dict) or item.get("match") != COUNTRY_RESIDENTIAL:
            raise PolicyConfigError("auto_lock profile only accepts runtime country policies")
        if item.get("region") in (None, "", regions.OTHER_REGION[0]):
            raise PolicyConfigError("auto_lock policy needs a known country")
        outside = [name for name in item.get("static_candidates", []) if not name_matches(item, name)]
        if outside:
            raise PolicyConfigError("candidate outside its locked country: %s" % outside[0])
    return config


def validate_policy_config(config):
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise PolicyConfigError("unsupported route policy schema")
    if config.get("profile", "fixed") not in PROFILES:
        raise PolicyConfigError("profile must be fixed or auto_lock")
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
    if config.get("profile") == "auto_lock":
        return validate_auto_lock_config(config)
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
        for name in ("exclude_types", "business_test_urls", "static_candidates"):
            _require_string_list(item, name)
        if "direct" not in {value.lower() for value in item["exclude_types"]}:
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
        if any(name in BUILTIN_CANDIDATES for name in item["static_candidates"]):
            raise PolicyConfigError("built-in policies cannot be static candidates")
        outside = [name for name in item["static_candidates"] if not name_matches(item, name)]
        if outside:
            raise PolicyConfigError(
                "static candidates must match their own region and residential pattern: %s" % outside[0])
    for item in policies:
        for other in policies:
            if other is item:
                continue
            shared = [name for name in item["static_candidates"] if name_matches(other, name)]
            if shared:
                raise PolicyConfigError("candidate matches more than one region policy: %s" % shared[0])
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
    if policy.get("match") == COUNTRY_RESIDENTIAL:
        return (
            regions.region_of(name)[0] == policy["region"]
            and regions.is_residential(name)
            and not regions.is_notice(name)
        )
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
        "hidden": True,
        "interrupt-exist-connections": False,
    }


def active_group(policy, mode="shadow"):
    if mode == "active":
        group = discovery_group(policy)
        group["name"] = policy["group_name"]
        group["hidden"] = False
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
