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

import charmhelpers
import heat_context
import json
from unittest.mock import patch
from test_utils import CharmTestCase

TO_PATCH = [
    'get_encryption_key',
    'generate_ec2_tokens',
    'config',
    'leader_get',
    'relation_get',
    'relation_ids',
    'related_units',
]


class TestHeatContext(CharmTestCase):

    def setUp(self):
        super(TestHeatContext, self).setUp(heat_context, TO_PATCH)

    def test_encryption_configuration(self):
        self.get_encryption_key.return_value = 'key'
        self.leader_get.return_value = 'password'
        self.assertEqual(
            heat_context.HeatSecurityContext()(),
            {'encryption_key': 'key',
             'heat_domain_admin_passwd': 'password'})
        self.leader_get.assert_called_with('heat-domain-admin-passwd')

    def test_instance_user_empty_configuration(self):
        self.config.return_value = None
        self.assertEqual(
            heat_context.InstanceUserContext()(),
            {'instance_user': ''})

    @patch('charmhelpers.contrib.openstack.'
           'context.IdentityServiceContext.__call__')
    def test_identity_configuration(self, __call__):
        __call__.return_value = {
            'service_port': 'port',
            'service_host': 'host',
            'auth_host': 'host',
            'auth_port': 'port',
            'admin_tenant_name': 'tenant',
            'admin_user': 'user',
            'admin_password': 'pass',
            'service_protocol': 'http',
            'auth_protocol': 'http'}
        self.generate_ec2_tokens.return_value = \
            'http://host:port/v2.0/ec2tokens'

        final_result = __call__.return_value
        final_result['keystone_ec2_url'] = \
            self.generate_ec2_tokens.return_value

        self.assertEqual(
            heat_context.HeatIdentityServiceContext()(), final_result)

    def test_quota_configuration_context(self):
        expected = {'max_stacks_per_tenant': '999'}
        self.config.side_effect = self.test_config.get
        self.test_config.set('max-stacks-per-tenant', '999')
        self.assertEqual(heat_context.QuotaConfigurationContext()(), expected)


class HeatPluginContextTest(CharmTestCase):

    def setUp(self):
        super(HeatPluginContextTest, self).setUp(heat_context, TO_PATCH)
        self.relation_get.side_effect = self.test_relation.get

    def tearDown(self):
        super(HeatPluginContextTest, self).tearDown()

    def test_init(self):
        heatp_ctxt = heat_context.HeatPluginContext()
        self.assertEqual(
            heatp_ctxt.interfaces,
            ['heat-plugin-subordinate']
        )
        self.assertEqual(heatp_ctxt.services, ['heat'])
        self.assertEqual(
            heatp_ctxt.config_file,
            '/etc/heat/heat.conf'
        )

    @patch.object(charmhelpers.contrib.openstack.context, 'log')
    @patch.object(charmhelpers.contrib.openstack.context, 'relation_get')
    @patch.object(charmhelpers.contrib.openstack.context, 'related_units')
    @patch.object(charmhelpers.contrib.openstack.context, 'relation_ids')
    def ctxt_check(self, rel_settings, expect, _rids, _runits, _rget, _log):
        self.test_relation.set(rel_settings)
        _runits.return_value = ['unit1']
        _rids.return_value = ['rid2']
        _rget.side_effect = self.test_relation.get
        self.relation_ids.return_value = ['rid2']
        self.related_units.return_value = ['unit1']
        heatp_ctxt = heat_context.HeatPluginContext()()
        self.assertEqual(heatp_ctxt, expect)

    def test_defaults(self):
        self.ctxt_check(
            {},
            {
                'plugin_dirs': '/usr/lib64/heat,/usr/lib/heat',
            }
        )

    def test_overrides(self):
        self.ctxt_check(
            {'plugin-dirs': '/usr/lib64/heat,/usr/lib/heat,/usr/local/lib'},
            {
                'plugin_dirs': '/usr/lib64/heat,/usr/lib/heat,/usr/local/lib',
            }
        )

    def test_subordinateconfig(self):
        principle_config = {
            "heat": {
                "/etc/heat/heat.conf": {
                    "sections": {
                        'DEFAULT': [
                            ('heatboost', True)
                        ],
                        'heatplugin': [
                            ('superkey', 'supervalue')
                        ],
                    }
                }
            }
        }
        self.ctxt_check(
            {
                'plugin-dirs': '/usr/lib64/heat,/usr/lib/heat,/usr/local/lib',
                'subordinate_configuration': json.dumps(principle_config)
            },
            {
                'plugin_dirs': '/usr/lib64/heat,/usr/lib/heat,/usr/local/lib',
                'sections': {u'DEFAULT': [[u'heatboost', True]],
                             u'heatplugin': [[u'superkey', u'supervalue']]},
            }
        )
