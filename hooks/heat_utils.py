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

from copy import deepcopy
from collections import OrderedDict
from subprocess import check_call

from charmhelpers.contrib.openstack import context, templating

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    get_os_codename_install_source,
    os_release,
    token_cache_pkgs,
    enable_memcache,
    CompareOpenStackReleases,
    os_application_version_set,
    make_assess_status_func,
    pause_unit,
    resume_unit,
)

from charmhelpers.contrib.hahelpers.cluster import (
    get_hacluster_config,
    get_managed_services_and_ports,
)


from charmhelpers.fetch import (
    add_source,
    apt_install,
    apt_update,
    apt_upgrade,
    apt_purge,
    apt_autoremove,
    filter_missing_packages,
)

from charmhelpers.core.hookenv import (
    log,
    config,
    relation_ids,
)

from charmhelpers.core.host import (
    lsb_release,
    service_start,
    service_stop,
    CompareHostReleases,
)

from heat_context import (
    API_PORTS,
    HeatIdentityServiceContext,
    HeatSecurityContext,
    InstanceUserContext,
    HeatApacheSSLContext,
    HeatHAProxyContext,
    HeatPluginContext,
    QuotaConfigurationContext,
)

TEMPLATES = 'templates/'

# The interface is said to be satisfied if anyone of the interfaces in
# the list has a complete context.
REQUIRED_INTERFACES = {
    'database': ['shared-db'],
    'messaging': ['amqp'],
    'identity': ['identity-service'],
}

BASE_PACKAGES = [
    'python-keystoneclient',
    'python-swiftclient',  # work-around missing epoch in juno heat package
    'uuid',
    'apache2',
    'haproxy',
]

PY3_PACKAGES = [
    'python3-heat',
    'python3-keystoneclient',
    'python3-memcache',
    'python3-swiftclient',
    'python3-six',

    # NOTE(lourot): these dependencies are declared as "recommended" on the
    # heat-engine package but starting from focal-victoria they are actually
    # hard dependencies. Until this is fixed we need this workaround. See
    # lp:1893935
    'python3-vitrageclient',
    'python3-zunclient',
]

VERSION_PACKAGE = 'heat-common'

BASE_SERVICES = [
    'heat-api',
    'heat-api-cfn',
    'heat-engine'
]

# Cluster resource used to determine leadership when hacluster'd
CLUSTER_RES = 'grp_heat_vips'
SVC = 'heat'
HEAT_DIR = '/etc/heat'
HEAT_CONF = '/etc/heat/heat.conf'
HEAT_API_PASTE = '/etc/heat/api-paste.ini'
HAPROXY_CONF = '/etc/haproxy/haproxy.cfg'
APACHE_PORTS_CONF = '/etc/apache2/ports.conf'
HTTPS_APACHE_CONF = '/etc/apache2/sites-available/openstack_https_frontend'
HTTPS_APACHE_24_CONF = os.path.join('/etc/apache2/sites-available',
                                    'openstack_https_frontend.conf')
ADMIN_OPENRC = '/root/admin-openrc-v3'
MEMCACHED_CONF = '/etc/memcached.conf'

CONFIG_FILES = OrderedDict([
    (HEAT_CONF, {
        'services': BASE_SERVICES,
        'contexts': [context.AMQPContext(ssl_dir=HEAT_DIR),
                     context.SharedDBContext(relation_prefix='heat',
                                             ssl_dir=HEAT_DIR),
                     context.OSConfigFlagContext(),
                     context.InternalEndpointContext(),
                     HeatIdentityServiceContext(service=SVC, service_user=SVC),
                     HeatHAProxyContext(),
                     HeatSecurityContext(),
                     InstanceUserContext(),
                     HeatPluginContext(),
                     QuotaConfigurationContext(),
                     context.SyslogContext(),
                     context.LogLevelContext(),
                     context.WorkerConfigContext(),
                     context.BindHostContext(),
                     context.MemcacheContext(),
                     context.OSConfigFlagContext()],
    }),
    (HEAT_API_PASTE, {
        'services': [s for s in BASE_SERVICES if 'api' in s],
        'contexts': [HeatIdentityServiceContext()],
    }),
    (HAPROXY_CONF, {
        'contexts': [context.HAProxyContext(singlenode_mode=True),
                     HeatHAProxyContext()],
        'services': ['haproxy'],
    }),
    (HTTPS_APACHE_CONF, {
        'contexts': [HeatApacheSSLContext()],
        'services': ['apache2'],
    }),
    (HTTPS_APACHE_24_CONF, {
        'contexts': [HeatApacheSSLContext()],
        'services': ['apache2'],
    }),
    (ADMIN_OPENRC, {
        'contexts': [HeatIdentityServiceContext(service=SVC,
                                                service_user=SVC)],
        'services': []
    }),
    (MEMCACHED_CONF, {
        'contexts': [context.MemcacheContext()],
        'services': ['memcached'],
    }),
    (APACHE_PORTS_CONF, {
        'contexts': [],
        'services': ['apache2'],
    }),
])


