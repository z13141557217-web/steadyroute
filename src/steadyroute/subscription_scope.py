"""Local exact-UID authorization; no subscription identity ships with the app."""
import json
import os
import pathlib
import clash_profile

SUMMARY = '当前订阅不在本机明确授权范围；稳航跳过写入、探测、维护和旧记录重放，不注入分组或 REJECT'


class ScopeError(clash_profile.ProfileError):
    pass


class Scope:
    def __init__(self, base, target):
        self.path = pathlib.Path(os.environ.get('STEADYROUTE_SUBSCRIPTION_AUTHORIZATION') or
                                 pathlib.Path(base) / 'subscription-authorization.json')
        self.target = target

    def authorized_uid(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            value = data.get('authorized_uid')
            if data.get('schema_version') == 1 and isinstance(value, str) and value.strip():
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def current_uid(self):
        try:
            return self.target.resolve()[0]
        except Exception:
            return None

    def allowed(self, expected=None):
        authorized, current = self.authorized_uid(), self.current_uid()
        return bool(authorized and current == authorized and (expected is None or current == expected))

    def require(self, expected=None):
        if not self.allowed(expected):
            raise ScopeError(SUMMARY)
        return self.authorized_uid()

    def snapshot(self):
        active = self.allowed()
        return dict(active=active, configured=bool(self.authorized_uid()),
                    summary='当前订阅已获本机授权；仅明确选择的节点可探测' if active else SUMMARY)
