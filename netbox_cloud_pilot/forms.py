import requests
from django import forms
from django.conf import settings
from django.forms import ValidationError

from netbox.forms import NetBoxModelForm
from utilities.forms import BootstrapMixin, ConfirmationForm as _ConfirmationForm
from utilities.forms.fields import CommentField
from .constants import NETBOX_SETTINGS, NODE_GROUP_SQLDB
from .models import *
from .utils import *

__all__ = (
    "NetBoxConfigurationForm",
    "NetBoxSettingsForm",
    "NetBoxBackupStorageForm",
    "NetBoxDBBackupForm",
    "NetBoxPluginInstallForm",
)


def create_fieldset():
    """
    Create a fieldset from the NETBOX_SETTINGS.
    """
    fieldset = ()

    # Iterate through each section in NETBOX_SETTINGS
    for section in NETBOX_SETTINGS.sections:
        fields = []

        # Iterate through each setting in the section
        for param in section.params:
            # Append the lowercase name of the setting to the fields list
            fields.append(param.key.lower())

        # Append the section name and its fields to the fieldset
        fieldset += ((section.name, fields),)

    return fieldset


class NetBoxConfigurationForm(NetBoxModelForm):
    key = forms.CharField(
        help_text="Jelastic API token where the NetBox instance is running.",
    )

    env_name = forms.CharField(
        label="Environment Name",
        help_text="Jelastic environment name where the NetBox instance is running.",
    )

    env_name_storage = forms.ChoiceField(
        label="Environment Name",
        required=False,
        help_text="Jelastic environment name where the backup storage is running.",
    )

    license = forms.CharField(
        required=False,
        help_text="NetBox Enterprise license key.",
    )

    comments = CommentField()

    fieldsets = (
        (None, ("key", "env_name", "description")),
        ("Backup", ("env_name_storage",)),
        ("Enterprise", ("license",)),
    )

    class Meta:
        model = NetBoxConfiguration
        fields = (
            "key",
            "env_name",
            "description",
            "env_name_storage",
            "license",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["env_name_storage"].initial = self.instance.env_name_storage

        if self.instance.env_name:
            env_infos = (
                self.instance.iaas(self.instance.env_name, auto_init=False)
                .client.environment.Control.GetEnvs()
                .get("infos", [])
            )

            # Fetch the environment list and build the choices
            self.fields["env_name_storage"].choices = [
                (
                    env_info["env"]["envName"],
                    f"{env_info['env']['displayName']} ({env_info['env']['envName']})",
                )
                for env_info in env_infos
                if env_info.get("env", {}).get("properties", {}).get("projectScope", "") == "backup"
            ]


class NetBoxSettingsForm(BootstrapMixin, forms.Form):
    fieldsets = create_fieldset()

    class Meta:
        fields = [param.key.lower() for section in NETBOX_SETTINGS.sections for param in section.params]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Dynamically create fields for each setting
        for _, params in kwargs.get("initial", {}).items():
            for param in params:
                self.fields[param.key.lower()] = param.field(
                    label=param.label,
                    required=param.required,
                    help_text=param.help_text,
                    initial=param.initial,
                    **param.field_kwargs,
                )

    def clean(self):
        cleaned_data = super().clean()

        data = {}

        # Iterate through each section in NETBOX_SETTINGS
        for section in NETBOX_SETTINGS.sections:
            data[section.name] = {}

            # Iterate through each setting in the section
            for param in section.params:
                # Get the value of the setting
                value = cleaned_data.get(param.key.lower())

                # Set the value of the setting
                data[section.name][param.key.upper()] = value

        return data


class NetBoxBackupStorageForm(BootstrapMixin, forms.Form):
    deployment = forms.ChoiceField(
        choices=(
            ("standalone", "Standalone"),
            ("cluster", "Cluster"),
        )
    )

    node_count = forms.ChoiceField(
        choices=((1, 1), (3, 3), (5, 5), (7, 7)),
        initial=1,
        help_text="Number of nodes in the cluster.",
        required=False,
    )

    storage_size = forms.IntegerField(
        label="Storage Size",
        required=False,
        help_text="Size of the storage in GB.",
        initial=10,
        max_value=200,
    )

    region = forms.ChoiceField(
        help_text="Region where the storage will be deployed, this can be different from the NetBox instance region."
    )

    display_name = forms.CharField(
        label="Display Name",
        help_text="Display name for the storage.",
        max_length=50,
        initial="Backup Storage",
    )

    fieldsets = (
        (None, ("display_name", "storage_size", "region")),
        ("Deployment", ("deployment", "node_count")),
    )

    class Meta:
        fields = [
            "deployment",
            "node_count",
            "storage_size",
            "region",
            "display_name",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Fetch the region list and build the choices
        nc = NetBoxConfiguration.objects.first()
        regions = nc.iaas(nc.env_name, auto_init=False).client.environment.Control.GetRegions().get("array", [])
        self.fields["region"].choices = [
            (hard_node_group["uniqueName"], region["displayName"])
            for region in regions
            for hard_node_group in region["hardNodeGroups"]
        ]

    def clean(self):
        cleaned_data = super().clean()

        if cleaned_data["deployment"] == "cluster":
            if cleaned_data["node_count"] == "1":
                raise ValidationError({"node_count": "Node count must be greater than 1 for a cluster deployment."})

        if cleaned_data["deployment"] == "standalone":
            if cleaned_data["node_count"] != "1":
                raise ValidationError({"node_count": "Node count must be 1 for a standalone deployment."})

        return cleaned_data


class NetBoxDBBackupForm(NetBoxModelForm):
    db_password = forms.CharField(
        label="Database Password",
        help_text="Password for the <strong>webadmin</strong> user. You will find this in your email.",
        required=False,
    )

    tags = None

    fieldsets = (
        (None, ("netbox_env", "db_password")),
        ("Backup", ("crontab", "keep_backups")),
    )

    class Meta:
        model = NetBoxDBBackup
        fields = ["netbox_env", "crontab", "keep_backups", "db_password"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance.pk:
            # Fetch the database password from the addon settings
            app = self.instance.netbox_env.get_env().get_installed_addon("db-backup", node_group=NODE_GROUP_SQLDB)
            data = app.get("settings", {}).get("main", {}).get("data", {})
            self.fields["db_password"].initial = data.get("dbpass")

    def clean(self):
        super().clean()

        if db_password := self.cleaned_data.get("db_password"):
            self.instance._db_password = db_password

        if not self.instance.pk and not db_password:
            raise ValidationError({"db_password": "This field is required when adding a new backup."})


class NetBoxPluginInstallForm(BootstrapMixin, forms.Form):
    name = forms.CharField(
        label="Plugin Name",
        help_text="Name of the plugin to install.",
        max_length=255,
        disabled=True,
    )

    version = forms.ChoiceField(
        label="Plugin Version",
        help_text="Version of the plugin to install.",
    )

    configuration = forms.JSONField(
        label="Configuration",
        help_text="Configuration for the plugin.",
        required=False,
        initial={},
    )

    fieldsets = (
        (None, ("name", "version")),
        ("Configuration", ("configuration",)),
    )

    class Meta:
        fields = ["name", "version", "configuration"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        initial = kwargs.get("initial", {})

        # Get the plugins.yaml
        plugins = get_plugins_list()
        if plugin := plugins.get(initial.get("name")):
            self.fields["version"].choices = [(release, release) for release in filter_releases(plugin)]

        if initial.get("type") == "update":
            from importlib.metadata import metadata

            plugin_name = plugin.get("app_label")
            self.fields["version"].initial = metadata(plugin_name).get("Version")
            self.fields["configuration"].initial = settings.PLUGINS_CONFIG[plugin_name]

    def clean(self):
        plugins = get_plugins_list()
        plugin = plugins.get(self.cleaned_data.get("name"))
        selected_version = self.cleaned_data.get("version")

        # If the plugin is private, ensure that license is provided
        nc = NetBoxConfiguration.objects.first()
        if plugin.get("private"):
            if not nc.license:
                raise ValidationError({"name": "This plugin requires a NetBox Enterprise license."})

            # Check if the plugin is accessible using the license
            response = requests.get(
                plugin.get("github_api_url"),
                headers={"Authorization": f"Bearer {nc.license}"},
            )
            if not response.ok:
                raise ValidationError({"name": "This plugin is not accessible using the provided license."})

        # Get the required_settings from the plugin
        required_settings = next(
            (
                release.get("netbox", {}).get("required_settings", [])
                for release in plugin.get("releases", [])
                if release.get("tag") == selected_version
            ),
            [],
        )
        configuration = self.cleaned_data.get("configuration")

        # Check if the required_settings are in the configuration
        if not all(key in configuration.keys() for key in required_settings):
            raise ValidationError({"configuration": f"Missing required settings: {', '.join(required_settings)}"})


class NetBoxUpgradeForm(BootstrapMixin, forms.Form):
    version = forms.ChoiceField(
        label="Version",
        help_text="Version to upgrade to.",
    )

    class Meta:
        fields = ["version"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        instance = NetBoxConfiguration.objects.first()
        env = instance.get_env()

        versions = env.get_upgrades()
        self.fields["version"].choices = [(str(version), str(version)) for version in versions]

    def clean(self):
        super().clean()

        instance = NetBoxConfiguration.objects.first()
        env = instance.get_env()

        # Run upgrade checks
        upgrade_check, error = env.upgrade_checks(self.cleaned_data.get("version"))
        if not upgrade_check:
            raise ValidationError({"version": error})


class ConfirmationForm(_ConfirmationForm):
    """
    A generic confirmation form. The form is not valid unless the `confirm` field is checked.
    """

    name = forms.CharField(widget=forms.HiddenInput())
