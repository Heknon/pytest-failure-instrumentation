---
title: "Serve live stacks from a container"
description: "Run pytest-failure-instrumentation's live stack server inside a container so a UI outside it, such as Sahara, can read the run's workers and stacks."
pageType: how-to
owner: "morphine"
---

When pytest runs in a container and something outside it, such as the Sahara
server, must read the run's workers and their live call stacks.

## Before you start

- [Failure instrumentation is installed](/failure-instrumentation/how-to/turn-it-on/).
- The container may use ptrace. Docker's default seccomp profile permits it;
  where it is refused, add the `SYS_PTRACE` capability.
- On the host, `/proc/sys/kernel/yama/ptrace_scope` is `0` or `1`. It is
  host-wide and a container cannot change it.

## Steps

1. Pick a token and put it in the environment, never in a file in the
   repository:

   ```bash
   export PYTEST_CALLSTACK_TOKEN=<secret>
   ```

   A server listening beyond loopback refuses to start without one.
2. Start pytest with the server bound to every interface, on a named port:

   ```bash
   pytest -n8 --callstack-host 0.0.0.0 --callstack-port <port>
   ```

   Naming `--callstack-host` or `--callstack-port` switches the plugin on by
   itself. Without `--callstack-port` a free port is drawn and written to
   `callstack-<pid>.json` in the run's evidence directory.
3. Publish the port (`docker run -p <port>:<port> …`), or run the container
   with host networking. On GitLab CI, see [Set up the live worker view on
   GitLab CI](/sahara/ui/how-to/set-up-the-live-worker-view/).

## Check it worked

From outside the container:

```bash
curl -H "Authorization: Bearer $PYTEST_CALLSTACK_TOKEN" <host>:<port>/workers
```

lists the run's workers. `/stack?worker=gw0` returns one worker's stack.

If a stack read answers `ptrace is not permitted`, check `ptrace_scope` on the
host and add `--cap-add=SYS_PTRACE` to the container.
