#!/usr/bin/python
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

import os
import shutil
import subprocess
import sys

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    charm_dir,
    log,
    relation_ids,
    relation_get,
    relation_set,
    related_units,
    local_unit,
    open_port,
    status_set,
    leader_get,
    leader_set,
    is_leader,
    WARNING,
)

from charmhelpers.core.host import (
    restart_on_change,
    service_reload,
    pwgen,
)

from charmhelpers.fetch import (
    apt_install,
    apt_update
)

from charmhelpers.contrib.hahelpers.cluster import (
    is_elected_leader,
    get_hacluster_config,
)

from charmhelpers.contrib.network.ip import (
    get_iface_for_address,
    get_netmask_for_address,
    is_ipv6,
    get_relation_ip,
)

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    openstack_upgrade_available,
    set_os_workload_status,
    sync_db_with_multi_ipv6_addresses,
    os_application_version_set,
)

from charmhelpers.contrib.openstack.ha.utils import (
    update_dns_ha_resource_params,
)

from charmhelpers.contrib.openstack.ip import (
    canonical_url,
    ADMIN,
    INTERNAL,
    PUBLIC,
)

from heat_utils import (
    do_openstack_upgrade,
    restart_map,
    determine_packages,
    migrate_database,
    register_configs,
    CLUSTER_RES,
    HEAT_CONF,
    REQUIRED_INTERFACES,
    setup_ipv6,
    VERSION_PACKAGE,
)

from heat_context import (
    API_PORTS,
    HEAT_PATH,
)

from charmhelpers.contrib.openstack.context import ADDRESS_TYPES
from charmhelpers.payload.execd import execd_preinstall
from charmhelpers.contrib.hardening.harden import harden

hooks = Hooks()
CONFIGS = register_configs()


@hooks.hook('install.real')
@harden()
def install():
    status_set('maintenance', 'Executing pre-install')
    execd_preinstall()
    configure_installation_source(config('openstack-origin'))
    status_set('maintenance', 'Installing apt packages')
    apt_update()
    apt_install(determine_packages(), fatal=True)

    _files = os.path.join(charm_dir(), 'files')
    if os.path.isdir(_files):
        for f in os.listdir(_files):
            f = os.path.join(_files, f)
            log('Installing %s to /usr/bin' % f)
            shutil.copy2(f, '/usr/bin')

    for port in API_PORTS.values():
        open_port(port)


@hooks.hook('config-changed')
@restart_on_change(restart_map())
@harden()
def config_changed():
    if not config('action-managed-upgrade'):
        if openstack_upgrade_available('heat-common'):
            status_set('maintenance', 'Running openstack upgrade')
            do_openstack_upgrade(CONFIGS)

    if config('prefer-ipv6'):
        status_set('maintenance', 'configuring ipv6')
        setup_ipv6()
        sync_db_with_multi_ipv6_addresses(config('database'),
                                          config('database-user'),
                                          relation_prefix='heat')

    CONFIGS.write_all()
    configure_https()

    for rid in relation_ids('cluster'):
        cluster_joined(relation_id=rid)
    for r_id in relation_ids('ha'):
        ha_joined(relation_id=r_id)


@hooks.hook('upgrade-charm')
@harden()
def upgrade_charm():
    if is_leader():
        # if we are upgrading, then the old version might have used the
        # HEAT_PATH/encryption-key. So we grab the key from that, and put it in
        # leader settings to ensure that the key remains the same during an
        # upgrade.
        encryption_path = os.path.join(HEAT_PATH, 'encryption-key')
        if os.path.isfile(encryption_path):
            with open(encryption_path, 'r') as f:
                encryption_key = f.read()
            try:
                leader_set({'heat-auth-encryption-key': encryption_key})
            except subprocess.CalledProcessError as e:
                log("upgrade: leader_set: heat-auth-encryption-key failed,"
                    " didn't delete the existing file: {}.\n"
                    "Error was: ".format(encryption_path, str(e)),
                    level=WARNING)
            else:
                # now we just delete the file
                os.remove(encryption_path)
    leader_elected()


@hooks.hook('amqp-relation-joined')
def amqp_joined(relation_id=None):
    relation_set(relation_id=relation_id,
                 username=config('rabbit-user'), vhost=config('rabbit-vhost'))


