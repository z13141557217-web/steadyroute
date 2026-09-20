"""Dynamic candidate reconciliation and lifecycle management."""

import hashlib

import route_policy
import state_contract


def _policy_state(state, policy):
    registry = state.setdefault("candidate_registry", {"policies": {}})
    policies = registry.setdefault("policies", {})
    return policies.setdefault(policy["id"], {
        "policy_id": policy["id"],
        "group_name": policy["group_name"],
        "discovery_group_name": policy["discovery_group_name"],
        "region": policy["region"],
        "group_status": "unknown",
        "candidates": [],
        "previous_candidates": [],
        "proposed_candidates": [],
        "last_nonempty_candidates": [],
        "pending_signature": None,
        "pending_count": 0,
        "nodes": {},
    })


def _node_id(policy, name):
    return state_contract.node_ui_id(policy["group_name"], name)


def _group_id(policy):
    return state_contract.group_ui_id(policy["group_name"])


def _append_event(state, code, policy, now, name=None, old=None, new=None, reason=None, severity="info"):
    subject = _node_id(policy, name) if name is not None else _group_id(policy)
    event = {
        "code": code,
        "severity": severity,
        "scope": "node" if name is not None else "group",
        "subject_id": subject,
        "group_id": _group_id(policy),
        "node_id": _node_id(policy, name) if name is not None else None,
        "from_state": old,
        "to_state": new,
        "reason_code": reason or code.lower(),
        "occurred_at": int(now),
        "occurred_at_iso": state_contract.utc_iso(now),
    }
    events = list(state.get("events", []))
    events.append(event)
    state["events"] = events[-state_contract.EVENT_LIMIT:]


def _set_group_status(state, policy, policy_state, status, now):
    previous = policy_state.get("group_status", "unknown")
    policy_state["group_status"] = status
    if status in {"missing", "malformed"} and previous not in {"missing", "malformed"}:
        _append_event(state, "GROUP_MISSING", policy, now, old=previous, new=status,
                      reason="discovery_group_unavailable", severity="warning")
    elif status == "ready" and previous in {"missing", "malformed", "offline"}:
        _append_event(state, "GROUP_RECOVERED", policy, now, old=previous, new=status,
                      reason="discovery_group_recovered")


def _safe_observation(policy, proxy_data):
    group = proxy_data.get(policy["discovery_group_name"])
    if group is None:
        return None, "missing"
    if not isinstance(group, dict) or not isinstance(group.get("all"), list):
        return None, "malformed"
    observed = []
    seen = set()
    for name in group["all"]:
        if not isinstance(name, str) or name in seen:
            continue
        if name in route_policy.BUILTIN_CANDIDATES:
            continue
        if not route_policy.name_matches(policy, name):
            continue
        seen.add(name)
        observed.append(name)
    return observed, "ready"


def _change(code, policy, name, old, new, now):
    return {
        "code": code,
        "node_id": _node_id(policy, name),
        "group_id": _group_id(policy),
        "from_state": old,
        "to_state": new,
        "occurred_at": int(now),
    }


def _apply_candidate_set(state, policy, policy_state, observed, now, current=None):
    previous = list(policy_state.get("candidates", []))
    if previous == observed:
        return False, []
    added = [name for name in observed if name not in previous]
    removed = [name for name in previous if name not in observed]
    nodes = policy_state.setdefault("nodes", {})
    changes = []
    rename_from = removed[0] if len(added) == 1 and len(removed) == 1 else None
    for name in removed:
        item = nodes.setdefault(name, {})
        old = item.get("lifecycle", "healthy")
        item.update({"lifecycle": "retired", "retired_at": int(now), "removed_at": int(now)})
        changes.append(_change("candidate_removed", policy, name, old, "retired", now))
        _append_event(state, "NODE_REMOVED", policy, now, name, old, "retired", "subscription_removed")
        _append_event(state, "NODE_RETIRED", policy, now, name, old, "retired", "audit_retention_started")
        if name == current:
            _append_event(state, "CURRENT_NODE_REMOVED", policy, now, name, old, "retired",
                          "selected_node_not_in_confirmed_candidates", severity="critical")
    for name in added:
        item = {
            "lifecycle": "discovered",
            "discovered_at": int(now),
            "retired_at": None,
            "suspected_rename_from": rename_from,
        }
        nodes[name] = item
        changes.append(_change("candidate_added", policy, name, None, "discovered", now))
        _append_event(state, "NODE_DISCOVERED", policy, now, name, None, "discovered", "subscription_added")
    policy_state["previous_candidates"] = previous
    policy_state["candidates"] = list(observed)
    if observed:
        policy_state["last_nonempty_candidates"] = list(observed)
    policy_state["added"] = list(added)
    policy_state["removed"] = list(removed)
    policy_state["last_changed_at"] = int(now)
    if not observed:
        _append_event(state, "NO_CANDIDATE", policy, now, old="ready", new="no_candidate",
                      reason="confirmed_empty_candidate_set", severity="critical")
    elif not previous:
        _append_event(state, "CANDIDATES_RECOVERED", policy, now, old="no_candidate", new="ready",
                      reason="candidate_set_available")
    return True, changes


