.. _kubernetes-ports:

Exposing Services on Kubernetes
===============================

.. note::
    This is a guide on how to configure an existing Kubernetes cluster (along with the caveats involved) to successfully expose ports and services externally through SkyPilot.

    If you are a SkyPilot user and your cluster has already been set up to expose ports,
    :ref:`Opening Ports <ports>` explains how to expose services in your task through SkyPilot.

SkyServe and SkyPilot clusters can :ref:`open ports <ports>` to expose services. For SkyPilot
clusters running on Kubernetes, we support either of two modes to expose ports:

* :ref:`LoadBalancer Service <kubernetes-loadbalancer>` (default)
* :ref:`Nginx Ingress <kubernetes-ingress>`


By default, SkyPilot creates a `LoadBalancer Service <https://kubernetes.io/docs/concepts/services-networking/service/>`__ on your Kubernetes cluster to expose the port.

If your cluster does not support LoadBalancer services, SkyPilot can also use `an existing Nginx IngressController <https://kubernetes.github.io/ingress-nginx/>`_ to create an `Ingress <https://kubernetes.io/docs/concepts/services-networking/ingress/>`_ to expose your service.

.. _kubernetes-loadbalancer:

LoadBalancer Service
--------------------

This mode exposes ports through a Kubernetes `LoadBalancer Service <https://kubernetes.io/docs/concepts/services-networking/service/#loadbalancer>`__. This is the default mode used by SkyPilot.

To use this mode, you must have a Kubernetes cluster that supports LoadBalancer Services:

* On Google GKE, Amazon EKS or other cloud-hosted Kubernetes services, this mode is supported out of the box and no additional configuration is needed.
* On bare metal and self-managed Kubernetes clusters, `MetalLB <https://metallb.universe.tf/>`_ can be used to support LoadBalancer Services.

When using this mode, SkyPilot will create a single LoadBalancer Service for all ports that you expose on a cluster.
Each port can be accessed using the LoadBalancer's external IP address and the port number. Use :code:`sky status --endpoints <cluster>` to view the external endpoints for all ports.

In cloud based Kubernetes clusters, this will automatically create an external Load Balancer.
GKE creates a `Pass-through Load Balancer <https://cloud.google.com/kubernetes-engine/docs/concepts/service-load-balancer>`__
and AWS creates a `Network Load Balancer <https://docs.aws.amazon.com/eks/latest/userguide/network-load-balancing.html>`__.
These load balancers will be automatically terminated when the cluster is deleted.

.. note::
    LoadBalancer services are not supported on kind clusters created using :code:`sky local up`.

.. note::
    The default LoadBalancer implementation in EKS selects a random port from the list of opened ports for the
    `LoadBalancer's health check <https://docs.aws.amazon.com/elasticloadbalancing/latest/network/target-group-health-checks.html>`_. This can cause issues if the selected port does not have a service running behind it.


    For example, if a SkyPilot task exposes 5 ports but only 2 of them have services running behind them, EKS may select a port that does not have a service running behind it and the LoadBalancer will not pass the healthcheck. As a result, the service will not be assigned an external IP address.

    To work around this issue, make sure all your ports have services running behind them.

