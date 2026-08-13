"""Render the README reel: runs side by side, rotating and moving their joints.

Frames come from the same renderer the live viewer uses, driven headlessly:
USDZ through USDLoader, authored PBR appearances, image based lighting, ACES
tone mapping. Each panel spins one full turn. Joints replay the recorded
joints sweep their limits, solved through the SDK so closed loops stay shut.

    uv run python scripts/readme_reel.py runs/<run-id>:"a toolbox" runs/<other>

The label after the colon is optional; the run directory name is the default.

With --physics the motion is simulated rather than solved: each object hangs
with gravity off and works its joints, then gravity comes on and it drops onto
a ramp and slides away. That path reads everything it needs out of the exported
file, so it takes bare USDZ paths as well as run directories.

    uv run python scripts/readme_reel.py a.usdz b.usdz --physics --rows 2
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Annotated, cast

import typer
from PIL import Image, ImageChops

from articraft.sdk import JointAxis
from articraft.sdk.assembly import _frame_matrix
from articraft.viewer import _read_version, load_viewer_run

BACKGROUND = "#f7f8fa"
PAGE = Path(__file__).with_name("reel.html")
CHROMES = (
    Path.home()
    / "Library/Caches/ms-playwright/chromium_headless_shell-1228"
    / "chrome-headless-shell-mac-arm64/chrome-headless-shell",
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
)
app = typer.Typer(help=__doc__, no_args_is_help=True)


def _poses(run: Path, version: dict, frames: int) -> list[dict[str, list[float]]]:
    """One body placement per frame: {body name: 16 numbers, column major}.

    The SDK solves the kinematics, including any closed loop, and the page just
    applies the matrices. Nothing about joints or frames crosses into
    JavaScript, so there is only ever one implementation of the motion.
    """
    import contextlib
    import importlib.util
    import io
    import sys

    import numpy as np

    workspace = run / "workspace"
    spec = importlib.util.spec_from_file_location(f"reel_{run.name}", workspace / "main.py")
    if spec is None or spec.loader is None:
        raise typer.BadParameter(f"cannot import {run}/workspace/main.py")
    module = importlib.util.module_from_spec(spec)
    # A run may split its code across files, so its own directory has to be
    # importable while main.py loads. The module also has to be registered:
    # dataclasses defined in it look themselves up through sys.modules later,
    # and find None if it was only ever executed.
    sys.path.insert(0, str(workspace))
    sys.modules[spec.name] = module
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(workspace))
    resolved = module.object_model.resolve()

    tree = [item.joint for item in resolved.joints if not item.exclude_from_articulation]
    closers = [item.joint for item in resolved.joints if item.exclude_from_articulation]

    # A ring decides most of its own joints. Sweeping all of them drives a four
    # bar against itself, so each ring keeps one driver and the rest are solved.
    hangs = {joint.body1: joint for joint in tree}

    def to_root(body: object) -> list:
        walk, seen = [], set()
        while body in hangs and id(body) not in seen:
            seen.add(id(body))
            walk.append(hangs[body])
            body = hangs[body].body0
        return walk

    rings: list[list] = []
    for closer in closers:
        near, far = to_root(closer.body0), to_root(closer.body1)
        far_names = {joint.name for joint in far}
        near_names = {joint.name for joint in near}
        rings.append(
            [j for j in near if j.name not in far_names]
            + [j for j in far if j.name not in near_names]
        )

    # Rings that share a joint are one mechanism -- a gripper's two dogbones
    # both hang off the same slider -- so they get one driver between them.
    # Merging has to be transitive: an excavator's bucket ring and its stick
    # ring both touch the stick hinge, and left as separate groups they pick
    # two drivers that fight over it, which shrinks the whole loop to whatever
    # amplitude that conflict still allows.
    merged: list[list] = []
    for ring in rings:
        names = {j.name for j in ring}
        touching = [g for g in merged if names & {j.name for j in g}]
        combined = list(ring)
        for group in touching:
            combined.extend(j for j in group if j.name not in {c.name for c in combined})
            merged.remove(group)
        merged.append(combined)

    def reach_of(dof) -> float:
        """How far this joint travels from rest, out toward its further stop.

        Rest is the authored pose and the travel that matters usually runs one
        way from it: a lid opens from shut, a gear retracts from down. Capped so
        a joint with no real stops does not wind through a full turn.
        """
        if dof.limits is None:
            return 0.0
        lower, upper = dof.limits
        far = upper if abs(upper) >= abs(lower) else lower
        limit = 1.2 if cast(JointAxis, dof.axis).is_rotational else 0.4
        return max(-limit, min(limit, far * 0.75))

    def moves(candidate) -> bool:
        """Does driving this joint actually pose the mechanism?

        Which joint of a ring is the input is not something the graph says: a
        gripper is driven by its slider, not by a finger, and asking the finger
        leaves the loop unsatisfiable. So try it and see.
        """
        dof = candidate.dofs[0]
        if dof.limits is None:
            return True
        # Test at the value the sweep will actually use: a driver that solves
        # halfway and fails at full travel silently falls back to rest, which
        # is how a whole panel ends up standing still.
        try:
            state = resolved.forward_kinematics({candidate.dof_id(dof): reach_of(dof)})
        except Exception:
            return False
        return any(abs(v) > 1e-6 for v in state.dof_positions.values())

    followers: set[str] = set()
    for group in merged:
        usable = [j for j in group if len(j.dofs) == 1 and moves(j)]
        keep = usable[0] if usable else None
        followers.update(j.name for j in group if keep is None or j.name != keep.name)

    def turns_about_vertical(joint) -> bool:
        """Does this joint spin the object the way the reel already does?

        Every panel rotates a full turn about world up. A joint on that same
        axis -- an excavator's slew ring -- adds to the camera instead of
        showing anything new, and the panel appears to judder while the others
        look smooth.
        """
        dof = joint.dofs[0]
        axis = cast(JointAxis, dof.axis)
        if not axis.is_rotational:
            return False
        direction = np.zeros(3)
        direction[axis.component] = 1.0
        world = _frame_matrix(joint.frame0)[:3, :3] @ direction
        return abs(float(world[2])) > 0.99

    candidates = [joint for joint in tree if len(joint.dofs) == 1 and joint.name not in followers]
    # Dropping the turntable only helps while something else still moves. A tool
    # that lies flat -- a pair of pliers -- has its one hinge on that axis too,
    # and skipping it would leave the panel standing still.
    upright = [joint for joint in candidates if not turns_about_vertical(joint)]
    driven = [(joint, joint.dofs[0]) for joint in (upright or candidates)]

    def wanted(blend: float, turn: float, scale: float) -> dict[str, float]:
        asked: dict[str, float] = {}
        for joint, dof in driven:
            if dof.limits is None:
                asked[joint.dof_id(dof)] = turn
                continue
            lower, upper = dof.limits
            asked[joint.dof_id(dof)] = max(lower, min(upper, reach_of(dof) * blend * scale))
        return asked

    def blends(count: int) -> list[tuple[float, float]]:
        # Ease in and out so the loop has no visible seam.
        return [
            (
                0.5 - 0.5 * math.cos(2.0 * math.pi * index / count),
                2.0 * math.pi * index / count,
            )
            for index in range(count)
        ]

    # One amplitude for the whole loop, the largest every frame can hold. A
    # linkage that cannot reach the deepest part of a sweep used to be caught
    # per frame and backed off there, which put a jump in the middle of the
    # motion; scaling the loop instead keeps it smooth and merely shallower.
    scale = 1.0
    for candidate in (1.0, 0.8, 0.65, 0.5, 0.35, 0.2, 0.1):
        try:
            for blend, turn in blends(frames):
                resolved.forward_kinematics(wanted(blend, turn, candidate))
        except Exception:
            continue
        scale = candidate
        break
    else:
        scale = 0.0

    placements: list[dict[str, list[float]]] = []
    for blend, turn in blends(frames):
        try:
            state = resolved.forward_kinematics(wanted(blend, turn, scale))
        except Exception:
            state = resolved.forward_kinematics({})
        placements.append(
            {
                name: [float(v) for v in np.asarray(matrix, dtype=float).T.flatten()]
                for name, matrix in state.body_poses.items()
            }
        )
    return placements


def _physics_poses(
    usdz: Path,
    frames: int,
    *,
    fps: float,
    ramp: float,
    hover: float,
    settle: float,
    speed: float,
    roll: float,
    runway: float,
) -> tuple[list[dict[str, list[float]]], dict[str, list[float]]]:
    """Body placements per frame, recorded from MuJoCo, and the ramp they use.

    The shot has two acts. While ``settle`` of the frames remain, gravity is off
    and the joints are driven straight through their travel, so the object hangs
    in place and works its mechanism. Then gravity comes on and nothing is driven
    any more: it drops onto a ramp and slides off under its own weight.

    The ramp is a real slab rather than the usual infinite plane, so there is an
    edge to leave and the page has something to draw. It is sized off the model's
    own extent, because these run from a 0.2 m hand to a 5 m landing gear leg and
    one fixed slab would be a runway under one and a kerb under the other.

    Body names come from the same authored names the page keys its parts by, so
    the payload is interchangeable with the kinematic one.
    """

    import mujoco
    import numpy as np

    from articraft.simulate import _build_spec

    spec = _build_spec(usdz)
    tilt = math.radians(ramp)
    # Compile once just to measure the object, then shape the slab to it. The
    # measurement has to skip the floor: it is still an infinite plane at this
    # point, and its extent would swamp anything standing on it.
    sizing = spec.compile()
    probe = mujoco.MjData(sizing)
    mujoco.mj_forward(sizing, probe)
    on_body = [g for g in range(sizing.ngeom) if sizing.geom_bodyid[g] > 0]
    low = np.min([probe.geom_xpos[g] - sizing.geom_rbound[g] for g in on_body], axis=0)
    high = np.max([probe.geom_xpos[g] + sizing.geom_rbound[g] for g in on_body], axis=0)
    extent = float(np.linalg.norm(high - low))
    reach = extent * 0.62
    half = [reach * runway, extent * 0.44, extent * 0.02]
    # Sink the slab along its own normal so the face the object lands on, rather
    # than the middle of the slab, is the plane through the origin it was posed on.
    normal = (math.sin(tilt), 0.0, math.cos(tilt))
    # Spend the extra length downhill, so an object that travels starts at the
    # top of its runway rather than in the middle of it. Centring the slab would
    # give a sliding object half the ramp and the reel half the journey.
    downhill = (math.cos(tilt), 0.0, -math.sin(tilt))
    shift = reach * (runway - 1.0)
    seat = [
        -half[2] * normal[0] + shift * downhill[0],
        0.0,
        -half[2] * normal[2] + shift * downhill[2],
    ]
    ramp_payload = {"size": list(half), "pos": list(seat), "tilt": [ramp]}
    for geom in spec.worldbody.geoms:
        if geom.name == "floor":
            # The mujoco stubs omit these enums, though they are there at runtime.
            geom.type = mujoco.mjtGeom.mjGEOM_BOX  # pyright: ignore[reportAttributeAccessIssue]
            geom.size = half
            geom.quat = [math.cos(tilt / 2), 0.0, math.sin(tilt / 2), 0.0]
            geom.pos = seat

    # Drive the joints through servos rather than by writing qpos. A loop
    # closure is a constraint the solver enforces while stepping, and writing
    # positions outright walks straight through it: that is what pulls a landing
    # gear's actuator rod off the strut it is pinned to while the object floats.
    for joint in spec.joints:
        if joint.type == mujoco.mjtJoint.mjJNT_FREE:  # pyright: ignore[reportAttributeAccessIssue]
            continue
        servo = spec.add_actuator()
        servo.name = f"drive_{joint.name}"
        servo.trntype = mujoco.mjtTrn.mjTRN_JOINT  # pyright: ignore[reportAttributeAccessIssue]
        servo.target = joint.name
        servo.gaintype = mujoco.mjtGain.mjGAIN_FIXED  # pyright: ignore[reportAttributeAccessIssue]
        servo.biastype = mujoco.mjtBias.mjBIAS_AFFINE  # pyright: ignore[reportAttributeAccessIssue]

    model = spec.compile()
    data = mujoco.MjData(model)

    # Tune each servo against the inertia it actually swings, not the mass of
    # the whole object. Scaling by total mass gives a 1 t landing gear a gain in
    # the hundreds of thousands, which rings at this timestep, blows the loop
    # closures open -- the piston leaving the strut -- and pumps in enough energy
    # that the object bounces higher than it was dropped from.
    # Critically damped at ``BAND`` Hz: kp = m*w^2, kv = 2*m*w.
    band = 3.0 * 2.0 * math.pi
    for actuator in range(model.nu):
        dof = model.jnt_dofadr[model.actuator_trnid[actuator, 0]]
        inertia = max(float(model.dof_M0[dof]), 1e-6)
        model.actuator_gainprm[actuator, 0] = inertia * band * band
        model.actuator_biasprm[actuator, 1] = -inertia * band * band
        model.actuator_biasprm[actuator, 2] = -2.0 * inertia * band

    free = [
        j
        for j in range(model.njnt)
        if model.jnt_type[j] == mujoco.mjtJoint.mjJNT_FREE  # pyright: ignore[reportAttributeAccessIssue]
    ]
    driven = [
        j
        for j in range(model.njnt)
        if model.jnt_type[j] in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE)
    ]
    rest = data.qpos.copy()
    # Hold it clear of the ramp, measured along the ramp normal so the drop is
    # the same height whatever the incline.
    for joint in free:
        adr = model.jnt_qposadr[joint]
        rest[adr] += hover * math.sin(tilt)
        rest[adr + 2] += hover * math.cos(tilt)
        if roll:
            # Spin it about the downhill axis before letting go. A shape that
            # settles on a curved face -- an engine cowl -- picks its resting
            # roll from the one it started at, and that decides whether the
            # open end finishes pointing at the sky or at the ramp.
            turn = math.radians(roll)
            spun = np.zeros(4)
            mujoco.mju_mulQuat(  # pyright: ignore[reportAttributeAccessIssue]
                spun,
                np.array([math.cos(turn / 2), math.sin(turn / 2), 0.0, 0.0]),
                rest[adr + 3 : adr + 7],
            )
            rest[adr + 3 : adr + 7] = spun

    names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b) for b in range(1, model.nbody)]

    def record() -> dict[str, list[float]]:
        placement = {}
        for index, name in enumerate(names, start=1):
            matrix = np.eye(4)
            matrix[:3, :3] = data.xmat[index].reshape(3, 3)
            matrix[:3, 3] = data.xpos[index]
            placement[name] = [float(v) for v in matrix.T.flatten()]
        return placement

    floating = max(1, int(frames * settle))
    placements: list[dict[str, list[float]]] = []
    commanded = {int(model.actuator_trnid[a, 0]): a for a in range(model.nu)}
    # ``speed`` is how much simulated time each frame covers. Above 1.0 the whole
    # shot plays quicker without dropping frames out of the motion.
    steps = max(1, round(speed / (fps * model.opt.timestep)))

    gravity = model.opt.gravity.copy()
    model.opt.gravity[:] = 0.0
    data.qpos[:] = rest
    mujoco.mj_forward(model, data)
    for frame in range(floating):
        blend = 0.5 * (1.0 - math.cos(2.0 * math.pi * frame / floating))
        for joint in driven:
            actuator = commanded.get(joint)
            if actuator is None:
                continue
            low, high = model.jnt_range[joint]
            if model.jnt_limited[joint] and math.isfinite(low) and math.isfinite(high):
                data.ctrl[actuator] = low + (high - low) * blend
            elif model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_HINGE:
                # A free spinner: let it turn a full revolution instead. Sliders
                # with no stops have no travel to show, so they stay at rest.
                data.ctrl[actuator] = 2.0 * math.pi * frame / floating
        for _ in range(steps):
            mujoco.mj_step(model, data)
            # Hold the object where it was placed while its mechanism works. A
            # weld would be the tidier way to say this, but it holds the pose the
            # model compiled with, which throws away the hover and the roll.
            for joint in free:
                position, velocity = model.jnt_qposadr[joint], model.jnt_dofadr[joint]
                data.qpos[position : position + 7] = rest[position : position + 7]
                data.qvel[velocity : velocity + 6] = 0.0
        placements.append(record())

    # Let go: the servos go slack and gravity comes back.
    model.actuator_gainprm[:, 0] = 0.0
    model.actuator_biasprm[:, 1] = 0.0
    model.actuator_biasprm[:, 2] = 0.0
    data.ctrl[:] = 0.0
    data.qvel[:] = 0.0  # drop whatever the servos were still carrying
    model.opt.gravity[:] = gravity
    for _ in range(frames - floating):
        for _ in range(steps):
            mujoco.mj_step(model, data)
        placements.append(record())
    return placements, ramp_payload


def _handler(bootstrap: bytes, models: dict[str, Path], captured: dict[tuple[int, int], bytes]):
    finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        finished_event = finished

        def log_message(self, format: str, *args: object) -> None:  # keep the console quiet
            del format, args

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(PAGE.read_bytes(), "text/html")
            elif self.path == "/bootstrap.json":
                self._send(bootstrap, "application/json")
            elif self.path.startswith("/models/"):
                name = self.path.removeprefix("/models/").removesuffix(".usdz")
                self._send(models[name].read_bytes(), "model/vnd.usdz+zip")
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/done":
                finished.set()
            else:
                _, _, panel, frame = self.path.split("/")
                captured[(int(panel), int(frame))] = body
            self._send(b"", "text/plain")

    return Handler, finished


def _above_noise(value: int) -> int:
    """Anything more than a hair off the background counts as content."""

    return 255 if value > 6 else 0


def _trim(shots: list[Image.Image], size: int) -> list[Image.Image]:
    """Crop a panel to what it actually draws, so thin objects keep no dead air.

    The crop is the union across every frame, so the object still holds still
    inside its panel while it turns.
    """
    canvas = Image.new("RGB", shots[0].size, BACKGROUND)
    box = None
    for shot in shots:
        mask = ImageChops.difference(shot, canvas).convert("L")
        found = mask.point(_above_noise)
        bounds = found.getbbox()
        if bounds is None:
            continue
        box = (
            bounds
            if box is None
            else (
                min(box[0], bounds[0]),
                min(box[1], bounds[1]),
                max(box[2], bounds[2]),
                max(box[3], bounds[3]),
            )
        )
    if box is None:
        box = (0, 0, shots[0].width, shots[0].height)
    pad = round(shots[0].height * 0.04)
    box = (
        max(0, box[0] - pad),
        max(0, box[1] - pad),
        min(shots[0].width, box[2] + pad),
        min(shots[0].height, box[3] + pad),
    )
    height = box[3] - box[1]
    width = round((box[2] - box[0]) * size / height)
    # A very wide object (pliers, a wrench) would dominate a grid row; letterbox
    # it instead so every panel stays within a tame aspect.
    cap = round(size * 1.6)
    if width <= cap:
        return [shot.crop(box).resize((width, size), Image.Resampling.LANCZOS) for shot in shots]
    scale = cap / width
    inner = max(1, round(size * scale))
    panels = []
    for shot in shots:
        image = shot.crop(box).resize((cap, inner), Image.Resampling.LANCZOS)
        panel = Image.new("RGB", (cap, size), BACKGROUND)
        panel.paste(image, (0, (size - inner) // 2))
        panels.append(panel)
    return panels


def _chrome() -> str:
    for path in CHROMES:
        if path.exists():
            return str(path)
    found = shutil.which("chromium") or shutil.which("google-chrome")
    if not found:
        raise typer.BadParameter("no Chrome or Chromium found to render with")
    return found


@app.command()
def main(
    runs: Annotated[list[str], typer.Argument(help="Run directories, as path or path:label.")],
    output: Annotated[Path, typer.Option(help="GIF to write.")] = Path("assets/readme/reel.gif"),
    frames: Annotated[int, typer.Option(help="Frames per rotation.")] = 30,
    size: Annotated[int, typer.Option(help="Panel size in pixels.")] = 320,
    fps: Annotated[float, typer.Option(help="Playback rate.")] = 20.0,
    elevation: Annotated[float, typer.Option(help="Camera height, as a fraction.")] = 0.34,
    zoom: Annotated[float, typer.Option(help="Fit margin; 1.0 just fits, more adds air.")] = 1.0,
    gap: Annotated[int, typer.Option(help="Pixels of air between panels.")] = 28,
    rows: Annotated[int, typer.Option(help="Grid rows to split the panels into.")] = 1,
    supersample: Annotated[int, typer.Option(help="Render scale before downsampling.")] = 3,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for the browser.")] = 600.0,
    physics: Annotated[
        bool, typer.Option(help="Simulate instead of solving: float, then drop and slide.")
    ] = False,
    ramp: Annotated[
        str, typer.Option(help="Ramp angle in degrees; one value, or one per panel.")
    ] = "16",
    hover: Annotated[float, typer.Option(help="Metres above the ramp to hang, then fall.")] = 0.6,
    settle: Annotated[
        float, typer.Option(help="Fraction of the shot spent floating before gravity.")
    ] = 0.5,
    speed: Annotated[
        float, typer.Option(help="Simulated seconds per frame, relative to real time.")
    ] = 1.0,
    phase: Annotated[
        str, typer.Option(help="Per-panel camera start angles in degrees, comma separated.")
    ] = "",
    roll: Annotated[
        str, typer.Option(help="Per-panel spin about the downhill axis, degrees, comma separated.")
    ] = "",
    runway: Annotated[
        str, typer.Option(help="Per-panel ramp length, as a multiple of the default.")
    ] = "",
) -> None:
    """Render one side-by-side loop from the given runs or exported USDZ files."""
    panels, models = [], {}
    rolls = [float(value) for value in roll.split(",") if value.strip()]
    # One angle serves every panel, or give each its own: what an object does
    # on a slope is decided by the friction its own materials author, so a
    # single incline either pins the grippy ones or throws the slick ones off.
    ramps = [float(value) for value in str(ramp).split(",") if value.strip()] or [16.0]
    runways = [float(value) for value in runway.split(",") if value.strip()]
    for index, entry in enumerate(runs):
        run, _, _ = entry.partition(":")
        identifier = f"panel{index}"
        if run.endswith(".usdz"):
            # An exported file carries its own parts and appearances, so a bare
            # USDZ is enough to render: no run directory, no main.py to import.
            path = Path(run)
            version = _read_version(path)
            models[identifier] = path
        else:
            version = load_viewer_run(Path(run)).versions[0]
            models[identifier] = load_viewer_run(Path(run)).files[str(version["id"])]
        slab = None
        if physics:
            placements, slab = _physics_poses(
                models[identifier],
                frames,
                fps=fps,
                ramp=ramps[index] if index < len(ramps) else ramps[-1],
                hover=hover,
                settle=settle,
                speed=speed,
                roll=rolls[index] if index < len(rolls) else 0.0,
                runway=runways[index] if index < len(runways) else 1.0,
            )
        elif run.endswith(".usdz"):
            raise typer.BadParameter("a bare USDZ can only be posed with --physics")
        else:
            placements = _poses(Path(run), version, frames)
        # A panel can open on a chosen side. An engine seen side on is a tube;
        # turned to face its intake, the first thing shown is the fan spinning.
        starts = [float(value) for value in phase.split(",") if value.strip()]
        panels.append(
            {
                "id": identifier,
                "model": version["model"],
                "poses": placements,
                "ramp": slab,
                "phase": starts[index] if index < len(starts) else 0.0,
            }
        )

    bootstrap = json.dumps(
        {
            "panels": panels,
            "frames": frames,
            "size": size,
            "elevation": elevation,
            "zoom": zoom,
            "supersample": supersample,
            "background": BACKGROUND,
            "fit": max(1, int(frames * settle)) if physics else frames,
        }
    ).encode()
    captured: dict[tuple[int, int], bytes] = {}
    handler, finished = _handler(bootstrap, models, captured)

    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/"
        browser = subprocess.Popen(
            [
                _chrome(),
                "--headless=new",
                "--disable-gpu",
                "--enable-unsafe-swiftshader",
                "--use-angle=swiftshader",
                "--hide-scrollbars",
                f"--window-size={size},{size}",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if not finished.wait(timeout):
                raise typer.BadParameter(
                    f"browser captured {len(captured)}/{frames * len(panels)} frames "
                    f"before the {timeout:.0f}s timeout"
                )
        finally:
            browser.terminate()

    columns = []
    for index in range(len(panels)):
        shots = []
        for frame in range(frames):
            raw = captured.get((index, frame))
            if raw is None:
                raise typer.BadParameter(f"panel {index} never sent frame {frame}")
            shots.append(Image.open(BytesIO(raw)).convert("RGB"))
        columns.append(_trim(shots, size))

    per_row = -(-len(columns) // rows)  # ceil: panels per grid row
    strips = []
    for frame in range(frames):
        row_images = []
        for start in range(0, len(columns), per_row):
            chunk = [column[frame] for column in columns[start : start + per_row]]
            width = sum(image.width for image in chunk) + gap * (len(chunk) - 1)
            strip = Image.new("RGB", (width, size), BACKGROUND)
            x = 0
            for image in chunk:
                strip.paste(image, (x, 0))
                x += image.width + gap
            row_images.append(strip)
        total = max(image.width for image in row_images)
        grid = Image.new(
            "RGB", (total, size * len(row_images) + gap * (len(row_images) - 1)), BACKGROUND
        )
        y = 0
        for strip in row_images:
            grid.paste(strip, ((total - strip.width) // 2, y))
            y += size + gap
        strips.append(grid)

    output.parent.mkdir(parents=True, exist_ok=True)
    extra = (
        # WebP keeps 24 bit colour. GIF quantises to 256 and bands every
        # gradient, which is worst on exactly the glossy dark surfaces here.
        {"quality": 92, "method": 6} if output.suffix.lower() == ".webp" else {"optimize": True}
    )
    strips[0].save(
        output,
        save_all=True,
        append_images=strips[1:],
        duration=round(1000.0 / fps),
        loop=0,
        **extra,
    )
    typer.echo(
        f"{output} ({output.stat().st_size / 1e6:.1f} MB, {len(strips)} frames, "
        f"{strips[0].width}x{strips[0].height})"
    )


if __name__ == "__main__":
    app()