@hooks.hook('amqp-relation-changed')
@restart_on_change(restart_map())
def amqp_changed():
    if 'amqp' not in CONFIGS.complete_contexts():
        log('amqp relation incomplete. Peer not ready?')
        return
    CONFIGS.write(HEAT_CONF)


@hooks.hook('shared-db-relation-joined')
def db_joined():
    if config('prefer-ipv6'):
        sync_db_with_multi_ipv6_addresses(config('database'),
                                          config('database-user'),
                                          relation_prefix='heat')
    else:
        # Avoid churn check for access-network early
        access_network = None
        for unit in related_units():
            access_network = relation_get(unit=unit,
                                          attribute='access-network')
            if access_network:
                break
        host = get_relation_ip('shared-db', cidr_network=access_network)

        relation_set(heat_database=config('database'),
                     heat_username=config('database-user'),
                     heat_hostname=host)


@hooks.hook('shared-db-relation-changed')
@restart_on_change(restart_map())
def db_changed():
    if 'shared-db' not in CONFIGS.complete_contexts():
        log('shared-db relation incomplete. Peer not ready?')
        return
    CONFIGS.write(HEAT_CONF)

    if is_elected_leader(CLUSTER_RES):
        allowed_units = relation_get('heat_allowed_units')
        if allowed_units and local_unit() in allowed_units.split():
            log('Cluster leader, performing db sync')
            migrate_database()
        else:
            log('allowed_units either not presented, or local unit '
                'not in acl list: %s' % repr(allowed_units))


def configure_https():
    """Enables SSL API Apache config if appropriate."""
    # need to write all to ensure changes to the entire request pipeline
    # propagate (c-api, haprxy, apache)
    CONFIGS.write_all()
    if 'https' in CONFIGS.complete_contexts():
        cmd = ['a2ensite', 'openstack_https_frontend']
        subprocess.check_call(cmd)
    else:
        cmd = ['a2dissite', 'openstack_https_frontend']
        subprocess.check_call(cmd)

    # TODO: improve this by checking if local CN certs are available
    # first then checking reload status (see LP #1433114).
    service_reload('apache2', restart_on_failure=True)

    for rid in relation_ids('identity-service'):
        identity_joined(rid=rid)


@hooks.hook('identity-service-relation-joined')
def identity_joined(rid=None):
    public_url_base = canonical_url(CONFIGS, PUBLIC)
    internal_url_base = canonical_url(CONFIGS, INTERNAL)
    admin_url_base = canonical_url(CONFIGS, ADMIN)

    api_url_template = '%s:8004/v1/$(tenant_id)s'
    public_api_endpoint = (api_url_template % public_url_base)
    internal_api_endpoint = (api_url_template % internal_url_base)
    admin_api_endpoint = (api_url_template % admin_url_base)

    cfn_url_template = '%s:8000/v1'
    public_cfn_endpoint = (cfn_url_template % public_url_base)
    internal_cfn_endpoint = (cfn_url_template % internal_url_base)
    admin_cfn_endpoint = (cfn_url_template % admin_url_base)

    relation_data = {
        'heat_service': 'heat',
        'heat_region': config('region'),
        'heat_public_url': public_api_endpoint,
        'heat_admin_url': admin_api_endpoint,
        'heat_internal_url': internal_api_endpoint,
        'heat-cfn_service': 'heat-cfn',
        'heat-cfn_region': config('region'),
        'heat-cfn_public_url': public_cfn_endpoint,
        'heat-cfn_admin_url': admin_cfn_endpoint,
        'heat-cfn_internal_url': internal_cfn_endpoint,
    }

    relation_set(relation_id=rid, **relation_data)


@hooks.hook('identity-service-relation-changed')
@restart_on_change(restart_map())
def identity_changed():
    if 'identity-service' not in CONFIGS.complete_contexts():
        log('identity-service relation incomplete. Peer not ready?')
        return

    CONFIGS.write_all()
    configure_https()


@hooks.hook('amqp-relation-broken',
            'identity-service-relation-broken',
            'shared-db-relation-broken')
def relation_broken():
    CONFIGS.write_all()


