"""Local service routing template. The reference subscription is never written."""
import json
import pathlib

import clash_profile
import netcoffee


def compile_rules(path, line, groups):
    if not line.get('enabled') or line.get('exit_policy') != 'locked' or not line.get('locked_node') or line.get('binding_missing'):
        raise clash_profile.ProfileError('请先核对原固定目标，再启用整套分流')
    try:
        data = json.loads(pathlib.Path(path).read_text())
        rules, mapping = data['rules'], data['mapping']
    except (OSError, ValueError, KeyError) as error:
        raise clash_profile.ProfileError('本机完整分流模板缺失或无效') from error
    target = line['group_name']
    result = [condition + ',' + target for condition in netcoffee.CONDITIONS]
    result += ['DOMAIN-SUFFIX,' + d + ',' + target for d in
               ('claude.ai','claude.com','anthropic.com','openai.com','chatgpt.com','oaistatic.com','oaiusercontent.com')]
    for rule in rules:
        parts = rule.split(',')
        index = -2 if parts[-1] == 'no-resolve' else -1
        original = parts[index]
        if original.lower() in ('claude','openai'):
            parts[index] = target
        elif original not in ('DIRECT','REJECT'):
            dest = mapping.get(original)
            if dest not in groups:
                raise clash_profile.ProfileError('模板对应的目标分组缺失：' + original)
            parts[index] = dest
        result.append(','.join(parts))
    if not rules or not rules[-1].startswith('MATCH,'):
        raise clash_profile.ProfileError('模板缺少最终兜底规则')
    return result


def prefix(text, previous=(), desired=()):
    lines = text.splitlines()
    at = clash_profile._find_key(lines, 'rules')
    if at is None:
        raise clash_profile.ProfileError('运行配置缺少 rules')
    indent, end = clash_profile._sequence(lines, at)
    spans = clash_profile._split_items(lines, at + 1, end, indent or 0)
    if previous:
        existing = [clash_profile._normal_rule(lines[first].strip()[2:]) for first, last in spans[:len(previous)]]
        if existing == previous:
            for first,last in reversed(spans[:len(previous)]):
                del lines[first:last]
        elif existing and existing[0] == previous[0]:
            raise clash_profile.ProfileError('模板规则顺序已改变，请先在 Clash 重载授权订阅，再重试')
    lines[at+1:at+1] = [' ' * (indent or 0) + '- ' + clash_profile.rule_item(rule) for rule in desired]
    return '\n'.join(lines) + '\n'