def resource_map(release=None):
    """
    Dynamically generate a map of resources that will be managed for a single
    hook execution.
    """
    _release = release or os_release('heat-common', base='icehouse')
    _resource_map = deepcopy(CONFIG_FILES)

    if os.path.exists('/etc/apache2/conf-available'):
        _resource_map.pop(HTTPS_APACHE_CONF)
    else:
        _resource_map.pop(HTTPS_APACHE_24_CONF)

    if not enable_memcache(release=_release):
        _resource_map.pop(MEMCACHED_CONF)

    return _resource_map


def register_configs(release=None):
    """Register config files with their respective contexts.
    Regstration of some configs may not be required depending on
    existing of certain relations.
    """
    release = release or os_release('heat-common', base='icehouse')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map(release).items():
        configs.register(cfg, rscs['contexts'])
    return configs


def api_port(service):
    return API_PORTS[service]


def determine_packages():
    release = CompareOpenStackReleases(os_release('heat-common'))

    # currently all packages match service names
    packages = BASE_PACKAGES + BASE_SERVICES
    packages.extend(token_cache_pkgs(source=config('openstack-origin')))

    if release >= 'rocky':
        packages = [p for p in packages if not p.startswith('python-')]
        packages.extend(PY3_PACKAGES)

    return list(set(packages))


def determine_purge_packages():
    '''
    Determine list of packages that where previously installed which are no
    longer needed.

    :returns: list of package names
    '''
    release = CompareOpenStackReleases(os_release('heat-common'))
    if release >= 'rocky':
        pkgs = [p for p in BASE_PACKAGES if p.startswith('python-')]
        pkgs.extend(['python-heat', 'python-memcache'])
        return pkgs
    return []


def remove_old_packages():
    '''Purge any packages that need ot be removed.

    :returns: bool Whether packages were removed.
    '''
    installed_packages = filter_missing_packages(determine_purge_packages())
    if installed_packages:
        apt_purge(installed_packages, fatal=True)
        apt_autoremove(purge=True, fatal=True)
    return bool(installed_packages)


def do_openstack_upgrade(configs):
    """Perform an uprade of heat.

    Takes care of upgrading packages,
    rewriting configs and potentially any other post-upgrade
    actions.

    :param configs: The charms main OSConfigRenderer object.

    """
    new_src = config('openstack-origin')
    new_os_rel = get_os_codename_install_source(new_src)

    log('Performing OpenStack upgrade to %s.' % (new_os_rel))

    configure_installation_source(new_src)
    dpkg_opts = [
        '--option', 'Dpkg::Options::=--force-confnew',
        '--option', 'Dpkg::Options::=--force-confdef',
    ]
    apt_update()
    apt_upgrade(options=dpkg_opts, fatal=True, dist=True)
    apt_install(packages=determine_packages(), options=dpkg_opts, fatal=True)

    remove_old_packages()

    # set CONFIGS to load templates from new release and regenerate config
    configs.set_release(openstack_release=new_os_rel)
    configs.write_all()

    migrate_database()


def restart_map():
    '''Determine the correct resource map to be passed to
    charmhelpers.core.restart_on_change() based on the services configured.

    :returns: dict: A dictionary mapping config file to lists of services
                    that should be restarted when file changes.
    '''
    return OrderedDict([(cfg, v['services'])
                        for cfg, v in resource_map().items()
                        if v['services']])


def services():
    """Returns a list of services associate with this charm"""
    _services = []
    for v in restart_map().values():
        _services = _services + v
    return list(set(_services))


