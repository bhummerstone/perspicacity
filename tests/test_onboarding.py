import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".github" / "skills" / "perspicacity-azure-onboard"
SPEC = importlib.util.spec_from_file_location("onboarding", SKILL / "scripts" / "onboarding.py")
onboarding = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(onboarding)

CONFIG = {
    **onboarding.DEFAULTS,
    "vmSize": "Standard_D16ds_v7",
    "subscriptionId": "11111111-1111-4111-8111-111111111111",
    "resourceGroup": "rg-test",
    "namePrefix": "test-vm",
    "adminUsername": "azureuser",
    "owner": "research-team",
    "sshPublicKeyPath": str(ROOT / "test.pub"),
}
PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(
    b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
).decode("ascii") + " test-fixture"
IMAGE = {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "24.04.202610010"}


def usage(name, current=0, limit=1000):
    return {"name": {"value": name}, "currentValue": current, "limit": limit}


def reference_profiles(compute):
    return {size: onboarding.profile_candidates(compute, size)[0] for size in onboarding.PROFILES[compute]}


def sku_for(config, profile=None):
    profile = profile or reference_profiles(config["compute"])[config["size"]]
    return {
        "name": profile["sku"],
        "family": "testFamily",
        "restrictions": [],
        "capabilities": [
            {"name": "HyperVGenerations", "value": "V1,V2"},
            {"name": "CpuArchitectureType", "value": "x64"},
            {"name": "vCPUs", "value": str(profile["vcpus"])},
            {"name": "MemoryGB", "value": str(profile["memoryGiB"])},
            {"name": "DiskControllerTypes", "value": "SCSI,NVMe"},
            {"name": "EncryptionAtHostSupported", "value": "True"},
        ],
    }


