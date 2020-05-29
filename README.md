# Overview

Heat is the main project in the OpenStack Orchestration program. It implements
an orchestration engine to launch multiple composite cloud applications based
on templates in the form of text files that can be treated like code.

This charm deploys the Heat infrastructure.

# Usage

Heat requires the existence of the other core OpenStack services deployed via
Juju charms, specifically: mysql, rabbitmq-server, keystone and
nova-cloud-controller. The following assumes these services have already
been deployed.

After deployment of the cloud, the domain-setup action must be run to configure
required domains, roles and users in the cloud for Heat stacks.

For juju 2.x deployments use:

    juju run-action heat/0 domain-setup

If using juju 1.x run:

    juju action do heat/0 domain-setup

This is only required for >= OpenStack Kilo.

## High availability

When more than one unit is deployed with the [hacluster][hacluster-charm]
application the charm will bring up an HA active/active cluster.

There are two mutually exclusive high availability options: using virtual IP(s)
or DNS. In both cases the hacluster subordinate charm is used to provide the
Corosync and Pacemaker backend HA functionality.

See [OpenStack high availability][cdg-ha-apps] in the [OpenStack Charms
Deployment Guide][cdg] for details.

## Spaces

This charm supports the use of Juju Network Spaces, allowing the charm to be
bound to network space configurations managed directly by Juju.  This is only
supported with Juju 2.0 and above.

API endpoints can be bound to distinct network spaces supporting the network
separation of public, internal and admin endpoints.

Access to the underlying MySQL instance can also be bound to a specific space
using the shared-db relation.

To use this feature, use the --bind option when deploying the charm:

    juju deploy heat --bind \
       "public=public-space \
        internal=internal-space \
        admin=admin-space \
        shared-db=internal-space"

Alternatively, these can also be provided as part of a juju native bundle
configuration:

```yaml
    heat:
      charm: cs:xenial/heat
      num_units: 1
      bindings:
        public: public-space
        admin: admin-space
        internal: internal-space
        shared-db: internal-space
```

NOTE: Spaces must be configured in the underlying provider prior to attempting
to use them.

NOTE: Existing deployments using os-*-network configuration options will
continue to function; these options are preferred over any network space
binding provided if set.

## Policy Overrides

Policy overrides is an **advanced** feature that allows an operator to override
the default policy of an OpenStack service. The policies that the service
supports, the defaults it implements in its code, and the defaults that a charm
may include should all be clearly understood before proceeding.

> **Caution**: It is possible to break the system (for tenants and other
  services) if policies are incorrectly applied to the service.

Policy statements are placed in a YAML file. This file (or files) is then (ZIP)
compressed into a single file and used as an application resource. The override
is then enabled via a Boolean charm option.

Here are the essential commands (filenames are arbitrary):

    zip overrides.zip override-file.yaml
    juju attach-resource heat policyd-override=overrides.zip
    juju config heat use-policyd-override=true

See appendix [Policy Overrides][cdg-appendix-n] in the [OpenStack Charms
Deployment Guide][cdg] for a thorough treatment of this feature.

# Bugs

Please report bugs on [Launchpad][lp-bugs-charm-heat].

For general charm questions refer to the OpenStack [Charm Guide][cg].

<!-- LINKS -->

[cg]: https://docs.openstack.org/charm-guide
[cdg]: https://docs.openstack.org/project-deploy-guide/charm-deployment-guide
[cdg-appendix-n]: https://docs.openstack.org/project-deploy-guide/charm-deployment-guide/latest/app-policy-overrides.html
[lp-bugs-charm-heat]: https://bugs.launchpad.net/charm-heat/+filebug
[hacluster-charm]: https://jaas.ai/hacluster
[cdg-ha-apps]: https://docs.openstack.org/project-deploy-guide/charm-deployment-guide/latest/app-ha.html#ha-applications
