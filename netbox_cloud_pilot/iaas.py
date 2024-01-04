import logging
import time
from functools import lru_cache
from typing import Tuple

import requests
import yaml
from django.conf import settings
from jelastic import Jelastic
from jelastic.api.exceptions import JelasticApiError
from semver.version import Version

from core.choices import JobStatusChoices
from core.models import Job
from netbox.config import get_config
from . import utils
from .constants import JELASTIC_API, NODE_GROUP_CP, NODE_GROUP_SQLDB

logger = logging.getLogger("netbox_cloud_pilot")

__all__ = (
    "IaaS",
    "IaaSNetBox",
)


class IaaSJob:
    @classmethod
    def enqueue(cls, func, model, request=None, *args, **kwargs):
        """
        Enqueue a job to be executed asynchronously.
        """
        logger.info(f"Enqueuing job {func.__name__}")

        kwargs["_func"] = func

        return Job.enqueue(
            cls._run_job,
            model,
            name=func.__name__,
            user=request.user if request else None,
            job_timeout=settings.RQ_DEFAULT_TIMEOUT,
            **kwargs,
        )

    @staticmethod
    def _run_job(job, *args, **kwargs):
        """
        Run a job.
        """
        data = {"params": kwargs}

        # Start the job
        try:
            job.start()
            func = kwargs.pop("_func")
            result = func(*args, **kwargs)

            data.update({"result": result})

            job.terminate()
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(e)
            data.update({"error": str(e)})
            job.terminate(status=JobStatusChoices.STATUS_ERRORED)

        job.data = data
        job.save()


