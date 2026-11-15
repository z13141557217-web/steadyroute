"""Authorization, inventory, migration and evidence are independent of serving selection."""
import copy
import json
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
        self.router.POLICY_CONFIG['probe_nodes'] = [TARGET, BACKUP]
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
        self.assertTrue(exit_policy.status(self.line,self.data,True)['boundary_verified'])
        self.assertIsNone(exit_policy.status(self.line,self.data,True)['effective_mode'])
        self.assertEqual(exit_policy.status(self.line,self.data,True,False)['policy_status'],'probe_abnormal')
        self.data[GROUP]['all'] += [BACKUP]
        self.assertIsNone(exit_policy.status(self.line,self.data,True)['effective_mode'])

    def test_dashboard_reports_off_target_session_from_old_group(self):
        self.state.update(proxies_observed_at=10000, connections_observed_at=10000)
        self.data['other exit'] = {'type': 'Http'}
        applied = {'groups': [GROUP], 'rules': ['DOMAIN-SUFFIX,claude.ai,' + GROUP]}
        service = mock.Mock()
        service.applied.return_value = applied
        service.line_problems.return_value = ''
        connections = [dict(id='old-session', metadata={'host': 'claude.ai'},
                            chains=['other exit', 'legacy AI'], rule='RuleSet', rulePayload='claude')]
        with mock.patch.object(self.router, 'SETTINGS', service):
            snapshot = self.router.build_status_snapshots(self.state, self.data, connections, now=10000, memory_mb=20)
        group = next(g for g in snapshot['v1']['groups'] if g['name'] == GROUP)
        self.assertEqual(group['exit_policy']['inherited_connections'], 1)
        self.assertEqual(group['decision']['severity'], 'warning')
        self.assertIsNone(group['exit_policy']['effective_mode'])

    def test_cached_dashboard_ages_evidence_without_controller_reads_or_timestamp_renewal(self):
        self.state.update(proxies_observed_at=10000, connections_observed_at=10000)
        applied = {'groups': [GROUP], 'rules': ['DOMAIN-SUFFIX,claude.ai,' + GROUP]}
        service = mock.Mock()
        service.applied.return_value = applied
        service.line_problems.return_value = ''
        connections = [dict(metadata={'host': 'claude.ai'}, chains=[TARGET, GROUP])]
        with mock.patch.object(self.router, 'SETTINGS', service):
            snapshot = self.router.build_status_snapshots(self.state, self.data, connections, now=10000, memory_mb=20)
        self.router.RUNTIME.update(last_snapshots=snapshot, last_state=self.state)
        self.router.refresh_cached_snapshot(now=10020)
        self.assertEqual(snapshot['v1']['groups'][0]['exit_policy']['connections']['checked_at'], 10000)
        self.router.update_dashboard_cache(snapshot)
        with mock.patch.object(self.router.time, 'time', return_value=10051), \
                mock.patch.object(self.router, 'api_request') as api:
            for kind in ('legacy', 'v1'):
                payload = json.loads(self.router.read_dashboard_cache(kind))
                group = next(g for g in payload['groups'] if g['name'] == GROUP)
                self.assertEqual(group['exit_policy']['policy_status'], 'evidence_stale')
                self.assertEqual(group['exit_policy']['connections']['checked_at'], 10000)
                self.assertIsNone(group['exit_policy']['effective_mode'])
                self.assertEqual(group['decision_severity'] if kind == 'legacy' else group['decision']['severity'], 'warning')
            api.assert_not_called()
        self.assertEqual(snapshot['v1']['groups'][0]['exit_policy']['policy_status'], 'locked')

    def test_failed_connection_fetch_is_not_a_successful_empty_observation(self):
        def get(method, path, **kwargs):
            if path == '/proxies':
                return {'proxies': copy.deepcopy(self.data)}
            if path == '/connections':
                raise OSError('fixture unavailable')
            raise AssertionError(path)
        with mock.patch.object(self.router, 'load_state', return_value=copy.deepcopy(self.state)), \
                mock.patch.object(self.router, 'api_request', side_effect=get):
            plan = self.router.plan_cycle()
        self.assertIsNone(plan['connections_observed_at'])
        self.assertIsNone(plan['state']['connections_observed_at'])


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

    def test_snapshot_reports_old_group_claude_connection_without_writes(self):
        line = self.changes()['ai_line']
        config = self.service.config()
        config['ai_line'] = line
        self.config_path.write_text(json.dumps(config))
        applied = {'groups': [line['group_name']],
                   'rules': ['DOMAIN-SUFFIX,claude.ai,' + line['group_name']]}
        connections = [dict(id='old-session', metadata={'host': 'claude.ai'},
                            chains=['other exit', 'old AI group'], rule='RuleSet', rulePayload='claude')]
        original = self.service._get
        def get(path):
            return {'connections': connections} if path == '/connections' else original(path)
        proxies = self.clash.proxies()
        proxies['other exit'] = {'type': 'Http'}
        proxies[line['group_name']].update(all=[NODES[0]], now=NODES[0])
        with mock.patch.object(self.service, 'applied', return_value=applied), \
                mock.patch.object(self.service, 'proxies', return_value=proxies), \
                mock.patch.object(self.service, 'line_problems', return_value=''), \
                mock.patch.object(self.service, '_get', side_effect=get):
            policy = self.service.snapshot()['exit_policy']
        self.assertEqual(policy['inherited_connections'], 1)
        self.assertEqual(policy['policy_status'], 'inherited_sessions')
        self.assertIsNone(policy['effective_mode'])
        self.assertFalse(any(method != 'GET' for method, path in self.clash.calls))

    def test_settings_and_dashboard_share_connection_conclusion_and_failure_states(self):
        line = self.changes()['ai_line']
        config = self.service.config()
        config['ai_line'] = line
        self.config_path.write_text(json.dumps(config))
        proxies = self.clash.proxies()
        proxies[line['group_name']].update(all=[NODES[0]], now=NODES[0])
        applied = {'groups': [line['group_name']], 'rules': ['DOMAIN-SUFFIX,claude.ai,' + line['group_name']]}
        router = cycle_harness.load_router('evidence_consistency_router')
        router.POLICY_CONFIG.update(profile='auto_lock', ai_line=line)
        router.PROFILE = 'auto_lock'
        state = dict(nodes={}, groups={}, controller_connected=True,
                     proxies_observed_at=self.clock[0], connections_observed_at=self.clock[0])
        router.refresh_auto_lock(state, proxies, self.clock[0])
        original = self.service._get
        for sessions in ([], [dict(metadata={'host': 'claude.ai'}, chains=[NODES[0], line['group_name']])],
                         [dict(metadata={'host': 'claude.ai'}, chains=['DIRECT'])],
                         [dict(metadata={'host': 'claude.ai'}, chains=[])], None):
            with self.subTest(sessions=sessions):
                def get(path):
                    if path == '/connections':
                        if sessions is None:
                            raise OSError('fixture unavailable')
                        return {'connections': sessions}
                    return original(path)
                with mock.patch.object(self.service, 'applied', return_value=applied), \
                        mock.patch.object(self.service, 'proxies', return_value=proxies), \
                        mock.patch.object(self.service, 'line_problems', return_value=''), \
                        mock.patch.object(self.service, '_get', side_effect=get), \
                        mock.patch.object(router, 'SETTINGS', self.service):
                    settings_policy = self.service.snapshot()['exit_policy']
                    state['connections_observed_at'] = self.clock[0] if sessions is not None else None
                    dashboard = router.build_status_snapshots(state, proxies, sessions or [], now=self.clock[0], memory_mb=20)
                policy = next(g['exit_policy'] for g in dashboard['v1']['groups'] if g['name'] == line['group_name'])
                for key in ('policy_status', 'effective_mode', 'boundary_verified', 'summary', 'severity', 'connections'):
                    self.assertEqual(settings_policy[key], policy[key], key)
                if sessions is None or not sessions or any(not c['chains'] or c['chains']==['DIRECT'] for c in sessions):
                    self.assertNotEqual(policy['severity'], 'ok')


