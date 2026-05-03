"""ZMQ drop-in replacement for unitree_sdk2py DDS channels.

Extracted from lerobot's unitree_sdk2_socket.py. Monkey-patches
unitree_sdk2py.core.channel so all downstream code (robot_arm.py,
robot_hand_unitree.py) sends motor commands over ZMQ instead of DDS.
"""

import base64
import json
import os
from typing import Any

import zmq

LOWCMD_PORT = 6000
LOWSTATE_PORT = 6001

_ctx = None
_lowcmd_sock = None
_lowstate_sock = None


class LowStateMsg:
    class MotorState:
        def __init__(self, data):
            self.q = data.get("q", 0.0)
            self.dq = data.get("dq", 0.0)
            self.tau_est = data.get("tau_est", 0.0)
            self.temperature = data.get("temperature", 0.0)

    class IMUState:
        def __init__(self, data):
            self.quaternion = data.get("quaternion", [1.0, 0.0, 0.0, 0.0])
            self.gyroscope = data.get("gyroscope", [0.0, 0.0, 0.0])
            self.accelerometer = data.get("accelerometer", [0.0, 0.0, 0.0])
            self.rpy = data.get("rpy", [0.0, 0.0, 0.0])
            self.temperature = data.get("temperature", 0.0)

    def __init__(self, data):
        self.motor_state = [self.MotorState(m) for m in data.get("motor_state", [])]
        self.imu_state = self.IMUState(data.get("imu_state", {}))
        wireless_b64 = data.get("wireless_remote", "")
        self.wireless_remote = base64.b64decode(wireless_b64) if wireless_b64 else b""
        self.mode_machine = data.get("mode_machine", 0)
        self.left_hand_state = None
        self.right_hand_state = None
        if "left_hand_state" in data:
            self.left_hand_state = [self.MotorState(m) for m in data["left_hand_state"]]
        if "right_hand_state" in data:
            self.right_hand_state = [self.MotorState(m) for m in data["right_hand_state"]]


def _lowcmd_to_dict(topic, msg, hand_cmds=None):
    motor_cmds = []
    for i in range(len(msg.motor_cmd)):
        motor_cmds.append({
            "mode": int(msg.motor_cmd[i].mode),
            "q": float(msg.motor_cmd[i].q),
            "dq": float(msg.motor_cmd[i].dq),
            "kp": float(msg.motor_cmd[i].kp),
            "kd": float(msg.motor_cmd[i].kd),
            "tau": float(msg.motor_cmd[i].tau),
        })
    data = {
        "mode_pr": int(msg.mode_pr),
        "mode_machine": int(msg.mode_machine),
        "motor_cmd": motor_cmds,
    }
    if hand_cmds is not None:
        data.update(hand_cmds)
    return {"topic": topic, "data": data}


class ChannelPublisher:
    def __init__(self, topic, msg_type):
        self.topic = topic
        self.msg_type = msg_type
        self.hand_cmds = None

    def Init(self):
        pass

    def Write(self, msg):
        if _lowcmd_sock is None:
            raise RuntimeError("init_zmq_channels must be called first")
        payload = json.dumps(_lowcmd_to_dict(self.topic, msg, hand_cmds=self.hand_cmds)).encode("utf-8")
        _lowcmd_sock.send(payload)


class ChannelSubscriber:
    def __init__(self, topic, msg_type):
        self.topic = topic
        self.msg_type = msg_type

    def Init(self):
        pass

    def Read(self):
        if _lowstate_sock is None:
            raise RuntimeError("init_zmq_channels must be called first")
        payload = _lowstate_sock.recv()
        msg_dict = json.loads(payload.decode("utf-8"))
        return LowStateMsg(msg_dict.get("data", {}))


def init_zmq_channels(robot_ip="192.168.123.164", lowcmd_port=6002):
    """Initialize ZMQ sockets and monkey-patch unitree_sdk2py.core.channel."""
    global _ctx, _lowcmd_sock, _lowstate_sock

    ctx = zmq.Context()
    _ctx = ctx

    lowcmd_sock = ctx.socket(zmq.PUSH)
    lowcmd_sock.setsockopt(zmq.CONFLATE, 1)
    lowcmd_sock.setsockopt(zmq.LINGER, 0)
    lowcmd_sock.connect(f"tcp://{robot_ip}:{lowcmd_port}")
    _lowcmd_sock = lowcmd_sock

    lowstate_sock = ctx.socket(zmq.SUB)
    lowstate_sock.setsockopt(zmq.CONFLATE, 1)
    lowstate_sock.setsockopt(zmq.LINGER, 0)
    lowstate_sock.connect(f"tcp://{robot_ip}:{LOWSTATE_PORT}")
    lowstate_sock.setsockopt_string(zmq.SUBSCRIBE, "")
    _lowstate_sock = lowstate_sock

    import unitree_sdk2py.core.channel as _ch
    _ch.ChannelPublisher = ChannelPublisher
    _ch.ChannelSubscriber = ChannelSubscriber
