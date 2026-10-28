# Perspicacity

Interactive Azure development-VM onboarding for coding agents, backed by Bicep and Azure Verified Modules.

## Use the skill

The repository skill is [perspicacity-azure-onboard](.github/skills/perspicacity-azure-onboard/SKILL.md).
Ask a skill-capable coding agent:

> Use perspicacity-azure-onboard to configure a new Azure development VM.

The agent guides you through subscription, CPU/GPU, region, priced Small/Medium/Large/XL options,
owner/team, username, local SSH keys and private IP addressing. It generates configuration, checks
live Azure availability, quota and reported deployment permissions, and requires separate approval
of the fresh itemized PAYG estimate before deploying billable resources. Bastion SSH/VS Code remains
the secure default. Docker Engine is installed on first boot. After VM creation and software readiness,
the agent identifies disks and requests separate formatting approval before preparing data/scratch
mounts. Application containers and data-source integrations remain out of scope.
To use it in another repository, copy the entire `.github/skills/perspicacity-azure-onboard` directory,
including its assets, references and Python helper, into that repository's skills directory.

**Defaults:** West US 2, Small CPU, VNet `192.168.0.0/24`, Bastion subnet `192.168.0.0/26`,
VM subnet `192.168.0.64/26`. Bastion **Standard** supports local SSH/VS Code.
The VM has no public IP; NAT supplies a static outbound IP. SSH keys stay local; there is no Key Vault.

## Compute and storage

| Size | CPU vCPU / RAM | Local NVMe devices / total |
|---|---|---|
| Small | 16 / 64 GiB | 2 x 440 GiB / 880 GiB |
| Medium | 32 / 128 GiB | 4 x 440 GiB / 1,760 GiB |
| Large | 64 / 256 GiB | 4 x 880 GiB / 3,520 GiB |
| XL | 128 / 512 GiB | 4 x 1,760 GiB / 7,040 GiB |

CPU automatically considers **Intel Ddsv6/Ddsv7 and AMD Dadsv6/Dadsv7** at the chosen size:
`Standard_D{vCPU}ds_v6`, `Standard_D{vCPU}ds_v7`, `Standard_D{vCPU}ads_v6` and
`Standard_D{vCPU}ads_v7`. AMD v6 has no 128-vCPU SKU, so XL has three candidates.
The helper excludes unavailable/restricted SKUs, incompatible capabilities and insufficient/unknown
quota. It ranks by effective available vCPUs (minimum of regional and family headroom), then family
headroom, newer generation, then catalog order (Intel before AMD within a generation).
Headroom is limit minus current usage, not the limit alone. This is quota-based, not a cheapest-price
or performance ranking; vendor/generation performance can differ despite equal capacity.
The selected SKU, processor/generation, quota evidence and its actual price are shown for review.
The exact SKU is pinned in generated config/parameters and rechecked without silently switching
families. It never shrinks the T-shirt or changes region to bypass quota.

| Size | GPU SKU | vCPU / RAM | GPU / VRAM | Total local NVMe |
|---|---|---|---|---|
| Small | `Standard_NC36ds_xl_RTXPRO6000BSE_v6` | 36 / 132 GiB | 1/4 / 24 GB | 1,600 GiB |
| Medium | `Standard_NC72ds_xl_RTXPRO6000BSE_v6` | 72 / 264 GiB | 1/2 / 48 GB | 3,200 GiB |
| Large | `Standard_NC144ds_xl_RTXPRO6000BSE_v6` | 144 / 516 GiB | 1 / 96 GB | 6,400 GiB |
| XL | `Standard_NC288ds_xl_RTXPRO6000BSE_v6` | 288 / 1,032 GiB | 2 / 192 GB total | 12,800 GiB |

Every profile has a separate **16 TiB E70 Standard SSD LRS** durable data disk and a 128 GiB Premium
SSD OS disk. The E70 data disk is detached when only the VM is deleted; deleting the resource group
still deletes it. Local NVMe is temporary, may span devices, and can be erased on deallocation or
host events. After creation, the skill recommends GPT/ext4 with E70 mounted at `/data` and each
temporary NVMe mounted separately at `/scratch/nvme1`, `/scratch/nvme2`, etc. Actual devices must be
identified and explicitly approved before partitioning/formatting. The OS disk is excluded, and there
is no automatic pooling, RAID or boot-time formatting.

