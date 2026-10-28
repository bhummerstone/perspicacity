# Operations and safeguards

Commands are PowerShell examples. Replace placeholders with the approved values from the generated
`config.json`. In another shell, adapt syntax/paths without changing the subscription or configuration.
The CLI helper only performs read operations; every mutation below needs explicit user authorization.
This is a new-environment flow, not an existing infrastructure migration or in-place resize tool.

## Missing Azure CLI: offer installation

Run `onboarding.py prerequisites` before subscription/login questions. It only locates `az` and,
when present, calls `az version` without authentication. Missing CLI produces a structured install
proposal and exit 2 (`AwaitingAzureCliInstallConsent`), not automatic installation. The agent must
ask the user whether to install the latest stable CLI, install manually, or continue with pricing only.

Use the installer on the execution host, not inside the new VM. After explicit approval:

- **Windows:** if WinGet is present, use
  `winget install --exact --id Microsoft.AzureCLI --source winget`. No `--version` pin is applied.
  Let the user answer package/source agreements and UAC prompts; do not silently accept them.
  If WinGet is absent, use Microsoft's signed MSI from the
  [official Windows guide](https://learn.microsoft.com/cli/azure/install-azure-cli-windows).
  Review the installer signature/publisher; do not install WinGet or use an arbitrary mirror as a fallback.
- **macOS:** if Homebrew is present, run `brew update` then `brew install azure-cli`, stopping if
  either fails. See the [official macOS guide](https://learn.microsoft.com/cli/azure/install-azure-cli-macos).
  If Homebrew is missing, discuss the additional installation separately; do not bootstrap it automatically.
- **Linux/WSL:** identify the actual distribution/version and use the matching Microsoft package
  repository and instructions from the
  [official Linux guide](https://learn.microsoft.com/cli/azure/install-azure-cli-linux).
  Show the distribution-specific plan before asking for installation consent. Do not assume apt
  on all Linux hosts, use an obsolete distro package, or pipe a downloaded script straight into a shell.

Run approved installers in a visible, user-owned terminal, not a background command that can hang
on a password or elevation prompt. Report package-manager/network/signature errors without
claiming completion or trying an unapproved fallback. If installation is declined, pause onboarding
with the manual guide or allow `prices` only; pricing does not need Azure CLI.
Installation consent does not authorize Azure login, Bicep/extensions, upgrades of an existing CLI,
provider/feature registration or billable infrastructure.

Reopen/refresh the terminal's PATH if needed, then rerun `onboarding.py prerequisites`. Require
successful `az version` output from the same execution host before resuming subscription/login
questions. Record/display the actual installed version, not a hardcoded "latest" version number.

## Review and deploy

The generated `main.parameters.json` contains only a public SSH key, never a private key or password.
Keep local generated files ignored; they still contain subscription IDs, resource names and file paths.
Review Bicep and the `avm-versions.json` pins. New AVM interfaces may require a reviewed template update;
do not mask compile failures with an older version unless the user explicitly approves that deviation.

Azure's platform-image metadata does not guarantee an OS disk size field. If it is absent, the helper
emits a warning and records the limitation instead of assuming a size; compatibility with the fixed
128 GiB OS disk still requires ARM validation before deployment. Reported sizes above 128 GiB are
rejected. CPU image controller capabilities are parsed as comma-separated tokens with whitespace
trimmed, and must explicitly include `NVMe`.

Collect an accountable owner/team identifier, not credentials or personal contact details. The required
`owner` parameter tags every deployed top-level AVM resource (seven private-mode modules or four
demo-mode modules, with NIC/disks inside the VM module); the resource-group command below applies
the same tag to the group. A tag does not grant RBAC access. Requests generated before this field
was added must be regenerated with an owner and reviewed.

The helper checks eligibility and headroom **even for West US 2**. A quota failure is not a capacity
failure. Check Azure Quotas for both Total Regional vCPUs and the exact VM family returned by Azure.
CPU sizes consider Intel Ddsv6/Ddsv7 and AMD Dadsv6/Dadsv7 with unchanged vCPU/RAM and local
NVMe capacities; AMD v6 ends at 96 vCPUs and cannot supply XL. For `vmSize: auto`, the helper
excludes missing/restricted/incompatible SKUs and insufficient or unknown quota, then ranks by
effective available vCPUs (minimum of regional/family headroom), family headroom, newer generation,
and catalog order (Intel before AMD within a generation). Quota uses limit minus current usage.
The report's `skuSelection` records candidate outcomes, the exact winner and quota evidence.
This ranks quota, not price, benchmark performance or physical capacity.
Review the chosen processor/generation, SKU and PAYG costs, and pin `vmSize` in the request.
Generation persists that exact SKU in the normalized config and parameters. Final rechecks
validate only the pinned SKU; they do not choose a different winner. If it no longer qualifies,
rerun selection and repeat configuration/cost review before regenerating in a new directory.
Old generated configs without a pinned `vmSize` must be regenerated/reviewed.
Network quotas can expose different counters across subscriptions. The helper checks matching reported
counters and discloses that missing service/disk limits still need ARM validation. Policies, billing
restrictions, regional allocation shortages and concurrent deployments can still block a deployment.
It also reads the caller's `Microsoft.Authorization/permissions` at subscription scope for a new group,
or at the existing empty group's scope. It requires the reported create/write/join and ARM
deployment/validation/what-if actions; wildcard grants are evaluated with their per-grant `notActions`.
See [the permissions API](https://learn.microsoft.com/rest/api/authorization/permissions/list-for-resource-group?view=rest-authorization-2022-04-01).
Missing actions or an unreadable permissions response stop the flow. Ask an administrator to review
least-privilege access or required PIM activation; never automatically grant roles or ask for Owner.
Deny assignments, access conditions, policy and actual resource-level enforcement still require
IAM review and ARM validation; passing the reported-action check is not proof deployment will succeed.

Host encryption is enabled for every CPU/GPU profile with **platform-managed keys**. It does not
add a Disk Encryption Set, Key Vault or Managed HSM. Before generation and again before deployment,
the helper requires `EncryptionAtHostSupported=True` on the exact SKU and subscription feature state
`Registered`. Missing capability, missing registration or a registration in progress stops the flow.
It never registers the feature automatically or falls back to disabled encryption.
Generated parameters explicitly require host encryption. Previously generated parameter files without
that setting fail the recheck and must be regenerated/reviewed; the helper does not patch old templates.

If registration is needed, obtain permission for the subscription change, use the explicitly scoped
`az feature register` command printed by the helper, and poll the corresponding `az feature show`
until its state is `Registered`. An accepted registration request is not completion.
See [Azure host-encryption prerequisites](https://learn.microsoft.com/azure/virtual-machines/linux/disks-enable-host-based-encryption-cli).
Platform-managed host encryption matches the candidate's host-encryption setting; it is not a claim
of complete CCC compliance or customer-managed key coverage.

### PAYG cost review

Before choosing a size, run `onboarding.py prices --compute cpu --location westus2 --subscription <UUID>`
(substitute the confirmed subscription and chosen compute/region) with `--access-mode bastion` or
`--access-mode public-ip` matching the user's access choice. This uses Azure login for read-only
compute eligibility/quota selection but needs no SSH keys. Present each selected profile's VM USD/hour,
VM USD/month and whole-environment **base** USD/month, plus the shared-resource breakdown.
Disclose unavailable sizes from `skuSelection` and their reasons; they have no price and must not
be offered as deployable. If all sizes are unavailable, the command fails without pricing.
Omitting `--subscription` prices every catalog variant without login or quota selection, for
pricing-only comparisons; those rates alone do not identify a deployable SKU.
`generate` fetches prices again for the selected profile and saves `cost-estimate.json`; `check`
refreshes prices in its JSON output immediately before final approval. Do not use a saved estimate
as if it were a fresh final quote.

The calculation uses current [Azure Retail Prices](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
in USD, Linux on-demand Consumption rates and 730 VM hours/month. Disks use monthly capacity
meters: E70 LRS Disk (16 TiB Standard SSD), P10 LRS Disk (128 GiB Premium OS). Do not select shared-disk
`Disk Mount` or legacy Page Blob meters. Bastion Standard's base hourly rate **already includes two
scale units**; do not double it. In recommended `bastion` mode, two separate Standard IPv4 IPs
are billed, and NAT's published public-cloud meter is explicitly `Global`, not another region
substituted for missing regional pricing. Demo `public-ip` mode bills one Standard static IPv4
attached to the VM and neither deploys nor prices Bastion/NAT. Missing prices for undeployed
services must not block demo pricing, but VM, disk and public-IP prices remain mandatory.
VNet, subnets, NSG and NIC have no separate fixed hourly charge in this architecture.

Label totals as estimates excluding usage, not all-inclusive bills: when deployed, NAT processes
**both outbound and return traffic** and Bastion transfer can add charges. In both modes Azure
bandwidth and Standard SSD disk
transactions are usage-based. No taxes, negotiated discounts, credits, reservations or savings plans
are assumed. All fixed disks/network services keep billing during VM deallocation. The baseline
assumes no deallocation; if discussing schedules, do not prorate those fixed charges away.
Missing/ambiguous/currently unavailable prices stop generation/recheck; never silently use a stale
price, a different region/SKU, zero, or user acknowledgement in place of a verified estimate.

After permission to create the resource group:

```powershell
$subscription = '<subscription UUID>'
$resourceGroup = '<approved resource group>'
$location = 'westus2' # Replace with the approved override if applicable.
$owner = '<approved owner/team label>'

az group create --subscription $subscription --name $resourceGroup --location $location --tags "owner=$owner" workload=perspicacity-azure-onboard
az deployment group validate --subscription $subscription --resource-group $resourceGroup --template-file .azure-vm\dev\infra\main.bicep --parameters '@.azure-vm\dev\main.parameters.json'
az deployment group what-if --subscription $subscription --resource-group $resourceGroup --template-file .azure-vm\dev\infra\main.bicep --parameters '@.azure-vm\dev\main.parameters.json'
```

Stop if validation fails or what-if proposes modifying/deleting unrelated resources. Rerun the helper
`check` against generated config **and** parameters to detect changes and refresh prices, quota and
permissions. A check failure blocks deployment. Then show the what-if, subscription, region, owner,
the selected access mode and demo SSH source restriction, all resources and the **fresh itemized PAYG base estimate**, timestamp, 730-hour
assumption, persistent charges and excluded usage/taxes. This review is the final step before asking
the user to approve deployment. Any changed price or configuration requires renewed approval.
File generation or resource-group approval is not VM deployment approval. Only then deploy:

```powershell
az deployment group create --subscription $subscription --resource-group $resourceGroup --name onboarding --mode Incremental --template-file .azure-vm\dev\infra\main.bicep --parameters '@.azure-vm\dev\main.parameters.json'
az deployment group show --subscription $subscription --resource-group $resourceGroup --name onboarding --query properties.outputs
```

Do not use Complete mode. A failed deployment can leave billable resources. Inspect deployment operations
and actual resources; do not say that a failure automatically rolled everything back.

## SSH and VS Code

### Recommended: private Bastion plus NAT

`accessMode: "bastion"` is the default bundled private-access option. Deploy Bastion Standard,
NAT and their two separate Standard static public IPs; never attach a public IP to the VM.
The user needs Reader access on the VM/NIC/Bastion resources and permission to use the selected
subscription. The deploying identity needs resource creation permissions; do not grant Owner by default.
Keep every Azure command scoped to the approved subscription.

Use the deployment outputs for `<bastion-name>` and `<vm-resource-id>`. Install/enable the Azure CLI
Bastion extension only with user consent if Azure CLI reports that it is missing.

In a user-visible terminal, keep a tunnel open:

```powershell
az network bastion tunnel --subscription $subscription --resource-group $resourceGroup --name '<bastion-name>' --target-resource-id '<vm-resource-id>' --resource-port 22 --port 50022
```

In another **user-owned** terminal:

```powershell
ssh -i "$HOME\.ssh\perspicacity" -p 50022 '<username>@127.0.0.1'
```

The private key path belongs to the SSH client, not the IaC or chat. An encrypted key prompts the user
locally for its passphrase. Never weaken host-key checking to make a connection work.
On first connection, verify the host fingerprint via the VM's Azure Run Command (authorized operation):
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` runs **inside the VM**. This is a public host key,
not the user's private key. Compare its fingerprint before accepting the first-use prompt.

For VS Code Remote SSH, add a host entry in the user's SSH config only with their permission:

```sshconfig
Host perspicacity-dev
    HostName 127.0.0.1
    Port 50022
    User <username>
    IdentityFile ~/.ssh/perspicacity
    HostKeyAlias <unique-vm-name>
    IdentitiesOnly yes
```

Keep the Bastion tunnel running while using VS Code. A distinct `HostKeyAlias` avoids confusing
multiple environments using the same localhost tunnel port. No inbound SSH public IP is needed.
If using the tool host to start a tunnel, expose it in an interactive terminal; do not claim a tunnel
on a remote execution host is available on the user's laptop.

### Demo only: direct Standard public IP

For an explicitly chosen demo, set `accessMode: "public-ip"` and require `sshSourceCidr` to be
the approved administrator IPv4 CIDR (prefer a single public egress address `/32`). Do not guess it
or query an external IP service without consent. Never use `0.0.0.0/0`, `*` or an IPv6 source.
The helper validates this restriction before generation/recheck. In Bastion mode omit this field
or leave it empty. The unused Bastion subnet is not deployed or validated in demo mode.
All resources still use AVM, and password authentication remains disabled.

The demo inventory is one VNet/VM subnet, NSG, VM with NIC/disks and one Standard static public IP
attached to that NIC. No NAT, Bastion, Bastion subnet or second public IP is deployed. The public
IP supplies explicit egress with subnet default outbound access disabled. Only TCP 22 is allowed
from the approved CIDR; all other inbound traffic remains denied, including application ports
and the Docker API. This is a demo tradeoff, not the recommended private architecture.
Provider/region, quota and reported-action checks omit NAT/Bastion and require one public IP.

Use the deployment's `vmPublicIp` output, not `bastionName` (empty in demo mode). In a user-owned terminal:

```powershell
ssh -i "$HOME\.ssh\perspicacity" '<username>@<vm-public-ip>'
```

Verify the first-use host fingerprint through the same authorized Run Command described above;
do not disable host-key checking. For VS Code Remote SSH, with permission add:

```sshconfig
Host perspicacity-demo
    HostName <vm-public-ip>
    Port 22
    User <username>
    IdentityFile ~/.ssh/perspicacity
    HostKeyAlias <unique-vm-name>
    IdentitiesOnly yes
```

No tunnel or Bastion extension is needed. If the user's public egress address changes, SSH is
expected to fail until a separately reviewed/authorized NSG source change; never widen the rule
to the whole internet to restore access. The helper is new-environment-only, not an in-place
network migration tool. Deleting the VM alone may leave the public IP and other billable resources.

## Validate the deployed environment

Check VM provisioning and power state and deployment output `deployedAccessMode` against approved config.
In private mode verify both subnets, the VM subnet's NAT association, Bastion Standard/native-client
connectivity and NIC absence of a public IP. In demo mode verify only the VM subnet is deployed,
no NAT is associated, no Bastion exists, and the NIC is attached to exactly the approved Standard
static IPv4 public IP. Check the effective NSG allows TCP 22 only from the reviewed `sshSourceCidr`
and denies other inbound traffic. In both modes verify subnet default outbound access is disabled.
Verify that the deployed VM's `securityProfile.encryptionAtHost` is `true` using
`az vm show --subscription <subscription-UUID> --resource-group <resource-group> --name <vm-name> --query securityProfile.encryptionAtHost`.
Inside the VM, the user should verify internet egress with a trusted public-IP service
and compare it with `outboundPublicIp` from deployment outputs. This contacts that service; obtain
consent before doing so if sending the egress address externally is not already authorized.

### First-boot Docker and GPU setup

Cloud-init is used for initial software installation on **both CPU and GPU**. The helper builds
one `#cloud-config` document, serialized as JSON-form YAML, with root-owned shell scripts and a
single fail-fast command sequence. The exact document is saved as `infra\cloud-init.yaml` for review
and embedded in the VM parameters. Docker runs first; GPU host-driver setup follows only on GPU
profiles. A failed script stops the sequence and must not be reported as ready.

ARM provisioning success does not mean cloud-init or Docker is ready. From the user-owned SSH
session (Bastion tunnel or direct demo IP), run these checks inside every VM:

```bash
sudo cloud-init status --wait
sudo systemctl is-active docker
sudo docker --host unix:///var/run/docker.sock info --format '{{.ServerVersion}}'
sudo docker buildx version
sudo docker compose version
```

Require successful cloud-init completion and an active, responding Docker daemon/plugins before
reporting software readiness. If a check fails, inspect `sudo cloud-init status --long`,
`/var/log/cloud-init-output.log`, `/var/log/cloud-init.log` and `sudo journalctl -u docker`.
Report failure or pending/unverified setup explicitly; never substitute VM creation for these checks.

The Docker bootstrap follows the [official Ubuntu APT repository installation](https://docs.docker.com/engine/install/ubuntu/),
installs the current stable Docker Engine/CLI, containerd, Buildx and Compose, and enables the
Docker service. The key is scoped to that repository with `Signed-By`; no downloaded convenience
script is executed. Initial installation requires outbound access to Ubuntu and `download.docker.com`.
No Docker Hub image is pulled or application container started automatically.

Docker stays on its local Unix socket. Use `sudo`; adding a user to `docker` grants root-equivalent
access and requires a separate, explicit decision. No Docker TCP listener or inbound container ports
are configured. Keep the selected mode's NSG/access restrictions in place if containers are introduced later.
Docker's default data directory stays on the OS disk (`/var/lib/docker`), not the E70 disk or
temporary NVMe. Cloud-init does not format or mount disks. The separate post-creation disk step below
does not move Docker storage or change Docker's configuration.

Cloud-init runs on **first boot**, not on every restart. Updating the scripts/parameters does not
retrofit existing VMs, upgrade their Docker installation, or rerun cloud-init. Regenerate/review older
parameter files (including CPU files with empty custom data). Diagnose failures before proposing
an explicitly approved repair over SSH or Azure Run Command; do not blindly recreate the VM or rerun
all cloud-init modules. No extra Azure extension/resource is needed for initial setup.
See [Azure cloud-init provisioning](https://learn.microsoft.com/azure/virtual-machines/linux/tutorial-automate-vm-deployment).

For GPU, also run inside the VM:

```bash
nvidia-smi
```

GPU cloud-init installs build tools/current-kernel headers and the documented Microsoft-hosted
NVIDIA GRID vGPU20.2 installer using open kernel modules. It disables neither host-key verification nor
TLS verification. It fails visibly in `/var/log/cloud-init-output.log` if installation is unsuccessful.
CPU enables Secure Boot/vTPM; GPU does not because this GRID installation requires them disabled.
The bootstrap installs Docker and the host driver, not NVIDIA Container Toolkit, CUDA toolkit,
an ML framework or research containers. Host `nvidia-smi` alone does not establish GPU access inside
containers; GPU-container enablement remains a separate task.

Before updating GPU OS/driver support, reread:

- [GPU OS support](https://learn.microsoft.com/azure/virtual-machines/sizes/gpu-accelerated/nc-rtxpro6000-bse-v6-series-overview)
- [Supported GRID versions and installation](https://learn.microsoft.com/azure/virtual-machines/linux/n-series-driver-setup#grid-drivers)

Do not install generic Ubuntu NVIDIA drivers in place of the supported Azure GRID driver.
If the installed kernel/driver requires a reboot, ask before rebooting, then rerun `nvidia-smi`.
If the documentation disagrees or installation fails, surface the mismatch rather than claiming success.

## Post-creation disk preparation

Make this a normal onboarding step after VM creation, successful cloud-init and Docker/GPU readiness,
not an unattended first-boot action. This prepares disks on the environment just created by this flow;
it does not authorize adopting, reformatting or changing storage on other VMs. Updating the skill or
generated files does not modify an already-running VM. Use the user-owned SSH session (Bastion or direct demo IP), or
Azure Run Command with permission for the guest inspection and subsequent approved mutations.

The data disk is **E70: 16,384 GiB StandardSSD_LRS**, LUN 0, caching None. This is separate from both
the OS disk and the local NVMe devices. Its storage class/size selects the Standard SSD capacity tier;
do not set the Premium disk `tier: P70` property.

### Inspect and identify without changing disks

Run inside the VM:

```bash
lsblk -e 7 -o NAME,PATH,TYPE,SIZE,MODEL,SERIAL,FSTYPE,UUID,MOUNTPOINTS
findmnt -rn -o SOURCE,TARGET,FSTYPE
swapon --show
cat /etc/fstab
```

Match the VM's Azure `storageProfile` managed-disk resource ID and LUN 0 to the guest device.
For NVMe, use `sudo azure-nvme-id` from `azure-vm-utils` when available: it distinguishes `type=os`,
`type=data, lun=0` and `type=local`. For SCSI, use Azure's SCSI LUN mapping. Follow
[Azure disk identification and preparation](https://learn.microsoft.com/azure/virtual-machines/linux/attach-disk-portal)
and [temporary NVMe guidance](https://learn.microsoft.com/azure/virtual-machines/enable-nvme-temp-faqs).
If identification tools are missing, request installation permission or stop for manual identification;
do not guess. CPU managed disks also use NVMe: `/dev/nvme*`, capacity or model alone is not proof of
scratch storage, and serials are not necessarily unique. Never reuse device paths from another VM.

Explicitly exclude the OS disk and every ancestor of `/`, `/boot`, EFI, other mounted filesystems,
swap, and active device-mapper/LVM/RAID devices. For each proposed whole disk, inspect its children,
block-device holders and `sudo wipefs --no-act <exact-device>`. Require an unpartitioned,
unmounted disk with no filesystem, partition-table, swap, LVM or RAID signatures and no holders.
Inspection errors or ambiguous identity block preparation. An existing filesystem or partition is
not permission to erase it: stop and review it, even on a newly created VM.

### Review the exact-device plan and obtain consent

Recommend this layout, without pooling or RAID:

| Disk role | Layout | Mount point | Ownership |
|---|---|---|---|
| E70 managed disk, LUN 0 | GPT, one ext4 partition spanning usable disk space | `/data` | Configured Linux administrator and their primary group, mode `0750` |
| Each positively identified local NVMe disk | GPT, one ext4 partition per device | `/scratch/nvme1`, `/scratch/nvme2`, etc. | Same administrator/group, mode `0750` |

Show each exact device path, Azure role/LUN or local index, model, serial, observed size, signature
status and proposed mount point. CPU device count/capacity must match the selected profile.
For GPU, use actual identified devices and reconcile their total capacity with the profile; do not
invent a count. Keep the approved local-index/identity-to-mount mapping, not kernel enumeration order.
Resolve the administrator's primary group with `id`; Azure Run Command runs as root, so do not use
its `$USER` as the owner. Stop on missing accounts or unexpected device counts/capacities.

Explain that partitioning/formatting destroys data on the listed devices, and that scratch can be
lost on deallocation/redeployment/host events. Ask one explicit approval question covering those
exact disks, GPT/ext4, mount points, administrator ownership and `/etc/fstab` changes. Neither billable
deployment approval nor a request to update the skill grants this destructive-operation consent.
Allow the user to decline or revise the layout. No commands that modify storage run before approval.

### Prepare only the approved empty devices

Recheck identity, signatures, holders, mounts and partition state immediately before each destructive
operation. If anything differs from the reviewed plan, stop and obtain a new review. Confirm required
utilities exist; missing packages need separate installation consent. Run one disk at a time with
fail-fast error handling; never glob devices or pipe discovery output directly into destructive tools.

For each exact approved disk, create a GPT and one aligned partition from `1MiB` to `100%` with
`parted`. GPT is required for the 16 TiB E70; do not use MBR. Run `partprobe` and `udevadm settle`,
then inspect the new partition's parent, size and path before formatting that partition with
`mkfs.ext4 -m 0` (no reserved percentage on these non-OS data volumes). Do not use force flags,
`wipefs` erase modes, RAID/LVM commands or format the whole OS disk. Never derive a partition path
by blindly appending `1`: NVMe and SCSI naming differ. Record the resulting filesystem UUID with
`blkid`, require ext4 and a nonempty unique UUID, and keep it associated with the approved disk.

Require mount targets to be absent or empty real directories, not symlinks or existing mounts;
stop on nonempty paths or conflicts. Create unmounted leaf mount directories as root-owned mode
`0000`, so ordinary users cannot accidentally write to the OS disk if a data/scratch mount is absent.
The `/scratch` parent may be root-owned mode `0755`. Mount each new filesystem at its approved target,
verify the exact source/UUID and ext4 type with `findmnt --mountpoint <target>`, then set the mounted
filesystem root's owner/group and mode `0750`. Do not recursively chown existing data or use `0777`.

Back up `/etc/fstab` to a unique path before editing; preserve unrelated lines. Add exactly one entry
per new filesystem using its actual UUID, not `/dev/nvme*` or `/dev/sd*`. Example entry shapes
(placeholders are not executable configuration):

```fstab
UUID=<E70-filesystem-UUID> /data ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2
UUID=<first-local-filesystem-UUID> /scratch/nvme1 ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2
```

Add one scratch entry per actual approved local device. Reject duplicate UUIDs, conflicting targets
or existing entries that disagree with the plan; do not blindly append or replace unrelated mounts.
Run `sudo findmnt --verify --verbose` and `sudo systemctl daemon-reload`; surface all errors.
If any step fails, stop and report which disks were partitioned, formatted, mounted or persisted.
Do not erase/reformat completed disks on retry or claim a rollback; preserve the fstab backup.

### Verify and hand off storage

Before any workload uses the new mounts, test each newly prepared target by syncing, unmounting only
that target and mounting it again by mount point from `/etc/fstab`. Do not unmount pre-existing or
busy mounts, use force/lazy unmount, run a broad `mount -a`, or reboot without separate consent.
Verify again with `findmnt --mountpoint <target>`, `lsblk -f` and `df -hT <target>`: source/UUID,
ext4 type and usable capacity must match the plan. As the configured administrator (not root),
create a uniquely named temporary file on each verified mount and delete only that file to prove
write access. A successful `df` or write alone is insufficient: an unmounted directory can be on `/`.

Report the device/role, filesystem UUID, mount point, usable capacity, owner and persistence result
for every disk. If preparation was declined, blocked or only partly completed, explicitly list the
raw/unmounted or unverified devices; do not call storage ready. Docker remains on its OS-disk default.

UUID entries preserve mounts across ordinary reboots while the filesystems survive. After local
scratch loss, `nofail` lets the VM boot but old UUIDs will not mount replacement blank disks.
Never install a boot-time autoformat service. Reinspect actual devices, obtain new exact-device
formatting consent and update only the affected scratch entries. Applications must check that their
scratch/data paths are mounted before writing, especially when running as root. No important data
may exist only on scratch; it must be reconstructible.

The E70 disk uses `Detach` on VM deletion, but is **not** protected from resource-group deletion.
Detached disks keep billing and still need backups if the data matters.

## Stop and cleanup

Stopping/deallocating a VM can erase temporary NVMe data. Warn and confirm before doing it.
It does not stop charges for deployed Bastion/NAT, static public IPs or disks. For full cleanup, inventory the environment,
arrange backup/export of durable data, show all resources that would be deleted, and ask for
explicit confirmation before deleting anything. Never automatically delete the resource group.