def reconcile(config, state, proxy_data, now, controller_ok=True):
    """Reconcile one successful controller snapshot; invalid snapshots are non-destructive."""
    route_policy.validate_policy_config(config)
    registry = state.setdefault("candidate_registry", {"policies": {}})
    registry["mode"] = config["mode"]
    changed = False
    all_changes = []
    for policy in config["policies"]:
        policy_state = _policy_state(state, policy)
        if not controller_ok:
            policy_state["group_status"] = "offline"
            policy_state["pending_signature"] = None
            policy_state["pending_count"] = 0
            continue
        observed, status = _safe_observation(policy, proxy_data)
        _set_group_status(state, policy, policy_state, status, now)
        if observed is None:
            policy_state["pending_signature"] = None
            policy_state["pending_count"] = 0
            continue
        policy_state["last_observed_at"] = int(now)
        policy_state["proposed_candidates"] = list(observed)
        if observed == policy_state.get("candidates", []):
            policy_state["pending_signature"] = None
            policy_state["pending_count"] = 0
            continue
        signature = hashlib.sha256("\0".join(observed).encode("utf-8")).hexdigest()
        if signature == policy_state.get("pending_signature"):
            policy_state["pending_count"] = int(policy_state.get("pending_count", 0)) + 1
        else:
            policy_state["pending_signature"] = signature
            policy_state["pending_count"] = 1
        if policy_state["pending_count"] < 2:
            continue
        current = (proxy_data.get(policy["group_name"]) or {}).get("now")
        applied, changes = _apply_candidate_set(
            state, policy, policy_state, observed, now, current=current
        )
        changed = changed or applied
        all_changes.extend(changes)
        policy_state["pending_signature"] = None
        policy_state["pending_count"] = 0
    subscription = state.setdefault("subscription", {})
    if changed:
        subscription["generation"] = int(subscription.get("generation", 0)) + 1
        subscription["last_refresh_at"] = int(now)
        subscription["changes"] = all_changes
        subscription["added_count"] = sum(1 for item in all_changes if item["code"] == "candidate_added")
        subscription["removed_count"] = sum(1 for item in all_changes if item["code"] == "candidate_removed")
        _append_event(
            state, "SUBSCRIPTION_CHANGED", config["policies"][0], now,
            old=None, new=str(subscription["generation"]), reason="confirmed_candidate_change",
        )
    subscription["candidate_count"] = sum(
        len(_policy_state(state, policy).get("candidates", [])) for policy in config["policies"]
    )
    return {
        "generation": int(subscription.get("generation", 0)),
        "changed": changed,
        "changes": all_changes,
    }


def _desired_lifecycle(policy, health, now):
    quarantine_until = health.get("quarantine_until")
    if quarantine_until is not None and int(quarantine_until) > int(now):
        return "quarantined"
    if quarantine_until and int(health.get("quarantine_recovery_streak", 0)) < 3:
        return "half_open"
    samples = int(health.get("samples", 0))
    if samples < int(policy["warmup_samples"]):
        return "warming"
    if (
        bool(health.get("last_success"))
        and int(health.get("success_streak", 0)) >= int(policy["warmup_successes"])
        and bool(health.get("business_successes", 0) or health.get("business_last_success"))
    ):
        return "healthy"
    return "degraded" if int(health.get("failure_streak", 0)) else "warming"


def refresh_lifecycles(config, state, now):
    for policy in config["policies"]:
        policy_state = _policy_state(state, policy)
        for name in policy_state.get("candidates", []):
            item = policy_state.setdefault("nodes", {}).setdefault(name, {"lifecycle": "discovered"})
            old = item.get("lifecycle", "discovered")
            if old == "retired":
                continue
            new = _desired_lifecycle(policy, state.get("nodes", {}).get(name, {}), now)
            if new == old:
                continue
            item["lifecycle"] = new
            item["lifecycle_changed_at"] = int(now)
            if new == "warming":
                code = "NODE_WARMUP_STARTED"
            elif new == "healthy" and old in {"discovered", "warming"}:
                code = "NODE_WARMUP_COMPLETED"
            else:
                code = "NODE_%s" % new.upper()
            _append_event(state, code, policy, now, name, old, new, "%s_criteria" % new)


