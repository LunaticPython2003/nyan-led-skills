"""JSON-lines plugin worker. Stdout belongs to IPC; plugin prints go to stderr."""

from contextlib import redirect_stdout
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .plugin_api import Context, Frame, Scene
from .plugins import read_plugin


def main():
    protocol = sys.stdout
    context, poll = None, None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                if context is None:
                    plugin = read_plugin(Path(sys.argv[1]))
                    namespace = {"__name__": f"nyan_plugin_{plugin.id}", "__file__": str(plugin.path)}
                    exec(compile(plugin.data["python"]["code"], str(plugin.path), "exec"), namespace)
                    poll = namespace.get("poll")
                    if not callable(poll):
                        raise ValueError("Plugin must define poll(ctx)")
                    context = Context(plugin.inputs(request["inputs"]), Path(request["cache_dir"]), *request["dimensions"])
                    context.cache_dir.mkdir(parents=True, exist_ok=True)
                frame = poll(context)
                if frame is not None and not isinstance(frame, (Frame, Scene)):
                    raise TypeError("poll(ctx) must return Frame, Scene, or None")
                response = {"frame": asdict(frame) if frame else None}
        except Exception as exc:
            response = {"error": f"{type(exc).__name__}: {exc}"}
        protocol.write(json.dumps(response) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
