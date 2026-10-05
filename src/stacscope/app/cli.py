"""stacscope: run the instrument's engine, or tell it what to do.

    stacscope serve [--config config/rig.toml]
        Start the rig the file describes, and listen for clients, until
        Ctrl-C. Prints the address clients connect to.

    stacscope status
        The Coordinator's state, its latest acquisition, and every device.
    stacscope start [--cycles N] [--root NAME]
        Start an acquisition of N cycles (until stopped, if not given),
        recorded to files named from NAME (not recorded, if not given).
    stacscope stop
        Stop the acquisition running.
    stacscope relaunch NAME
        Start the lost device NAME again.
    stacscope watch
        Print everything the Coordinator publishes, until Ctrl-C.

The commands other than serve reach the engine at ws://127.0.0.1:8765, or
at --url. They exit with 0 when done, 1 when the engine refuses (saying
why), 2 for a mistake in the command or the config file, and 3 when the
engine cannot be reached or does not answer within --timeout seconds.
"""

import argparse
import sys

from stacscope.client import DEFAULT_URL, TIMEOUT, AskTimeout, Client, Disconnected, Refused

DEFAULT_CONFIG = "config/rig.toml"

OK, REFUSED, MISTAKE, UNREACHABLE = 0, 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    """Runs the command in ``argv`` (the process's own by default) and
    returns its exit status."""
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args.config)
    try:
        with Client(args.url, timeout=args.timeout) as client:
            return args.run(client, args)
    except Refused as exc:
        print(f"refused: {exc.reason}", file=sys.stderr)
        return REFUSED
    except (Disconnected, AskTimeout) as exc:
        print(str(exc), file=sys.stderr)
        return UNREACHABLE
    except KeyboardInterrupt:
        return OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stacscope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    serve = commands.add_parser("serve", help="run the engine until Ctrl-C")
    serve.add_argument("--config", default=DEFAULT_CONFIG, help=f"the rig config (default: {DEFAULT_CONFIG})")

    def client_command(name: str, run, help: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help)
        command.add_argument("--url", default=DEFAULT_URL, help=f"where the engine listens (default: {DEFAULT_URL})")
        command.add_argument(
            "--timeout", type=float, default=TIMEOUT, help=f"seconds to wait for an answer (default: {TIMEOUT:g})"
        )
        command.set_defaults(run=run)
        return command

    client_command("status", _status, "the Coordinator's state, and every device")
    start = client_command("start", _start, "start an acquisition")
    start.add_argument("--cycles", type=int, default=-1, help="cycles to run (default: until stopped)")
    start.add_argument("--root", default="", help="name the recorded files from this (default: not recorded)")
    client_command("stop", _stop, "stop the acquisition running")
    relaunch = client_command("relaunch", _relaunch, "start a lost device again")
    relaunch.add_argument("name", help="the device's name")
    client_command("watch", _watch, "print what the Coordinator publishes, until Ctrl-C")
    return parser


def _serve(path: str) -> int:
    # Imported here: the client commands need none of the rig.
    from stacscope.app.config import ConfigError, load_config
    from stacscope.app.serve import run

    try:
        config = load_config(path)
    except (ConfigError, OSError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return MISTAKE
    try:
        run(config)
    except KeyboardInterrupt:
        pass  # pressed before the engine was running: nothing to close
    return OK


def _status(client: Client, args: argparse.Namespace) -> int:
    status = client.status()
    print(f"{status.state.value}, acquisition {status.acquisition}")
    width = max((len(d.name) for d in status.devices), default=0)
    for device in status.devices:
        role = "timing source" if device.source else "device"
        need = "required" if device.required else "optional"
        state = "running" if device.present else "lost"
        print(f"  {device.name:<{width}}  {role:<13}  {need:<8}  {state}")
    return OK


def _start(client: Client, args: argparse.Namespace) -> int:
    number = client.start(args.cycles, args.root)
    print(f"acquisition {number} started")
    return OK


def _stop(client: Client, args: argparse.Namespace) -> int:
    print(f"acquisition {client.stop()} stopped")
    return OK


def _relaunch(client: Client, args: argparse.Namespace) -> int:
    client.relaunch(args.name)
    print(f"{args.name} is starting")
    return OK


def _watch(client: Client, args: argparse.Namespace) -> int:
    with client.subscribe() as events:
        for msg in events:
            print(msg, flush=True)
    return OK


if __name__ == "__main__":
    sys.exit(main())
