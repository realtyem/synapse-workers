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
4. Support for metrics. If you like to look at pretty graphs in Grafana, this is 
 prometheus ready.
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
* SYNAPSE_METRICS: 'yes', '1', 'true', or 'on'. Anything else is a 'no'

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
or 'on'. Anything else is a 'no'
* *SYNAPSE_METRICS*: This will enable the builtin prometheus service and add the necessary bits 
 to Synapse to expose metrics.
* *SYNAPSE_ENABLE_REDIS_METRIC_EXPORT*: Redis is built into the docker image. It will 
 automatically be used whenever a worker or multiple workers are declared. This enables 
 exporting Redis metrics to the built-in Prometheus service. SYNAPSE_METRICS is required.
* *SYNAPSE_ENABLE_POSTGRES_METRIC_EXPORT*: If you are using a Postgresql Database, it can grab 
* metrics from it as well. SYNAPSE_METRICS is required. 
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
