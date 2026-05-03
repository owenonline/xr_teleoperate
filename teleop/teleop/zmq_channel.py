"""ZMQ drop-in replacement for unitree_sdk2py DDS channels.

Extracted from lerobot's unitree_sdk2_socket.py. Monkey-patches
unitree_sdk2py.core.channel so all downstream code (robot_arm.py,
robot_hand_unitree.py) sends motor commands over ZMQ instead of DDS.
"""

import base64
import json
import os
import threading
from typing import Any

import zmq

LOWCMD_PORT = 6000
LOWSTATE_PORT = 6001

_ctx = None
_lowcmd_sock = None
_lowstate_sock = None
_init_pid = None
_robot_ip = "192.168.123.164"
_lowcmd_port = 6002
_latest_state = None
_state_lock = threading.Lock()
_state_thread = None


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


_HAND_TOPICS = {"rt/dex3/left/cmd", "rt/dex3/right/cmd"}

# Shared state: the last LowCmd dict sent by the arm controller.
# Hand publishers embed their data into this and re-send.
_last_lowcmd = {}
_lowcmd_lock = threading.Lock()


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


def _handcmd_to_list(msg, joint_indices):
    cmds = []
    for joint in joint_indices:
        cmds.append({
            "q": float(msg.motor_cmd[joint].q),
            "dq": float(msg.motor_cmd[joint].dq),
            "kp": float(msg.motor_cmd[joint].kp),
            "kd": float(msg.motor_cmd[joint].kd),
            "tau": float(msg.motor_cmd[joint].tau),
        })
    return cmds


class ChannelPublisher:
    def __init__(self, topic, msg_type):
        self.topic = topic
        self.msg_type = msg_type
        self.hand_cmds = None

    def Init(self):
        pass

    def Write(self, msg):
        global _last_lowcmd
        _ensure_sockets()

        if self.topic in _HAND_TOPICS:
            joint_indices = list(range(len(msg.motor_cmd)))
            hand_data = _handcmd_to_list(msg, joint_indices)
            key = "left_hand_cmd" if "left" in self.topic else "right_hand_cmd"
            with _lowcmd_lock:
                _last_lowcmd[key] = hand_data
                payload = json.dumps({"topic": "rt/lowcmd", "data": dict(_last_lowcmd)}).encode("utf-8")
        else:
            d = _lowcmd_to_dict(self.topic, msg, hand_cmds=self.hand_cmds)
            with _lowcmd_lock:
                _last_lowcmd.update(d["data"])
                payload = json.dumps({"topic": self.topic, "data": dict(_last_lowcmd)}).encode("utf-8")

        _lowcmd_sock.send(payload)


_HAND_STATE_TOPICS = {"rt/dex3/left/state", "rt/dex3/right/state"}


class HandStateMsg:
    """Mimics HandState_ from the DDS SDK."""
    class MotorState:
        def __init__(self, data):
            self.q = data.get("q", 0.0)
            self.dq = data.get("dq", 0.0)
            self.tau_est = data.get("tau_est", 0.0)

    def __init__(self, motors_data):
        self.motor_state = [self.MotorState(m) for m in motors_data]


class ChannelSubscriber:
    def __init__(self, topic, msg_type):
        self.topic = topic
        self.msg_type = msg_type

    def Init(self):
        pass

    def Read(self):
        _ensure_sockets()
        with _state_lock:
            data = _latest_state
        if data is None:
            return None

        if self.topic in _HAND_STATE_TOPICS:
            key = "left_hand_state" if "left" in self.topic else "right_hand_state"
            hand_data = data.get(key)
            if hand_data is None:
                return None
            return HandStateMsg(hand_data)

        return LowStateMsg(data)


def _state_recv_loop():
    """Background thread: continuously recv from SUB socket into _latest_state."""
    global _latest_state
    while True:
        try:
            payload = _lowstate_sock.recv()
            data = json.loads(payload.decode("utf-8")).get("data", {})
            with _state_lock:
                _latest_state = data
        except zmq.ContextTerminated:
            break
        except Exception:
            pass


def _ensure_sockets():
    """Create ZMQ sockets for the current process (handles multiprocessing fork)."""
    global _ctx, _lowcmd_sock, _lowstate_sock, _init_pid, _state_thread, _latest_state
    pid = os.getpid()
    if _init_pid == pid and _lowcmd_sock is not None:
        return
    _init_pid = pid
    _latest_state = None
    _ctx = zmq.Context()

    _lowcmd_sock = _ctx.socket(zmq.PUSH)
    _lowcmd_sock.setsockopt(zmq.CONFLATE, 1)
    _lowcmd_sock.setsockopt(zmq.LINGER, 0)
    _lowcmd_sock.connect(f"tcp://{_robot_ip}:{_lowcmd_port}")

    _lowstate_sock = _ctx.socket(zmq.SUB)
    _lowstate_sock.setsockopt(zmq.CONFLATE, 1)
    _lowstate_sock.setsockopt(zmq.LINGER, 0)
    _lowstate_sock.connect(f"tcp://{_robot_ip}:{LOWSTATE_PORT}")
    _lowstate_sock.setsockopt_string(zmq.SUBSCRIBE, "")

    _state_thread = threading.Thread(target=_state_recv_loop, daemon=True)
    _state_thread.start()


def init_zmq_channels(robot_ip="192.168.123.164", lowcmd_port=6002):
    """Initialize ZMQ sockets. Call after the module-level patch has been applied."""
    global _robot_ip, _lowcmd_port
    _robot_ip = robot_ip
    _lowcmd_port = lowcmd_port
    _ensure_sockets()
