#!/usr/bin/env python

# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import os
import threading
from typing import Any

import zmq


# Module-level ZMQ state mirrors the Unitree SDK's global ChannelFactory Singleton.
# Only one robot connection per process is supported.
_ctx: zmq.Context | None = None
_lowcmd_sock: zmq.Socket | None = None
_lowstate_sock: zmq.Socket | None = None
_init_pid: int | None = None
_cfg_robot_ip: str = "192.168.123.164"

LOWCMD_PORT = int(os.environ.get("LOWCMD_PORT", "6000"))
LOWSTATE_PORT = 6001

# DDS topic names follow Unitree SDK naming conventions
# ruff: noqa: N816
kTopicLowCommand_Debug = "rt/lowcmd"


class LowStateMsg:
    """
    Wrapper class that mimics the Unitree SDK LowState_ message structure.

    Reconstructs the message from deserialized JSON data to maintain
    compatibility with existing code that expects SDK message objects.
    """

    class MotorState:
        """Motor state data for a single joint."""

        def __init__(self, data: dict[str, Any]) -> None:
            self.q: float = data.get("q", 0.0)
            self.dq: float = data.get("dq", 0.0)
            self.tau_est: float = data.get("tau_est", 0.0)
            self.temperature: float = data.get("temperature", 0.0)

    class IMUState:
        """IMU sensor data."""

        def __init__(self, data: dict[str, Any]) -> None:
            self.quaternion: list[float] = data.get("quaternion", [1.0, 0.0, 0.0, 0.0])
            self.gyroscope: list[float] = data.get("gyroscope", [0.0, 0.0, 0.0])
            self.accelerometer: list[float] = data.get("accelerometer", [0.0, 0.0, 0.0])
            self.rpy: list[float] = data.get("rpy", [0.0, 0.0, 0.0])
            self.temperature: float = data.get("temperature", 0.0)

    def __init__(self, data: dict[str, Any]) -> None:
        """Initialize from deserialized JSON data."""
        self.motor_state = [self.MotorState(m) for m in data.get("motor_state", [])]
        self.imu_state = self.IMUState(data.get("imu_state", {}))
        # Decode base64-encoded wireless_remote bytes
        wireless_b64 = data.get("wireless_remote", "")
        self.wireless_remote: bytes = base64.b64decode(wireless_b64) if wireless_b64 else b""
        self.mode_machine: int = data.get("mode_machine", 0)

        # Optional dex3 hand state (present when --dex3 is used on the server)
        self.left_hand_state: list[LowStateMsg.MotorState] | None = None
        self.right_hand_state: list[LowStateMsg.MotorState] | None = None
        if "left_hand_state" in data:
            self.left_hand_state = [self.MotorState(m) for m in data["left_hand_state"]]
        if "right_hand_state" in data:
            self.right_hand_state = [self.MotorState(m) for m in data["right_hand_state"]]


def lowcmd_to_dict(
    topic: str,
    msg: Any,
    hand_cmds: dict[str, list[dict[str, float]]] | None = None,
) -> dict[str, Any]:
    """Convert LowCmd message to a JSON-serializable dictionary.

    Args:
        topic: DDS topic name.
        msg: LowCmd SDK message.
        hand_cmds: Optional dict with "left_hand_cmd" and/or "right_hand_cmd" lists
            of motor command dicts (q, dq, kp, kd, tau) for dex3 hand control.
    """
    motor_cmds = []
    # Iterate over all motor commands in the message
    for i in range(len(msg.motor_cmd)):
        motor_cmds.append(
            {
                "mode": int(msg.motor_cmd[i].mode),
                "q": float(msg.motor_cmd[i].q),
                "dq": float(msg.motor_cmd[i].dq),
                "kp": float(msg.motor_cmd[i].kp),
                "kd": float(msg.motor_cmd[i].kd),
                "tau": float(msg.motor_cmd[i].tau),
            }
        )

    data: dict[str, Any] = {
        "mode_pr": int(msg.mode_pr),
        "mode_machine": int(msg.mode_machine),
        "motor_cmd": motor_cmds,
    }

    # Embed hand commands when dex3 is enabled
    if hand_cmds is not None:
        data.update(hand_cmds)

    return {"topic": topic, "data": data}


