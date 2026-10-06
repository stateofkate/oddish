# Network isolation

Tasks may restrict what the agent and verifier can reach. The task declares a network mode: public, no-network, or an allow-list of hosts, and may switch modes between the agent phase and the verifier phase. This page describes how those rules hold on the oddish-mon path and what changed.

## How restriction works inside the sandbox

Harbor's Docker environment runs the task's containers inside the sandbox. For any mode other than public, Harbor starts an egress-control sidecar: a small container that owns the network namespace the task containers share, and applies firewall rules with nftables. Listed hosts are reached through a proxy in the sidecar; everything else is rejected. Public tasks get no sidecar and are untouched.

oddish-mon's own heartbeats never pass through the sidecar. The monitor has its own network. A task's policy cannot affect liveness, and zero heartbeats failed in any restricted trial during verification.

## What was wrong and what was fixed

![Before: the task container holds NET_RAW, so ping and a marked socket escape a no-network task. After: the privilege is dropped and both are refused](../../../assets/diagrams/ping-before-after.svg)

Measured on both providers on 21 September 2026. Four changes to Harbor's Docker environment, merged to Harbor main on 3 October 2026 and pinned by Oddish:

- Drop the NET_RAW and NET_ADMIN privileges from every container routed through the sidecar, and refuse a task whose own Compose file asks for them or for privileged mode.
- Make no-network allow local traffic only, and keep the rules in place if the proxy exits.
- Remove the ICMP exemption from the allow-list mode, so ping follows the same rule as everything else.
- Support sandbox kernels that lack one nftables feature, which Modal's VM kernel does.

After the fix, measured on both providers: web, API, direct DNS and ping are all refused from a no-network task; listed hosts answer and unlisted do not in an allow-list task; public tasks are unchanged; a task cannot change the rules, slip past with a marked socket, or see the monitor's paths.

## Verification

Run on the final Harbor commit on 21 September, per provider: no-network 22 of 22 checks, allow-list 22 of 22, public 20 of 20, phase switching (agent under an allow-list, verifier offline) 22 of 22, a two-service task 33 of 33. Each check recorded socket creation, sending and reply separately; a timeout was counted as inconclusive, never as a denial. The installed Harbor revision was read from inside every probe sandbox.

## The one behaviour change

On the old path, a restricted task could ping a listed IP address. On the new path it cannot ping at all, because the privilege that ping needs is the same one that allowed the bypass. Public tasks keep ping. This was accepted as the price of a policy that holds. Traceroute, packet capture and raw-socket tools in restricted tasks are affected the same way.

## Refused tasks

A task whose Compose file declares a privileged service, such as one that runs its own Kubernetes or BuildKit daemon, is now refused in restricted mode on both paths rather than retried. Since 3 October Harbor reports that refusal as non-retryable, so the trial fails within seconds instead of burning attempts. Two tasks in the 40-task regression set are in this situation; their authors decide whether to drop the privilege or the restriction.
