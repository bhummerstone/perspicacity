"""Resolve and validate a new Azure environment; never creates Azure resources."""

import argparse
import base64
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fnmatch import fnmatchcase
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


ASSETS = Path(__file__).resolve().parents[1] / "assets"
PROFILES = json.loads((ASSETS / "profiles.json").read_text(encoding="utf-8"))
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
DEFAULTS = {
    "location": PROFILES["defaultLocation"],
    "compute": "cpu",
    "size": PROFILES["defaultSize"],
    "vmSize": "auto",
    "accessMode": "bastion",
    "sshSourceCidr": "",
    "vnetCidr": "192.168.0.0/24",
    "bastionSubnetCidr": "192.168.0.0/26",
    "vmSubnetCidr": "192.168.0.64/26",
}
REQUIRED = {"subscriptionId", "resourceGroup", "namePrefix", "adminUsername", "sshPublicKeyPath", "owner"}
RESERVED_USERS = {
    "administrator", "admin", "user", "user1", "test", "user2", "test1", "user3",
    "admin1", "1", "123", "a", "actuser", "adm", "admin2", "aspnet", "backup",
    "console", "david", "guest", "john", "owner", "root", "server", "sql", "support",
    "support_388945a0", "sys", "test2", "test3", "user4", "user5",
}


class OnboardingError(Exception):
    """An explicit configuration, CLI or Azure preflight failure."""


