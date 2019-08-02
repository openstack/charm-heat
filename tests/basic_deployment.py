#!/usr/bin/env python
#
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

"""
Basic heat functional test.
"""
import os
import json
import subprocess

import amulet
from heatclient.common import template_utils

from charmhelpers.contrib.openstack.amulet.deployment import (
    OpenStackAmuletDeployment
)

from charmhelpers.contrib.openstack.amulet.utils import (
    OpenStackAmuletUtils,
    DEBUG,
    # ERROR,
)
from novaclient import client as nova_client

# Use DEBUG to turn on debug logging
u = OpenStackAmuletUtils(DEBUG)

# Resource and name constants
IMAGE_NAME = 'cirros-image-1'
KEYPAIR_NAME = 'testkey'
STACK_NAME = 'hello_world'
RESOURCE_TYPE = 'server'
TEMPLATES_PATH = 'tests/files'


class HeatBasicDeployment(OpenStackAmuletDeployment):
    """Amulet tests on a basic heat deployment."""

    def __init__(self, series=None, openstack=None, source=None,
                 stable=False):
        """Deploy the entire test environment."""
        super(HeatBasicDeployment, self).__init__(series, openstack,
                                                  source, stable)
        self._add_services()
        self._add_relations()
        self._configure_services()
        self._deploy()

        u.log.info('Waiting on extended status checks...')
        exclude_services = []
        self._auto_wait_for_status(exclude_services=exclude_services)

        self.d.sentry.wait()
        self._initialize_tests()

    def _add_services(self):
        """Add services

           Add the services that we're testing, where heat is local,
           and the rest of the service are from lp branches that are
           compatible with the local charm (e.g. stable or next).
           """
        this_service = {'name': 'heat',
                        'constraints': {'mem': '2G'},
                        'units': 2}
        other_services = [
            {'name': 'keystone'},
            {'name': 'rabbitmq-server'},
            self.get_percona_service_entry(),
            {'name': 'glance'},
            {'name': 'nova-cloud-controller'},
            {'name': 'nova-compute'}
        ]
        if self._get_openstack_release() >= self.xenial_ocata:
            other_services.extend([
                {'name': 'neutron-gateway'},
                {'name': 'neutron-api'},
                {'name': 'neutron-openvswitch'},
            ])
        super(HeatBasicDeployment, self)._add_services(this_service,
                                                       other_services)

    def _add_relations(self):
        """Add all of the relations for the services."""

        relations = {
            'heat:amqp': 'rabbitmq-server:amqp',
            'heat:identity-service': 'keystone:identity-service',
            'heat:shared-db': 'percona-cluster:shared-db',
            'nova-compute:image-service': 'glance:image-service',
            'nova-compute:amqp': 'rabbitmq-server:amqp',
            'nova-cloud-controller:shared-db': 'percona-cluster:shared-db',
            'nova-cloud-controller:identity-service':
            'keystone:identity-service',
            'nova-cloud-controller:amqp': 'rabbitmq-server:amqp',
            'nova-cloud-controller:cloud-compute':
            'nova-compute:cloud-compute',
            'nova-cloud-controller:image-service': 'glance:image-service',
            'keystone:shared-db': 'percona-cluster:shared-db',
            'glance:identity-service': 'keystone:identity-service',
            'glance:shared-db': 'percona-cluster:shared-db',
            'glance:amqp': 'rabbitmq-server:amqp'
        }
        if self._get_openstack_release() >= self.xenial_ocata:
            relations.update({
                'neutron-gateway:amqp': 'rabbitmq-server:amqp',
                'nova-cloud-controller:quantum-network-service':
                'neutron-gateway:quantum-network-service',
                'neutron-api:shared-db': 'percona-cluster:shared-db',
                'neutron-api:amqp': 'rabbitmq-server:amqp',
                'neutron-api:neutron-api': 'nova-cloud-controller:neutron-api',
                'neutron-api:identity-service': 'keystone:identity-service',
                'nova-compute:neutron-plugin': 'neutron-openvswitch:'
                                               'neutron-plugin',
                'rabbitmq-server:amqp': 'neutron-openvswitch:amqp',
            })
        super(HeatBasicDeployment, self)._add_relations(relations)

    def _configure_services(self):
        """Configure all of the services."""
        nova_config = {'config-flags': 'auto_assign_floating_ip=False',
                       'enable-live-migration': 'False'}
        keystone_config = {'admin-password': 'openstack',
                           'admin-token': 'ubuntutesting'}
        pxc_config = {
            'dataset-size': '25%',
            'max-connections': 1000,
            'root-password': 'ChangeMe123',
            'sst-password': 'ChangeMe123',
        }

        heat_config = {
            'debug': True,
            'verbose': True,
        }

        nova_cc_config = {
            'api-rate-limit-rules': "( POST, '*', .*, 9999, MINUTE );",
        }
        if self._get_openstack_release() >= self.xenial_ocata:
            nova_cc_config['network-manager'] = 'Neutron'

        configs = {
            'nova-cloud-controller': nova_cc_config,
            'nova-compute': nova_config,
            'keystone': keystone_config,
            'percona-cluster': pxc_config,
            'heat': heat_config,
        }
        super(HeatBasicDeployment, self)._configure_services(configs)

    def _initialize_tests(self):
        """Perform final initialization before tests get run."""
        # Access the sentries for inspecting service units
        self.heat_sentry = self.d.sentry['heat'][0]
        self.pxc_sentry = self.d.sentry['percona-cluster'][0]
        self.keystone_sentry = self.d.sentry['keystone'][0]
        self.rabbitmq_sentry = self.d.sentry['rabbitmq-server'][0]
        self.nova_compute_sentry = self.d.sentry['nova-compute'][0]
        self.glance_sentry = self.d.sentry['glance'][0]
        u.log.debug('openstack release val: {}'.format(
            self._get_openstack_release()))
        u.log.debug('openstack release str: {}'.format(
            self._get_openstack_release_string()))

        # Authenticate admin with keystone
        self.keystone_session, self.keystone = u.get_default_keystone_session(
            self.keystone_sentry,
            openstack_release=self._get_openstack_release())

        force_v1_client = False
        if self._get_openstack_release() == self.trusty_icehouse:
            # Updating image properties (such as arch or hypervisor) using the
            # v2 api in icehouse results in:
            # https://bugs.launchpad.net/python-glanceclient/+bug/1371559
            u.log.debug('Forcing glance to use v1 api')
            force_v1_client = True

        # Authenticate admin with glance endpoint
        self.glance = u.authenticate_glance_admin(
            self.keystone,
            force_v1_client=force_v1_client)

        # Authenticate admin with nova endpoint
        self.nova = nova_client.Client(2, session=self.keystone_session)

        u.create_flavor(nova=self.nova,
                        name='m1.tiny', ram=512, vcpus=1, disk=1)

        self.heat = u.authenticate_heat_admin(self.keystone)

        # Action is REQUIRED to run for a functioning heat deployment
        if self._get_openstack_release() >= self.trusty_kilo:
            u.log.debug('Running domain-setup action on heat unit...')
            u.wait_on_action(u.run_action(self.heat_sentry, 'domain-setup'))

    def _image_create(self):
        """Create an image to be used by the heat template, verify it exists"""
        u.log.debug('Creating glance image ({})...'.format(IMAGE_NAME))

        # Create a new image
        image_new = u.create_cirros_image(self.glance, IMAGE_NAME)

        # Confirm image is created and has status of 'active'
        if not image_new:
            message = 'glance image create failed'
            amulet.raise_status(amulet.FAIL, msg=message)

        # Verify new image name
        images_list = list(self.glance.images.list())
        if images_list[0].name != IMAGE_NAME:
            message = ('glance image create failed or unexpected '
                       'image name {}'.format(images_list[0].name))
            amulet.raise_status(amulet.FAIL, msg=message)

    def _keypair_create(self):
        """Create a keypair to be used by the heat template,
           or get a keypair if it exists."""
        self.keypair = u.create_or_get_keypair(self.nova,
                                               keypair_name=KEYPAIR_NAME)
        if not self.keypair:
            msg = 'Failed to create or get keypair.'
            amulet.raise_status(amulet.FAIL, msg=msg)
        u.log.debug("Keypair: {} {}".format(self.keypair.id,
                                            self.keypair.fingerprint))

    def _stack_create(self):
        """Create a heat stack from a basic heat template, verify its status"""
        u.log.debug('Creating heat stack...')

        t_name = 'hot_hello_world.yaml'
        if self._get_openstack_release() < self.xenial_queens:
            t_url = u.file_to_url(os.path.join(TEMPLATES_PATH, 'icehouse',
                                               t_name))
        else:
            t_url = u.file_to_url(os.path.join(TEMPLATES_PATH, 'queens',
                                               t_name))

        r_req = self.heat.http_client
        u.log.debug('template url: {}'.format(t_url))

        t_files, template = template_utils.get_template_contents(t_url, r_req)
        env_files, env = template_utils.process_environment_and_files(
            env_path=None)

        fields = {
            'stack_name': STACK_NAME,
            'timeout_mins': '15',
            'disable_rollback': False,
            'parameters': {
                'admin_pass': 'Ubuntu',
                'key_name': KEYPAIR_NAME,
                'image': IMAGE_NAME
            },
            'template': template,
            'files': dict(list(t_files.items()) + list(env_files.items())),
            'environment': env
        }

        # Create the stack.
        try:
            _stack = self.heat.stacks.create(**fields)
            u.log.debug('Stack data: {}'.format(_stack))
            _stack_id = _stack['stack']['id']
            u.log.debug('Creating new stack, ID: {}'.format(_stack_id))
        except Exception as e:
            # Generally, an api or cloud config error if this is hit.
            msg = 'Failed to create heat stack: {}'.format(e)
            amulet.raise_status(amulet.FAIL, msg=msg)

        # Confirm stack reaches COMPLETE status.
        # /!\ Heat stacks reach a COMPLETE status even when nova cannot
        # find resources (a valid hypervisor) to fit the instance, in
        # which case the heat stack self-deletes!  Confirm anyway...
        ret = u.resource_reaches_status(self.heat.stacks, _stack_id,
                                        expected_stat="COMPLETE",
                                        msg="Stack status wait")
        _stacks = list(self.heat.stacks.list())
        u.log.debug('All stacks: {}'.format(_stacks))
        if not ret:
            msg = 'Heat stack failed to reach expected state.'
            amulet.raise_status(amulet.FAIL, msg=msg)

        # Confirm stack still exists.
        try:
            _stack = self.heat.stacks.get(STACK_NAME)
        except Exception as e:
            # Generally, a resource availability issue if this is hit.
            msg = 'Failed to get heat stack: {}'.format(e)
            amulet.raise_status(amulet.FAIL, msg=msg)

        # Confirm stack name.
        u.log.debug('Expected, actual stack name: {}, '
                    '{}'.format(STACK_NAME, _stack.stack_name))
        if STACK_NAME != _stack.stack_name:
            msg = 'Stack name mismatch, {} != {}'.format(STACK_NAME,
                                                         _stack.stack_name)
            amulet.raise_status(amulet.FAIL, msg=msg)

    def _stack_resource_compute(self):
        """Confirm that the stack has created a subsequent nova
           compute resource, and confirm its status."""
        u.log.debug('Confirming heat stack resource status...')

        # Confirm existence of a heat-generated nova compute resource.
        _resource = self.heat.resources.get(STACK_NAME, RESOURCE_TYPE)
        _server_id = _resource.physical_resource_id
        if _server_id:
            u.log.debug('Heat template spawned nova instance, '
                        'ID: {}'.format(_server_id))
        else:
            msg = 'Stack failed to spawn a nova compute resource (instance).'
            amulet.raise_status(amulet.FAIL, msg=msg)

        # Confirm nova instance reaches ACTIVE status.
        ret = u.resource_reaches_status(self.nova.servers, _server_id,
                                        expected_stat="ACTIVE",
                                        msg="nova instance")
        if not ret:
            msg = 'Nova compute instance failed to reach expected state.'
            amulet.raise_status(amulet.FAIL, msg=msg)

    def _stack_delete(self):
        """Delete a heat stack, verify."""
        u.log.debug('Deleting heat stack...')
        u.delete_resource(self.heat.stacks, STACK_NAME, msg="heat stack")

    def _image_delete(self):
        """Delete that image."""
        u.log.debug('Deleting glance image...')
        image = self.nova.glance.find_image(IMAGE_NAME)
        u.delete_resource(self.glance.images, image.id, msg="glance image")

    def _keypair_delete(self):
        """Delete that keypair."""
        u.log.debug('Deleting keypair...')
        u.delete_resource(self.nova.keypairs, KEYPAIR_NAME, msg="nova keypair")

    def test_100_services(self):
        """Verify the expected services are running on the corresponding
           service units."""
        service_names = {
            self.heat_sentry: ['heat-api',
                               'heat-api-cfn',
                               'heat-engine'],
        }

        ret = u.validate_services_by_name(service_names)
        if ret:
            amulet.raise_status(amulet.FAIL, msg=ret)

    def test_110_service_catalog(self):
        """Expect certain endpoints and endpoint data to be
        present in the Keystone service catalog"""
        u.log.debug('Checking service catalog endpoint data...')
        ep_validate = {'adminURL': u.valid_url,
                       'region': 'RegionOne',
                       'publicURL': u.valid_url,
                       'internalURL': u.valid_url,
                       'id': u.not_null}
        expected = {'compute': [ep_validate], 'orchestration': [ep_validate],
                    'image': [ep_validate], 'identity': [ep_validate]}

        actual = self.keystone.service_catalog.get_endpoints()
        ret = u.validate_svc_catalog_endpoint_data(
            expected,
            actual,
            openstack_release=self._get_openstack_release())
        if ret:
            amulet.raise_status(amulet.FAIL, msg=ret)

    def test_120_heat_endpoint(self):
        """Verify the heat api endpoint data."""
        u.log.debug('Checking api endpoint data...')
        endpoints = self.keystone.endpoints.list()

        if self._get_openstack_release() < self.trusty_kilo:
            # Before Kilo
            admin_port = internal_port = public_port = '3333'
        else:
            # Kilo and later
            admin_port = internal_port = public_port = '8004'

        expected = {'id': u.not_null,
                    'region': 'RegionOne',
                    'adminurl': u.valid_url,
                    'internalurl': u.valid_url,
                    'publicurl': u.valid_url,
                    'service_id': u.not_null}

        ret = u.validate_endpoint_data(
            endpoints,
            admin_port,
            internal_port,
            public_port,
            expected,
            openstack_release=self._get_openstack_release())
        if ret:
            message = 'heat endpoint: {}'.format(ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_130_memcache(self):
        u.validate_memcache(self.heat_sentry,
                            '/etc/heat/heat.conf',
                            self._get_openstack_release(),
                            earliest_release=self.trusty_mitaka)

    def test_200_heat_mysql_shared_db_relation(self):
        """Verify the heat:mysql shared-db relation data"""
        u.log.debug('Checking heat:mysql shared-db relation data...')
        unit = self.heat_sentry
        relation = ['shared-db', 'percona-cluster:shared-db']
        expected = {
            'private-address': u.valid_ip,
            'heat_database': 'heat',
            'heat_username': 'heat',
            'heat_hostname': u.valid_ip
        }

        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('heat:mysql shared-db', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_201_mysql_heat_shared_db_relation(self):
        """Verify the mysql:heat shared-db relation data"""
        u.log.debug('Checking mysql:heat shared-db relation data...')
        unit = self.pxc_sentry
        relation = ['shared-db', 'heat:shared-db']
        expected = {
            'private-address': u.valid_ip,
            'db_host': u.valid_ip,
            'heat_allowed_units': u.not_null,
            'heat_password': u.not_null
        }

        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('percona-cluster:heat shared-db', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_202_heat_keystone_identity_relation(self):
        """Verify the heat:keystone identity-service relation data"""
        u.log.debug('Checking heat:keystone identity-service relation data...')
        unit = self.heat_sentry
        relation = ['identity-service', 'keystone:identity-service']
        expected = {
            'heat_service': 'heat',
            'heat_region': 'RegionOne',
            'heat_public_url': u.valid_url,
            'heat_admin_url': u.valid_url,
            'heat_internal_url': u.valid_url,
            'heat-cfn_service': 'heat-cfn',
            'heat-cfn_region': 'RegionOne',
            'heat-cfn_public_url': u.valid_url,
            'heat-cfn_admin_url': u.valid_url,
            'heat-cfn_internal_url': u.valid_url
        }
        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('heat:keystone identity-service', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_203_keystone_heat_identity_relation(self):
        """Verify the keystone:heat identity-service relation data"""
        u.log.debug('Checking keystone:heat identity-service relation data...')
        unit = self.keystone_sentry
        relation = ['identity-service', 'heat:identity-service']
        expected = {
            'service_protocol': 'http',
            'service_tenant': 'services',
            'admin_token': 'ubuntutesting',
            'service_password': u.not_null,
            'service_port': '5000',
            'auth_port': '35357',
            'auth_protocol': 'http',
            'private-address': u.valid_ip,
            'auth_host': u.valid_ip,
            'service_username': 'heat_heat-cfn',
            'service_tenant_id': u.not_null,
            'service_host': u.valid_ip
        }
        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('keystone:heat identity-service', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_204_heat_rmq_amqp_relation(self):
        """Verify the heat:rabbitmq-server amqp relation data"""
        u.log.debug('Checking heat:rabbitmq-server amqp relation data...')
        unit = self.heat_sentry
        relation = ['amqp', 'rabbitmq-server:amqp']
        expected = {
            'username': u.not_null,
            'private-address': u.valid_ip,
            'vhost': 'openstack'
        }

        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('heat:rabbitmq-server amqp', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_205_rmq_heat_amqp_relation(self):
        """Verify the rabbitmq-server:heat amqp relation data"""
        u.log.debug('Checking rabbitmq-server:heat amqp relation data...')
        unit = self.rabbitmq_sentry
        relation = ['amqp', 'heat:amqp']
        expected = {
            'private-address': u.valid_ip,
            'password': u.not_null,
            'hostname': u.valid_ip,
        }

        ret = u.validate_relation_data(unit, relation, expected)
        if ret:
            message = u.relation_error('rabbitmq-server:heat amqp', ret)
            amulet.raise_status(amulet.FAIL, msg=message)

    def test_300_heat_config(self):
        """Verify the data in the heat config file."""
        u.log.debug('Checking heat config file data...')
        unit = self.heat_sentry
        conf = '/etc/heat/heat.conf'

        ks_rel = self.keystone_sentry.relation('identity-service',
                                               'heat:identity-service')
        rmq_rel = self.rabbitmq_sentry.relation('amqp',
                                                'heat:amqp')
        mysql_rel = self.pxc_sentry.relation('shared-db',
                                             'heat:shared-db')

        u.log.debug('keystone:heat relation: {}'.format(ks_rel))
        u.log.debug('rabbitmq:heat relation: {}'.format(rmq_rel))
        u.log.debug('percona-cluster:heat relation: {}'.format(mysql_rel))

        if self._get_openstack_release() < self.xenial_queens:
            dialect = 'mysql'
        else:
            dialect = 'mysql+pymysql'
        db_uri = "{}://{}:{}@{}/{}".format(dialect,
                                           'heat',
                                           mysql_rel['heat_password'],
                                           mysql_rel['db_host'],
                                           'heat')

        expected = {
            'DEFAULT': {
                'use_syslog': 'False',
                'debug': 'True',
                'verbose': 'True',
                'log_dir': '/var/log/heat',
                'instance_driver': 'heat.engine.nova',
                'plugin_dirs': '/usr/lib64/heat,/usr/lib/heat',
                'environment_dir': '/etc/heat/environment.d',
                'host': 'heat',
            },
            'database': {
                'connection': db_uri
            },
            'heat_api': {
                'bind_port': '7994'
            },
            'heat_api_cfn': {
                'bind_port': '7990'
            },
            'paste_deploy': {
                'api_paste_config': '/etc/heat/api-paste.ini'
            },
        }

        for section, pairs in expected.items():
            ret = u.validate_config_data(unit, conf, section, pairs)
            if ret:
                message = "heat config error: {}".format(ret)
                amulet.raise_status(amulet.FAIL, msg=message)

    def test_400_heat_resource_types_list(self):
        """Check default heat resource list behavior, also confirm
           heat functionality."""
        u.log.debug('Checking default heat resource list...')
        try:
            types = list(self.heat.resource_types.list())
            if type(types) is list:
                u.log.debug('Resource type list check is ok.')
            else:
                msg = 'Resource type list is not a list!'
                u.log.error('{}'.format(msg))
                raise
            if len(types) > 0:
                u.log.debug('Resource type list is populated '
                            '({}, ok).'.format(len(types)))
            else:
                msg = 'Resource type list length is zero!'
                u.log.error(msg)
                raise
        except:
            msg = 'Resource type list failed.'
            u.log.error(msg)
            raise

    def test_402_heat_stack_list(self):
        """Check default heat stack list behavior, also confirm
           heat functionality."""
        u.log.debug('Checking default heat stack list...')
        try:
            stacks = list(self.heat.stacks.list())
            if type(stacks) is list:
                u.log.debug("Stack list check is ok.")
            else:
                msg = 'Stack list returned something other than a list.'
                u.log.error(msg)
                raise
        except:
            msg = 'Heat stack list failed.'
            u.log.error(msg)
            raise

    def test_410_heat_stack_create_delete(self):
        """Create a heat stack from template, confirm that a corresponding
           nova compute resource is spawned, delete stack."""
        u.log.debug('Creating, deleting heat stack (compute)...')
        self._image_create()
        self._keypair_create()
        self._stack_create()
        self._stack_resource_compute()
        self._stack_delete()
        self._image_delete()
        self._keypair_delete()

    def test_500_auth_encryption_key_same_on_units(self):
        """Test that the auth_encryption_key in heat.conf is the same on all of
        the units.
        """
        u.log.debug("Checking the 'auth_encryption_key' is the same on "
                    "all units.")
        output, ret = self._run_arbitrary(
            "--application heat "
            "--format json "
            "grep auth_encryption_key /etc/heat/heat.conf")
        if ret:
            msg = "juju run returned error: ({}) -> {}".format(ret, output)
            amulet.raise_status(amulet.FAIL, msg=msg)
        output = json.loads(output)
        keys = {}
        for r in output:
            k = r['Stdout'].split('=')[1].strip()
            keys[r['UnitId']] = k
        # see if keys are different.
        ks = keys.values()
        if any(((k != ks[0]) for k in ks[1:])):
            msg = ("'auth_encryption_key' is not identical on every unit: {}"
                   .format("{}={}".format(k, v) for k, v in keys.items()))
            amulet.raise_status(amulet.FAIL, msg=msg)

    @staticmethod
    def _run_arbitrary(command, timeout=300):
        """Run an arbitrary command (as root), but not necessarily on a unit.

        (Otherwise the self.run(...) command could have been used for the unit

        :param str command: The command to run.
        :param int timeout: Seconds to wait before timing out.
        :return: A 2-tuple containing the output of the command and the exit
            code of the command.
        """
        cmd = ['juju', 'run', '--timeout', "{}s".format(timeout),
               ] + command.split()
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        output = stdout if p.returncode == 0 else stderr
        return output.decode('utf8').strip(), p.returncode

    def test_900_heat_restart_on_config_change(self):
        """Verify that the specified services are restarted when the config
           is changed."""
        sentry = self.heat_sentry
        juju_service = 'heat'

        # Expected default and alternate values
        set_default = {'use-syslog': 'False'}
        set_alternate = {'use-syslog': 'True'}

        # Config file affected by juju set config change
        conf_file = '/etc/heat/heat.conf'

        # Services which are expected to restart upon config change
        services = {
            'heat-api': conf_file,
            'heat-api-cfn': conf_file,
            'heat-engine': conf_file
        }

        # Make config change, check for service restarts
        u.log.debug('Making config change on {}...'.format(juju_service))
        mtime = u.get_sentry_time(sentry)
        self.d.configure(juju_service, set_alternate)

        sleep_time = 30
        for s, conf_file in services.items():
            u.log.debug("Checking that service restarted: {}".format(s))
            if not u.validate_service_config_changed(sentry, mtime, s,
                                                     conf_file,
                                                     sleep_time=sleep_time):
                self.d.configure(juju_service, set_default)
                msg = "service {} didn't restart after config change".format(s)
                amulet.raise_status(amulet.FAIL, msg=msg)
            sleep_time = 0

        self.d.configure(juju_service, set_default)

    def test_901_pause_resume(self):
        """Test pause and resume actions."""
        u.log.debug('Checking pause and resume actions...')
        unit = self.d.sentry['heat'][0]
        unit_name = unit.info['unit_name']

        u.log.debug('Checking for active status on {}'.format(unit_name))
        assert u.status_get(unit)[0] == "active"

        u.log.debug('Running pause action on {}'.format(unit_name))
        action_id = u.run_action(unit, "pause")
        u.log.debug('Waiting on action {}'.format(action_id))
        assert u.wait_on_action(action_id), "Pause action failed."
        u.log.debug('Checking for maintenance status on {}'.format(unit_name))
        assert u.status_get(unit)[0] == "maintenance"

        u.log.debug('Running resume action on {}'.format(unit_name))
        action_id = u.run_action(unit, "resume")
        u.log.debug('Waiting on action {}'.format(action_id))
        assert u.wait_on_action(action_id), "Resume action failed."
        u.log.debug('Checking for active status on {}'.format(unit_name))
        assert u.status_get(unit)[0] == "active"
        u.log.debug('OK')