CPU resolves the latest stable Canonical Ubuntu LTS with x64/Gen2/NVMe support.
GPU uses the latest documented supported Ubuntu LTS (currently 24.04) and bootstraps the supported
Azure NVIDIA GRID driver; Secure Boot/vTPM are disabled for GPU, enabled for CPU.
Both profiles require **encryption at host with Azure platform-managed keys**. No Key Vault,
Managed HSM or Disk Encryption Set is added. Preflight requires the subscription's `EncryptionAtHost`
feature to be registered and the exact SKU to advertise `EncryptionAtHostSupported=True`; otherwise
generation stops. Feature registration requires permission and is never performed automatically.
The helper pins an exact image version and the latest stable **direct** AVM module versions at generation.
New AVM interfaces must compile successfully; failed resolution never silently falls back.

Both CPU and GPU use cloud-init to install Docker Engine/CLI, containerd, Buildx and Compose from
Docker's official Ubuntu APT repository and enable the daemon. GPU retains the supported host-driver
bootstrap. Docker requires `sudo`, uses the local Unix socket, and has no public TCP endpoint.
No research containers or NVIDIA Container Toolkit are installed. Docker storage defaults to the OS
disk; cloud-init leaves E70 and local NVMe untouched. A separate, consent-gated post-creation step
prepares empty disks, grants the configured administrator access, persists mounts by filesystem UUID
in `/etc/fstab` and verifies remounts and administrator write access. Docker storage is not moved.
After scratch loss, new device inspection and formatting consent are required; missing scratch
UUIDs do not trigger automatic formatting. Declined or incomplete preparation is reported explicitly.
This adds no Azure resources or fixed service charge.
Cloud-init may still be running after Azure reports VM provisioning success. Require
`sudo cloud-init status --wait`, Docker daemon/plugin checks and (for GPU) `nvidia-smi` before claiming
readiness; see [operations](.github/skills/perspicacity-azure-onboard/references/operations.md).

