"""Authorization, inventory, migration and evidence are independent of serving selection."""
import copy
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness
import candidate_registry
import exit_policy
from test_settings import ServiceBase, NODES

GROUP = 'AI test line'
TARGET = '台湾 家宽 target [a].+'
BACKUP = '台湾 家宽 backup'


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.router = cycle_harness.load_router('exit_policy_test_router')
        self.line = dict(enabled=True, group_name=GROUP, country='TW', exit_policy='locked', locked_node=TARGET)
        self.router.POLICY_CONFIG.update(profile='auto_lock', ai_line=self.line)
        self.router.PROFILE = 'auto_lock'
        self.data = {TARGET:{'type':'Http'}, BACKUP:{'type':'Http'},
                     GROUP:{'type':'Selector','now':TARGET,'all':[TARGET]}}
        self.state = dict(nodes={TARGET:cycle_harness.mature_node(100, 10000),
                                 BACKUP:cycle_harness.mature_node(10,10000)},groups={})
        self.router.refresh_auto_lock(self.state, self.data, 10000)

    def test_old_false_is_failover_not_locked(self):
        self.assertEqual(exit_policy.mode({'slow_exit':False}), 'failover_only')
        self.assertEqual(exit_policy.mode({}), 'adaptive')
        for value in ('invalid', None, True):
            with self.assertRaises(ValueError):
                exit_policy.validate({'exit_policy':value})
        with self.assertRaises(ValueError):
            exit_policy.validate({'exit_policy':'locked','locked_node':TARGET,'slow_exit':False})

    def test_every_automatic_selection_reason_is_blocked_at_final_boundary(self):
        with mock.patch.object(self.router, 'api_request') as api:
            for reason in ('automatic','removed','confirmed_failure','optimize','adopt','stability','slow_exit','resume','repair'):
                for target in (TARGET, BACKUP):
                    with self.subTest(reason=reason, target=target), self.assertRaises(self.router.RegionGuardError):
                        self.router.select_node(GROUP, target, False, reason=reason)
            api.assert_not_called()

    def test_full_and_fast_decision_path_preserves_sessions_even_when_failed_removed_or_drifted(self):
        with mock.patch.object(self.router,'select_node') as select, mock.patch.object(self.router,'api_request') as api:
            for current, failures in ((TARGET,0),(TARGET,1),(TARGET,100),(BACKUP,100),('REJECT',100),(None,100)):
                self.data[GROUP]['now'] = current
                self.state['nodes'][TARGET].update(failure_streak=failures,effective_failure_streak=failures,last_success=not failures)
                self.state['groups'][GROUP] = dict(manual_hold_until=1,better_streak=100,slow_window=[1]*30)
                self.router.evaluate_group(GROUP,[TARGET,BACKUP],self.data,[],self.state,False)
                self.assertEqual(self.router.close_old_connections(GROUP,TARGET,[dict(id='live',chains=[GROUP,TARGET])]),0)
            select.assert_not_called()
            api.assert_not_called()

    def test_inventory_keeps_backups_despite_single_member_serving_group_and_empty_set(self):
        policy = self.router.POLICY_BY_GROUP[GROUP]
        self.assertEqual(set(policy['static_candidates']),{TARGET,BACKUP})
        self.assertEqual(set(candidate_registry._safe_observation(policy,self.data)[0]),{TARGET,BACKUP})
        del self.data[TARGET]
        self.data[GROUP].update(now='REJECT',all=['REJECT'])
        self.router.refresh_auto_lock(self.state,self.data,10020)
        self.assertEqual(self.router.POLICY_BY_GROUP[GROUP]['locked_node'],TARGET)
        del self.data[BACKUP]
        self.router.refresh_auto_lock(self.state,self.data,10040)
        self.assertIn(GROUP,self.router.POLICY_BY_GROUP)
        self.assertEqual(candidate_registry._safe_observation(self.router.POLICY_BY_GROUP[GROUP],self.data),([], 'ready'))

    def test_failover_authorizes_only_confirmed_failure_or_removal(self):
        self.line['exit_policy'] = 'failover_only'
        with mock.patch.object(self.router,'api_request') as api:
            for reason in ('adopt','optimize','stability','automatic','slow_exit','recovery'):
                with self.assertRaises(self.router.RegionGuardError):
                    self.router.select_node(GROUP,BACKUP,False,reason=reason)
            for reason in ('confirmed_failure','removed'):
                self.router.select_node(GROUP,BACKUP,False,reason=reason)
            self.assertEqual(api.call_count,2)

    def test_status_never_claims_offline_or_unconstrained_group_is_effective(self):
        self.assertIsNone(exit_policy.status(self.line)['effective_mode'])
        self.assertIsNone(exit_policy.status(self.line,self.data,False)['effective_mode'])
        self.assertEqual(exit_policy.status(self.line,self.data,True)['effective_mode'],'locked')
        self.assertEqual(exit_policy.status(self.line,self.data,True,False)['policy_status'],'probe_abnormal')
        self.data[GROUP]['all'] += [BACKUP]
        self.assertIsNone(exit_policy.status(self.line,self.data,True)['effective_mode'])


class SettingsTests(ServiceBase):
    def changes(self):
        return {'ai_line':dict(enabled=True,country='TW',group_name='AI 台湾家宽线路',exit_policy='locked',locked_node=NODES[0])}

    def test_preview_is_exact_and_read_only_then_persists_single_source(self):
        before = self.files()
        plan = self.service.preview(self.changes())
        self.assertEqual(plan['lines'][0]['after'],[NODES[0]])
        self.assertTrue(plan['groups'][0]['filter'].startswith('\\A'))
        self.assertEqual(self.files(),before)
        original = self.clash.proxies
        def proxies():
            data = original()
            if '\\A' in self.clash.loaded.text:
                data['AI 台湾家宽线路'].update(now=NODES[0],all=[NODES[0]])
            return data
        self.clash.proxies = proxies
        self.service.apply(self.changes())
        config = self.service.config()
        self.assertEqual(config['ai_line']['exit_policy'],'locked')
        self.assertNotIn('slow_exit',config['ai_line'])
        self.assertEqual(self.service.config()['ai_line']['locked_node'],NODES[0])

    def test_different_current_choice_fails_before_any_write(self):
        changes=self.changes(); changes['ai_line']['locked_node']=NODES[1]
        before=self.files()
        with self.assertRaisesRegex(Exception,'当前选择与锁定目标不同'):
            self.service.apply(changes)
        self.assertEqual(before,self.files())
        self.assertFalse(any(method=='PUT' for method,path in self.clash.calls))

    def test_conflicting_legacy_switch_and_invalid_mode_rejected(self):
        changes=self.changes();changes['ai_line']['slow_exit']=False
        with self.assertRaises(Exception): self.service.merged(changes)
        changes=self.changes();changes['ai_line']['exit_policy']='unknown'
        with self.assertRaises(Exception): self.service.merged(changes)
