import time
import multiprocessing as mp

import numpy as np

import transforms3d as t3d

from webots_ros.srv import get_bool, get_boolRequest, get_boolResponse
from webots_ros.srv import get_float, get_floatRequest, get_floatResponse
from webots_ros.srv import set_float, set_floatRequest, set_floatResponse

from multinodes import Node

from pf_drive.util import t3d_ext
from pf_drive.util import ROSContext


class WebotsRotationalMotorController:
    INFINITY = float('inf')

    def __init__(self, motor_srv, ros_context):
        # TODO: service reset when resetting simulation. 不过看起来服务句柄不会丢失.
        self.set_position_srv = motor_srv + '/set_position'
        self.set_velocity_srv = motor_srv + '/set_velocity'
        self.set_torque_srv = motor_srv + '/set_torque'

        self.ros_context = ros_context
        self.ros_context.register_service(self.set_position_srv, set_float)
        self.ros_context.register_service(self.set_velocity_srv, set_float)
        self.ros_context.register_service(self.set_torque_srv, set_float)
    
    def set_position(self, position):
        response = self.ros_context.call_service(self.set_position_srv, set_floatRequest(value = position))
        return response.success if response is not None else False

    def set_velocity(self, velocity):
        if not self.set_position(self.INFINITY):
            return False
        response = self.ros_context.call_service(self.set_velocity_srv, set_floatRequest(value = velocity))
        return response.success if response is not None else False
    
    def set_torque(self, torque):
        response = self.ros_context.call_service(self.set_torque_srv, set_floatRequest(value = torque))
        return response.success if response is not None else False


"""
    `command`, input (pipe)
        format:
            (type = 'vw', v, w)
            (type = 'vphi', v, phi)
    `odom`, output (any) [optional]
        format:
            4x4 np.array, TODO: 是否 .tolist() 减少少量长度? 需与转换耗时比较.
    Notes:
        1. Cable 建议使用 latest = True 的 pipe.
"""
class WebotsROSAckermannActuator(Node):
    def __init__(self, name, is_shutdown_event,
        left_front_steer_motor_srv,
        right_front_steer_motor_srv,
        left_rear_motor_srv,
        right_rear_motor_srv,
        get_time_srv,
        track,
        wheelbase,
        wheel_radius,
        max_steering_angle
    ):
        super().__init__(name, is_shutdown_event)
        
        self.left_front_steer_motor_srv = left_front_steer_motor_srv
        self.right_front_steer_motor_srv = right_front_steer_motor_srv
        self.left_rear_motor_srv = left_rear_motor_srv
        self.right_rear_motor_srv = right_rear_motor_srv
        self.get_time_srv = get_time_srv

        self.l = track
        self.d = wheelbase
        self.r = wheel_radius
        self.max_phi = max_steering_angle

        self.R_min_abs = self.d / np.tan(self.max_phi) + self.l / 2

        self.odom = np.eye(4)

        self.ros = ROSContext(self.name)
        self.left_front_steer_motor = WebotsRotationalMotorController(self.left_front_steer_motor_srv, self.ros)
        self.right_front_steer_motor = WebotsRotationalMotorController(self.right_front_steer_motor_srv, self.ros)
        self.left_rear_motor = WebotsRotationalMotorController(self.left_rear_motor_srv, self.ros)
        self.right_rear_motor = WebotsRotationalMotorController(self.right_rear_motor_srv, self.ros)
        self.ros.register_service(self.get_time_srv, get_float)

    def get_time(self):
        response = self.ros.call_service(self.get_time_srv, get_floatRequest(0))
        return response.value if response is not None else None
    
    def call_services(self, phi_l, phi_r, w_rear):
        self.left_front_steer_motor.set_position(phi_l)
        self.right_front_steer_motor.set_position(phi_r)
        self.left_rear_motor.set_velocity(w_rear)
        self.right_rear_motor.set_velocity(w_rear)
    
    def calc_and_output_odom(self, v, R, dt):
        dist = v * dt
        odom_R = t3d_ext.edR(self.odom)

        if R > 1e9: # inf
            self.odom[:3, 3] += np.dot(odom_R, np.array([dist, 0, 0]))
        else:
            theta = dist / R
            rot = t3d.euler.euler2mat(0, 0, theta)

            # P: robot, O: origin, R: instantaneous center of rotation, T: target of P
            PR = np.dot(odom_R, np.array([0, R, 0]))
            RT = -np.dot(rot, PR)

            self.odom[:3, :3] = np.dot(rot, odom_R)
            self.odom[:3, 3] += PR + RT
        
        self.io['odom'].write(self.odom) # 外部已经确保 odom 接口存在
    
    def run(self):
        self.ros.init_node(anonymous = False) # 不可在 __init__ 中调用，否则会导致和 main 中的 init_node 冲突

        current_time = self.get_time()
        last_time = current_time
        while not self.is_shutdown() and not self.ros.is_shutdown():
            # 检查接口
            if 'command' not in self.io.keys():
                time.sleep(0.1)
                continue

            # 接收指令
            if not self.io['command'].poll():
                continue
            command = self.io['command'].read()
            if not (isinstance(command, tuple) or isinstance(command, list)):
                continue

            # 计算时间
            current_time = self.get_time()
            if current_time is None or last_time is None:
                last_time = current_time
                continue
            dt = current_time - last_time
            last_time = current_time
            
            # 计算执行器指令
            if command[0] == 'vw':
                v, w = command[1], command[2]
                sgn = np.sign(v + 1e-3) * np.sign(w + 1e-3)
                R_min = sgn * self.R_min_abs
                R = float('inf') if abs(w) < 1e-3 else v / w
            elif command[0] == 'vphi':
                v, phi = command[1], command[2]
                sgn = np.sign(v + 1e-3) * np.sign(phi + 1e-3)
                R_min = sgn * self.R_min_abs
                R = float('inf') if abs(phi) < 1e-3 else self.d / np.tan(phi)
            
            if abs(R) < abs(R_min): # 无法实现该转角，使用最大转角
                R = R_min
            
            phi_l = np.arctan(self.d / (R + self.l / 2))
            phi_r = np.arctan(self.d / (R - self.l / 2))
            w_rear = v / self.r

            p = mp.Process(target = self.call_services, args = (phi_l, phi_r, w_rear))
            p.start()

            # 计算里程计
            if 'odom' in self.io.keys():
                # mp.Process(target = self.calc_and_output_odom, args = (v, R, dt)).start()
                self.calc_and_output_odom(v, R, dt)
        
        p.join()

