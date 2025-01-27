# Synapse with Workers Docker Image
***BIG FAT WARNING BITS: I'm still double-checking everything here is true, use with a pinch of salt.***

Synapse Docker image based on Matrix.org's [workers dockerfile](https://github.com/matrix-org/synapse/docker/Dockerfile-workers). Built for use on Unraid, but can
be used elsewhere. By default, Python only allows a single process because of a [GIL](https://realpython.com/python-gil/). The developers of Synapse decided that the best
way to bring the use of multiple CPU's into the equation was to use 'workers'.

[Synapse Official repository](https://github.com/matrix-org/synapse)

[Synapse Documentation](https://matrix-org.github.io/synapse/latest/)

[Synapse Configuration Manual](https://matrix-org.github.io/synapse/latest/usage/configuration/config_documentation.html)

A few things make this container great:
1. You can delete your homeserver.yaml(the main configuration file) and it will just
 remake it based on variables you declare(If you haven't declared them, obviously that
 won't work right). Keeps everything clean and up-to-date. Everything can be stored in
 the template for ease of use. If you use an existing homeserver.yaml, you shouldn't
 need to do anything to update your listeners(TODO: need to check this is true if using
 the historical 8448 port still. I have no way to verify this.)
2. Adding workers so that it can truly be multiprocess capable is as simple as adding
 the names to an environmental variable and recreating(or force updating) the container.
 All the worker details including reverse proxying of endpoints and replication is
 handled for you. Add and remove workers as you want.
3. Internal reverse proxy. Allows handling of federation traffic and worker endpoints
 dynamically. As a worker that has no traffic directed at it is useless, this takes
 care of the details. You will still need an external reverse proxy to handle the TLS
 encryption(if you are using Unraid, swag is excellent at this).
4. Support for metrics. If you like to look at pretty graphs in Grafana, this has
 Prometheus built-in(must be enabled, see below).
## Setup
### Pre-existing configuration
Moving an existing instance of your homeserver from the normal docker image to this one
is a fairly simple affair. Merely change your existing monolith docker image to point to
this image instead. If I've done my homework right, it should Just Work(TM). If you have
any existing command line arguments you pass as part of your template(such as `-m
synapse.app.homeserver`) you may delete all of those as they won't be needed any more.

### New configuration
Most guides will get you up and running ok(provided the guide isn't seriously out of
date), and I highly recommend you use the monolith images to get started, then switch
to this one. The recommended method is still using the Official Synapse Manual(linked
above). Most(but not all) configuration options can be set in advance with environmental
variables(see below) if you do not have a homeserver.yaml already made.

## Environment Variables
*(if remaking your configuration, unused otherwise)*:
* *SYNAPSE_SERVER_NAME*: . If you have an existing database, make sure you don't change this as many things will break.
* *SYNAPSE_REPORT_STATS*: yes or no, explicitly can't be true or false or 1 or 0.

This variable is where the magic happens. It is where workers instances are requested. It can be changed so that every time the image is updated/remade they will activate.<br>
* *SYNAPSE_WORKER_TYPES*: two major options here:
  * leave blank or empty for a normal monolith installation.
  * Include a string of the worker types separated by a comma. Full list of worker types can be
   found in the WORKERS_CONFIG template [here](https://github.com/realtyem/synapse-workers/blob/master/configure_workers_and_start.py). Some further instructions for special configurations can be found near
   the top of that file.

### Configuration generation bits:

Everything below can be added but isn't required to get everything up and running. Most just
allow for easier regeneration of main configuration.
* *POSTGRES_HOST*: IP address like 127.0.0.1 or path to the unix socket. Default is 'db'
* *POSTGRES_PORT*: 5432 is the default.
* *POSTGRES_DB*: synapse is the default, but needs to be whatever the actual
  database name is inside the Postgres instance
* *POSTGRES_USER*: The name of the database user. Default is 'synapse'
* *POSTGRES_PASSWORD*: The password associated with the Postgres user for your
  database. The only actually required Postgres variable with no defaults.
* *SYNAPSE_WORKERS_WRITE_LOGS_TO_DISK*: 1 or 0
* *SYNAPSE_LOG_LEVEL*: ERROR, WARNING, INFO, DEBUG. INFO is default
* *SYNAPSE_METRICS*: 'yes', '1', 'true', or 'on'. Anything else is a 'no'

Not required to set as the defaults are good. Only accessed on first generate of main config, ignored otherwise.
* *SYNAPSE_CONFIG_DIR* and *SYNAPSE_DATA_DIR*: /data is the default. Both point to
  the same place, but have different uses. If you find you need to change one(which
  you shouldn't), change both.
* *SYNAPSE_CONFIG_PATH*: SYNAPSE_CONFIG_DIR + /homeserver.yaml is the default.
  Shouldn't need to change.
* SYNAPSE_NO_TLS: leave this alone, it gets overridden in the source because "unsupported in worker mode". You should be on Unraid, your reverse proxy handles this anyways.
* SYNAPSE_EVENT_CACHE_SIZE: default is 10K, don't bother changing as there are better ways to increase caching if you have the RAM.
* SYNAPSE_MAX_UPLOAD_SIZE: default is 50M. Recommend changing to 2048M to avoid uploading issues. Nginx will be the bottleneck.
* SYNAPSE_TURN_URIS: list of URLs for turn servers, separate by a comma, no spaces
* SYNAPSE_TURN_SECRET: super secret string for not having to use annoying passwords and stuff for turn. Look it up.
* SYNAPSE_ENABLE_REGISTRATION: don't use this by default, huge security hole.
* SYNAPSE_REGISTRATION_SHARED_SECRET: (this will be autogenerated for you)
* SYNAPSE_MACAROON_SECRET_KEY: (this will be autogenerated for you, probably saved as a file.)
* SYNAPSE_ALLOW_GUEST:
* SYNAPSE_LOG_SENSITIVE: 1 or 0, default 0. This will input sensitive database bits into the logs. Won't need unless debugging.
* SYNAPSE_AS_REGISTRATION_DIR: path to the yaml Application service files, if using.
* SYNAPSE_APPSERVICES: same as above? Not sure.
* SYNAPSE_LOG_CONFIG: path and file or default homeserver log.config template file. Default is /data/log.config
* SYNAPSE_SERVE_SERVER_WELLKNOWN:  True or False, default false. Change to true for federated mode plus delegation. Won't be used if behind reverse proxy.
* SYNAPSE_PUBLIC_BASEURL:  full "https://matrix.example.com" used for delegation. If you use 'example.com' as your server name, but reverse proxy
 to 'matrix.example.com', then put the 'matrix.example.com' here.
* UID: defaults to 991
* GID: defaults to 991. Isn't that the group ID for dnsmasq?


Paths to map to somewhere as volumes
* */data*: (Required: where your configs live. Should be mapped to appdata/synapse if using Unraid.
* */data/media_store*: (Recommended. Thumbnails, other images, videos, other media lives here.
 Only map this if you want this not on your Unraid's appdata. Warning: can get quite large if
 active and federated.)
* */var/lib/prometheus*: (Recommended: where the metrics database lives. SQLite. If not set it
 will be added to the container and stats will be lost each time the container is updated or
 rebuilt.)
* */var/run/postgresql*: (Optional: where the postgres unix socket lives. Only bother with this if you want to use a unix socket for your Postgres connection. Can't do the same to redis...yet)

### Additions
Anything in this section can be enabled by giving it a value of  'yes', 'y', '1', 'true', 't',
or 'on'. Anything else is a 'no'.
* *SYNAPSE_ENABLE_COMPRESSOR*: The Matrix team made a database compressor that can make the
 state parts of the database less....sprawly? It's included by default. Enable to run it as a
 cron job every sunday at 1am. Actual space saving will not occur until your next auto-vaccuum
 run. POSTGRES_PASSWORD is required at minimum, all related postgres variables are
 recommended(username,host,port,databasename).
Grafana dashboards are provided in the contrib directory of the source repo.<br>
* *SYNAPSE_ENABLE_BUILTIN_COTURN*: Enables the built-in coturn server. Configuration is
 automatically generated and put in turnserver.conf. Other options you can set related to this,
 but not the homeserver configuration are COTURN_MIN_PORT and COTURN_MAX_PORT, which default
 if not set to 49153 and 49173, respectively. And you can enable COTURN_METRICS so you can see
 those pretty(boring) graphs.
* *NGINX_LISTEN_UNIX_SOCKET*: Set to a path and file that is represented internally for
   a Unix socket connection into the main entrypoint of the internal reverse proxy. If
   you do not bind mount this location to outside the container, this is useless. For
   example, set this to `/tmp/synapse-main.sock` and bind mount this to somewhere your
   external reverse proxy can find it, such as `/dev/shm` or `/run`.
* **Special Note for these next two options**: if you use either of these, they will create
  sockets on the internal directory structure at `/run`. I highly recommend adding to
  the docker start arguments `--tmpfs /run` so this will become non-persisted in the
  docker layer. In unRAID, you can find this under `Advanced view` and `Extra
  Parameters`. If you run into errors that sound like "Could not create socket file" or
  "Could not create lock file" you may have forgotten to do this.(May fix this later, to
  make sockets created onto `/dev/shm` instead as that is not persisted or exposed)
  * *SYNAPSE_PUBLIC_UNIX_SOCKETS*: Set to anything(1 is fine) to enable internal usage of
    Unix sockets between the internal reverse proxy and Synapse public facing endpoints.
  * *SYNAPSE_HTTP_REPLICATION_UNIX_SOCKETS*: set to anything(1 is fine) to enable internal
    Synapse HTTP replication endpoints to use Unix sockets. This only is useful if you
    have any workers declared(see *SYNAPSE_WORKER_TYPES* above).
* **
## Metrics
* *SYNAPSE_METRICS*: This will enable the built-in prometheus service and set it up to scrape
  the main process and any workers. If the below other variables are declared, they will be
  incorporated into the scrape targets.
* *SYNAPSE_METRICS_ENABLE_LISTENERS*: Defaults to *True* if *SYNAPSE_METRICS* is also *True*,
  otherwise *False*. This can be used to force the metric endpoints in Synapse to be setup.
* *SYNAPSE_METRICS_UNIX_SOCKETS*: Enable Unix socket scraping support. **IMPORTANT NOTE**:
  this does not currently work as Prometheus does not support scraping a Unix socket for
  general scraping. See [issue](github.com/prometheus/prometheus/issues/12024) for details.
* *SYNAPSE_ENABLE_REDIS_METRIC_EXPORT*: Redis is built into the docker image. It will
 automatically be used whenever a worker or multiple workers are declared. This enables
 exporting Redis metrics to the built-in Prometheus service. SYNAPSE_METRICS is required.
* *SYNAPSE_ENABLE_POSTGRES_METRIC_EXPORT*: If you are using a Postgresql Database, it can grab
  metrics from it as well. SYNAPSE_METRICS is required.
* *PROMETHEUS_REMOTE_WRITE_HTTP_URL*: No default. If set to an external timescale database,
  will add the correct `remote_write` block into the prometheus configuration. Use for things
  like [VictoriaMetrics](https://github.com/VictoriaMetrics/VictoriaMetrics)). See
  [prometheus docs](https://prometheus.io/docs/prometheus/latest/storage/#remote-storage-integrations)
  for more details about remote storage options. Using the default port and adjusting
  '<victoriametrics-addr>' for your own IP or hostname, this
  `http://<victoriametrics-addr>:8428/api/v1/write` should be enough to get started.
* *PROMETHEUS_SCRAPE_INTERVAL*: Defaults to 15 seconds, allows the built-in Prometheus
  scrape interval to be changed. This will be set to the global `scrape_interval` as well
  as the one for the Synapse job.
* *PROMETHEUS_STORAGE_RETENTION_TIME*: Defaults to `1y`. Time for prometheus to retain metrics data. (https://prometheus.io/docs/prometheus/latest/storage/#operational-aspects)

* **

## Nginx configuration
The built-in reverse proxy has defaults that are sane for a generic webserver/remote
proxy. This is not optimized for Synapse. We can do better.
* *NGINX_WORKER_PROCESSES*: Number of Nginx worker processes created. Defaults to
  "auto", which should be the number of CPU cores(including logical processors. *NOTE*:
  This will not honor docker container restriction on cpu. You will need to set this
  explicitly.
* *NGINX_WORKER_CONNECTIONS*: Number of simultaneous connections(client, remote server
  and Synapse workers/main process) per Nginx worker. Compilation default is 512. New
  default is 2048.
* *NGINX_GENERAL_PAGE_SIZE_BYTES*: The main size tunable, defaults to 4k(4096). Most
  other buffer size variables are multipliers of this number, so allow adjusting all of
  them at once with a single tunable. Fine-tuning, if required, is below.

### Connection compression
The default gzip buffer settings is 32 4k, so we will allow adjusting this by
piggy-backing on `NGINX_GENERAL_PAGE_SIZE_BYTES` in a following section
* *NGINX_GZIP_COMP_LEVEL*: The compression level of responses. Default is 1. Higher is
  more compressed at the expense of more CPU used. Tradeoffs above 1 didn't seem worth
  the additional CPU.
* *NGINX_GZIP_HTTP_VERSION*: Normally, Nginx will only serve a gzipped response if the
  HTTP version is 1.1 or higher. This allows lowering that threshold. Defaults to 1.1
* *NGINX_GZIP_MIN_LENGTH*: If a response is smaller than this(in bytes), just send it.
  Normal default is 20, which is really tiny and a waste of time to try and compress.
  New default is 200, but could probably put this at a MTU frame size for efficiency.

### Keep-alives
Nginx has support for HTTP 1.1 persistent connections for both client requests and
upstreams. Both are now enabled by default.
#### Client Request Keep-alives
* *NGINX_CLIENT_KEEPALIVE_ENABLE*: Defaults to True. Must be `True` or `False`. Sets
  `NGINX_CLIENT_KEEPALIVE_TIMEOUT_SECONDS` to `0`, which according to Nginx docs is the
  correct way to disable keep-alives from requests of this kind.
* *NGINX_CLIENT_KEEPALIVE_TIMEOUT_SECONDS*: Defaults to 60 seconds. This will close the
  socket if no traffic has occurred in this much time.

#### Upstream Keep-alives
The number of keep-alive connections is intentionally kept low, as
documentation suggests that it is a *per host* and not a global setting. Docs also
suggest that each upstream gets 2 connections, one from the incoming client request and
another to connect to the upstream. So, the formula is:

(number_of_given_worker_type * 2 * `NGINX_KEEPALIVE_CONNECTION_MULTIPLIER`)

* *NGINX_UPSTREAM_KEEPALIVE_ENABLE*: Defaults to True. Must be `True` or `False`. Allows
  for removal of all keepalive statements for debugging. Nginx recommends having this
  enabled.
* *NGINX_UPSTREAM_KEEPALIVE_TIMEOUT_SECONDS*: Defaults to 60 seconds. This will close
  the socket if no traffic has occurred in this much time.
* *NGINX_UPSTREAM_KEEPALIVE_CONNECTION_MULTIPLIER*: A keepalive multiplier, this gets multiplied
  by the number of server lines in a given upstream. This value is a maximum number of
  idle connections to keepalive. Default is 1, more did not seem to help during testing.

### Upstream Fail Retries
* *NGINX_UPSTREAM_SERVER_MAX_FAILS*: Defaults to `1`. The number of unsuccessful attempts to
  communicate with the server that should happen in the duration set by
  `NGINX_UPSTREAM_SERVER_FAIL_TIMEOUT` to consider the server unavailable for a duration also
  set by `NGINX_UPSTREAM_SERVER_FAIL_TIMEOUT`. Set to `0` to disable accounting and effectively
  ignores `NGINX_UPSTREAM_SERVER_FAIL_TIMEOUT`.
* *NGINX_UPSTREAM_SERVER_FAIL_TIMEOUT*: Defaults to `10` seconds.
  * the time during which the specified number of unsuccessful attempts to communicate with the
    server should happen to consider the server unavailable
  * and the period of time the server will be considered unavailable

### Proxy Buffering
* *NGINX_PROXY_BUFFERING*: Defaults to "on", change to "off" to disable buffering of
  responses from Synapse to clients. See also `NGINX_PROXY_BUFFER_SIZE_BYTES` below.
* *NGINX_PROXY_REQUEST_BUFFERING*: Defaults to "on", change to "off" to disable
  buffering of requests from clients to Synapse. This enables the usage of
  `NGINX_CLIENT_BODY_BUFFER_SIZE_BYTES` below.

#### Proxy Buffering fine-tuning
* *NGINX_PROXY_BUFFER_SIZE_BYTES*: The initial buffer for a response is this big,
  defaults to `NGINX_GENERAL_PAGE_SIZE_BYTES`. **However**, if `NGINX_PROXY_BUFFERING` is
  disabled, it will default to `NGINX_PROXY_BUFFERING_DISABLED_PAGE_MULTIPLIER` * `NGINX_GENERAL_PAGE_SIZE_BYTES` instead.
* *NGINX_PROXY_BUFFERING_DISABLED_PAGE_MULTIPLIER*: If proxy buffering is disabled, use
  this multiplier to calculate the allowed memory space. Defaults to 1.
* *NGINX_PROXY_BUSY_BUFFERS_SIZE_BYTES*: The effective lock size for the currently being
  filled from upstream buffer. Defaults to 2 * `NGINX_GENERAL_PAGE_SIZE_BYTES`
* *NGINX_PROXY_TEMP_FILE_WRITE_SIZE_BYTES*: When a response is to large to fit into the
  proxy buffer, it is written to a temporary file. This determines how much data is
  written at once. Defaults to 2 * `NGINX_GENERAL_PAGE_SIZE_BYTES`
* *NGINX_CLIENT_BODY_BUFFER_SIZE_BYTES*: While not strictly a proxy buffering variable,
  it is related because of proxying of requests to an upstream. This allows for large
  incoming requests to not be written to a temporary file before being proxied. Defaults
  to 1024 * `NGINX_GENERAL_PAGE_SIZE_BYTES`. The math that rationalizes this has to do
  with maximum incoming PDU and EDU sizes and counts in a single Synapse `Transaction`,
  and is more thoroughly documented in `nginx.conf.j2` in this repo.
* *NGINX_PROXY_READ_TIMEOUT*: Defaults to "60s". Sometimes upstream responses can take
  more than a minute between successive writes, use this to accommodate. Don't forget
  the 's' to denote seconds.

#### Further Notes about Nginx
* The `SYNAPSE_MAX_UPLOAD_SIZE` above is also used to limit incoming traffic in Nginx.
  This guard exists to prevent a DOS style attack against Synapse directly. While
  Synapse has utilities to reject large uploads, they will not begin working until after
  the transfer has completed. For example, someone uploads a 50GB image file to Synapse,
  thereby taking up a huge amount of disk space *before* it is rejected and removed,
  causing several systemic problems including database out-of-space errors.
* Disabling proxy buffering is very strange in documentation. It allows it but seems to
  do it anyways. I suppose the request/response has to be placed somewhere before
  transferring on it's way. It speaks of it being synchronous in nature, which implies
  that when enabled it is asynchronous.
* Part of the metrics acquisition process requires creating a special log file to scrape
  for the data. This log file is internal to the container and is not exposed by default.
  `logrotate` is used to keep the file to a more manageable level. Disable `logrotate`
  by setting `NGINX_DISABLE_LOGROTATE` to a 'truthy' value and then you can manage this
  log file yourself.