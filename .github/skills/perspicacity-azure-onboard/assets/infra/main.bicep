targetScope = 'resourceGroup'

@description('Azure region, checked against the selected subscription and SKU by the helper.')
param location string = 'westus2'

@description('Short prefix for resources in this new environment.')
@minLength(3)
@maxLength(20)
param namePrefix string

@description('Linux administrator username.')
param adminUsername string

@description('Accountable owner/team label used for resource tags, not a role assignment.')
@minLength(1)
@maxLength(128)
param owner string

@description('SSH public key only. The private key must remain on the user machine.')
param sshPublicKey string

@description('Exact SKU resolved from the selected CPU/GPU T-shirt profile.')
param vmSize string

@description('CPU uses Trusted Launch; the supported GPU GRID driver requires Secure Boot and vTPM disabled.')
@allowed([
  'cpu'
  'gpu'
])
param computeMode string = 'cpu'

@description('Recommended private access through Bastion plus NAT, or a direct public IP for demos only.')
@allowed([
  'bastion'
  'public-ip'
])
param accessMode string = 'bastion'

@description('Required in public-ip mode: approved administrator IPv4 CIDR for inbound SSH, never 0.0.0.0/0.')
param sshSourceCidr string = ''

@description('Host encryption with Azure platform-managed keys is required for every profile.')
@allowed([
  true
])
param encryptionAtHost bool = true

@description('Canonical image reference with an exact version resolved during generation.')
param imageReference object

@description('Private IPv4 address space. The helper checks containment, alignment and overlap.')
param vnetCidr string = '192.168.0.0/24'

@description('Dedicated AzureBastionSubnet, /26 or larger.')
param bastionSubnetCidr string = '192.168.0.0/26'

@description('VM subnet, with explicit outbound connectivity through NAT or the VM public IP.')
param vmSubnetCidr string = '192.168.0.64/26'

@description('Required first-boot cloud-init installs Docker for all profiles and the supported host driver for GPU. Never include secrets.')
@minLength(1)
param customData string

@description('Resource tags.')
param tags object = {
  workload: 'perspicacity-azure-onboard'
}

var resourceTags = union(tags, { owner: owner })
var privateAccess = accessMode == 'bastion'

module outboundIp 'br/public:avm/res/network/public-ip-address:0.13.0' = {
  name: 'outbound-ip'
  params: {
    name: privateAccess ? '${namePrefix}-nat-pip' : '${namePrefix}-vm-pip'
    location: location
    skuName: 'Standard'
    publicIPAllocationMethod: 'Static'
    availabilityZones: []
    tags: resourceTags
    enableTelemetry: false
  }
}

module bastionIp 'br/public:avm/res/network/public-ip-address:0.13.0' = if (privateAccess) {
  name: 'bastion-ip'
  params: {
    name: '${namePrefix}-bastion-pip'
    location: location
    skuName: 'Standard'
    publicIPAllocationMethod: 'Static'
    availabilityZones: []
    tags: resourceTags
    enableTelemetry: false
  }
}

module nat 'br/public:avm/res/network/nat-gateway:2.1.1' = if (privateAccess) {
  name: 'nat'
  params: {
    name: '${namePrefix}-nat'
    location: location
    availabilityZone: -1
    natGatewaySku: 'Standard'
    publicIpResourceIds: [
      outboundIp.outputs.resourceId
    ]
    tags: resourceTags
    enableTelemetry: false
  }
}