def migrate_database():
    """Runs heat-manage to initialize a new database or migrate existing"""
    log('Migrating the heat database.')
    [service_stop(s) for s in services()]
    check_call(['heat-manage', 'db_sync'])
    [service_start(s) for s in services()]


def setup_ipv6():
    ubuntu_rel = lsb_release()['DISTRIB_CODENAME'].lower()
    if CompareHostReleases(ubuntu_rel) < "trusty":
        raise Exception("IPv6 is not supported in the charms for Ubuntu "
                        "versions less than Trusty 14.04")

    # Need haproxy >= 1.5.3 for ipv6 so for Trusty if we are <= Kilo we need to
    # use trusty-backports otherwise we can use the UCA.
    if (ubuntu_rel == 'trusty' and
            CompareOpenStackReleases(os_release('heat-common')) < 'liberty'):
        add_source('deb http://archive.ubuntu.com/ubuntu trusty-backports '
                   'main')
        apt_update()
        apt_install('haproxy/trusty-backports', fatal=True)


def check_optional_relations(configs):
    """Check that if we have a relation_id for high availability that we can
    get the hacluster config.  If we can't then we are blocked.

    This function is called from assess_status/set_os_workload_status as the
    charm_func and needs to return either None, None if there is no problem or
    the status, message if there is a problem.

    :param configs: an OSConfigRender() instance.
    :return 2-tuple: (string, string) = (status, message)
    """
    if relation_ids('ha'):
        try:
            get_hacluster_config()
        except Exception:
            return ('blocked',
                    'hacluster missing configuration: '
                    'vip, vip_iface, vip_cidr')
    # return 'unknown' as the lowest priority to not clobber an existing
    # status.
    return "unknown", ""


def get_optional_interfaces():
    """Return the optional interfaces that should be checked if the relavent
    relations have appeared.

    :returns: {general_interface: [specific_int1, specific_int2, ...], ...}
    """
    optional_interfaces = {}
    if relation_ids('ha'):
        optional_interfaces['ha'] = ['cluster']

    return optional_interfaces


def assess_status(configs):
    """Assess status of current unit
    Decides what the state of the unit should be based on the current
    configuration.
    SIDE EFFECT: calls set_os_workload_status(...) which sets the workload
    status of the unit.
    Also calls status_set(...) directly if paused state isn't complete.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    assess_status_func(configs)()
    os_application_version_set(VERSION_PACKAGE)


def assess_status_func(configs):
    """Helper function to create the function that will assess_status() for
    the unit.
    Uses charmhelpers.contrib.openstack.utils.make_assess_status_func() to
    create the appropriate status function and then returns it.
    Used directly by assess_status() and also for pausing and resuming
    the unit.

    NOTE: REQUIRED_INTERFACES is augmented with the optional interfaces
    depending on the current config before being passed to the
    make_assess_status_func() function.

    NOTE(ajkavanagh) ports are not checked due to race hazards with services
    that don't behave sychronously w.r.t their service scripts.  e.g.
    apache2.
    @param configs: a templating.OSConfigRenderer() object
    @return f() -> None : a function that assesses the unit's workload status
    """
    required_interfaces = REQUIRED_INTERFACES.copy()
    required_interfaces.update(get_optional_interfaces())
    _services, _ = get_managed_services_and_ports(services(), [])
    return make_assess_status_func(
        configs, required_interfaces,
        charm_func=check_optional_relations,
        services=_services, ports=None)


def pause_unit_helper(configs):
    """Helper function to pause a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.pause_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(pause_unit, configs)


def resume_unit_helper(configs):
    """Helper function to resume a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.resume_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(resume_unit, configs)


def _pause_resume_helper(f, configs):
    """Helper function that uses the make_assess_status_func(...) from
    charmhelpers.contrib.openstack.utils to create an assess_status(...)
    function that can be used with the pause/resume of the unit
    @param f: the function to be used with the assess_status(...) function
    @returns None - this function is executed for its side-effect
    """
    # TODO(ajkavanagh) - ports= has been left off because of the race hazard
    # that exists due to service_start()
    _services, _ = get_managed_services_and_ports(services(), [])
    f(assess_status_func(configs),
      services=_services,
      ports=None)
