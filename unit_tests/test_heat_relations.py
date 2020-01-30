# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os.path
import sys

from mock import call, patch, MagicMock
from test_utils import CharmTestCase, patch_open

# python-apt is not installed as part of test-requirements but is imported by
# some charmhelpers modules so create a fake import.
mock_apt = MagicMock()
sys.modules['apt'] = mock_apt
mock_apt.apt_pkg = MagicMock()


import heat_utils as utils

_reg = utils.register_configs
_map = utils.restart_map

utils.register_configs = MagicMock()
utils.restart_map = MagicMock()

with patch('charmhelpers.contrib.hardening.harden.harden') as mock_dec:
    mock_dec.side_effect = (lambda *dargs, **dkwargs: lambda f:
                            lambda *args, **kwargs: f(*args, **kwargs))
    import heat_relations as relations

utils.register_configs = _reg
utils.restart_map = _map

TO_PATCH = [
    # charmhelpers.core.hookenv
    'Hooks',
    'config',
    'is_leader',
    'leader_set',
    'log',
    'open_port',
    'relation_set',
    'related_units',
    # charmhelpers.core.host
    'apt_install',
    'apt_update',
    'restart_on_change',
    'service_restart',
    # charmhelpers.contrib.openstack.utils
    'charm_dir',
    'configure_installation_source',
    'determine_packages',
    'openstack_upgrade_available',
    'os_release',
    'sync_db_with_multi_ipv6_addresses',
    # charmhelpers.contrib.openstack.ha.utils
    'generate_ha_relation_data',
    # charmhelpers.contrib.hahelpers.cluster_utils
    # heat_utils
    'restart_map',
    'register_configs',
    'do_openstack_upgrade',
    'remove_old_packages',
    'services',
    # other
    'execd_preinstall',
    'log',
    'migrate_database',
    'is_elected_leader',
    'relation_ids',
    'relation_get',
    'local_unit',
    'get_relation_ip',
    'is_db_maintenance_mode',
]