def command(args):
    executable = shutil.which(args[0])
    if not executable:
        if args[0] == "az":
            raise OnboardingError(
                "Azure CLI is missing from PATH. Installation can be offered with your permission; "
                "run onboarding.py prerequisites for the platform-specific install option. "
                "The onboarding skill asks for consent before installing, then verifies az version."
            )
        raise OnboardingError(f"Missing prerequisite: {args[0]}. Install it and retry.")
    # Windows batch wrappers interpret metacharacters even with shell=False.
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        if any(re.search(r'[&|<>^%!\r\n"]', str(arg)) for arg in args[1:]):
            raise OnboardingError("Use CLI arguments and paths without Windows shell metacharacters.")
    result = subprocess.run(
        [executable, *map(str, args[1:])], text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise OnboardingError(
            f"{args[0]} {' '.join(map(str, args[1:]))} failed ({result.returncode}):\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return result.stdout


def az(*args, subscription=None):
    argv = ["az", *args, "--only-show-errors", "--output", "json"]
    if subscription:
        argv.extend(["--subscription", subscription])
    return json.loads(command(argv))


def prerequisites():
    """Check Azure CLI without login; return an install proposal, never execute it."""
    if shutil.which("az"):
        version = az("version")["azure-cli"]
        if not isinstance(version, str) or not version.strip():
            raise OnboardingError("Azure CLI did not report a usable version; repair it before continuing.")
        return {"status": "AzureCliReady", "azureCli": {"installed": True, "version": version}}
    installation = {"requiresConsent": True, "commands": []}
    guide = "https://learn.microsoft.com/cli/azure/install-azure-cli"
    if sys.platform == "win32":
        installation["method"] = "WinGet or Microsoft's signed MSI installer"
        installation["documentation"] = guide + "-windows"
        if shutil.which("winget"):
            installation["commands"] = [
                ["winget", "install", "--exact", "--id", "Microsoft.AzureCLI", "--source", "winget"],
            ]
    elif sys.platform == "darwin":
        installation["method"] = "Homebrew"
        installation["documentation"] = guide + "-macos"
        if shutil.which("brew"):
            installation["commands"] = [["brew", "update"], ["brew", "install", "azure-cli"]]
    else:
        installation["method"] = "Microsoft's instructions for the execution host's OS/distribution"
        installation["documentation"] = guide + ("-linux" if sys.platform.startswith("linux") else "")
    return {
        "status": "AwaitingAzureCliInstallConsent", "azureCli": {"installed": False},
        "installation": installation,
        "nextStep": "Offer the latest stable Azure CLI installation using the host's question tool; "
                    "do not install until approved. After installation, refresh the terminal/PATH "
                    "if necessary and rerun prerequisites before login or deployment.",
    }


def validate_networks(config):
    networks = {}
    keys = ["vnetCidr", "vmSubnetCidr"]
    if config["accessMode"] == "bastion":
        keys.append("bastionSubnetCidr")
    for key in keys:
        try:
            network = ipaddress.ip_network(config[key], strict=True)
        except ValueError as exc:
            raise OnboardingError(f"{key}: {exc}") from exc
        if network.version != 4 or not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):
            raise OnboardingError(f"{key} must be an aligned RFC1918 private IPv4 CIDR.")
        networks[key] = network
    vnet, vm = networks["vnetCidr"], networks["vmSubnetCidr"]
    if vm.prefixlen > 29:
        raise OnboardingError("The VM subnet must be /29 or larger to leave usable Azure addresses.")
    if any(not network.subnet_of(vnet) for network in networks.values()):
        raise OnboardingError("All deployed subnets must be contained in vnetCidr.")
    if "bastionSubnetCidr" in networks:
        bastion = networks["bastionSubnetCidr"]
        if bastion.prefixlen > 26:
            raise OnboardingError("AzureBastionSubnet must be /26 or larger.")
        if bastion.overlaps(vm):
            raise OnboardingError("The Bastion and VM subnets must not overlap.")
    return networks


def validate_access(access_mode, ssh_source_cidr):
    if access_mode not in ("bastion", "public-ip"):
        raise OnboardingError("accessMode must be bastion (recommended) or public-ip (demo only).")
    if access_mode == "bastion":
        if ssh_source_cidr:
            raise OnboardingError("sshSourceCidr is only used with accessMode public-ip.")
        return
    try:
        source = ipaddress.ip_network(ssh_source_cidr, strict=True)
    except ValueError as exc:
        raise OnboardingError("public-ip mode requires sshSourceCidr: an aligned administrator IPv4 CIDR, preferably /32.") from exc
    if source.version != 4 or source.prefixlen == 0:
        raise OnboardingError("sshSourceCidr must be a restricted IPv4 CIDR, never 0.0.0.0/0.")


def read_config(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OnboardingError("Configuration must be a JSON object.")
    missing = REQUIRED - raw.keys()
    unknown = raw.keys() - REQUIRED - DEFAULTS.keys()
    if missing or unknown:
        raise OnboardingError(f"Missing fields: {sorted(missing)}; unknown fields: {sorted(unknown)}.")
    config = {**DEFAULTS, **raw}
    if any(not isinstance(value, str) or (not value and key != "sshSourceCidr") for key, value in config.items()):
        raise OnboardingError("All configuration values must be nonempty strings except optional sshSourceCidr.")
    validate_access(config["accessMode"], config["sshSourceCidr"])
    try:
        config["subscriptionId"] = str(uuid.UUID(config["subscriptionId"]))
    except ValueError as exc:
        raise OnboardingError("subscriptionId must be a subscription UUID, not a display name.") from exc
    if config["subscriptionId"] == str(uuid.UUID(int=0)):
        raise OnboardingError("Replace the example subscription UUID with your subscription ID.")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", config["resourceGroup"]):
        raise OnboardingError("resourceGroup must contain 1-64 letters, digits, hyphens or underscores.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,18}[a-z0-9]", config["namePrefix"]):
        raise OnboardingError("namePrefix must be 3-20 lowercase letters/digits/hyphens, with alphanumeric ends.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:@/+-]{0,127}", config["owner"]) or config["owner"] != config["owner"].strip():
        raise OnboardingError("owner must be a 1-128 character team/owner label without surrounding whitespace or shell metacharacters.")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", config["adminUsername"]):
        raise OnboardingError("Use a Linux username of 1-32 lowercase letters, digits, underscores or hyphens.")
    if config["adminUsername"] in RESERVED_USERS:
        raise OnboardingError("This administrator username is reserved by Azure; choose another.")
    if not re.fullmatch(r"[a-z][a-z0-9]+", config["location"]):
        raise OnboardingError("Use a region code such as westus2, not a display name.")
    if config["compute"] not in ("cpu", "gpu") or config["size"] not in ("small", "medium", "large", "xl"):
        raise OnboardingError("compute must be cpu/gpu; size must be small/medium/large/xl.")
    if config["vmSize"] != "auto":
        pinned_profile(config)
    key = Path(config["sshPublicKeyPath"]).expanduser()
    config["sshPublicKeyPath"] = str((path.parent / key).resolve() if not key.is_absolute() else key.resolve())
    validate_networks(config)
    return config


def read_public_key(path):
    text = Path(path).read_text(encoding="utf-8").strip()
    parts = text.split()
    if len(text.splitlines()) != 1 or len(parts) < 2 or parts[0] not in ("ssh-ed25519", "ssh-rsa"):
        raise OnboardingError("Provide a single OpenSSH RSA/Ed25519 PUBLIC key, never a private key.")
    try:
        data = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise OnboardingError("The SSH public key has invalid base64 data.") from exc
    if len(data) < 4:
        raise OnboardingError("The SSH public key is truncated.")
    length = int.from_bytes(data[:4], "big")
    if data[4:4 + length] != parts[0].encode("ascii"):
        raise OnboardingError("The SSH public key type does not match its encoded data.")
    fingerprint = command(["ssh-keygen", "-l", "-f", path])
    if parts[0] == "ssh-rsa" and int(fingerprint.split()[0]) < 2048:
        raise OnboardingError("RSA keys must have at least 2048 bits; generate a new 4096-bit key or use Ed25519.")
    return text


def capabilities(sku):
    return {entry["name"].lower(): entry["value"] for entry in sku.get("capabilities", [])}


def profile_candidates(compute, size):
    profile = PROFILES[compute][size]
    if compute == "gpu":
        return [dict(profile)]
    return [
        {
            **profile, "sku": f"Standard_D{profile['vcpus']}{variant['suffix']}",
            "processor": variant["processor"], "generation": variant["generation"],
        }
        for variant in PROFILES["cpuVariants"] if profile["vcpus"] <= variant["maxVcpus"]
    ]


def pinned_profile(config):
    profile = next(
        (profile for profile in profile_candidates(config["compute"], config["size"])
         if profile["sku"] == config["vmSize"]), None,
    )
    if profile is None:
        raise OnboardingError("vmSize must be auto or an exact catalog SKU for the chosen compute/T-shirt size; parameters require a pinned SKU.")
    return profile


def select_sku(skus, config, profile):
    matches = [sku for sku in skus if sku["name"] == profile["sku"]]
    if len(matches) != 1:
        raise OnboardingError(f"{profile['sku']} is not offered in {config['location']}. Choose another region.")
    sku = matches[0]
    for restriction in sku.get("restrictions", []):
        if restriction.get("type") != "Zone":
            raise OnboardingError(
                f"{sku['name']} is restricted for this subscription/region: {json.dumps(restriction)}. "
                "Request access/quota or explicitly choose another region/profile."
            )
    caps = capabilities(sku)
    if "V2" not in caps.get("hypervgenerations", "").split(","):
        raise OnboardingError("The selected SKU does not advertise Generation 2 support.")
    if caps.get("cpuarchitecturetype", "").lower() != "x64":
        raise OnboardingError("The selected SKU does not advertise the required x64 architecture.")
    if int(caps.get("vcpus", 0)) != profile["vcpus"]:
        raise OnboardingError("Live vCPU count differs from the sizing catalog; review the profile before deploying.")
    if Decimal(caps.get("memorygb", "0")) != profile["memoryGiB"]:
        raise OnboardingError("Live memory size differs from the sizing catalog; review the profile before deploying.")
    if caps.get("encryptionathostsupported", "").lower() != "true":
        raise OnboardingError(
            f"{sku['name']} does not advertise EncryptionAtHostSupported=True in {config['location']}. "
            "Choose another approved region/profile; host encryption will not be disabled."
        )
    if config["compute"] == "cpu":
        if "NVMe" not in caps.get("diskcontrollertypes", "").split(","):
            raise OnboardingError("The CPU SKU does not advertise NVMe support.")
        if caps.get("trustedlaunchdisabled", "").lower() == "true":
            raise OnboardingError("The CPU SKU does not support the required Trusted Launch configuration.")
    return sku


def check_quota(usages, requirements):
    by_name = {row["name"]["value"].lower(): row for row in usages}
    checks = []
    for name, needed in requirements.items():
        row = by_name.get(name.lower())
        if row is None:
            raise OnboardingError(
                f"Quota {name} was not returned by Azure; cannot verify headroom. "
                "Check subscription Usage + quotas or request access before continuing."
            )
        used, limit = int(row["currentValue"]), int(row["limit"])
        if used + needed > limit:
            raise OnboardingError(
                f"Insufficient {name} quota: used {used}, limit {limit}, need {needed} more. "
                "Request a quota increase in Azure Quotas or choose another approved region/size."
            )
        checks.append({"name": name, "used": used, "limit": limit, "required": needed})
    return checks


def select_compute(config, skus, usages):
    """Rank eligible same-size variants by quota; never replace an explicitly pinned SKU."""
    candidates = profile_candidates(config["compute"], config["size"])
    if config["vmSize"] != "auto":
        candidates = [pinned_profile(config)]
    eligible, evidence = [], []
    for index, profile in enumerate(candidates):
        entry = {"sku": profile["sku"]}
        evidence.append(entry)
        try:
            sku = select_sku(skus, config, profile)
            checks = check_quota(usages, {
                "cores": profile["vcpus"], sku["family"]: profile["vcpus"], "virtualMachines": 1,
            })
        except OnboardingError as exc:
            entry.update(status="Unavailable", reason=str(exc))
            continue
        regional, family = (check["limit"] - check["used"] for check in checks[:2])
        entry.update(
            status="Eligible", family=sku["family"], regionalAvailableVcpus=regional,
            familyAvailableVcpus=family, effectiveAvailableVcpus=min(regional, family),
        )
        rank = (min(regional, family), family, profile.get("generation", 0), -index)
        eligible.append((rank, profile, checks))
    if not eligible:
        reasons = "\n".join(f"{entry['sku']}: {entry['reason']}" for entry in evidence)
        raise OnboardingError(
            f"No eligible {config['compute']} {config['size']} SKU in {config['location']} with sufficient quota:\n{reasons}"
        )
    _, profile, checks = max(eligible, key=lambda candidate: candidate[0])
    return profile, checks, {
        "mode": "QuotaRanked" if config["vmSize"] == "auto" else "Pinned",
        "selectedSku": profile["sku"],
        "ranking": "Effective available vCPUs, then family available vCPUs, then newer generation, then catalog order (Intel before AMD within a generation).",
        "candidates": evidence,
    }


def compute_inventory(subscription, location, compute):
    return (
        az("vm", "list-skus", "--location", location, "--resource-type", "virtualMachines",
           "--size", "Standard_D" if compute == "cpu" else "Standard_NC", "--all", subscription=subscription),
        az("vm", "list-usage", "--location", location, subscription=subscription),
    )


def latest_offer(offers, compute):
    today = dt.date.today()
    candidates = []
    for offer in offers:
        match = re.fullmatch(r"ubuntu-(\d{2})_04-lts", offer["name"])
        if not match:
            continue
        year = int(match[1])
        if year % 2 or (2000 + year, 4) > (today.year, today.month):
            continue
        if compute == "gpu" and f"{year:02d}_04" not in PROFILES["gpuSupportedUbuntuLts"]:
            continue
        candidates.append((year, offer["name"]))
    if not candidates:
        raise OnboardingError("No supported stable Canonical Ubuntu LTS offer was found; review the image catalog.")
    return max(candidates)[1]


def resolve_image(config, pinned=None):
    subscription, location = config["subscriptionId"], config["location"]
    if pinned is None:
        offers = az("vm", "image", "list-offers", "--location", location, "--publisher", "Canonical",
                    subscription=subscription)
        offer = latest_offer(offers, config["compute"])
        skus = az("vm", "image", "list-skus", "--location", location, "--publisher", "Canonical",
                  "--offer", offer, subscription=subscription)
        available = {sku["name"] for sku in skus}
        image_sku = next((name for name in ("server", "server-gen2") if name in available), None)
        if image_sku is None:
            raise OnboardingError(f"No standard server image SKU found in {offer}; do not silently use Pro/ARM/preview.")
        pinned = {"publisher": "Canonical", "offer": offer, "sku": image_sku, "version": "latest"}
    if pinned.get("publisher") != "Canonical" or set(pinned) != {"publisher", "offer", "sku", "version"}:
        raise OnboardingError("Only standard Canonical Ubuntu image references are supported.")
    if pinned["sku"] not in ("server", "server-gen2"):
        raise OnboardingError("Only standard server/server-gen2 image SKUs are supported.")
    if latest_offer([{"name": pinned["offer"]}], config["compute"]) != pinned["offer"]:
        raise OnboardingError("The pinned Ubuntu offer is not supported by this profile.")
    urn = ":".join(pinned[key] for key in ("publisher", "offer", "sku", "version"))
    image = az("vm", "image", "show", "--location", location, "--urn", urn, subscription=subscription)
    if image.get("architecture", "").lower() != "x64" or image.get("hyperVGeneration") != "V2":
        raise OnboardingError("The image must advertise x64 architecture and Generation 2 support.")
    if image.get("plan"):
        raise OnboardingError("This image requires Marketplace terms; use a standard Canonical server image.")
    os_disk = image["osDiskImage"]
    if "sizeInGb" in os_disk:
        if int(os_disk["sizeInGb"]) > 128:
            raise OnboardingError("The image OS disk exceeds the template's 128 GiB OS disk.")
    else:
        print(
            "WARNING: Azure image metadata does not report OS disk size; "
            "compatibility with the 128 GiB OS disk requires ARM validation before deployment.",
            file=sys.stderr,
        )
    features = {entry["name"].lower(): entry["value"] for entry in image.get("features", [])}
    controllers = {value.strip() for value in features.get("diskcontrollertypes", "").split(",")}
    if config["compute"] == "cpu" and "NVMe" not in controllers:
        raise OnboardingError("The CPU image does not advertise NVMe support; review image compatibility.")
    version = image["name"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise OnboardingError("Azure did not return an exact numeric image version.")
    return {**pinned, "version": version}


def check_permissions(config, group_exists):
    scope = f"/subscriptions/{config['subscriptionId']}"
    if group_exists:
        scope += f"/resourceGroups/{config['resourceGroup']}"
    url = f"https://management.azure.com{scope}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
    permissions, seen = [], set()
    while url:
        if urllib.parse.urlsplit(url).scheme != "https" or urllib.parse.urlsplit(url).netloc != "management.azure.com" or url in seen:
            raise OnboardingError("Unexpected or repeated ARM permissions pagination URL.")
        seen.add(url)
        page = az("rest", "--method", "get", "--url", url, subscription=config["subscriptionId"])
        permissions.extend(page["value"])
        url = page.get("nextLink")
    required = [
        "Microsoft.Resources/subscriptions/resourceGroups/write",
        "Microsoft.Resources/deployments/read",
        "Microsoft.Resources/deployments/write",
        "Microsoft.Resources/deployments/validate/action",
        "Microsoft.Resources/deployments/whatIf/action",
        "Microsoft.Compute/virtualMachines/write",
        "Microsoft.Compute/disks/write",
        *[f"Microsoft.Network/{resource}/write" for resource in (
            "virtualNetworks", "virtualNetworks/subnets", "networkInterfaces",
            "networkSecurityGroups", "publicIPAddresses",
            *(("natGateways", "bastionHosts") if config["accessMode"] == "bastion" else ()),
        )],
        *[f"Microsoft.Network/{resource}/join/action" for resource in (
            "virtualNetworks/subnets", "networkInterfaces", "networkSecurityGroups",
            "publicIPAddresses", *(("natGateways",) if config["accessMode"] == "bastion" else ()),
        )],
    ]
    def matches(action, patterns):
        return any(fnmatchcase(action.lower(), pattern.lower()) for pattern in patterns)
    missing = [
        action for action in required if not any(
            matches(action, entry["actions"]) and not matches(action, entry.get("notActions", []))
            for entry in permissions
        )
    ]
    if missing:
        raise OnboardingError(
            f"Missing deployment permissions at {scope}: {', '.join(missing)}. "
            "Ask an administrator to review least-privilege access/PIM activation; no roles were granted."
        )
    return {"scope": scope, "requiredActions": required, "status": "ReportedActionsAllowed"}


def retail_prices(location, profiles, access_mode="bastion"):
    if not re.fullmatch(r"[a-z][a-z0-9]+", location):
        raise OnboardingError("Use a region code such as westus2.")
    # The service rejects long OData URLs; batch at most four exact SKUs per query.
    if len(profiles) > 4:
        names = list(profiles)
        return [
            row for offset in range(0, len(names), 4)
            for row in retail_prices(location, {name: profiles[name] for name in names[offset:offset + 4]}, access_mode)
        ]
    skus = " or ".join(f"armSkuName eq '{profile['sku']}'" for profile in profiles.values())
    regional = (
        f"(serviceName eq 'Virtual Machines' and ({skus})) or "
        "(serviceName eq 'Storage' and (skuName eq 'E70 LRS' or skuName eq 'P10 LRS')) or "
        "(serviceName eq 'Virtual Network' and productName eq 'IP Addresses' and skuName eq 'Standard')"
    )
    if access_mode == "bastion":
        regional += " or (serviceName eq 'Azure Bastion' and skuName eq 'Standard')"
    nat_filter = (
        " or (serviceName eq 'NAT Gateway' and skuName eq 'Standard' and armRegionName eq 'Global')"
        if access_mode == "bastion" else ""
    )
    filters = (
        f"priceType eq 'Consumption' and ((armRegionName eq '{location}' and ({regional})){nat_filter})"
    )
    url = "https://prices.azure.com/api/retail/prices?" + urllib.parse.urlencode({
        "$filter": filters, "currencyCode": "'USD'",
    })
    rows, seen = [], set()
    while url:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "prices.azure.com" or parsed.port not in (None, 443) or url in seen:
            raise OnboardingError("Unexpected or repeated Retail Prices pagination URL.")
        seen.add(url)
        with urllib.request.urlopen(url, timeout=60) as response:
            page = json.load(response)
        rows.extend(page["Items"])
        url = page.get("NextPageLink")
    return rows


def estimate_costs(location, profiles, access_mode="bastion"):
    """Return a live USD PAYG base estimate, not an all-inclusive bill or capacity check."""
    now = dt.datetime.now(dt.timezone.utc)
    if access_mode not in ("bastion", "public-ip"):
        raise OnboardingError("accessMode must be bastion or public-ip.")
    private_access = access_mode == "bastion"
    rows = retail_prices(location, profiles, access_mode)
    def meter(label, unit, region=None, **fields):
        region = region or location
        candidates = [
            row for row in rows
            if row["type"] == "Consumption" and row["currencyCode"] == "USD"
            and row["armRegionName"] == region and row["unitOfMeasure"] == unit
            and row["tierMinimumUnits"] == 0
            and all(row.get(key) == value for key, value in fields.items())
            and not any(word in " ".join(
                row.get(key, "") for key in ("productName", "skuName", "meterName")
            ).lower() for word in ("windows", "spot", "low priority", "lowpriority", "devtest", "red hat", "suse", "ubuntu pro"))
            and dt.datetime.fromisoformat(row["effectiveStartDate"].replace("Z", "+00:00")) <= now
        ]
        primary = [row for row in candidates if row.get("isPrimaryMeterRegion")]
        candidates = primary or candidates
        if candidates:
            latest = max(row["effectiveStartDate"] for row in candidates)
            candidates = [row for row in candidates if row["effectiveStartDate"] == latest]
            candidates = list({(row["meterId"], str(row["retailPrice"])): row for row in candidates}.values())
        if len(candidates) != 1:
            raise OnboardingError(f"Expected one current PAYG meter for {label} in {region}; found {len(candidates)}. Pricing must be verified before approval.")
        row = candidates[0]
        try:
            rate = Decimal(str(row["retailPrice"]))
        except InvalidOperation as exc:
            raise OnboardingError(f"Invalid PAYG rate for {label}.") from exc
        if not rate.is_finite() or rate <= 0:
            raise OnboardingError(f"Invalid PAYG rate for {label}.")
        return row, rate
    def money(value):
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    shared = []
    meters = [
        ("E70 16 TiB Standard SSD LRS", "Storage", "Standard SSD Managed Disks", "E70 LRS Disk", "1/Month", 1, location),
        ("P10 128 GiB Premium SSD LRS OS disk", "Storage", "Premium SSD Managed Disks", "P10 LRS Disk", "1/Month", 1, location),
    ]
    if private_access:
        meters.extend([
            ("Bastion Standard (two included scale units)", "Azure Bastion", "Azure Bastion", "Standard Gateway", "1 Hour", 1, location),
            ("Standard NAT Gateway", "NAT Gateway", "NAT Gateway", "Standard Gateway", "1 Hour", 1, "Global"),
        ])
    meters.append((
        "Two Standard static IPv4 addresses" if private_access else "One Standard static IPv4 address (VM)",
        "Virtual Network", "IP Addresses", "Standard IPv4 Static Public IP", "1 Hour",
        2 if private_access else 1, location,
    ))
    for label, service, product, name, unit, quantity, region in meters:
        row, rate = meter(label, unit, region, serviceName=service, productName=product, meterName=name)
        monthly = rate * quantity * (730 if unit == "1 Hour" else 1)
        shared.append({
            "resource": label, "quantity": quantity, "unit": unit, "unitPriceUsd": float(rate),
            "monthlyUsd": money(monthly), "meterId": row["meterId"],
            "effectiveStartDate": row["effectiveStartDate"], "meterRegion": row["armRegionName"],
        })
    fixed = sum(Decimal(str(line["monthlyUsd"])) for line in shared)
    sizes = {}
    for size, profile in profiles.items():
        row, rate = meter(profile["sku"], "1 Hour", serviceName="Virtual Machines", armSkuName=profile["sku"])
        sizes[size] = {
            **profile, "vmHourlyUsd": float(rate), "vmMonthlyUsd": money(rate * 730),
            "baseMonthlyUsd": money(rate * 730 + fixed),
            "meterId": row["meterId"], "effectiveStartDate": row["effectiveStartDate"],
        }
    return {
        "checkedAtUtc": now.isoformat(), "source": "https://prices.azure.com/api/retail/prices",
        "location": location, "accessMode": access_mode,
        "currency": "USD", "priceType": "Consumption", "hoursPerMonth": 730,
        "sharedResources": shared, "sharedMonthlyUsd": money(fixed), "profiles": sizes,
        "assumptions": [
            "Base estimate assumes 730 VM hours/month and the fixed infrastructure exists all month.",
            *([
                "Bastion base price includes two scale units; do not multiply it by two.",
                "NAT uses Azure's explicitly Global public-cloud meter, not a different region's price.",
            ] if private_access else ["Demo mode has one NIC-attached Standard static public IP; no Bastion or NAT."]),
            "Disks and deployed network services/public IPs keep billing when the VM is deallocated.",
            "No Spot, reservations, savings plans, negotiated discounts, credits or taxes.",
        ],
        "excludedUsage": [
            *([
                "NAT processing of outbound AND return traffic.",
                "Bastion data transfer.",
            ] if private_access else []),
            "Azure bandwidth charges and Standard SSD disk transactions.",
            "Backups, extra monitoring/log storage and any additional services.",
        ],
    }


def preflight(config, pinned_image=None):
    validate_access(config["accessMode"], config["sshSourceCidr"])
    private_access = config["accessMode"] == "bastion"
    subscription, location = config["subscriptionId"], config["location"]
    if az("cloud", "show")["name"] != "AzureCloud":
        raise OnboardingError("These public-cloud AVM/image profiles currently support AzureCloud only.")
    account = az("account", "show", subscription=subscription)
    if account["state"] != "Enabled":
        raise OnboardingError("The selected subscription is not enabled.")
    regions = az(
        "rest", "--method", "get", "--url",
        f"https://management.azure.com/subscriptions/{subscription}/locations?api-version=2022-12-01",
        subscription=subscription,
    )["value"]
    region = next((region for region in regions if region["name"] == location), None)
    if region is None:
        raise OnboardingError(f"{location} is not an available subscription region.")
    services = {
        "Microsoft.Compute": ("virtualMachines", "disks"),
        "Microsoft.Network": ("virtualNetworks", "publicIPAddresses", "networkSecurityGroups",
                              "networkInterfaces", *(("bastionHosts", "natGateways") if private_access else ())),
    }
    for namespace, resource_types in services.items():
        provider = az("provider", "show", "--namespace", namespace, subscription=subscription)
        if provider["registrationState"] != "Registered":
            raise OnboardingError(
                f"{namespace} is not registered. With permission, run: "
                f"az provider register --namespace {namespace} --subscription {subscription} --wait"
            )
        for resource_type in resource_types:
            entry = next((entry for entry in provider["resourceTypes"]
                          if entry["resourceType"].lower() == resource_type.lower()), None)
            if entry is None or region["displayName"].lower() not in {
                name.lower() for name in entry.get("locations", [])
            }:
                raise OnboardingError(f"{namespace}/{resource_type} does not advertise support in {location}.")
    feature = az("feature", "show", "--namespace", "Microsoft.Compute", "--name", "EncryptionAtHost",
                 subscription=subscription)
    feature_state = feature.get("properties", {}).get("state")
    if feature_state != "Registered":
        raise OnboardingError(
            f"EncryptionAtHost feature state is {feature_state!r}; Registered is required. "
            "With permission, register it, then wait until feature show reports Registered:\n"
            f"az feature register --namespace Microsoft.Compute --name EncryptionAtHost --subscription {subscription}\n"
            f"az feature show --namespace Microsoft.Compute --name EncryptionAtHost --subscription {subscription}"
        )
    group_exists = az("group", "exists", "--name", config["resourceGroup"], subscription=subscription)
    if group_exists:
        resources = az("resource", "list", "--resource-group", config["resourceGroup"], subscription=subscription)
        if resources:
            raise OnboardingError("Use a new or empty resource group. This flow does not adopt or resize existing resources.")
    permission_report = check_permissions(config, group_exists)
    skus, usages = compute_inventory(subscription, location, config["compute"])
    profile, quotas, selection = select_compute(config, skus, usages)
    network_usages = az("network", "list-usages", "--location", location, subscription=subscription)
    ip_count = 2 if private_access else 1
    network_needs = {
        "virtualnetworks": 1, "networksecuritygroups": 1, "networkinterfaces": 1,
        "publicipaddresses": ip_count, "publicipaddressesstandard": ip_count,
        "standardpublicipaddresses": ip_count, "staticpublicipaddresses": ip_count,
    }
    if private_access:
        network_needs.update(natgateways=1, bastionhosts=1)
    requirements = {}
    for row in network_usages:
        normalized = re.sub(r"[^a-z]", "", row["name"]["value"].lower())
        if normalized in network_needs:
            requirements[row["name"]["value"]] = network_needs[normalized]
    if not any("publicip" in name.lower() for name in requirements):
        raise OnboardingError("Azure did not return public IP quota; verify Standard public IP headroom before retrying.")
    quotas.extend(check_quota(network_usages, requirements))
    image = resolve_image(config, pinned_image)
    return {
        "checkedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "subscriptionId": subscription,
        "resourceGroup": config["resourceGroup"],
        "location": location,
        "owner": config["owner"],
        "accessMode": config["accessMode"],
        "sshSourceCidr": config["sshSourceCidr"],
        "permissionCheck": permission_report,
        "profile": profile,
        "skuSelection": selection,
        "imageReference": image,
        "hostEncryption": {
            "enabled": True,
            "keyManagement": "PlatformManaged",
            "subscriptionFeatureState": feature_state,
            "skuSupported": True,
        },
        "quotaChecks": quotas,
        "limitations": [
            "Quota and SKU eligibility do not reserve physical capacity.",
            "Only network quota counters returned by Azure were checked; deployment validation is still required.",
            "Deny assignments/conditional access, Azure Policy and billing restrictions require separate review and ARM validation.",
            "Disk capacity and service-specific limits are finally checked by ARM validation/allocation.",
            "Azure image metadata may omit OS disk size; compatibility with the 128 GiB OS disk requires ARM validation.",
        ],
    }


def cloud_init(compute):
    scripts = ["docker"] + (["gpu"] if compute == "gpu" else [])
    packages = ["ca-certificates", "curl"] + (["build-essential", "dkms"] if compute == "gpu" else [])
    files = [
        {
            "path": f"/usr/local/sbin/onboarding-{script}-setup",
            "owner": "root:root", "permissions": "0700",
            "content": (ASSETS / "infra" / f"{script}-setup.sh").read_text(encoding="utf-8"),
        }
        for script in scripts
    ]
    config = {
        "package_update": True, "packages": packages, "write_files": files,
        "runcmd": [["bash", "-e", "-c", "\n".join(entry["path"] for entry in files)]],
    }
    # JSON is valid YAML and safely encodes shell scripts without indentation/substitution hazards.
    return "#cloud-config\n" + json.dumps(config, indent=2) + "\n"


def parameters(config, image, public_key):
    values = {
        "location": config["location"], "namePrefix": config["namePrefix"],
        "adminUsername": config["adminUsername"], "sshPublicKey": public_key,
        "vmSize": pinned_profile(config)["sku"],
        "computeMode": config["compute"], "imageReference": image,
        "encryptionAtHost": True,
        "owner": config["owner"],
        "accessMode": config["accessMode"], "sshSourceCidr": config["sshSourceCidr"],
        "vnetCidr": config["vnetCidr"], "bastionSubnetCidr": config["bastionSubnetCidr"],
        "vmSubnetCidr": config["vmSubnetCidr"],
        "customData": cloud_init(config["compute"]),
    }
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in values.items()},
    }


def refresh_modules(template):
    pattern = r"br/public:avm/res/([a-z0-9/-]+):(\d+\.\d+\.\d+)"
    versions = {}
    for module in sorted({match[0] for match in re.findall(pattern, template)}):
        url = f"https://mcr.microsoft.com/v2/bicep/avm/res/{module}/tags/list"
        with urllib.request.urlopen(url, timeout=30) as response:
            tags = json.load(response)["tags"]
        stable = [tag for tag in tags if re.fullmatch(r"\d+\.\d+\.\d+", tag)]
        if not stable:
            raise OnboardingError(f"No stable published AVM version found for {module}.")
        versions[module] = max(stable, key=lambda tag: tuple(map(int, tag.split("."))))
    if not versions:
        raise OnboardingError("The template contains no versioned AVM references.")
    return re.sub(pattern, lambda match: f"br/public:avm/res/{match[1]}:{versions[match[1]]}", template), versions


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def generate(config, report, public_key, output):
    if output.exists():
        raise OnboardingError(f"Refusing to overwrite {output}. Choose a new output directory.")
    config = {**config, "vmSize": report["profile"]["sku"]}
    template, versions = refresh_modules((ASSETS / "infra" / "main.bicep").read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="onboarding-", dir=output.parent) as temporary:
        stage = Path(temporary) / "environment"
        shutil.copytree(ASSETS / "infra", stage / "infra")
        (stage / "infra" / "main.bicep").write_text(template, encoding="utf-8")
        write_json(stage / "config.json", config)
        generated_parameters = parameters(config, report["imageReference"], public_key)
        write_json(stage / "main.parameters.json", generated_parameters)
        (stage / "infra" / "cloud-init.yaml").write_text(
            generated_parameters["parameters"]["customData"]["value"], encoding="utf-8"
        )
        write_json(stage / "preflight.json", report)
        write_json(stage / "cost-estimate.json", report["costEstimate"])
        write_json(stage / "avm-versions.json", versions)
        command(["az", "bicep", "build", "--file", stage / "infra" / "main.bicep",
                 "--outfile", stage / "main.arm.json"])
        (stage / "main.arm.json").unlink()
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "check", "prices", "prerequisites"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--location", default=PROFILES["defaultLocation"], help="Region for prices.")
    parser.add_argument("--compute", choices=("cpu", "gpu", "all"), default="all", help="Profile family for prices.")
    parser.add_argument("--subscription", help="For prices: select quota-eligible SKUs using this subscription UUID.")
    parser.add_argument("--access-mode", choices=("bastion", "public-ip"),
                        help="For prices: bastion plus NAT (default), or public-ip demo. generate/check use config.")
    parser.add_argument("--output", type=Path, default=Path(".azure-vm") / "dev")
    parser.add_argument("--parameters", type=Path, help="Recheck an existing generated parameter file without upgrading its image.")
    args = parser.parse_args()
    try:
        if args.action == "prerequisites":
            if args.config or args.parameters:
                raise OnboardingError("prerequisites requires neither --config nor --parameters.")
            report = prerequisites()
            print(json.dumps(report, indent=2))
            return 0 if report["azureCli"]["installed"] else 2
        if args.action == "prices":
            if args.config or args.parameters:
                raise OnboardingError("prices uses --location/--compute, not --config/--parameters.")
            profiles, selections = {}, {}
            if args.subscription:
                args.subscription = str(uuid.UUID(args.subscription))
                if not re.fullmatch(r"[a-z][a-z0-9]+", args.location):
                    raise OnboardingError("Use a region code such as westus2.")
            for compute in ("cpu", "gpu"):
                if args.compute not in ("all", compute):
                    continue
                if args.subscription:
                    skus, usages = compute_inventory(args.subscription, args.location, compute)
                for size in PROFILES[compute]:
                    name = f"{compute}-{size}"
                    if args.subscription:
                        config = {**DEFAULTS, "compute": compute, "size": size, "location": args.location}
                        try:
                            profile, _, selection = select_compute(config, skus, usages)
                        except OnboardingError as exc:
                            selections[name] = {"status": "Unavailable", "reason": str(exc)}
                            continue
                        profiles[name] = profile
                        selections[name] = {"status": "Eligible", **selection}
                    else:
                        for profile in profile_candidates(compute, size):
                            profiles[f"{name}-{profile['sku']}" if compute == "cpu" else name] = profile
            if not profiles:
                raise OnboardingError("No quota-eligible T-shirt options:\n" + json.dumps(selections, indent=2))
            report = estimate_costs(args.location, profiles, args.access_mode or DEFAULTS["accessMode"])
            if args.subscription:
                report.update(subscriptionId=args.subscription, skuSelection=selections)
            print(json.dumps(report, indent=2))
            return 0
        if not args.config:
            raise OnboardingError("--config is required for generate/check.")
        if args.subscription:
            raise OnboardingError("generate/check use the subscriptionId in --config, not --subscription.")
        if args.access_mode:
            raise OnboardingError("generate/check use accessMode in --config, not --access-mode.")
        config = read_config(args.config.resolve())
        public_key = read_public_key(config["sshPublicKeyPath"])
        pinned = None
        existing = None
        if args.parameters:
            if args.action != "check":
                raise OnboardingError("--parameters is only valid with check.")
            existing = json.loads(args.parameters.read_text(encoding="utf-8"))
            pinned = existing["parameters"]["imageReference"]["value"]
            if pinned["version"] == "latest":
                raise OnboardingError("Generated parameters must pin an exact Ubuntu image version.")
            if config["vmSize"] == "auto":
                raise OnboardingError("Rechecking generated parameters requires a pinned vmSize in config.json. Regenerate and review older output.")
        report = preflight(config, pinned)
        resolved_config = {**config, "vmSize": report["profile"]["sku"]}
        if existing is not None and existing != parameters(resolved_config, report["imageReference"], public_key):
            raise OnboardingError("Parameters differ from the approved config/key/profile. Regenerate in a new directory.")
        report["costEstimate"] = estimate_costs(config["location"], {
            config["size"]: report["profile"],
        }, config["accessMode"])
        if args.action == "generate":
            generate(config, report, public_key, args.output.resolve())
        print(json.dumps(report, indent=2))
        if args.action == "generate":
            print(f"\nGenerated {args.output}. No Azure resources were created. Review costs and run ARM validation/what-if.")
        return 0
    except (OnboardingError, OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
