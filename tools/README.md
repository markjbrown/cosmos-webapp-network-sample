# IP Planner Tool

This folder contains a small helper script to choose non-overlapping CIDR ranges for VNets/subnets.

## What it does

- Calls `az network vnet list` (subscription-wide) to collect all existing VNet address prefixes
- Finds the first free VNet CIDR starting at a base range (default `172.16.0.0/16`)
  - Defaults to choosing `/24` VNets as `172.<second>.<third>.0/24`
  - Increments the *third octet* first: `172.16.1.0/24`, `172.16.2.0/24`, ...
  - Then increments the *second octet* (within 172.16.0.0/12): `172.17.0.0/24`, `172.17.1.0/24`, ...
- Allocates two subnets inside that VNet (defaults match this repo)

Outputs values suitable for these Bicep parameters:

- `vnetAddressPrefix`
- `webAppSubnetAddressPrefix`
- `privateEndpointSubnetAddressPrefix`

## Usage

From the repo root:

```bash
python tools/ip_planner.py
```

Ask for the smallest ranges that satisfy a required number of usable IPs (Azure reserves 5 IPs per subnet):

```bash
# Example: need 20 usable IPs for the Web App subnet, 10 usable for Cosmos
python tools/ip_planner.py --webapp-ips 20 --cosmos-ips 10
```

Smallest typical setup (what many people want):

```bash
# Web subnet: 5 usable IPs, Cosmos subnet: 1 usable IP
python tools/ip_planner.py --webapp-ips 5 --cosmos-ips 1
```

If you also want to force the VNet size (total IPs in the VNet CIDR), provide `--vnet-ips`:

```bash
python tools/ip_planner.py --vnet-ips 256 --webapp-ips 20 --private-endpoint-ips 10
```

If you omit `--vnet-ips`, the tool defaults to the smallest VNet that can fit the requested subnets.

Specify a subscription explicitly:

```bash
python tools/ip_planner.py --subscription <subscriptionId>
```

Change the base range you want to search within:

```bash
python tools/ip_planner.py --base 172.20.0.0/16
```

Use the older "base-only" search (only searches within the provided base, using whatever `--vnet-prefix` you set):

```bash
python tools/ip_planner.py --base 10.5.0.0/16 --search-strategy base
```

Output JSON instead of the Bicep-style snippet:

```bash
python tools/ip_planner.py --format json
```

Offline mode (no `az` call):

```bash
# existing.json is a JSON array of CIDRs, e.g. ["10.5.0.0/24","10.5.1.0/24"]
python tools/ip_planner.py --existing existing.json
```
