"""Command-line entry points for the simulation runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .sdk import Client, SimulationError


def _client(args: argparse.Namespace) -> Client:
    token = args.token or os.environ.get(args.token_env, "")
    if not token:
        raise SystemExit(
            f"provide --token or set {args.token_env} to a project token"
        )
    return Client(args.url, token, timeout=args.timeout)


def _add_client_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=os.environ.get("SIM_RUNTIME_URL", "http://127.0.0.1:8080"),
        help="service base URL (default: %(default)s)",
    )
    parser.add_argument("--token", default=None, help="project bearer token")
    parser.add_argument(
        "--token-env",
        default="SIM_RUNTIME_TOKEN",
        help="environment variable holding the token (default: %(default)s)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds")


def _print(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def command_capabilities(args: argparse.Namespace) -> int:
    _print(_client(args).capabilities())
    return 0


def command_smoke(args: argparse.Namespace) -> int:
    client = _client(args)
    started = time.monotonic()
    with client.session(
        backend="fake", task="reach", phase="playground", mode="typed", seed=7
    ) as session:
        observation = session.observation()
        cameras = sorted(observation["cameras"])
        result = session.step(
            {"ee_position": [0.0, 0.0, 0.01], "ee_joint_state": [0.0]}
        )
        image = session.download_camera(observation, cameras[0], path=args.output)
        state = session.status()
        print(
            json.dumps(
                {
                    "ok": True,
                    "session_id": session.session_id,
                    "task": session.task,
                    "cameras": cameras,
                    "step_applied": result["applied"],
                    "success": result["success"],
                    "terminated": result["terminated"],
                    "native_steps_used": result["native_steps_used"],
                    "observation_sequence": state["observation_sequence"],
                    "downloaded_bytes": len(image),
                    "elapsed_sec": round(time.monotonic() - started, 3),
                },
                indent=2,
            )
        )
    return 0


def command_backend_smoke(args: argparse.Namespace) -> int:
    from .backends import create_backend
    from .core.session import SessionLimits, SimulationSession

    backend = create_backend(args.backend)
    started = time.monotonic()
    native = backend.create_session(args.task, seed=args.seed)
    session = SimulationSession(
        native, phase="playground", mode="typed", seed=args.seed,
        limits=SessionLimits(native_action_limit=native.native_action_limit),
    )
    try:
        observation = session.public_observation()
        step_started = time.monotonic()
        outcome = session.apply_action(
            {
                "arm_joint_state": [0.0, 0.0, 0.0, 0.0, 0.0],
                "ee_joint_state": [1.0],
            }
        )
        step_sec = time.monotonic() - step_started
        print(
            json.dumps(
                {
                    "ok": True,
                    "backend": backend.name,
                    "task": session.task,
                    "cameras": {
                        name: list(frame.shape)
                        for name, frame in observation["vision"].items()
                    },
                    "state_keys": sorted(observation["state"]),
                    "step_applied": outcome.applied,
                    "native_steps_used": outcome.native_steps_used,
                    "step_sec": round(step_sec, 3),
                    "total_sec": round(time.monotonic() - started, 3),
                },
                indent=2,
            )
        )
    finally:
        session.close()
    return 0


def command_serve(_args: argparse.Namespace) -> int:
    from .service.__main__ import main as serve

    serve()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulation-runtime",
        description="Standalone, project-scoped robot simulation service.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    capabilities = subcommands.add_parser(
        "capabilities", help="print the service capability document"
    )
    _add_client_options(capabilities)
    capabilities.set_defaults(func=command_capabilities)

    smoke = subcommands.add_parser(
        "smoke", help="exercise a running service with the deterministic fake backend"
    )
    _add_client_options(smoke)
    smoke.add_argument("--output", default=None, help="write the first camera image here")
    smoke.set_defaults(func=command_smoke)

    backend_smoke = subcommands.add_parser(
        "backend-smoke", help="load a backend in-process and step it once"
    )
    backend_smoke.add_argument("--backend", default="mujoco_so101")
    backend_smoke.add_argument("--task", default="pick_cube")
    backend_smoke.add_argument("--seed", type=int, default=0)
    backend_smoke.set_defaults(func=command_backend_smoke)

    serve = subcommands.add_parser("serve", help="start the HTTP service")
    serve.set_defaults(func=command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SimulationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