class IaaS(IaaSJob):
    """
    This class is used to manage the IaaS layer of the Jelastic platform.
    """

    def __init__(self, token: str, env_name: str, auto_init: bool = True):
        self.admin_url = JELASTIC_API
        self.client = Jelastic(base_url=self.admin_url, token=token)
        self.env_name = env_name

        if auto_init:
            self._get_env_info()
        logger.debug(f"IaaS layer initialized for environment {self.env_name}")

    @lru_cache(maxsize=1)
    def _get_env_info(self):
        """
        Get information about the environment.
        """
        logger.debug("Getting environment information")
        return self.client.environment.Control.GetEnvInfo(env_name=self.env_name)

    @lru_cache(maxsize=10)
    def _get_env_var(self, node_group):
        """
        Get environment variables for a node group.
        """
        logger.debug(f"Getting environment variables for node group {node_group}")
        return self.client.environment.Control.GetContainerEnvVarsByGroup(
            env_name=self.env_name, node_group=node_group
        ).get("object", {})

    def clear_cache(self):
        """
        Clear the cache.
        """
        logger.debug("Clearing cache")
        self._get_env_info.cache_clear()
        self._get_env_var.cache_clear()

    def get_env(self):
        """
        Get the environment.
        """
        return self._get_env_info()["env"]

    def get_nodes(self, node_group: str = None, is_master: bool = True) -> dict | list:
        """
        Get the environment nodes.
        """
        logger.debug(f"Getting nodes for node group {node_group}, is_master={is_master}")
        nodes = self._get_env_info().get("nodes", [])
        if node_group:
            nodes = [node for node in nodes if node["nodeGroup"] == node_group]
        if is_master:
            nodes = [node for node in nodes if node["ismaster"]][0]
        return nodes

    def get_node_groups(self):
        """
        Get the environment node groups.
        """
        node_groups = sorted(
            self._get_env_info().get("nodeGroups", {}),
            key=lambda x: x.get("displayName", x.get("name")),
        )

        # For each node group, get the related nodes
        for node_group in node_groups:
            node_group["node"] = self.get_nodes(node_group=node_group["name"], is_master=True)

        return node_groups

    def get_node(self, node_id):
        """
        Get a node by its ID.
        """
        logger.debug(f"Getting node {node_id} for environment {self.env_name}")
        nodes = self._get_env_info().get("nodes", [])
        return next((node for node in nodes if str(node["id"]) == node_id), None)

    def get_external_domains(self):
        """
        Get the external domains.
        """
        logger.debug(f"Getting external domains for environment {self.env_name}")
        return self._get_env_info().get("extdomains", [])

    def is_ssl_enabled(self):
        """
        Check if SSL is enabled.
        """
        logger.debug(f"Checking if built-in SSL is enabled for environment {self.env_name}")
        return self.get_env().get("sslstate", False)

    def get_node_log(self, node_id, path="/var/log/run.log"):
        """
        Get the logs of a node.
        """
        logger.debug(f"Getting log for node {node_id} for environment {self.env_name}")
        return self.client.environment.Control.ReadLog(env_name=self.env_name, node_id=node_id, path=path).get(
            "body", ""
        )

    def get_url(self):
        """
        Get the environment URL.
        """
        logger.debug(f"Getting URL for environment {self.env_name}")
        scheme = "https" if self.is_ssl_enabled() else "http"
        if ext_domains := self.get_external_domains():
            return f"{scheme}://{ext_domains[0]}"

        domain = self.get_env().get("domain", "")
        return f"{scheme}://{domain}"

    def get_addons(self, node_group, search=None):
        """
        Get the addons for a node group.
        """
        logger.debug(f"Getting addons for node group {node_group}")
        return self.client.marketplace.App.GetAddonList(
            env_name=self.env_name, node_group=node_group, search=search
        ).get("apps", [])

    def get_installed_addon(self, app_id, node_group, search=None):
        """
        Get the installed addon for a node group.
        """
        logger.debug(f"Checking if addon ({app_id}) is installed for node group {node_group}")
        addons = self.get_addons(node_group=node_group, search=search)

        return next(
            (addon for addon in addons if addon["app_id"] == app_id and addon.get("isInstalled", False)),
            None,
        )

    def add_env_vars(self, node_group, env_vars):
        """
        Add environment variables to a node group.
        """
        logger.debug(f"Adding environment variables to node group {node_group}")
        return self.client.environment.Control.AddContainerEnvVars(
            env_name=self.env_name, node_group=node_group, vars=env_vars
        )

    def get_env_vars(self, node_group):
        """
        Get environment variables for a node group.
        """
        logger.debug(f"Getting environment variables for node group {node_group}")
        return self.client.environment.Control.GetContainerEnvVarsByGroup(
            env_name=self.env_name, node_group=node_group
        ).get("object", {})

    def remove_env_vars(self, node_group, env_vars: list[str]):
        """
        Remove environment variables from a node group.
        """
        logger.debug(f"Removing environment variables from node group {node_group}")
        return self.client.environment.Control.RemoveContainerEnvVars(
            env_name=self.env_name, node_group=node_group, vars=env_vars
        )

    def run_script(self, name, code, description=None, params=None):
        """
        Run a script.
        """
        app_id = self.get_env().get("appid")

        try:
            result = self.client.development.Scripting.GetScripts(
                app_id=app_id,
                type="js",
            )
            scripts = result.get("scripts", [])
            for script in scripts:
                if script.get("name") == name:
                    logger.debug(f"Deleting existing script {name}")
                    self.client.development.Scripting.DeleteScript(app_id=app_id, name=name)
                    continue
        except JelasticApiError as e:
            logger.error(e)

        self.client.development.Scripting.CreateScript(app_id=app_id, name=name, type="js", code=code)

        return self.client.utils.Scheduler.CreateEnvTask(
            env_name=self.env_name,
            script=name,
            trigger={"once_delay": 10000},
            description=description,
            params=params,
        )

    def restart_nodes(self, node_groups: list[str], lazy: bool = False, delay: int = 10000):
        """
        Restart nodes for a list of node groups.
        """
        results = []

        logger.debug(f"Restarting nodes for node groups {', '.join(node_groups)}")
        if lazy:
            for node_group in node_groups:
                script_name = f"restart-{node_group}-ncp"
                script_code = """
                var c = jelastic.environment.control, e = envName, s = session, r, resp;
                resp = c.GetEnvInfo(e, s);
                if (resp.result != 0) return resp;
                r = c.RestartNodes({ envName: e, session: s, nodeGroup: nodeGroup, isSequential: false });
                if (r.result != 0) return r;
                return { result: 0, message: 'Restarted ' + nodeGroup}
                """

                task_result = self.run_script(
                    name=script_name,
                    code=script_code,
                    description=f"Restart {node_group} nodes",
                    params={"envName": self.env_name, "nodeGroup": node_group},
                )
                results.append(task_result)

            return results

        for node_group in node_groups:
            task_result = self.client.environment.Control.RestartNodes(
                env_name=self.env_name,
                node_group=node_group,
            )
            results.append(task_result)

        return results

    def execute_cmd(self, node_id, command):
        """
        Execute a command on a node.
        """
        logger.debug(f"Executing command {command} on node {node_id}")
        return self.client.environment.Control.ExecCmdById(
            env_name=self.env_name,
            node_id=node_id,
            command_list=[
                {
                    "command": command,
                }
            ],
        ).get("responses", [])

    def execute_action(self, app_unique_name, action="configure", params=None):
        """
        Execute an action on an addon.
        """
        logger.info(f"Executing action {action} on addon {app_unique_name}")
        return self.client.marketplace.Installation.ExecuteAction(
            app_unique_name=app_unique_name,
            action=action,
            params=params,
        )

    def install_addon(self, app_id, node_group, addon_settings=None):
        """
        Install an addon.
        """
        # Check if the addon is already installed
        if addon := self.get_installed_addon(app_id=app_id, node_group=node_group):
            logger.info(f"Addon {app_id} is already installed")
            return self.execute_action(
                app_unique_name=addon.get("uniqueName"),
                params=addon_settings,
            )

        logger.info(f"Installing addon {app_id}")
        return self.client.marketplace.App.InstallAddon(
            env_name=self.env_name,
            app_id=app_id,
            settings=addon_settings,
            node_group=node_group,
        )

    def uninstall_addon(self, app_id, node_group, search=None):
        """
        Uninstall an addon.
        """
        # Check if the addon is already installed
        if addon := self.get_installed_addon(app_id=app_id, node_group=node_group, search=search):
            logger.info(f"Uninstalling addon {app_id}")

            return self.client.marketplace.Installation.Uninstall(
                app_unique_name=addon.get("uniqueName"),
                target_app_id=self.get_env().get("appid"),
                app_template_id=app_id,
            )

        msg = f"Addon {app_id} is not installed on node group {node_group}."
        logger.info(msg)
        return {"result": 0, "message": msg}

    def get_master_node(self, node_group):
        return self.get_nodes(node_group=node_group, is_master=True)


