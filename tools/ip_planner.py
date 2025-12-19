#!/usr/bin/env python3
"""IP Planner for Azure VNets/Subnets.

Goal:
- Query existing VNet address spaces in a subscription
- Find the first non-overlapping VNet CIDR in a chosen base range
- Allocate required subnets inside that VNet
- Print values suitable for filling Bicep parameters:
    - vnetAddressPrefix
    - webAppSubnetAddressPrefix
    - privateEndpointSubnetAddressPrefix

This script intentionally uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Plan:
    vnet: ipaddress.IPv4Network
    webapp_subnet: ipaddress.IPv4Network
    private_endpoint_subnet: ipaddress.IPv4Network

    def to_dict(self) -> dict:
        return {
            "vnetAddressPrefix": str(self.vnet),
            "webAppSubnetAddressPrefix": str(self.webapp_subnet),
            "privateEndpointSubnetAddressPrefix": str(self.private_endpoint_subnet),
        }


def _run_az_json(args: Sequence[str]) -> object:
    az_path = shutil.which("az")
    if not az_path:
        raise RuntimeError(
            "Azure CLI 'az' was not found in PATH. Install Azure CLI and restart your terminal."
        )

    # On Windows, 'az' is commonly an az.cmd wrapper. subprocess can't directly execute .cmd/.bat
    # without going through cmd.exe.
    az_is_cmd = os.name == "nt" and az_path.lower().endswith((".cmd", ".bat"))
    command = [az_path, *args, "-o", "json"]
    completed = subprocess.run(
        command,
        shell=az_is_cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        message = stderr or stdout or f"az exited with code {completed.returncode}"
        raise RuntimeError(message)

    out = (completed.stdout or "").strip()
    if not out:
        return []
    return json.loads(out)


def get_existing_vnet_prefixes(subscription: Optional[str]) -> List[ipaddress.IPv4Network]:
    # We intentionally query only addressSpace.addressPrefixes because the request
    # is about avoiding overlaps in the subscription.
    az_args = [
        "network",
        "vnet",
        "list",
        "--query",
        "[].addressSpace.addressPrefixes[]",
    ]
    if subscription:
        az_args.extend(["--subscription", subscription])

    data = _run_az_json(az_args)
    prefixes: List[ipaddress.IPv4Network] = []
    if isinstance(data, list):
        for item in data:
            if not item:
                continue
            try:
                net = ipaddress.ip_network(str(item), strict=False)
            except ValueError:
                continue
            if isinstance(net, ipaddress.IPv4Network):
                prefixes.append(net)
    return prefixes


def parse_network(value: str) -> ipaddress.IPv4Network:
    net = ipaddress.ip_network(value, strict=True)
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError("Only IPv4 is supported in this tool")
    return net


def _next_power_of_two(n: int) -> int:
    if n <= 0:
        raise ValueError("IP count must be a positive integer")
    # Small, dependency-free power-of-two helper
    return 1 << (n - 1).bit_length()


def prefix_len_for_total_addresses(total_addresses: int) -> int:
    """Return smallest IPv4 prefix length that has >= total_addresses."""
    total = _next_power_of_two(total_addresses)
    if total > 2**32:
        raise ValueError("Requested range is too large for IPv4")
    # total = 2^(32-prefix)
    prefix_len = 32 - (total.bit_length() - 1)
    if prefix_len < 0 or prefix_len > 32:
        raise ValueError("Invalid prefix computed")
    return prefix_len


def subnet_prefix_len_for_usable_ips(usable_ips: int) -> int:
    """Azure reserves 5 IPs per subnet; treat input as usable IP requirement."""
    # https://learn.microsoft.com/azure/virtual-network/ip-services/virtual-network-ip-addresses-overview
    required_total = usable_ips + 5
    return prefix_len_for_total_addresses(required_total)


def overlaps_any(candidate: ipaddress.IPv4Network, used: Iterable[ipaddress.IPv4Network]) -> bool:
    for u in used:
        if candidate.overlaps(u):
            return True
    return False


def first_free_subnet(
    base: ipaddress.IPv4Network,
    prefix_len: int,
    used: Sequence[ipaddress.IPv4Network],
) -> ipaddress.IPv4Network:
    if prefix_len < base.prefixlen:
        raise ValueError(
            f"Requested prefix /{prefix_len} is larger than base {base} (/ {base.prefixlen})."
        )

    for candidate in base.subnets(new_prefix=prefix_len):
        if not overlaps_any(candidate, used):
            return candidate
    raise RuntimeError(
        f"No free /{prefix_len} networks found inside base range {base} that avoid overlaps."
    )


def allocate_two_subnets(
    vnet: ipaddress.IPv4Network,
    webapp_prefix_len: int,
    pe_prefix_len: int,
) -> Tuple[ipaddress.IPv4Network, ipaddress.IPv4Network]:
    # Allocate deterministically from low to high.
    # We assume subnets are intended to be non-overlapping and inside the vnet.
    if webapp_prefix_len < vnet.prefixlen or pe_prefix_len < vnet.prefixlen:
        raise ValueError("Subnet prefixes must be within the VNet prefix")

    # Allocate deterministically from low to high, placing the larger subnet first
    # to make packing more reliable.
    # Note: smaller prefix_len => larger subnet.
    first_prefix = min(webapp_prefix_len, pe_prefix_len)
    second_prefix = max(webapp_prefix_len, pe_prefix_len)

    first = next(vnet.subnets(new_prefix=first_prefix))
    used = [first]
    for cand in vnet.subnets(new_prefix=second_prefix):
        if not overlaps_any(cand, used):
            second = cand
            # Map back to the named outputs
            if first_prefix == webapp_prefix_len:
                return first, second
            return second, first

    raise RuntimeError(
        f"Unable to allocate two subnets (/ {webapp_prefix_len} and / {pe_prefix_len}) within VNet {vnet}."
    )


def build_plan(
    used_prefixes: Sequence[ipaddress.IPv4Network],
    base: ipaddress.IPv4Network,
    vnet_prefix_len: int,
    webapp_prefix_len: int,
    pe_prefix_len: int,
) -> Plan:
    # Try requested vnet size first; if packing fails, increase vnet size until it works.
    # Smaller prefix_len => larger network.
    # Only consider VNets within the base range.
    for prefix_len in range(vnet_prefix_len, base.prefixlen - 1, -1):
        try:
            vnet = first_free_subnet(base=base, prefix_len=prefix_len, used=used_prefixes)
        except RuntimeError:
            # No free networks at this size; try a larger VNet.
            continue
        try:
            webapp, pe = allocate_two_subnets(
                vnet=vnet,
                webapp_prefix_len=webapp_prefix_len,
                pe_prefix_len=pe_prefix_len,
            )
            return Plan(vnet=vnet, webapp_subnet=webapp, private_endpoint_subnet=pe)
        except RuntimeError:
            # Not enough room; try a larger vnet.
            continue

    raise RuntimeError(
        "Unable to allocate requested subnets in any available VNet size within the base range."
    )


def _pool_for_octet_search(start_base: ipaddress.IPv4Network) -> tuple[ipaddress.IPv4Network, range]:
    # Returns (pool_network, allowed_second_octets)
    if start_base.prefixlen != 16:
        raise ValueError("Octet-based search requires --base to be a /16 (example: 172.16.0.0/16)")

    addr = start_base.network_address
    ten_slash8 = ipaddress.IPv4Network("10.0.0.0/8")
    if addr in ten_slash8:
        # 10.0.0.0/8 pool; second octet can be 0..255
        return ten_slash8, range(0, 256)

    one_seventy_two_slash12 = ipaddress.IPv4Network("172.16.0.0/12")
    if addr in one_seventy_two_slash12:
        # 172.16.0.0/12 pool; second octet can be 16..31
        return one_seventy_two_slash12, range(16, 32)

    raise ValueError(
        "Octet-based search supports only 10.x.0.0/16 (within 10.0.0.0/8) or 172.16-31.0.0/16 (within 172.16.0.0/12). "
        "Use --search-strategy base for other ranges."
    )


def _iter_third_then_second_octet_vnets(
    start_base: ipaddress.IPv4Network,
    vnet_prefix_len: int,
    start_third_octet: int,
) -> Iterable[ipaddress.IPv4Network]:
    # Requested behavior:
    # - Prefer ranges like 10.5.1.x, then 10.5.2.x, ... (increment third octet)
    # - When exhausted, move to 10.6.x.x, 10.7.x.x, ... (increment second octet)
    #
    # Implementation detail:
    # - We iterate /24 "buckets" in that order (10.<second>.<third>.0/24)
    # - Within each /24 bucket we try the desired VNet prefix (e.g., /27) from low to high.
    if vnet_prefix_len < 24:
        raise ValueError(
            "Octet-based search requires --vnet-prefix to be /24 or smaller networks (e.g., 24, 25, 26, 27). "
            "Use --search-strategy base for larger VNets like /23 or /22."
        )
    pool, allowed_seconds = _pool_for_octet_search(start_base)
    if not (0 <= start_third_octet <= 255):
        raise ValueError("start_third_octet must be between 0 and 255")

    a, b, _, _ = str(start_base.network_address).split(".")
    start_second = int(b)
    if start_second not in allowed_seconds:
        raise ValueError(
            f"--base {start_base} is not within the allowed pool {pool} for octet-based search."
        )

    for second in range(start_second, allowed_seconds.stop):
        third_start = start_third_octet if second == start_second else 0
        for third in range(third_start, 256):
            bucket = ipaddress.IPv4Network(f"{a}.{second}.{third}.0/24")
            if vnet_prefix_len == 24:
                yield bucket
            else:
                for cand in bucket.subnets(new_prefix=vnet_prefix_len):
                    yield cand


def build_plan_with_rollover(
    used_prefixes: Sequence[ipaddress.IPv4Network],
    start_base: ipaddress.IPv4Network,
    vnet_prefix_len: int,
    webapp_prefix_len: int,
    pe_prefix_len: int,
    start_third_octet: int,
) -> Plan:
    last_error: Optional[Exception] = None
    candidates_tried = 0

    for vnet in _iter_third_then_second_octet_vnets(
        start_base=start_base,
        vnet_prefix_len=vnet_prefix_len,
        start_third_octet=start_third_octet,
    ):
        candidates_tried += 1
        if overlaps_any(vnet, used_prefixes):
            continue
        try:
            webapp, pe = allocate_two_subnets(
                vnet=vnet,
                webapp_prefix_len=webapp_prefix_len,
                pe_prefix_len=pe_prefix_len,
            )
            return Plan(vnet=vnet, webapp_subnet=webapp, private_endpoint_subnet=pe)
        except Exception as ex:  # noqa: BLE001
            last_error = ex
            continue

    # Better diagnostics for a very common case: an existing VNet covers the entire pool.
    pool, _ = _pool_for_octet_search(start_base)
    if any(pool.subnet_of(u) for u in used_prefixes):
        raise RuntimeError(
            f"No available ranges were found because an existing VNet address space in the subscription covers all of {pool}. "
            "Choose a different private range (for example, switch between 10.0.0.0/8 and 172.16.0.0/12) or use --search-strategy base."
        )

    if last_error is not None:
        raise RuntimeError(
            f"Unable to find a non-overlapping VNet after trying {candidates_tried} candidates starting at {start_base}. "
            f"Last error: {last_error}"
        )

    raise RuntimeError(
        f"Unable to find a non-overlapping VNet after trying {candidates_tried} candidates starting at {start_base}."
    )


def print_bicep_snippet(plan: Plan) -> None:
    # Intentionally prints only the param assignments the user asked for.
    d = plan.to_dict()
    print("# Bicep parameter values")
    print(f"vnetAddressPrefix: {d['vnetAddressPrefix']}")
    print(f"webAppSubnetAddressPrefix: {d['webAppSubnetAddressPrefix']}")
    print(f"privateEndpointSubnetAddressPrefix: {d['privateEndpointSubnetAddressPrefix']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Find a non-overlapping VNet CIDR in a subscription and allocate required subnets. "
            "Defaults start searching at 172.16.0.0 and produce a /24 VNet (256 IPs) plus two /27 subnets (32 IPs each)."
        )
    )
    parser.add_argument(
        "--subscription",
        help=(
            "Azure subscription id or name to use for az queries. "
            "If omitted, uses the default subscription in az CLI context."
        ),
        default=None,
    )
    parser.add_argument(
        "--base",
        help=(
            "Starting base CIDR to search within (IPv4). Default: 172.16.0.0/16. "
            "With the default 'octets' strategy, the tool searches VNets as /24 blocks inside <first>.<second>.<third>.0/24, "
            "incrementing the third octet first (172.16.1.0/24, 172.16.2.0/24, ...) and then the second octet (172.17.0.0/24, 172.17.1.0/24, ...)."
        ),
        default="172.16.0.0/16",
        type=parse_network,
    )
    parser.add_argument(
        "--start-third-octet",
        help=(
            "When using the default octet-based search, the first VNet tried will be 10.<second>.<startThird>.0/24. "
            "Default: 1 (so 10.5.1.0/24 is tried before 10.5.0.0/24)."
        ),
        type=int,
        default=1,
    )
    parser.add_argument(
        "--search-strategy",
        choices=["octets", "base"],
        default="octets",
        help=(
            "Search strategy. 'octets' (default) uses 10.<second>.<third>.0/24 ordering (increment third then second). "
            "'base' searches only within --base using the chosen --vnet-prefix."
        ),
    )
    parser.add_argument(
        "--vnet-prefix",
        help="VNet prefix length. Default: 24 (256 addresses)",
        default=24,
        type=int,
    )
    parser.add_argument(
        "--webapp-subnet-prefix",
        help="Web App subnet prefix length. Default: 27 (32 addresses)",
        default=27,
        type=int,
    )
    parser.add_argument(
        "--private-endpoint-subnet-prefix",
        help="Private Endpoint subnet prefix length. Default: 27 (32 addresses)",
        default=27,
        type=int,
    )

    # IP-count based inputs (optional). If provided, they override the *-prefix values.
    parser.add_argument(
        "--webapp-ips",
        help="Usable IPs needed in the Web App subnet (Azure reserves 5).",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--private-endpoint-ips",
        help="Usable IPs needed in the Private Endpoint subnet (Azure reserves 5).",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--cosmos-ips",
        help=(
            "Alias for --private-endpoint-ips. Usable IPs needed in the Cosmos subnet (Azure reserves 5). "
            "Use this when you're thinking of the subnet as 'Cosmos' rather than 'Private Endpoint'."
        ),
        type=int,
        default=None,
    )
    parser.add_argument(
        "--vnet-ips",
        help=(
            "Total IPs to allocate for the VNet address space. If omitted, the tool picks the smallest VNet that "
            "can fit the requested subnets."
        ),
        type=int,
        default=None,
    )
    parser.add_argument(
        "--format",
        choices=["bicep", "json"],
        default="bicep",
        help="Output format. Default: bicep",
    )
    parser.add_argument(
        "--existing",
        help=(
            "Optional path to a JSON file containing an array of existing CIDRs. "
            "If provided, the script will not call az."
        ),
        default=None,
    )

    args = parser.parse_args(argv)

    try:
        if args.existing:
            with open(args.existing, "r", encoding="utf-8") as f:
                raw = json.load(f)
            used: List[ipaddress.IPv4Network] = []
            if isinstance(raw, list):
                for item in raw:
                    try:
                        net = ipaddress.ip_network(str(item), strict=False)
                    except ValueError:
                        continue
                    if isinstance(net, ipaddress.IPv4Network):
                        used.append(net)
        else:
            used = get_existing_vnet_prefixes(subscription=args.subscription)

        webapp_prefix_len = args.webapp_subnet_prefix
        pe_prefix_len = args.private_endpoint_subnet_prefix
        vnet_prefix_len = args.vnet_prefix

        using_ip_sizing = False

        if args.webapp_ips is not None:
            webapp_prefix_len = subnet_prefix_len_for_usable_ips(args.webapp_ips)
            using_ip_sizing = True
        pe_usable_ips = args.private_endpoint_ips
        if pe_usable_ips is None and args.cosmos_ips is not None:
            pe_usable_ips = args.cosmos_ips

        if pe_usable_ips is not None:
            pe_prefix_len = subnet_prefix_len_for_usable_ips(pe_usable_ips)
            using_ip_sizing = True
        if args.vnet_ips is not None:
            vnet_prefix_len = prefix_len_for_total_addresses(args.vnet_ips)
        elif using_ip_sizing:
            # Start with the smallest vnet that can plausibly fit both subnets.
            # If packing fails due to alignment, build_plan will expand.
            web_total = 2 ** (32 - webapp_prefix_len)
            pe_total = 2 ** (32 - pe_prefix_len)
            vnet_prefix_len = prefix_len_for_total_addresses(web_total + pe_total)

        if args.search_strategy == "base":
            plan = build_plan(
                used_prefixes=used,
                base=args.base,
                vnet_prefix_len=vnet_prefix_len,
                webapp_prefix_len=webapp_prefix_len,
                pe_prefix_len=pe_prefix_len,
            )
        else:
            plan = build_plan_with_rollover(
                used_prefixes=used,
                start_base=args.base,
                vnet_prefix_len=vnet_prefix_len,
                webapp_prefix_len=webapp_prefix_len,
                pe_prefix_len=pe_prefix_len,
                start_third_octet=args.start_third_octet,
            )

        if args.format == "json":
            print(json.dumps(plan.to_dict(), indent=2))
        else:
            print_bicep_snippet(plan)

        return 0

    except Exception as ex:  # noqa: BLE001
        print(f"ERROR: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