class PrerequisiteTests(unittest.TestCase):
    def probe(self, platform, tools=(), version="2.99.0"):
        with patch.object(onboarding.sys, "platform", platform), patch.object(
            onboarding.shutil, "which", side_effect=lambda name: name if name in tools else None
        ), patch.object(onboarding, "az", return_value={"azure-cli": version}) as azure:
            report = onboarding.prerequisites()
        if "az" in tools:
            azure.assert_called_once_with("version")
        else:
            azure.assert_not_called()
        return report

    def test_installed_cli_is_verified_without_login_or_upgrade(self):
        report = self.probe("win32", ("az",))
        self.assertEqual(report, {
            "status": "AzureCliReady", "azureCli": {"installed": True, "version": "2.99.0"},
        })
        for version in ("", None):
            with self.subTest(version=version), self.assertRaises(onboarding.OnboardingError):
                self.probe("win32", ("az",), version)

    def test_missing_windows_cli_proposes_latest_exact_package_without_installing(self):
        with patch.object(onboarding, "command", side_effect=AssertionError("Must not install")):
            report = self.probe("win32", ("winget",))
        self.assertEqual(report["status"], "AwaitingAzureCliInstallConsent")
        self.assertTrue(report["installation"]["requiresConsent"])
        self.assertEqual(report["installation"]["commands"], [
            ["winget", "install", "--exact", "--id", "Microsoft.AzureCLI", "--source", "winget"],
        ])
        self.assertIn("question tool", report["nextStep"])

    def test_missing_package_manager_requires_official_guidance_not_automatic_bootstrap(self):
        for platform, suffix in (("win32", "-windows"), ("darwin", "-macos"), ("linux", "-linux"), ("unknown", "")):
            with self.subTest(platform=platform):
                report = self.probe(platform)
                self.assertEqual(report["installation"]["commands"], [])
                self.assertEqual(report["installation"]["documentation"],
                                 "https://learn.microsoft.com/cli/azure/install-azure-cli" + suffix)

    def test_mac_proposes_brew_update_then_latest_install(self):
        report = self.probe("darwin", ("brew",))
        self.assertEqual(report["installation"]["commands"], [
            ["brew", "update"], ["brew", "install", "azure-cli"],
        ])

    def test_prerequisites_action_needs_no_config_or_key_and_reports_pending_consent(self):
        for installed in (False, True):
            with self.subTest(installed=installed), patch(
                "sys.argv", ["onboarding.py", "prerequisites"]
            ), patch.object(onboarding, "prerequisites", return_value={"azureCli": {"installed": installed}}), patch.object(
                onboarding, "read_config", side_effect=AssertionError("Must not need config")
            ), patch.object(onboarding, "read_public_key", side_effect=AssertionError("Must not need key")), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(onboarding.main(), 0 if installed else 2)
                self.assertEqual(json.loads(output.getvalue())["azureCli"]["installed"], installed)

    def test_broken_cli_surfaces_error_and_is_not_reinstalled(self):
        with patch("sys.argv", ["onboarding.py", "prerequisites"]), patch.object(
            onboarding.shutil, "which", return_value="az"
        ), patch.object(onboarding, "az", side_effect=onboarding.OnboardingError("CLI failed")), redirect_stdout(
            io.StringIO()
        ) as output, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(onboarding.main(), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("CLI failed", error.getvalue())

    def test_direct_missing_az_error_explains_install_consent_and_recovery(self):
        with patch.object(onboarding.shutil, "which", return_value=None), patch.object(
            onboarding.subprocess, "run", side_effect=AssertionError("Must not install")
        ), self.assertRaisesRegex(onboarding.OnboardingError, "permission.*prerequisites"):
            onboarding.command(["az", "account", "show"])


class NetworkTests(unittest.TestCase):
    def test_defaults_are_disjoint_private_subnets(self):
        networks = onboarding.validate_networks(CONFIG)
        self.assertEqual(networks["vnetCidr"].num_addresses, 256)
        self.assertEqual(networks["bastionSubnetCidr"].num_addresses, 64)
        self.assertFalse(networks["bastionSubnetCidr"].overlaps(networks["vmSubnetCidr"]))

    def test_invalid_networks_fail(self):
        changes = [
            {"vnetCidr": "192.168.0.1/24"},
            {"vnetCidr": "8.8.8.0/24"},
            {"vnetCidr": "127.0.0.0/8"},
            {"vnetCidr": "fd00::/64"},
            {"bastionSubnetCidr": "192.168.0.0/27"},
            {"vmSubnetCidr": "192.168.1.0/26"},
            {"vmSubnetCidr": "192.168.0.0/26"},
            {"vmSubnetCidr": "192.168.0.64/30"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(onboarding.OnboardingError):
                onboarding.validate_networks({**CONFIG, **change})

    def test_custom_private_network(self):
        result = onboarding.validate_networks({
            **CONFIG, "vnetCidr": "10.20.0.0/24",
            "bastionSubnetCidr": "10.20.0.0/26", "vmSubnetCidr": "10.20.0.128/25",
        })
        self.assertEqual(str(result["vmSubnetCidr"]), "10.20.0.128/25")


class ConfigTests(unittest.TestCase):
    def read(self, changes=None, remove=()):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            config = {**CONFIG, **(changes or {})}
            for key in remove:
                config.pop(key)
            path.write_text(json.dumps(config), encoding="utf-8")
            return onboarding.read_config(path)

    def test_omitted_options_use_approved_defaults(self):
        config = self.read(remove=tuple(onboarding.DEFAULTS))
        self.assertEqual(config["location"], "westus2")
        self.assertEqual(config["size"], "small")
        self.assertEqual(config["compute"], "cpu")
        self.assertEqual(config["vmSize"], "auto")

    def test_relative_public_key_path_is_relative_to_config(self):
        config = self.read({"sshPublicKeyPath": "keys/test.pub"})
        self.assertTrue(Path(config["sshPublicKeyPath"]).is_absolute())
        self.assertEqual(Path(config["sshPublicKeyPath"]).parts[-2:], ("keys", "test.pub"))

    def test_unknown_fields_missing_fields_and_bad_values_fail(self):
        changes = [
            {"region": "eastus"},
            {"subscriptionId": "not-a-uuid"},
            {"subscriptionId": "00000000-0000-0000-0000-000000000000"},
            {"adminUsername": "root"},
            {"adminUsername": "azureuser;bad"},
            {"location": "West US 2"},
            {"location": "westus2&bad"},
            {"resourceGroup": "rg&bad"},
            {"namePrefix": "../bad"},
            {"size": "xxl"},
            {"compute": "accelerator"},
            {"vmSize": "Standard_D16s_v7"},
            {"vmSize": "Standard_D32ds_v7"},
            {"vmSize": "Standard_D16ds_v5"},
            {"sshPublicKeyPath": None},
            {"owner": ""},
            {"owner": " team"},
            {"owner": "team&bad"},
            {"owner": "x" * 129},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(onboarding.OnboardingError):
                self.read(change)
        with self.assertRaises(onboarding.OnboardingError):
            self.read(remove=("adminUsername",))
        with self.assertRaises(onboarding.OnboardingError):
            self.read(remove=("owner",))

    def test_public_key_is_checked_by_openssh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.pub"
            path.write_text(PUBLIC_KEY + "\n", encoding="utf-8")
            with patch.object(onboarding, "command", return_value="256 SHA256:test test (ED25519)") as run:
                self.assertEqual(onboarding.read_public_key(path), PUBLIC_KEY)
                run.assert_called_once_with(["ssh-keygen", "-l", "-f", path])

    def test_private_malformed_or_multiple_keys_rejected(self):
        invalid = [
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "ssh-ed25519 !!!",
            "ssh-ed25519 YQ==",
            PUBLIC_KEY + "\n" + PUBLIC_KEY,
            PUBLIC_KEY.replace("ssh-ed25519", "ssh-rsa", 1),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.pub"
            for text in invalid:
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text[:30]), self.assertRaises(onboarding.OnboardingError):
                    onboarding.read_public_key(path)


class ProfileTests(unittest.TestCase):
    def test_agreed_cpu_mappings_and_per_device_totals(self):
        for size, cpus, count, per_disk, total in (
            ("small", 16, 2, 440, 880), ("medium", 32, 4, 440, 1760),
            ("large", 64, 4, 880, 3520), ("xl", 128, 4, 1760, 7040),
        ):
            profile = onboarding.PROFILES["cpu"][size]
            candidates = onboarding.profile_candidates("cpu", size)
            self.assertEqual(candidates[0]["sku"], f"Standard_D{cpus}ds_v7")
            expected = {f"Standard_D{cpus}{suffix}" for suffix in ("ds_v7", "ads_v7", "ds_v6", "ads_v6")}
            if size == "xl":
                expected.remove("Standard_D128ads_v6")
            self.assertEqual({candidate["sku"] for candidate in candidates}, expected)
            self.assertEqual(profile["memoryGiB"], cpus * 4)
            self.assertEqual((profile["nvmeDevices"], profile["nvmeGiBPerDevice"]), (count, per_disk))
            self.assertEqual(count * per_disk, total)

    def test_gpu_profiles_are_ga_sizes_not_retired_preview_names(self):
        for size, cpus, gpus, vram, nvme in (
            ("small", 36, 0.25, 24, 1600), ("medium", 72, 0.5, 48, 3200),
            ("large", 144, 1, 96, 6400), ("xl", 288, 2, 192, 12800),
        ):
            profile = onboarding.PROFILES["gpu"][size]
            self.assertEqual(profile["sku"], f"Standard_NC{cpus}ds_xl_RTXPRO6000BSE_v6")
            self.assertEqual((profile["gpuCount"], profile["gpuMemoryGB"], profile["localNvmeGiB"]),
                             (gpus, vram, nvme))

    def test_all_profiles_accept_the_exact_live_sku(self):
        for compute in ("cpu", "gpu"):
            for size in ("small", "medium", "large", "xl"):
                config = {**CONFIG, "compute": compute, "size": size}
                sku = sku_for(config)
                self.assertEqual(onboarding.select_sku([sku], config, reference_profiles(compute)[size]), sku)

    def test_location_restrictions_fail_but_zone_only_restrictions_do_not(self):
        sku = sku_for(CONFIG)
        sku["restrictions"] = [{"type": "Location", "reasonCode": "NotAvailableForSubscription"}]
        with self.assertRaisesRegex(onboarding.OnboardingError, "restricted"):
            onboarding.select_sku([sku], CONFIG, reference_profiles("cpu")["small"])
        sku["restrictions"] = [{"type": "Zone", "reasonCode": "NotAvailableForSubscription"}]
        self.assertEqual(onboarding.select_sku([sku], CONFIG, reference_profiles("cpu")["small"]), sku)

    def test_missing_or_changed_sku_capabilities_fail(self):
        with self.assertRaisesRegex(onboarding.OnboardingError, "not offered"):
            onboarding.select_sku([], CONFIG, reference_profiles("cpu")["small"])
        for name, value in (("HyperVGenerations", "V1"), ("CpuArchitectureType", "Arm64"),
                            ("vCPUs", "32"), ("MemoryGB", "128"), ("DiskControllerTypes", "SCSI")):
            sku = sku_for(CONFIG)
            for cap in sku["capabilities"]:
                if cap["name"] == name:
                    cap["value"] = value
            with self.subTest(name=name), self.assertRaises(onboarding.OnboardingError):
                onboarding.select_sku([sku], CONFIG, reference_profiles("cpu")["small"])

    def test_host_encryption_support_is_required_for_every_profile(self):
        for compute in ("cpu", "gpu"):
            for size in ("small", "medium", "large", "xl"):
                config = {**CONFIG, "compute": compute, "size": size}
                for value in ("False", "", None):
                    sku = sku_for(config)
                    cap = next(cap for cap in sku["capabilities"] if cap["name"] == "EncryptionAtHostSupported")
                    if value is None:
                        sku["capabilities"].remove(cap)
                    else:
                        cap["value"] = value
                    with self.subTest(compute=compute, size=size, capability=value), self.assertRaisesRegex(
                        onboarding.OnboardingError, "EncryptionAtHostSupported=True"
                    ):
                        onboarding.select_sku([sku], config, reference_profiles(compute)[size])

    def test_host_encryption_capability_value_is_case_insensitive(self):
        sku = sku_for(CONFIG)
        next(cap for cap in sku["capabilities"] if cap["name"] == "EncryptionAtHostSupported")["value"] = "true"
        self.assertEqual(onboarding.select_sku([sku], CONFIG, reference_profiles("cpu")["small"]), sku)


class QuotaTests(unittest.TestCase):
    def test_exact_threshold_is_allowed(self):
        checks = onboarding.check_quota([usage("cores", 84, 100)], {"cores": 16})
        self.assertEqual(checks, [{"name": "cores", "used": 84, "limit": 100, "required": 16}])

    def test_over_limit_or_missing_counter_fails(self):
        for rows in ([usage("cores", 85, 100)], []):
            with self.subTest(rows=rows), self.assertRaises(onboarding.OnboardingError):
                onboarding.check_quota(rows, {"cores": 16})

    def test_zero_gpu_family_quota_is_not_ignored(self):
        with self.assertRaisesRegex(onboarding.OnboardingError, "Insufficient"):
            onboarding.check_quota([usage("gpuFamily", 0, 0)], {"gpuFamily": 36})


class CpuSelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {**CONFIG, "vmSize": "auto"}
        self.profiles = onboarding.profile_candidates("cpu", "small")
        self.skus = [
            {**sku_for(self.config, profile), "family": f"family{index}"}
            for index, profile in enumerate(self.profiles)
        ]
        self.usages = [usage("cores", 0, 1000), usage("virtualMachines", 0, 100)]
        self.usages.extend(usage(f"family{index}", 0, 100) for index in range(4))

    def choose(self, config=None):
        return onboarding.select_compute(config or self.config, self.skus, self.usages)

    def test_each_intel_amd_v6_v7_variant_can_win_on_available_family_quota(self):
        for index, candidate in enumerate(self.profiles):
            with self.subTest(sku=candidate["sku"]):
                row = self.usages[2 + index]
                row["limit"] = 200
                profile, checks, report = self.choose()
                self.assertEqual(profile, candidate)
                self.assertEqual(checks[1]["name"], f"family{index}")
                self.assertEqual(report["mode"], "QuotaRanked")
                self.assertEqual(report["selectedSku"], candidate["sku"])
                self.assertEqual(len(report["candidates"]), 4)
                row["limit"] = 100

    def test_usage_not_limit_alone_determines_winner(self):
        self.usages[2] = usage("family0", 199, 200)
        self.usages[3] = usage("family1", 10, 100)
        self.usages[4] = usage("family2", 0, 150)
        profile, _, report = self.choose()
        self.assertEqual(profile["sku"], "Standard_D16ds_v6")
        self.assertEqual(report["candidates"][0]["status"], "Unavailable")
        self.assertEqual(report["candidates"][2]["effectiveAvailableVcpus"], 150)

    def test_regional_cap_then_family_headroom_then_generation_and_stable_order_break_ties(self):
        self.usages[0]["limit"] = 16
        self.usages[4]["limit"] = 101
        profile, _, report = self.choose()
        self.assertEqual(profile["generation"], 6)
        self.assertTrue(all(entry["effectiveAvailableVcpus"] == 16 for entry in report["candidates"]))
        self.usages[4]["limit"] = 100
        for sku in self.skus:
            sku["capabilities"] = list(reversed(sku["capabilities"]))
        self.skus.reverse()
        self.assertEqual(self.choose()[0]["sku"], "Standard_D16ds_v7")
        self.usages[2]["limit"] = 0
        self.assertEqual(self.choose()[0]["sku"], "Standard_D16ads_v7")

    def test_restricted_missing_incompatible_and_insufficient_candidates_are_explained(self):
        self.skus[0]["restrictions"] = [{"type": "Location", "reasonCode": "NotAvailableForSubscription"}]
        self.skus.pop(1)
        self.usages[4]["limit"] = 0
        profile, _, report = self.choose()
        self.assertEqual(profile["sku"], "Standard_D16ads_v6")
        reasons = {entry["sku"]: entry.get("reason", "") for entry in report["candidates"]}
        self.assertIn("restricted", reasons["Standard_D16ds_v7"])
        self.assertIn("not offered", reasons["Standard_D16ads_v7"])
        self.assertIn("Insufficient", reasons["Standard_D16ds_v6"])

    def test_security_and_hardware_requirements_are_not_relaxed_to_get_more_quota(self):
        for name, value in (
            ("EncryptionAtHostSupported", "False"), ("HyperVGenerations", "V1"),
            ("CpuArchitectureType", "Arm64"), ("DiskControllerTypes", "SCSI"),
            ("vCPUs", "8"), ("MemoryGB", "32"), ("TrustedLaunchDisabled", "True"),
        ):
            with self.subTest(capability=name):
                skus = copy.deepcopy(self.skus)
                skus[0]["capabilities"] = [
                    cap for cap in skus[0]["capabilities"] if cap["name"] != name
                ] + [{"name": name, "value": value}]
                self.usages[2]["limit"] = 10000
                profile, _, report = onboarding.select_compute(self.config, skus, self.usages)
                self.assertEqual(profile["sku"], "Standard_D16ads_v7")
                self.assertEqual(report["candidates"][0]["status"], "Unavailable")

    def test_unknown_family_quota_is_not_assumed_unlimited(self):
        self.usages = self.usages[:2] + self.usages[3:]
        profile, _, report = self.choose()
        self.assertEqual(profile["sku"], "Standard_D16ads_v7")
        self.assertIn("not returned", report["candidates"][0]["reason"])

    def test_exact_thresholds_and_case_insensitive_quota_names(self):
        self.usages = [usage("CORES", 84, 100), usage("VIRTUALMACHINES", 99, 100), usage("FAMILY3", 84, 100)]
        self.assertEqual(self.choose()[0]["sku"], "Standard_D16ads_v6")
        self.usages[-1]["currentValue"] = 85
        with self.assertRaisesRegex(onboarding.OnboardingError, "No eligible"):
            self.choose()

    def test_regional_and_vm_count_limits_cannot_be_bypassed_by_switching_family(self):
        for row in self.usages[:2]:
            limit = row["limit"]
            row["limit"] = 0
            with self.subTest(quota=row["name"]["value"]), self.assertRaisesRegex(
                onboarding.OnboardingError, f"Insufficient {row['name']['value']} quota"
            ):
                self.choose()
            row["limit"] = limit

    def test_pinned_sku_is_rechecked_not_replaced_when_other_family_is_better(self):
        config = {**self.config, "vmSize": "Standard_D16ads_v6"}
        profile, _, report = self.choose(config)
        self.assertEqual(profile["sku"], config["vmSize"])
        self.assertEqual(report["mode"], "Pinned")
        self.assertEqual(len(report["candidates"]), 1)
        self.usages[-1]["limit"] = 0
        with self.assertRaisesRegex(onboarding.OnboardingError, "No eligible"):
            self.choose(config)

    def test_xl_never_invents_unsupported_amd_v6_size_or_downsizes(self):
        config = {**self.config, "size": "xl"}
        candidates = onboarding.profile_candidates("cpu", "xl")
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(profile["vcpus"] == 128 and profile["memoryGiB"] == 512 for profile in candidates))
        with self.assertRaisesRegex(onboarding.OnboardingError, "No eligible"):
            self.choose(config)
        with self.assertRaisesRegex(onboarding.OnboardingError, "exact catalog SKU"):
            self.choose({**config, "vmSize": "Standard_D128ads_v6"})

    def test_gpu_keeps_its_existing_exact_profile(self):
        config = {**self.config, "compute": "gpu"}
        sku = sku_for(config)
        profile, _, report = onboarding.select_compute(
            config, [sku], [usage("cores"), usage("testFamily"), usage("virtualMachines")]
        )
        self.assertEqual(profile, onboarding.PROFILES["gpu"]["small"])
        self.assertEqual(len(report["candidates"]), 1)


class ImageTests(unittest.TestCase):
    def test_newest_stable_lts_excludes_preview_pro_non_lts_and_future(self):
        offers = [{"name": name} for name in (
            "ubuntu-22_04-lts", "ubuntu-24_04-lts", "ubuntu-24_04-lts-preview",
            "ubuntu-24_04-lts-pro", "ubuntu-25_04-lts", "ubuntu-98_04-lts",
        )]
        self.assertEqual(onboarding.latest_offer(offers, "cpu"), "ubuntu-24_04-lts")

    def test_gpu_does_not_take_an_unsupported_newer_lts(self):
        offers = [{"name": "ubuntu-24_04-lts"}, {"name": "ubuntu-26_04-lts"}]
        self.assertEqual(onboarding.latest_offer(offers, "gpu"), "ubuntu-24_04-lts")

    def image(self):
        return {
            "name": IMAGE["version"], "architecture": "x64", "hyperVGeneration": "V2",
            "osDiskImage": {"sizeInGb": 30},
            "features": [{"name": "DiskControllerTypes", "value": "SCSI,NVMe"}],
        }

    def test_latest_resolves_to_exact_version(self):
        with patch.object(onboarding, "az", side_effect=[
            [{"name": IMAGE["offer"]}], [{"name": "server"}], self.image(),
        ]):
            self.assertEqual(onboarding.resolve_image(CONFIG), IMAGE)

    def test_pinned_image_is_revalidated_without_resolving_latest(self):
        with patch.object(onboarding, "az", return_value=self.image()) as azure:
            self.assertEqual(onboarding.resolve_image(CONFIG, IMAGE), IMAGE)
            self.assertEqual(azure.call_count, 1)
            self.assertIn(":".join(IMAGE.values()), azure.call_args.args)

    def test_incompatible_images_fail_instead_of_falling_back(self):
        changes = [
            {"architecture": "Arm64"}, {"hyperVGeneration": "V1"},
            {"osDiskImage": {"sizeInGb": 256}}, {"features": []},
            {"plan": {"name": "paid"}}, {"name": "latest"},
        ]
        for change in changes:
            with self.subTest(change=change), patch.object(
                onboarding, "az", return_value={**self.image(), **change}
            ), self.assertRaises(onboarding.OnboardingError):
                onboarding.resolve_image(CONFIG, IMAGE)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.sku = sku_for(CONFIG)
        self.vm_usage = [usage("cores"), usage("testFamily"), usage("virtualMachines")]
        self.network_usage = [usage("PublicIPAddresses", 8, 10), usage("NatGateways", 0, 1)]
        self.region = "West US 2"
        self.region_code = "westus2"
        self.group_resources = []
        self.provider_region = "West US 2"
        self.feature_state = "Registered"
        self.permission_entries = [{"actions": ["*"], "notActions": []}]
        self.permission_urls = []
        self.location_urls = []
        self.group_exists = False

    def azure(self, *args, subscription=None):
        if args[:2] != ("cloud", "show"):
            self.assertEqual(subscription, CONFIG["subscriptionId"])
        route = args[:2]
        if route == ("cloud", "show"):
            return {"name": "AzureCloud"}
        if route == ("account", "show"):
            return {"state": "Enabled"}
        if route == ("provider", "show"):
            return {
                "registrationState": "Registered",
                "resourceTypes": [{"resourceType": name, "locations": [self.provider_region]} for name in (
                    "virtualMachines", "disks", "virtualNetworks", "bastionHosts", "natGateways",
                    "publicIPAddresses", "networkSecurityGroups", "networkInterfaces",
                )],
            }
        if route == ("feature", "show"):
            self.assertEqual(args[2:], ("--namespace", "Microsoft.Compute", "--name", "EncryptionAtHost"))
            return {"properties": {"state": self.feature_state}}
        if route == ("group", "exists"):
            return self.group_exists or bool(self.group_resources)
        if route == ("rest", "--method"):
            if "/locations?" in args[4]:
                self.assertEqual(args[2:4], ("get", "--url"))
                self.location_urls.append(args[4])
                return {"value": [{"name": self.region_code, "displayName": self.region}]}
            self.permission_urls.append(args[4])
            return {"value": self.permission_entries}
        if route == ("resource", "list"):
            return self.group_resources
        if route == ("vm", "list-skus"):
            return [self.sku]
        if route == ("vm", "list-usage"):
            return self.vm_usage
        if route == ("network", "list-usages"):
            return self.network_usage
        self.fail(f"Unexpected Azure command: {args}")

    def run_check(self, config=None):
        with patch.object(onboarding, "az", side_effect=self.azure), patch.object(
            onboarding, "resolve_image", return_value=IMAGE
        ):
            return onboarding.preflight(config or CONFIG)

    def test_default_region_has_full_checks_not_a_bypass(self):
        report = self.run_check()
        self.assertEqual(report["location"], "westus2")
        self.assertEqual(len(report["quotaChecks"]), 5)
        self.assertEqual(report["quotaChecks"][3]["required"], 2)
        self.assertIn("checkedAtUtc", report)
        self.assertEqual(report["owner"], CONFIG["owner"])
        self.assertEqual(report["permissionCheck"]["status"], "ReportedActionsAllowed")
        self.assertEqual(report["hostEncryption"], {
            "enabled": True, "keyManagement": "PlatformManaged",
            "subscriptionFeatureState": "Registered", "skuSupported": True,
        })

    def test_locations_use_subscription_scoped_arm_endpoint(self):
        self.run_check()
        self.assertEqual(self.location_urls, [
            f"https://management.azure.com/subscriptions/{CONFIG['subscriptionId']}/locations?api-version=2022-12-01"
        ])

    def test_unregistered_pending_or_unknown_host_encryption_feature_blocks(self):
        for state in ("NotRegistered", "Registering", "Unregistering", None):
            self.feature_state = state
            with self.subTest(state=state), self.assertRaisesRegex(
                onboarding.OnboardingError,
                f"az feature register --namespace Microsoft.Compute --name EncryptionAtHost --subscription {CONFIG['subscriptionId']}",
            ):
                self.run_check()

    def test_gpu_and_custom_region_also_require_feature_registration(self):
        self.region_code = "eastus"
        self.region = self.provider_region = "East US"
        for compute in ("cpu", "gpu"):
            config = {**CONFIG, "location": "eastus", "compute": compute, "vmSize": "auto"}
            self.sku = sku_for(config)
            self.feature_state = "Registering"
            with self.subTest(compute=compute), self.assertRaisesRegex(
                onboarding.OnboardingError, "Registered is required"
            ):
                self.run_check(config)
            self.feature_state = "Registered"
            report = self.run_check(config)
            self.assertEqual(report["location"], "eastus")
            self.assertTrue(report["hostEncryption"]["skuSupported"])

    def test_custom_region_cannot_skip_region_checks(self):
        with self.assertRaisesRegex(onboarding.OnboardingError, "available subscription region"):
            self.run_check({**CONFIG, "location": "eastus"})

    def test_resource_service_region_support_is_required(self):
        self.provider_region = "East US"
        with self.assertRaisesRegex(onboarding.OnboardingError, "advertise support"):
            self.run_check()

    def test_nonempty_resource_group_cannot_be_adopted(self):
        self.group_resources = [{"id": "/existing/vm"}]
        with self.assertRaisesRegex(onboarding.OnboardingError, "new or empty"):
            self.run_check()

    def test_permissions_check_subscription_for_new_group_and_group_for_existing_empty_group(self):
        for existing in (False, True):
            self.group_exists = existing
            report = self.run_check()
            scope = f"/subscriptions/{CONFIG['subscriptionId']}"
            if existing:
                scope += f"/resourceGroups/{CONFIG['resourceGroup']}"
            self.assertEqual(report["permissionCheck"]["scope"], scope)
            self.assertIn(scope + "/providers/Microsoft.Authorization/permissions?", self.permission_urls[-1])

    def test_reader_and_excluded_network_permissions_block_without_role_mutation(self):
        for entries in (
            [{"actions": ["*/read"], "notActions": []}],
            [{"actions": ["*"], "notActions": ["microsoft.network/*"]}],
            [],
        ):
            self.permission_entries = entries
            with self.subTest(entries=entries), self.assertRaisesRegex(onboarding.OnboardingError, "Missing deployment permissions"):
                self.run_check()

    def test_other_grant_can_restore_notactions_and_permissions_are_paginated(self):
        permissions = [
            {"value": [{"actions": ["*"], "notActions": ["Microsoft.Network/*"]}],
             "nextLink": "https://management.azure.com/next"},
            {"value": [{"actions": ["microsoft.network/*"], "notActions": []}]},
        ]
        with patch.object(onboarding, "az", side_effect=permissions) as azure:
            self.assertEqual(onboarding.check_permissions(CONFIG, False)["status"], "ReportedActionsAllowed")
        self.assertEqual(azure.call_count, 2)
        with patch.object(onboarding, "az", return_value={"value": [], "nextLink": "https://example.com/next"}):
            with self.assertRaisesRegex(onboarding.OnboardingError, "pagination"):
                onboarding.check_permissions(CONFIG, False)

    def test_family_regional_and_vm_count_quotas_each_block(self):
        for name in ("cores", "testFamily", "virtualMachines"):
            self.vm_usage = [usage("cores"), usage("testFamily"), usage("virtualMachines")]
            next(row for row in self.vm_usage if row["name"]["value"] == name)["limit"] = 0
            with self.subTest(name=name), self.assertRaisesRegex(onboarding.OnboardingError, "Insufficient"):
                self.run_check()

    def test_two_public_ips_and_nat_quota_are_checked(self):
        for rows in ([usage("PublicIPAddresses", 9, 10)], [usage("PublicIPAddresses"), usage("NatGateways", 1, 1)]):
            self.network_usage = rows
            with self.subTest(rows=rows), self.assertRaisesRegex(onboarding.OnboardingError, "Insufficient"):
                self.run_check()
        self.network_usage = []
        with self.assertRaisesRegex(onboarding.OnboardingError, "public IP quota"):
            self.run_check()

    def test_bastion_only_preflight_does_not_query_other_vnets(self):
        with patch.object(onboarding, "az", side_effect=self.azure) as azure, patch.object(
            onboarding, "resolve_image", return_value=IMAGE
        ):
            report = onboarding.preflight(CONFIG)
        self.assertFalse(any(call.args[:3] == ("network", "vnet", "list") for call in azure.call_args_list))
        self.assertNotIn("overlappingVnets", report)


class GenerationTests(unittest.TestCase):
    def report(self):
        return {
            "imageReference": IMAGE, "profile": reference_profiles("cpu")["small"],
            "quotaChecks": [], "costEstimate": {"fixture": True},
        }

    def test_template_requires_host_encryption_without_additional_resources(self):
        template = (onboarding.ASSETS / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("param encryptionAtHost bool = true", template)
        self.assertIn("    encryptionAtHost: encryptionAtHost\n", template)
        self.assertNotIn("diskEncryptionSetResourceId", template)
        self.assertNotIn("avm/res/key-vault/", template)

    def test_parameters_match_all_eight_profiles_and_only_public_key(self):
        for compute in ("cpu", "gpu"):
            for size in ("small", "medium", "large", "xl"):
                config = {**CONFIG, "compute": compute, "size": size, "vmSize": reference_profiles(compute)[size]["sku"]}
                values = onboarding.parameters(config, IMAGE, PUBLIC_KEY)["parameters"]
                self.assertEqual(values["vmSize"]["value"], reference_profiles(compute)[size]["sku"])
                self.assertEqual(values["imageReference"]["value"], IMAGE)
                self.assertIs(values["encryptionAtHost"]["value"], True)
                self.assertEqual(values["owner"]["value"], CONFIG["owner"])
                self.assertEqual(values["sshPublicKey"]["value"], PUBLIC_KEY)
                self.assertTrue(values["customData"]["value"].startswith("#cloud-config\n"))
                self.assertIn("docker-ce", values["customData"]["value"])
                self.assertEqual("nvidia-smi" in values["customData"]["value"], compute == "gpu")
                self.assertNotIn("PRIVATE KEY", json.dumps(values))

    def test_owner_tag_is_required_and_passed_to_every_top_level_module(self):
        template = (onboarding.ASSETS / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("param owner string\n", template)
        self.assertIn("var resourceTags = union(tags, { owner: owner })", template)
        self.assertEqual(template.count("    tags: resourceTags\n"), 7)

    def test_avm_semantic_version_sort_and_pin_every_direct_module(self):
        text = (onboarding.ASSETS / "infra" / "main.bicep").read_text(encoding="utf-8")
        def response(url, timeout):
            self.assertTrue(url.startswith("https://mcr.microsoft.com/v2/bicep/avm/res/"))
            return io.StringIO(json.dumps({"tags": ["0.9.0", "0.10.0", "0.11.0-preview"]}))
        with patch.object(onboarding.urllib.request, "urlopen", side_effect=response) as fetch:
            refreshed, versions = onboarding.refresh_modules(text)
        self.assertEqual(len(versions), 6)
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(set(versions.values()), {"0.10.0"})
        self.assertIn("br/public:avm/res/compute/virtual-machine:0.10.0", refreshed)

    def test_no_stable_module_version_fails(self):
        with patch.object(onboarding.urllib.request, "urlopen", return_value=io.StringIO('{"tags":["1.0.0-rc"]}')):
            with self.assertRaisesRegex(onboarding.OnboardingError, "No stable"):
                onboarding.refresh_modules("br/public:avm/res/network/nat-gateway:1.0.0")

    def test_output_is_persistent_only_after_compilation(self):
        template = (onboarding.ASSETS / "infra" / "main.bicep").read_text(encoding="utf-8")
        def compile_command(args):
            Path(args[-1]).write_text("{}", encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dev"
            with patch.object(onboarding, "refresh_modules", return_value=(template, {"test": "1.0.0"})), patch.object(
                onboarding, "command", side_effect=compile_command
            ):
                onboarding.generate(CONFIG, self.report(), PUBLIC_KEY, output)
            for name in ("config.json", "preflight.json", "cost-estimate.json", "main.parameters.json", "avm-versions.json",
                         "infra/main.bicep", "infra/cloud-init.yaml", "infra/docker-setup.sh", "infra/gpu-setup.sh"):
                self.assertTrue((output / name).is_file(), name)
            self.assertFalse((output / "main.arm.json").exists())
            self.assertEqual(json.loads((output / "config.json").read_text()), CONFIG)
            self.assertEqual(json.loads((output / "cost-estimate.json").read_text()), self.report()["costEstimate"])
            self.assertEqual(
                (output / "infra" / "cloud-init.yaml").read_text(),
                json.loads((output / "main.parameters.json").read_text())["parameters"]["customData"]["value"],
            )
            with self.assertRaisesRegex(onboarding.OnboardingError, "overwrite"):
                onboarding.generate(CONFIG, self.report(), PUBLIC_KEY, output)

    def test_compile_failure_leaves_no_success_shaped_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dev"
            with patch.object(onboarding, "refresh_modules", return_value=("bad bicep", {})), patch.object(
                onboarding, "command", side_effect=onboarding.OnboardingError("compile failed")
            ), self.assertRaisesRegex(onboarding.OnboardingError, "compile failed"):
                onboarding.generate(CONFIG, self.report(), PUBLIC_KEY, output)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_auto_selection_is_pinned_in_config_parameters_and_report_for_every_variant(self):
        template = (onboarding.ASSETS / "infra" / "main.bicep").read_text(encoding="utf-8")
        for profile in onboarding.profile_candidates("cpu", "small"):
            with self.subTest(sku=profile["sku"]), tempfile.TemporaryDirectory() as directory:
                report = {**self.report(), "profile": profile}
                output = Path(directory) / "dev"
                with patch.object(onboarding, "refresh_modules", return_value=(template, {})), patch.object(
                    onboarding, "command", side_effect=lambda args: Path(args[-1]).write_text("{}")
                ):
                    onboarding.generate({**CONFIG, "vmSize": "auto"}, report, PUBLIC_KEY, output)
                self.assertEqual(json.loads((output / "config.json").read_text())["vmSize"], profile["sku"])
                self.assertEqual(json.loads((output / "main.parameters.json").read_text())["parameters"]["vmSize"]["value"],
                                 profile["sku"])
                self.assertEqual(json.loads((output / "preflight.json").read_text())["profile"], profile)

    def test_unpinned_old_config_cannot_reselect_a_sku_during_parameter_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "main.parameters.json"
            path.write_text(json.dumps(onboarding.parameters(CONFIG, IMAGE, PUBLIC_KEY)))
            with patch("sys.argv", ["onboarding.py", "check", "--config", "unused.json", "--parameters", str(path)]), patch.object(
                onboarding, "read_config", return_value={**CONFIG, "vmSize": "auto"}
            ), patch.object(onboarding, "read_public_key", return_value=PUBLIC_KEY), patch.object(
                onboarding, "preflight"
            ) as preflight, redirect_stderr(io.StringIO()) as error:
                self.assertEqual(onboarding.main(), 1)
                self.assertIn("pinned vmSize", error.getvalue())
                preflight.assert_not_called()

    def test_recheck_detects_parameter_drift(self):
        for changed in (None, "size", "owner", "missing-host-encryption", "disabled-host-encryption", "missing-docker"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "main.parameters.json"
                parameters = onboarding.parameters(CONFIG, IMAGE, PUBLIC_KEY)
                if changed == "size":
                    parameters["parameters"]["vmSize"]["value"] = "Standard_D2s_v5"
                elif changed == "owner":
                    parameters["parameters"]["owner"]["value"] = "different-team"
                elif changed == "missing-host-encryption":
                    parameters["parameters"].pop("encryptionAtHost")
                elif changed == "disabled-host-encryption":
                    parameters["parameters"]["encryptionAtHost"]["value"] = False
                elif changed == "missing-docker":
                    parameters["parameters"]["customData"]["value"] = ""
                path.write_text(json.dumps(parameters), encoding="utf-8")
                with patch("sys.argv", ["onboarding.py", "check", "--config", "unused.json",
                                        "--parameters", str(path)]), patch.object(
                    onboarding, "read_config", return_value=copy.deepcopy(CONFIG)
                ), patch.object(onboarding, "read_public_key", return_value=PUBLIC_KEY), patch.object(
                    onboarding, "preflight", return_value=self.report()
                ), patch.object(
                    onboarding, "estimate_costs", return_value={"fixture": True}
                ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(onboarding.main(), 1 if changed else 0)


class CloudInitTests(unittest.TestCase):
    def config(self, compute):
        header, content = onboarding.cloud_init(compute).split("\n", 1)
        self.assertEqual(header, "#cloud-config")
        self.assertLess(len(content.encode("utf-8")), 65536)
        return json.loads(content)

    def test_both_modes_have_one_fail_fast_sequence_and_exact_root_owned_scripts(self):
        for compute in ("cpu", "gpu"):
            with self.subTest(compute=compute):
                config = self.config(compute)
                scripts = ["docker"] + (["gpu"] if compute == "gpu" else [])
                self.assertTrue(config["package_update"])
                self.assertEqual(config["packages"][:2], ["ca-certificates", "curl"])
                self.assertEqual("dkms" in config["packages"], compute == "gpu")
                self.assertEqual(len(config["write_files"]), len(scripts))
                for entry, name in zip(config["write_files"], scripts):
                    self.assertEqual(entry["path"], f"/usr/local/sbin/onboarding-{name}-setup")
                    self.assertEqual(entry["owner"], "root:root")
                    self.assertEqual(entry["permissions"], "0700")
                    self.assertEqual(entry["content"], (onboarding.ASSETS / "infra" / f"{name}-setup.sh").read_text())
                self.assertEqual(config["runcmd"], [[
                    "bash", "-e", "-c", "\n".join(entry["path"] for entry in config["write_files"]),
                ]])

    def test_template_requires_nonempty_cloud_init_for_cpu_too(self):
        template = (onboarding.ASSETS / "infra" / "main.bicep").read_text()
        self.assertIn("@minLength(1)\nparam customData string\n", template)
        self.assertIn("    customData: customData\n", template)

    def test_docker_has_no_root_group_grant_tcp_listener_or_automatic_container_pull(self):
        script = self.config("cpu")["write_files"][0]["content"]
        self.assertIn("Signed-By: /etc/apt/keyrings/docker.asc", script)
        self.assertIn("Components: stable", script)
        self.assertIn("https://download.docker.com/linux/ubuntu", script)
        self.assertNotIn("get.docker.com", script)
        for forbidden in ("usermod", "docker run", "docker pull", "tcp://", "2375", "mkfs"):
            self.assertNotIn(forbidden, script)
        gpu = self.config("gpu")["write_files"][1]["content"]
        self.assertIn("595.91.07-grid-azure.run", gpu)
        self.assertIn("--silent --dkms -M open", gpu)
        self.assertNotIn("nvidia-container-toolkit", onboarding.cloud_init("gpu"))


@unittest.skipUnless(shutil.which("bash"), "Bash is required for shell bootstrap execution tests.")
class BootstrapShellTests(unittest.TestCase):
    def docker(self, fail="", os_id="ubuntu"):
        stubs = r'''
source() { ID="$MOCK_OS"; VERSION_CODENAME=noble; }
dpkg() { printf 'amd64\n'; }
install() { :; }
curl() { printf 'curl %s\n' "$*" >&2; [[ "$FAIL_STEP" != key ]]; }
chmod() { :; }
tee() { cat >/dev/null; }
apt-get() { printf 'apt-get %s\n' "$*" >&2; [[ "$FAIL_STEP" != apt ]]; }
systemctl() { printf 'systemctl %s\n' "$*" >&2; [[ "$FAIL_STEP" != daemon ]]; }
docker() { printf 'docker %s\n' "$*" >&2; [[ "$FAIL_STEP" != client ]]; }
'''
        script = (onboarding.ASSETS / "infra" / "docker-setup.sh").read_text()
        return subprocess.run(
            [shutil.which("bash"), "--noprofile", "--norc", "-s"],
            input=f"FAIL_STEP={fail!r}\nMOCK_OS={os_id!r}\n" + stubs + script,
            text=True, capture_output=True, check=False,
        )

    def test_scripts_have_valid_bash_syntax(self):
        for path in (onboarding.ASSETS / "infra").glob("*.sh"):
            with self.subTest(script=path.name):
                result = subprocess.run(
                    [shutil.which("bash"), "--noprofile", "--norc", "-n"],
                    input=path.read_text(), text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_docker_installs_official_packages_enables_service_and_checks_plugins(self):
        result = self.docker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin", result.stderr)
        for expected in ("systemctl enable --now docker", "systemctl is-active --quiet docker",
                         "docker --host unix:///var/run/docker.sock info", "docker buildx version", "docker compose version"):
            self.assertIn(expected, result.stderr)

    def test_repository_install_service_and_daemon_failures_fail_loudly(self):
        for fail in ("key", "apt", "daemon", "client"):
            with self.subTest(fail=fail):
                result = self.docker(fail)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Docker setup failed; inspect /var/log/cloud-init-output.log", result.stderr)
                self.assertNotIn("docker compose version", result.stderr)

    def test_non_ubuntu_guest_is_rejected_before_installation(self):
        result = self.docker(os_id="debian")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires the selected Ubuntu amd64 image", result.stderr)
        self.assertNotIn("curl ", result.stderr)

    def test_failed_docker_step_cannot_be_hidden_by_a_successful_gpu_step(self):
        sequence = json.loads(onboarding.cloud_init("gpu").split("\n", 1)[1])["runcmd"][0]
        commands = sequence[-1].replace("/usr/local/sbin/onboarding-docker-setup", "docker_step").replace(
            "/usr/local/sbin/onboarding-gpu-setup", "gpu_step"
        )
        for fail in (False, True):
            result = subprocess.run(
                [shutil.which("bash"), *sequence[1:-1],
                 f"docker_step() {{ echo docker; return {1 if fail else 0}; }}\ngpu_step() {{ echo gpu; }}\n" + commands],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 1 if fail else 0, result.stderr)
            self.assertEqual("gpu" in result.stdout, not fail)


class PricingTests(unittest.TestCase):
    def rows(self, config=None, profiles=None):
        config = config or CONFIG
        definitions = [
            ("Storage", "Standard SSD Managed Disks", "E70 LRS Disk", "1/Month", 1228.8, "E70 LRS", ""),
            ("Storage", "Premium SSD Managed Disks", "P10 LRS Disk", "1/Month", 17.92, "P10 LRS", ""),
            ("Azure Bastion", "Azure Bastion", "Standard Gateway", "1 Hour", 0.29, "Standard", ""),
            ("NAT Gateway", "NAT Gateway", "Standard Gateway", "1 Hour", 0.045, "Standard", ""),
            ("Virtual Network", "IP Addresses", "Standard IPv4 Static Public IP", "1 Hour", 0.005, "Standard", ""),
        ]
        definitions.extend(
            ("Virtual Machines", "Virtual Machines Linux", profile["sku"], "1 Hour", 1.305 * 2 ** index, profile["sku"], profile["sku"])
            for index, profile in enumerate((profiles or reference_profiles(config["compute"])).values())
        )
        return [
            {
                "serviceName": service, "productName": product, "meterName": name,
                "unitOfMeasure": unit, "retailPrice": price, "skuName": sku, "armSkuName": arm,
                "armRegionName": "Global" if service == "NAT Gateway" else config["location"],
                "currencyCode": "USD", "type": "Consumption", "tierMinimumUnits": 0,
                "isPrimaryMeterRegion": service in ("Storage", "Virtual Machines", "NAT Gateway"),
                "effectiveStartDate": "2020-01-01T00:00:00Z", "meterId": str(index),
            }
            for index, (service, product, name, unit, price, sku, arm) in enumerate(definitions)
        ]

    def estimate(self, rows=None, config=None):
        config = config or CONFIG
        with patch.object(onboarding, "retail_prices", return_value=self.rows(config) if rows is None else rows):
            return onboarding.estimate_costs(config["location"], reference_profiles(config["compute"]))

    def test_full_fixed_costs_and_all_sizes_use_decimal_totals_and_730_hours(self):
        report = self.estimate()
        self.assertEqual(report["sharedMonthlyUsd"], 1498.57)
        self.assertEqual(report["profiles"]["small"]["vmMonthlyUsd"], 952.65)
        self.assertEqual(report["profiles"]["small"]["baseMonthlyUsd"], 2451.22)
        self.assertEqual(report["sharedResources"][2]["monthlyUsd"], 211.7)
        self.assertEqual(report["sharedResources"][4]["monthlyUsd"], 7.3)
        self.assertEqual(report["sharedResources"][3]["meterRegion"], "Global")
        self.assertEqual(len(report["profiles"]), 4)
        self.assertTrue(report["excludedUsage"])

    def test_wrong_os_spot_reservations_units_regions_currency_and_future_rates_are_not_used(self):
        rows = self.rows()
        vm = rows[-4]
        for change in (
            {"productName": "Virtual Machines Windows"}, {"productName": "Virtual Machines Ubuntu Pro"},
            {"productName": "Virtual Machines SUSE"}, {"skuName": "Small Spot"},
            {"meterName": "Small Low Priority"}, {"type": "Reservation"},
            {"type": "DevTestConsumption"}, {"unitOfMeasure": "100 Hours"},
            {"armRegionName": "eastus"}, {"currencyCode": "GBP"},
            {"effectiveStartDate": "2999-01-01T00:00:00Z"}, {"tierMinimumUnits": 100},
        ):
            with self.subTest(change=change):
                report = self.estimate(rows + [{**vm, **change, "meterId": "unwanted", "retailPrice": 0.001}])
                self.assertEqual(report["profiles"]["small"]["vmHourlyUsd"], 1.305)

    def test_capacity_not_shared_disk_mount_is_priced_and_newest_rate_is_used(self):
        rows = self.rows()
        report = self.estimate(rows + [
            {**rows[0], "meterName": "E70 LRS Disk Mount", "retailPrice": 109.5},
            {**rows[-4], "retailPrice": 2, "effectiveStartDate": "2021-01-01T00:00:00Z"},
        ])
        self.assertEqual(report["sharedMonthlyUsd"], 1498.57)
        self.assertEqual(report["profiles"]["small"]["vmHourlyUsd"], 2)

    def test_missing_ambiguous_or_invalid_rates_block_instead_of_using_zero(self):
        rows = self.rows()
        for invalid in (
            rows[1:],
            rows + [{**rows[-4], "meterId": "duplicate", "retailPrice": 2}],
            [{**row, "retailPrice": 0} if index == 0 else row for index, row in enumerate(rows)],
            [{**row, "retailPrice": "invalid"} if index == 0 else row for index, row in enumerate(rows)],
        ):
            with self.subTest(rows=invalid), self.assertRaises(onboarding.OnboardingError):
                self.estimate(invalid)

    def test_region_override_and_gpu_profiles_are_priced_independently(self):
        config = {**CONFIG, "location": "eastus", "compute": "gpu"}
        report = self.estimate(config=config)
        self.assertEqual(report["location"], "eastus")
        self.assertEqual(report["profiles"]["large"]["sku"], onboarding.PROFILES["gpu"]["large"]["sku"])
        with self.assertRaises(onboarding.OnboardingError):
            self.estimate(rows=self.rows(), config=config)

    def test_retail_pagination_and_regional_filters_include_explicit_global_nat(self):
        pages = [
            io.StringIO(json.dumps({"Items": [1], "NextPageLink": "https://prices.azure.com:443/api/retail/prices?next=2"})),
            io.StringIO(json.dumps({"Items": [2], "NextPageLink": None})),
        ]
        with patch.object(onboarding.urllib.request, "urlopen", side_effect=pages) as fetch:
            self.assertEqual(onboarding.retail_prices("eastus", reference_profiles("cpu")), [1, 2])
        query = onboarding.urllib.parse.parse_qs(onboarding.urllib.parse.urlsplit(fetch.call_args_list[0].args[0]).query)["$filter"][0]
        self.assertIn("armRegionName eq 'eastus'", query)
        self.assertIn("armRegionName eq 'Global'", query)
        with patch.object(onboarding.urllib.request, "urlopen", return_value=io.StringIO(
            '{"Items": [], "NextPageLink": "http://example.com/next"}'
        )), self.assertRaisesRegex(onboarding.OnboardingError, "pagination"):
            onboarding.retail_prices("eastus", reference_profiles("cpu"))

    def test_prices_needs_no_subscription_key_or_azure_login(self):
        with patch("sys.argv", ["onboarding.py", "prices", "--compute", "cpu"]), patch.object(
            onboarding, "estimate_costs", return_value={"profiles": {}}
        ) as estimate, patch.object(onboarding, "az", side_effect=AssertionError("Azure login is not needed")), patch.object(
            onboarding, "read_public_key", side_effect=AssertionError("SSH key is not needed")
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(onboarding.main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["profiles"], {})
        profiles = estimate.call_args.args[1]
        self.assertEqual(len(profiles), 15)
        self.assertEqual({profile["processor"] for profile in profiles.values()}, {"Intel", "AMD"})
        self.assertEqual({profile["generation"] for profile in profiles.values()}, {6, 7})

    def test_subscription_prices_use_quota_selected_profiles_and_disclose_unavailable_sizes(self):
        profiles = onboarding.profile_candidates("cpu", "small")
        skus = [{**sku_for(CONFIG, profile), "family": f"family{index}"} for index, profile in enumerate(profiles)]
        usages = [usage("cores"), usage("virtualMachines")]
        usages.extend(usage(f"family{index}", 0, (index + 1) * 100) for index in range(4))
        argv = ["onboarding.py", "prices", "--compute", "cpu", "--subscription", CONFIG["subscriptionId"]]
        with patch("sys.argv", argv), patch.object(
            onboarding, "compute_inventory", return_value=(skus, usages)
        ) as inventory, patch.object(onboarding, "estimate_costs", return_value={"profiles": {}}) as estimate, patch.object(
            onboarding, "read_public_key", side_effect=AssertionError("No key needed")
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(onboarding.main(), 0)
        inventory.assert_called_once_with(CONFIG["subscriptionId"], "westus2", "cpu")
        estimate.assert_called_once_with("westus2", {"cpu-small": profiles[-1]})
        report = json.loads(output.getvalue())
        self.assertEqual(report["subscriptionId"], CONFIG["subscriptionId"])
        self.assertEqual(report["skuSelection"]["cpu-small"]["selectedSku"], "Standard_D16ads_v6")
        self.assertEqual(report["skuSelection"]["cpu-xl"]["status"], "Unavailable")

    def test_no_quota_eligible_prices_return_failure_without_fabricated_rates(self):
        with patch("sys.argv", ["onboarding.py", "prices", "--compute", "cpu",
                               "--subscription", CONFIG["subscriptionId"]]), patch.object(
            onboarding, "compute_inventory", return_value=([], [])
        ), patch.object(onboarding, "estimate_costs") as estimate, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(onboarding.main(), 1)
        estimate.assert_not_called()
        self.assertIn("No quota-eligible", error.getvalue())

    def test_all_eight_profiles_use_bounded_queries_and_deduplicate_common_meters(self):
        profiles = {
            f"{compute}-{size}": profile for compute in ("cpu", "gpu")
            for size, profile in reference_profiles(compute).items()
        }
        pages = [
            io.StringIO(json.dumps({"Items": self.rows({**CONFIG, "compute": compute}), "NextPageLink": None}))
            for compute in ("cpu", "gpu")
        ]
        with patch.object(onboarding.urllib.request, "urlopen", side_effect=pages) as fetch:
            report = onboarding.estimate_costs("westus2", profiles)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(report["profiles"]), 8)
        self.assertEqual(report["sharedMonthlyUsd"], 1498.57)

    def test_all_candidate_pricing_batches_exact_skus_without_azure_login(self):
        profiles = {
            f"{compute}-{size}-{profile['sku']}" if compute == "cpu" else f"{compute}-{size}": profile
            for compute in ("cpu", "gpu") for size in onboarding.PROFILES[compute]
            for profile in onboarding.profile_candidates(compute, size)
        }
        names = list(profiles)
        pages = [
            io.StringIO(json.dumps({
                "Items": self.rows(profiles={name: profiles[name] for name in names[offset:offset + 4]}),
                "NextPageLink": None,
            }))
            for offset in range(0, len(names), 4)
        ]
        with patch("sys.argv", ["onboarding.py", "prices"]), patch.object(
            onboarding.urllib.request, "urlopen", side_effect=pages
        ) as fetch, patch.object(onboarding, "az", side_effect=AssertionError("Must not use Azure login")), redirect_stdout(
            io.StringIO()
        ) as output:
            self.assertEqual(onboarding.main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["profiles"]), 19)
        self.assertEqual(report["sharedMonthlyUsd"], 1498.57)
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(report["profiles"]["cpu-small-Standard_D16ads_v6"]["processor"], "AMD")
        for call in fetch.call_args_list:
            query = onboarding.urllib.parse.parse_qs(onboarding.urllib.parse.urlsplit(call.args[0]).query)["$filter"][0]
            self.assertLessEqual(query.count("armSkuName eq"), 4)

    def test_generate_and_final_check_refresh_pricing_and_pricing_failures_stop_generation(self):
        for action in ("generate", "check"):
            for fail in (False, True):
                with self.subTest(action=action, fail=fail), patch(
                    "sys.argv", ["onboarding.py", action, "--config", "unused.json"]
                ), patch.object(onboarding, "read_config", return_value=CONFIG), patch.object(
                    onboarding, "read_public_key", return_value=PUBLIC_KEY
                ), patch.object(onboarding, "preflight", return_value={
                    "imageReference": IMAGE, "profile": reference_profiles("cpu")["small"],
                }), patch.object(
                    onboarding, "estimate_costs", side_effect=onboarding.OnboardingError("No verified price") if fail else None,
                    return_value={"fixture": True}
                ) as estimate, patch.object(onboarding, "generate") as generate, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(onboarding.main(), 1 if fail else 0)
                    estimate.assert_called_once_with(CONFIG["location"], {"small": reference_profiles("cpu")["small"]})
                    self.assertEqual(generate.called, action == "generate" and not fail)

    def test_generation_prices_the_selected_amd_v6_sku_not_the_reference_intel_v7(self):
        profile = onboarding.profile_candidates("cpu", "small")[-1]
        with patch("sys.argv", ["onboarding.py", "generate", "--config", "unused.json"]), patch.object(
            onboarding, "read_config", return_value={**CONFIG, "vmSize": "auto"}
        ), patch.object(onboarding, "read_public_key", return_value=PUBLIC_KEY), patch.object(
            onboarding, "preflight", return_value={"profile": profile, "imageReference": IMAGE}
        ), patch.object(onboarding, "estimate_costs", return_value={}) as estimate, patch.object(
            onboarding, "generate"
        ) as generate, redirect_stdout(io.StringIO()):
            self.assertEqual(onboarding.main(), 0)
        estimate.assert_called_once_with("westus2", {"small": profile})
        self.assertEqual(generate.call_args.args[1]["profile"]["sku"], "Standard_D16ads_v6")


if __name__ == "__main__":
    unittest.main()
