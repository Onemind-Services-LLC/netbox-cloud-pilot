import requests
import semver
import yaml
from django.conf import settings

from .constants import NETBOX_JPS_REPO

__all__ = ("get_plugins_list", "filter_releases")


def get_plugins_list():
    plugins = []

    # Download plugins.yaml from GitHub
    response = requests.get(f"{NETBOX_JPS_REPO}/plugins.yaml")
    if response.ok:
        plugins = yaml.safe_load(response.text)

    return plugins


def is_compatible(netbox_version, min_version, max_version):
    """
    Check if the NetBox version is compatible with the plugin.
    """
    if min_version and semver.compare(netbox_version, min_version) < 0:
        return False

    if max_version and semver.compare(netbox_version, max_version) > 0:
        return False

    return True


def filter_releases(plugin):
    """
    Filter the releases based on the NetBox version.
    """
    compatible_releases = []

    for release in plugin.get("releases", []):
        netbox = release.get("netbox")
        min_version = netbox.get("min")
        max_version = netbox.get("max")

        if is_compatible(settings.VERSION, min_version, max_version):
            compatible_releases.append(release["tag"])

    try:
        return sorted(compatible_releases, key=semver.parse_version_info, reverse=True)
    except ValueError:
        return compatible_releases