Sources: [Intel v7](https://learn.microsoft.com/azure/virtual-machines/sizes/general-purpose/ddsv7-series),
[Intel v6](https://learn.microsoft.com/azure/virtual-machines/sizes/general-purpose/ddsv6-series),
[AMD v7](https://learn.microsoft.com/azure/virtual-machines/sizes/general-purpose/dadsv7-series),
[AMD v6](https://learn.microsoft.com/azure/virtual-machines/sizes/general-purpose/dadsv6-series),
[GPU specifications](https://learn.microsoft.com/azure/virtual-machines/sizes/gpu-accelerated/nc-rtxpro6000-bse-v6-series),
[GPU OS support](https://learn.microsoft.com/azure/virtual-machines/sizes/gpu-accelerated/nc-rtxpro6000-bse-v6-series-overview),
[managed disk types](https://learn.microsoft.com/azure/virtual-machines/disks-types).

## PAYG estimates before choosing a size

Fetch current USD Linux PAYG prices without signing into Azure or providing a key/configuration:

```powershell
python .github\skills\perspicacity-azure-onboard\scripts\onboarding.py prices --location westus2
```

Add `--compute cpu` or `--compute gpu` to show one mode. Without a subscription, CPU pricing
covers all 15 catalog candidates, not one assumed winner per T-shirt; this requires no Azure login.
For the interactive flow, supply the confirmed subscription to select quota-eligible SKUs first:

```powershell
python .github\skills\perspicacity-azure-onboard\scripts\onboarding.py prices --compute cpu --location westus2 --subscription <subscription-UUID>
```

This needs Azure login but no SSH keys. It prices one selected SKU per eligible T-shirt and
discloses unavailable sizes/reasons in `skuSelection`; those sizes must not be offered as deployable.
If none qualify, it fails without a fabricated price. Generation still runs full preflight.
The skill shows these costs alongside
the T-shirt specifications, then refreshes the selected profile at generation and again as the
final step before deployment approval. Missing or ambiguous prices block the flow.

**Example snapshot: West US 2, 23 October 2026, USD PAYG, 730 VM hours/month. CPU rates are Intel Ddsv7 only.**
Monthly figures include the VM, both disks, Bastion Standard, NAT and two static public IPv4s;
they exclude usage-based charges and taxes. Rerun the command; this table is not a live quote.

| Size | CPU VM/hour | CPU base/month | GPU VM/hour | GPU base/month |
|---|---:|---:|---:|---:|
| Small | $1.305 | $2,451.22 | $1.442 | $2,551.23 |
| Medium | $2.611 | $3,404.60 | $2.830 | $3,564.47 |
| Large | $5.221 | $5,309.90 | $6.380 | $6,155.97 |
| XL | $10.442 | $9,121.23 | $11.000 | $9,528.57 |

The shared base is **$1,498.57/month**: E70 data disk $1,228.80, P10 OS disk $17.92,
Bastion Standard $211.70 (two scale units already included), NAT $32.85 and two public IPs $7.30.
These charges continue during VM deallocation. Local NVMe is included in the VM rate.
NAT data processing (outbound **and return** traffic), bandwidth, Bastion transfer and Standard SSD
transactions add usage-based charges. No Spot pricing, commitments, credits or negotiated discounts
are assumed. Rates come from the [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices);
the NAT line uses its explicitly Global public-cloud meter.

## Generate without deploying

The skill first checks Azure CLI and offers to install the latest stable version if it is missing,
using explicit consent and Microsoft's OS-specific installation instructions. Windows uses WinGet
when available; macOS uses Homebrew; Linux follows distribution-specific Microsoft repositories.
No software is installed automatically. Manual installation and pricing-only paths are available.

```powershell
python .github\skills\perspicacity-azure-onboard\scripts\onboarding.py prerequisites
```

This needs neither Azure login nor a configuration file. Exit 0 confirms the installed CLI responds
to `az version`; exit 2 reports a missing CLI with an installation proposal for the agent to offer.
After an approved install, reopen/refresh the terminal if needed and rerun the check before continuing.

Prerequisites: Python 3.10+, Azure CLI with Bicep, OpenSSH `ssh-keygen`, Azure login/read and deployment permissions,
and access to Azure and Microsoft Container Registry. No Python packages are required.
Create a unique passphrase-protected SSH key in your own terminal, or use an existing public key.
Never give an agent your private key or passphrase.

Copy the [example configuration](.github/skills/perspicacity-azure-onboard/assets/config.example.json) to
`.azure-vm/request.json` and replace its subscription UUID, names, owner/team, username and public-key path.
The required `owner` is a resource tag, not a role assignment. Older requests need this field and
regenerated/reviewed parameters. The helper accepts only new/empty resource groups and never changes
the active subscription or grants roles. It checks the caller's reported actions at subscription scope
for a new group, or at an existing empty group's scope; deny assignments and policy still require ARM validation.
`vmSize` defaults to `auto` for quota-based selection. In the interactive flow, replace it with the
exact SKU reviewed in the priced size step before generation. Generated `config.json` always pins
the resolved `vmSize`; final `check --parameters` refuses older output without that pin and never
reselects a different variant. Regenerate/review if the pinned SKU no longer qualifies.

```powershell
python .github\skills\perspicacity-azure-onboard\scripts\onboarding.py generate --config .azure-vm\request.json --output .azure-vm\dev
```

The ignored output directory contains Bicep, parameters, normalized configuration, timestamped preflight
evidence, `cost-estimate.json` with the selected-profile rates/assumptions, AVM version pins and a
reviewable `infra\cloud-init.yaml` matching the cloud-init embedded in the parameters.
Generation performs read-only Azure/pricing checks and local compilation;
it does not create Azure resources or SSH keys. Existing output directories are never overwritten.

After ARM validation/what-if, recheck the same generated parameters to refresh permissions, quota
and prices, then show the fresh costs and obtain final deployment approval:

```powershell
python .github\skills\perspicacity-azure-onboard\scripts\onboarding.py check --config .azure-vm\dev\config.json --parameters .azure-vm\dev\main.parameters.json
```

**Availability and quota are checked for every region, including the default.** Insufficient quota,
SKU restrictions, image incompatibility or missing permissions stop the flow. Sufficient quota does
not guarantee physical capacity; ARM validation/what-if and user confirmation are still required.
Access is through Bastion, with no VPN or VNet peering. Onboarding does not ask about or check
overlap with home or other networks; it still validates CIDR alignment, subnet containment and
non-overlap between the Bastion and VM subnets.

See [operations](.github/skills/perspicacity-azure-onboard/references/operations.md) for deployment gates,
Bastion tunneling, VS Code, GPU verification and safe disk handling.
VM deallocation does **not** stop Bastion, NAT, public-IP or managed-disk charges.
E70 provisions 16 TiB even on Small; review current costs before deploying.

## Development

Run offline helper tests (no Azure login or resources required):

```powershell
python -m unittest discover -s tests -v
```

Compile the checked-in AVM baseline without deploying:

```powershell
az bicep build --file .github\skills\perspicacity-azure-onboard\assets\infra\main.bicep --stdout
```