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

Collect an accountable owner/team identifier, not credentials or personal contact details. The required
`owner` parameter tags all seven top-level AVM resources; the resource-group command below applies
the same tag to the group. A tag does not grant RBAC access. Requests generated before this field
was added must be regenerated with an owner and reviewed.

The helper checks eligibility and headroom **even for West US 2**. A quota failure is not a capacity
failure. Check Azure Quotas for both Total Regional vCPUs and the exact VM family returned by Azure.
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

Before choosing a size, run `onboarding.py prices --compute cpu --location westus2` (substitute the
chosen family/region). It needs neither Azure login nor SSH keys. Present each profile's VM USD/hour,
VM USD/month and whole-environment **base** USD/month, plus the shared-resource breakdown.
`generate` fetches prices again for the selected profile and saves `cost-estimate.json`; `check`
refreshes prices in its JSON output immediately before final approval. Do not use a saved estimate
as if it were a fresh final quote.

The calculation uses current [Azure Retail Prices](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
in USD, Linux on-demand Consumption rates and 730 VM hours/month. Disks use monthly capacity
meters: E70 LRS Disk (16 TiB Standard SSD), P10 LRS Disk (128 GiB Premium OS). Do not select shared-disk
`Disk Mount` or legacy Page Blob meters. Bastion Standard's base hourly rate **already includes two
scale units**; do not double it. Two separate Standard IPv4 IPs are billed. NAT's published public-cloud
meter is explicitly `Global`, not another region substituted for missing regional pricing.
VNet, subnets, NSG and NIC have no separate fixed hourly charge in this architecture.

Label totals as estimates excluding usage, not all-inclusive bills: NAT processes **both outbound
and return traffic**, Azure bandwidth and Bastion transfer can add charges, and Standard SSD disk
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
Bastion access, all resources and the **fresh itemized PAYG base estimate**, timestamp, 730-hour
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

## Validate the deployed environment

Check VM provisioning and power state, both subnets, the VM subnet's NAT association, and NIC absence
of a public IP. Verify that the deployed VM's `securityProfile.encryptionAtHost` is `true` using
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

ARM provisioning success does not mean cloud-init or Docker is ready. From the user-owned Bastion
SSH session, run these checks inside every VM:

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
are configured. Keep NSG/Bastion restrictions in place if containers are introduced later.
Docker's default data directory is on the OS disk (`/var/lib/docker`), not the unmounted E70 disk or
temporary NVMe. No disks are formatted/mounted and Docker storage is not relocated automatically.

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

## Disks

The data disk is **E70: 16,384 GiB StandardSSD_LRS**, LUN 0, caching None. This is separate from both
the OS disk and the local NVMe devices. Its storage class/size selects the Standard SSD capacity tier;
do not set the Premium disk `tier: P70` property.

Inspect disks with `lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINTS` and Azure's LUN/device mapping.
Use [Azure NVMe disk identification](https://learn.microsoft.com/azure/virtual-machines/enable-nvme-interface)
and [temporary NVMe disk guidance](https://learn.microsoft.com/azure/virtual-machines/enable-nvme-temp-faqs).
CPU remote managed disks also use NVMe, so `/dev/nvme*` alone is **not** proof a device is scratch space.
Device paths may differ by kernel/image/controller.

Do not autoformat on boot. If the user wants mounted scratch/data storage, identify the devices,
show their models/serials/LUNs/filesystems, propose mount points and ask for explicit destructive
format/pool approval for the exact devices. Never run `mkfs`/RAID commands on an ambiguous device or
an existing filesystem. Use filesystem UUIDs rather than transient device names for durable mounts.
Local scratch must be reconstructible after host events; no important data should exist there alone.

The E70 disk uses `Detach` on VM deletion, but is **not** protected from resource-group deletion.
Detached disks keep billing and still need backups if the data matters.

## Stop and cleanup

Stopping/deallocating a VM can erase temporary NVMe data. Warn and confirm before doing it.
It does not stop Bastion/NAT/public-IP/disk charges. For full cleanup, inventory the environment,
arrange backup/export of durable data, show all resources that would be deleted, and ask for
explicit confirmation before deleting anything. Never automatically delete the resource group.
