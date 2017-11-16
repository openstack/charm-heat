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

from collections import OrderedDict
from mock import patch, MagicMock, call
from test_utils import CharmTestCase

from charmhelpers.core import hookenv

_conf = hookenv.config
hookenv.config = MagicMock()

import heat_utils as utils

hookenv.config = _conf

TO_PATCH = [
    'config',
    'log',
    'os_release',
    'get_os_codename_install_source',
    'configure_installation_source',
    'apt_install',
    'apt_update',
    'apt_upgrade',
    'check_call',
    'service_start',
    'service_stop',
    'token_cache_pkgs',
    'enable_memcache',
]


# Restart map should be constructed such that API services restart
# before frontends (haproxy/apaceh) to avoid port conflicts.
RESTART_MAP = OrderedDict([
    ('/etc/heat/heat.conf', ['heat-api', 'heat-api-cfn', 'heat-engine']),
    ('/etc/heat/api-paste.ini', ['heat-api', 'heat-api-cfn']),
    ('/etc/haproxy/haproxy.cfg', ['haproxy']),
    ('/etc/apache2/sites-available/openstack_https_frontend', ['apache2']),
    ('/etc/apache2/sites-available/openstack_https_frontend.conf',
     ['apache2']),
    ('/etc/memcached.conf', ['memcached']),
])


class HeatUtilsTests(CharmTestCase):

    def setUp(self):
        super(HeatUtilsTests, self).setUp(utils, TO_PATCH)
        self.config.side_effect = self.test_config.get

    @patch('charmhelpers.contrib.openstack.context.SubordinateConfigContext')
    def test_determine_packages(self, subcontext):
        self.os_release.return_value = 'havana'
        pkgs = utils.determine_packages()
        ex = list(set(utils.BASE_PACKAGES + utils.BASE_SERVICES))
        self.assertEqual(ex, pkgs)

    @patch('charmhelpers.contrib.openstack.context.SubordinateConfigContext')
    def test_determine_packages_newton(self, subcontext):
        self.os_release.return_value = 'newton'
        self.token_cache_pkgs.return_value = ['memcached']
        pkgs = utils.determine_packages()
        ex = list(set(utils.BASE_PACKAGES + ['memcached'] +
                      utils.BASE_SERVICES))
        self.assertEqual(sorted(ex), sorted(pkgs))

    def test_restart_map(self):
        self.assertEqual(RESTART_MAP, utils.restart_map())

    def test_openstack_upgrade(self):
        self.config.side_effect = None
        self.config.return_value = 'cloud:precise-havana'
        self.get_os_codename_install_source.return_value = 'havana'
        configs = MagicMock()
        utils.do_openstack_upgrade(configs)
        self.assertTrue(self.apt_update.called)
        self.assertTrue(self.apt_upgrade.called)
        self.assertTrue(self.apt_install.called)
        configs.set_release.assert_called_with(openstack_release='havana')
        self.assertTrue(configs.write_all.called)

    def test_api_ports(self):
        cfn = utils.api_port('heat-api-cfn')
        self.assertEqual(cfn, 8000)
        cfn = utils.api_port('heat-api')
        self.assertEqual(cfn, 8004)

    def test_migrate_database(self):
        utils.migrate_database()
        self.assertTrue(self.log.called)
        self.check_call.assert_called_with(['heat-manage', 'db_sync'])
        expected = [call('heat-api'), call('heat-api-cfn'),
                    call('heat-engine'), call('apache2')]
        self.service_stop.assert_has_calls(expected, any_order=True)
        self.service_start.assert_has_calls(expected, any_order=True)
