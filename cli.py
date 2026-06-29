"""
cli.py — a tiny command-line client for the Korral StoreLink MCP server.

Ask the server questions from the terminal, no browser needed. It goes through the real
MCP tool layer (so you see exactly what an agent would get): in-memory by default, or the
Docker container with --docker.

Because the docker arguments are passed as a Python list (not a shell string), a space in
your project path is NOT a problem here — unlike the browser Inspector.

Usage
-----
  python cli.py                          # interactive REPL (in-memory server)
  python cli.py --docker                 # interactive REPL against the Docker container
  python cli.py stores                   # one-shot command, then exit
  python cli.py stock 47 8847291
  python cli.py order 47 8847291 24 projected stockout before next delivery
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex

from fastmcp import Client

HELP = """\
Commands:
  stores                                       list stores
  sku <sku>                                    look up a SKU (+ supplier lead time)
  stock <store_id> <sku> [hours]               stock position / stockout risk
  order <store_id> <sku> <qty> <reason...>     raise a replenishment order
  dry   <store_id> <sku> <qty> <reason...>     dry-run an order (writes nothing)
  status <store_id> <order_id>                 read back an order
  help                                         show this help
  quit | exit                                  leave

Examples:
  stock 47 8847291
  order 47 8847291 24 projected stockout before next delivery
  stock 5 8847291          (demonstrates the clean missing-credential error)
"""

DOCKER_IMAGE = os.environ.get("KORRAL_IMAGE", "korral-storelink")


def make_client(use_docker: bool) -> Client:
    if use_docker:
        from fastmcp.client.transports import StdioTransport

        cwd = os.getcwd()
        return Client(
            StdioTransport(
                command="docker",
                args=[
                    "run", "-i", "--rm",
                    "-v", f"{cwd}/secrets:/run/secrets/korral:ro",
                    "-e", "KORRAL_KEYS_FILE=/run/secrets/korral/keys.json",
                    "-e", "KORRAL_AUDIT_LOG=/tmp/audit.log",
                    DOCKER_IMAGE,
                ],
            )
        )
    # In-memory: drive the real FastMCP server object directly (no subprocess).
    from server import mcp

    return Client(mcp)


async def run_command(client: Client, parts: list[str]):
    cmd, rest = parts[0], parts[1:]
    if cmd == "stores":
        return await _call(client, "list_stores", {})
    if cmd == "sku":
        return await _call(client, "get_sku", {"sku": rest[0]})
    if cmd == "stock":
        args = {"store_id": int(rest[0]), "sku": rest[1]}
        if len(rest) > 2:
            args["window_hours"] = int(rest[2])
        return await _call(client, "get_stock_position", args)
    if cmd in ("order", "dry"):
        return await _call(
            client,
            "raise_replenishment",
            {
                "store_id": int(rest[0]),
                "sku": rest[1],
                "quantity": int(rest[2]),
                "reason": " ".join(rest[3:]),
                "dry_run": cmd == "dry",
            },
        )
    if cmd == "status":
        return await _call(
            client, "get_replenishment_status", {"store_id": int(rest[0]), "order_id": rest[1]}
        )
    raise ValueError(f"unknown command: {cmd!r} (type 'help')")


async def _call(client: Client, name: str, arguments: dict):
    result = await client.call_tool(name, arguments)
    return result.data


def _show(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


async def repl(client: Client) -> None:
    async with client:
        print(f"Connected to Korral StoreLink MCP. Type 'help', or 'quit' to leave.")
        while True:
            try:
                line = input("korral> ").strip()
            except EOFError:
                print()
                break
            if not line:
                continue
            if line in ("quit", "exit"):
                break
            if line == "help":
                print(HELP)
                continue
            try:
                _show(await run_command(client, shlex.split(line)))
            except Exception as exc:  # tool errors come back clean (no traceback)
                print(f"error: {exc}")


async def one_shot(client: Client, parts: list[str]) -> None:
    async with client:
        try:
            _show(await run_command(client, parts))
        except Exception as exc:
            print(f"error: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Command-line client for the Korral MCP server.")
    parser.add_argument("--docker", action="store_true", help="talk to the Docker container")
    parser.add_argument("command", nargs="*", help="one-shot command; omit for interactive mode")
    args = parser.parse_args()

    client = make_client(args.docker)
    if args.command:
        asyncio.run(one_shot(client, args.command))
    else:
        asyncio.run(repl(client))


if __name__ == "__main__":
    main()
