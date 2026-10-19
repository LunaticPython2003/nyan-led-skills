"""Terminal interface for the application and running plugin watcher."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys

from bleak.exc import BleakError

from . import __version__
from . import service
from .daemon import run
from .devices import device_settings, models
from .plugins import discover, read_plugin
from .settings import Paths, instance_lock, load_config, parse_dimensions, save_config


async def request(paths, command):
    try:
        reader, writer = await asyncio.open_unix_connection(str(paths.socket))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ConnectionError("Watcher is not running. Start it with: nyan service install (or nyan watch)") from exc
    try:
        writer.write(json.dumps(command).encode() + b"\n")
        await writer.drain()
        async with asyncio.timeout(15):
            line = await reader.readline()
        response = json.loads(line)
        if not response.get("ok"):
            raise ValueError(response.get("error", "Watcher request failed"))
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def parse_inputs(values):
    result = {}
    for value in values:
        key, separator, raw = value.partition("=")
        if not separator or not key:
            raise ValueError("Inputs use --set NAME=VALUE")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def show_status(status, as_json=False):
    if as_json:
        print(json.dumps(status, indent=2))
        return
    device = status["device"]
    print(f"Plugin: {status['plugin']} | {status['phase']} | BLE: {'connected' if status['connected'] else 'disconnected'}")
    print(f"Device: {device['model']} | {device['width']}x{device['height']} | {device['quality']} | calibrated={device.get('calibrated', False)}")
    if status.get("dry_run"):
        print("Dry run: Bluetooth is disabled")
    if status.get("last_upload"):
        upload = status["last_upload"]
        print(f"Last: {upload['label']} | {upload['chunks']} chunks | {upload['seconds']:.2f}s")
        if "sample_age" in upload:
            print(f"Live sample age at display: {upload['sample_age']:.2f}s")
    if status.get("error"):
        print(f"Error: {status['error']}")


def parser():
    root = argparse.ArgumentParser(prog="nyan", description="Plugin-driven BLE LED backpack")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch", help="Run the plugin watcher in the foreground")
    watch.add_argument("--dry-run", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    use = commands.add_parser("use", help="Switch the active content plugin")
    use.add_argument("plugin")
    use.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    plugins = commands.add_parser("plugins")
    sub = plugins.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    validate = sub.add_parser("validate")
    validate.add_argument("path", type=Path)
    scaffold = sub.add_parser("new")
    scaffold.add_argument("id")
    device = commands.add_parser("device")
    devices = device.add_subparsers(dest="action", required=True)
    devices.add_parser("list")
    devices.add_parser("show")
    configure = devices.add_parser("configure")
    configure.add_argument("--model")
    configure.add_argument("--address")
    configure.add_argument("--quality", choices=["natural", "detail"])
    configure.add_argument("--fit", choices=["cover", "contain", "stretch"])
    calibration = commands.add_parser("calibrate", help="Show bounds, then confirm matching physical edges")
    calibration.add_argument("--size", type=parse_dimensions)
    calibration.add_argument("--confirm", action="store_true")
    show = commands.add_parser("show", help="Display a local image or animated GIF through the watcher")
    show.add_argument("path", type=Path, nargs="?")
    content = show.add_mutually_exclusive_group()
    content.add_argument("--color", help="Render a solid color instead of reading a file")
    content.add_argument("--text", help="Render text instead of reading a file")
    commands.add_parser("reload")
    commands.add_parser("stop")
    commands.add_parser("scan")
    commands.add_parser("discover")
    controller = commands.add_parser("service")
    controller.add_argument("action", choices=["install", "start", "stop", "restart", "status", "logs"])
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    paths = Paths.current()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        if arguments.command == "watch":
            run(paths, dry_run=arguments.dry_run)
        elif arguments.command == "service":
            return service.control(arguments.action)
        elif arguments.command == "status":
            show_status(asyncio.run(request(paths, {"command": "status"})), arguments.json)
        elif arguments.command == "use":
            inputs = parse_inputs(arguments.set)
            plugins, _ = discover(paths)
            if arguments.plugin not in plugins:
                raise ValueError(f"Unknown plugin: {arguments.plugin}")
            config = load_config(paths)
            supplied = config["plugin_inputs"].get(arguments.plugin, {}) | inputs
            plugins[arguments.plugin].inputs(supplied)
            try:
                status = asyncio.run(request(paths, {"command": "use", "plugin": arguments.plugin, "inputs": inputs}))
                show_status(status)
            except ConnectionError:
                config["active_plugin"] = arguments.plugin
                config["plugin_inputs"][arguments.plugin] = supplied
                save_config(paths, config)
                print(f"Selected {arguments.plugin}. Start the watcher with: nyan service install")
        elif arguments.command == "plugins":
            if arguments.action == "list":
                plugins, errors = discover(paths)
                for plugin in plugins.values():
                    missing = plugin.missing_dependencies()
                    state = "missing " + ", ".join(missing) if missing else "ready"
                    print(f"{plugin.id:<16} {state:<25} {plugin.data.get('name', plugin.id)}")
                for path, error in errors.items():
                    print(f"INVALID {path}: {error}", file=sys.stderr)
            elif arguments.action == "validate":
                plugin = read_plugin(arguments.path)
                print(f"Valid plugin: {plugin.id}")
                missing = plugin.missing_dependencies()
                if missing:
                    raise ValueError(f"Missing dependencies: {', '.join(missing)}")
            else:
                import re
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", arguments.id):
                    raise ValueError("Use a lowercase plugin id")
                target = paths.config / "plugins" / f"{arguments.id}.toml"
                target.parent.mkdir(parents=True, exist_ok=True)
                template = Path(__file__).with_name("plugins") / "clock.toml"
                content = template.read_text().replace('id = "clock"', f'id = "{arguments.id}"', 1)
                with target.open("x") as output:
                    output.write(content)
                print(target)
        elif arguments.command == "device":
            if arguments.action == "list":
                for model in models(paths).values():
                    print(f"{model['id']:<28} {model['name']}")
            elif arguments.action == "show":
                model, settings = device_settings(paths, load_config(paths))
                print(json.dumps({"settings": settings, "capabilities": model.get("capabilities", {})}, indent=2))
            else:
                config = load_config(paths)
                for name in ("model", "address", "quality", "fit"):
                    value = getattr(arguments, name)
                    if value is not None:
                        if name in ("model", "address") and value != config["device"].get(name):
                            config["device"]["calibrated"] = False
                            config["device"].pop("width", None)
                            config["device"].pop("height", None)
                        config["device"][name] = value
                device_settings(paths, config)
                save_config(paths, config)
                try:
                    asyncio.run(request(paths, {"command": "reload"}))
                except ConnectionError:
                    pass
                print("Device settings saved")
        elif arguments.command == "calibrate":
            if arguments.confirm and arguments.size:
                raise ValueError("Show --size first; use --confirm only after checking the physical panel")
            if arguments.confirm:
                status = asyncio.run(request(paths, {"command": "confirm_calibration"}))
                print("Calibration saved; restoring the active plugin")
            else:
                if arguments.size is None:
                    raise ValueError("Use --size WIDTHxHEIGHT, then --confirm once all physical edges match")
                status = asyncio.run(request(paths, {"command": "calibrate", "dimensions": arguments.size}))
                print("Calibration queued. When visible: GREEN top, BLUE left, YELLOW right, RED bottom.")
                print("Check the COMPLETE panel for clipping or unused LEDs, then run: nyan calibrate --confirm")
            show_status(status)
        elif arguments.command == "show":
            if arguments.path is not None and (arguments.color or arguments.text):
                raise ValueError("Choose a file, --color, or --text")
            if arguments.path is not None:
                source = str(arguments.path.expanduser().resolve())
                if not Path(source).is_file():
                    raise ValueError(f"Image file does not exist: {source}")
            elif arguments.color or arguments.text:
                from PIL import Image, ImageDraw, ImageFont
                _, settings = device_settings(paths, load_config(paths))
                dimensions = settings["width"], settings["height"]
                image = Image.new("RGB", dimensions, arguments.color or "black")
                if arguments.text:
                    draw = ImageDraw.Draw(image)
                    font = ImageFont.load_default(size=max(8, dimensions[0] // 5))
                    draw.text((4, dimensions[1] // 2), arguments.text, font=font, fill="white", anchor="lm")
                path = paths.cache / "manual.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                image.save(path)
                source = str(path)
            else:
                raise ValueError("Provide an image/GIF path, --color COLOR, or --text TEXT")
            try:
                status = asyncio.run(request(paths, {"command": "use", "plugin": "image", "inputs": {"path": source}}))
                show_status(status)
            except ConnectionError:
                with instance_lock(paths):
                    asyncio.run(show_once(paths, source))
        elif arguments.command in ("scan", "discover"):
            if arguments.command == "discover":
                with instance_lock(paths):
                    asyncio.run(inspect_ble(True, paths))
            else:
                asyncio.run(inspect_ble(False, paths))
        else:
            response = asyncio.run(request(paths, {"command": arguments.command}))
            if arguments.command == "reload":
                show_status(response)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError, BleakError, subprocess.SubprocessError, TimeoutError) as exc:
        print(f"nyan: {exc}", file=sys.stderr)
        return 1
    return 0


async def show_once(paths, source):
    from .devices import create_driver
    from .rendering import encode
    model, settings = device_settings(paths, load_config(paths))
    if not settings["calibrated"]:
        raise ValueError("Calibrate through nyan watch before the first upload")
    frame = await asyncio.to_thread(encode, {"source": source, "label": Path(source).name}, settings, paths.cache / "manual")
    driver = create_driver(model, settings)
    try:
        result = await driver.upload(frame.data)
        print(f"Displayed {frame.label}: {result.chunks} chunks in {result.seconds:.2f}s")
    finally:
        await driver.close()


async def inspect_ble(detailed, paths):
    from bleak import BleakClient, BleakScanner
    if detailed:
        model, settings = device_settings(paths, load_config(paths))
        if not settings["address"]:
            raise ValueError("Configure a device address first")
        async with BleakClient(settings["address"]) as client:
            for service in client.services:
                print(service.uuid)
                for char in service.characteristics:
                    print(f"  {char.uuid} {', '.join(char.properties)}")
    else:
        devices = await BleakScanner.discover(timeout=10, return_adv=True)
        for device, advertisement in sorted(devices.values(), key=lambda item: item[1].rssi, reverse=True):
            print(f"{device.address} {advertisement.rssi:>4} {advertisement.local_name or device.name or '(unknown)'}")
