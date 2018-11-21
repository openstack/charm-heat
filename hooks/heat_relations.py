#!/usr/bin/env python3
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
    service_restart,
)

from charmhelpers.fetch import (
    apt_install,
    apt_update
)

from charmhelpers.contrib.hahelpers.cluster import (
    is_elected_leader,
)

from charmhelpers.contrib.network.ip import (
    get_relation_ip,
)

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    openstack_upgrade_available,
    os_release,
    series_upgrade_complete,
    series_upgrade_prepare,
    sync_db_with_multi_ipv6_addresses,
    is_db_maintenance_mode,
)

from charmhelpers.contrib.openstack.ha.utils import (
    generate_ha_relation_data,
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
    setup_ipv6,
    pause_unit_helper,
    resume_unit_helper,
    assess_status,
    remove_old_packages,
    services,
)

from heat_context import (
    API_PORTS,
    HEAT_PATH,
)

from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.contrib.hardening.harden import harden
from charmhelpers.contrib.openstack.context import ADDRESS_TYPES
from charmhelpers.payload.execd import execd_preinstall

from charmhelpers.contrib.openstack.cert_utils import (
    get_certificate_request,
    process_certificates,
)

import charmhelpers.contrib.openstack.policyd as policyd

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
            log('Installing {} to /usr/bin'.format(f))
            shutil.copy2(f, '/usr/bin')

    for port in API_PORTS.values():
        open_port(port)
    # call the policy overrides handler which will install any policy overrides
    policyd.maybe_do_policyd_overrides(
        os_release('heat-common'),
        'heat',
        restart_handler=restart_heat_api,
    )


def restart_heat_api():
    service_restart('heat-api')


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
    update_nrpe_config()

    for rid in relation_ids('cluster'):
        cluster_joined(relation_id=rid)
    for r_id in relation_ids('ha'):
        ha_joined(relation_id=r_id)

    # call the policy overrides handler which will install any policy overrides
    policyd.maybe_do_policyd_overrides_on_config_changed(
        os_release('heat-common'),
        'heat',
        restart_handler=restart_heat_api,
    )


@hooks.hook('upgrade-charm.real')
@harden()
def upgrade_charm():
    apt_install(determine_packages(), fatal=True)
    if remove_old_packages():
        log("Package purge detected, restarting services", "INFO")
        for s in services():
            service_restart(s)
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
                    "Error was: {}".format(encryption_path, str(e)),
                    level=WARNING)
            else:
                # now we just delete the file
                os.remove(encryption_path)
    leader_elected()
    update_nrpe_config()

    # call the policy overrides handler which will install any policy overrides
    policyd.maybe_do_policyd_overrides(
        os_release('heat-common'),
        'heat',
        restart_handler=restart_heat_api,
    )


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
    if is_db_maintenance_mode():
        log('Database maintenance mode, aborting hook.')
        return
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
                'not in acl list: {}'.format(repr(allowed_units)))


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

    api_url_template = '{}:8004/v1/$(tenant_id)s'
    public_api_endpoint = (api_url_template.format(public_url_base))
    internal_api_endpoint = (api_url_template.format(internal_url_base))
    admin_api_endpoint = (api_url_template.format(admin_url_base))

    cfn_url_template = '{}:8000/v1'
    public_cfn_endpoint = (cfn_url_template.format(public_url_base))
    internal_cfn_endpoint = (cfn_url_template.format(internal_url_base))
    admin_cfn_endpoint = (cfn_url_template.format(admin_url_base))

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
    settings = generate_ha_relation_data('heat')
    relation_set(relation_id=relation_id, **settings)


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


@hooks.hook('heat-plugin-subordinate-relation-joined',
            'heat-plugin-subordinate-relation-changed')
@restart_on_change(restart_map(), stopstart=True)
def heat_plugin_subordinate_relation_joined(relid=None):
    CONFIGS.write_all()


@hooks.hook('update-status')
@harden()
def update_status():
    log('Updating status.')


@hooks.hook('certificates-relation-joined')
def certs_joined(relation_id=None):
    relation_set(
        relation_id=relation_id,
        relation_settings=get_certificate_request())


@hooks.hook('certificates-relation-changed')
@restart_on_change(restart_map(), stopstart=True)
def certs_changed(relation_id=None, unit=None):
    process_certificates('heat', relation_id, unit)
    configure_https()


@hooks.hook('pre-series-upgrade')
def pre_series_upgrade():
    log("Running prepare series upgrade hook", "INFO")
    series_upgrade_prepare(
        pause_unit_helper, CONFIGS)


@hooks.hook('post-series-upgrade')
def post_series_upgrade():
    log("Running complete series upgrade hook", "INFO")
    series_upgrade_complete(
        resume_unit_helper, CONFIGS)


@hooks.hook('nrpe-external-master-relation-joined',
            'nrpe-external-master-relation-changed')
def update_nrpe_config():
    # python-dbus is used by check_upstart_job
    apt_install('python-dbus')
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe.copy_nrpe_checks()
    nrpe.add_init_service_checks(nrpe_setup, services(), current_unit)
    nrpe.add_haproxy_checks(nrpe_setup, current_unit)
    nrpe_setup.write()


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status(CONFIGS)


if __name__ == '__main__':
    main()