class HeatRelationTests(CharmTestCase):

    def setUp(self):
        super(HeatRelationTests, self).setUp(relations, TO_PATCH)
        self.config.side_effect = self.test_config.get
        self.charm_dir.return_value = '/var/lib/juju/charms/heat/charm'

    @patch.object(relations.policyd, 'maybe_do_policyd_overrides')
    def test_install_hook(self, mock_maybe_do_policyd_overrides):
        repo = 'cloud:precise-havana'
        self.determine_packages.return_value = [
            'python-keystoneclient', 'uuid', 'heat-api',
            'heat-api-cfn', 'heat-engine']
        self.test_config.set('openstack-origin', repo)
        relations.install()
        self.configure_installation_source.assert_called_with(repo)
        self.assertTrue(self.apt_update.called)
        self.apt_install.assert_called_with(['python-keystoneclient',
                                             'uuid', 'heat-api',
                                             'heat-api-cfn',
                                             'heat-engine'], fatal=True)
        self.assertTrue(self.execd_preinstall.called)

    @patch.object(relations, 'configure_https')
    @patch.object(relations.policyd,
                  'maybe_do_policyd_overrides_on_config_changed')
    def test_config_changed_no_upgrade(
        self,
        maybe_do_policyd_overrides_on_config_changed,
        mock_configure_https
    ):
        self.openstack_upgrade_available.return_value = False
        relations.config_changed()

    @patch.object(relations, 'configure_https')
    @patch.object(relations.policyd,
                  'maybe_do_policyd_overrides_on_config_changed')
    def test_config_changed_with_upgrade(
        self,
        maybe_do_policyd_overrides_on_config_changed,
        mock_configure_https
    ):
        self.openstack_upgrade_available.return_value = True
        relations.config_changed()
        self.assertTrue(self.do_openstack_upgrade.called)

    @patch.object(relations, 'configure_https')
    @patch.object(relations.policyd,
                  'maybe_do_policyd_overrides_on_config_changed')
    def test_config_changed_with_openstack_upgrade_action(
        self,
        maybe_do_policyd_overrides_on_config_changed,
        mock_configure_https
    ):
        self.openstack_upgrade_available.return_value = True
        self.test_config.set('action-managed-upgrade', True)

        relations.config_changed()

        self.assertFalse(self.do_openstack_upgrade.called)

    @patch('os.path.isfile')
    @patch('os.remove')
    @patch.object(relations, 'leader_elected')
    @patch.object(relations.policyd, 'maybe_do_policyd_overrides')
    def test_upgrade_charm(self,
                           mock_maybe_do_policyd_overrides,
                           leader_elected, os_remove, os_path_isfile):
        os_path_isfile.return_value = False
        self.is_leader.return_value = False
        self.remove_old_packages.return_value = True
        self.services.return_value = ['heat-api']

        relations.upgrade_charm()
        leader_elected.assert_called_once_with()
        os_path_isfile.assert_not_called()
        # now say we are the leader
        self.is_leader.return_value = True
        os_path_isfile.return_value = False

        relations.upgrade_charm()
        self.leader_set.assert_not_called()
        os_path_isfile.return_value = True
        with patch_open() as (mock_open, mock_file):
            mock_file.read.return_value = "abc"
            relations.upgrade_charm()
            filename = os.path.join(relations.HEAT_PATH, 'encryption-key')
            mock_open.assert_called_once_with(filename, 'r')
            self.leader_set.assert_called_once_with(
                {'heat-auth-encryption-key': 'abc'})
            os_remove.assert_called_once_with(filename)
        self.remove_old_packages.assert_called_with()
        self.service_restart.assert_called_with('heat-api')

    def test_db_joined(self):
        self.get_relation_ip.return_value = '192.168.20.1'
        relations.db_joined()
        self.relation_set.assert_called_with(heat_database='heat',
                                             heat_username='heat',
                                             heat_hostname='192.168.20.1')
        self.assertTrue(self.get_relation_ip.called)

    def _shared_db_test(self, configs):
        self.is_db_maintenance_mode.return_value = False
        self.relation_get.return_value = 'heat/0 heat/1'
        self.local_unit.return_value = 'heat/0'
        configs.complete_contexts = MagicMock()
        configs.complete_contexts.return_value = ['shared-db']
        configs.write = MagicMock()
        relations.db_changed()
        self.assertTrue(self.migrate_database.called)

    @patch.object(relations, 'CONFIGS')
    def test_db_changed(self, configs):
        self._shared_db_test(configs)
        self.assertEqual([call('/etc/heat/heat.conf')],
                         configs.write.call_args_list)

    @patch.object(relations, 'CONFIGS')
    def test_db_changed_missing_relation_data(self, configs):
        self.is_db_maintenance_mode.return_value = False
        configs.complete_contexts = MagicMock()
        configs.complete_contexts.return_value = []
        relations.db_changed()
        self.log.assert_called_with(
            'shared-db relation incomplete. Peer not ready?'
        )

    def test_amqp_joined(self):
        relations.amqp_joined()
        self.relation_set.assert_called_with(
            username='heat',
            vhost='openstack',
            relation_id=None)

    def test_amqp_joined_passes_relation_id(self):
        "Ensures relation_id correct passed to relation_set"
        relations.amqp_joined(relation_id='heat:1')
        self.relation_set.assert_called_with(username='heat',
                                             vhost='openstack',
                                             relation_id='heat:1')

    @patch.object(relations, 'CONFIGS')
    def test_amqp_changed_relation_data(self, configs):
        configs.complete_contexts = MagicMock()
        configs.complete_contexts.return_value = ['amqp']
        configs.write = MagicMock()
        relations.amqp_changed()
        self.assertEqual([call('/etc/heat/heat.conf')],
                         configs.write.call_args_list)

    @patch.object(relations, 'CONFIGS')
    def test_amqp_changed_missing_relation_data(self, configs):
        configs.complete_contexts = MagicMock()
        configs.complete_contexts.return_value = []
        relations.amqp_changed()
        self.assertTrue(self.log.called)

    @patch.object(relations, 'CONFIGS')
    def test_relation_broken(self, configs):
        relations.relation_broken()
        self.assertTrue(configs.write_all.called)

    @patch.object(relations, 'canonical_url')
    def test_identity_service_joined(self, _canonical_url):
        "It properly requests unclustered endpoint via identity-service"
        _canonical_url.return_value = 'http://heatnode1'
        relations.identity_joined()
        expected = {
            'heat_service': 'heat',
            'heat_region': 'RegionOne',
            'heat_public_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat_admin_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat_internal_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat-cfn_service': 'heat-cfn',
            'heat-cfn_region': 'RegionOne',
            'heat-cfn_public_url': 'http://heatnode1:8000/v1',
            'heat-cfn_admin_url': 'http://heatnode1:8000/v1',
            'heat-cfn_internal_url': 'http://heatnode1:8000/v1',
            'relation_id': None,
        }
        self.relation_set.assert_called_with(**expected)

    @patch.object(relations, 'canonical_url')
    def test_identity_service_joined_with_relation_id(self, _canonical_url):
        _canonical_url.return_value = 'http://heatnode1'
        relations.identity_joined(rid='identity-service:0')
        ex = {
            'heat_service': 'heat',
            'heat_region': 'RegionOne',
            'heat_public_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat_admin_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat_internal_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat-cfn_service': 'heat-cfn',
            'heat-cfn_region': 'RegionOne',
            'heat-cfn_public_url': 'http://heatnode1:8000/v1',
            'heat-cfn_admin_url': 'http://heatnode1:8000/v1',
            'heat-cfn_internal_url': 'http://heatnode1:8000/v1',
            'relation_id': 'identity-service:0',
        }
        self.relation_set.assert_called_with(**ex)

    @patch('charmhelpers.contrib.openstack.ip.unit_get',
           lambda *args: 'heatnode1')
    @patch('charmhelpers.contrib.openstack.ip.is_clustered',
           lambda *args: False)
    @patch('charmhelpers.contrib.openstack.ip.service_name')
    @patch('charmhelpers.contrib.openstack.ip.config')
    def test_identity_service_public_address_override(self, ip_config,
                                                      _service_name):
        ip_config.side_effect = self.test_config.get
        _service_name.return_value = 'heat'
        self.test_config.set('os-public-hostname', 'heat.example.org')
        relations.identity_joined(rid='identity-service:0')
        exp = {
            'heat_service': 'heat',
            'heat_region': 'RegionOne',
            'heat_public_url': 'http://heat.example.org:8004/v1/$(tenant_id)s',
            'heat_admin_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat_internal_url': 'http://heatnode1:8004/v1/$(tenant_id)s',
            'heat-cfn_service': 'heat-cfn',
            'heat-cfn_region': 'RegionOne',
            'heat-cfn_public_url': 'http://heat.example.org:8000/v1',
            'heat-cfn_admin_url': 'http://heatnode1:8000/v1',
            'heat-cfn_internal_url': 'http://heatnode1:8000/v1',
            'relation_id': 'identity-service:0',
        }
        self.relation_set.assert_called_with(**exp)

    @patch.object(relations, 'configure_https')
    @patch.object(relations, 'CONFIGS')
    def test_identity_changed(self, configs, mock_configure_https):
        configs.complete_contexts.return_value = ['identity-service']
        relations.identity_changed()
        self.assertTrue(configs.write_all.called)

    @patch.object(relations, 'CONFIGS')
    def test_identity_changed_incomplete(self, configs):
        configs.complete_contexts.return_value = []
        relations.identity_changed()
        self.assertTrue(self.log.called)
        self.assertFalse(configs.write.called)

    def test_db_joined_with_ipv6(self):
        'It properly requests access to a shared-db service'
        self.sync_db_with_multi_ipv6_addresses.return_value = MagicMock()
        self.test_config.set('prefer-ipv6', True)
        relations.db_joined()
        self.sync_db_with_multi_ipv6_addresses.assert_called_with(
            'heat', 'heat', relation_prefix='heat')

    @patch.object(relations, 'CONFIGS')
    def test_non_leader_db_changed(self, configs):
        self.is_elected_leader.return_value = False
        configs.complete_contexts.return_value = []
        self.relation_get.return_value = 'heat/0 heat/1'
        self.local_unit.return_value = 'heat/0'
        configs.complete_contexts = MagicMock()
        configs.complete_contexts.return_value = ['shared-db']
        configs.write = MagicMock()
        relations.db_changed()
        self.assertFalse(self.migrate_database.called)

    def test_ha_relation_joined(self):
        self.generate_ha_relation_data.return_value = {'rel_data': 'data'}
        relations.ha_joined(relation_id='rid:23')
        self.relation_set.assert_called_once_with(
            relation_id='rid:23', rel_data='data')
