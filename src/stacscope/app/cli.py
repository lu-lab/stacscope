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
    stacscope properties DEVICE
        Every property of the device: its value, unit and limits.
    stacscope set DEVICE PROPERTY VALUE
        Set a property, and print every property that changed. VALUE is a
        number, one of the property's labels, or true or false.

The commands other than serve reach the engine at ws://127.0.0.1:8765, or
at --url. They exit with 0 when done, 1 when the engine refuses (saying
why), 2 for a mistake in the command or the config file, and 3 when the
engine cannot be reached or does not answer within --timeout seconds.
"""

import argparse
import sys

from stacscope.client import DEFAULT_URL, TIMEOUT, AskTimeout, Client, Disconnected, Refused
from stacscope.core.protocol import Property, PropertyAccess, PropertyKind

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
    except LookupError as exc:
        print(f"refused: {exc.args[0]}", file=sys.stderr)
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
    properties = client_command("properties", _properties, "every property of a device")
    properties.add_argument("device", help="the device's name")
    set_ = client_command("set", _set, "set a property of a device")
    set_.add_argument("device", help="the device's name")
    set_.add_argument("property", help="the property's name")
    set_.add_argument("value", help="a number, a label, or true or false")
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


def _properties(client: Client, args: argparse.Namespace) -> int:
    _print_table(client.properties(args.device).values())
    return OK


_TRUE, _FALSE = {"true", "on", "yes", "1"}, {"false", "off", "no", "0"}


def _set(client: Client, args: argparse.Namespace) -> int:
    # The table says how to read the value: "1" is a number to one property
    # and a flag to another.
    table = client.properties(args.device)
    if args.property not in table:
        print(f"refused: {args.device} has no property {args.property!r}", file=sys.stderr)
        return REFUSED
    kind, text = table[args.property].kind, args.value
    value: bool | float | str = text
    if kind is PropertyKind.NUMBER:
        try:
            value = float(text)
        except ValueError:
            print(f"{args.property} takes a number, not {text!r}", file=sys.stderr)
            return MISTAKE
    elif kind is PropertyKind.FLAG:
        if text.lower() not in _TRUE | _FALSE:
            print(f"{args.property} takes true or false, not {text!r}", file=sys.stderr)
            return MISTAKE
        value = text.lower() in _TRUE
    _print_table(client.set(args.device, args.property, value).values())
    return OK


def _print_table(properties) -> None:
    properties = list(properties)
    width = max((len(p.name) for p in properties), default=0)
    for p in properties:
        print(f"  {p.name:<{width}}  {_describe(p)}")


def _describe(p: Property) -> str:
    value = f"{p.value:g}" if p.kind is PropertyKind.NUMBER else str(p.value).lower() if p.kind is PropertyKind.FLAG else p.value
    parts = [f"{value} {p.unit}".rstrip()]
    if p.labels:
        parts.append(f"of {', '.join(p.labels)}")
    elif p.choices:
        parts.append(f"of {', '.join(f'{c:g}' for c in p.choices)}")
    elif p.minimum is not None or p.maximum is not None:
        low = "" if p.minimum is None else f"{p.minimum:g}"
        high = "" if p.maximum is None else f"{p.maximum:g}"
        step = "" if p.step is None else f", step {p.step:g}"
        parts.append(f"{low} to {high}{step}")
    access = {PropertyAccess.READ_ONLY: "read-only", PropertyAccess.IDLE: "only while idle"}.get(p.access)
    if access:
        parts.append(access)
    return "; ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