@hooks.hook('leader-elected')
def leader_elected():
    if is_leader():
        if not leader_get('heat-domain-admin-passwd'):
            try:
                leader_set({'heat-domain-admin-passwd': pwgen(32)})
            except subprocess.CalledProcessError as e:
                log('leader_set: heat-domain-admin-password failed: {}'
                    .format(str(e)), level=WARNING)
        if not leader_get('heat-auth-encryption-key'):
            try:
                leader_set({'heat-auth-encryption-key': pwgen(32)})
            except subprocess.CalledProcessError as e:
                log('leader_set: heat-domain-admin-password failed: {}'
                    .format(str(e)), level=WARNING)


@hooks.hook('cluster-relation-joined')
def cluster_joined(relation_id=None):
    settings = {}

    for addr_type in ADDRESS_TYPES:
        address = get_relation_ip(
            addr_type,
            cidr_network=config('os-{}-network'.format(addr_type)))
        if address:
            settings['{}-address'.format(addr_type)] = address

    settings['private-address'] = get_relation_ip('cluster')

    relation_set(relation_id=relation_id, relation_settings=settings)


@hooks.hook('cluster-relation-changed',
            'cluster-relation-departed')
@restart_on_change(restart_map(), stopstart=True)
def cluster_changed():
    CONFIGS.write_all()


@hooks.hook('ha-relation-joined')
def ha_joined(relation_id=None):
    cluster_config = get_hacluster_config()

    resources = {
        'res_heat_haproxy': 'lsb:haproxy'
    }

    resource_params = {
        'res_heat_haproxy': 'op monitor interval="5s"'
    }

    if config('dns-ha'):
        update_dns_ha_resource_params(relation_id=relation_id,
                                      resources=resources,
                                      resource_params=resource_params)
    else:
        vip_group = []
        for vip in cluster_config['vip'].split():
            if is_ipv6(vip):
                res_heat_vip = 'ocf:heartbeat:IPv6addr'
                vip_params = 'ipv6addr'
            else:
                res_heat_vip = 'ocf:heartbeat:IPaddr2'
                vip_params = 'ip'

            iface = (get_iface_for_address(vip) or
                     config('vip_iface'))
            netmask = (get_netmask_for_address(vip) or
                       config('vip_cidr'))

            if iface is not None:
                vip_key = 'res_heat_{}_vip'.format(iface)
                if vip_key in vip_group:
                    if vip not in resource_params[vip_key]:
                        vip_key = '{}_{}'.format(vip_key, vip_params)
                    else:
                        log("Resource '%s' (vip='%s') already exists in "
                            "vip group - skipping" % (vip_key, vip), WARNING)
                        continue

                resources[vip_key] = res_heat_vip
                resource_params[vip_key] = (
                    'params {ip}="{vip}" cidr_netmask="{netmask}"'
                    ' nic="{iface}"'.format(ip=vip_params,
                                            vip=vip,
                                            iface=iface,
                                            netmask=netmask)
                )
                vip_group.append(vip_key)

        if len(vip_group) >= 1:
            relation_set(relation_id=relation_id,
                         groups={'grp_heat_vips': ' '.join(vip_group)})

    init_services = {
        'res_heat_haproxy': 'haproxy'
    }
    clones = {
        'cl_heat_haproxy': 'res_heat_haproxy'
    }
    relation_set(relation_id=relation_id,
                 init_services=init_services,
                 corosync_bindiface=cluster_config['ha-bindiface'],
                 corosync_mcastport=cluster_config['ha-mcastport'],
                 resources=resources,
                 resource_params=resource_params,
                 clones=clones)


@hooks.hook('ha-relation-changed')
def ha_changed():
    clustered = relation_get('clustered')
    if not clustered or clustered in [None, 'None', '']:
        log('ha_changed: hacluster subordinate not fully clustered.')
    else:
        log('Cluster configured, notifying other services and updating '
            'keystone endpoint configuration')
        for rid in relation_ids('identity-service'):
            identity_joined(rid=rid)


@hooks.hook('update-status')
@harden()
def update_status():
    log('Updating status.')


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    set_os_workload_status(CONFIGS, REQUIRED_INTERFACES)
    os_application_version_set(VERSION_PACKAGE)


if __name__ == '__main__':
    main()