class ExistingConnectionTests(unittest.TestCase):
    def test_mixed_paths_zero_activity_failure_and_freshness_have_distinct_evidence(self):
        self.assertEqual(exit_policy.connection_snapshot({'connections': None}), [])
        self.assertEqual(exit_policy.connection_snapshot({'connections': []}), [])
        self.assertIsNone(exit_policy.connection_snapshot({}))
        self.assertIsNone(exit_policy.connection_snapshot({'connections': 'invalid'}))
        line = dict(enabled=True, group_name=GROUP, exit_policy='locked', locked_node=TARGET)
        proxies = {TARGET: {'type': 'Http'}, BACKUP: {'type': 'Http'}, 'Japan test exit': {'type': 'Http'},
                   GROUP: {'type': 'Selector', 'all': [TARGET], 'now': TARGET}}
        def session(chain):
            return dict(metadata={'host': 'claude.ai'}, chains=chain)
        def read(connections, **extra):
            params = dict(now=1000, checked_at=1000, connections_checked_at=1000)
            params.update(extra)
            return exit_policy.status(line, proxies, True, connections=connections,
                managed_rules=['DOMAIN-SUFFIX,claude.ai,' + GROUP], **params)
        cases = [
            ([session([TARGET, GROUP])], (1, 0, 0), 'locked'),
            ([session([BACKUP, 'legacy'])], (0, 1, 0), 'inherited_sessions'),
            ([session(['Japan test exit', 'legacy'])], (0, 1, 0), 'inherited_sessions'),
            ([session(['DIRECT'])], (0, 1, 0), 'inherited_sessions'),
            ([session([])], (0, 0, 1), 'path_unknown'),
            ([session([GROUP])], (0, 0, 1), 'path_unknown'),
            ([session(['unresolved exit'])], (0, 0, 1), 'path_unknown'),
            ([session([TARGET, GROUP]), session(['DIRECT']), session([]),
              dict(metadata={'host': 'ordinary.invalid'}, chains=['DIRECT'])], (1, 1, 1), 'inherited_sessions'),
            ([], (0, 0, 0), 'no_sessions'),
            (None, (0, 0, 0), 'connections_unavailable'),
        ]
        for connections, counts, expected in cases:
            with self.subTest(expected=expected, counts=counts):
                policy = read(connections)
                self.assertEqual(policy['policy_status'], expected)
                self.assertEqual(tuple(policy['connections'][k] for k in ('target', 'foreign', 'unknown')), counts)
                self.assertTrue(policy['boundary_verified'])
                if expected != 'locked':
                    self.assertIsNone(policy['effective_mode'])
                    self.assertNotEqual(policy['severity'], 'ok')
                else:
                    self.assertEqual(policy['effective_mode'], 'locked')
                self.assertIn('未识别连接不判违规', policy['coverage_note'])
        mixed = read(cases[7][0])
        self.assertEqual(mixed['connections']['unclassified'], 1)
        for params in ({'now': 1051}, {'connections_checked_at': 900}, {'checked_at': 900},
                       {'connections_checked_at': 1100}):
            with self.subTest(params=params):
                policy = read([session([TARGET, GROUP])], **params)
                self.assertEqual(policy['policy_status'], 'evidence_stale')
                self.assertIsNone(policy['effective_mode'])
                self.assertNotEqual(policy['severity'], 'ok')
        offline = exit_policy.status(line, None, True, connections=[session([TARGET, GROUP])], now=1000)
        self.assertEqual(offline['policy_status'], 'controller_offline')
        self.assertIsNone(offline['effective_mode'])

    def test_known_ai_without_chain_never_claims_a_verified_path(self):
        line = dict(enabled=True, group_name=GROUP, exit_policy='locked', locked_node=TARGET)
        proxies = {TARGET: {'type': 'Http'}, 'other exit': {'type': 'Http'}, GROUP: {'type': 'Selector', 'all': [TARGET], 'now': TARGET}}
        policy = exit_policy.status(line, proxies, True,
            connections=[dict(metadata={'host': 'claude.ai'})],
            managed_rules=['DOMAIN-SUFFIX,claude.ai,' + GROUP])
        self.assertEqual(policy['connections']['unknown'], 1)
        self.assertEqual(policy['connections']['foreign'], 0)
        self.assertIsNone(policy['effective_mode'])
        self.assertNotEqual(policy['severity'], 'ok')

    def test_rule_metadata_recognizes_legacy_groups_without_matching_unrelated_traffic(self):
        rules = ['DOMAIN-SUFFIX,claude.ai,' + GROUP, 'DOMAIN,exact.invalid,' + GROUP,
                 'DOMAIN-KEYWORD,datadog,' + GROUP, 'IP-CIDR,192.0.2.0/24,' + GROUP,
                 'IP-CIDR6,2001:db8::/32,' + GROUP, 'PROCESS-NAME,Claude,' + GROUP,
                 'GEOSITE,category-ai-!cn,' + GROUP, 'DOMAIN-SUFFIX,other.invalid,Other group']
        line = dict(enabled=True, group_name=GROUP, exit_policy='locked', locked_node=TARGET)
        proxies = {TARGET: {'type': 'Http'}, 'other exit': {'type': 'Http'}, GROUP: {'type': 'Selector', 'all': [TARGET], 'now': TARGET}}
        positive = [
            {'host': 'Claude.AI.'}, {'host': 'api.claude.ai'}, {'host': 'exact.invalid'},
            {'host': 'browser-datadog.invalid'}, {'destinationIP': '192.0.2.55'},
            {'destinationIP': '2001:db8::55'}, {'process': 'Claude'}, {'processPath': '/apps/Claude'},
        ]
        negative = [
            {}, {'host': 'notclaude.ai'}, {'host': 'claude.ai.attacker.invalid'},
            {'host': 'sub.exact.invalid'}, {'host': 'other.invalid'}, {'destinationIP': '192.0.3.55'},
            {'destinationIP': 'invalid'}, {'process': 'Claude Helper'},
        ]
        for metadata in positive + negative:
            with self.subTest(metadata=metadata):
                connection = dict(metadata=metadata, chains=['other exit', 'legacy group'])
                policy = exit_policy.status(line, proxies, True, connections=[connection], managed_rules=rules)
                self.assertEqual(policy['inherited_connections'], int(metadata in positive))
                connection['chains'] = [TARGET, 'legacy group']
                self.assertEqual(exit_policy.status(line, proxies, True, connections=[connection],
                                                   managed_rules=rules)['inherited_connections'], 0)
        recorded = dict(chains=['other exit'], rule='GeoSite', rulePayload='category-ai-!cn')
        self.assertTrue(exit_policy.managed_connection(recorded, GROUP, rules))
        recorded.update(rule='RuleSet', rulePayload='unknown-ai-provider')
        self.assertFalse(exit_policy.managed_connection(recorded, GROUP, rules))