module vmNsg 'br/public:avm/res/network/network-security-group:0.5.4' = {
  name: 'vm-nsg'
  params: {
    name: '${namePrefix}-vm-nsg'
    location: location
    securityRules: [
      {
        name: privateAccess ? 'AllowSshFromBastion' : 'AllowSshFromApprovedSource'
        properties: {
          priority: 100
          access: 'Allow'
          direction: 'Inbound'
          protocol: 'Tcp'
          sourceAddressPrefix: privateAccess ? bastionSubnetCidr : sshSourceCidr
          sourcePortRange: '*'
          destinationAddressPrefix: vmSubnetCidr
          destinationPortRange: '22'
        }
      }
      {
        name: 'DenyOtherInbound'
        properties: {
          priority: 200
          access: 'Deny'
          direction: 'Inbound'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
    tags: resourceTags
    enableTelemetry: false
  }
}

module vnet 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: 'vnet'
  params: {
    name: '${namePrefix}-vnet'
    location: location
    addressPrefixes: [
      vnetCidr
    ]
    subnets: concat(privateAccess ? [
      {
        name: 'AzureBastionSubnet'
        addressPrefix: bastionSubnetCidr
      }
    ] : [], [
      {
        name: 'vm'
        addressPrefix: vmSubnetCidr
        defaultOutboundAccess: false
        natGatewayResourceId: privateAccess ? nat!.outputs.resourceId : null
        networkSecurityGroupResourceId: vmNsg.outputs.resourceId
      }
    ])
    tags: resourceTags
    enableTelemetry: false
  }
}

module bastion 'br/public:avm/res/network/bastion-host:0.8.2' = if (privateAccess) {
  name: 'bastion'
  params: {
    name: '${namePrefix}-bastion'
    location: location
    virtualNetworkResourceId: vnet.outputs.resourceId
    bastionSubnetPublicIpResourceId: bastionIp!.outputs.resourceId
    skuName: 'Standard'
    scaleUnits: 2
    enableFileCopy: false
    enableShareableLink: false
    tags: resourceTags
    enableTelemetry: false
  }
}

module vm 'br/public:avm/res/compute/virtual-machine:0.22.3' = {
  name: 'vm'
  params: {
    name: '${namePrefix}-vm'
    location: location
    osType: 'Linux'
    vmSize: vmSize
    availabilityZone: -1
    adminUsername: adminUsername
    disablePasswordAuthentication: true
    publicKeys: [
      {
        path: '/home/${adminUsername}/.ssh/authorized_keys'
        keyData: sshPublicKey
      }
    ]
    imageReference: imageReference
    encryptionAtHost: encryptionAtHost
    securityType: computeMode == 'cpu' ? 'TrustedLaunch' : null
    secureBootEnabled: computeMode == 'cpu'
    vTpmEnabled: computeMode == 'cpu'
    diskControllerType: computeMode == 'cpu' ? 'NVMe' : 'SCSI'
    managedIdentities: {
      systemAssigned: true
    }
    osDisk: {
      diskSizeGB: 128
      createOption: 'FromImage'
      deleteOption: 'Delete'
      caching: 'None'
      managedDisk: {
        storageAccountType: 'Premium_LRS'
      }
    }
    dataDisks: [
      {
        name: '${namePrefix}-durable-e70'
        lun: 0
        diskSizeGB: 16384
        createOption: 'Empty'
        deleteOption: 'Detach'
        caching: 'None'
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
      }
    ]
    nicConfigurations: [
      {
        name: '${namePrefix}-nic'
        deleteOption: 'Delete'
        enableAcceleratedNetworking: true
        ipConfigurations: [
          {
            name: 'ipconfig'
            subnetResourceId: '${vnet.outputs.resourceId}/subnets/vm'
            privateIPAllocationMethod: 'Dynamic'
            pipConfiguration: privateAccess ? null : {
              publicIPAddressResourceId: outboundIp.outputs.resourceId
            }
          }
        ]
      }
    ]
    bootDiagnostics: true
    patchMode: 'ImageDefault'
    customData: customData
    tags: resourceTags
    enableTelemetry: false
  }
}

@description('VM resource ID, also used as the Bastion tunnel target in private mode.')
output vmResourceId string = vm.outputs.resourceId

@description('Selected access architecture.')
output deployedAccessMode string = accessMode

@description('Bastion name in private mode; empty for direct public-IP demos.')
output bastionName string = privateAccess ? bastion!.outputs.name : ''

@description('Static egress IP: NAT in private mode, NIC-attached VM IP in demo mode.')
output outboundPublicIp string = outboundIp.outputs.ipAddress

@description('Direct SSH address for public-IP demos; empty in private mode.')
output vmPublicIp string = privateAccess ? '' : outboundIp.outputs.ipAddress

@description('Managed durable data disk, detached rather than deleted when only the VM is deleted.')
output durableDiskName string = '${namePrefix}-durable-e70'