.. note::
    **EKS Subnet Tagging Requirement**: For EKS clusters, the subnets used by your cluster must be tagged with the appropriate Kubernetes cluster tags for the AWS Load Balancer Controller to create Elastic Load Balancers (ELBs). If your LoadBalancer services are not getting external IPs assigned, ensure your subnets are tagged as follows:

    .. code-block:: bash

        # Replace <CLUSTER_NAME> with your EKS cluster name and <REGION> with your AWS region
        # Replace <SUBNET_ID> with your subnet IDs (repeat for each subnet)
        aws ec2 create-tags --region <REGION> \
          --resources <SUBNET_ID> \
          --tags Key=kubernetes.io/cluster/<CLUSTER_NAME>,Value=shared \
                Key=kubernetes.io/role/elb,Value=1

    For example, if your cluster name is ``my-eks-cluster`` in region ``us-east-2`` with subnets ``subnet-abc123`` and ``subnet-def456``:

    .. code-block:: bash

        aws ec2 create-tags --region us-east-2 \
          --resources subnet-abc123 subnet-def456 \
          --tags Key=kubernetes.io/cluster/my-eks-cluster,Value=shared \
                Key=kubernetes.io/role/elb,Value=1

    You can also use this script to automatically tag all subnets for your EKS cluster:

    .. code-block:: bash

        # Get cluster name and region from kubeconfig, then tag subnets
        CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' | sed 's/.*\///')
        REGION=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' | sed -n 's/.*\.eks\.\([^.]*\)\.amazonaws\.com.*/\1/p')
        for SUBNET_ID in $(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" --query 'cluster.resourcesVpcConfig.subnetIds' --output text); do
          aws ec2 create-tags --region "$REGION" --resources "$SUBNET_ID" \
            --tags "Key=kubernetes.io/cluster/$CLUSTER_NAME,Value=shared" \
                   "Key=kubernetes.io/role/elb,Value=1"
        done

    This is required for both regular SkyPilot clusters and SkyServe services that use LoadBalancer mode on EKS.

Internal load balancers
^^^^^^^^^^^^^^^^^^^^^^^

To restrict your services to be accessible only within the cluster, you can set all SkyPilot services to use `internal load balancers <https://kubernetes.io/docs/concepts/services-networking/service/#internal-load-balancer>`_.

Depending on your cloud, set the appropriate annotation in the SkyPilot config file (``~/.sky/config.yaml``):

.. code-block:: yaml

    # ~/.sky/config.yaml
    kubernetes:
      custom_metadata:
        annotations:
          # For GCP/GKE
          networking.gke.io/load-balancer-type: "Internal"
          # For AWS/EKS
          service.beta.kubernetes.io/aws-load-balancer-internal: "true"
          # For Azure/AKS
          service.beta.kubernetes.io/azure-load-balancer-internal: "true"


.. _kubernetes-ingress:

Nginx Ingress
-------------

This mode exposes ports by creating a Kubernetes `Ingress <https://kubernetes.io/docs/concepts/services-networking/ingress/>`_ backed by an existing `Nginx Ingress Controller <https://kubernetes.github.io/ingress-nginx/>`_.

To use this mode:

1. Install the Nginx Ingress Controller on your Kubernetes cluster. Refer to the `documentation <https://kubernetes.github.io/ingress-nginx/deploy/>`_ for installation instructions specific to your environment.
2. Verify that the ``ingress-nginx-controller`` service has a valid external IP:

.. code-block:: bash

    $ kubectl get service ingress-nginx-controller -n ingress-nginx

    # Example output:
    # NAME                             TYPE                CLUSTER-IP    EXTERNAL-IP     PORT(S)
    # ingress-nginx-controller         LoadBalancer        10.24.4.254   35.202.58.117   80:31253/TCP,443:32699/TCP


.. note::
    If the ``EXTERNAL-IP`` field is ``<none>``, you can manually
    specify the Ingress IP or hostname through the ``skypilot.co/external-ip``
    annotation on the ``ingress-nginx-controller`` service. In this case,
    having a valid ``EXTERNAL-IP`` field is not required.

    For example, if your ``ingress-nginx-controller`` service is ``NodePort``:

    .. code-block:: bash

      # Add skypilot.co/external-ip annotation to the nginx ingress service.
      # Replace <IP> in the following command with the IP you select.
      # Can be any node's IP if using NodePort service type.
      $ kubectl annotate service ingress-nginx-controller skypilot.co/external-ip=<IP> -n ingress-nginx

    If the ``EXTERNAL-IP`` field is ``<none>`` and the ``skypilot.co/external-ip`` annotation does not exist,
    SkyPilot will use ``localhost`` as the external IP for the Ingress,
    and the endpoint may not be accessible from outside the cluster.


3. Update the :ref:`SkyPilot config <config-yaml>` at :code:`~/.sky/config.yaml` to use the ingress mode.

.. code-block:: yaml

    kubernetes:
      ports: ingress

.. tip::

    For RKE2 and K3s, the pre-installed Nginx ingress is not correctly configured by default. Follow the `bare-metal installation instructions <https://kubernetes.github.io/ingress-nginx/deploy/#bare-metal-clusters/>`_ to set up the Nginx ingress controller correctly.


When using this mode, SkyPilot creates an ingress resource and a ClusterIP service for each port opened. The port can be accessed externally by using the Ingress URL plus a path prefix of the form :code:`/skypilot/{pod_name}/{port}`.

Use :code:`sky status --endpoints <cluster>` to view the full endpoint URLs for all ports.

.. code-block::

    $ sky status --endpoints mycluster
    8888: http://34.173.152.251/skypilot/test-2ea4/8888

.. note::

    When exposing a port under a sub-path such as an ingress, services expecting root path access, (e.g., Jupyter notebooks) may face issues. To resolve this, configure the service to operate under a different base URL. For Jupyter, use `--NotebookApp.base_url <https://jupyter-notebook.readthedocs.io/en/5.7.4/config.html>`_ flag during launch. Alternatively, consider using :ref:`LoadBalancer <kubernetes-loadbalancer>` mode. SkyServe services can instead be given root-path hostnames with the opt-in :ref:`wildcard subdomains <kubernetes-wildcard-subdomains>` below.

.. _kubernetes-wildcard-subdomains:

SkyServe wildcard subdomains (experimental)
-------------------------------------------

.. note::

    Opt-in and off by default. When unset, SkyPilot's behavior is unchanged.

Sub-path endpoints break applications that expect the root path, and they carry
no access control of their own. With a wildcard domain configured, each
SkyServe service is served at the root of its own hostname **and SkyPilot
authorizes every request to it**:

.. code-block::

    $ sky serve status
    SERVICE   VERSION  ENDPOINT
    my-llm    1        https://my-llm--a1b2c3d4.skypilot.example.com

Only callers who can reach the SkyPilot API server *and* have access to the
workspace the service was deployed in can reach the service. Humans
authenticate with their normal SSO login; machines use a
:ref:`service account token <service-accounts>`. Services never handle
authentication or authorization themselves.

Cluster ports (``sky launch --ports``) continue to use sub-path endpoints.

Domain requirements
^^^^^^^^^^^^^^^^^^^

The wildcard domain **must share a registrable domain with the API server**,
for example an API server on ``skypilot.example.com`` and services on
``*.skypilot.example.com``. Authorization uses the caller's API server session,
and a browser only sends that session to hosts under the same registrable
domain. SkyPilot refuses to start emitting hostnames otherwise.

You also need:

1. A wildcard DNS record ``*.skypilot.example.com`` pointing at the ingress
   controller.
2. A wildcard TLS certificate. Wildcard issuance requires a DNS-01 challenge.
   Prefer one wildcard certificate over per-service certificates: per-host
   certificates publish every service name to Certificate Transparency logs.
3. The API server's SSO session cookie scoped to the shared parent domain, so
   it reaches service hostnames -- ``--cookie-domain=.skypilot.example.com``
   and ``--whitelist-domain=.skypilot.example.com`` on oauth2-proxy.

Configuration
^^^^^^^^^^^^^

Admin-only: honored exclusively from the API server's own config
(``apiService.config`` in the Helm chart). A value set in a client's
``~/.sky/config.yaml`` is dropped with a warning.

.. code-block:: yaml

    kubernetes:
      ports: ingress
      ingress:
        # Must share a registrable domain with the API server host.
        # Unset (the default) keeps sub-path endpoints only.
        wildcard_domain: skypilot.example.com

        tls:
          # none     - no TLS at the ingress; endpoints advertised as http://
          # external - TLS terminated upstream (cloud LB, Gateway, mesh).
          #            No key enters the cluster. Recommended.
          # secret   - the ingress controller terminates TLS using a Secret.
          #            See the warning below.
          mode: external

There is no auth configuration. SkyPilot derives the authorization endpoint
from its own API server address, because a mistyped URL would silently expose
every service.

How authorization works
^^^^^^^^^^^^^^^^^^^^^^^

The ingress asks the API server about every request before it reaches a
service:

1. A request arrives for ``my-llm--a1b2c3d4.skypilot.example.com``.
2. The ingress calls ``/serve/authz`` on the API server, forwarding the
   original hostname and the caller's credentials.
3. The API server authenticates the caller -- SSO session cookie for a human,
   ``Authorization: Bearer`` service account token for a machine -- using the
   same code path that protects every other API.
4. It maps the hostname to a service, looks up that service's workspace, and
   checks the caller's access to it.
5. On success the request proceeds, carrying ``X-Skypilot-User``,
   ``X-Skypilot-User-Id`` and ``X-Skypilot-Workspace`` so the service knows
   who the caller is without authenticating anyone itself. On failure a human
   is redirected to log in and a machine receives 401 or 403.

Hostnames are keyed on the **service name**, not the load balancer port the
service occupies, so a torn-down service's address is never inherited by
whichever service next takes that port. The hostname-to-service map is
maintained by a reconciler that re-derives it from the live service table, so
a removed service loses both its route and its authorization entry.

.. note::

    The reconcile runs periodically (default 60s, configurable via
    ``daemons.serve-endpoint-reconcile-daemon.interval_seconds``). A service
    created just now is not reachable until the next pass, and a service whose
    workspace was never recorded -- for instance one created before this
    feature was enabled -- denies all requests until it is re-deployed.

Security notes
^^^^^^^^^^^^^^

**Services see the caller's session cookie.** The shared parent domain that
makes authorization work also means the browser sends its SkyPilot session
cookie to every service hostname, where the service's own code can read it
from the request headers. A service can therefore act as its visitors against
the API server. Deploy only services you trust as much as the API server
itself; this is not a model for running mutually untrusted tenants under one
domain.

**Services can set cookies for the parent domain**, which are then sent to the
API server and to other services. Keep the shared parent narrow -- prefer
``*.skypilot.example.com`` over ``*.example.com`` -- so this does not extend
to unrelated applications in your organization.

**Prefer terminating TLS outside the cluster.** An Ingress can only reference a
TLS Secret in its own namespace, and SkyPilot creates Ingresses in the task
namespace. ``tls.mode: secret`` therefore requires the wildcard private key --
which can impersonate every service -- to be replicated into every namespace
where user workloads run. It is gated behind
``tls.i_understand_key_replication: true``. ``tls.mode: external`` keeps the
key out of the cluster entirely.

.. warning::

    Ingress NGINX was retired in March 2026 and receives no further security
    patches. This feature is built on it because it is what SkyPilot's ingress
    mode already uses, but a Gateway API backend is the intended destination.
