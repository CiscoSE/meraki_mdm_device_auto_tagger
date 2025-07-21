#!/usr/bin/env python3
"""
Script: auto_tag_ipads_with_file_logger.py
Purpose: Automatically tag iPads in Meraki Systems Manager based on cellular capability, with logging to file.
"""

import os
import argparse
from typing import List
import meraki
from dotenv import load_dotenv

from rich.console import Console
from rich.logging import RichHandler
import logging

# Load environment variables from .env file
load_dotenv()

DEFAULT_CELLULAR_TAG = "Store_Ipad"
DEFAULT_WIFI_ONLY_TAG = "Curbside_iPad"

console = Console()

logfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_tag.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]",
    handlers=[
        RichHandler(console=console, show_path=False, markup=True),
        logging.FileHandler(logfile_path, mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger("rich")


def resolve_network_id(dashboard, network_name: str, org_name: str) -> str:
    """
    Resolve the network ID from the network name and organization name.
    :param dashboard: Meraki Dashboard API client instance
    :param network_name: Name of the network to resolve
    :param org_name: Name of the organization to search in
    :return: Network ID if found, otherwise raises ValueError
    """
    # Find Org
    orgs = dashboard.organizations.getOrganizations(total_pages='all')
    org = next((o for o in orgs if o['name'].lower() == org_name.lower()), None)
    if not org:
        raise ValueError(f"Organization '{org_name}' not found")

    org_id = org['id']

    # Find the network
    networks = dashboard.organizations.getOrganizationNetworks(org_id, total_pages='all',
                                                               productType=['systemsManager'])
    net = next((n for n in networks if n['name'].lower() == network_name.lower()), None)

    if not net:
        raise ValueError(f"Network '{network_name}' not found in organization '{org_name}'")

    return net['id']


def batch_modify_tags(dashboard, network_id: str, serials: List[str], tag: str, action: str):
    """
    Modify tags for a batch of devices in the specified network.
    :param dashboard: Meraki Dashboard API client instance
    :param network_id: ID of the network to modify devices in
    :param serials: List of device serials to modify tags for
    :param tag: Tag to add
    :param action: Action to perform ('add')
    """
    max_batch = 20
    for i in range(0, len(serials), max_batch):
        batch = serials[i:i + max_batch]
        dashboard.sm.modifyNetworkSmDevicesTags(
            networkId=network_id,
            ids=batch,
            tags=[tag],
            updateAction=action
        )


def main():
    """
    Main function to execute the auto-tagging script.
    """
    # Define argument parser
    parser = argparse.ArgumentParser(description="Auto-tag Meraki iPads by hardware attributes")
    parser.add_argument("--org-name",
                        help="Organization name, required if API key has access to multiple orgs",
                        required=True)
    parser.add_argument("--network-name", help="Name of the Meraki System Manager network to use", required=True)
    parser.add_argument("--cellular-tag", default=DEFAULT_CELLULAR_TAG)
    parser.add_argument("--wifi-tag", default=DEFAULT_WIFI_ONLY_TAG)
    parser.add_argument("--remove-wrong", action="store_true")
    args = parser.parse_args()

    # Sanity check API Key
    api_key = os.getenv("MERAKI_DASHBOARD_API_KEY")
    if not api_key:
        logger.error("API key missing")
        raise SystemExit("Set the MERAKI_DASHBOARD_API_KEY in a .env file or as an environment variable")

    dashboard = meraki.DashboardAPI(api_key, suppress_logging=True, wait_on_rate_limit=True)

    # Resolve network name to id
    try:
        network_id = resolve_network_id(dashboard, args.network_name, args.org_name)
        logger.info(f"Resolved network name {args.network_name} to  ID: {network_id}")
    except Exception as e:
        logger.error(str(e))
        raise SystemExit("Failed to resolve network ID")

    # Retrieve devices in the network
    devices = dashboard.sm.getNetworkSmDevices(
        networkId=network_id,
        total_pages='all',
        fields=["imei", "iccid", "simCarrierNetwork", "phoneNumber", "tags"]
    )
    logger.info(f"Total System Manager devices retrieved: {len(devices)}")

    # Identify which devices need to be tagged with cellular or wifi tags
    tag_cellular, tag_wifi = [], []
    untag_cellular, untag_wifi = [], []
    for device in devices:
        logger.info(f"Processing device: {device}")

        # Sanity check, if missing device unique id, skip
        device_id = device.get("id")
        if not device_id:
            continue

        # Determine if cellular-capable (if ALL fields are None, then WiFi only) + current tags
        fields = [
            device.get("imei"),
            device.get("iccid"),
            device.get("simCarrierNetwork"),
            device.get("phoneNumber")
        ]
        has_cell = not all(f is None for f in fields)

        current_tags = set(device.get("tags") or [])

        # Identify correct tag
        correct_tag = args.cellular_tag if has_cell else args.wifi_tag

        # Queue for tagging (only for those without correct tags)
        if correct_tag not in current_tags:
            if has_cell:
                logger.info(
                    f"Device id '{device_id}' is cellular-capable but missing tag in {current_tags}, tagging with '{args.cellular_tag}'")
                tag_cellular.append(device_id)
            else:
                logger.info(
                    f"Device id '{device_id}' is Wi-Fi only but missing tag in {current_tags}, tagging with '{args.wifi_tag}'")
                tag_wifi.append(device_id)
        else:
            logger.info(f"Device id '{device_id}' already has the correct tag '{correct_tag}', skipping")

        # If remove_wrong is enabled, queue for untagging if the tag is incorrect
        if args.remove_wrong:
            wrong_tag = args.wifi_tag if has_cell else args.cellular_tag
            if wrong_tag in current_tags:
                if has_cell:
                    logger.info(
                        f"Device id '{device_id}' is cellular-capable but incorrectly tagged with '{wrong_tag}', removing")
                    untag_wifi.append(device_id)
                else:
                    logger.info(
                        f"Device id '{device_id}' is Wi-Fi only but incorrectly tagged with '{wrong_tag}', removing")
                    untag_cellular.append(device_id)

    # Add correct tags!
    if len(tag_cellular) > 0:
        logger.info(f"Tagging {len(tag_cellular)} devices with '{args.cellular_tag}'")
        batch_modify_tags(dashboard, network_id, tag_cellular, args.cellular_tag, "add")

    if len(tag_wifi) > 0:
        logger.info(f"Tagging {len(tag_wifi)} devices with '{args.wifi_tag}'")
        batch_modify_tags(dashboard, network_id, tag_wifi, args.wifi_tag, "add")

    # Remove incorrect tags if requested
    if args.remove_wrong:
        if untag_cellular:
            logger.info(f"Removing '{args.cellular_tag}' from {len(untag_cellular)} Wi-Fi-only devices")
            batch_modify_tags(dashboard, network_id, untag_cellular, args.cellular_tag, "delete")
        if untag_wifi:
            logger.info(f"Removing '{args.wifi_tag}' from {len(untag_wifi)} cellular-capable devices")
            batch_modify_tags(dashboard, network_id, untag_wifi, args.wifi_tag, "delete")

    logger.info("✅ Script complete")


if __name__ == "__main__":
    main()
