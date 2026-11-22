# Upstream note for MiniLocker: a per-sandbox egress allowlist

ToolForge uses MiniLocker's egress proxy for every tool that has approved network hosts or brokered secrets. Three properties of that
proxy make approvals weaker than they read, and each has a small fix in MiniLocker. ToolForge documents the gap
(`docs/approvals.md`, the Blast Radius footer) and works around it only by measuring.

## What happens today

1. `EgressManager(allowed_hosts, ...)` takes one list for the whole proxy. ToolForge starts it with the union of the hosts of every tool
   that ran since the process started, so a tool approved for `a.example` can reach `b.example` while a tool approved for `b.example` has
   run before.
2. The same is true of broker services: `proxy_env()` gives every sandbox the placeholders of every service the proxy was started with.
3. `host_allowed` accepts a host's subdomains (`www.example.org` for `example.org`).

## The change that would close it

* Key the allowlist and the broker services by the sandbox's address (the proxy already knows the client IP and ToolForge already reads
  events by it): `EgressManager.register(client_ip, hosts, services)` and `unregister(client_ip)`, with the proxy refusing a client it does
  not know.
* Make subdomain matching opt-in, for example `*.example.org` for a subdomain and `example.org` for exactly that host.

With that, ToolForge would register each sandbox's own hosts and services when it opens it, drop the union logic in `EgressPool`, and
the report's "undeclared host" violation would also be something the proxy refused.

## Why ToolForge does not patch it itself

`third_party/minilocker` is a pinned submodule. ToolForge imports it through one seam and does not edit it.
