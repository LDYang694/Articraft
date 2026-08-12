from __future__ import annotations

import math

import numpy as np
import pytest
from build123d import Box

from articraft.sdk.assembly import JointAxis, JointDOF, JointFrame, RigidBodyAssembly
from articraft.sdk.errors import LoopClosureError

GROUND_A = (-0.2, 0.0, 0.1)
GROUND_D = (0.25, 0.0, 0.1)
CRANK = 0.2
ROCKER = 0.25
POINT_B = (GROUND_A[0] + CRANK, 0.0, GROUND_A[2])
POINT_C = (GROUND_D[0], 0.0, GROUND_D[2] + ROCKER)
COUPLER = math.dist(POINT_B, POINT_C)
COUPLER_TILT = -math.atan2(POINT_C[2] - POINT_B[2], POINT_C[0] - POINT_B[0])


def four_bar() -> RigidBodyAssembly:
    assembly = RigidBodyAssembly("four_bar")
    ground = assembly.rigid_body("ground")
    crank = assembly.rigid_body("crank")
    coupler = assembly.rigid_body("coupler")
    rocker = assembly.rigid_body("rocker")
    for body, length in (
        (ground, 0.5),
        (crank, CRANK),
        (coupler, COUPLER),
        (rocker, ROCKER),
    ):
        body.add(Box(max(length, 0.05), 0.04, 0.04), name="bar")
    hinge = (JointDOF(JointAxis.ROT_Y, limits=(-math.pi, math.pi)),)
    crank_pin = assembly.joint(
        "crank_pin",
        body0=ground,
        frame0=JointFrame(xyz=GROUND_A),
        body1=crank,
        dofs=hinge,
    )
    coupler_pin = assembly.joint(
        "coupler_pin",
        body0=crank,
        frame0=JointFrame(xyz=(CRANK, 0.0, 0.0), rpy=(0.0, COUPLER_TILT, 0.0)),
        body1=coupler,
        dofs=hinge,
    )
    rocker_pin = assembly.joint(
        "rocker_pin",
        body0=ground,
        frame0=JointFrame(xyz=GROUND_D, rpy=(0.0, -math.pi / 2.0, 0.0)),
        body1=rocker,
        dofs=hinge,
    )
    assembly.joint(
        "closing_pin",
        body0=coupler,
        frame0=JointFrame(xyz=(COUPLER, 0.0, 0.0)),
        body1=rocker,
        frame1=JointFrame(xyz=(ROCKER, 0.0, 0.0)),
        dofs=hinge,
    )
    assembly.articulation(
        "main",
        root=ground,
        joints=(crank_pin, coupler_pin, rocker_pin),
    )
    return assembly


def closure_gap(assembly: RigidBodyAssembly, positions: dict[str, float]) -> float:
    resolved = assembly.resolve()
    state = resolved.forward_kinematics(positions)
    closure = resolved.get_joint("closing_pin").joint
    point0 = state.matrix(closure.body0) @ np.array([*closure.frame0.xyz, 1.0])
    point1 = state.matrix(closure.body1) @ np.array([*closure.frame1.xyz, 1.0])
    return float(np.linalg.norm(point0[:3] - point1[:3]))


def test_four_bar_stays_closed_across_its_crank() -> None:
    assembly = four_bar()

    gaps = [
        closure_gap(assembly, {"crank_pin.rotY": float(value)})
        for value in np.linspace(-0.55, 0.55, 15)
    ]

    assert max(gaps) < 1e-6


def test_unspecified_ring_coordinates_are_derived() -> None:
    resolved = four_bar().resolve()

    state = resolved.forward_kinematics({"crank_pin.rotY": 0.4})

    assert state.dof_positions["crank_pin.rotY"] == pytest.approx(0.4)
    assert abs(state.dof_positions["coupler_pin.rotY"]) > 0.01
    assert abs(state.dof_positions["rocker_pin.rotY"]) > 0.01


def test_whichever_ring_coordinate_is_supplied_leads() -> None:
    assembly = four_bar()

    assert closure_gap(assembly, {"rocker_pin.rotY": -0.2}) < 1e-6


def test_loop_solution_is_deterministic() -> None:
    resolved = four_bar().resolve()

    first = resolved.forward_kinematics({"crank_pin.rotY": 0.1})
    resolved.forward_kinematics({"crank_pin.rotY": -0.5})
    second = resolved.forward_kinematics({"crank_pin.rotY": 0.1})

    assert np.allclose(first.matrix("rocker"), second.matrix("rocker"))


def test_unreachable_loop_pose_fails_clearly() -> None:
    with pytest.raises(LoopClosureError, match="cannot reach this pose"):
        four_bar().resolve().forward_kinematics({"crank_pin.rotY": 2.6})