def _create_sockets(robot_ip: str) -> None:
    """Create fresh ZMQ sockets for the current process."""
    global _ctx, _lowcmd_sock, _lowstate_sock, _init_pid
    _init_pid = os.getpid()

    _ctx = zmq.Context()

    _lowcmd_sock = _ctx.socket(zmq.PUSH)
    _lowcmd_sock.setsockopt(zmq.CONFLATE, 1)
    _lowcmd_sock.setsockopt(zmq.LINGER, 0)
    _lowcmd_sock.connect(f"tcp://{robot_ip}:{LOWCMD_PORT}")

    _lowstate_sock = _ctx.socket(zmq.SUB)
    _lowstate_sock.setsockopt(zmq.CONFLATE, 1)
    _lowstate_sock.setsockopt(zmq.LINGER, 0)
    _lowstate_sock.connect(f"tcp://{robot_ip}:{LOWSTATE_PORT}")
    _lowstate_sock.setsockopt_string(zmq.SUBSCRIBE, "")


def _ensure_sockets() -> None:
    """Recreate sockets if we're in a forked child process."""
    if _init_pid != os.getpid():
        _create_sockets(_cfg_robot_ip)


def ChannelFactoryInitialize(domain_id: int = 0, config: Any = None) -> None:  # noqa: N802
    """
    Initialize ZMQ sockets for robot communication.

    This function mimics the Unitree SDK's ChannelFactoryInitialize but uses
    ZMQ sockets to connect to the robot server bridge instead of DDS.

    Args:
        domain_id: Ignored (for API compatibility with Unitree SDK)
        config: object with robot_ip and optional lowcmd_port attributes
    """
    global _cfg_robot_ip

    robot_ip = getattr(config, "robot_ip", "192.168.123.164") if config else "192.168.123.164"
    _cfg_robot_ip = robot_ip
    _create_sockets(robot_ip)


_HAND_CMD_TOPICS = {"rt/dex3/left/cmd", "rt/dex3/right/cmd"}
_last_lowcmd_data: dict[str, Any] = {}
_lowcmd_data_lock = threading.Lock()


def _handcmd_to_list(msg: Any) -> list[dict[str, float]]:
    """Convert a HandCmd_ message to a list of motor command dicts."""
    cmds = []
    for i in range(len(msg.motor_cmd)):
        cmds.append({
            "q": float(msg.motor_cmd[i].q),
            "dq": float(msg.motor_cmd[i].dq),
            "kp": float(msg.motor_cmd[i].kp),
            "kd": float(msg.motor_cmd[i].kd),
            "tau": float(msg.motor_cmd[i].tau),
        })
    return cmds


class ChannelPublisher:
    """ZMQ-based publisher that sends commands to the robot server."""

    def __init__(self, topic: str, msg_type: type) -> None:
        self.topic = topic
        self.msg_type = msg_type
        # Optional dex3 hand commands to embed in the next Write
        self.hand_cmds: dict[str, list[dict[str, float]]] | None = None

    def Init(self) -> None:  # noqa: N802
        """Initialize the publisher (no-op for ZMQ)."""
        pass

    def Write(self, msg: Any) -> None:  # noqa: N802
        """Serialize and send a command message to the robot."""
        _ensure_sockets()

        if self.topic in _HAND_CMD_TOPICS:
            key = "left_hand_cmd" if "left" in self.topic else "right_hand_cmd"
            hand_data = _handcmd_to_list(msg)
            with _lowcmd_data_lock:
                _last_lowcmd_data[key] = hand_data
                payload = json.dumps({"topic": "rt/lowcmd", "data": dict(_last_lowcmd_data)}).encode("utf-8")
        else:
            d = lowcmd_to_dict(self.topic, msg, hand_cmds=self.hand_cmds)
            with _lowcmd_data_lock:
                _last_lowcmd_data.update(d["data"])
            payload = json.dumps(d).encode("utf-8")

        _lowcmd_sock.send(payload)


class ChannelSubscriber:
    """ZMQ-based subscriber that receives state from the robot server."""

    def __init__(self, topic: str, msg_type: type) -> None:
        self.topic = topic
        self.msg_type = msg_type

    def Init(self) -> None:  # noqa: N802
        """Initialize the subscriber (no-op for ZMQ)."""
        pass

    def Read(self) -> LowStateMsg:  # noqa: N802
        """Receive and deserialize a state message from the robot."""
        _ensure_sockets()

        payload = _lowstate_sock.recv()
        msg_dict = json.loads(payload.decode("utf-8"))
        return LowStateMsg(msg_dict.get("data", {}))