class IaaSNetBox(IaaS):
    """
    This class is used to manage the IaaS layer of the Jelastic platform for NetBox.
    """

    def get_nb_node_groups(self):
        """
        Get the environment node groups for NetBox.
        """
        results = []

        node_groups = self.get_node_groups()

        # For each node group, get the related nodes
        for node_group in node_groups:
            if "netbox" in node_group.get("node", {}).get("customitem", {}).get("dockerName"):
                results.append(node_group)

        return results

    def load_plugins(self):
        """
        Loads the plugins from the plugins.yaml file.
        """
        master_node_id = self.get_master_node(NODE_GROUP_CP).get("id")
        plugins_yaml = self.execute_cmd(master_node_id, "cat /etc/netbox/config/plugins.yaml")[0].get("out", "")
        return yaml.safe_load(plugins_yaml) or {}

    def dump_plugins(self, plugins):
        """
        Dumps the plugins to the plugins.yaml file.
        """
        master_node_id = self.get_master_node(NODE_GROUP_CP).get("id")
        plugins_yaml = yaml.dump(plugins)

        self.client.environment.File.Write(
            env_name=self.env_name,
            path="/etc/netbox/config/plugins.yaml",
            body=plugins_yaml,
            node_id=master_node_id,
            is_append_mode=False,
        )

    def install_plugin(self, plugin: dict, version, plugin_settings=None, github_token=None):
        master_node_id = self.get_master_node(NODE_GROUP_CP).get("id")
        activate_env = "source /opt/netbox/venv/bin/activate"

        # Install the plugin version
        if plugin.get("private"):
            # Ensure `git` is installed
            self.execute_cmd(master_node_id, "apt-get install -y git")

            github_url = plugin.get("github_url")
            github_url = github_url.replace("https://github.com", f"git+https://{github_token}@github.com")

            self.execute_cmd(master_node_id, f"{activate_env} && pip install {github_url}@{version}")
        else:
            self.execute_cmd(
                master_node_id,
                f'{activate_env} && pip install {plugin.get("name")}=={version}',
            )

        plugins = self.load_plugins()
        plugins[plugin.get("app_label")] = plugin_settings or {}
        self.dump_plugins(plugins)

        # Run collectstatic command
        self.execute_cmd(
            node_id=master_node_id,
            command=f"{activate_env} && /opt/netbox/netbox/manage.py collectstatic --no-input --clear 1>/dev/null",
        )

        return self.restart_nodes(
            node_groups=[node_group["name"] for node_group in self.get_nb_node_groups()],
            lazy=True,
        )

    def uninstall_plugin(self, plugin: dict):
        """
        Uninstall an addon for NetBox.
        """
        plugins = self.load_plugins()
        plugins.pop(plugin.get("app_label"))
        self.dump_plugins(plugins)

        return self.restart_nodes(
            node_groups=[node_group["name"] for node_group in self.get_nb_node_groups()],
            lazy=True,
        )

    def get_env_var(self, variable, default=None):
        """
        Get the environment variable for NetBox.
        """
        container_vars = self._get_env_var(NODE_GROUP_CP)
        return getattr(get_config(), variable, container_vars.get(variable, None)) or default

    def _get_docker_tags(self):
        """
        Get the Docker tags for NetBox.
        """
        master_node = self.get_master_node(NODE_GROUP_CP)
        docker = master_node.get("customitem", {})

        response = requests.get(f'https://hub.docker.com/v2/repositories/{docker["dockerName"]}/tags?page_size=1000')
        response.raise_for_status()
        response = response.json()

        names = [d["name"] for d in response["results"] if d["name"].startswith("v")]
        tags = []
        for name in names:
            try:
                version = Version.parse(name.lstrip("v"))
                if not version.prerelease:
                    tags.append(version)
            except ValueError:
                pass

        return tags

    def get_upgrades(self):
        """
        Get the available upgrades for NetBox.
        """
        docker_tags = self._get_docker_tags()
        current_version = Version.parse(settings.VERSION)

        return [tag for tag in docker_tags if tag > current_version]

    def is_upgrade_available(self):
        """
        Check if an upgrade is available for NetBox.
        """
        return bool(self.get_upgrades())

    def is_db_backup_running(self, app_unique_name):
        # Get current running actions
        current_actions = self.client.environment.Tracking.GetCurrentActions().get("array", [])
        for action in current_actions:
            action_parameters = action.get("parameters", {})

            if (
                action_parameters.get("appUniqueName") == app_unique_name
                and action_parameters.get("action") == "backup"
            ):
                return True

        return False

    def db_backup(self, app_unique_name):
        """
        Backup NetBox database.
        """
        while self.is_db_backup_running(app_unique_name=app_unique_name):
            logger.debug("Waiting for database backup to finish...")
            time.sleep(30)

        return self.execute_action(app_unique_name=app_unique_name, action="backup")

    def upgrade_checks(self, version) -> Tuple[bool, str]:
        """
        Run upgrade checks for NetBox.
        """
        # Fetch the plugins from the store
        plugins = utils.get_plugins_list()
        for plugin_name, plugin in plugins.items():
            if plugin_name in settings.PLUGINS:
                if not utils.filter_releases(plugin, version):
                    return (
                        False,
                        f"Plugin {plugin_name} does not have a release for version {version}",
                    )

        return True, ""

    def upgrade(self, version):
        """
        Upgrade NetBox.
        """
        version = f"v{version}"

        if addon := self.get_installed_addon(app_id="db-backup", node_group=NODE_GROUP_SQLDB):
            self.db_backup(app_unique_name=addon.get("uniqueName"))

        # Fetch all node groups
        for node_group in self.get_nb_node_groups():
            node_group_name = node_group["name"]
            script_name = f"upgrade-netbox-{node_group_name}-ncp"
            script_code = """
            var c = jelastic.environment.control, e = envName, s = session, r, resp;
            resp = c.GetEnvInfo(e, s);
            if (resp.result != 0) return resp;

            // Wait for the environment to be running before redeploying
            var isRunning = false, attempts = 0, maxAttempts = 30;
            while (!isRunning && attempts < maxAttempts) {
              envInfo = c.GetEnvInfo(e, s);
              if (envInfo.result != 0) return envInfo;

              if (envInfo.env.status == 1) {
                isRunning = true;
              } else {
                attempts++;
                java.lang.Thread.sleep(30000);
              }
            }

            if (!isRunning) {
              return { result: 1, message: 'Environment is not running' };
            }

            r = c.RedeployContainersByGroup({ envName: e, session: s, nodeGroup: nodeGroup, tag: tag, useExistingVolumes: true });
            if (r.result != 0) return r;
            return { result: 0, message: 'Upgraded ' + nodeGroup}
            """

            self.run_script(
                name=script_name,
                code=script_code,
                description=f"Upgrade {node_group_name} nodes",
                params={
                    "tag": version,
                    "envName": self.env_name,
                    "nodeGroup": node_group_name,
                },
            )

        self.clear_cache()
