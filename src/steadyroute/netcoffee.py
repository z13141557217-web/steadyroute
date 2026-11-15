"""Built-in diagnostic destinations and explicit per-subscription exit bindings.

Only condition text is bundled. Names, country and leaf identity come from an
explicit user selection, never from a screenshot or this module.
"""
import copy
import json

CONDITIONS = (
    'DOMAIN,2026.ip138.com',
    'DOMAIN,my.ip.cn',
    'AND,((IP-CIDR,1.1.1.1/32,no-resolve),(DST-PORT,443))',
    'DOMAIN,claude.ai',
    'DOMAIN,www.anthropic.com',
)
STOPPED_LIMIT = ('稳航仅处理本机精确授权的订阅；其他订阅跳过写入、探测、维护和旧记录重放，'
                 '不注入 AI/HK 分组或 REJECT。授权订阅的持久规则可在稳航停止后保留；'
                 'Clash 切换加载窗口和已有连接无法保证固定出口，不能视为始终生效。')


def source_names(target, uid):
    import clash_profile
    path = target.profile_dir / (uid + '.yaml')
    if not path.is_file() or path.is_symlink():
        raise clash_profile.ProfileError('当前订阅源无法核对；未绑定或导入')
    text = path.read_text(encoding='utf-8')
    try:
        rows = json.loads(text).get('proxies') or []
        return [row['name'] for row in rows]
    except (ValueError, KeyError, TypeError, AttributeError):
        lines = text.splitlines()
        at = clash_profile._find_key(lines, 'proxies')
        if at is None or lines[at].partition(':')[2].strip():
            raise clash_profile.ProfileError('当前订阅源格式无法核对；未绑定或导入')
        indent, end = clash_profile._sequence(lines, at)
        names = [clash_profile._item_name(lines[first]) for first, _ in
                 clash_profile._split_items(lines, at + 1, end, indent or 0)]
        if any(name is None for name in names):
            raise clash_profile.ProfileError('订阅源节点名称无法完整核对；未导入')
        return names


def validate(config):
    value = config.get('netcoffee', {})
    if not isinstance(value, dict) or not isinstance(value.get('enabled', False), bool):
        raise ValueError('netcoffee must have a boolean enabled')
    bindings = value.get('bindings', {})
    if not isinstance(bindings, dict):
        raise ValueError('netcoffee.bindings must be an object')
    for uid, row in bindings.items():
        if not isinstance(uid, str) or not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) or not row[key].strip()
                for key in ('locked_node', 'country', 'group_name')):
            raise ValueError('invalid subscription binding')
    nodes = config.get('probe_nodes', [])
    if not isinstance(nodes, list) or any(not isinstance(n, str) or not n.strip() for n in nodes):
        raise ValueError('probe_nodes must be an explicit string array')


def effective(config, uid):
    result = copy.deepcopy(config)
    result['_profile_uid'] = uid
    settings = config.get('netcoffee') or {}
    if not settings.get('enabled'):
        return result
    binding = (settings.get('bindings') or {}).get(uid)
    line = result.setdefault('ai_line', {})
    line.update(enabled=True, exit_policy='locked')
    line.pop('slow_exit', None)
    if binding:
        line.update(binding)
        line.pop('binding_missing', None)
        result['probe_nodes'] = [binding['locked_node']]
    else:
        # This marker is never a candidate; group_definition forces REJECT.
        line.update(locked_node='SteadyRoute unbound subscription', binding_missing=True)
        result['probe_nodes'] = []
    return result


def extra_rules(group, existing):
    """Reuse our existing Claude suffix rules; never add redundant exact rules."""
    additions = []
    for condition in CONDITIONS:
        full = condition + ',' + group
        covered = full in existing
        if condition.startswith('DOMAIN,'):
            host = condition.split(',', 1)[1]
            covered = covered or any(rule.startswith('DOMAIN-SUFFIX,') and
                rule.split(',')[2] == group and (host == rule.split(',')[1] or
                host.endswith('.' + rule.split(',')[1])) for rule in existing)
        if not covered:
            additions.append(full)
    return additions


def signature(line):
    parts = line.split(',')
    if parts[-1] == 'no-resolve':
        parts.pop()
    kind = parts[0].replace('-', '').lower().replace('ipcidr6', 'ipcidr')
    return (kind, payload_signature(kind, ','.join(parts[1:-1])), parts[-1])


def payload_signature(kind, payload):
    value = ''.join(str(payload).split())
    if kind == 'and':
        # Mihomo omits no-resolve and renders the AND separator as && in /rules.
        value = value.lower().replace(',no-resolve', '').replace('&&', ',')
        value = value.replace('ip-cidr', 'ipcidr').replace('dst-port', 'dstport')
    return value


def live_signature(row):
    kind = str(row.get('type', '')).replace('-', '').lower()
    return kind, payload_signature(kind, row.get('payload', '')), row.get('proxy')


def verify_rules(rules, expected):
    live = [live_signature(r) for r in rules if not (r.get('extra') or {}).get('disabled')]
    return [signature(rule) for rule in expected] == live[:len(expected)]


def snapshot(config, applied, uid=None):
    setting = config.get('netcoffee') or {}
    current = uid or config.get('_profile_uid')
    binding = (setting.get('bindings') or {}).get(current)
    return dict(enabled=bool(setting.get('enabled')), profile_uid=current,
                binding=binding, binding_missing=bool(setting.get('enabled') and not binding),
                conditions=list(CONDITIONS),
                rules_status='unverified',
                installed=bool(applied.get('profile_uid') == current and applied.get('netcoffee_rules')),
                stopped_limit=STOPPED_LIMIT,
                ip_consistency='unverified',
                ip_checks=[dict(label=label, destination=destination, status='unverified',
                                ip=None, checked_at=None, reason='未执行实际公网 IP 检测')
                           for label, destination in (('IP138', '2026.ip138.com'),
                           ('IP.cn', 'my.ip.cn'), ('Cloudflare', '1.1.1.1:443'))])