def purge_retired(config, state, now):
    for policy in config["policies"]:
        policy_state = _policy_state(state, policy)
        nodes = policy_state.setdefault("nodes", {})
        for name, item in list(nodes.items()):
            retired_at = item.get("retired_at")
            if item.get("lifecycle") != "retired" or retired_at is None:
                continue
            if int(now) - int(retired_at) < int(policy["retire_after_seconds"]):
                continue
            del nodes[name]
            _append_event(state, "NODE_RETIREMENT_PURGED", policy, now, name, "retired", None,
                          "retirement_retention_elapsed")


def healthy_candidates(policy, state):
    policy_state = _policy_state(state, policy)
    nodes = policy_state.get("nodes", {})
    return [name for name in policy_state.get("candidates", []) if nodes.get(name, {}).get("lifecycle") == "healthy"]


def routing_candidates(config, policy, state):
    if config["mode"] == "shadow":
        return list(policy["static_candidates"])
    return healthy_candidates(policy, state)


def warmup_probe_targets(config, state, per_policy=1):
    targets = []
    for policy in config["policies"]:
        policy_state = _policy_state(state, policy)
        nodes = policy_state.get("nodes", {})
        warming = [
            name for name in policy_state.get("candidates", [])
            if nodes.get(name, {}).get("lifecycle") in {"discovered", "warming", "half_open"}
        ]
        if not warming:
            continue
        cursor = int(policy_state.get("warmup_probe_cursor", 0))
        for offset in range(min(int(per_policy), len(warming))):
            targets.append((policy, warming[(cursor + offset) % len(warming)]))
        policy_state["warmup_probe_cursor"] = (cursor + int(per_policy)) % len(warming)
    return targets


def current_node_plan(policy, state, current):
    policy_state = _policy_state(state, policy)
    if current in policy_state.get("candidates", []):
        return {"status": "current_present", "target": None, "execute": False}
    candidates = healthy_candidates(policy, state)
    target = None
    if candidates:
        target = min(candidates, key=lambda name: float(state.get("nodes", {}).get(name, {}).get("score", 1000000)))
    return {
        "status": "current_removed" if target else "no_candidate",
        "target": target,
        "execute": bool(config_mode_active(state) and target),
    }


def config_mode_active(state):
    return state.get("candidate_registry", {}).get("mode") == "active"


def public_snapshot(config, state, proxy_data, now):
    policies = []
    for policy in config["policies"]:
        policy_state = _policy_state(state, policy)
        current = (proxy_data.get(policy["group_name"]) or {}).get("now")
        if current is None:
            current = state.get("groups", {}).get(policy["group_name"], {}).get("last_seen")
        candidates = list(policy_state.get("candidates", []))
        proposed = list(policy_state.get("proposed_candidates", []))
        static = list(policy["static_candidates"])
        plan = current_node_plan(policy, state, current) if current else {
            "status": "unknown", "target": None, "execute": False,
        }
        nodes = []
        for name, item in policy_state.get("nodes", {}).items():
            health = state.get("nodes", {}).get(name, {})
            retired_at = item.get("retired_at")
            nodes.append({
                "id": _node_id(policy, name),
                "name": name,
                "lifecycle": item.get("lifecycle", "discovered"),
                "samples": int(health.get("samples", 0)),
                "samples_required": int(policy["warmup_samples"]),
                "success_streak": int(health.get("success_streak", 0)),
                "successes_required": int(policy["warmup_successes"]),
                "business_probe_succeeded": bool(
                    health.get("business_successes", 0) or health.get("business_last_success")
                ),
                "warmup_progress": min(
                    1.0, float(health.get("samples", 0)) / float(policy["warmup_samples"])
                ),
                "retirement_remaining_seconds": (
                    max(0, int(policy["retire_after_seconds"]) - (int(now) - int(retired_at)))
                    if retired_at is not None else None
                ),
                "suspected_rename": bool(item.get("suspected_rename_from")),
            })
        policies.append({
            "id": policy["id"],
            "group_name": policy["group_name"],
            "discovery_group_name": policy["discovery_group_name"],
            "region": policy["region"],
            "scenario_type": "real_shadow_snapshot",
            "group_status": policy_state.get("group_status", "unknown"),
            "previous_candidate_count": len(policy_state.get("previous_candidates", [])),
            "current_candidate_count": len(candidates),
            "proposed_candidate_count": len(proposed),
            "differences": {
                "static_only": [name for name in static if name not in proposed],
                "dynamic_only": [name for name in proposed if name not in static],
                "added": list(policy_state.get("added", [])),
                "removed": list(policy_state.get("removed", [])),
            },
            "current_node": current,
            "current_node_exists": current in candidates if current is not None else None,
            "current_node_plan": plan,
            "fail_closed": not candidates or plan["status"] == "no_candidate",
            "nodes": nodes,
        })
    return {
        "mode": config["mode"],
        "generation": int(state.get("subscription", {}).get("generation", 0)),
        "read_only": True,
        "policies": policies,
    }
